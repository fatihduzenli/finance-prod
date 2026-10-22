"""Deposit resolution at the end of a lease (docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md
D7, D12).

A deposit is a setup charge whose fee type is refundable: money held, never revenue.
Once its lease has ended, an admin resolves it once (:func:`resolve_deposit`, one
``deposit_resolutions`` row per deposit):

- ``refunded``: the amount is paid back by Zelle, wire, check or other; a ``refund_deposit``
  payment (money out, never a provider refund) is written with the mandatory note.
- ``withheld``: the business keeps the whole paid deposit; nothing is written to
  payments. To keep only part of it, the admin picks ``refunded`` or ``transferred`` with
  the amount given back, and the rest is recorded as withheld on the same row.
- ``transferred``: the amount moves to another lease of the same operator that has not
  ended: a ``security_deposit`` setup charge is created there with ``price=amount`` and
  no cost, paid by a ``one_off`` payment of method ``other`` with the note "Deposit
  transferred from <store label> lease", so its balance derives from payments like
  every other charge.

``withheld_amount`` is the part of the paid deposit the business keeps: paid less the
amount for a refund or a transfer, the amount itself when withheld. "Paid" is the
deposit's settled ``one_off`` payments less their settled refunds.

**At a lease end** (PLAN-012e D5(d)) the wizard resolves each deposit with
:func:`resolve_deposit_split`: one row whose ``amount`` is the part refunded (kind
``refunded``, by Zelle, wire, check or other) and whose ``withheld_amount`` is the part kept for
the deductions, or, when nothing is refunded, kind ``withheld`` for the whole deposit.
``offset_amount`` is the part of the withheld amount the wizard applies to the lease's
open charges as "deposit offset" payments. Every rule is checked
before anything is written, under the deposit's row lock (and the target lease's for a
transfer); a refusal is one sentence and writes nothing. Every resolution is audited
(entity ``one_off_charge``). No call reaches the payment provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ledger.models import (
    MANUAL_PAYMENT_METHODS,
    AuditLog,
    CollectionMethod,
    DepositResolution,
    DepositResolutionKind,
    FeeType,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import one_off_charges
from ledger.services.rent_run import CENT, MONEY_MAX
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from ledger.wording import format_amount

DEPOSIT_FEE_CODE = "security_deposit"
TRANSFER_NOTE = "Deposit transferred from {store} lease"

# --- Fixed sentences ------------------------------------------------------------------

KIND_INVALID = "Choose refunded, withheld or transferred."
AMOUNT_NOT_POSITIVE = "The amount must be more than zero."
NOT_CENTS = "The amount must have at most two decimals."
TOO_LARGE = "The amount is too large."
NOTE_REQUIRED = "A note is required: say what happened to the deposit."
DATE_FUTURE = "The date cannot be in the future."
METHOD_REQUIRED = "Choose Zelle, wire, check or other: how the deposit was paid back."
METHOD_NOT_REFUND = "A method is given for a refund only. Nothing was saved."
TARGET_REQUIRED = "Choose the lease the deposit moves to."
TARGET_NOT_TRANSFER = "A lease to move the deposit to is given for a transfer only."
NOT_REFUNDABLE = (
    "Only a deposit can be resolved: this fee type is not refundable. Nothing was saved."
)
LEASE_NOT_ENDED = "The lease has not ended: set its end date first. Nothing was saved."
ALREADY_RESOLVED = "This deposit is already resolved. Nothing was saved."
ABOVE_PAID = "The amount is more than was paid on the deposit ({paid}). Nothing was saved."
WITHHELD_PART = (
    "To keep part of the deposit, choose refunded or transferred with the amount given back; "
    "the rest is recorded as withheld. Nothing was saved."
)
TARGET_OTHER_OPERATOR = "A deposit moves only to a lease of the same operator. Nothing was saved."
TARGET_ENDED = "A deposit cannot move to a lease that has ended. Nothing was saved."
TARGET_HAS_DEPOSIT = "That lease already has a security deposit charge. Nothing was saved."
SPLIT_NOT_WHOLE = (
    "The refunded and withheld parts must add up to what was paid on the deposit ({paid}). "
    "Nothing was saved."
)
SPLIT_NEGATIVE = "The refunded and withheld parts cannot be negative. Nothing was saved."
OFFSET_ABOVE_WITHHELD = "The offset cannot be more than the withheld part. Nothing was saved."
NO_DEPOSIT_TYPE = "The security deposit fee type is missing. Nothing was saved."
DONE = {
    DepositResolutionKind.REFUNDED: "Deposit refunded.",
    DepositResolutionKind.WITHHELD: "Deposit withheld.",
    DepositResolutionKind.TRANSFERRED: "Deposit transferred.",
}


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    #: The form field a refusal belongs to (``None``: the whole form).
    field: str | None = None
    resolution: DepositResolution | None = None


def _refused(message: str, field: str | None = None) -> Outcome:
    return Outcome(False, message, field)


# --- Reads ----------------------------------------------------------------------------


def is_deposit(charge: OneOffCharge) -> bool:
    return charge.fee_type is not None and charge.fee_type.refundable


def can_resolve(charge: OneOffCharge) -> bool:
    """Whether the lease page offers "Resolve deposit" for this charge."""
    return (
        is_deposit(charge)
        and charge.lease is not None
        and charge.lease.ended_on is not None
        and not DepositResolution.objects.filter(setup_charge=charge).exists()
    )


def target_leases(charge: OneOffCharge) -> list[Lease]:
    """Leases a deposit may move to: the same operator's other leases not ended."""
    lease = charge.lease
    if lease is None or lease.operator_id is None:
        return []
    return list(
        Lease.objects.filter(operator_id=lease.operator_id, ended_on__isnull=True)
        .exclude(pk=lease.pk)
        .select_related("store")
        .order_by("store__external_label", "started_on", "id")
    )


