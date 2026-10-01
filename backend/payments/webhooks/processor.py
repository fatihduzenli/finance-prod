"""Process one stored webhook event (docs/qa/PLAN-008-webhooks.md D1, D7, D8).

:func:`process_event` locks the event row, runs the handler for its type in a savepoint
and records the outcome. A handler failure is recorded as the exception's class name
and, when the exception carries them, the names of the fields involved; never a message
or a value. The third failed attempt moves the event to dead letter and alerts once.
Nothing here retries: a failed event waits for a replay or the next
``process_webhooks`` run.
"""

from __future__ import annotations

import logging
import re
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from payments import alerts
from payments.models import WebhookEvent, WebhookOutcome
from payments.webhooks.events import UNKNOWN_EVENT, ignored
from payments.webhooks.handlers import HANDLERS

#: Failed attempts after which an event is dead-lettered.
MAX_ATTEMPTS = 3
#: A field name is reported only when it is this plain.
_FIELD_NAME = re.compile(r"[A-Za-z0-9_.]{1,64}")
_MAX_FIELDS = 20

logger = logging.getLogger(__name__)


def describe_error(error: BaseException) -> str:
    """The class name and, if known, the field names. No message, no value."""
    fields: list[str] = []
    error_fields = getattr(error, "error_fields", None)
    if isinstance(error_fields, tuple | list):
        fields = [str(name) for name in error_fields]
    elif isinstance(error, ValidationError) and hasattr(error, "error_dict"):
        fields = [str(name) for name in error.error_dict]
    safe = [name for name in fields if _FIELD_NAME.fullmatch(name)][:_MAX_FIELDS]
    name = type(error).__name__
    return f"{name} (fields: {', '.join(safe)})" if safe else name


def process_event(event_id: uuid.UUID, *, replay: bool = False) -> str | None:
    """Run the handler for one event; return the outcome stored, or None if no such row.

    Without ``replay``, an event already processed or dead-lettered is left as it is.
    With ``replay`` (the staff button), the handler runs again whatever the state;
    handlers are idempotent, so a replay of a processed event changes nothing else.
    """
    newly_dead = False
    with transaction.atomic():
        event = WebhookEvent.objects.select_for_update().filter(pk=event_id).first()
        if event is None:
            return None
        if not replay and (
            event.processed_at is not None or event.outcome == WebhookOutcome.DEAD_LETTER
        ):
            return event.outcome
        handler = HANDLERS.get(event.event_type)
        now = timezone.now()
        event.last_attempt_at = now
        try:
            with transaction.atomic():
                if handler is None:
                    outcome = ignored(UNKNOWN_EVENT)
                else:
                    outcome = handler(event.event_type, event.provider_object_id)
        except Exception as error:  # any handler failure is recorded, never raised
            event.attempts += 1
            event.error = describe_error(error)
            event.processed_at = None
            if event.attempts >= MAX_ATTEMPTS:
                newly_dead = event.outcome != WebhookOutcome.DEAD_LETTER
                event.outcome = WebhookOutcome.DEAD_LETTER
            else:
                event.outcome = WebhookOutcome.FAILED
            logger.warning(
                "Webhook event %s failed (attempt %d): %s", event.pk, event.attempts, event.error
            )
        else:
            event.processed_at = now
            event.outcome = outcome
            event.error = ""
        event.save(
            update_fields=["last_attempt_at", "attempts", "error", "processed_at", "outcome"]
        )
        if newly_dead:
            event_pk, event_type, attempts = str(event.pk), event.event_type, event.attempts
            transaction.on_commit(
                lambda: alerts.webhook_dead_lettered(event_pk, event_type, attempts)
            )
    return event.outcome
