"""Customer events (PLAN-008 D4; docs/DATA_OWNERSHIP.md §3).

The operator is ours: no field of it is ever overwritten from the provider. The
customer is re-fetched, its trimmed copy is kept in ``provider_snapshots`` (a new row
only when it changed), and an e-mail that differs from ours flags the operator for
review, because receipts go to the address the provider holds.
"""

from __future__ import annotations

import payments
from ledger.models import Operator
from payments.models import ExternalRef, ProviderSnapshot, WebhookOutcome
from payments.operator_sync import ENTITY_OPERATOR, KIND_CUSTOMER, normalise_email
from payments.provider import CustomerRecord, mask_identifier
from payments.webhooks.events import ignored
from payments.webhooks.handlers.common import (
    NO_OBJECT_ID,
    NOT_AT_PROVIDER,
    audit,
    entity_for,
    fetch,
    json_summary,
)

EMAIL_DIFFERS = "Email differs from the provider; receipts go to the provider's address"
UNKNOWN_CUSTOMER = ignored("unknown customer")


def handle(event_type: str, provider_object_id: str) -> str:
    if not provider_object_id:
        return NO_OBJECT_ID
    ref = entity_for(KIND_CUSTOMER, provider_object_id)
    if ref is None or ref.entity_type != ENTITY_OPERATOR:
        return UNKNOWN_CUSTOMER
    record = fetch(payments.get_provider().get_customer, provider_object_id)
    if record is None:
        return NOT_AT_PROVIDER
    operator = Operator.objects.select_for_update().filter(pk=ref.entity_id).first()
    if operator is None:
        return UNKNOWN_CUSTOMER
    refresh_snapshot(ref, record)
    provider_email = normalise_email(record.operator_email)
    if provider_email and provider_email != (operator.email or "") and not operator.needs_review:
        operator.needs_review = True
        operator.save(update_fields=["needs_review", "updated_at"])
        audit(
            "operator.provider_email_differs",
            ENTITY_OPERATOR,
            operator.pk,
            {"needs_review": False},
            {
                "needs_review": True,
                "message": EMAIL_DIFFERS,
                "customer": mask_identifier(provider_object_id),
            },
        )
    return WebhookOutcome.PROCESSED


def refresh_snapshot(ref: ExternalRef, record: CustomerRecord) -> None:
    """Keep the provider's trimmed copy; a new row only when it changed."""
    payload = json_summary(record.raw_summary)
    latest = ref.snapshots.order_by("-fetched_at", "-id").values_list("payload", flat=True).first()
    if latest == payload:
        return
    ProviderSnapshot.objects.create(external_ref=ref, payload=payload, fetched_at=record.fetched_at)
