"""The reconciliation page (docs/qa/PLAN-010-worker-and-reconciliation.md D10).

``billing`` and above may read it (checked here; the route is read-only in
``accounts.registry``) and resolve a mismatch with a note; "Run now" needs ``admin``
(registry and again here) and is queued as a ``reconcile_run`` job for the worker, which
runs the same locked reconciliation as the scheduled job. Nothing here reads the
provider. Every identifier is shown masked; times of a run are shown in the
reconciliation's time zone. The run list leaves out the ``skipped`` rows of scheduled
firings that were not due (one a day, see :mod:`payments.reconciliation.schedule`).

The mismatches export as CSV (``reconciliation:export``, PLAN-011 D5) for ``billing`` and
above, like the page.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import F, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from finance.dates import day_text
from ledger.forms import ResolveMismatchForm
from ledger.services import csv_export
from ledger.services.csv_export import CsvWriter
from ledger.views.jobs import busy, job_context, submit
from ledger.views.leases import _admin_only
from ledger.views.operators import (
    PAGE_SIZE,
    _actor,
    _role,
    csv_response,
    export_name,
    role_required,
)
from ledger.views.run import _billing_only
from payments import jobs
from payments.models import (
    JobKind,
    ReconciliationMismatch,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationStep,
    ReconciliationTrigger,
)
from payments.provider import mask_identifier
from payments.reconciliation import mismatches, schedule

#: Recent runs listed under the last one.
RECENT_RUNS = 10
RUN_QUEUED = (
    "The nightly check is starting; its progress shows below; the report follows when it is done."
)


def _local(moment: datetime | None) -> str:
    if moment is None:
        return ""
    # The day as a page reads it (DESIGN-SPEC §5), in the reconciliation's own zone.
    local = schedule.local(moment)
    return f"{day_text(local.date())} · {local:%H:%M %Z}"


def _run_row(run: ReconciliationRun) -> dict[str, Any]:
    labels = dict(ReconciliationStep.choices)
    return {
        "run": run,
        "started_local": _local(run.started_at),
        "steps": [
            {
                "letter": letter,
                "label": labels.get(letter, letter),
                "counts": sorted(counts.items()),
            }
            for letter, counts in sorted(run.counts.items())
        ],
    }


def _mismatches() -> QuerySet[ReconciliationMismatch]:
    """Every mismatch, open ones first, newest first."""
    return ReconciliationMismatch.objects.select_related("run", "resolved_by").order_by(
        F("resolved_at").asc(nulls_first=True), "-created_at", "-id"
    )


@role_required
@require_safe
def reconciliation_page(request: HttpRequest) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    last = (
        ReconciliationRun.objects.exclude(status=ReconciliationStatus.SKIPPED)
        .order_by("-started_at", "-id")
        .first()
    )
    recent = ReconciliationRun.objects.exclude(
        trigger=ReconciliationTrigger.SCHEDULED,
        status=ReconciliationStatus.SKIPPED,
        error__startswith=schedule.NOT_DUE,
    ).order_by("-started_at", "-id")[:RECENT_RUNS]
    page = Paginator(_mismatches(), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/reconciliation/page.html",
        {
            **job_context(jobs.RECONCILIATION),
            "last": _run_row(last) if last is not None else None,
            "recent": [_run_row(run) for run in recent],
            "page": page,
            "rows": [
                {
                    "mismatch": mismatch,
                    "entity_masked": mask_identifier(str(mismatch.entity_id)),
                    "found_local": _local(mismatch.created_at),
                }
                for mismatch in page
            ],
            "open_count": ReconciliationMismatch.objects.filter(resolved_at__isnull=True).count(),
            "timezone": schedule.zone().key,
            "can_run": meets(_role(request), Role.ADMIN),
        },
    )


EXPORT_HEADER = (
    "Found at",
    "Mismatch",
    "Record type",
    "Record (masked)",
    "Detail",
    "Resolved at",
    "Resolved by",
    "Resolution note",
)


@role_required
@require_safe
def reconciliation_export(request: HttpRequest) -> HttpResponse | StreamingHttpResponse:
    """``ledger:reconciliation:export`` (PLAN-011 D5): every mismatch, as the page lists
    them; ``billing`` and above, like the page."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    records = (
        [
            mismatch.created_at,
            mismatch.get_kind_display(),
            mismatch.entity_type,
            mask_identifier(str(mismatch.entity_id)),
            mismatch.detail,
            mismatch.resolved_at,
            mismatch.resolved_by.get_username() if mismatch.resolved_by is not None else "",
            mismatch.resolution_note,
        ]
        for mismatch in _mismatches()[: csv_export.CSV_MAX_ROWS + 1].iterator(chunk_size=2_000)
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("reconciliation"))


@role_required
@require_POST
def reconciliation_run(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    if not busy(request, jobs.RECONCILIATION) and submit(
        request, JobKind.RECONCILE_RUN, jobs.RECONCILIATION
    ):
        messages.info(request, RUN_QUEUED)
    return redirect("ledger:reconciliation:page")


@role_required
@require_POST
def reconciliation_resolve(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    if not ReconciliationMismatch.objects.filter(pk=key).exists():
        raise Http404
    form = ResolveMismatchForm(request.POST)
    if not form.is_valid():
        errors = form.errors.get("note") or [mismatches.NOTE_REQUIRED]
        messages.error(request, str(errors[0]))
        return redirect("ledger:reconciliation:page")
    outcome = mismatches.resolve(key, note=form.cleaned_data["note"], actor=_actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:reconciliation:page")