# --- The resolution -------------------------------------------------------------------


def _shape_refusal(
    kind: str,
    amount: Decimal,
    resolved_on: date,
    method: str,
    note: str,
    to_lease: Lease | None,
    today: date,
) -> Outcome | None:
    """The first rule broken before any row is read, or ``None``. ``note`` is visible."""
    if kind not in DepositResolutionKind.values:
        return _refused(KIND_INVALID, "kind")
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        return _refused(AMOUNT_NOT_POSITIVE, "amount")
    if amount != amount.quantize(CENT):
        return _refused(NOT_CENTS, "amount")
    if amount > MONEY_MAX:
        return _refused(TOO_LARGE, "amount")
    if resolved_on > today:
        return _refused(DATE_FUTURE, "resolved_on")
    if not note:
        return _refused(NOTE_REQUIRED, "note")
    try:
        validate_no_payment_data(note)
    except ValidationError:
        return _refused(PAYMENT_DATA_MESSAGE, "note")
    refund = kind == DepositResolutionKind.REFUNDED
    if refund and method not in MANUAL_PAYMENT_METHODS:
        return _refused(METHOD_REQUIRED, "method")
    if not refund and method:
        return _refused(METHOD_NOT_REFUND, "method")
    transfer = kind == DepositResolutionKind.TRANSFERRED
    if transfer and to_lease is None:
        return _refused(TARGET_REQUIRED, "to_lease")
    if not transfer and to_lease is not None:
        return _refused(TARGET_NOT_TRANSFER, "to_lease")
    return None


def resolve_deposit(
    setup_charge: OneOffCharge,
    kind: str,
    amount: Decimal,
    resolved_on: date,
    method: str,
    note: str,
    to_lease: Lease | None,
    actor: User | None,
    *,
    today: date | None = None,
) -> Outcome:
    """Resolve a deposit once its lease has ended (D7); see the module for the rules."""
    text = visible_text(note or "")
    method = (method or "").strip()
    refused = _shape_refusal(
        kind, amount, resolved_on, method, text, to_lease, today or timezone.localdate()
    )
    if refused is not None:
        return refused
    try:
        with transaction.atomic():
            charge = (
                OneOffCharge.objects.select_for_update(of=("self",))
                .select_related("fee_type", "lease", "lease__store")
                .get(pk=setup_charge.pk)
            )
            refused = _charge_refusal(charge, kind, amount)
            if refused is not None:
                return refused
            paid = one_off_charges.settled(charge)
            target: Lease | None = None
            if to_lease is not None:
                target = Lease.objects.select_for_update().get(pk=to_lease.pk)
                refused = _target_refusal(charge, target)
                if refused is not None:
                    return refused
            resolution = DepositResolution.objects.create(
                setup_charge=charge,
                kind=kind,
                amount=amount,
                withheld_amount=amount if kind == DepositResolutionKind.WITHHELD else paid - amount,
                resolved_on=resolved_on,
                method=method,
                note=text,
                to_lease=target,
                created_by=actor,
            )
            effects = _write_effects(resolution, charge, target, actor)
            AuditLog.objects.create(
                actor=actor,
                action=f"one_off_charge.deposit_{kind}",
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(charge.pk),
                before={"paid": str(paid)},
                after={**_state(resolution), **effects},
            )
    except IntegrityError:
        # A concurrent resolution of the same deposit committed first.
        return _refused(ALREADY_RESOLVED)
    except ValidationError:
        # The card-number guard on save; the transaction is rolled back.
        return _refused(PAYMENT_DATA_MESSAGE, "note")
    return Outcome(True, DONE[DepositResolutionKind(kind)], resolution=resolution)


