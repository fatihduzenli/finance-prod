"""The nightly reconciliation (docs/qa/PLAN-010-worker-and-reconciliation.md D5 to D10).

:func:`run_reconciliation` is the one entry point: the worker's ``reconcile_run`` job
(queued by "Run now", or by ``reconcile --scheduled`` after
:func:`payments.reconciliation.schedule.should_run_scheduled`) and the manual
``reconcile`` command call it. Under the lock (:mod:`.lock`, D6) it writes a
``reconciliation_runs`` row, estimates the requests the run needs and refuses a run that
would spend more than half the hourly budget (docs/CLAUDE.md Rule 3), runs the steps a
to e (:mod:`.steps`) through the rate-limited provider, and then (f) completes the row
and advances ``reconcile_cursor`` to the run's start, only when every step completed.
A failure leaves the cursor where it was, records the step reached and the error's class
name, and the next run redoes the window (D9). One ``fatal`` Sentry event goes out per
run with new mismatches and one per failed run (D8).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

import payments
from payments import alerts
from payments.models import (
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationStep,
    SyncState,
)
from payments.reconciliation import lock, steps
from payments.stax.limiter import PER_MINUTE, get_limiter

#: The ``sync_state`` row holding ``reconcile_cursor`` (ISO 8601 in ``cursor``).
CURSOR_ROW = "reconciliation"
#: The provider lists each run reads at least one page of: transactions, deposits and
#: ACH rejections.
LIST_PAGES = 3
#: Half the hourly budget, in requests (docs/CLAUDE.md Rule 3).
BUDGET_REQUESTS = PER_MINUTE * 60 // 2
ALREADY_RUNNING = "Skipped: a reconciliation is already running."
#: The error of a ``running`` row found while the lock was free: its process stopped.
ABANDONED = "Abandoned"
BUDGET_EXCEEDED = "BudgetExceeded"

logger = logging.getLogger(__name__)


class BudgetExceeded(Exception):  # noqa: N818 (reads as a state, like the provider errors)
    """The run would spend more than half the hourly request budget."""


def reconcile_cursor() -> datetime | None:
    """The start of the last run that completed every step, or ``None``."""
    row = SyncState.objects.filter(job_name=CURSOR_ROW).first()
    if row is None or not row.cursor:
        return None
    try:
        return datetime.fromisoformat(row.cursor)
    except ValueError:
        return None


def window_start(now: datetime) -> datetime:
    """``reconcile_cursor − RECONCILE_OVERLAP_HOURS``, or ``now − RECONCILE_LOOKBACK_DAYS``
    on the first run; never more than 364 days back (the provider's ranges span a year
    at most) and never after ``now`` (a range must run forwards)."""
    cursor = reconcile_cursor()
    if cursor is None:
        start = now - timedelta(days=settings.RECONCILE_LOOKBACK_DAYS)
    else:
        start = cursor - timedelta(hours=settings.RECONCILE_OVERLAP_HOURS)
    return min(max(start, now - timedelta(days=364)), now)


def estimate_requests(now: datetime) -> int:
    """Invoices to re-fetch plus one page per list."""
    return len(steps.invoices_to_check(now)) + LIST_PAGES


def skipped(
    trigger: str, reason: str, *, now: datetime | None = None, actor: User | None = None
) -> ReconciliationRun:
    """A ``skipped`` row: the run did not start, for ``reason`` (a fixed sentence)."""
    moment = now or timezone.now()
    return ReconciliationRun.objects.create(
        trigger=trigger,
        status=ReconciliationStatus.SKIPPED,
        started_at=moment,
        finished_at=moment,
        duration_ms=0,
        error=reason,
        requested_by=actor,
    )


def run_reconciliation(
    trigger: str, *, now: datetime | None = None, actor: User | None = None
) -> ReconciliationRun:
    """Run once under the lock; a second start meanwhile is recorded as skipped."""
    started = now or timezone.now()
    with lock.held() as acquired:
        if not acquired:
            return skipped(trigger, ALREADY_RUNNING, now=started, actor=actor)
        # The lock was free, so no run is in progress: a row still ``running`` belongs
        # to a process that stopped mid-run.
        ReconciliationRun.objects.filter(status=ReconciliationStatus.RUNNING).update(
            status=ReconciliationStatus.FAILED, error=ABANDONED, finished_at=started
        )
        run = ReconciliationRun.objects.create(
            trigger=trigger,
            status=ReconciliationStatus.RUNNING,
            started_at=started,
            requested_by=actor,
        )
        return _execute(run)


def _execute(run: ReconciliationRun) -> ReconciliationRun:
    clock = time.monotonic_ns()
    limiter = get_limiter()
    departures = limiter.departures
    ctx = steps.Context(
        run=run,
        provider=payments.get_provider(),
        now=run.started_at,
        window_start=window_start(run.started_at),
    )
    try:
        if estimate_requests(run.started_at) > BUDGET_REQUESTS:
            raise BudgetExceeded
        for letter, step in steps.STEPS:
            run.step_reached = letter
            run.save(update_fields=["step_reached"])
            run.counts[letter] = dict(step(ctx))
            run.save(update_fields=["counts"])
        run.step_reached = ReconciliationStep.REPORT
        run.status = ReconciliationStatus.DONE
    except Exception as error:  # any failure is recorded by class name; the cursor stays
        run.status = ReconciliationStatus.FAILED
        run.error = type(error).__name__
        logger.warning("Reconciliation failed at step %s: %s", run.step_reached, run.error)
    run.finished_at = timezone.now()
    run.duration_ms = (time.monotonic_ns() - clock) // 1_000_000
    run.requests = max(limiter.departures - departures, 0)
    run.save()
    if run.status == ReconciliationStatus.DONE:
        SyncState.objects.update_or_create(
            job_name=CURSOR_ROW,
            defaults={"cursor": run.started_at.isoformat(), "last_success_at": run.started_at},
        )
    if ctx.new_by_kind:
        alerts.reconciliation_mismatches(ctx.new_by_kind)
    if run.status == ReconciliationStatus.FAILED:
        alerts.reconciliation_failed(run.step_reached, run.error)
    return run
