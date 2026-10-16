"""The home page, the month grid and the arrears list
(docs/qa/PLAN-011-dashboard-reports-mobile.md D1 to D3, D10).

Read only and open to every role: ``accounts.registry`` lists every route here as
read-only, so any unsafe method is refused (403), and the views take GET and HEAD only
(405 otherwise). "Today" is the date in the project's ``TIME_ZONE``. The query strings
are read by :func:`ledger.services.grid.parse_query` and
:func:`ledger.services.receivables.parse_arrears_filter`, which never fail: an unusable
value is dropped and the page says so. Each export applies the page's query. The grid,
the arrears list and their exports run inside the reports' statement timeout
(:func:`ledger.views.reports.bounded`); a cancelled query answers the page with the
sentence :data:`ledger.views.reports.TOO_SLOW`.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import conditional_escape, format_html, format_html_join
from django.utils.safestring import SafeString
from django.views.decorators.http import require_safe

from accounts.roles import Role, meets, role_of
from ledger.models import PaymentChannel, PaymentStatus
from ledger.services import attention, grid, one_off_charges, receivables, rent_run
from ledger.views.operators import csv_response, role_required
from ledger.views.reports import TOO_SLOW, ReportTooSlow, bounded


@role_required
@require_safe
def home(request: HttpRequest) -> HttpResponse:
    """``ledger:home`` (D1): what needs attention (PLAN-011b D6), then who owes what,
    and since when."""
    today = timezone.localdate()
    data = receivables.dashboard(today)
    needs = attention.attention(today)
    payments_query = urlencode(
        {
            "date_from": data.month_start.isoformat(),
            "date_to": data.month_end.isoformat(),
            "status": PaymentStatus.SETTLED,
        }
    )
    return render(
        request,
        "ledger/dashboard/home.html",
        {
            "data": data,
            "attention": needs,
            # PLAN-012c-b D2: Kept, Reschedule and Broken on a broken promise.
            "can_record_outcome": meets(role_of(request.user), Role.BILLING),
            "payments_query": payments_query,
            # PLAN-012d D11: today's rent run in one line, from its audit rows.
            "run_line": rent_run.run_line(today),
            "waived_live_heading": one_off_charges.WAIVED_LIVE_HEADING,
            "not_sent": [
                (invoice, receivables.invoice_store_label(invoice)) for invoice in data.not_sent
            ],
            "failed_attempts": [
                (invoice, receivables.invoice_store_label(invoice))
                for invoice in data.failed_attempts
            ],
        },
    )


#: Stands for a charge's id in its page's URL, reversed once per page (PLAN-011 D6).
_CHARGE_PK = "00000000-0000-0000-0000-000000000000"
_NOTE = format_html('<span class="note-marker" title="Has a note">{}</span>', grid.NOTE_MARKER)


def cell_html(cell: grid.Cell, charge_url: str) -> SafeString:
    """One grid cell, as the template wrote it, built here: 12,000 cells through the
    template engine (a ``{% url %}`` each) took longer than the page's 2 s (D6)."""
    css = f"cell cell-{cell.status}" if cell.status else "cell"
    if cell.charge_id is not None and cell.proration_text:
        # PLAN-012e D2: a prorated month shows its arithmetic as the link's title.
        inner = format_html(
            '<a href="{}" aria-label="{}" title="{}">{}{}</a>',
            charge_url.replace(_CHARGE_PK, str(cell.charge_id)),
            cell.label,
            cell.proration_text,
            cell.text,
            _NOTE if cell.has_note else "",
        )
    elif cell.charge_id is not None:
        inner = format_html(
            '<a href="{}" aria-label="{}">{}{}</a>',
            charge_url.replace(_CHARGE_PK, str(cell.charge_id)),
            cell.label,
            cell.text,
            _NOTE if cell.has_note else "",
        )
    else:
        inner = conditional_escape(cell.text)
    return format_html(
        '<td class="{}" data-month="{}" data-status="{}">{}</td>',
        css,
        f"{cell.month:%Y-%m}",
        cell.status,
        inner,
    )


