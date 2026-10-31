"""Returned debits: the NSF fee, the automatic re-debit and Debit again (docs/qa/PLAN-011b D5;
docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md F2 to F4; ADR-0016).

**At the return** (:func:`on_return`, called by the transaction webhook in the return's own
transaction, once per returned ACH debit; a refund's return does nothing):

- the **NSF fee** as its own ``nsf_fee`` one-off charge at the price of the ``nsf_fee_first``
  fee type (100.00; made active by ledger migration 0063; inactive or without a price, no fee
  is made), ``source_payment`` the returned payment, due the day of the notice, collected by
  hand (``manual``: no invoice, nothing reaches the processor; staff bill or debit it with
  the charge's own actions, or waive it there); one fee per returned payment (a unique
  index), so a replayed event adds nothing; ``operators.nsf_return_count`` rises by one;
- for **R01 and R09** (:func:`payments.retryable_return`) an **automatic re-debit**: a
  ``redebit`` charge of the returned amount only, due and ``retry_date`` the third business
  day after the notice (:func:`ledger.services.business_days.add`), ``retry_state``
  ``auto_pending``, on the account's linked bank method (``autopay_due``); refused, with no
  retry, when this original debit was re-debited twice already within 180 days (the chain
  returned payment → its re-debit → that re-debit's returned payment, walked back to the
  original; Nacha's limit: the lease is flagged ``redebit_limit``, "Re-debit limit reached
  (Nacha)"), or when the account cannot be debited (no processor customer or linked bank
  method, the amount above the provider limit, the operator archived);
- every other code (and a retryable one that cannot be retried, the limit apart) flags the
  lease Needs review (``returned_debit``; the audit row's reason reads "Returned debit
  (<code>): new payment method needed"), a reason already set kept;
- template **F** (``returned_debit``) is queued at once through
  :func:`ledger.services.messaging.queue` (rule ``F``; the send window applies) about the
  charge the debit was for (a re-debit's own return: the charge of the original debit),
  with the reason in plain words (:func:`payments.return_reason_words`), the retry date,
  the debit's date and the returned amount; without a retry, its no-retry variant
  ``returned_debit_no_retry``;
- a returned automatic re-debit is ``done`` and its own returned payment resolved (the new
  return carries on); one row ``payment.return_processed`` audits what was done.

**The retry's schedule** (:func:`create_due_redebits`, the daily tick): every ``auto_pending``
re-debit due today or on the next business day gets its processor schedule, through the
existing ``schedule_create`` job, and becomes ``scheduled``; nothing reaches the processor
before that eve. One whose eve was missed (its day is already past) is re-dated to
the next business day, audited ``redebit.rescheduled``, and scheduled in the same run. A
re-debit whose payment settles (:func:`on_settled`) is ``done`` and its returned payment
resolved (``rescheduled`` on its date).

**Stop automatic retry** (:func:`stop_retry`, billing on the charge or the returns queue; and
:func:`stop_on_contact`, a staff contact entry or promise logged for the lease within two
business days of the notice): an ``auto_pending`` re-debit is voided locally ("Stopped by
staff" or "Staff contact logged"), audited ``redebit.stopped``; a ``scheduled`` one is
cancelled through the existing cancel of a one-off charge (the schedule's delete is queued
and the charge voided once the provider confirmed). After the two days the re-debit
proceeds. The return then stays in the returns queue for staff.

**Debit again** (:func:`reschedule`, billing; the returns queue): for a return without a
pending automatic re-debit, staff agree a re-debit date. In one transaction and audited it

- refuses, with nothing written: a payment that is not returned or already resolved, a
  live re-debit of it (pending, or a stop not confirmed by the provider yet), a re-debit
  date before today (New York), a fee waiver by anyone but an admin or without a note (or
  when the fee was made at the return: it is waived on its own page), an operator that
  cannot be found, is archived or has no payment provider customer, NSF fee types missing,
  a total above the confirmed provider limit, or above the large-debit threshold without
  the confirmation (a debit only);
- creates the ``redebit`` one-off charge: price the returned amount, description
  ``Re-debit of <what the payment paid>``, ``source_payment`` the returned payment, due on
  the re-debit date, on the payment's lease (when it had one) and its operator;
- when the return has no fee yet (a return before PLAN-014 F), creates the ``nsf_fee``
  charge, ``companion_of`` the re-debit: price the
  ``nsf_fee_first`` fee type's (the operator's ``nsf_return_count`` is 0) or the
  ``nsf_fee_later`` one's; ``waived`` with the note when an admin waives it; and raises
  ``operators.nsf_return_count`` by one (waived or not);
- marks the payment resolved: ``resolution_kind = rescheduled``, ``redebit_date``, the
  note (or a fixed sentence naming the date);
- queues **one** provider job for the re-debit charge: ``schedule_create`` (collection
  ``autopay_due``) when the operator has a linked bank method, otherwise a local invoice
  and ``invoice_create`` (collection ``email``). The worker sends the re-debit and, when
  it was made here, the fee (left out when waived), with the re-debit charge's reference
  (:func:`payments.one_off_invoices.charge_amounts`).

A returned installment (PLAN-012e D11) is handled the same way; its plan's status is brought
up to date once the return is resolved (:func:`ledger.services.one_off_charges.sync_plan`).
A staff void of a payment never reaches :func:`on_return` and queues no message.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

import payments
from accounts.roles import Role, meets, role_of
from finance.dates import day_short_text, day_text
from ledger import merge
from ledger.models import (
    DESCRIPTION_MAX_LENGTH,
    NSF_FEE_FIRST,
    NSF_FEE_LATER,
    ZERO,
    AuditLog,
    CollectionMethod,
    ContactEntry,
    ContactKind,
    FeeType,
    InsuranceCharge,
    Invoice,
    InvoiceStatus,
    Lease,
    MessageRule,
    MessageTemplate,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    OutboundMessage,
    Payment,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    ResolutionKind,
    RetryState,
    ReviewReason,
)
from ledger.services import business_days, leases, messaging, numbering, one_off_charges, returns
from ledger.services.payments_ledger import charge_lease
from ledger.validators import validate_no_payment_data, visible_text
from ledger.wording import format_amount, provider_sentence
from payments import jobs
from payments.models import Job, JobKind

logger = logging.getLogger(__name__)

ACTION = "payment.return_rescheduled"
#: How a re-debit charge's description begins.
REDEBIT_PREFIX = "Re-debit of "

WAIVE_ADMIN = "Only an admin can waive the NSF fee. Nothing was changed."
WAIVE_NOTE_REQUIRED = "A note is required to waive the NSF fee. Nothing was changed."
DATE_REQUIRED = "Enter the re-debit date."
DATE_IN_PAST = "The re-debit date must be today or later (New York time). Nothing was changed."
NOT_RETURNED = returns.RESOLVE_NOT_RETURNED
ALREADY = returns.RESOLVE_ALREADY
NO_OPERATOR = "The operator of this payment is unknown. Nothing was changed."
OPERATOR_ARCHIVED = "The operator is archived. Nothing was changed."
NO_PROVIDER_CUSTOMER = provider_sentence(
    "The operator is not connected to {provider}: nothing can be re-debited or invoiced. "
    "Nothing was changed."
)
FEE_TYPE_MISSING = "The NSF fee types are missing or have no amount. Nothing was changed."
PAYMENT_DATA = "This text may not contain card or account numbers."
RESCHEDULED_DEBIT = (
    "Rescheduled: re-debit of {amount} on {date} from the linked bank account, NSF fee {fee}."
)
RESCHEDULED_INVOICE = (
    "Rescheduled: invoice {number} for {amount} due {date} (the operator has no linked bank "
    "account; send it from the charge page), NSF fee {fee}."
)
FEE_WAIVED = "waived"
#: PLAN-014 F4: the fee was made at the return; Debit again adds none.
FEE_AT_RETURN = "added at the return"
DEFAULT_NOTE = "Rescheduled: re-debit on {date}."
RETRY_PENDING = (
    "An automatic re-debit is set for {date}. Stop it first to debit again on another date. "
    "Nothing was changed."
)
REDEBIT_LIVE = (
    "A re-debit of this payment is still open (a stop waits for the provider). Nothing was changed."
)
WAIVE_AT_RETURN = (
    "The NSF fee was added when the debit was returned: waive it on the fee's own page. "
    "Nothing was changed."
)


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    #: The form field a refusal belongs to (``None``: the whole form).
    field: str | None = None
    #: A refusal for the role (the view answers 403).
    forbidden: bool = False
    #: The large-debit confirmation is missing (the form shows its box).
    needs_confirm: bool = False
    redebit: OneOffCharge | None = None
    fee: OneOffCharge | None = None
    job: Job | None = None


def source_description(payment: Payment) -> str:
    """What the returned payment paid, as the statement names it."""
    if payment.rent_charge is not None:
        charge = payment.rent_charge
        return f"Rent {charge.period_month:%Y-%m}" + (
            f" (debit {charge.seq})" if charge.seq > 1 else ""
        )
    if payment.insurance_charge is not None:
        return f"Insurance, policy year {payment.insurance_charge.policy_year}"
    if payment.one_off_charge is not None:
        return payment.one_off_charge.label or "a one-off charge"
    if payment.charge_adjustment_id is not None:
        return "an adjustment"
    return "a payment"


def fee_type_for(operator: Operator) -> FeeType | None:
    """The NSF fee type of the operator's next rescheduled return, active or not."""
    code = NSF_FEE_FIRST if operator.nsf_return_count == 0 else NSF_FEE_LATER
    return FeeType.objects.filter(code=code).first()


