"""One payment allocated by hand over a store's open charges (docs/qa/PLAN-012d-store-workspace.md
D6, D6a; ADR-0013).

Staff enter what was received once (method, amount, date, reference, note) and type how
much of it pays each **open charge** of the store's current lease (:func:`open_charges`:
the rent, one-off, installment, insurance and deposit charges with a balance, oldest due
first). The credit held on the lease (:mod:`ledger.services.credits`) may be applied to
the same charges in the same entry. :func:`record_entry` writes, **in one transaction** under
the lease's row lock:

- one settled payment per allocated charge through the charge's own service
  (:func:`ledger.services.rent_run.record_manual_payment`,
  :func:`ledger.services.one_off_charges.record_payment`,
  :func:`ledger.services.insurance.record_manual_payment`), each with the creation key
  ``uuid5(entry key, str(charge id))``, all sharing a new ``allocation_group`` and the
  entry's method, date, reference and note; the per-charge rules apply as on the
  per-charge forms (the balance, the provider's paid-as-cash marking when the charge's invoice
  is fully covered, a builder invoice once every line is covered, partial coverage
  flagged for review), the marking queued through the existing mark jobs
  (``payment_mark``, ``one_off_payment_mark``) with the line's key as replay key, never
  called here;
- the credit applied: settled payments of method ``credit`` (:func:`credits.apply`), the
  credits used oldest first under their row locks, each payment's key
  ``uuid5(entry key, "credit-<credit id>-<charge id>")``;
- what is not allocated, when the entry says to keep it, as a new
  :class:`~ledger.models.LeaseCredit`;
- the audit row ``payment_entry.recorded`` on the lease (the group, the lines, the credit
  kept and applied, the entry's key), beside each service's and each credit's own rows.

Refused, with nothing written (one sentence): no current lease; a method other than
Zelle, wire or other; an amount of zero, negative, not in cents or too large; a date in
the future; a note missing or holding a card or account number (the reference too); an
allocation or credit application for a charge that is not an open charge of the lease
(a forged id); one negative or not in cents; a charge given more than its balance;
allocations that do not add up to the amount unless the rest is kept as a credit (never
more than the amount); credit applied beyond the credit held on the date; nothing to
record; any refusal of a charge's own service. A replayed entry key returns the first
result and writes nothing.

**Schedule a payment** (:func:`schedule`, PLAN-012d D2 as amended for Batch B): an item
(a catalogue name, matched as the invoice builder's Item box matches it, or a typed one),
an optional description, an amount and a debit date make one one-off charge on the
store's current lease collected by "Autopay on the due date", through
:func:`ledger.services.one_off_charges.add_charge`: the existing ``schedule_create`` job
makes the one-occurrence provider schedule. The category follows the item as on a
builder line (:func:`ledger.services.invoice_builder.category_for`); the description is
the item and, when given, the detail after it. Every rule of a one-off charge applies
(a provider customer and a linked bank method, the date today or later, the provider
limit, the large-debit confirmation); the form's key makes a resubmission create nothing.

Money is ``Decimal`` throughout; nothing here calls the payment provider.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from ledger.models import (
    LIVE_PLAN_STATUSES,
    ZERO,
    AuditLog,
    CollectionMethod,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    RentCharge,
    Store,
)
from ledger.services import (
    credits,
    insurance,
    invoice_builder,
    lease_end,
    one_off_charges,
    receivables,
    rent_run,
)
from ledger.services.leases import format_amount
from ledger.validators import validate_no_payment_data, visible_text
from payments import jobs
from payments.models import JobKind

ENTITY_LEASE = "lease"
RECORDED_ACTION = "payment_entry.recorded"
KIND_DEPOSIT = "deposit"

NO_LEASE = "This store has no current lease: there is nothing to pay. Nothing was recorded."
AMOUNT_REQUIRED = "Enter the amount received, or apply the credit held. Nothing was recorded."
NOT_LISTED = (
    "A charge in the allocation is not an open charge of this store's lease. Nothing was recorded."
)
NEGATIVE = "An allocation cannot be negative. Nothing was recorded."
NOT_CENTS = "Allocations must have at most two decimals. Nothing was recorded."
OVER_BALANCE = (
    "{description}: {amount} is more than its balance of {balance}. Nothing was recorded."
)
OVER_AMOUNT = (
    "The allocations add up to {allocated}, more than the {amount} received. Nothing was recorded."
)
SHORT = (
    "The allocations add up to {allocated}, not the {amount} received: allocate the rest "
    "or keep it as a credit on the lease. Nothing was recorded."
)
CREDIT_ALLOCATED = (
    "Applying the credit pays the charges listed; leave the amount empty to apply the credit "
    "alone. Nothing was recorded."
)
CREDIT_OVER = (
    "The credit applied ({applied}) is more than the credit held on {day} ({held}). Nothing "
    "was recorded."
)
NOTHING = "Nothing to record: enter the amount received. Nothing was recorded."
RECORDED = "Payment of {amount} recorded on {count} charge(s)."
RECORDED_CREDIT = " {credit} kept as a credit on the lease."
KEPT = "Payment of {amount} kept as a credit on the lease."
APPLIED = " Credit of {applied} applied."
CREDIT_ONLY = "Credit of {applied} applied to {count} charge(s)."


@dataclass(frozen=True, slots=True)
class OpenCharge:
    """One open charge of the lease, as the form lists it."""

    kind: str
    charge_id: uuid.UUID
    kind_label: str
    description: str
    due_on: date
    balance: Decimal

    @property
    def field(self) -> str:
        return f"alloc_{self.charge_id}"

    @property
    def credit_field(self) -> str:
        return f"credit_{self.charge_id}"


@dataclass(frozen=True, slots=True)
class EntryInput:
    """The form's entry. ``amount`` is ``None`` when only the credit is applied;
    ``allocations`` and ``applied`` map a charge id to an amount (zero: nothing)."""

    creation_key: uuid.UUID
    method: str
    amount: Decimal | None
    paid_on: date
    reference: str
    note: str
    allocations: Mapping[uuid.UUID, Decimal]
    applied: Mapping[uuid.UUID, Decimal] = field(default_factory=dict)
    keep_credit: bool = False
    #: The form named an allocation or credit input of a charge it does not list (a
    #: forged or stale id): refused after the replay check.
    unlisted: bool = False


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    ok: bool
    message: str
    field: str | None = None
    replayed: bool = False
    payments: tuple[Payment, ...] = ()
    credit: Any = None
    #: What happened at the payment provider for each line (the fixed sentences).
    provider_messages: tuple[str, ...] = ()


class _Refused(Exception):  # noqa: N818 (reads as an outcome)
    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


# --- The open charges -------------------------------------------------------------------


_KIND_LABELS = {
    receivables.KIND_RENT: "Rent",
    receivables.KIND_INSURANCE: "Insurance",
    receivables.KIND_ONE_OFF: "One-off",
    KIND_DEPOSIT: "Deposit",
}


def _deposits(lease: Lease) -> list[OpenCharge]:
    """The lease's deposits still owed (receivables leaves them out: money held, not
    revenue; they are still collected)."""
    rows = (
        OneOffCharge.objects.filter(lease=lease, fee_type__refundable=True, balance__gt=0)
        .exclude(status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .exclude(installment_plans__status__in=LIVE_PLAN_STATUSES)
        .values_list("pk", "fee_type__name", "due_date", "balance")
    )
    return [
        OpenCharge(
            receivables.KIND_ONE_OFF,
            pk,
            _KIND_LABELS[KIND_DEPOSIT],
            f"{OneOffCategory.SETUP.label}: {name}",
            due,
            Decimal(str(balance)),
        )
        for pk, name, due, balance in rows
    ]


def open_charges(lease: Lease, today: date) -> list[OpenCharge]:
    """Every open charge of ``lease`` (its rows of
    :func:`ledger.services.receivables.open_balances` and its deposits still owed), oldest
    due first."""
    if lease.operator_id is None:
        return []
    found = [
        OpenCharge(
            row.kind,
            row.charge_id,
            _KIND_LABELS[row.kind],
            row.description,
            row.due_on,
            row.balance,
        )
        for row in receivables.open_balances(today, operator=lease.operator_id)
        if row.lease_id == lease.pk and row.balance > 0
    ]
    found += _deposits(lease)
    return sorted(
        found, key=lambda row: (row.due_on, row.kind, row.description, str(row.charge_id))
    )


def current_lease(store: Store, today: date) -> Lease | None:
    """The lease a store page payment is for: the store's current lease."""
    return lease_end.current_lease(store, today)