def _grid_rows(built: grid.Grid) -> list[tuple[grid.GridRow, SafeString]]:
    charge_url = reverse("ledger:charges:detail", kwargs={"pk": _CHARGE_PK})
    return [
        (row, format_html_join("", "{}", ((cell_html(cell, charge_url),) for cell in row.cells)))
        for row in built.rows
    ]


def _grid_page(
    request: HttpRequest, query: grid.GridQuery, built: grid.Grid | None
) -> HttpResponse:
    return render(
        request,
        "ledger/grid/page.html",
        {
            "query": query,
            # PLAN-012d D12: the month selects offer the current month +/- 24.
            "picker_months": grid.picker_months(query),
            "grid": built,
            "rows": _grid_rows(built) if built is not None else [],
            "too_slow": TOO_SLOW if built is None else "",
            "export_query": urlencode(query.as_query()),
            # PLAN-012d D16: "Clear" drops the words filter and keeps the rest.
            "clear_query": urlencode(
                {name: value for name, value in query.as_query().items() if name != "q"}
            ),
            "operators": receivables.arrears_operators(),
            "channels": PaymentChannel.choices,
            "statuses": grid.CELL_STATUSES,
            "note_marker": grid.NOTE_MARKER,
        },
    )


@role_required
@require_safe
def grid_page(request: HttpRequest) -> HttpResponse:
    """``ledger:grid:page`` (D2)."""
    query = grid.parse_query(request.GET)
    try:
        built = bounded(lambda: grid.build(query))
    except ReportTooSlow:
        built = None
    return _grid_page(request, query, built)


@role_required
@require_safe
def grid_export(request: HttpRequest) -> HttpResponseBase:
    """``ledger:grid:export`` (D2): the grid in the sheet's column order."""
    query = grid.parse_query(request.GET)
    try:
        built = bounded(lambda: grid.build(query))
    except ReportTooSlow:
        return _grid_page(request, query, None)
    return csv_response(
        grid.csv_lines(built),
        f"grid-{query.from_month:%Y-%m}-to-{query.to_month:%Y-%m}"
        f"-{timezone.localdate():%Y-%m-%d}.csv",
    )


def _arrears_page(
    request: HttpRequest,
    selected: receivables.ArrearsFilter,
    report: receivables.ArrearsReport | None,
) -> HttpResponse:
    base = {
        name: value for name, value in selected.as_query().items() if name not in ("sort", "dir")
    }
    sort_links = {
        sort: urlencode(
            {
                **base,
                "sort": sort,
                "dir": "asc" if selected.sort == sort and selected.descending else "desc",
            }
        )
        for sort in receivables.SORTS
    }
    return render(
        request,
        "ledger/arrears/page.html",
        {
            "selected": selected,
            "report": report,
            "too_slow": TOO_SLOW if report is None else "",
            "export_query": urlencode(selected.as_query()),
            "sort_links": sort_links,
            "operators": receivables.arrears_operators(),
            "buckets": [
                (bucket, receivables.BUCKET_LABELS[bucket]) for bucket in receivables.BUCKETS
            ],
        },
    )


@role_required
@require_safe
def arrears_page(request: HttpRequest) -> HttpResponse:
    """``ledger:arrears:page`` (D3)."""
    selected = receivables.parse_arrears_filter(request.GET)
    today = timezone.localdate()
    try:
        report = bounded(lambda: receivables.arrears(today, selected))
    except ReportTooSlow:
        report = None
    return _arrears_page(request, selected, report)


@role_required
@require_safe
def arrears_export(request: HttpRequest) -> HttpResponseBase:
    """``ledger:arrears:export`` (D3): the filtered and sorted rows."""
    selected = receivables.parse_arrears_filter(request.GET)
    today = timezone.localdate()
    try:
        report = bounded(lambda: receivables.arrears(today, selected))
    except ReportTooSlow:
        return _arrears_page(request, selected, None)
    return csv_response(receivables.arrears_csv_lines(report), f"arrears-{today:%Y-%m-%d}.csv")