def _money(amount: Decimal) -> str:
    return format_amount(amount)


def _text_refused(text: str) -> bool:
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return True
    return False


def _is_admin(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.ADMIN)


def reschedule(
    payment_id: uuid.UUID,
    *,
    redebit_date: date | None,
    waive_fee: bool,
    note: str,
    actor: User | None,
    confirmed: bool = False,
    today: date | None = None,
    now: datetime | None = None,
) -> Outcome:
    """Reschedule one returned payment (see the module docstring)."""
    text = visible_text(note or "")
    if waive_fee and not _is_admin(actor):
        return Outcome(False, WAIVE_ADMIN, "waive_fee", forbidden=True)
    if waive_fee and not text:
        return Outcome(False, WAIVE_NOTE_REQUIRED, "note")
    if redebit_date is None:
        return Outcome(False, DATE_REQUIRED, "redebit_date")
    day = today or timezone.localdate()
    if redebit_date < day:
        return Outcome(False, DATE_IN_PAST, "redebit_date")
    if text and _text_refused(text):
        return Outcome(False, PAYMENT_DATA, "note")
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update(of=("self",))
            .select_related(
                "invoice__operator",
                "rent_charge__lease__store",
                "rent_charge__lease__operator",
                "insurance_charge__lease__store",
                "insurance_charge__lease__operator",
                "one_off_charge__lease__store",
                "one_off_charge__lease__operator",
                "one_off_charge__operator",
                "one_off_charge__fee_type",
            )
            .get(pk=payment_id)
        )
        if payment.status != PaymentStatus.RETURNED:
            return Outcome(False, NOT_RETURNED)
        if payment.resolved_at is not None:
            return Outcome(False, ALREADY)
        billed = returns.row(payment).operator
        if billed is None:
            return Outcome(False, NO_OPERATOR)
        operator = Operator.objects.select_for_update().get(pk=billed.pk)
        if operator.archived_at is not None:
            return Outcome(False, OPERATOR_ARCHIVED)
        # PLAN-014 C5: the re-debit bills through the account of the returned charge's
        # lease (else the operator's first active lease's).
        lease = charge_lease(payment)
        account = lease if lease is not None else leases.first_active_lease(operator)
        if account is None:
            return Outcome(False, leases.NO_LEASE_BILLING)
        if not leases.customer_ref(account):
            return Outcome(False, NO_PROVIDER_CUSTOMER)
        live = live_redebit(payment)
        if live is not None:
            if live.retry_state in PENDING_STATES and live.retry_date is not None:
                return Outcome(False, RETRY_PENDING.format(date=day_text(live.retry_date)))
            return Outcome(False, REDEBIT_LIVE)
        # PLAN-014 F4: a fee made at the return is the return's only fee.
        made_fee = return_fee(payment)
        if made_fee is not None and waive_fee:
            return Outcome(False, WAIVE_AT_RETURN, "waive_fee")
        fee_type = fee_type_for(operator)
        if made_fee is None and (fee_type is None or fee_type.default_price is None):
            return Outcome(False, FEE_TYPE_MISSING)
        fee_price = (
            Decimal(fee_type.default_price)
            if made_fee is None and fee_type is not None and fee_type.default_price is not None
            else ZERO
        )
        amount = Decimal(payment.amount)
        total = amount if waive_fee or made_fee is not None else amount + fee_price
        debit = bool(leases.linked_payment_method(account))
        if debit:
            limit = leases.provider_limit_refusal([total])
            if limit:
                return Outcome(False, limit)
            if leases.is_large_debit(total) and confirmed is not True:
                return Outcome(
                    False,
                    leases.large_debit_confirmation([total]),
                    "confirm",
                    needs_confirm=True,
                )
        collection = CollectionMethod.AUTOPAY_DUE if debit else CollectionMethod.EMAIL
        what = source_description(payment)
        charge_note = text or DEFAULT_NOTE.format(date=redebit_date.isoformat())
        moment = now or timezone.now()
        redebit = OneOffCharge(
            operator=operator,
            lease=lease,
            category=OneOffCategory.REDEBIT,
            collection=collection,
            description=f"{REDEBIT_PREFIX}{what}"[:DESCRIPTION_MAX_LENGTH],
            price=amount,
            due_date=redebit_date,
            note=charge_note,
            status=OneOffChargeStatus.OWED,
            source_payment=payment,
        )
        fee = made_fee
        new_fee: OneOffCharge | None = None
        if made_fee is None and fee_type is not None:
            new_fee = OneOffCharge(
                operator=operator,
                lease=lease,
                category=OneOffCategory.NSF_FEE,
                collection=collection,
                description=f"{fee_type.name}: returned debit of {what}"[:DESCRIPTION_MAX_LENGTH],
                price=fee_price,
                due_date=redebit_date,
                note=charge_note,
                status=OneOffChargeStatus.WAIVED if waive_fee else OneOffChargeStatus.OWED,
                waived_at=moment if waive_fee else None,
                waived_by=actor if waive_fee else None,
                waive_note=text if waive_fee else "",
            )
            fee = new_fee
        try:
            with transaction.atomic():
                redebit.save()
                if new_fee is not None:
                    new_fee.companion_of = redebit
                    new_fee.save()
        except ValidationError:
            return Outcome(False, PAYMENT_DATA, "note")
        invoice = None
        if collection == CollectionMethod.EMAIL:
            invoice = Invoice.objects.create(
                invoice_number=numbering.next_invoice_number(day.year),
                operator=operator,
                one_off_charge=redebit,
                issue_date=day,
                due_date=max(redebit_date, day),
                memo=redebit.description,
                subtotal=total,
                tax=ZERO,
                fees=ZERO,
                surcharge=ZERO,
                total=total,
                internal_status=InvoiceStatus.DRAFT,
                allow_card=False,
                allow_ach=True,
                legacy=False,
            )
        count_before = operator.nsf_return_count
        if new_fee is not None:
            operator.nsf_return_count = count_before + 1
            operator.save(update_fields=["nsf_return_count", "updated_at"])
        payment.resolved_at = moment
        payment.resolved_by = actor
        payment.resolution_note = charge_note
        payment.resolution_kind = ResolutionKind.RESCHEDULED
        payment.redebit_date = redebit_date
        payment.save(
            update_fields=[
                "resolved_at",
                "resolved_by",
                "resolution_note",
                "resolution_kind",
                "redebit_date",
                "updated_at",
            ]
        )
        for charge in (redebit, *([new_fee] if new_fee is not None else [])):
            AuditLog.objects.create(
                actor=actor,
                action=one_off_charges.CREATED_ACTION,
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(charge.pk),
                before=None,
                after={
                    **one_off_charges.charge_state(charge),
                    "note": charge.note,
                    "source_payment": str(payment.pk),
                    "invoice_number": (
                        invoice.invoice_number
                        if invoice is not None and charge is redebit
                        else None
                    ),
                },
            )
        if debit and leases.is_large_debit(total):
            AuditLog.objects.create(
                actor=actor,
                action=one_off_charges.LARGE_DEBIT_ACTION,
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(redebit.pk),
                before=None,
                after={
                    "amount": str(total),
                    "threshold": str(settings.LARGE_DEBIT_WARNING_AMOUNT),
                    "confirmed_by": actor.pk if actor is not None else None,
                },
            )
        AuditLog.objects.create(
            actor=actor,
            action=ACTION,
            entity_type=returns.ENTITY_PAYMENT,
            entity_id=str(payment.pk),
            before={
                "status": payment.status,
                "resolved_at": None,
                "nsf_return_count": count_before,
            },
            after={
                "status": payment.status,
                "resolved_at": moment.isoformat(),
                "resolution_kind": payment.resolution_kind,
                "redebit_date": redebit_date.isoformat(),
                "return_code": payment.return_code,
                "resolution_note": charge_note,
                "operator": str(operator.pk),
                "nsf_return_count": operator.nsf_return_count,
                "redebit_charge": str(redebit.pk),
                "fee_charge": str(fee.pk) if fee is not None else None,
                "fee_type": fee_type.code if new_fee is not None and fee_type else None,
                "fee": str(fee_price) if new_fee is not None else None,
                "fee_at_return": made_fee is not None,
                "fee_waived": waive_fee,
                "collection": collection,
                "total": str(total),
            },
        )
        kind = JobKind.SCHEDULE_CREATE if debit else JobKind.INVOICE_CREATE
        job = jobs.enqueue(kind, redebit, None, actor)
        if payment.one_off_charge is not None and payment.one_off_charge.plan_id is not None:
            # PLAN-012e D11: the return of an installment is resolved; its plan leaves
            # ``attention`` once no other return of it is open.
            one_off_charges.sync_plan(payment.one_off_charge.plan_id, actor=actor)
    if made_fee is not None:
        fee_text = FEE_AT_RETURN
    else:
        fee_text = FEE_WAIVED if waive_fee else _money(fee_price)
    if debit:
        message = RESCHEDULED_DEBIT.format(
            amount=_money(total), date=day_text(redebit_date), fee=fee_text
        )
    else:
        number = invoice.invoice_number if invoice is not None else ""
        message = RESCHEDULED_INVOICE.format(
            number=number, amount=_money(total), date=day_text(redebit_date), fee=fee_text
        )
    return Outcome(True, message, redebit=redebit, fee=fee, job=job)


