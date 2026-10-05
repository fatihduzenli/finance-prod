"""What every handler shares: reference lookups, audit rows and the fixed outcomes.

Provider identifiers are stored only in ``external_refs`` (docs/CLAUDE.md Rule 8) and
appear in audit rows masked. Every audit row written by a handler has no actor: the
provider, not a person, caused it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from ledger.models import AuditLog
from payments.errors import ProviderRejected
from payments.models import ExternalRef
from payments.operator_sync import PROVIDER_KEY
from payments.provider import Summary
from payments.webhooks.events import ignored

ENTITY_INVOICE = "invoice"
KIND_INVOICE = "invoice"
ENTITY_PAYMENT = "payment"
KIND_TRANSACTION = "transaction"
ENTITY_CHARGE = "rent_charge"
ENTITY_INSURANCE_CHARGE = "insurance_charge"
ENTITY_SETUP_CHARGE = "setup_charge"
ENTITY_SCHEDULE = "collection_schedule"
KIND_SCHEDULE = "schedule"
ENTITY_DEPOSIT = "deposit"

NOT_FOUND = 404

NO_OBJECT_ID = ignored("no object id")
NOT_AT_PROVIDER = ignored("not at the provider")


def entity_for(external_kind: str, external_id: str) -> ExternalRef | None:
    """The current reference holding a provider ID of one kind, if any."""
    if not external_id:
        return None
    return ExternalRef.objects.filter(
        provider=PROVIDER_KEY,
        external_kind=external_kind,
        external_id=external_id,
        superseded_at__isnull=True,
    ).first()


def current_ref(entity_type: str, entity_id: uuid.UUID, external_kind: str) -> str:
    """The provider ID an entity of ours currently holds, or ``""``."""
    ref = ExternalRef.objects.filter(
        entity_type=entity_type,
        entity_id=entity_id,
        provider=PROVIDER_KEY,
        external_kind=external_kind,
        superseded_at__isnull=True,
    ).first()
    return ref.external_id if ref is not None else ""


def add_ref(entity_type: str, entity_id: uuid.UUID, external_kind: str, external_id: str) -> None:
    ExternalRef.objects.create(
        entity_type=entity_type,
        entity_id=entity_id,
        provider=PROVIDER_KEY,
        external_kind=external_kind,
        external_id=external_id,
    )


def audit(
    action: str,
    entity_type: str,
    entity_id: object,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=dict(before) if before is not None else None,
        after=dict(after) if after is not None else None,
    )


def json_summary(summary: Summary) -> dict[str, Any]:
    """A provider summary as JSON: amounts become exact strings, never floats."""

    def plain(value: object) -> object:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items()}
        return value

    return {str(key): plain(value) for key, value in summary.items()}


def fetch[T](call: Callable[[str], T], external_id: str) -> T | None:
    """Re-fetch one object; ``None`` when the provider answers that it does not exist."""
    try:
        return call(external_id)
    except ProviderRejected as error:
        if error.status == NOT_FOUND:
            return None
        raise
