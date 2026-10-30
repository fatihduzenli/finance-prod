"""Operators, local writes only (docs/qa/PLAN-006-operators.md §4.2; PLAN-014 C1, C3).

An operator is a person: since PLAN-014 the processor customer, the bank method and the
billing details belong to each lease (``ledger.LeaseBilling``, :mod:`payments.lease_sync`).
Saving, editing or archiving an operator never reaches the payment provider; its own
``sync_*`` columns of PLAN-006 D7 are kept for history and no longer written.

A repeated create form finds the operator its ``creation_key`` already made (D6). Every
change writes one ``audit_log`` row with the operator's fields before and after.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from ledger.models import AuditLog, Operator

ENTITY_OPERATOR = "operator"

#: The operator fields staff edit, in form order.
OPERATOR_FIELDS: tuple[str, ...] = (
    # PLAN-012f-b D9: the form sends the kind and its name fields, never the display
    # name, which ``Operator.save`` derives from them; a caller that sets the display
    # name directly still can.
    "kind",
    "company",
    "contact_person",
    "first_name",
    "last_name",
    "display_name",
    "legal_name",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "notes",
    # PLAN-012a D3: local only.
    "sms_phone",
    "sms_consent_at",
    "email_consent_at",
)


class OperatorArchived(Exception):  # noqa: N818 (reads as a state, like the provider errors)
    """An archived operator may not be edited."""


def create_operator(
    fields: Mapping[str, Any], *, creation_key: uuid.UUID, actor: User | None
) -> tuple[Operator, bool]:
    """Save a new operator, or return the one this ``creation_key`` already made.

    Returns ``(operator, created)``. A replayed key never changes the existing row,
    whatever fields came with it (D6). Any other integrity error is raised.
    """
    existing = Operator.objects.filter(creation_key=creation_key).first()
    if existing is not None:
        return existing, False
    operator = Operator(creation_key=creation_key, **_editable(fields))
    try:
        with transaction.atomic():
            operator.save()
            _audit(actor, "operator.created", operator, None, snapshot(operator))
    except IntegrityError:
        existing = Operator.objects.filter(creation_key=creation_key).first()
        if existing is None:
            raise
        return existing, False
    return operator, True


def update_operator(
    operator_id: uuid.UUID, fields: Mapping[str, Any], *, actor: User | None
) -> Operator:
    """Apply staff edits under a row lock; audited when something changed."""
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is not None:
            raise OperatorArchived
        before = snapshot(operator)
        for name, value in _editable(fields).items():
            setattr(operator, name, value)
        operator.save()
        after = snapshot(operator)
        if after != before:
            _audit(actor, "operator.updated", operator, before, after)
    return operator


def archive_operator(operator_id: uuid.UUID, *, actor: User | None) -> Operator:
    """Soft delete (D9). Archiving an archived operator changes nothing."""
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is None:
            before = snapshot(operator)
            operator.archived_at = timezone.now()
            operator.save(update_fields=["archived_at", "updated_at"])
            _audit(actor, "operator.archived", operator, before, snapshot(operator))
    return operator


def _editable(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: fields[name] for name in OPERATOR_FIELDS if name in fields}


def _json_value(value: Any) -> Any:
    """A consent time as ISO text in UTC: the audit row is JSON, and the same moment read
    from the form (local time) and from the database (UTC) must compare equal."""
    return value.astimezone(UTC).isoformat() if isinstance(value, datetime) else value


def snapshot(operator: Operator) -> dict[str, Any]:
    """The operator's audit view: its editable fields and its review and archive state."""
    values: dict[str, Any] = {
        name: _json_value(getattr(operator, name)) for name in OPERATOR_FIELDS
    }
    values["needs_review"] = operator.needs_review
    values["archived_at"] = operator.archived_at.isoformat() if operator.archived_at else None
    return values


def _audit(
    actor: User | None,
    action: str,
    operator: Operator,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_OPERATOR,
        entity_id=str(operator.pk),
        before=before,
        after=after,
    )