# --- At the return (PLAN-014 F2) ----------------------------------------------------------

#: The audit row of what :func:`on_return` did with one returned payment.
RETURN_ACTION = "payment.return_processed"
STOPPED_ACTION = "redebit.stopped"
SCHEDULED_ACTION = "redebit.scheduled"
RESCHEDULED_ACTION = "redebit.rescheduled"
DONE_ACTION = "redebit.done"
FLAGGED_ACTION = "lease.flagged_for_review"
#: The automatic re-debit's day: this many business days after the notice.
RETRY_BUSINESS_DAYS = 3
#: Staff stop it by a contact logged up to this many business days after the notice.
STOP_BUSINESS_DAYS = 2
#: At most this many re-debits of one original debit within :data:`LIMIT_DAYS` (Nacha).
LIMIT_COUNT = 2
LIMIT_DAYS = 180
#: The longest chain of returned re-debits walked back to the original (a guard only).
CHAIN_MAX = 20
#: The templates of rule F (plan §9).
TEMPLATE_RETRY = "returned_debit"
TEMPLATE_NO_RETRY = "returned_debit_no_retry"
#: The states of a re-debit that has not been tried yet.
PENDING_STATES: tuple[str, ...] = (RetryState.AUTO_PENDING, RetryState.SCHEDULED)

