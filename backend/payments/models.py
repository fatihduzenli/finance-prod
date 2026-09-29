"""Provider-side tables (docs/qa/PLAN-004-schema.md §4.2, docs/CLAUDE.md Rule 8).

``ExternalRef`` is the only place a provider identifier of a business entity is stored:
business tables in ``ledger`` carry none, so a second provider needs no migration.
Deposits and webhook events are provider records themselves and keep their provider
identifiers (PLAN-004 decision D12).
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from ledger.models import (
    HALF_CENT,
    GuardedModel,
    MoneyField,
    free_text,
    guarded_char,
    guarded_json,
)

PROVIDER_MAX_LENGTH = 32
EXTERNAL_ID_MAX_LENGTH = 255


class ExternalRef(GuardedModel):
    entity_type = guarded_char(64)
    #: Generic reference, no foreign key; checked by reconciliation (PLAN-004 §7).
    entity_id = models.UUIDField()
    provider = guarded_char(PROVIDER_MAX_LENGTH)
    external_kind = guarded_char(64)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    external_id = models.CharField(max_length=EXTERNAL_ID_MAX_LENGTH)
    created_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "external_refs"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_kind", "external_id"],
                name="external_refs_external_id_unique",
            ),
            models.UniqueConstraint(
                fields=["entity_type", "entity_id", "provider", "external_kind"],
                condition=Q(superseded_at__isnull=True),
                name="external_refs_one_current_ref",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.external_kind} for {self.entity_type} {self.entity_id}"


class ProviderSnapshot(GuardedModel):
    """A trimmed copy of a provider object, as fetched (docs/CLAUDE.md Rule 1)."""

    external_ref = models.ForeignKey(
        ExternalRef, on_delete=models.CASCADE, related_name="snapshots"
    )
    payload = guarded_json()
    fetched_at = models.DateTimeField()

    class Meta:
        db_table = "provider_snapshots"
        indexes = [
            models.Index(
                fields=["external_ref", "fetched_at"], name="provider_snapshots_fetched_idx"
            )
        ]


class Deposit(GuardedModel):
    provider = guarded_char(PROVIDER_MAX_LENGTH)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    provider_deposit_id = models.CharField(max_length=EXTERNAL_ID_MAX_LENGTH)
    deposit_date = models.DateField()
    gross = MoneyField()
    fees = MoneyField()
    net = MoneyField()
    status = guarded_char(64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "deposits"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_deposit_id"], name="deposits_provider_id_unique"
            ),
            models.CheckConstraint(
                condition=Q(net__gt=F("gross") - F("fees") - HALF_CENT)
                & Q(net__lt=F("gross") - F("fees") + HALF_CENT),
                name="deposits_net_is_gross_minus_fees",
            ),
        ]


class WebhookEvent(GuardedModel):
    received_at = models.DateTimeField(auto_now_add=True)
    event_type = guarded_char(100)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    provider_object_id = models.CharField(max_length=EXTERNAL_ID_MAX_LENGTH)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    dedupe_hash = models.CharField(max_length=64, unique=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = guarded_char(32, blank=True)
    error = free_text()

    class Meta:
        db_table = "webhook_events"


class SyncState(GuardedModel):
    job_name = guarded_char(100, unique=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    cursor = free_text()

    class Meta:
        db_table = "sync_state"
