"""The returns queue (docs/qa/PLAN-008-webhooks.md D6).

An ACH debit the provider reports returned lands here (``payments.status = returned``,
``resolved_at`` null) through the webhook processor, which also reopens the charge.
Staff resolve a return with a mandatory note; that sets ``resolved_at``,
``resolved_by``, ``resolution_note`` and ``resolution_kind = note`` and writes an audit
row. Resolving with a note changes nothing else: no charge, no provider call; a returned
installment's plan (PLAN-012e D11) leaves ``attention`` once no return of it is open
(:func:`ledger.services.one_off_charges.sync_plan`). The other
resolution, **Reschedule** (a re-debit on an agreed date with the NSF fee, PLAN-011b D5),
is :func:`ledger.services.nsf.reschedule`.
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

from ledger.models import AuditLog, Operator, Payment, PaymentStatus, ResolutionKind, Store
from ledger.services import one_off_charges
from ledger.services.payments_ledger import charge_lease
from ledger.validators import visible_text

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
            "one_off_charge__lease__store",
            "one_off_charge__lease__operator",
            "one_off_charge__operator",
        )
        .order_by("returned_at", "created_at", "id")
    )


def row(payment: Payment) -> ReturnRow:
    """What the queue shows of one returned payment."""
    lease = charge_lease(payment)
    operator = payment.invoice.operator if payment.invoice is not None else None
    if operator is None and lease is not None:
        operator = lease.operator
    if operator is None and payment.one_off_charge is not None:
        operator = payment.one_off_charge.operator
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
    """Mark one returned payment resolved, with a note. Nothing else changes.

    The note is judged and stored as :func:`~ledger.validators.visible_text`: invisible
    characters and whitespace alone are no note (PLAN-010c D12)."""
    text = visible_text(note or "")
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
        payment.resolution_kind = ResolutionKind.NOTE
        try:
            payment.save(
                update_fields=["resolved_at", "resolved_by", "resolution_note", "resolution_kind"]
            )
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
                "resolution_kind": payment.resolution_kind,
            },
        )
        if payment.one_off_charge is not None and payment.one_off_charge.plan_id is not None:
            one_off_charges.sync_plan(payment.one_off_charge.plan_id, actor=actor)
    return ResolveOutcome(True, RESOLVED)