# --- Recording --------------------------------------------------------------------------


def line_key(creation_key: uuid.UUID, charge_id: uuid.UUID) -> uuid.UUID:
    """The creation key of the allocated part of one charge (D6a)."""
    return uuid.uuid5(creation_key, str(charge_id))


def credit_key(creation_key: uuid.UUID, credit_id: uuid.UUID, charge_id: uuid.UUID) -> uuid.UUID:
    """The creation key of one credit's part applied to one charge (D6a)."""
    return uuid.uuid5(creation_key, f"credit-{credit_id}-{charge_id}")


def _replayed(lease: Lease, creation_key: uuid.UUID) -> AuditLog | None:
    return AuditLog.objects.filter(
        action=RECORDED_ACTION,
        entity_type=ENTITY_LEASE,
        entity_id=str(lease.pk),
        after__creation_key=str(creation_key),
    ).first()


def _pay_rent(charge_id: uuid.UUID, **values: Any) -> rent_run.PaymentOutcome:
    """A rent charge's manual payment with its marking deferred: the provider is never
    called here, the outcome's ``mark_due`` says to queue the ``payment_mark`` job."""
    return rent_run.record_manual_payment(charge_id, defer_mark=True, **values)


def _service(kind: str) -> credits.PayService:
    if kind == receivables.KIND_RENT:
        return _pay_rent
    if kind == receivables.KIND_INSURANCE:
        return insurance.record_manual_payment
    return one_off_charges.record_payment


