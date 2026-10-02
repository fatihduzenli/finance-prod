"""``process_webhooks``: process stored webhook events that are not processed yet.

docs/qa/PLAN-008-webhooks.md D1. The manual catch-up: retries failed events (the
``worker`` takes only events never attempted, PLAN-010 D2), oldest first, at most
``--limit`` per run. A dead-lettered event is skipped; staff replay it from its page.
Each event is processed in its own transaction by
``payments.webhooks.processor.process_event``, which locks the row, so this command and
the worker never process one event twice at the same time. It prints counts only.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from payments.models import WebhookEvent, WebhookOutcome
from payments.webhooks.processor import process_event

DEFAULT_LIMIT = 100


def _kind(outcome: str | None) -> str:
    return (outcome or "skipped").split(":", 1)[0]


class Command(BaseCommand):
    help = "Process stored webhook events not yet processed, oldest first."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help=f"Most events processed in this run (default {DEFAULT_LIMIT}).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        limit: int = options["limit"]
        if limit < 1:
            raise CommandError("--limit must be at least 1")
        event_ids = list(
            WebhookEvent.objects.filter(processed_at__isnull=True)
            .exclude(outcome=WebhookOutcome.DEAD_LETTER)
            .order_by("received_at", "id")
            .values_list("pk", flat=True)[:limit]
        )
        counts = Counter(_kind(process_event(event_id)) for event_id in event_ids)
        self.stdout.write(
            f"Events taken: {len(event_ids)}. "
            f"Processed: {counts[WebhookOutcome.PROCESSED.value]}. "
            f"Ignored: {counts[WebhookOutcome.IGNORED.value]}. "
            f"Failed: {counts[WebhookOutcome.FAILED.value]}. "
            f"Dead letter: {counts[WebhookOutcome.DEAD_LETTER.value]}."
        )
