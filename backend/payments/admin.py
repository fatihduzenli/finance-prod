"""Admin wiring for the provider-side tables: webhook events, read-only
(docs/qa/PLAN-008-webhooks.md D8).

Events are written by the receiver and the processor only; staff replay them from the
``/webhooks/`` screens. The admin lists them and refuses add, change and delete for
everyone. The stored payload is the redacted copy.
"""

from __future__ import annotations

from django.contrib import admin

from ledger.admin import ReadOnlyAdmin
from payments.models import WebhookEvent


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    list_display = (
        "received_at",
        "event_type",
        "event_source",
        "outcome",
        "attempts",
        "duplicate_count",
        "processed_at",
    )
    list_filter = ("event_source",)
    search_fields = ("event_type", "outcome")
    ordering = ("-received_at",)
