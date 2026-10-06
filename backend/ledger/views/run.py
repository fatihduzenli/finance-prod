"""The monthly rent run screen (PLAN-007 §4.1, 7b).

``run:today`` lists the debits due today with last month's outcome, the leases due
today that are skipped and why, the insurance due in the next seven days, and a banner
when the payment provider may not be called (breaker open, failure budget exhausted).
It reads only: no provider call and no write.

``run:create_charges`` and ``run:match`` need ``billing`` (D10; checked by
``accounts.registry`` and again here). Both end in a redirect. Creating charges acts on
the server's today, never a date from the request, with the outcome as messages.
"Match invoices" reads the provider, so it is queued as a ``run_match`` job for the
worker (PLAN-010 D3); the page shows the latest one and its report lines.

``run:export`` (PLAN-011 D5) is the debits due today as CSV, any role.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.services import insurance, rent_run
from ledger.services.csv_export import CsvWriter
from ledger.views.jobs import busy, job_context, submit
from ledger.views.operators import _actor, _role, csv_response, export_name, role_required
from payments import jobs
from payments.health import provider_health
from payments.models import JobKind, JobStatus, WebhookEvent, WebhookOutcome

#: Most report lines shown after a run; the rest are counted.
MAX_LINES = 20
MATCH_QUEUED = "Matching queued. The report appears below when it is done."


def _match_context() -> dict[str, Any]:
    """The latest "Match invoices" job: its state, and its stored report lines once it
    finished (the first line goes with the state)."""
    context = job_context(jobs.RENT_RUN)
    job = context["job"]
    finished = job is not None and job.status in (JobStatus.DONE, JobStatus.FAILED)
    lines = context["job_text"].splitlines() if finished else []
    if lines:
        context["job_text"] = lines[0]
    context["match_lines"] = lines[1:]
    return context


@role_required
@require_safe
def run_today(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    return render(
        request,
        "ledger/run/today.html",
        {
            **_match_context(),
            "run": rent_run.leases_due(today),
            "insurance_due": insurance.insurance_due_soon(today),
            "insurance_days": insurance.DUE_SOON_DAYS,
            "health": provider_health(),
            "threshold": settings.LARGE_DEBIT_WARNING_AMOUNT,
            "can_run": meets(_role(request), Role.BILLING),
            "dead_letters": WebhookEvent.objects.filter(outcome=WebhookOutcome.DEAD_LETTER).count(),
        },
    )


EXPORT_HEADER = (
    "Store",
    "Store number",
    "Operator",
    "Debit of the month",
    "Debit day",
    "Channel",
    "Amount (USD)",
    "Carried balance (USD)",
    "Status",
    "Last month status",
    "Last month balance (USD)",
)
NOT_CREATED = "Not created"


@role_required
@require_safe
def run_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:run:export`` (PLAN-011 D5): the debits due today, as the page lists them."""
    run = rent_run.leases_due(timezone.localdate())
    records = (
        [
            item.lease.store.external_label,
            item.lease.store.store_number,
            item.lease.operator.display_name if item.lease.operator is not None else "",
            item.seq,
            item.debit_day,
            item.lease.get_payment_channel_display(),
            item.amount,
            item.carried_balance,
            item.charge.get_status_display() if item.charge is not None else NOT_CREATED,
            item.last_month.get_status_display() if item.last_month is not None else "",
            item.last_month.balance if item.last_month is not None else None,
        ]
        for item in run.due
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("rent-run"))


def _billing_only(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    return None


@role_required
@require_POST
def run_create_charges(request: HttpRequest) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    report = rent_run.create_charges(timezone.localdate(), actor=_actor(request))
    messages.success(
        request,
        f"Charges created: {len(report.created)}. Already created: {len(report.already)}. "
        f"Skipped leases: {len(report.skipped)}.",
    )
    for item in report.skipped[:MAX_LINES]:
        messages.warning(request, f"Skipped {item.lease.store.external_label}: {item.reason}")
    return redirect("ledger:run:today")


@role_required
@require_POST
def run_match(request: HttpRequest) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    health = provider_health()
    if not health.ok:
        for text in health.messages:
            messages.error(request, text)
        return redirect("ledger:run:today")
    if not busy(request, jobs.RENT_RUN) and submit(request, JobKind.RUN_MATCH, jobs.RENT_RUN):
        messages.info(request, MATCH_QUEUED)
    return redirect("ledger:run:today")
