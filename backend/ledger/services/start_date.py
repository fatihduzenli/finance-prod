"""Change start date (docs/qa/15/PLAN-015d §3 (1), §9.1, §9.3).

A prospective lease, or an active one while ``today`` is in its start month, may have its
start date changed. :func:`preview` says what the change would do, writing nothing;
:func:`apply_change` runs :func:`preview` again under the lease's row lock and does it.

**Refused** (one sentence, nothing written): another status, an active lease past its start
month, an active lease's new start after today (it is collected already), the same date, a date
more than :data:`MAX_MONTHS` months from today, a date inside a month another lease of the store
is billed for (an overlap), an empty reason or one holding a card or account number.

**What follows the date** (:class:`StartDateChange`):

- ``started_on`` moves; so does the lease's only rent row's ``effective_from`` (as
  :func:`ledger.services.leases.activate` moves it); a lease with several rows keeps them.
- **The move-in invoice.** A draft not sent is made again (:func:`ledger.services.move_in.
  rebuild_draft`). A sent one with nothing paid is voided at the provider by the worker
  (``invoice_void``, :func:`ledger.services.move_in.request_void`), which then re-prices or
  voids its rent and prepares a new draft. A paid one stays as it is: the rent paid for days
  that are no longer the lease's is kept as a credit (``credit``: no refund row — the money
  stays received and its allocation moves: the payment that paid that rent is re-pointed from
  the rent charge to a :class:`~ledger.models.LeaseCredit` of the same amount, its method,
  processor reference and allocation group unchanged, a payment paying more than that part
  split in two; the credit is then applied to the lease's new first charges that exist) or
  refunded through the existing refund path (``refund``: a ``payment_refund`` job per
  bank-debit payment; the worker calls the provider) — the choice is required, and a refund
  needs the money to have come by bank debit. The money comes from the rent charge's own
  payments, then from the move-in invoice's payments up to the rent's share of them; the
  deposit's part of those payments is never touched.
- **Rent charges.** Every charge of a month before the new start month that has nothing paid
  is void (``rent_charge.voided``; :data:`~ledger.models.RentChargeStatus.VOID`, never due).
  A charge of the new start month, or of a later month that was the start month, is re-priced
  (:func:`ledger.services.rent_run.reamount_start_month`). A start moved earlier into a month
  already paid bills the extra days on that month's charge (§9.3): :attr:`StartDateChange.
  extra` names them and the amount, and :func:`apply_change` needs ``confirmed``.
- **Autopay.** An active lease's live rent schedules are created again from the first debit
  after the new start (``autopay_move``, the worker).
- **The deposit.** With ``move_deposit``, a deposit due on a date moves by as many days as the
  start, and a deposit plan's installments not paid (:func:`ledger.services.installments.
  reschedule`); a deposit on the move-in invoice follows the invoice.
- Reminders and calendar items follow the data (nothing of them is stored).

Audited ``lease.start_date_changed`` (the lease before and after, the reason, the old and new
start, the invoice's outcome and whether the deposit moved); the Notes & follow-ups tab lists it
("Start date changed Nov 15, 2026 → Nov 20, 2026 — <reason>"). Money moves only on an admin's
request (§9.1); a prospective lease without paid rent may be changed by billing.
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from accounts.roles import Role, meets, role_of
from finance.dates import day_short_text, day_text
from ledger.models import (
    NOT_BILLED_RENT,
    ZERO,
    AuditLog,
    CollectionStatus,
    InstallmentOrigin,
    InstallmentPlanStatus,
    Invoice,
    Lease,
    MoneyMethod,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
)
from ledger.services import (
    credits,
    deposits,
    free_rent,
    installments,
    leases,
    move_in,
    one_off_charges,
    rent_run,
)
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from ledger.wording import format_amount
from payments import jobs
from payments.models import JobKind

MAX_MONTHS = 12
CHANGED_ACTION = leases.START_DATE_CHANGED_ACTION
CREDIT = "credit"
REFUND = "refund"
CHOICES = (CREDIT, REFUND)

DRAFT_NONE = "none"
DRAFT_UNSENT = "unsent"
DRAFT_SENT_UNPAID = "sent_unpaid"
DRAFT_PAID = "paid"
OUTCOME_REBUILT = "draft_rebuilt"
OUTCOME_VOIDED = "voided_and_redrafted"
OUTCOME_CREDIT = "kept_as_credit"
OUTCOME_REFUND = "refunded"

NOT_CHANGEABLE = (
    "Only a prospective lease, or an active one in its first month, can have its start date "
    "changed. Nothing was changed."
)
NO_START = "The lease has no start date to change. Nothing was changed."
SAME_DATE = "That is the lease's start date already. Nothing was changed."
TOO_FAR = "The new start date must be within 12 months of today. Nothing was changed."
ACTIVE_FUTURE = (
    "An active lease is collected already: its new start date cannot be after today. "
    "Nothing was changed."
)
OVERLAP = (
    "Another lease of this store is billed for {month}: the new start date would overlap it. "
    "Nothing was changed."
)
REASON_REQUIRED = "Enter the reason for the change."
CHOICE_REQUIRED = "Rent was paid for days that move: keep it as a credit or refund it."
REFUND_NOT_ACH = (
    "Only a bank debit can be refunded from here; keep this rent as a credit instead. "
    "Nothing was changed."
)
CONFIRM_REQUIRED = "Tick the box to confirm the extra days billed."
ADMIN_ONLY = "Only an admin can change the start date when money moves. Nothing was changed."
CHANGED = "Start date changed to {date}."
CREDIT_NOTE = "Kept as a credit: the start date changed from {old} to {new}."
HELD_ACTION = "payment.held_as_credit"
EXTRA_TEXT = "{span}: {amount} more billed on the {month} rent (the start moved earlier)."

EFFECT_DRAFT = {
    DRAFT_NONE: "",
    DRAFT_UNSENT: "The move-in draft is made again for the new date.",
    DRAFT_SENT_UNPAID: (
        "The move-in invoice {number} is voided at the processor and a new draft is prepared."
    ),
    DRAFT_PAID: "The move-in invoice {number} is paid: it stays as it is.",
}


@dataclass(frozen=True, slots=True)
class StartDateChange:
    """What a change of the start date does (the dialog's effects list; :func:`apply_change`'s
    result). ``refusal`` is ``""`` when it may be done."""

    lease: Lease
    old_start: date | None
    new_start: date
    refusal: str = ""
    draft_state: str = DRAFT_NONE
    invoice: Invoice | None = None
    to_void: tuple[RentCharge, ...] = ()
    first_month: Decimal | None = None
    autopay: bool = False
    deposit_text: str = ""
    deposit_movable: bool = False
    #: Rent paid for days that are no longer the lease's: kept as a credit or refunded.
    paid_back: Decimal = ZERO
    #: Whether every payment of that rent came by bank debit (a refund needs it).
    refundable: bool = False
    #: A start moved earlier into a paid month: the extra days' words and amount (§9.3).
    extra_text: str = ""
    extra: Decimal = ZERO
    effects: tuple[str, ...] = field(default_factory=tuple)
    outcome: str = ""

    @property
    def ok(self) -> bool:
        return not self.refusal

    @property
    def needs_choice(self) -> bool:
        return self.paid_back > 0

    @property
    def moves_money(self) -> bool:
        return self.paid_back > 0 or self.extra > 0


def _month(day: date) -> date:
    return day.replace(day=1)


def _months_between(first: date, second: date) -> int:
    return abs((first.year - second.year) * 12 + first.month - second.month)


def _overlap(lease: Lease, new_start: date) -> str:
    """A month another lease of the store is billed for, holding the new start date."""
    others = (
        RentCharge.objects.filter(lease__store=lease.store, period_month=_month(new_start))
        .exclude(lease=lease)
        .exclude(status__in=NOT_BILLED_RENT)
        .filter(Q(lease__ended_on__isnull=True) | Q(lease__ended_on__gte=new_start))
    )
    return OVERLAP.format(month=_month(new_start).strftime("%B %Y")) if others.exists() else ""


def _refusal(lease: Lease, new_start: date, today: date) -> str:
    old = lease.started_on
    if old is None:
        return NO_START
    if lease.ended_on is not None:
        return NOT_CHANGEABLE
    if lease.collection_status == CollectionStatus.ACTIVE:
        if _month(today) != _month(old):
            return NOT_CHANGEABLE
        if new_start > today:
            return ACTIVE_FUTURE
    elif lease.collection_status != CollectionStatus.PROSPECTIVE:
        return NOT_CHANGEABLE
    if new_start == old:
        return SAME_DATE
    if _months_between(new_start, today) > MAX_MONTHS:
        return TOO_FAR
    return _overlap(lease, new_start)


def _new_amount(lease: Lease, month: date, new_start: date) -> Decimal | None:
    """What ``month``'s first debit bills once the lease starts on ``new_start`` (its rent
    as the rent run reads it, the only rent row moving with the start as :func:`apply_change`
    moves it; ``None`` without a debit day or rent)."""
    days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
    if not days:
        return None
    rows = list(lease.rent_history.all()[:2])
    saved = lease.started_on
    lease.started_on = new_start
    try:
        if len(rows) == 1:
            rent: Decimal | None = Decimal(rows[0].amount)
        else:
            found = leases.rent_on(lease, rent_run.debit_date(lease, month, days[0]))
            rent = Decimal(found.amount) if found is not None else None
        if rent is None:
            return None
        amount = rent_run.first_month(lease, month, rent).amount
        return leases.debit_amounts(amount, twice_monthly=len(days) == 2)[0]
    finally:
        lease.started_on = saved


def _draft(lease: Lease) -> tuple[str, Invoice | None]:
    if move_in.current_draft(lease) is not None:
        return DRAFT_UNSENT, None
    invoice = move_in.sent_invoice(lease)
    if invoice is None:
        return DRAFT_NONE, None
    if move_in.invoice_paid(invoice):
        return DRAFT_PAID, invoice
    return DRAFT_SENT_UNPAID, invoice


def _deposit(lease: Lease) -> tuple[str, bool]:
    """The deposit's words and whether its date can move with the start."""
    text = deposits.card_text(lease)
    if not text:
        return "", False
    return text, True


