"""Dispute events (PLAN-008 D4): an audit row and a review flag; there is no dispute table.

TODO-VERIFY-053: the provider offers no endpoint that reads a dispute, and the object
ID of a dispute event is assumed to be the disputed transaction's ID. The transaction is
re-fetched to confirm it exists; the charge of our payment for it is flagged for review.
"""

from __future__ import annotations

import payments
from ledger.models import AuditLog, InsuranceCharge, OneOffCharge, RentCharge
from payments.models import WebhookOutcome
from payments.provider import mask_identifier
from payments.webhooks.events import ignored
from payments.webhooks.handlers.common import (
    ENTITY_CHARGE,
    ENTITY_INSURANCE_CHARGE,
    ENTITY_ONE_OFF_CHARGE,
    ENTITY_PAYMENT,
    NO_OBJECT_ID,
    NOT_AT_PROVIDER,
    audit,
    fetch,
)
from payments.webhooks.handlers.transaction import local_payment

UNKNOWN_PAYMENT = ignored("unknown payment")


def handle(event_type: str, provider_object_id: str) -> str:
    if not provider_object_id:
        return NO_OBJECT_ID
    payment = local_payment(provider_object_id)
    if payment is None:
        return UNKNOWN_PAYMENT
    if fetch(payments.get_provider().get_transaction, provider_object_id) is None:
        return NOT_AT_PROVIDER
    context = {
        "event": event_type,
        "payment": str(payment.pk),
        "provider_transaction": mask_identifier(provider_object_id),
    }
    recorded = AuditLog.objects.filter(
        action__endswith=".disputed", after__payment=str(payment.pk), after__event=event_type
    )
    if recorded.exists():
        return WebhookOutcome.PROCESSED  # a repeat of the same event writes nothing
    charge: RentCharge | InsuranceCharge | OneOffCharge | None = None
    if payment.rent_charge_id is not None:
        charge = RentCharge.objects.select_for_update().get(pk=payment.rent_charge_id)
        entity = ENTITY_CHARGE
    elif payment.insurance_charge_id is not None:
        charge = InsuranceCharge.objects.select_for_update().get(pk=payment.insurance_charge_id)
        entity = ENTITY_INSURANCE_CHARGE
    elif payment.one_off_charge_id is not None:
        charge = OneOffCharge.objects.select_for_update().get(pk=payment.one_off_charge_id)
        entity = ENTITY_ONE_OFF_CHARGE
    if charge is None:
        audit("payment.disputed", ENTITY_PAYMENT, payment.pk, None, context)
        return WebhookOutcome.PROCESSED
    flagged = charge.needs_review
    if not flagged:
        charge.needs_review = True
        charge.save(update_fields=["needs_review", "updated_at"])
    audit(
        f"{entity}.disputed",
        entity,
        charge.pk,
        {"needs_review": flagged},
        {"needs_review": True, **context},
    )
    return WebhookOutcome.PROCESSED
