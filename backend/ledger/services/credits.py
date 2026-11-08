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
  (:func:`ledger.services.transfer.balance_blockers`);
- it is **reversed** when the bank debit whose money it held is returned
  (:func:`reverse_from_return`, PLAN-015e, called by the transaction webhook's return path):
  ``remaining`` falls to zero, ``reversed_at`` and ``reversed_reason`` are set, and each
  credit payment that applied it is returned too (its charge owed again and flagged), all
  local rows (the processor is never written: docs/CLAUDE.md Rule 2). A credit already paid
  back at the lease end is only marked reversed (``reversed_after_refund``): the money went
  out, and staff decide.

:func:`balance` is the store header's "Credit $X"; :func:`held` the P&L footer's
"Credits held" (what was received by a date less what was applied or paid back by then, a
credit reversed by then counting for nothing);
:func:`rows_for_store` the Payments tab's list. Every change is audited (entity
``lease_credit``). Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

from finance.dates import day_text
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
    ResolutionKind,
    Store,
)
from ledger.services.rent_run import PaymentOutcome
from ledger.wording import format_amount

ENTITY = "lease_credit"
CREATED_ACTION = "lease_credit.created"
APPLIED_ACTION = "lease_credit.applied"
REFUNDED_ACTION = "lease_credit.refunded"
#: PLAN-015e: the bank debit whose money the credit held was returned.
REVERSED_ACTION = "lease_credit.reversed"
#: The audit action of a payment whose state the reversal changed, as the webhook's.
PAYMENT_STATUS_ACTION = "payment.status_changed"
#: A credit payment returned with the credit is resolved at once: the return to act on is
#: the bank debit's own, in the returns queue.
APPLICATION_RESOLVED = (
    "Returned with the credit it applied: the bank debit that funded the credit was "
    "returned; see that return."
)
CENT = Decimal("0.01")
CREDIT_SHORT = "The credit held is less than the credit applied. Nothing was recorded."

#: A charge's manual payment service, as :func:`apply` calls it.
PayService = Callable[..., PaymentOutcome]


def balance(lease: Lease) -> Decimal:
    """The credit held on ``lease``: what was received and is neither applied to a charge
    nor paid back, nor reversed (PLAN-015e)."""
    total = LeaseCredit.objects.filter(lease=lease, reversed_at__isnull=True).aggregate(
        total=Sum("remaining")
    )["total"]
    return Decimal(total or ZERO).quantize(CENT)


def _not_reversed_by(end: date, prefix: str = "") -> Q:
    """The credit (``prefix`` reaching it) was not reversed on or before ``end``."""
    return Q(**{f"{prefix}reversed_at__isnull": True}) | Q(
        **{f"{prefix}reversed_at__date__gt": end}
    )


def held(end: date) -> Decimal:
    """The credits held at the end of ``end`` (the P&L footer, D6b): every credit received
    on or before ``end``, less the credit payments and credit refunds settled by then. A
    credit reversed on or before ``end`` (PLAN-015e) counts for nothing, nor do its payments;
    a credit payment returned with its credit after ``end`` still counts as applied then."""
    received = (
        LeaseCredit.objects.filter(received_on__lte=end)
        .filter(_not_reversed_by(end))
        .aggregate(total=Sum("amount"))
    )
    used = (
        Payment.objects.filter(
            Q(method=PaymentMethod.CREDIT) | Q(kind=PaymentKind.CREDIT_REFUND),
            credit__isnull=False,
            is_refund=False,
            settled_on__lte=end,
        )
        .filter(
            Q(status=PaymentStatus.SETTLED)
            | Q(status=PaymentStatus.RETURNED, returned_at__date__gt=end)
        )
        .filter(_not_reversed_by(end, "credit__"))
        .aggregate(total=Sum("amount"))
    )
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
    context: dict[str, Any] | None = None,
) -> LeaseCredit:
    """Keep ``amount`` as a credit on ``lease`` (in the caller's transaction), audited;
    ``context`` joins the audit row's ``after`` (where the money came from)."""
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
    _audit(actor, CREATED_ACTION, credit, None, {**state(credit), **(context or {})})
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


# --- A returned bank debit (PLAN-015e) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreditReversal:
    """What :func:`reverse_from_return` did: the credit, what it still held (``unapplied``),
    the credit payments returned with it (their charges owed again) and whether it had been
    paid back at the lease end already (``refunded``: then nothing else was changed)."""

    credit: LeaseCredit
    payment: Payment
    unapplied: Decimal
    applications: list[Payment]
    refunded: bool


def return_reason_text(payment: Payment) -> str:
    """ "R01: Insufficient Funds": the returned payment's code and reason, as stored."""
    code = (payment.return_code or "").strip()
    reason = (payment.return_reason or "").strip()
    return ": ".join(part for part in (code, reason) if part)


