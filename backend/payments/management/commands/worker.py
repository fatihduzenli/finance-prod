"""``worker``: the single process that calls the payment provider (PLAN-010 D2, D4).

Each tick takes one queued job (:func:`payments.jobs.claim_next`) and runs it, or else
one stored webhook event that has never been attempted
(:func:`payments.webhooks.processor.process_event`), or else sleeps one second. Jobs
come first. One process, one thread: the rate limiter, the breaker and the failure
budget have one owner. A failed webhook event is not taken again here; it waits for a
replay or ``process_webhooks``, as before (PLAN-008 D1).

SIGTERM (and SIGINT) finish the current item and exit 0. ``--once`` runs one tick and
exits (tests, and a manual catch-up). The command refuses to start unless
``STAX_CALLER=worker``. One log line per item: the kind, the masked entity ID and the
outcome class; never a provider value.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import close_old_connections, connection

from payments import jobs
from payments.models import WebhookEvent, WebhookOutcome
from payments.provider import mask_identifier
from payments.stax.transport import WORKER
from payments.webhooks.processor import process_event

#: Seconds slept when a tick found nothing to do.
IDLE_SECONDS = 1
#: The ``kind`` logged for a webhook event.
EVENT_KIND = "webhook_event"

logger = logging.getLogger(__name__)


def next_event_id() -> Any:
    """The oldest stored event never attempted; failed events wait for a replay."""
    return (
        WebhookEvent.objects.filter(processed_at__isnull=True, attempts=0)
        .exclude(outcome=WebhookOutcome.DEAD_LETTER)
        .order_by("received_at", "id")
        .values_list("pk", flat=True)
        .first()
    )


def tick() -> bool:
    """Process one item; False when there was nothing to do."""
    if not connection.in_atomic_block:
        # As between two requests: drop a connection that is broken or past its age.
        close_old_connections()
    job = jobs.claim_next()
    if job is not None:
        job = jobs.run(job)
        logger.info(
            "Worker item: kind=%s entity=%s outcome=%s",
            job.kind,
            mask_identifier(str(job.entity_id)),
            job.status,
        )
        return True
    event_id = next_event_id()
    if event_id is None:
        return False
    try:
        outcome = process_event(event_id)
    except Exception as error:  # the loop must survive any failure; class name only
        outcome = type(error).__name__
    logger.info(
        "Worker item: kind=%s entity=%s outcome=%s",
        EVENT_KIND,
        mask_identifier(str(event_id)),
        (outcome or "skipped").split(":", 1)[0],
    )
    return True


class Command(BaseCommand):
    help = "Run queued payment-provider jobs and stored webhook events, one at a time."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Run one tick and exit.")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.STAX_CALLER != WORKER:
            raise CommandError("The worker runs only with STAX_CALLER=worker.")
        stop = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            stop.set()

        previous = {
            number: signal.signal(number, request_stop)
            for number in (signal.SIGTERM, signal.SIGINT)
        }
        logger.info("Worker started")
        try:
            while not stop.is_set():
                try:
                    worked = tick()
                except Exception as error:  # a broken tick is logged; the loop goes on
                    logger.error("Worker tick failed: %s", type(error).__name__)
                    worked = False
                if options["once"]:
                    break
                if not worked:
                    stop.wait(IDLE_SECONDS)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        logger.info("Worker stopped")
