"""Event names and outcome text shared by the receiver, the processor and the handlers
(docs/qa/PLAN-008-webhooks.md D3, D10)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from payments.models import WebhookOutcome

#: The events the app registers (D10), from the Merchant Webhooks page of the spec.
EVENT_NAMES: tuple[str, ...] = (
    "create_customer",
    "update_customer",
    "create_invoice",
    "update_invoice",
    "send_invoice",
    "create_scheduled_invoice",
    "update_scheduled_invoice",
    "delete_scheduled_invoice",
    "create_transaction",
    "update_transaction",
    "update_transaction_settled",
    "create_deposit",
    "create_dispute",
    "update_dispute",
    "update_payment_method",
)

#: Event types the receiver assigns when only the object's shape names it.
SHAPE_TYPES: tuple[str, ...] = ("invoice", "schedule", "customer", "transaction", "deposit")

#: The event type of a delivery whose name could not be found anywhere.
UNKNOWN_TYPE = "unknown"

UNKNOWN_EVENT = "unknown event"


def ignored(reason: str) -> str:
    """The stored outcome of an event that was processed and deliberately not applied."""
    return f"{WebhookOutcome.IGNORED.value}: {reason}"


#: When the event being processed was received (PLAN-008a §2 rule 2). Set by the
#: processor around the handler call; a handler still receives only the event type and
#: the object ID, and never the body. ``None`` outside the processor.
_RECEIVED_AT: ContextVar[datetime | None] = ContextVar("webhook_received_at", default=None)


@contextmanager
def handling(received_at: datetime) -> Iterator[None]:
    """Make ``received_at`` the current event's receipt time for the block."""
    token = _RECEIVED_AT.set(received_at)
    try:
        yield
    finally:
        _RECEIVED_AT.reset(token)


def received_at() -> datetime | None:
    """The receipt time of the event being processed, or ``None``."""
    return _RECEIVED_AT.get()
