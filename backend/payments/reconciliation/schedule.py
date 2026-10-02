"""When the scheduled run is due (docs/qa/PLAN-010-worker-and-reconciliation.md D5).

Container Apps cron is UTC only, so the job fires at 06:00 and 07:00 UTC every day; one
of the two is the configured local time (02:00 in New York) whatever daylight saving
does. A firing runs when the local wall-clock time is inside the window that starts at
``RECONCILE_LOCAL_TIME`` and lasts :data:`WINDOW`, and no scheduled run has started on
that local date yet. The window is two hours because on the day clocks go forward the
local time 02:xx does not exist: that day the 07:00 UTC firing reads 03:00 and still
runs, and the 06:00 one (01:00) does not. On the day clocks go back the 06:00 UTC firing
reads 01:00 and the 07:00 one 02:00. On every other day the second firing finds the
first one's run, or its ``reconcile_run`` job still queued or running, and stops: the
job container only queues the run and the worker runs it (D5), so the run may not have
started yet when the second firing comes. "Run now" never asks this module.

A firing that is not due writes a ``skipped`` row whose reason starts with
:data:`NOT_DUE`; the reconciliation page leaves those rows out of its run list, so one
firing a day that is not due does not fill it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from payments import jobs
from payments.models import ReconciliationRun, ReconciliationStatus, ReconciliationTrigger

#: How long after the configured local time a firing may still start the day's run.
WINDOW = timedelta(hours=2)
DUE = "due"
#: How every reason of a firing that is not due starts.
NOT_DUE = "Not due:"
ALREADY_STARTED = f"{NOT_DUE} a scheduled run already started or is queued on this local date."
OUTSIDE_WINDOW = f"{NOT_DUE} outside the local run window."
#: Runs that count as "started on that local date"; a skipped firing does not.
_STARTED = (ReconciliationStatus.RUNNING, ReconciliationStatus.DONE, ReconciliationStatus.FAILED)


def zone() -> ZoneInfo:
    return ZoneInfo(settings.RECONCILE_TIMEZONE)


def local(moment: datetime) -> datetime:
    """``moment`` in the reconciliation's time zone."""
    return moment.astimezone(zone())


def ran_in(start: datetime, *, now: datetime) -> bool:
    """Whether a scheduled run started, or a scheduled ``reconcile_run`` job still queued
    or running was created, inside the window that opens at local wall time ``start``:
    the job is queued only inside a window, so this is "started that day"."""
    since = now - timedelta(days=3)
    recent = [
        *ReconciliationRun.objects.filter(
            trigger=ReconciliationTrigger.SCHEDULED,
            status__in=_STARTED,
            started_at__gte=since,
        ).values_list("started_at", flat=True),
        *jobs.scheduled_reconciliations_waiting(since),
    ]
    return any(start <= local(moment).replace(tzinfo=None) < start + WINDOW for moment in recent)


def should_run_scheduled(now: datetime | None = None) -> tuple[bool, str]:
    """``(True, "due")``, or ``(False, reason)`` with a fixed sentence.

    The run's local date is the date of the window the firing falls in (a window that
    starts late in the evening ends on the next date).
    """
    moment = now or timezone.now()
    wall = local(moment).replace(tzinfo=None)
    for day in (wall.date(), wall.date() - timedelta(days=1)):
        start = datetime.combine(day, settings.RECONCILE_LOCAL_TIME)
        if start <= wall < start + WINDOW:
            if ran_in(start, now=moment):
                return False, ALREADY_STARTED
            return True, DUE
    return False, OUTSIDE_WINDOW
