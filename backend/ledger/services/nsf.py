"""Reschedule a returned debit, with the NSF fee (docs/qa/PLAN-011b D5).

A returned ACH debit waits in the returns queue until staff resolve it. Besides the note
of PLAN-008 (:func:`ledger.services.returns.resolve_return`), staff (billing) may
**Reschedule** it: the operator agreed a re-debit date. :func:`reschedule`, in one
transaction and audited:

- refuses, with nothing written: a payment that is not returned or already resolved, a
  re-debit date before today (New York), a fee waiver by anyone but an admin or without
  a note, an operator that cannot be found, is archived or has no payment provider
  customer, NSF fee types missing, a total above the confirmed provider limit, or above
  the large-debit threshold without the confirmation (a debit only);
- creates the ``redebit`` one-off charge: price the returned amount, description
  ``Re-debit of <what the payment paid>``, ``source_payment`` the returned payment, due on
  the re-debit date, on the payment's lease (when it had one) and its operator;
- creates the ``nsf_fee`` charge, ``companion_of`` the re-debit: price the
  ``nsf_fee_first`` fee type's (the operator's ``nsf_return_count`` is 0) or the
  ``nsf_fee_later`` one's; ``waived`` with the note when an admin waives it;
- raises ``operators.nsf_return_count`` by one (waived or not); nothing else ever does;
- marks the payment resolved: ``resolution_kind = rescheduled``, ``redebit_date``, the
  note (or a fixed sentence naming the date);
- queues **one** provider job for the re-debit charge: ``schedule_create`` (collection
  ``autopay_due``) when the operator has a linked bank method, otherwise a local invoice
  and ``invoice_create`` (collection ``email``). The worker sends both lines, the
  re-debit and the fee (the fee left out when waived), with the re-debit charge's
  reference (:func:`payments.one_off_invoices.charge_amounts`).

The return webhook never calls this: no fee exists without the staff action. A returned
installment (PLAN-012e D11) is rescheduled the same way; its plan's status is brought up
to date once the return is resolved
(:func:`ledger.services.one_off_charges.sync_plan`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import Role, meets, role_of
from finance.dates import day_text
from ledger.models import (
    DESCRIPTION_MAX_LENGTH,
    NSF_FEE_FIRST,
    NSF_FEE_LATER,
    ZERO,
    AuditLog,
    CollectionMethod,
    FeeType,
    Invoice,
    InvoiceStatus,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentStatus,
    ResolutionKind,
)
from ledger.services import leases, numbering, one_off_charges, returns
from ledger.services.payments_ledger import charge_lease
from ledger.validators import validate_no_payment_data, visible_text
from ledger.wording import format_amount, provider_sentence
from payments import jobs
from payments.models import Job, JobKind

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
DEFAULT_NOTE = "Rescheduled: re-debit on {date}."


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
        fee_type = fee_type_for(operator)
        if fee_type is None or fee_type.default_price is None:
            return Outcome(False, FEE_TYPE_MISSING)
        fee_price = Decimal(fee_type.default_price)
        amount = Decimal(payment.amount)
        total = amount if waive_fee else amount + fee_price
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
        fee = OneOffCharge(
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
        try:
            with transaction.atomic():
                redebit.save()
                fee.companion_of = redebit
                fee.save()
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
        for charge in (redebit, fee):
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
                "fee_charge": str(fee.pk),
                "fee_type": fee_type.code,
                "fee": str(fee_price),
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
