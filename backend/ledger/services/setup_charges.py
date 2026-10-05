"""Setup charges: the one-time items charged on a lease (docs/qa/PLAN-010b D3 to D6).

Rules kept here, not in the views:

- **Creation.** From the lease form's checklist (:func:`create_lease_with_checklist`:
  the lease and every ticked charge in one transaction, under the lease form's
  creation key, so a resubmitted form makes one lease and one set of charges), or one
  at a time from the lease page (:func:`add_charge`). The fee type must be active; a
  lease has at most one non-legacy charge per fee type (a unique index). The price is
  above zero, the cost (optional; empty means unknown) not negative, both with at most
  two decimals; the vendor goes through the card-number guard.
- **Status** is never set by hand. :func:`recompute` derives it from the settled
  ``setup`` payments minus their settled refunds: ``paid``, ``partial`` or ``owed``. A
  deposit refund (``refund_deposit``) is money paid back at the lease's end and does
  not reopen the charge. ``waived`` is set only by :func:`waive`.
- **Edit** (:func:`edit_charge`): cost, price, vendor and due date; the price is locked
  once any payment exists on the charge, and a waived charge is not edited.
- **Waive** (:func:`waive`, admin): a note is mandatory; refused once any payment on the
  charge is settled. Nothing is sent to the payment provider.
- **Manual payments** (:func:`record_payment`): the rent rules (method, amount, date,
  note and the form's creation key; :func:`ledger.services.rent_run.payment_refusal`),
  kind ``setup``. The provider invoice, when one is linked and the payment covers the
  whole price, is never marked here: the outcome says ``mark_due`` and the caller
  queues the ``setup_payment_mark`` job, which runs :func:`mark_provider_invoice` in
  the worker with the rent rules of :func:`ledger.services.rent_run.mark_charge_invoice`.
  A partial payment, or one that differs from the invoice total, flags the charge and
  makes no call. A failed marking is retried by hand (:func:`retry_provider_mark`, the
  ``setup_payment_retry_mark`` job), refused for ``RETRY_LOCK_SECONDS`` after the last
  attempt, as on rent charges.
- **Linking an invoice** (:func:`link_invoice`): the rent rules of
  :func:`ledger.services.rent_run.link_charge_invoice`, with the same sentences.

Every write is audited (entity ``setup_charge``); provider identifiers are masked.
Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from ledger.models import (
    ZERO,
    AuditLog,
    FeeType,
    Lease,
    Payment,
    PaymentKind,
    PaymentStatus,
    SetupCharge,
    SetupChargeStatus,
)
from ledger.services import leases, rent_run
from ledger.services.rent_run import (
    CENT,
    MONEY_MAX,
    PAY_ALREADY,
    PAY_NOTE_PAYMENT_DATA,
    PAY_NOTHING_DUE,
    PAY_OVER_BALANCE,
    PAY_RECORDED,
    PAY_WAIVED,
    LinkOutcome,
    MarkAction,
    MarkResult,
    PaymentOutcome,
)
from ledger.validators import validate_no_payment_data, visible_text
from payments import offline_marking

ENTITY_SETUP_CHARGE = "setup_charge"

# --- Fixed sentences ------------------------------------------------------------------

PRICE_NOT_POSITIVE = "The price must be more than zero."
COST_NEGATIVE = "The cost cannot be negative."
NOT_CENTS = "Amounts must have at most two decimals."
TOO_LARGE = "The amount is too large."
FEE_TYPE_INACTIVE = "This fee type is not active. Nothing was saved."
ALREADY_ON_LEASE = "This lease already has a charge for {name}. Nothing was saved."
TEXT_PAYMENT_DATA = PAY_NOTE_PAYMENT_DATA
ADDED = "{name} charge added."
SAVED = "Setup charge saved."
UNCHANGED = "Nothing was changed."
EDIT_WAIVED = "A waived charge cannot be edited. Nothing was saved."
PRICE_LOCKED = "The price cannot change once a payment is recorded. Nothing was saved."
WAIVE_NOTE_REQUIRED = "A note is required: say why the charge is waived."
WAIVE_SETTLED = "A charge with a settled payment cannot be waived. Nothing was changed."
WAIVE_ALREADY = "This charge is already waived. Nothing was changed."
WAIVED = "Setup charge waived."
RETURNED = "An ACH payment on this charge was returned."
DISPUTED = "A payment on this charge is disputed."
TOTAL_MISMATCH = (
    "The invoice total at the payment provider differs from the price. The price was not changed."
)

CREATED_ACTION = "setup_charge.created"
PAYMENT_ACTION = "setup_charge.payment_recorded"


# --- Values and outcomes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChargeInput:
    """One setup charge as staff entered it (a checklist row or the add form)."""

    fee_type: FeeType
    price: Decimal
    cost: Decimal | None
    vendor: str
    due_date: date
    note: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    #: The form field a refusal belongs to (``None``: the whole form).
    field: str | None = None
    charge: SetupCharge | None = None


class SetupRefused(Exception):  # noqa: N818 (reads as an outcome, like LeaseRefused)
    """A checklist row broke a rule; the whole save is rolled back."""

    def __init__(self, field: str | None, message: str, fee_type: FeeType | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.message = message
        self.fee_type = fee_type


# --- Reads ----------------------------------------------------------------------------


def checklist_types() -> list[FeeType]:
    """The active fee types, in catalogue order: one checklist row each (D5)."""
    return list(FeeType.objects.filter(active=True).order_by("sort_order", "name", "id"))


def charges_of(lease: Lease) -> list[SetupCharge]:
    return list(
        SetupCharge.objects.filter(lease=lease)
        .select_related("fee_type", "lease", "deposit_resolution")
        .order_by("fee_type__sort_order", "fee_type__name", "created_at", "id")
    )


def has_payment(charge: SetupCharge) -> bool:
    return Payment.objects.filter(setup_charge=charge).exists()


def has_settled_payment(charge: SetupCharge) -> bool:
    return Payment.objects.filter(
        setup_charge=charge,
        kind=PaymentKind.SETUP,
        is_refund=False,
        status=PaymentStatus.SETTLED,
    ).exists()


def review_message(charge: SetupCharge) -> str:
    """Why a flagged charge needs review, or ``""`` when it is not flagged."""
    if not charge.needs_review:
        return ""
    parts = []
    if charge.invoices.exclude(total=charge.price).exists():
        parts.append(TOTAL_MISMATCH)
    if AuditLog.objects.filter(
        entity_type=ENTITY_SETUP_CHARGE,
        entity_id=str(charge.pk),
        action=SETUP.action("linked_total_differs"),
    ).exists():
        parts.append(rent_run.LINKED_TOTAL_DIFFERS)
    if charge.review_message:
        parts.append(f"{offline_marking.shown(charge.review_message)}.")
    if charge.payments.filter(status=PaymentStatus.RETURNED).exists():
        parts.append(RETURNED)
    if AuditLog.objects.filter(
        entity_type=ENTITY_SETUP_CHARGE,
        entity_id=str(charge.pk),
        action=SETUP.action("disputed"),
    ).exists():
        parts.append(DISPUTED)
    return " ".join(parts)


# --- Validation -----------------------------------------------------------------------


def _money_refusal(value: Decimal | None, *, field: str, positive: bool) -> Outcome | None:
    if value is None:
        return Outcome(False, PRICE_NOT_POSITIVE, field) if positive else None
    if not isinstance(value, Decimal) or not value.is_finite():
        return Outcome(False, PRICE_NOT_POSITIVE if positive else COST_NEGATIVE, field)
    if positive and value <= 0:
        return Outcome(False, PRICE_NOT_POSITIVE, field)
    if not positive and value < 0:
        return Outcome(False, COST_NEGATIVE, field)
    if value != value.quantize(CENT):
        return Outcome(False, NOT_CENTS, field)
    if value > MONEY_MAX:
        return Outcome(False, TOO_LARGE, field)
    return None


def _text_refusal(text: str, field: str) -> Outcome | None:
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return Outcome(False, TEXT_PAYMENT_DATA, field)
    return None


def refusal(item: ChargeInput) -> Outcome | None:
    """The first rule ``item`` breaks before anything is written, or ``None``."""
    return (
        _money_refusal(item.price, field="price", positive=True)
        or _money_refusal(item.cost, field="cost", positive=False)
        or _text_refusal(item.vendor, "vendor")
        or _text_refusal(item.note, "note")
    )


# --- Creation (D5) --------------------------------------------------------------------


def create_lease_with_checklist(
    fields: Mapping[str, Any],
    *,
    rent: Decimal,
    creation_key: uuid.UUID,
    items: Sequence[ChargeInput],
    actor: User | None,
) -> tuple[Lease, bool, list[SetupCharge]]:
    """Save a new lease and the ticked setup charges in one transaction (D5).

    A resubmitted form (same ``creation_key``) returns the lease the first one made and
    creates nothing. A refused row raises :class:`SetupRefused` and nothing is saved,
    the lease included.
    """
    with transaction.atomic():
        lease, created = leases.create_lease(
            fields, rent=rent, creation_key=creation_key, actor=actor
        )
        if not created:
            return lease, False, []
        charges = create_from_checklist(lease, items, actor=actor)
    return lease, True, charges


def create_from_checklist(
    lease: Lease, items: Sequence[ChargeInput], *, actor: User | None
) -> list[SetupCharge]:
    """Create one setup charge per item, all or none (D5); raises :class:`SetupRefused`."""
    made: list[SetupCharge] = []
    with transaction.atomic():
        for item in items:
            outcome = _create(lease, item, actor)
            if not outcome.ok or outcome.charge is None:
                raise SetupRefused(outcome.field, outcome.message, item.fee_type)
            made.append(outcome.charge)
    return made


def add_charge(lease_id: uuid.UUID, item: ChargeInput, *, actor: User | None) -> Outcome:
    """Add one setup charge to a lease (the lease page's form, billing)."""
    try:
        with transaction.atomic():
            lease = Lease.objects.select_for_update().get(pk=lease_id)
            outcome = _create(lease, item, actor)
            if not outcome.ok:
                transaction.set_rollback(True)
            return outcome
    except IntegrityError:
        # A concurrent add of the same fee type committed first.
        return Outcome(False, ALREADY_ON_LEASE.format(name=item.fee_type.name), "fee_type")


def _create(lease: Lease, item: ChargeInput, actor: User | None) -> Outcome:
    refused = refusal(item)
    if refused is not None:
        return refused
    fee_type = FeeType.objects.filter(pk=item.fee_type.pk, active=True).first()
    if fee_type is None:
        return Outcome(False, FEE_TYPE_INACTIVE, "fee_type")
    taken = SetupCharge.objects.filter(lease=lease, fee_type=fee_type, legacy=False)
    if taken.exists():
        return Outcome(False, ALREADY_ON_LEASE.format(name=fee_type.name), "fee_type")
    charge = SetupCharge(
        lease=lease,
        fee_type=fee_type,
        cost=item.cost,
        price=item.price,
        vendor=item.vendor.strip(),
        due_date=item.due_date,
        note=item.note.strip(),
        status=SetupChargeStatus.OWED,
    )
    try:
        with transaction.atomic():
            charge.save()
    except ValidationError:
        return Outcome(False, TEXT_PAYMENT_DATA, "vendor")
    except IntegrityError:
        return Outcome(False, ALREADY_ON_LEASE.format(name=fee_type.name), "fee_type")
    _audit(actor, CREATED_ACTION, charge, None, charge_state(charge))
    return Outcome(True, ADDED.format(name=fee_type.name), charge=charge)


# --- Edit and waive (D6) --------------------------------------------------------------


def edit_charge(
    charge_id: uuid.UUID,
    *,
    cost: Decimal | None,
    price: Decimal,
    vendor: str,
    due_date: date,
    actor: User | None,
) -> Outcome:
    """Change cost, price, vendor or due date; the price is locked once a payment exists."""
    vendor = (vendor or "").strip()
    refused = (
        _money_refusal(price, field="price", positive=True)
        or _money_refusal(cost, field="cost", positive=False)
        or _text_refusal(vendor, "vendor")
    )
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = SetupCharge.objects.select_for_update().get(pk=charge_id)
        if charge.status == SetupChargeStatus.WAIVED:
            return Outcome(False, EDIT_WAIVED)
        if (charge.cost, charge.price, charge.vendor, charge.due_date) == (
            cost,
            price,
            vendor,
            due_date,
        ):
            return Outcome(True, UNCHANGED, charge=charge)
        if price != charge.price and has_payment(charge):
            return Outcome(False, PRICE_LOCKED, "price")
        before = charge_state(charge)
        charge.cost = cost
        charge.price = price
        charge.vendor = vendor
        charge.due_date = due_date
        # No payment exists when the price changes, so the status stays what it is.
        charge.save(update_fields=["cost", "price", "vendor", "due_date", "updated_at"])
        _audit(actor, "setup_charge.edited", charge, before, charge_state(charge))
    return Outcome(True, SAVED, charge=charge)


def waive(
    charge_id: uuid.UUID, *, note: str, actor: User | None, now: datetime | None = None
) -> Outcome:
    """Waive a setup charge (admin): a note is mandatory; refused once a payment settled."""
    text = visible_text(note or "")
    if not text:
        return Outcome(False, WAIVE_NOTE_REQUIRED, "note")
    refused = _text_refusal(text, "note")
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = SetupCharge.objects.select_for_update().get(pk=charge_id)
        if charge.status == SetupChargeStatus.WAIVED:
            return Outcome(False, WAIVE_ALREADY)
        if has_settled_payment(charge):
            return Outcome(False, WAIVE_SETTLED)
        before = charge_state(charge)
        charge.status = SetupChargeStatus.WAIVED
        charge.waived_at = now or timezone.now()
        charge.waived_by = actor
        charge.waive_note = text
        charge.save(update_fields=["status", "waived_at", "waived_by", "waive_note", "updated_at"])
        _audit(actor, "setup_charge.waived", charge, before, {**charge_state(charge), "note": text})
    return Outcome(True, WAIVED, charge=charge)


# --- Payments (D6) --------------------------------------------------------------------


def record_payment(
    charge_id: uuid.UUID,
    *,
    method: str,
    amount: Decimal,
    paid_on: date,
    note: str,
    actor: User | None,
    creation_key: uuid.UUID | None = None,
    today: date | None = None,
    reference: str = "",
) -> PaymentOutcome:
    """Record a Zelle, wire or other payment against a setup charge (kind ``setup``).

    Never calls the provider: when the charge has a provider invoice and the payment
    covers the whole price at its total, the outcome has ``mark_due=True`` and the
    caller queues :func:`mark_provider_invoice` for the worker. ``creation_key`` is kept
    in the audit row and looked up under the charge's row lock.
    """
    text = visible_text(note or "")
    reference = (reference or "").strip()
    refused = rent_run.payment_refusal(method, amount, paid_on, text, today)
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = SetupCharge.objects.select_for_update().get(pk=charge_id)
        if creation_key is not None and _payment_for_key(charge, creation_key):
            return PaymentOutcome(True, PAY_ALREADY, replayed=True)
        if charge.status == SetupChargeStatus.WAIVED:
            return PaymentOutcome(False, PAY_WAIVED)
        balance = charge.price - settled(charge)
        if balance <= 0:
            return PaymentOutcome(False, PAY_NOTHING_DUE)
        if amount > balance:
            return PaymentOutcome(False, PAY_OVER_BALANCE, "amount")
        whole = balance == charge.price and amount == balance
        before = charge_state(charge)
        payment = Payment(
            setup_charge=charge,
            kind=PaymentKind.SETUP,
            method=method,
            amount=amount,
            status=PaymentStatus.SETTLED,
            initiated_on=paid_on,
            settled_on=paid_on,
            reference=reference,
            note=text,
            recorded_by=actor,
        )
        try:
            payment.save()
        except ValidationError:
            # The card-number guard: the transaction is rolled back with nothing saved.
            transaction.set_rollback(True)
            return PaymentOutcome(False, PAY_NOTE_PAYMENT_DATA, "note")
        recompute(charge)
        _audit(
            actor,
            PAYMENT_ACTION,
            charge,
            before,
            {
                **charge_state(charge),
                "payment": str(payment.pk),
                "method": method,
                "amount": str(amount),
                "paid_on": paid_on.isoformat(),
                "creation_key": str(creation_key) if creation_key is not None else None,
            },
        )
        linked = rent_run.provider_invoice(charge)
        if linked is None:
            return PaymentOutcome(
                True, PAY_RECORDED, payment=payment, provider=MarkResult(MarkAction.NO_INVOICE)
            )
        if not (whole and linked[0].total == amount):
            reason = offline_marking.PARTIAL_AMOUNT if not whole else offline_marking.TOTAL_DIFFERS
            result = rent_run.not_updated(SETUP, charge, payment, linked, reason, actor)
            return PaymentOutcome(True, PAY_RECORDED, payment=payment, provider=result)
    return PaymentOutcome(True, PAY_RECORDED, payment=payment, mark_due=True)


def mark_provider_invoice(
    charge_id: uuid.UUID, payment_id: uuid.UUID, *, actor: User | None
) -> MarkResult:
    """Mark the setup charge's provider invoice paid for this manual payment (the worker's
    ``setup_payment_mark`` job)."""
    return rent_run.mark_charge_invoice(SETUP, charge_id, payment_id, actor=actor)


def retry_provider_mark(charge_id: uuid.UUID, *, actor: User | None) -> MarkResult:
    """Retry the last failed marking of the setup charge's provider invoice (the worker's
    ``setup_payment_retry_mark`` job), refused for ``RETRY_LOCK_SECONDS`` after it."""
    return rent_run.retry_charge_mark(SETUP, charge_id, actor=actor)


def retry_wait(charge: SetupCharge, now: datetime | None = None) -> int | None:
    """``None`` when the charge has no failed marking to retry; otherwise the whole
    seconds until the retry is allowed (0: now)."""
    return rent_run.charge_retry_wait(SETUP, charge, now)


def link_invoice(
    charge_id: uuid.UUID,
    invoice_id: uuid.UUID,
    *,
    confirm: bool,
    note: str,
    actor: User | None,
) -> LinkOutcome:
    """Link an unlinked invoice of the lease's operator to the setup charge (the rent
    rules and sentences)."""
    return rent_run.link_charge_invoice(
        SETUP, charge_id, invoice_id, confirm=confirm, note=note, actor=actor
    )


def _payment_for_key(charge: SetupCharge, creation_key: uuid.UUID) -> bool:
    return AuditLog.objects.filter(
        action=PAYMENT_ACTION,
        entity_type=ENTITY_SETUP_CHARGE,
        entity_id=str(charge.pk),
        after__creation_key=str(creation_key),
    ).exists()


# --- Recomputation --------------------------------------------------------------------


def settled(charge: SetupCharge) -> Decimal:
    """Settled ``setup`` payments minus their settled refunds, to the cent. A deposit
    refund (``refund_deposit``) is not counted."""
    rows = Payment.objects.filter(
        setup_charge=charge, kind=PaymentKind.SETUP, status=PaymentStatus.SETTLED
    )
    paid = rows.filter(is_refund=False).aggregate(total=Sum("amount"))["total"] or ZERO
    refunded = rows.filter(is_refund=True).aggregate(total=Sum("amount"))["total"] or ZERO
    return (Decimal(paid) - Decimal(refunded)).quantize(CENT)


def recompute(charge: SetupCharge) -> None:
    """Recompute the paid amount and the status from the payments (D3).

    Paid in full: ``paid``; partly: ``partial``; nothing: ``owed``. A waived charge is
    left as it is. The caller holds the row lock and writes the audit row.
    """
    if charge.status == SetupChargeStatus.WAIVED:
        return
    paid = settled(charge)
    if paid >= charge.price and paid > 0:
        status = SetupChargeStatus.PAID
    elif paid > 0:
        status = SetupChargeStatus.PARTIAL
    else:
        status = SetupChargeStatus.OWED
    if (charge.amount_paid, charge.status) != (paid, status):
        charge.amount_paid = paid
        charge.status = status
        charge.save(update_fields=["amount_paid", "status", "updated_at"])


# --- Helpers --------------------------------------------------------------------------


def charge_state(charge: SetupCharge) -> dict[str, Any]:
    """The audited fields of a setup charge."""
    return {
        "lease": str(charge.lease_id),
        "fee_type": charge.fee_type.code,
        "cost": str(charge.cost) if charge.cost is not None else None,
        "price": str(charge.price),
        "vendor": charge.vendor,
        "due_date": charge.due_date.isoformat(),
        "amount_paid": str(charge.amount_paid),
        "status": charge.status,
        "needs_review": charge.needs_review,
    }


def _audit(
    actor: User | None,
    action: str,
    charge: SetupCharge,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_SETUP_CHARGE,
        entity_id=str(charge.pk),
        before=before,
        after=after,
    )


#: Setup charges, for the shared payment, marking and link rules of the rent run.
SETUP = rent_run.ChargeKind(SetupCharge, ENTITY_SETUP_CHARGE, "price", charge_state)
