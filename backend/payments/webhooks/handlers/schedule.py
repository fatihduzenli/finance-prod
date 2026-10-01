"""Scheduled-invoice events (PLAN-008 D4).

The schedule is re-fetched. Whether it runs is the provider's to say, so an active or
paused row of ours follows the provider between ``active`` and ``paused``; a row in any
other state is never moved, only flagged when the provider runs it. Amounts are never
written: a provider total that differs from ours flags the row for review, as does a
schedule the provider no longer has.
"""

from __future__ import annotations

import payments
from ledger.models import CollectionSchedule, ScheduleState
from payments.models import WebhookOutcome
from payments.provider import ScheduleRecord, mask_identifier
from payments.webhooks.events import ignored
from payments.webhooks.handlers.common import (
    ENTITY_SCHEDULE,
    KIND_SCHEDULE,
    NO_OBJECT_ID,
    audit,
    entity_for,
    fetch,
)

UNKNOWN_SCHEDULE = ignored("unknown schedule")
#: TODO-VERIFY-051: the status strings of a scheduled invoice; ``ACTIVE`` and ``PAUSED``
#: are assumed, besides the ``active`` flag.
PROVIDER_ACTIVE = "ACTIVE"
PROVIDER_PAUSED = "PAUSED"
_FOLLOWED = (ScheduleState.ACTIVE, ScheduleState.PAUSED)


def handle(event_type: str, provider_object_id: str) -> str:
    if not provider_object_id:
        return NO_OBJECT_ID
    ref = entity_for(KIND_SCHEDULE, provider_object_id)
    if ref is None or ref.entity_type != ENTITY_SCHEDULE:
        return UNKNOWN_SCHEDULE
    row = CollectionSchedule.objects.select_for_update().filter(pk=ref.entity_id).first()
    if row is None:
        return UNKNOWN_SCHEDULE
    masked = mask_identifier(provider_object_id)
    record = fetch(payments.get_provider().get_scheduled_invoice, provider_object_id)
    if record is None:
        if row.state != ScheduleState.ENDED:
            _flag(row, "collection_schedule.missing_at_provider", {"schedule": masked})
        return WebhookOutcome.PROCESSED
    state = provider_state(record)
    if state is not None and state != row.state:
        if row.state in _FOLLOWED:
            before = row.state
            row.state = state
            row.save(update_fields=["state", "updated_at"])
            audit(
                "collection_schedule.state_confirmed",
                ENTITY_SCHEDULE,
                row.pk,
                {"state": before},
                {"state": state, "schedule": masked},
            )
        elif state == ScheduleState.ACTIVE:
            _flag(row, "collection_schedule.state_differs", {"provider_state": state})
    if record.total is not None and record.total != row.amount:
        _flag(
            row,
            "collection_schedule.total_mismatch",
            {"amount": str(row.amount), "provider_total": str(record.total)},
        )
    return WebhookOutcome.PROCESSED


def provider_state(record: ScheduleRecord) -> ScheduleState | None:
    """``active`` or ``paused`` as the provider reports it; ``None`` when it does not say."""
    status = record.raw_status.strip().upper()
    if status == PROVIDER_PAUSED or (not status and record.active is False):
        return ScheduleState.PAUSED
    if status == PROVIDER_ACTIVE or (not status and record.active is True):
        return ScheduleState.ACTIVE
    return None


def _flag(row: CollectionSchedule, action: str, context: dict[str, str]) -> None:
    """Flag once; a row already flagged is left as it is."""
    if row.needs_review:
        return
    row.needs_review = True
    row.save(update_fields=["needs_review", "updated_at"])
    audit(
        action, ENTITY_SCHEDULE, row.pk, {"needs_review": False}, {"needs_review": True, **context}
    )