def _settled_refunds(payment: Payment) -> Decimal:
    rows = payment.refunds.filter(status=PaymentStatus.SETTLED)
    return Decimal(sum((row.amount for row in rows), ZERO))


def _rent_sources(charge: RentCharge) -> list[tuple[Payment, Decimal]]:
    """The settled payments that paid ``charge``'s rent and how much of each did: its own
    payments, newest first, then the payments on the builder (move-in) invoice billing it on
    a Rent line, newest first, up to the rent's share of them (the rest paid the invoice's
    other lines, the deposit among them, and is never touched)."""
    found: list[tuple[Payment, Decimal]] = []
    for payment in Payment.objects.filter(
        rent_charge=charge, is_refund=False, status=PaymentStatus.SETTLED
    ).order_by("-settled_on", "-created_at"):
        found.append((payment, Decimal(payment.amount) - _settled_refunds(payment)))
    share = one_off_charges.rent_share(charge)
    for payment in (
        Payment.objects.filter(
            one_off_charge__invoices__line_items__rent_charge=charge,
            kind=PaymentKind.ONE_OFF,
            is_refund=False,
            status=PaymentStatus.SETTLED,
        )
        .distinct()
        .order_by("-settled_on", "-created_at")
    ):
        part = min(share, Decimal(payment.amount) - _settled_refunds(payment))
        if part > 0:
            found.append((payment, part))
            share -= part
    return [(payment, part) for payment, part in found if part > 0]


