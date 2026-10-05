"""The profit and loss report and its CSV export (docs/qa/PLAN-010b D11, D12).

Read only and open to every role: ``accounts.registry`` lists both routes as read-only,
so any unsafe method is refused (403), and the views take GET and HEAD only (405
otherwise). The query string is read by :func:`ledger.services.pnl.parse_query`, which
never fails: an unusable range is replaced and the page says so. The export applies
the same query and streams its lines.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_safe

from ledger.services import pnl
from ledger.views.operators import role_required


@role_required
@require_safe
def pnl_report(request: HttpRequest) -> HttpResponse:
    query = pnl.parse_query(request.GET)
    rows = pnl.rows(query)
    return render(
        request,
        "ledger/reports/pnl.html",
        {
            "query": query,
            "groups": pnl.grouped(rows, query.group),
            "total": pnl.total(rows),
            "row_count": len(rows),
            "cost_only_count": sum(1 for row in rows if row.cost_only),
            "deposits_held": pnl.deposits_held(query),
            "group_choices": pnl.GROUP_CHOICES,
            "group_label": dict(pnl.GROUP_CHOICES)[query.group],
            "export_query": urlencode(query.as_query()),
            "toggle_query": _toggle(query),
        },
    )


def _toggle(query: pnl.PnlQuery) -> str:
    """The same query with the breakdown switched."""
    values = {name: value for name, value in query.as_query().items() if name != "detail"}
    if not query.detail:
        values["detail"] = "1"
    return urlencode(values)


@role_required
@require_safe
def pnl_export(request: HttpRequest) -> StreamingHttpResponse:
    query = pnl.parse_query(request.GET)
    response = StreamingHttpResponse(pnl.csv_lines(query), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="pnl-{query.from_month:%Y-%m}-to-{query.to_month:%Y-%m}'
        f'-{timezone.localdate():%Y-%m-%d}.csv"'
    )
    response["Cache-Control"] = "no-store"
    return response