def _money_refusal(value: Decimal) -> str:
    if value < 0:
        return NEGATIVE
    if value != value.quantize(rent_run.CENT):
        return NOT_CENTS
    return ""


def _text_refusal(entry: EntryInput, today: date) -> tuple[str, str | None] | None:
    """The first rule the entry's own fields break, before any row is read."""
    if entry.method not in rent_run.MANUAL_METHODS:
        return rent_run.PAY_METHOD, "method"
    if entry.paid_on > today:
        return rent_run.PAY_FUTURE, "paid_on"
    note = visible_text(entry.note or "")
    if not note:
        return rent_run.PAY_NOTE_REQUIRED, "note"
    for name, text in (("note", note), ("reference", entry.reference)):
        try:
            validate_no_payment_data(text)
        except ValidationError:
            return rent_run.PAY_NOTE_PAYMENT_DATA, name
    amount = entry.amount
    if amount is not None:
        if not amount.is_finite() or amount <= 0:
            return rent_run.PAY_NOT_POSITIVE, "amount"
        if amount != amount.quantize(rent_run.CENT):
            return rent_run.PAY_NOT_CENTS, "amount"
        if amount > rent_run.MONEY_MAX:
            return one_off_charges.TOO_LARGE, "amount"
    for value in (*entry.allocations.values(), *entry.applied.values()):
        if not value.is_finite():
            return NOT_CENTS, None
        refusal = _money_refusal(value)
        if refusal:
            return refusal, None
    return None


def _check(
    entry: EntryInput, listed: Mapping[uuid.UUID, OpenCharge], held: Decimal, day: date
) -> tuple[dict[uuid.UUID, Decimal], dict[uuid.UUID, Decimal], Decimal]:
    """The allocations and credit applications above zero, and the remainder kept as a
    credit; :class:`_Refused` for the first rule broken (read under the lease's lock)."""
    named = set(entry.allocations) | set(entry.applied)
    if named - set(listed):
        raise _Refused(NOT_LISTED)
    allocations = {pk: value for pk, value in entry.allocations.items() if value > 0}
    applied = {pk: value for pk, value in entry.applied.items() if value > 0}
    for pk, charge in listed.items():
        total = allocations.get(pk, ZERO) + applied.get(pk, ZERO)
        if total > charge.balance:
            raise _Refused(
                OVER_BALANCE.format(
                    description=charge.description,
                    amount=format_amount(total),
                    balance=format_amount(charge.balance),
                ),
                charge.field,
            )
    applied_total = sum(applied.values(), ZERO)
    if applied_total > held:
        raise _Refused(
            CREDIT_OVER.format(
                applied=format_amount(applied_total),
                day=day.isoformat(),
                held=format_amount(held),
            )
        )
    allocated = sum(allocations.values(), ZERO)
    if entry.amount is None:
        if allocations:
            raise _Refused(CREDIT_ALLOCATED, "amount")
        if not applied:
            raise _Refused(AMOUNT_REQUIRED, "amount")
        return allocations, applied, ZERO
    if allocated > entry.amount:
        raise _Refused(
            OVER_AMOUNT.format(
                allocated=format_amount(allocated), amount=format_amount(entry.amount)
            )
        )
    remainder = entry.amount - allocated
    if remainder > 0 and not entry.keep_credit:
        raise _Refused(
            SHORT.format(allocated=format_amount(allocated), amount=format_amount(entry.amount))
        )
    return allocations, applied, remainder