#: Why a returned debit gets no automatic re-debit (the audit row's ``retry``).
RETRY_AUTO = "auto"
NO_RETRY_CODE = "code"
NO_RETRY_LIMIT = "limit"
NO_RETRY_ACCOUNT = "account"

REVIEW_RETURNED = "Returned debit ({code}): new payment method needed"
REVIEW_LIMIT = "Re-debit limit reached (Nacha)"
NO_CODE = "no code"
#: A code that is not an ACH return code's shape is never copied into a charge's text.
OTHER_CODE = "unlisted code"
_CODE_SHAPE = re.compile(r"R[0-9]{2}")
FEE_NOTE = "Returned debit ({code}): {reason}."
REDEBIT_NOTE = "Automatic re-debit of the returned debit ({code}) on {date}."
STOPPED_BY_STAFF = "Stopped by staff"
STOPPED_BY_CONTACT = "Staff contact logged"
STOPPED = "The automatic re-debit is stopped. The return stays in the returns queue."
STOP_CANCEL_QUEUED = (
    "The automatic re-debit is stopped: its schedule's cancel is queued, and the charge is "
    "voided once the provider confirms."
)
NOTHING_TO_STOP = "This charge has no automatic re-debit to stop. Nothing was changed."
STOP_REFUSED = "The automatic re-debit could not be stopped: {reason}"
RESOLVED_SETTLED = "Re-debited automatically on {date}."
RESOLVED_RETURNED = "The automatic re-debit of {date} was returned too ({code}); see its return."

#: The returns queue's retry column (F4).
STATE_PENDING = "Automatic re-debit on {date}"
STATE_STOPPED = "Stopped"
STATE_DONE = "Re-debited"
STATE_NO_RETRY = "No retry: {code}"
STATE_LIMIT = "No retry: re-debit limit reached"
STATE_ACCOUNT = "No retry: no bank account to debit"


def shown(code: str) -> str:
    """The return code as charges, notes and the queue name it: ``R10``; "no code"; or
    "unlisted code" for text of another shape (never copied into stored text)."""
    if not code:
        return NO_CODE
    return code if _CODE_SHAPE.fullmatch(code) else OTHER_CODE


@dataclass(frozen=True, slots=True)
class ReturnOutcome:
    """What :func:`on_return` made of one returned payment."""

    fee: OneOffCharge | None
    redebit: OneOffCharge | None
    retry: str
    message: OutboundMessage | None


def return_fee(payment: Payment) -> OneOffCharge | None:
    """The NSF fee charge naming the returned payment, if one was made."""
    return OneOffCharge.objects.filter(
        source_payment=payment, category=OneOffCategory.NSF_FEE
    ).first()


def live_redebit(payment: Payment) -> OneOffCharge | None:
    """The re-debit of the returned payment that is not voided, if any."""
    return (
        OneOffCharge.objects.filter(source_payment=payment, category=OneOffCategory.REDEBIT)
        .exclude(status=OneOffChargeStatus.VOIDED)
        .first()
    )


def chain_redebits(payment: Payment) -> list[OneOffCharge]:
    """The re-debits a returned payment descends from, nearest first: the payment's own
    charge when it is a re-debit of a return, that re-debit's returned payment's, and so on
    back to the original debit."""
    found: list[OneOffCharge] = []
    current = payment
    for _ in range(CHAIN_MAX):
        charge = current.one_off_charge if current.one_off_charge_id is not None else None
        if charge is None or charge.category != OneOffCategory.REDEBIT:
            break
        source = charge.source_payment_id
        if source is None:
            break
        found.append(charge)
        current = Payment.objects.select_related("one_off_charge").get(pk=source)
    return found