def _by_bank_debit(charge: RentCharge) -> bool:
    return all(row.method == PaymentMethod.ACH for row, _part in _rent_sources(charge))


def preview(lease: Lease, new_start: date, *, today: date) -> StartDateChange:
    """What changing ``lease``'s start to ``new_start`` would do (see the module docstring),
    writing nothing."""
    old = lease.started_on
    refusal = _refusal(lease, new_start, today)
    if refusal or old is None:
        return StartDateChange(lease, old, new_start, refusal or NO_START)
    new_month, old_month = _month(new_start), _month(old)
    state, invoice = _draft(lease)
    on_invoice = set(
        RentCharge.objects.filter(invoice_lines__invoice=invoice).values_list("pk", flat=True)
        if invoice is not None
        else []
    )
    charges = list(
        RentCharge.objects.filter(lease=lease)
        .exclude(status__in=NOT_BILLED_RENT)
        .filter(
            period_month__gte=min(new_month, old_month), period_month__lte=max(new_month, old_month)
        )
        .order_by("period_month", "seq")
    )
    to_void: list[RentCharge] = []
    paid_back = ZERO
    extra = ZERO
    extra_text = ""
    all_ach = True
    for charge in charges:
        paid = Decimal(charge.amount_paid)
        if charge.period_month < new_month:
            if paid > 0:
                paid_back += paid
                all_ach = all_ach and _by_bank_debit(charge)
            if charge.pk not in on_invoice or paid > 0:
                to_void.append(charge)
            continue
        new_amount = _new_amount(lease, charge.period_month, new_start)
        if new_amount is None or paid <= 0:
            continue
        net = new_amount - Decimal(charge.concession)
        if paid > net:
            paid_back += paid - net
            all_ach = all_ach and _by_bank_debit(charge)
        elif new_amount > Decimal(charge.amount_due):
            extra += new_amount - Decimal(charge.amount_due)
            first = new_start if charge.period_month == new_month else charge.period_month
            last = (old - timedelta(days=1)) if _month(old) == charge.period_month else old
            extra_text = EXTRA_TEXT.format(
                span=f"{day_short_text(first)} – {day_short_text(last)}",
                amount=format_amount(new_amount - Decimal(charge.amount_due)),
                month=charge.period_month.strftime("%B"),
            )
    first_month = _new_amount(lease, new_month, new_start)
    autopay = lease.collection_status == CollectionStatus.ACTIVE and leases.autopay_on(lease)
    deposit_text, movable = _deposit(lease)
    effects: list[str] = [f"Start date: {day_text(old)} → {day_text(new_start)}."]
    if first_month is not None:
        effects.append(f"First month at the new date: {format_amount(first_month)}.")
    if state != DRAFT_NONE and invoice is not None:
        effects.append(EFFECT_DRAFT[state].format(number=invoice.invoice_number))
    elif state != DRAFT_NONE:
        effects.append(EFFECT_DRAFT[state])
    for charge in to_void:
        effects.append(
            f"Rent {charge.period_month.strftime('%B %Y')} "
            f"({format_amount(Decimal(charge.amount_due))}) is void: it never falls due."
        )
    if paid_back > 0:
        effects.append(
            f"{format_amount(paid_back)} of rent paid is kept as a credit or refunded (you choose)."
        )
    if extra_text:
        effects.append(extra_text)
    if autopay:
        effects.append("Autopay is set up again from the first debit after the new date.")
    if deposit_text:
        effects.append(f"Deposit: {deposit_text}. It moves only when you tick the box.")
    return StartDateChange(
        lease,
        old,
        new_start,
        draft_state=state,
        invoice=invoice,
        to_void=tuple(to_void),
        first_month=first_month,
        autopay=autopay,
        deposit_text=deposit_text,
        deposit_movable=movable,
        paid_back=paid_back,
        refundable=paid_back > 0 and all_ach,
        extra_text=extra_text,
        extra=extra,
        effects=tuple(effects),
    )


