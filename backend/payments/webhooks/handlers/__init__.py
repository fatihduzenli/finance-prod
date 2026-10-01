"""Handlers by event type (docs/qa/PLAN-008-webhooks.md D4).

A handler receives the event type and the provider object ID only, never the body
(PLAN-008 §7, "payload trusted just for the status"); it re-fetches what it needs
through ``payments.get_provider()`` and returns the outcome to store. Raising marks
the attempt failed, and the processor rolls back everything the handler wrote.

Every known event name and every shape type has a handler. Payment-method events are
recorded and deliberately not applied: no payment method is stored here beyond the
reference staff link (PLAN-007 D1).
"""

from __future__ import annotations

from collections.abc import Callable

from payments.webhooks.events import ignored
from payments.webhooks.handlers import customer, deposit, dispute, invoice, schedule, transaction

#: ``(event_type, provider_object_id) -> outcome``.
Handler = Callable[[str, str], str]

NOT_APPLIED = ignored("not applied")


def not_applied(event_type: str, provider_object_id: str) -> str:
    """An event that is stored and deliberately changes nothing: no call, no write."""
    return NOT_APPLIED


HANDLERS: dict[str, Handler] = {
    "create_customer": customer.handle,
    "update_customer": customer.handle,
    "customer": customer.handle,
    "create_invoice": invoice.handle,
    "update_invoice": invoice.handle,
    "send_invoice": invoice.handle,
    "invoice": invoice.handle,
    "create_scheduled_invoice": schedule.handle,
    "update_scheduled_invoice": schedule.handle,
    "delete_scheduled_invoice": schedule.handle,
    "schedule": schedule.handle,
    "create_transaction": transaction.handle,
    "update_transaction": transaction.handle,
    "update_transaction_settled": transaction.handle,
    "transaction": transaction.handle,
    "create_deposit": deposit.handle,
    "deposit": deposit.handle,
    "create_dispute": dispute.handle,
    "update_dispute": dispute.handle,
    "update_payment_method": not_applied,
}
