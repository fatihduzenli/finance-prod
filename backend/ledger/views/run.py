"""The monthly rent run screen (PLAN-007 §4.1, 7b).

``run:today`` lists the debits due today with last month's outcome, the leases due
today that are skipped and why, the insurance due in the next seven days, and a banner
when the payment provider may not be called (breaker open, failure budget exhausted).
It reads only: no provider call and no write.

``run:create_charges`` and ``run:match`` need ``billing`` (D10; checked by
``accounts.registry`` and again here). Both always act on the server's today, never a
date from the request, and end in a redirect with the outcome as messages.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.services import insurance, rent_run
from ledger.services.rent_run import MatchAction
from ledger.views.operators import _actor, _role, role_required
from payments.health import provider_health

#: Most report lines shown after a run; the rest are counted.
MAX_LINES = 20


@role_required
@require_safe
def run_today(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    return render(
        request,
        "ledger/run/today.html",
        {
            "run": rent_run.leases_due(today),
            "insurance_due": insurance.insurance_due_soon(today),
            "insurance_days": insurance.DUE_SOON_DAYS,
            "health": provider_health(),
            "threshold": settings.LARGE_DEBIT_WARNING_AMOUNT,
            "can_run": meets(_role(request), Role.BILLING),
        },
    )


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
    report = rent_run.match_schedule_invoices(actor=_actor(request))
    for text in report.refused:
        messages.error(request, text)
    if report.refused:
        return redirect("ledger:run:today")
    messages.success(
        request,
        f"Schedules read: {report.schedules}. Linked: {report.count(MatchAction.LINKED)}. "
        f"Linked with a total mismatch: {report.count(MatchAction.MISMATCH)}. "
        f"Already linked: {report.count(MatchAction.ALREADY)}. "
        f"Not linked: {report.count(MatchAction.REPORTED)}.",
    )
    shown = [line for line in report.lines if line.action is not MatchAction.ALREADY]
    for line in shown[:MAX_LINES]:
        text = f"Invoice {line.invoice}: {line.message}"
        if line.action is MatchAction.LINKED:
            messages.info(request, text)
        else:
            messages.warning(request, text)
    if report.failure:
        messages.error(request, report.failure)
    return redirect("ledger:run:today")