def limit_reached(payment: Payment, today: date) -> bool:
    """Whether the original debit behind ``payment`` was re-debited :data:`LIMIT_COUNT`
    times within the last :data:`LIMIT_DAYS` days."""
    since = today - timedelta(days=LIMIT_DAYS)
    recent = [
        charge
        for charge in chain_redebits(payment)
        if timezone.localdate(charge.created_at) >= since
    ]
    return len(recent) >= LIMIT_COUNT


def message_charge(payment: Payment) -> messaging.Charge | None:
    """The charge a returned debit's message is about: the charge the debit was for; for a
    re-debit (of a return, or a rent charge's bank debit) the charge of the original."""
    current = payment
    for _ in range(CHAIN_MAX):
        if current.rent_charge_id is not None:
            return RentCharge.objects.select_related("lease__store").get(pk=current.rent_charge_id)
        if current.insurance_charge_id is not None:
            return InsuranceCharge.objects.select_related("lease__store").get(
                pk=current.insurance_charge_id
            )
        if current.one_off_charge_id is None:
            return None
        charge = OneOffCharge.objects.select_related("lease__store", "operator").get(
            pk=current.one_off_charge_id
        )
        if charge.category == OneOffCategory.REDEBIT and charge.source_rent_charge_id is not None:
            return RentCharge.objects.select_related("lease__store").get(
                pk=charge.source_rent_charge_id
            )
        source = charge.source_payment_id
        if charge.category != OneOffCategory.REDEBIT or source is None:
            return charge
        current = Payment.objects.get(pk=source)
    return None


def _account_refusal(account: Lease | None, operator: Operator, amount: Decimal) -> bool:
    """Whether the automatic re-debit cannot be made on ``account``."""
    return (
        account is None
        or operator.archived_at is not None
        or not leases.customer_ref(account)
        or not leases.linked_payment_method(account)
        or bool(leases.provider_limit_refusal([amount]))
    )


def _flag_lease(lease: Lease | None, reason: ReviewReason, text: str, payment: Payment) -> None:
    """Flag the lease Needs review (a reason already set is kept), audited."""
    if lease is None:
        return
    row = Lease.objects.select_for_update(of=("self",)).get(pk=lease.pk)
    before = {"needs_review": row.needs_review, "review_reason": row.review_reason}
    row.needs_review = True
    if not row.review_reason:
        row.review_reason = reason
    row.save(update_fields=["needs_review", "review_reason", "updated_at"])
    AuditLog.objects.create(
        actor=None,
        action=FLAGGED_ACTION,
        entity_type="lease",
        entity_id=str(row.pk),
        before=before,
        after={
            "needs_review": True,
            "review_reason": row.review_reason,
            "reason": text,
            "payment": str(payment.pk),
        },
    )


def _resolve_source(charge: OneOffCharge, note: str, now: datetime) -> None:
    """Resolve the returned payment an automatic re-debit collected again, once the
    re-debit is done (settled or returned in its turn); nothing when staff resolved it."""
    if charge.source_payment_id is None:
        return
    source = Payment.objects.select_for_update(of=("self",)).get(pk=charge.source_payment_id)
    if source.status != PaymentStatus.RETURNED or source.resolved_at is not None:
        return
    source.resolved_at = now
    source.resolved_by = None
    source.resolution_note = note
    source.resolution_kind = ResolutionKind.RESCHEDULED
    source.redebit_date = charge.retry_date or charge.due_date
    source.save(
        update_fields=[
            "resolved_at",
            "resolved_by",
            "resolution_note",
            "resolution_kind",
            "redebit_date",
            "updated_at",
        ]
    )
    AuditLog.objects.create(
        actor=None,
        action=ACTION,
        entity_type=returns.ENTITY_PAYMENT,
        entity_id=str(source.pk),
        before={"status": source.status, "resolved_at": None},
        after={
            "status": source.status,
            "resolved_at": now.isoformat(),
            "resolution_kind": source.resolution_kind,
            "redebit_date": source.redebit_date.isoformat(),
            "return_code": source.return_code,
            "resolution_note": note,
            "redebit_charge": str(charge.pk),
            "automatic": True,
        },
    )
    if source.one_off_charge_id is not None:
        plan_id = (
            OneOffCharge.objects.filter(pk=source.one_off_charge_id)
            .values_list("plan_id", flat=True)
            .first()
        )
        if plan_id is not None:
            one_off_charges.sync_plan(plan_id, actor=None)


def _finish_retry(payment: Payment, note: str, now: datetime, code: str = "") -> None:
    """The payment of an automatic re-debit settled or was returned: the re-debit is
    ``done`` and its returned payment resolved."""
    if payment.one_off_charge_id is None:
        return
    charge = OneOffCharge.objects.select_for_update(of=("self",)).get(pk=payment.one_off_charge_id)
    if charge.retry_state not in PENDING_STATES:
        return
    before = charge.retry_state
    charge.retry_state = RetryState.DONE
    charge.save(update_fields=["retry_state", "updated_at"])
    AuditLog.objects.create(
        actor=None,
        action=DONE_ACTION,
        entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
        entity_id=str(charge.pk),
        before={"retry_state": before},
        after={"retry_state": charge.retry_state, "payment": str(payment.pk)},
    )
    day = day_text(charge.retry_date or charge.due_date)
    _resolve_source(charge, note.format(date=day, code=code), now)


def _queue_message(
    payment: Payment,
    account: Lease | None,
    code: str,
    words: str,
    retry_date: date | None,
    today: date,
) -> OutboundMessage | None:
    """Template F (or its no-retry variant) about the debit's charge, at once; ``None``
    when there is no lease or charge to name, the template is inactive, or it cannot be
    rendered (logged by class name)."""
    charge = message_charge(payment)
    if account is None or charge is None:
        return None
    code_name = TEMPLATE_RETRY if retry_date is not None else TEMPLATE_NO_RETRY
    template = MessageTemplate.objects.filter(code=code_name, active=True).first()
    if template is None:
        logger.warning("Template %s inactive: no message for payment %s", code_name, payment.pk)
        return None
    occurrence = (
        OutboundMessage.objects.filter(
            lease=account,
            charge_entity_type=messaging.charge_type_of(charge),
            charge_entity_id=charge.pk,
            rule=MessageRule.F,
            manual=False,
        ).count()
        + 1
    )
    context = messaging.context_for(
        account,
        charge,
        MessageRule.F,
        today=today,
        amount=Decimal(payment.amount),
        debit_date=payment.initiated_on,
        retry_date=retry_date,
        return_reason=words,
    )
    try:
        return messaging.queue(
            MessageRule.F, account, charge, template, context, occurrence=occurrence
        )
    except (messaging.ChannelUnavailable, merge.TemplateRefused, ValueError) as error:
        logger.warning(
            "Returned-debit message not queued for payment %s: %s",
            payment.pk,
            type(error).__name__,
        )
        return None


