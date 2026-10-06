"""The audit log page and its CSV export (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md
D8, D9).

Admin only. ``accounts.registry`` lists both routes as read-only, so any unsafe method
is refused (403) for every role; the views take GET and HEAD only (405 otherwise) and
refuse anyone below admin themselves (403). The query string is read by
:func:`ledger.services.audit.parse_query`, which never fails. Every query runs under the
report statement timeout (:func:`ledger.views.reports.bounded`): a cancelled one answers
200 with the page and :data:`ledger.views.reports.TOO_SLOW`. The export reads all its
records inside that block, then streams them.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.services import audit
from ledger.views.operators import _role, csv_response, role_required
from ledger.views.reports import TOO_SLOW, ReportTooSlow, bounded

#: The record page of each linked entity type (``ledger.services.audit.LINKED_TYPES``
#: and the owners of documents).
RECORD_PAGES: dict[str, str] = {
    "operator": "ledger:operators:detail",
    "store": "ledger:stores:detail",
    "lease": "ledger:leases:detail",
    "one_off_charge": "ledger:one_off_charges:detail",
    "rent_charge": "ledger:charges:detail",
    "payment": "ledger:payments:detail",
}


def _link(row: audit.AuditRow) -> str:
    if row.record is None:
        return ""
    kind, pk = row.record
    return reverse(RECORD_PAGES[kind], kwargs={"pk": pk})


def _page(
    request: HttpRequest, query: audit.AuditQuery, found: audit.AuditPage | None
) -> HttpResponse:
    return render(
        request,
        "ledger/audit/page.html",
        {
            "query": query,
            "too_slow": TOO_SLOW if found is None else "",
            "messages_shown": found.messages if found is not None else query.messages,
            "rows": [(row, _link(row)) for row in found.rows] if found is not None else [],
            "previous_query": urlencode(query.as_query(page=query.page - 1))
            if query.page > 1
            else "",
            "next_query": urlencode(query.as_query(page=query.page + 1))
            if found is not None and found.has_next
            else "",
            "export_query": urlencode(query.as_query(page=1)),
            "max_days": audit.MAX_DAYS,
            "page_size": audit.PAGE_SIZE,
        },
    )


@role_required
@require_safe
def audit_page(request: HttpRequest) -> HttpResponse:
    """``ledger:audit:page`` (admin)."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    query = audit.parse_query(request.GET)
    try:
        found = bounded(lambda: audit.rows(query))
    except ReportTooSlow:
        return _page(request, query, None)
    return _page(request, query, found)


@role_required
@require_safe
def audit_export(request: HttpRequest) -> HttpResponseBase:
    """``ledger:audit:export`` (admin): the filtered rows as CSV."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    query = audit.parse_query(request.GET)
    try:
        records = bounded(lambda: list(audit.csv_records(query)))
    except ReportTooSlow:
        return _page(request, query, None)
    return csv_response(
        audit.csv_lines_of(records),
        f"audit-{query.date_from:%Y-%m-%d}-to-{query.date_to:%Y-%m-%d}"
        f"-{timezone.localdate():%Y-%m-%d}.csv",
    )