def _split_shape_refusal(
    refunded: Decimal,
    withheld: Decimal,
    offset: Decimal,
    resolved_on: date,
    method: str,
    note: str,
    today: date,
) -> Outcome | None:
    for value in (refunded, withheld, offset):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            return _refused(SPLIT_NEGATIVE, "amount")
        if value != value.quantize(CENT):
            return _refused(NOT_CENTS, "amount")
        if value > MONEY_MAX:
            return _refused(TOO_LARGE, "amount")
    if refunded + withheld <= 0:
        return _refused(AMOUNT_NOT_POSITIVE, "amount")
    if offset > withheld:
        return _refused(OFFSET_ABOVE_WITHHELD, "amount")
    if resolved_on > today:
        return _refused(DATE_FUTURE, "resolved_on")
    if not note:
        return _refused(NOTE_REQUIRED, "note")
    try:
        validate_no_payment_data(note)
    except ValidationError:
        return _refused(PAYMENT_DATA_MESSAGE, "note")
    if refunded > 0 and method not in MANUAL_PAYMENT_METHODS:
        return _refused(METHOD_REQUIRED, "method")
    if refunded == 0 and method:
        return _refused(METHOD_NOT_REFUND, "method")
    return None


def resolve_deposit_split(
    setup_charge: OneOffCharge,
    refunded: Decimal,
    withheld: Decimal,
    *,
    offset: Decimal = Decimal("0.00"),
    method: str,
    resolved_on: date,
    reference: str,
    note: str,
    actor: User | None,
    today: date | None = None,
) -> Outcome:
    """Resolve a deposit at a lease end in one row (PLAN-012e D5(d)): ``refunded`` paid
    back by ``method`` (a ``refund_deposit`` payment with ``reference``) and ``withheld``
    kept, together exactly what was paid on it; ``offset`` of the withheld part goes to
    the lease's open charges (written by the caller). Kind ``refunded`` when anything is
    refunded, else ``withheld``. The rules of :func:`resolve_deposit` hold (a deposit,
    its lease ended, resolved once), under the deposit's row lock."""
    text = visible_text(note or "")
    method = (method or "").strip()
    reference = visible_text(reference or "")
    refused = _split_shape_refusal(
        refunded, withheld, offset, resolved_on, method, text, today or timezone.localdate()
    )
    if refused is not None:
        return refused
    kind = DepositResolutionKind.REFUNDED if refunded > 0 else DepositResolutionKind.WITHHELD
    try:
        with transaction.atomic():
            charge = (
                OneOffCharge.objects.select_for_update(of=("self",))
                .select_related("fee_type", "lease", "lease__store")
                .get(pk=setup_charge.pk)
            )
            if not is_deposit(charge) or charge.lease is None:
                return _refused(NOT_REFUNDABLE)
            if charge.lease.ended_on is None:
                return _refused(LEASE_NOT_ENDED)
            if DepositResolution.objects.filter(setup_charge=charge).exists():
                return _refused(ALREADY_RESOLVED)
            paid = one_off_charges.settled(charge)
            if refunded + withheld != paid:
                return _refused(SPLIT_NOT_WHOLE.format(paid=format_amount(paid)), "amount")
            resolution = DepositResolution.objects.create(
                setup_charge=charge,
                kind=kind,
                amount=refunded if refunded > 0 else withheld,
                withheld_amount=withheld,
                offset_amount=offset,
                resolved_on=resolved_on,
                method=method if refunded > 0 else "",
                note=text,
                created_by=actor,
            )
            effects: dict[str, Any] = {}
            if refunded > 0:
                payment = _payment(
                    charge,
                    PaymentKind.REFUND_DEPOSIT,
                    method,
                    refunded,
                    resolved_on,
                    text,
                    actor,
                    reference=reference,
                )
                effects = {"payment": str(payment.pk)}
            AuditLog.objects.create(
                actor=actor,
                action=f"one_off_charge.deposit_{kind}",
                entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
                entity_id=str(charge.pk),
                before={"paid": str(paid)},
                after={
                    **_state(resolution),
                    "offset_amount": str(resolution.offset_amount),
                    "split": True,
                    **effects,
                },
            )
    except IntegrityError:
        return _refused(ALREADY_RESOLVED)
    except ValidationError:
        return _refused(PAYMENT_DATA_MESSAGE, "note")
    return Outcome(True, DONE[kind], resolution=resolution)