def reverse_from_return(
    payment: Payment,
    refresh: Callable[[Payment], object],
    now: datetime | None = None,
) -> CreditReversal | None:
    """Reverse the credit whose money the returned ``payment`` held (a ``rent`` payment held
    as a credit, PLAN-015d), under the credit's row lock, in the caller's transaction:

    - every credit payment that applied it (settled, ``method`` credit) is ``returned`` with
      the bank debit's code and reason, resolved at once (:data:`APPLICATION_RESOLVED`: the
      bank debit's own return is the one to act on) and audited ``payment.status_changed``;
      ``refresh`` (the transaction handler's charge recompute) then reopens its charge and
      flags it for review;
    - the credit's ``remaining`` becomes 0, ``reversed_at`` now and ``reversed_reason`` the
      return's code and reason; audited ``lease_credit.reversed`` (the amount, what was
      unapplied, the payments returned and their amounts, the returned payment, the reason).

    A credit paid back at the lease end already (a settled ``credit_refund``) is only marked
    reversed, audited with ``reversed_after_refund``: the money went out and staff decide.
    ``None`` and nothing written when ``payment`` is not a returned payment naming a credit,
    or the credit is reversed already (a replayed event)."""
    if payment.credit_id is None or payment.status != PaymentStatus.RETURNED:
        return None
    credit = LeaseCredit.objects.select_for_update().get(pk=payment.credit_id)
    if credit.reversed_at is not None:
        return None
    moment = now or timezone.now()
    before = state(credit)
    unapplied = Decimal(credit.remaining)
    refunded = Payment.objects.filter(
        credit=credit, kind=PaymentKind.CREDIT_REFUND, status=PaymentStatus.SETTLED
    ).exists()
    applications: list[Payment] = []
    if not refunded:
        applications = list(
            Payment.objects.select_for_update()
            .filter(
                credit=credit,
                method=PaymentMethod.CREDIT,
                status=PaymentStatus.SETTLED,
                is_refund=False,
            )
            .order_by("settled_on", "created_at", "id")
        )
    for application in applications:
        _return_application(application, payment, moment)
        refresh(application)
    reason = return_reason_text(payment)
    credit.remaining = ZERO
    credit.reversed_at = moment
    credit.reversed_reason = reason
    credit.save(update_fields=["remaining", "reversed_at", "reversed_reason", "updated_at"])
    _audit(
        None,
        REVERSED_ACTION,
        credit,
        before,
        {
            **state(credit),
            "reversed_at": moment.isoformat(),
            "unapplied": str(unapplied),
            "applications": [
                {"payment": str(row.pk), "amount": str(row.amount)} for row in applications
            ],
            "payment": str(payment.pk),
            "debit_date": payment.initiated_on.isoformat() if payment.initiated_on else None,
            "return_code": payment.return_code,
            "reason": reason,
            "reversed_after_refund": refunded,
        },
    )
    return CreditReversal(credit, payment, unapplied, applications, refunded)


REVERSED_TEXT = "Credit of {amount} reversed {date}"


def reversed_text(lease: Lease) -> str:
    """ "Credit of $1,500.00 reversed Nov 6, 2026": the lease's latest credit a returned debit
    reversed, while the lease is flagged Needs review (the LEASE card's Credit row,
    PLAN-015e); ``""`` otherwise (one query when flagged)."""
    if not lease.needs_review:
        return ""
    credit = (
        LeaseCredit.objects.filter(lease=lease, reversed_at__isnull=False)
        .order_by("-reversed_at", "-id")
        .first()
    )
    if credit is None or credit.reversed_at is None:
        return ""
    return REVERSED_TEXT.format(
        amount=format_amount(credit.amount), date=day_text(timezone.localdate(credit.reversed_at))
    )


def _return_application(application: Payment, source: Payment, moment: datetime) -> None:
    before = {"status": application.status, "resolved_at": None}
    application.status = PaymentStatus.RETURNED
    application.returned_at = moment
    application.return_code = source.return_code
    application.return_reason = source.return_reason
    application.resolved_at = moment
    application.resolved_by = None
    application.resolution_note = APPLICATION_RESOLVED
    application.resolution_kind = ResolutionKind.NOTE
    application.save(
        update_fields=[
            "status",
            "returned_at",
            "return_code",
            "return_reason",
            "resolved_at",
            "resolved_by",
            "resolution_note",
            "resolution_kind",
            "updated_at",
        ]
    )
    AuditLog.objects.create(
        actor=None,
        action=PAYMENT_STATUS_ACTION,
        entity_type="payment",
        entity_id=str(application.pk),
        before=before,
        after={
            "status": application.status,
            "method": application.method,
            "amount": str(application.amount),
            "credit": str(application.credit_id),
            "return_code": application.return_code,
            "resolved_at": moment.isoformat(),
            "returned_with": str(source.pk),
        },
    )


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
