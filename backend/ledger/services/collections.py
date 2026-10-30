"""Collections settings of an operator (docs/qa/PLAN-011b-collections-nsf-attention-calendar.md
D4): *handled personally*, and the masked address the pages show.

The collections ladder that lived here is retired (PLAN-014 D6): the messages, their queue,
send window and sending are :mod:`ledger.services.messaging`'s; the reminder rules are Batch
E's. An operator marked *handled personally* has its operator-facing messages held in the
Outbox (``handled_personally``), never the internal E.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction

from ledger.models import AuditLog, Operator
from ledger.validators import contains_bank_routing_pattern, contains_card_number, visible_text

HAND_ARCHIVED = "The operator is archived."


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str


def mask_address(address: str) -> str:
    """``a***@domain``: the first character of the local part and the domain."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


# --- Handled personally -------------------------------------------------------------------

NOTE_MAX_LENGTH = 500
HANDLED_NOTE_REQUIRED = "Say who handles this operator in person, and why."
HANDLED_NOTE_TOO_LONG = f"The note is at most {NOTE_MAX_LENGTH} characters."
HANDLED_PAYMENT_DATA = "This text may not contain card or account numbers."
HANDLED_ON = "Collection e-mails are off for this operator: it is handled personally."
HANDLED_OFF = "Collection e-mails are on again for this operator."


def set_handled_personally(
    operator_id: uuid.UUID, handled: bool, note: str, actor: User
) -> Outcome:
    """Mark the operator as handled personally (its messages are held) or not. A note is
    required to turn it on. Audited as ``operator.handled_personally_changed``."""
    text = visible_text(note or "")
    if handled and not text:
        return Outcome(False, HANDLED_NOTE_REQUIRED)
    if len(text) > NOTE_MAX_LENGTH:
        return Outcome(False, HANDLED_NOTE_TOO_LONG)
    if contains_card_number(text) or contains_bank_routing_pattern(text):
        return Outcome(False, HANDLED_PAYMENT_DATA)
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is not None:
            return Outcome(False, HAND_ARCHIVED)
        before = {
            "handled_personally": operator.handled_personally,
            "note": operator.handled_personally_note,
        }
        operator.handled_personally = handled
        if handled or text:
            operator.handled_personally_note = text
        operator.save(update_fields=["handled_personally", "handled_personally_note", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action="operator.handled_personally_changed",
            entity_type="operator",
            entity_id=str(operator.pk),
            before=before,
            after={
                "handled_personally": operator.handled_personally,
                "note": operator.handled_personally_note,
            },
        )
    return Outcome(True, HANDLED_ON if handled else HANDLED_OFF)
