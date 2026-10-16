"""Credits held on a lease (docs/qa/PLAN-012d-store-workspace.md D6a, D6b; ADR-0013).

A **credit** (:class:`~ledger.models.LeaseCredit`) is money received for a lease and not
applied to a charge: the remainder of a payment entered on the store page and kept as a
credit (:mod:`ledger.services.payments_entry`). It is a liability to the operator, like a
deposit, never revenue:

- it is never billed and never collected; :mod:`ledger.services.receivables` does not
  read it (it is not owed);
- applying it to a charge (:func:`apply`) is a settled payment of method ``credit`` on
  that charge, written through the charge's own payment service, naming the credit, and
  lowering ``remaining`` under the credit's row lock; it counts as collected in the month
  applied under the charge's category, as every settled payment on the charge does;
- it is paid back only at the lease end (:func:`refund_all`): the final statement lists
  what remains as a refund due, and the confirm writes one ``credit_refund`` payment per
  credit by the settlement's method and sets ``remaining`` to zero;
- a transfer is refused while the outgoing lease holds a credit
  (:func:`ledger.services.transfer.balance_blockers`).

:func:`balance` is the store header's "Credit $X"; :func:`held` the P&L footer's
"Credits held" (what was received by a date less what was applied or paid back by then);
:func:`rows_for_store` the Payments tab's list. Every change is audited (entity
``lease_credit``). Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.db.models import Q, QuerySet, Sum

from ledger.models import (
    ZERO,
    AuditLog,
    Lease,
    LeaseCredit,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    Store,
)
from ledger.services.rent_run import PaymentOutcome

ENTITY = "lease_credit"
CREATED_ACTION = "lease_credit.created"
APPLIED_ACTION = "lease_credit.applied"
REFUNDED_ACTION = "lease_credit.refunded"
CENT = Decimal("0.01")
CREDIT_SHORT = "The credit held is less than the credit applied. Nothing was recorded."

#: A charge's manual payment service, as :func:`apply` calls it.
PayService = Callable[..., PaymentOutcome]


def balance(lease: Lease) -> Decimal:
    """The credit held on ``lease``: what was received and is neither applied to a charge
    nor paid back."""
    total = LeaseCredit.objects.filter(lease=lease).aggregate(total=Sum("remaining"))["total"]
    return Decimal(total or ZERO).quantize(CENT)


def held(end: date) -> Decimal:
    """The credits held at the end of ``end`` (the P&L footer, D6b): every credit received
    on or before ``end``, less the credit payments and credit refunds settled by then."""
    received = LeaseCredit.objects.filter(received_on__lte=end).aggregate(total=Sum("amount"))
    used = Payment.objects.filter(
        Q(method=PaymentMethod.CREDIT) | Q(kind=PaymentKind.CREDIT_REFUND),
        credit__isnull=False,
        status=PaymentStatus.SETTLED,
        is_refund=False,
        settled_on__lte=end,
    ).aggregate(total=Sum("amount"))
    return (Decimal(received["total"] or ZERO) - Decimal(used["total"] or ZERO)).quantize(CENT)


def locked(lease: Lease, on: date) -> list[LeaseCredit]:
    """The lease's credits with something remaining received on or before ``on``, oldest
    first, under their row locks (the order :func:`apply` uses them in)."""
    return list(
        LeaseCredit.objects.select_for_update()
        .filter(lease=lease, remaining__gt=0, received_on__lte=on)
        .order_by("received_on", "created_at", "id")
    )


def state(credit: LeaseCredit) -> dict[str, Any]:
    """The audited fields of a credit."""
    return {
        "lease": str(credit.lease_id),
        "amount": str(credit.amount),
        "remaining": str(credit.remaining),
        "method": credit.method,
        "received_on": credit.received_on.isoformat(),
        "allocation_group": str(credit.allocation_group),
    }


def create(
    lease: Lease,
    amount: Decimal,
    *,
    method: str,
    received_on: date,
    reference: str,
    note: str,
    allocation_group: uuid.UUID,
    actor: User | None,
) -> LeaseCredit:
    """Keep ``amount`` as a credit on ``lease`` (in the caller's transaction), audited."""
    credit = LeaseCredit(
        lease=lease,
        amount=amount,
        remaining=amount,
        method=method,
        received_on=received_on,
        reference=reference,
        note=note,
        created_by=actor,
        allocation_group=allocation_group,
    )
    credit.save()
    _audit(actor, CREATED_ACTION, credit, None, state(credit))
    return credit


@dataclass(frozen=True, slots=True)
class Applied:
    credit: LeaseCredit
    outcome: PaymentOutcome


def apply(
    credits: Sequence[LeaseCredit],
    service: PayService,
    charge_id: uuid.UUID,
    amount: Decimal,
    *,
    key: Callable[[LeaseCredit], uuid.UUID],
    paid_on: date,
    reference: str,
    note: str,
    allocation_group: uuid.UUID,
    actor: User | None,
    today: date,
) -> list[Applied] | PaymentOutcome:
    """Apply ``amount`` of ``credits`` (locked by :func:`locked`, in that order) to one
    charge through its payment ``service``: one ``credit`` payment per credit used, each
    with its creation key ``key(credit)``, ``remaining`` lowered and audited. Returns what
    was applied, or the first refusal (the caller rolls back)."""
    left = amount
    applied: list[Applied] = []
    for credit in credits:
        if left <= 0:
            break
        part = min(left, Decimal(credit.remaining))
        if part <= 0:
            continue
        outcome = service(
            charge_id,
            method=PaymentMethod.CREDIT,
            amount=part,
            paid_on=paid_on,
            note=note,
            actor=actor,
            creation_key=key(credit),
            today=today,
            reference=reference,
            allocation_group=allocation_group,
            credit_id=credit.pk,
        )
        if not outcome.ok or outcome.payment is None:
            return outcome
        before = state(credit)
        credit.remaining = Decimal(credit.remaining) - part
        credit.save(update_fields=["remaining", "updated_at"])
        _audit(
            actor,
            APPLIED_ACTION,
            credit,
            before,
            {
                **state(credit),
                "payment": str(outcome.payment.pk),
                "applied": str(part),
                "charge": str(charge_id),
            },
        )
        applied.append(Applied(credit, outcome))
        left -= part
    if left > 0:
        return PaymentOutcome(False, CREDIT_SHORT)
    return applied


def refund_all(
    lease: Lease,
    *,
    method: str,
    on: date,
    reference: str,
    note: str,
    actor: User | None,
) -> list[Payment]:
    """Pay back every credit of ``lease`` with something remaining (the lease end's
    confirm, D6b): one settled ``credit_refund`` payment each by ``method``, ``remaining``
    set to zero, audited. The caller holds the lease's row lock."""
    refunds: list[Payment] = []
    rows = LeaseCredit.objects.select_for_update().filter(lease=lease, remaining__gt=0)
    for credit in rows.order_by("received_on", "created_at", "id"):
        before = state(credit)
        payment = Payment(
            kind=PaymentKind.CREDIT_REFUND,
            method=method,
            amount=credit.remaining,
            status=PaymentStatus.SETTLED,
            initiated_on=on,
            settled_on=on,
            reference=reference,
            note=note,
            recorded_by=actor,
            credit=credit,
            allocation_group=credit.allocation_group,
        )
        payment.save()
        credit.remaining = ZERO
        credit.save(update_fields=["remaining", "updated_at"])
        _audit(
            actor,
            REFUNDED_ACTION,
            credit,
            before,
            {**state(credit), "payment": str(payment.pk), "refunded": str(payment.amount)},
        )
        refunds.append(payment)
    return refunds


# --- The Payments tab (D6b) --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreditRow:
    credit: LeaseCredit
    #: The credit payments and the credit refund that used it, oldest first.
    uses: list[Payment]


def rows_for_store(store: Store) -> list[CreditRow]:
    """The credits of the store's leases, newest first, each with what used it (two
    queries)."""
    return _rows(LeaseCredit.objects.filter(lease__store=store))


def rows_for_operator(operator: Operator) -> list[CreditRow]:
    """The credits of the operator's leases, as :func:`rows_for_store` (PLAN-012d D14)."""
    return _rows(LeaseCredit.objects.filter(lease__operator=operator))


def _rows(found: QuerySet[LeaseCredit]) -> list[CreditRow]:
    credits = list(
        found.select_related("lease__store").order_by("-received_on", "-created_at", "id")
    )
    uses: dict[uuid.UUID, list[Payment]] = {}
    if credits:
        for payment in (
            Payment.objects.filter(credit__in=[credit.pk for credit in credits])
            .select_related(
                "rent_charge",
                "insurance_charge",
                "one_off_charge__fee_type",
                "one_off_charge__plan__rent_charge",
                "one_off_charge__plan__one_off_charge__fee_type",
            )
            .order_by("settled_on", "created_at", "id")
        ):
            if payment.credit_id is not None:
                uses.setdefault(payment.credit_id, []).append(payment)
    return [CreditRow(credit, uses.get(credit.pk, [])) for credit in credits]


def _audit(
    actor: User | None,
    action: str,
    credit: LeaseCredit,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY,
        entity_id=str(credit.pk),
        before=before,
        after=after,
    )
