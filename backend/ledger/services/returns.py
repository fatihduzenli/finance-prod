"""The returns queue (docs/qa/PLAN-008-webhooks.md D6).

An ACH debit the provider reports returned lands here (``payments.status = returned``,
``resolved_at`` null) through the webhook processor, which also reopens the charge.
Staff resolve a return with a mandatory note; that sets ``resolved_at``,
``resolved_by`` and ``resolution_note`` and writes an audit row. Resolving changes
nothing else: no charge, no provider call, and nothing anywhere re-debits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from ledger.models import AuditLog, Operator, Payment, PaymentStatus, Store

ENTITY_PAYMENT = "payment"

RESOLVE_NOTE_REQUIRED = "A note is required: say how the return was resolved."
RESOLVE_NOTE_PAYMENT_DATA = "This text may not contain card or account numbers."
RESOLVE_NOT_RETURNED = "This payment is not a returned payment. Nothing was changed."
RESOLVE_ALREADY = "This return was already resolved. Nothing was changed."
RESOLVED = "Return resolved."


@dataclass(frozen=True, slots=True)
class ReturnRow:
    payment: Payment
    operator: Operator | None
    store: Store | None
    amount: Decimal
    code: str
    reason: str
    returned_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    ok: bool
    message: str


def open_returns() -> QuerySet[Payment]:
    """Returned payments not yet resolved, oldest return first."""
    return (
        Payment.objects.filter(status=PaymentStatus.RETURNED, resolved_at__isnull=True)
        .select_related(
            "invoice__operator",
            "rent_charge__lease__store",
            "rent_charge__lease__operator",
            "insurance_charge__lease__store",
            "insurance_charge__lease__operator",
        )
        .order_by("returned_at", "created_at", "id")
    )


def row(payment: Payment) -> ReturnRow:
    """What the queue shows of one returned payment."""
    lease = (
        payment.rent_charge.lease
        if payment.rent_charge is not None
        else payment.insurance_charge.lease
        if payment.insurance_charge is not None
        else None
    )
    operator = payment.invoice.operator if payment.invoice is not None else None
    if operator is None and lease is not None:
        operator = lease.operator
    return ReturnRow(
        payment=payment,
        operator=operator,
        store=lease.store if lease is not None else None,
        amount=payment.amount,
        code=payment.return_code,
        reason=payment.return_reason,
        returned_at=payment.returned_at,
    )


def resolve_return(
    payment_id: uuid.UUID, *, note: str, actor: User | None, now: datetime | None = None
) -> ResolveOutcome:
    """Mark one returned payment resolved, with a note. Nothing else changes."""
    text = (note or "").strip()
    if not text:
        return ResolveOutcome(False, RESOLVE_NOTE_REQUIRED)
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        if payment.status != PaymentStatus.RETURNED:
            return ResolveOutcome(False, RESOLVE_NOT_RETURNED)
        if payment.resolved_at is not None:
            return ResolveOutcome(False, RESOLVE_ALREADY)
        payment.resolved_at = now or timezone.now()
        payment.resolved_by = actor
        payment.resolution_note = text
        try:
            payment.save(update_fields=["resolved_at", "resolved_by", "resolution_note"])
        except ValidationError:
            # The card-number guard: nothing was saved.
            return ResolveOutcome(False, RESOLVE_NOTE_PAYMENT_DATA)
        AuditLog.objects.create(
            actor=actor,
            action="payment.return_resolved",
            entity_type=ENTITY_PAYMENT,
            entity_id=str(payment.pk),
            before={"status": payment.status, "resolved_at": None},
            after={
                "status": payment.status,
                "resolved_at": payment.resolved_at.isoformat(),
                "return_code": payment.return_code,
                "resolution_note": text,
            },
        )
    return ResolveOutcome(True, RESOLVED)