def record_entry(
    store: Store, entry: EntryInput, *, actor: User | None, today: date | None = None
) -> EntryOutcome:
    """Record ``entry`` against the open charges of the store's current lease (see the
    module docstring)."""
    day = today or timezone.localdate()
    refusal = _text_refusal(entry, day)
    if refusal is not None:
        return EntryOutcome(False, refusal[0], refusal[1])
    lease = current_lease(store, day)
    if lease is None:
        return EntryOutcome(False, NO_LEASE)
    try:
        with transaction.atomic():
            return _record(lease, entry, actor, day)
    except _Refused as refused:
        return EntryOutcome(False, refused.message, refused.field)


def _record(lease: Lease, entry: EntryInput, actor: User | None, day: date) -> EntryOutcome:
    lease = Lease.objects.select_for_update(of=("self",)).get(pk=lease.pk)
    if _replayed(lease, entry.creation_key) is not None:
        return EntryOutcome(True, rent_run.PAY_ALREADY, replayed=True)
    if entry.unlisted:
        raise _Refused(NOT_LISTED)
    listed = {charge.charge_id: charge for charge in open_charges(lease, day)}
    held_credits = credits.locked(lease, entry.paid_on)
    held = sum((Decimal(credit.remaining) for credit in held_credits), ZERO)
    allocations, applied, remainder = _check(entry, listed, held, entry.paid_on)
    group = uuid.uuid4()
    note = visible_text(entry.note)
    reference = (entry.reference or "").strip()
    written: list[Payment] = []
    lines: list[dict[str, str]] = []
    notices: list[str] = []
    for charge_id, charge in listed.items():
        service = _service(charge.kind)
        amount = allocations.get(charge_id)
        if amount is not None:
            key = line_key(entry.creation_key, charge_id)
            outcome = service(
                charge_id,
                method=entry.method,
                amount=amount,
                paid_on=entry.paid_on,
                note=note,
                actor=actor,
                creation_key=key,
                today=day,
                reference=reference,
                allocation_group=group,
            )
            payment = _written(outcome, charge, key, actor, notices)
            written.append(payment)
            lines.append(_line(charge, payment))
        part = applied.get(charge_id)
        if part is not None:
            used = credits.apply(
                held_credits,
                service,
                charge_id,
                part,
                key=functools.partial(_credit_key, entry.creation_key, charge_id),
                paid_on=entry.paid_on,
                reference=reference,
                note=note,
                allocation_group=group,
                actor=actor,
                today=day,
            )
            if not isinstance(used, list):
                raise _Refused(_line_refusal(charge, used.message), charge.credit_field)
            for item in used:
                payment = _written(
                    item.outcome,
                    charge,
                    _credit_key(entry.creation_key, charge_id, item.credit),
                    actor,
                    notices,
                )
                written.append(payment)
                lines.append(_line(charge, payment))
    kept = None
    if remainder > 0:
        kept = credits.create(
            lease,
            remainder,
            method=entry.method,
            received_on=entry.paid_on,
            reference=reference,
            note=note,
            allocation_group=group,
            actor=actor,
        )
    applied_total = sum(applied.values(), ZERO)
    AuditLog.objects.create(
        actor=actor,
        action=RECORDED_ACTION,
        entity_type=ENTITY_LEASE,
        entity_id=str(lease.pk),
        before=None,
        after={
            "allocation_group": str(group),
            "creation_key": str(entry.creation_key),
            "method": entry.method,
            "amount": str(entry.amount) if entry.amount is not None else None,
            "paid_on": entry.paid_on.isoformat(),
            "lines": lines,
            "credit_kept": (
                {"credit": str(kept.pk), "amount": str(kept.amount)} if kept is not None else None
            ),
            "credit_applied": str(applied_total),
        },
    )
    return EntryOutcome(
        True,
        _message(entry, allocations, applied, remainder),
        payments=tuple(written),
        credit=kept,
        provider_messages=tuple(notices),
    )


