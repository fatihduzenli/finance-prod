"""The dispute list (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D8, D9).

``billing`` and above read the list and its CSV export and press **Refresh** (checked
here; the read routes are read-only in ``accounts.registry``); ``admin`` adds evidence and
sends failed evidence again (registry and again in the service). Nothing here reads the
provider: Refresh queues ``disputes_sync`` and the page shows the last read with its
time. Disputes are listed by respond-by time; fewer than seven days left (or past) is
highlighted. The provider's transaction is shown masked only.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import DisputeEvidenceForm
from ledger.models import Dispute, DisputeEvidence, DisputeEvidenceStatus
from ledger.services import csv_export, disputes
from ledger.services.csv_export import CsvWriter
from ledger.views.jobs import job_context
from ledger.views.operators import _actor, _role, csv_response, export_name, role_required
from ledger.views.run import _billing_only
from payments import jobs
from payments.models import JobKind

ANCHOR = "#dispute-"


def _page_url() -> str:
    return reverse("ledger:disputes:page")


def _parse(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _evidence_row(evidence: DisputeEvidence) -> dict[str, Any]:
    job = disputes.evidence_job(evidence)
    return {
        "evidence": evidence,
        "job_text": jobs.describe(job) if job is not None else "",
        "can_retry": evidence.status == DisputeEvidenceStatus.FAILED,
    }


def _row(dispute: Dispute, today: date) -> dict[str, Any]:
    left = disputes.days_left(dispute, today)
    return {
        "dispute": dispute,
        "days_left": left,
        "past": disputes.is_past(dispute),
        "soon": left is not None and left < disputes.HIGHLIGHT_DAYS,
        "transaction": disputes.masked_transaction(dispute),
        "evidence": [
            _evidence_row(item)
            for item in sorted(dispute.evidence.all(), key=lambda row: (row.created_at, row.pk))
        ],
    }


def _rows(today: date) -> list[dict[str, Any]]:
    found = disputes.ordered().prefetch_related("evidence__document")
    return [_row(dispute, today) for dispute in found]


@role_required
@require_safe
def disputes_page(request: HttpRequest) -> HttpResponse:
    """``ledger:disputes:page`` (billing and above)."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    return render(
        request,
        "ledger/disputes/page.html",
        {
            **job_context(jobs.DISPUTES, [JobKind.DISPUTES_SYNC]),
            "rows": _rows(timezone.localdate()),
            "last_synced": disputes.last_synced(),
            "can_add_evidence": meets(_role(request), Role.ADMIN),
            "evidence_form": DisputeEvidenceForm(),
            "notice": disputes.NOTICE,
            "highlight_days": disputes.HIGHLIGHT_DAYS,
        },
    )


EXPORT_HEADER = (
    "Respond by",
    "Days left",
    "Status",
    "Reason",
    "Amount (USD)",
    "Operator",
    "Store",
    "Payment ID",
    "Provider transaction (masked)",
    "Created at provider",
    "Updated at provider",
    "Evidence files",
    "Read at",
)


def _export_record(item: dict[str, Any]) -> list[object]:
    dispute: Dispute = item["dispute"]
    return [
        dispute.respond_by,
        item["days_left"],
        dispute.status,
        dispute.reason,
        dispute.amount,
        dispute.operator.display_name if dispute.operator is not None else "",
        dispute.store.external_label if dispute.store is not None else "",
        str(dispute.payment_id) if dispute.payment_id is not None else "",
        item["transaction"],
        dispute.provider_created_at,
        dispute.changed_at,
        len(item["evidence"]),
        dispute.fetched_at,
    ]


@role_required
@require_safe
def disputes_export(request: HttpRequest) -> HttpResponse | StreamingHttpResponse:
    """``ledger:disputes:export``: the list as the page shows it (billing and above)."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    rows = _rows(timezone.localdate())[: csv_export.CSV_MAX_ROWS + 1]
    return csv_response(
        CsvWriter(EXPORT_HEADER).lines(_export_record(item) for item in rows),
        export_name("disputes"),
    )


@role_required
@require_POST
def disputes_refresh(request: HttpRequest) -> HttpResponse:
    """``ledger:disputes:refresh`` (billing): queue a read of the provider's list."""
    outcome = disputes.refresh(_actor(request))
    if outcome.forbidden:
        return forbidden(request)
    if outcome.ok:
        messages.info(request, outcome.message)
    else:
        messages.warning(request, outcome.message)
    return redirect("ledger:disputes:page")


@role_required
@require_POST
def dispute_evidence(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:disputes:evidence`` (admin): store one file and queue its upload."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    dispute = get_object_or_404(Dispute.objects.select_related("operator"), pk=_parse(pk))
    back = f"{_page_url()}{ANCHOR}{dispute.pk}"
    form = DisputeEvidenceForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, str(error))
        return redirect(back)
    outcome = disputes.add_evidence(
        dispute,
        form.cleaned_data["file"],
        form.cleaned_data["name"],
        _actor(request),
        acknowledged=form.cleaned_data.get("acknowledged") is True,
    )
    if outcome.forbidden:
        return forbidden(request)
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(back)


@role_required
@require_POST
def dispute_evidence_retry(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:disputes:evidence_retry`` (admin): send failed evidence again."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    evidence = get_object_or_404(DisputeEvidence, pk=_parse(pk))
    outcome = disputes.retry_evidence(evidence, _actor(request))
    if outcome.forbidden:
        return forbidden(request)
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(f"{_page_url()}{ANCHOR}{evidence.dispute_id}")
