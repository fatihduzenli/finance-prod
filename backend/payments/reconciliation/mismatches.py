"""Recording and resolving reconciliation mismatches (PLAN-010 D7, D10).

A mismatch is new when no unresolved row of the same ``(kind, entity_type, entity_id)``
exists; otherwise the existing row stands and the finding is counted as seen. A partial
unique index enforces the same rule in the database. Resolving needs a note and writes
an audit row; it changes nothing else.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ledger.models import AuditLog
from ledger.validators import visible_text
from payments.lease_sync import PROVIDER_KEY
from payments.models import ReconciliationMismatch, ReconciliationRun

ENTITY_MISMATCH = "reconciliation_mismatch"
#: ``entity_type`` of a mismatch about a provider transaction we hold no row for.
ENTITY_PROVIDER_TRANSACTION = "provider_transaction"
#: The note of a mismatch resolved by the run itself.
OVERWRITTEN = "Overwritten by reconciliation."

NOTE_REQUIRED = "A note is required: say how the mismatch was resolved."
NOTE_PAYMENT_DATA = "This text may not contain card or account numbers."
ALREADY_RESOLVED = "This mismatch was already resolved. Nothing was changed."
RESOLVED = "Mismatch resolved."


def provider_entity_id(external_kind: str, external_id: str) -> uuid.UUID:
    """A fixed UUID standing for a provider object we hold no row for: the same object
    always gives the same UUID, and the provider's identifier is not stored."""
    name = f"finance:provider-object:{PROVIDER_KEY}:{external_kind}:{external_id}"
    return uuid.uuid5(uuid.NAMESPACE_URL, name)


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    ok: bool
    message: str


def record(
    run: ReconciliationRun,
    kind: str,
    entity_type: str,
    entity_id: uuid.UUID,
    detail: str,
    *,
    resolved_at: datetime | None = None,
) -> bool:
    """Store one finding; True when it is a new mismatch, False when an unresolved row of
    the same kind and entity already exists. With ``resolved_at`` the row is stored
    resolved by the run (:data:`OVERWRITTEN`) and is always new."""
    key = {"kind": kind, "entity_type": entity_type, "entity_id": entity_id}
    if (
        resolved_at is None
        and ReconciliationMismatch.objects.filter(**key, resolved_at__isnull=True).exists()
    ):
        return False
    try:
        with transaction.atomic():
            ReconciliationMismatch.objects.create(
                run=run,
                detail=detail,
                resolved_at=resolved_at,
                resolution_note=OVERWRITTEN if resolved_at is not None else "",
                **key,
            )
    except IntegrityError:
        return False  # another run recorded it between the check and the insert
    return True


def resolve(
    mismatch_id: uuid.UUID, *, note: str, actor: User | None, now: datetime | None = None
) -> ResolveOutcome:
    """Mark one mismatch resolved with a note; audited."""
    text = visible_text(note or "")
    if not text:
        return ResolveOutcome(False, NOTE_REQUIRED)
    with transaction.atomic():
        mismatch = ReconciliationMismatch.objects.select_for_update().get(pk=mismatch_id)
        if mismatch.resolved_at is not None:
            return ResolveOutcome(False, ALREADY_RESOLVED)
        mismatch.resolved_at = now or timezone.now()
        mismatch.resolved_by = actor
        mismatch.resolution_note = text
        try:
            mismatch.save(update_fields=["resolved_at", "resolved_by", "resolution_note"])
        except ValidationError:
            # The card-number guard: nothing was saved.
            return ResolveOutcome(False, NOTE_PAYMENT_DATA)
        AuditLog.objects.create(
            actor=actor,
            action="reconciliation_mismatch.resolved",
            entity_type=ENTITY_MISMATCH,
            entity_id=str(mismatch.pk),
            before={"kind": mismatch.kind, "resolved_at": None},
            after={
                "kind": mismatch.kind,
                "resolved_at": mismatch.resolved_at.isoformat(),
                "resolution_note": text,
            },
        )
    return ResolveOutcome(True, RESOLVED)