def _credit_key(creation_key: uuid.UUID, charge_id: uuid.UUID, credit: Any) -> uuid.UUID:
    return credit_key(creation_key, credit.pk, charge_id)


def _line_refusal(charge: OpenCharge, message: str) -> str:
    return f"{charge.description}: {message}"


def _written(
    outcome: rent_run.PaymentOutcome,
    charge: OpenCharge,
    key: uuid.UUID,
    actor: User | None,
    notices: list[str],
) -> Payment:
    """The payment a service wrote, with its marking queued; :class:`_Refused` when it
    refused or found the line's key used (a key is never reused by another entry)."""
    if not outcome.ok or outcome.payment is None:
        raise _Refused(_line_refusal(charge, outcome.message), charge.field)
    payment = outcome.payment
    if outcome.mark_due:
        if charge.kind == receivables.KIND_RENT:
            jobs.enqueue(
                JobKind.PAYMENT_MARK,
                RentCharge.objects.get(pk=charge.charge_id),
                {"payment": payment.pk},
                actor,
                replay_key=key,
            )
        else:
            jobs.enqueue(
                JobKind.ONE_OFF_PAYMENT_MARK,
                OneOffCharge.objects.get(pk=charge.charge_id),
                {"payment": payment.pk},
                actor,
                replay_key=key,
            )
    elif outcome.provider is not None and outcome.provider.message:
        notices.append(f"{charge.description}: {outcome.provider.message}")
    return payment


def _line(charge: OpenCharge, payment: Payment) -> dict[str, str]:
    return {
        "kind": charge.kind,
        "charge": str(charge.charge_id),
        "payment": str(payment.pk),
        "method": payment.method,
        "amount": str(payment.amount),
    }


def _message(
    entry: EntryInput,
    allocations: Mapping[uuid.UUID, Decimal],
    applied: Mapping[uuid.UUID, Decimal],
    remainder: Decimal,
) -> str:
    applied_total = sum(applied.values(), ZERO)
    if entry.amount is None:
        return CREDIT_ONLY.format(applied=format_amount(applied_total), count=len(applied))
    if allocations:
        text = RECORDED.format(amount=format_amount(entry.amount), count=len(allocations))
        if remainder > 0:
            text += RECORDED_CREDIT.format(credit=format_amount(remainder))
    else:
        text = KEPT.format(amount=format_amount(entry.amount))
    if applied_total > 0:
        text += APPLIED.format(applied=format_amount(applied_total))
    return text


# --- Schedule a payment ------------------------------------------------------------------

SCHEDULE_NOTE = "Scheduled from the store page: {item}."


def schedule(
    store: Store,
    *,
    item: str,
    description: str,
    amount: Decimal,
    due_date: date,
    confirmed: bool,
    creation_key: uuid.UUID,
    actor: User | None,
    today: date | None = None,
) -> one_off_charges.Outcome:
    """Schedule one debit of ``amount`` on ``due_date`` for the store's current lease (see
    the module docstring)."""
    day = today or timezone.localdate()
    lease = current_lease(store, day)
    if lease is None:
        return one_off_charges.Outcome(False, NO_LEASE)
    name = visible_text(item or "")
    offered = {
        invoice_builder.catalogue_key(fee_type.name): fee_type
        for fee_type in reversed(invoice_builder.offered_fee_types())
    }
    fee_type = offered.get(invoice_builder.catalogue_key(name))
    if fee_type is not None:
        name = visible_text(fee_type.name)
    category = invoice_builder.category_for(fee_type)
    detail = visible_text(description or "")
    charge = one_off_charges.ChargeInput(
        fee_type=fee_type if category == OneOffCategory.SETUP else None,
        price=amount,
        cost=None,
        vendor="",
        due_date=due_date,
        note=SCHEDULE_NOTE.format(item=name),
        category=category,
        collection=CollectionMethod.AUTOPAY_DUE,
        description=f"{name}: {detail}" if detail else name,
        confirmed=confirmed,
        creation_key=creation_key,
    )
    return one_off_charges.add_charge(lease.pk, charge, actor=actor)
