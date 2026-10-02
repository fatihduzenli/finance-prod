"""``reconcile``: one reconciliation run (docs/qa/PLAN-010-worker-and-reconciliation.md
D5 to D9).

``reconcile --scheduled`` is what the scheduled Container Apps job runs. It calls no
provider and works with any ``STAX_CALLER``: when
:func:`payments.reconciliation.schedule.should_run_scheduled` says the run is due it
queues a ``reconcile_run`` job with trigger ``scheduled`` for the worker, which runs it
(the worker stays the one process calling the provider, docs/CLAUDE.md Rule 3);
otherwise a ``skipped`` row records the reason. It prints one line.

Without ``--scheduled`` the run is manual and starts at once in this process (the same
lock still applies): a catch-up run from inside the worker container. That form refuses
to start unless ``STAX_CALLER=worker``, prints the status and the counts only, and exits
1 when the run failed.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from payments import jobs
from payments.models import ReconciliationStatus, ReconciliationTrigger
from payments.reconciliation import run_reconciliation, skipped
from payments.reconciliation.schedule import should_run_scheduled
from payments.stax.transport import WORKER

QUEUED = "Due: the scheduled reconciliation is queued for the worker."


class Command(BaseCommand):
    help = (
        "Run the reconciliation once (with --scheduled: queue it for the worker, only when "
        "it is due)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--scheduled",
            action="store_true",
            help="Apply the local-time rule of the scheduled job and queue the run.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["scheduled"]:
            self._queue_if_due()
            return
        if settings.STAX_CALLER != WORKER:
            raise CommandError("The reconciliation runs only with STAX_CALLER=worker.")
        run = run_reconciliation(ReconciliationTrigger.MANUAL)
        lines = [f"Status: {run.status}. Requests: {run.requests}. Duration: {run.duration_ms} ms."]
        for letter, counts in sorted(run.counts.items()):
            shown = ", ".join(f"{name}={number}" for name, number in sorted(counts.items()))
            lines.append(f"Step {letter}: {shown or 'nothing'}.")
        if run.error:
            lines.append(f"Error: {run.error}")
        self.stdout.write("\n".join(lines))
        if run.status == ReconciliationStatus.FAILED:
            raise CommandError(f"The reconciliation failed at step {run.step_reached or '-'}.")

    def _queue_if_due(self) -> None:
        due, reason = should_run_scheduled()
        if not due:
            run = skipped(ReconciliationTrigger.SCHEDULED, reason)
            self.stdout.write(f"Status: {run.status}. {reason}")
            return
        jobs.queue_scheduled_reconciliation()
        self.stdout.write(QUEUED)