def on_return(payment: Payment, today: date, now: datetime | None = None) -> ReturnOutcome | None:
    """The fee, the automatic re-debit or the Needs review flag, and template F, for one
    returned ACH debit (see the module docstring); ``None`` and nothing written for a
    refund, a payment not returned, one processed already, or one without an operator.
    Runs in the caller's transaction, which holds the payment's row lock."""
    if (
        payment.is_refund
        or payment.method != PaymentMethod.ACH
        or payment.status != PaymentStatus.RETURNED
        or return_fee(payment) is not None
        or AuditLog.objects.filter(
            action=RETURN_ACTION, entity_type=returns.ENTITY_PAYMENT, entity_id=str(payment.pk)
        ).exists()
    ):
        return None
    billed = returns.row(payment).operator
    if billed is None:
        logger.warning("Returned payment %s has no operator: nothing made", payment.pk)
        return None
    moment = now or timezone.now()
    with transaction.atomic():
        operator = Operator.objects.select_for_update(of=("self",)).get(pk=billed.pk)
        lease = charge_lease(payment)
        account = lease if lease is not None else leases.first_active_lease(operator)
        code = (payment.return_code or "").strip().upper()
        shown_code = shown(code)
        words = payments.return_reason_words(code)
        what = source_description(payment)
        amount = Decimal(payment.amount)
        # A returned automatic re-debit is done; its own return carries on below.
        _finish_retry(payment, RESOLVED_RETURNED, moment, shown_code)
        fee: OneOffCharge | None = None
        fee_type = FeeType.objects.filter(code=NSF_FEE_FIRST, active=True).first()
        if fee_type is not None and fee_type.default_price is not None:
            fee = OneOffCharge.objects.create(
                operator=operator,
                lease=lease,
                category=OneOffCategory.NSF_FEE,
                collection=CollectionMethod.MANUAL,
                description=(f"{OneOffCategory.NSF_FEE.label}: returned debit of {what}")[
                    :DESCRIPTION_MAX_LENGTH
                ],
                price=Decimal(fee_type.default_price),
                due_date=today,
                note=FEE_NOTE.format(code=shown_code, reason=words),
                status=OneOffChargeStatus.OWED,
                source_payment=payment,
            )
            count_before = operator.nsf_return_count
            operator.nsf_return_count = count_before + 1
            operator.save(update_fields=["nsf_return_count", "updated_at"])
            AuditLog.objects.create(
                actor=None,
                action=one_off_charges.CREATED_ACTION,
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(fee.pk),
                before=None,
                after={
                    **one_off_charges.charge_state(fee),
                    "note": fee.note,
                    "source_payment": str(payment.pk),
                    "fee_type": fee_type.code,
                    "nsf_return_count": operator.nsf_return_count,
                    "invoice_number": None,
                },
            )
        else:
            logger.warning("NSF fee type inactive or without a price: no fee for %s", payment.pk)
        if not payments.retryable_return(code):
            retry = NO_RETRY_CODE
        elif limit_reached(payment, today):
            retry = NO_RETRY_LIMIT
        elif _account_refusal(account, operator, amount):
            retry = NO_RETRY_ACCOUNT
        else:
            retry = RETRY_AUTO
        redebit: OneOffCharge | None = None
        retry_date: date | None = None
        if retry == RETRY_AUTO:
            retry_date = business_days.add(today, RETRY_BUSINESS_DAYS)
            redebit = OneOffCharge.objects.create(
                operator=operator,
                lease=lease,
                category=OneOffCategory.REDEBIT,
                collection=CollectionMethod.AUTOPAY_DUE,
                description=f"{REDEBIT_PREFIX}{what}"[:DESCRIPTION_MAX_LENGTH],
                price=amount,
                due_date=retry_date,
                note=REDEBIT_NOTE.format(code=shown_code, date=day_text(retry_date)),
                status=OneOffChargeStatus.OWED,
                source_payment=payment,
                retry_date=retry_date,
                retry_state=RetryState.AUTO_PENDING,
            )
            AuditLog.objects.create(
                actor=None,
                action=one_off_charges.CREATED_ACTION,
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(redebit.pk),
                before=None,
                after={
                    **one_off_charges.charge_state(redebit),
                    "note": redebit.note,
                    "source_payment": str(payment.pk),
                    "retry_date": retry_date.isoformat(),
                    "retry_state": redebit.retry_state,
                    "invoice_number": None,
                },
            )
        elif retry == NO_RETRY_LIMIT:
            _flag_lease(account, ReviewReason.REDEBIT_LIMIT, REVIEW_LIMIT, payment)
        else:
            _flag_lease(
                account,
                ReviewReason.RETURNED_DEBIT,
                REVIEW_RETURNED.format(code=shown_code),
                payment,
            )
        message = _queue_message(payment, account, code, words, retry_date, today)
        AuditLog.objects.create(
            actor=None,
            action=RETURN_ACTION,
            entity_type=returns.ENTITY_PAYMENT,
            entity_id=str(payment.pk),
            before=None,
            after={
                "return_code": code,
                "return_reason_words": words,
                "fee_charge": str(fee.pk) if fee is not None else None,
                "retry": retry,
                "retry_date": retry_date.isoformat() if retry_date is not None else None,
                "redebit_charge": str(redebit.pk) if redebit is not None else None,
                "message": str(message.pk) if message is not None else None,
                "lease": str(account.pk) if account is not None else None,
            },
        )
    return ReturnOutcome(fee=fee, redebit=redebit, retry=retry, message=message)