class StartDateRefused(Exception):  # noqa: N818 (an outcome, as LeaseRefused)
    def __init__(self, field_name: str | None, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


def _keep_as_credit(
    lease: Lease, charge: RentCharge, amount: Decimal, actor: User | None, today: date, note: str
) -> Decimal:
    """Hold up to ``amount`` of the rent paid on ``charge`` as credits on the lease, one per
    payment it came from (:func:`_hold`); no refund row is written. What was kept."""
    left = amount
    for payment, available in _rent_sources(charge):
        if left <= 0:
            break
        part = min(left, available)
        _hold(lease, charge, payment, part, actor, today, note)
        left -= part
    rent_run.recompute_charge(RentCharge.objects.select_for_update().get(pk=charge.pk))
    return amount - left


def _payment_state(payment: Payment) -> dict[str, Any]:
    return {
        "amount": str(payment.amount),
        "kind": payment.kind,
        "method": payment.method,
        "rent_charge": str(payment.rent_charge_id) if payment.rent_charge_id else None,
        "one_off_charge": str(payment.one_off_charge_id) if payment.one_off_charge_id else None,
        "credit": str(payment.credit_id) if payment.credit_id else None,
    }


def _hold(
    lease: Lease,
    charge: RentCharge,
    payment: Payment,
    part: Decimal,
    actor: User | None,
    today: date,
    note: str,
) -> Payment:
    """Move ``part`` of ``payment`` from ``charge`` to a new credit of that amount: the money
    stays received (the processor knows nothing different). A rent payment wholly taken is
    re-pointed from the charge to the credit; otherwise ``part`` is split off into a payment
    of its own naming the credit (same method, dates, reference, invoice and allocation
    group) and the payment it came from (``split_from``, PLAN-015e: a return of the
    payment's transaction reaches it), the payment keeping the rest on its charge. Audited
    (``lease_credit.created`` with ``from_rent_charge``; :data:`HELD_ACTION`). The held
    payment."""
    credit = credits.create(
        lease,
        part,
        # A credit's method is never ACH (lease_credits_method_manual): a bank debit's
        # money is held as "other"; the payment keeps its own method.
        method=payment.method if payment.method != PaymentMethod.ACH else MoneyMethod.OTHER,
        received_on=payment.settled_on or today,
        reference=payment.reference,
        note=note,
        allocation_group=payment.allocation_group or uuid.uuid4(),
        actor=actor,
        context={"from_rent_charge": str(charge.pk), "from_payment": str(payment.pk)},
    )
    before = _payment_state(payment)
    whole = (
        payment.kind == PaymentKind.RENT
        and part == Decimal(payment.amount)
        and not payment.refunds.exists()
    )
    if whole:
        payment.rent_charge = None
        payment.credit = credit
        payment.save(update_fields=["rent_charge", "credit", "updated_at"])
        held = payment
    else:
        payment.amount = Decimal(payment.amount) - part
        payment.save(update_fields=["amount", "updated_at"])
        held = Payment.objects.create(
            invoice=payment.invoice,
            kind=PaymentKind.RENT,
            method=payment.method,
            amount=part,
            status=PaymentStatus.SETTLED,
            initiated_on=payment.initiated_on,
            settled_on=payment.settled_on,
            provider_created_at=payment.provider_created_at,
            reference=payment.reference,
            note=payment.note or note,
            recorded_by=payment.recorded_by,
            allocation_group=payment.allocation_group,
            credit=credit,
            split_from=payment,
        )
    AuditLog.objects.create(
        actor=actor,
        action=HELD_ACTION,
        entity_type="payment",
        entity_id=str(payment.pk),
        before=before,
        after={
            **_payment_state(payment),
            "held_payment": str(held.pk),
            "held": str(part),
            "credit": str(credit.pk),
            "from_rent_charge": str(charge.pk),
        },
    )
    return held


def _refund(charge: RentCharge, amount: Decimal, actor: User | None) -> Decimal:
    """Queue ``payment_refund`` for up to ``amount`` of the bank debits on ``charge`` (the
    existing refund path; the worker calls the provider); what was queued."""
    left = amount
    for payment, available in _rent_sources(charge):
        if left <= 0:
            break
        part = min(left, available)
        jobs.enqueue(
            JobKind.PAYMENT_REFUND,
            payment,
            {"amount": str(part), "confirmed": True, "creation_key": str(uuid.uuid4())},
            actor,
        )
        left -= part
    return amount - left


def _move_rent_row(lease: Lease, new_start: date) -> None:
    rows = list(lease.rent_history.all()[:2])
    if len(rows) == 1:
        rows[0].effective_from = new_start
        rows[0].save(update_fields=["effective_from"])


def _move_deposit(lease: Lease, days: int, actor: User | None, today: date) -> bool:
    """Shift the deposit's own date, or its plan's installments not paid, by ``days``."""
    deposit = (
        OneOffCharge.objects.filter(lease=lease, fee_type__refundable=True)
        .exclude(status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .order_by("created_at")
        .first()
    )
    if deposit is None or days == 0:
        return False
    plan = (
        deposit.installment_plans.filter(origin=InstallmentOrigin.DEPOSIT)
        .exclude(status=InstallmentPlanStatus.CANCELLED)
        .first()
    )
    if plan is not None:
        moving = [
            row
            for row in installments.installments_of(plan)
            if row.status == OneOffChargeStatus.OWED and Decimal(row.amount_paid) == 0
        ]
        if not moving:
            return False
        first = max(moving[0].due_date + timedelta(days=days), today)
        return installments.reschedule(plan, first, plan.cadence, actor, today=today).ok
    if not deposit.dated or deposits.riding_invoice(deposit) or deposit.balance <= 0:
        return False
    before = deposit.due_date
    deposit.due_date = max(before + timedelta(days=days), today)
    deposit.save(update_fields=["due_date", "updated_at"])
    AuditLog.objects.create(
        actor=actor,
        action="one_off_charge.due_date_moved",
        entity_type="one_off_charge",
        entity_id=str(deposit.pk),
        before={"due_date": before.isoformat()},
        after={"due_date": deposit.due_date.isoformat(), "reason": "start_date_changed"},
    )
    return True


def _is_admin(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.ADMIN)


def apply_change(
    lease_id: uuid.UUID,
    new_start: date,
    *,
    reason: str,
    choice: str | None,
    move_deposit: bool,
    actor: User | None,
    today: date,
    confirmed: bool = False,
) -> StartDateChange:
    """Change the start date (see the module docstring); :class:`StartDateRefused`, nothing
    written, when it may not be done."""
    text = visible_text(reason or "")
    if not text:
        raise StartDateRefused("reason", REASON_REQUIRED)
    try:
        validate_no_payment_data(text)
    except ValidationError:
        raise StartDateRefused("reason", PAYMENT_DATA_MESSAGE) from None
    with transaction.atomic():
        lease = Lease.objects.select_for_update().get(pk=lease_id)
        change = preview(lease, new_start, today=today)
        if not change.ok:
            raise StartDateRefused("new_start", change.refusal)
        if change.moves_money and not _is_admin(actor):
            raise StartDateRefused(None, ADMIN_ONLY)
        if change.needs_choice and choice not in CHOICES:
            raise StartDateRefused("choice", CHOICE_REQUIRED)
        if change.needs_choice and choice == REFUND and not change.refundable:
            raise StartDateRefused("choice", REFUND_NOT_ACH)
        if change.extra > 0 and not confirmed:
            raise StartDateRefused("confirm", CONFIRM_REQUIRED)
        old = change.old_start
        if old is None:  # preview refused it already
            raise StartDateRefused("new_start", NO_START)
        before = {
            "started_on": old.isoformat(),
            "rent_from": [row.effective_from.isoformat() for row in lease.rent_history.all()],
            "collection_status": lease.collection_status,
        }
        lease.started_on = new_start
        lease.save(update_fields=["started_on", "updated_at"])
        _move_rent_row(lease, new_start)
        context = {"start_date_change": True}
        new_month = _month(new_start)
        note = CREDIT_NOTE.format(old=day_text(old), new=day_text(new_start))
        kept = ZERO
        # Rent paid for days that move: back off the charges, as the choice says.
        if change.paid_back > 0:
            left = change.paid_back
            for charge in (
                RentCharge.objects.filter(lease=lease)
                .exclude(status__in=NOT_BILLED_RENT)
                .filter(amount_paid__gt=0)
                .order_by("period_month", "seq")
            ):
                if left <= 0:
                    break
                if charge.period_month < new_month:
                    due = Decimal(charge.amount_paid)
                else:
                    amount = _new_amount(lease, charge.period_month, new_start) or ZERO
                    due = Decimal(charge.amount_paid) - (amount - Decimal(charge.concession))
                part = min(left, max(due, ZERO))
                if part <= 0:
                    continue
                if choice == CREDIT:
                    kept += _keep_as_credit(lease, charge, part, actor, today, note)
                else:
                    _refund(charge, part, actor)
                left -= part
        # The months before the new start month never fall due.
        on_invoice = (
            set(
                RentCharge.objects.filter(invoice_lines__invoice=change.invoice).values_list(
                    "pk", flat=True
                )
            )
            if change.draft_state == DRAFT_SENT_UNPAID and change.invoice is not None
            else set()
        )
        for charge in RentCharge.objects.filter(lease=lease, period_month__lt=new_month).exclude(
            status__in=NOT_BILLED_RENT
        ):
            if charge.pk in on_invoice:
                continue
            if rent_run.can_void(charge) or choice == REFUND:
                _void(charge, actor, context)
        # The new start month (and an old start month after it) priced for the new date.
        for charge in (
            RentCharge.objects.filter(lease=lease, period_month__gte=new_month)
            .filter(period_month__lte=max(new_month, _month(old)))
            .exclude(status__in=NOT_BILLED_RENT)
        ):
            if charge.pk not in on_invoice:
                rent_run.reamount_start_month(charge, actor, context)
        # The move-in invoice.
        outcome = ""
        if change.draft_state == DRAFT_UNSENT:
            move_in.rebuild_draft(lease, actor, today=today)
            outcome = OUTCOME_REBUILT
        elif change.draft_state == DRAFT_SENT_UNPAID and change.invoice is not None:
            move_in.request_void(
                change.invoice, actor, rent=move_in.RENT_FOLLOW_START, redraft=True
            )
            outcome = OUTCOME_VOIDED
        elif change.paid_back > 0:
            outcome = OUTCOME_CREDIT if choice == CREDIT else OUTCOME_REFUND
        # An active lease: the months owed from the new start, then the credit on them.
        if lease.collection_status == CollectionStatus.ACTIVE:
            rent_run.catch_up_lease(lease, today, actor)
            free_rent.recompute(lease, actor)
            if kept > 0:
                _apply_credit(lease, actor, today)
        else:
            free_rent.recompute(lease, actor)
        if change.autopay:
            jobs.enqueue(
                JobKind.AUTOPAY_MOVE,
                lease,
                {"start": new_start.isoformat(), "key": str(uuid.uuid4())},
                actor,
            )
        moved = move_deposit and _move_deposit(lease, (new_start - old).days, actor, today)
        after = {
            "started_on": new_start.isoformat(),
            "rent_from": [row.effective_from.isoformat() for row in lease.rent_history.all()],
            "collection_status": lease.collection_status,
            "reason": text,
            "old_start": old.isoformat(),
            "new_start": new_start.isoformat(),
            "invoice_outcome": outcome or None,
            "deposit_moved": moved,
            "voided": [str(charge.pk) for charge in change.to_void],
            "paid_back": str(change.paid_back),
            "choice": choice if change.paid_back > 0 else None,
            "extra": str(change.extra),
        }
        AuditLog.objects.create(
            actor=actor,
            action=CHANGED_ACTION,
            entity_type=leases.ENTITY_LEASE,
            entity_id=str(lease.pk),
            before=before,
            after=after,
        )
    return StartDateChange(
        lease,
        old,
        new_start,
        draft_state=change.draft_state,
        invoice=change.invoice,
        to_void=change.to_void,
        first_month=change.first_month,
        autopay=change.autopay,
        paid_back=change.paid_back,
        extra=change.extra,
        effects=change.effects,
        outcome=outcome,
    )


def _void(charge: RentCharge, actor: User | None, context: dict[str, Any]) -> None:
    """Void a month that never falls due; a month whose bank debit is being refunded is void
    with its payment on it (the refund nets it when it settles)."""
    if rent_run.void_charge(charge, actor, context):
        return
    before = rent_run.charge_state(charge)
    RentCharge.objects.filter(pk=charge.pk).update(status="void")
    AuditLog.objects.create(
        actor=actor,
        action=rent_run.VOIDED_ACTION,
        entity_type=rent_run.ENTITY_CHARGE,
        entity_id=str(charge.pk),
        before=before,
        after={**before, "status": "void", "refund_requested": True, **context},
    )


def _apply_credit(lease: Lease, actor: User | None, today: date) -> None:
    """The credit kept goes on the lease's open charges from the new start month, oldest
    first, as staff would apply it."""
    for charge in (
        RentCharge.objects.filter(
            lease=lease, balance__gt=0, period_month__gte=_month(lease.started_on or today)
        )
        .exclude(status__in=NOT_BILLED_RENT)
        .order_by("period_month", "seq")
    ):
        held = credits.balance(lease)
        if held <= 0:
            return
        group = uuid.uuid4()
        applied = credits.apply(
            credits.locked(lease, today),
            functools.partial(rent_run.record_manual_payment, defer_mark=True),
            charge.pk,
            min(held, Decimal(charge.balance)),
            key=lambda credit: uuid.uuid5(group, str(credit.pk)),
            paid_on=today,
            reference="",
            note="Credit kept when the start date changed.",
            allocation_group=group,
            actor=actor,
            today=today,
        )
        if not isinstance(applied, list):
            return
        for row in applied:
            # The processor's invoice is marked by the worker, never from here (Rule 2).
            if row.outcome.mark_due and row.outcome.payment is not None:
                jobs.enqueue(
                    JobKind.PAYMENT_MARK, charge, {"payment": row.outcome.payment.pk}, actor
                )