def _charge_refusal(charge: OneOffCharge, kind: str, amount: Decimal) -> Outcome | None:
    if not is_deposit(charge) or charge.lease is None:
        return _refused(NOT_REFUNDABLE)
    if charge.lease.ended_on is None:
        return _refused(LEASE_NOT_ENDED)
    if DepositResolution.objects.filter(setup_charge=charge).exists():
        return _refused(ALREADY_RESOLVED)
    paid = one_off_charges.settled(charge)
    if amount > paid:
        return _refused(ABOVE_PAID.format(paid=format_amount(paid)), "amount")
    if kind == DepositResolutionKind.WITHHELD and amount != paid:
        return _refused(WITHHELD_PART, "amount")
    return None


def _target_refusal(charge: OneOffCharge, target: Lease) -> Outcome | None:
    operator_id = charge.lease.operator_id if charge.lease is not None else None
    if operator_id is None or target.operator_id != operator_id:
        return _refused(TARGET_OTHER_OPERATOR, "to_lease")
    if target.ended_on is not None:
        return _refused(TARGET_ENDED, "to_lease")
    deposit_type = FeeType.objects.filter(code=DEPOSIT_FEE_CODE).first()
    if deposit_type is None:
        return _refused(NO_DEPOSIT_TYPE)
    if OneOffCharge.objects.filter(lease=target, fee_type=deposit_type, legacy=False).exists():
        return _refused(TARGET_HAS_DEPOSIT, "to_lease")
    return None


def _write_effects(
    resolution: DepositResolution,
    charge: OneOffCharge,
    target: Lease | None,
    actor: User | None,
) -> dict[str, Any]:
    """The payments a resolution writes; the identifiers go in the audit row."""
    if resolution.kind == DepositResolutionKind.REFUNDED:
        payment = _payment(
            charge,
            PaymentKind.REFUND_DEPOSIT,
            resolution.method,
            resolution.amount,
            resolution.resolved_on,
            resolution.note,
            actor,
        )
        return {"payment": str(payment.pk)}
    if resolution.kind == DepositResolutionKind.TRANSFERRED and target is not None:
        deposit_type = FeeType.objects.get(code=DEPOSIT_FEE_CODE)
        received = OneOffCharge.objects.create(
            operator_id=target.operator_id,
            lease=target,
            fee_type=deposit_type,
            category=OneOffCategory.SETUP,
            collection=CollectionMethod.NONE,
            description=deposit_type.name[:120],
            cost=None,
            price=resolution.amount,
            due_date=resolution.resolved_on,
            status=OneOffChargeStatus.OWED,
        )
        payment = _payment(
            received,
            PaymentKind.ONE_OFF,
            PaymentMethod.OTHER,
            resolution.amount,
            resolution.resolved_on,
            TRANSFER_NOTE.format(
                store=charge.lease.store.external_label if charge.lease is not None else ""
            ),
            actor,
        )
        one_off_charges.recompute(received)
        AuditLog.objects.create(
            actor=actor,
            action=one_off_charges.CREATED_ACTION,
            entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
            entity_id=str(received.pk),
            before=None,
            after={
                **one_off_charges.charge_state(received),
                "transferred_from": str(charge.pk),
                "payment": str(payment.pk),
            },
        )
        return {"target_charge": str(received.pk), "payment": str(payment.pk)}
    return {}


def _payment(
    charge: OneOffCharge,
    kind: str,
    method: str,
    amount: Decimal,
    on: date,
    note: str,
    actor: User | None,
    *,
    reference: str = "",
) -> Payment:
    payment = Payment(
        one_off_charge=charge,
        kind=kind,
        method=method,
        amount=amount,
        status=PaymentStatus.SETTLED,
        initiated_on=on,
        settled_on=on,
        reference=reference,
        note=note,
        recorded_by=actor,
    )
    payment.save()
    return payment


def _state(resolution: DepositResolution) -> dict[str, Any]:
    """The audited fields of a resolution."""
    return {
        "resolution": str(resolution.pk),
        "kind": resolution.kind,
        "amount": str(resolution.amount),
        "withheld_amount": str(resolution.withheld_amount),
        "resolved_on": resolution.resolved_on.isoformat(),
        "method": resolution.method,
        "to_lease": str(resolution.to_lease_id) if resolution.to_lease_id else None,
        "note": resolution.note,
    }


def lease_label(lease: Lease) -> str:
    started = lease.started_on.isoformat() if lease.started_on else "no start date"
    return f"Lease {lease.store.external_label} from {started}"