def on_settled(payment: Payment, now: datetime | None = None) -> None:
    """A settled payment of an automatic re-debit: the re-debit is ``done`` and its
    returned payment resolved. Nothing for any other payment."""
    if payment.is_refund or payment.status != PaymentStatus.SETTLED:
        return
    with transaction.atomic():
        _finish_retry(payment, RESOLVED_SETTLED, now or timezone.now())


# --- The retry's schedule (F3) --------------------------------------------------------------


def _redate_missed(today: date, eve: date) -> None:
    """Re-date every ``auto_pending`` re-debit whose day is already past (its eve was
    missed: no tick that day, or the return came after it) to ``eve``, the next business
    day; audited ``redebit.rescheduled`` with both dates. Its stop window is already past."""
    missed = list(
        OneOffCharge.objects.filter(
            category=OneOffCategory.REDEBIT,
            retry_state=RetryState.AUTO_PENDING,
            retry_date__lt=today,
        )
        .exclude(status=OneOffChargeStatus.VOIDED)
        .order_by("retry_date", "created_at", "id")
        .values_list("pk", flat=True)
    )
    for pk in missed:
        with transaction.atomic():
            charge = OneOffCharge.objects.select_for_update(of=("self",)).get(pk=pk)
            if (
                charge.retry_state != RetryState.AUTO_PENDING
                or charge.status == OneOffChargeStatus.VOIDED
                or charge.retry_date is None
                or charge.retry_date >= today
            ):
                continue
            before = {
                "retry_date": charge.retry_date.isoformat(),
                "due_date": charge.due_date.isoformat(),
            }
            charge.retry_date = eve
            charge.due_date = eve
            charge.save(update_fields=["retry_date", "due_date", "updated_at"])
            AuditLog.objects.create(
                actor=None,
                action=RESCHEDULED_ACTION,
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(charge.pk),
                before=before,
                after={"retry_date": eve.isoformat(), "due_date": eve.isoformat()},
            )


def create_due_redebits(today: date) -> int:
    """Queue the processor schedule of every ``auto_pending`` re-debit due today or on the
    next business day (the eve), through the ``schedule_create`` job; each becomes
    ``scheduled``, audited ``redebit.scheduled``. A re-debit whose day is already past (its
    eve was missed) is first re-dated to the next business day (:func:`_redate_missed`), so
    it is scheduled in the same run. How many were queued; a second run the same day queues
    nothing."""
    eve = business_days.add(today, 1)
    _redate_missed(today, eve)
    candidates = list(
        OneOffCharge.objects.filter(
            category=OneOffCategory.REDEBIT,
            retry_state=RetryState.AUTO_PENDING,
            retry_date__gte=today,
            retry_date__lte=eve,
        )
        .exclude(status=OneOffChargeStatus.VOIDED)
        .order_by("retry_date", "created_at", "id")
        .values_list("pk", flat=True)
    )
    queued = 0
    for pk in candidates:
        with transaction.atomic():
            charge = OneOffCharge.objects.select_for_update(of=("self",)).get(pk=pk)
            if (
                charge.retry_state != RetryState.AUTO_PENDING
                or charge.status == OneOffChargeStatus.VOIDED
            ):
                continue
            charge.retry_state = RetryState.SCHEDULED
            charge.save(update_fields=["retry_state", "updated_at"])
            job = jobs.enqueue(JobKind.SCHEDULE_CREATE, charge, None, None)
            AuditLog.objects.create(
                actor=None,
                action=SCHEDULED_ACTION,
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(charge.pk),
                before={"retry_state": RetryState.AUTO_PENDING},
                after={
                    "retry_state": charge.retry_state,
                    "retry_date": charge.retry_date.isoformat() if charge.retry_date else None,
                    "job": str(job.pk),
                },
            )
            queued += 1
    return queued


# --- Stop automatic retry (F3) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StopOutcome:
    ok: bool
    message: str


def stop_retry(
    charge: OneOffCharge | uuid.UUID,
    actor: User | None,
    reason: str = STOPPED_BY_STAFF,
    *,
    now: datetime | None = None,
) -> StopOutcome:
    """Stop an automatic re-debit (see the module docstring): voided locally while it is
    ``auto_pending``; while ``scheduled``, its schedule's cancel is queued through the
    one-off charge's cancel (refused, with nothing changed, once a payment of it exists)."""
    pk = charge.pk if isinstance(charge, OneOffCharge) else charge
    moment = now or timezone.now()
    with transaction.atomic():
        row = OneOffCharge.objects.select_for_update(of=("self",)).get(pk=pk)
        if row.retry_state not in PENDING_STATES or row.status == OneOffChargeStatus.VOIDED:
            return StopOutcome(False, NOTHING_TO_STOP)
        before = {
            **one_off_charges.charge_state(row),
            "retry_state": row.retry_state,
        }
        if row.retry_state == RetryState.SCHEDULED:
            refusal = one_off_charges.cancel_refusal(row)
            if refusal:
                return StopOutcome(False, STOP_REFUSED.format(reason=refusal))
            row.retry_state = RetryState.STOPPED
            row.save(update_fields=["retry_state", "updated_at"])
            cancelled = one_off_charges.cancel_charge(row.pk, note=reason, actor=actor)
            if not cancelled.ok:
                transaction.set_rollback(True)
                return StopOutcome(False, STOP_REFUSED.format(reason=cancelled.message))
            text = STOP_CANCEL_QUEUED
        else:
            row.retry_state = RetryState.STOPPED
            row.status = OneOffChargeStatus.VOIDED
            row.voided_at = moment
            row.voided_by = actor
            row.void_note = reason
            row.save(
                update_fields=[
                    "retry_state",
                    "status",
                    "voided_at",
                    "voided_by",
                    "void_note",
                    "updated_at",
                ]
            )
            text = STOPPED
        AuditLog.objects.create(
            actor=actor,
            action=STOPPED_ACTION,
            entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
            entity_id=str(row.pk),
            before=before,
            after={
                **one_off_charges.charge_state(row),
                "retry_state": row.retry_state,
                "reason": reason,
                "source_payment": str(row.source_payment_id),
            },
        )
    return StopOutcome(True, text)


