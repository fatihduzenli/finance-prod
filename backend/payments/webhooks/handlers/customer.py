"""Customer events (PLAN-008 D4; docs/DATA_OWNERSHIP.md §3; PLAN-014 C3).

A customer belongs to a lease (ledger migration 0059 moved the operators' customers to their
leases). The lease's billing details are ours: no field of them is ever overwritten from the
provider. The customer is re-fetched, its trimmed copy is kept in ``provider_snapshots`` (a
new row only when it changed), and an e-mail that differs from the lease's billing e-mail
flags the lease for review, because receipts go to the address the provider holds. A
customer reference still held by an operator is reported as a mismatch
(:data:`CUSTOMER_ON_OPERATOR`) and changes nothing.
"""

from __future__ import annotations

import payments
from ledger.models import Lease
from payments.lease_sync import (
    ENTITY_LEASE,
    ENTITY_OPERATOR,
    KIND_CUSTOMER,
    billing_of,
    normalise_email,
)
from payments.models import ExternalRef, ProviderSnapshot, WebhookOutcome
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
CUSTOMER_ON_OPERATOR = ignored("customer mismatch: held by an operator")


def handle(event_type: str, provider_object_id: str) -> str:
    if not provider_object_id:
        return NO_OBJECT_ID
    ref = entity_for(KIND_CUSTOMER, provider_object_id)
    if ref is not None and ref.entity_type == ENTITY_OPERATOR:
        return CUSTOMER_ON_OPERATOR
    if ref is None or ref.entity_type != ENTITY_LEASE:
        return UNKNOWN_CUSTOMER
    record = fetch(payments.get_provider().get_customer, provider_object_id)
    if record is None:
        return NOT_AT_PROVIDER
    lease = Lease.objects.select_for_update().filter(pk=ref.entity_id).first()
    if lease is None:
        return UNKNOWN_CUSTOMER
    refresh_snapshot(ref, record)
    provider_email = normalise_email(record.operator_email)
    billing_email = normalise_email(billing_of(lease).email or "")
    if provider_email and provider_email != billing_email and not lease.needs_review:
        lease.needs_review = True
        lease.save(update_fields=["needs_review", "updated_at"])
        audit(
            "lease.provider_email_differs",
            ENTITY_LEASE,
            lease.pk,
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