#: The contact entries that stop an automatic re-debit.
STOPPING_KINDS: frozenset[str] = frozenset(
    {ContactKind.CALL, ContactKind.EMAIL, ContactKind.VISIT, ContactKind.PROMISE}
)


def stop_on_contact(entry: ContactEntry, actor: User | None, today: date | None = None) -> int:
    """Stop the pending automatic re-debits of the entry's lease (of its operator, for an
    entry without a lease) whose return's notice is at most :data:`STOP_BUSINESS_DAYS`
    business days before today: a staff call, e-mail, visit or promise. How many stopped.
    Runs in the caller's transaction."""
    if actor is None or entry.kind not in STOPPING_KINDS:
        return 0
    day = today or timezone.localdate()
    rows = (
        OneOffCharge.objects.filter(
            category=OneOffCategory.REDEBIT,
            retry_state__in=PENDING_STATES,
            operator=entry.operator_id,
        )
        .exclude(status=OneOffChargeStatus.VOIDED)
        .select_related("source_payment")
        .order_by("retry_date", "id")
    )
    if entry.lease_id is not None:
        rows = rows.filter(Q(lease=entry.lease_id) | Q(lease__isnull=True))
    stopped = 0
    for row in rows:
        returned_at = row.source_payment.returned_at if row.source_payment else None
        if returned_at is None:
            continue
        notice = timezone.localdate(returned_at)
        if not notice <= day <= business_days.add(notice, STOP_BUSINESS_DAYS):
            continue
        if stop_retry(row.pk, actor, STOPPED_BY_CONTACT).ok:
            stopped += 1
    return stopped


# --- The returns queue (F4) -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueState:
    """What the returns queue shows of one return's retry (F4)."""

    #: "Automatic re-debit on Nov 3", "Stopped", "No retry: R10"; ``""`` for a return
    #: :func:`on_return` never processed.
    text: str = ""
    #: The automatic re-debit still to be tried ("Stop automatic retry").
    pending: uuid.UUID | None = None
    #: The fee made at the return (waived on its own page; Debit again adds none).
    fee: uuid.UUID | None = None


def queue_states(found: Iterable[Payment]) -> dict[uuid.UUID, QueueState]:
    """The :class:`QueueState` of each returned payment of the queue. Three queries."""
    rows = list(found)
    ids = [payment.pk for payment in rows]
    codes = {payment.pk: (payment.return_code or "").strip() for payment in rows}
    texts: dict[uuid.UUID, str] = {}
    pending: dict[uuid.UUID, uuid.UUID] = {}
    for entity_id, after in AuditLog.objects.filter(
        action=RETURN_ACTION,
        entity_type=returns.ENTITY_PAYMENT,
        entity_id__in=[str(pk) for pk in ids],
    ).values_list("entity_id", "after"):
        key = uuid.UUID(entity_id)
        retry = (after or {}).get("retry")
        if retry == NO_RETRY_CODE:
            texts[key] = STATE_NO_RETRY.format(code=shown((codes.get(key) or "").upper()))
        elif retry == NO_RETRY_LIMIT:
            texts[key] = STATE_LIMIT
        elif retry == NO_RETRY_ACCOUNT:
            texts[key] = STATE_ACCOUNT
    for pk, source, state, retry_date, status in (
        OneOffCharge.objects.filter(source_payment__in=ids, category=OneOffCategory.REDEBIT)
        .exclude(retry_state="")
        .order_by("created_at", "id")
        .values_list("pk", "source_payment", "retry_state", "retry_date", "status")
    ):
        live = status != OneOffChargeStatus.VOIDED
        if state in PENDING_STATES and retry_date is not None and live:
            texts[source] = STATE_PENDING.format(date=day_short_text(retry_date))
            pending[source] = pk
        elif state in (RetryState.STOPPED, *PENDING_STATES):
            texts[source] = STATE_STOPPED
        elif state == RetryState.DONE:
            texts[source] = STATE_DONE
    fees = dict(
        OneOffCharge.objects.filter(
            source_payment__in=ids, category=OneOffCategory.NSF_FEE
        ).values_list("source_payment", "pk")
    )
    return {
        pk: QueueState(text=texts.get(pk, ""), pending=pending.get(pk), fee=fees.get(pk))
        for pk in ids
    }


def pending_return_ids() -> set[uuid.UUID]:
    """The returned payments whose automatic re-debit is still to be tried (one query): the
    attention list leaves them to ``redebit_pending``."""
    return set(
        OneOffCharge.objects.filter(
            category=OneOffCategory.REDEBIT,
            retry_state__in=PENDING_STATES,
            source_payment__isnull=False,
        )
        .exclude(status=OneOffChargeStatus.VOIDED)
        .values_list("source_payment", flat=True)
    )


LINE_PENDING = "Automatic re-debit on {date}, unless staff stop it before then."
LINE_STOPPED = "Automatic re-debit stopped."
LINE_DONE = "Automatic re-debit done."


def can_stop(charge: OneOffCharge) -> bool:
    """Whether the charge is an automatic re-debit still to be tried (no query)."""
    return charge.retry_state in PENDING_STATES and charge.status != OneOffChargeStatus.VOIDED


def retry_line(charge: OneOffCharge) -> str:
    """The charge page's line about its automatic re-debit, or ``""`` (no query)."""
    if can_stop(charge) and charge.retry_date is not None:
        return LINE_PENDING.format(date=day_text(charge.retry_date))
    if charge.retry_state == RetryState.STOPPED or (
        charge.retry_state in PENDING_STATES and charge.status == OneOffChargeStatus.VOIDED
    ):
        return LINE_STOPPED
    if charge.retry_state == RetryState.DONE:
        return LINE_DONE
    return ""
