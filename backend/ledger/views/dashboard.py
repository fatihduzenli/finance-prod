"""The home page, the month grid and the arrears list
(docs/qa/PLAN-011-dashboard-reports-mobile.md D1 to D3, D10; the home page as Design v2,
docs/qa/PLAN-015-design-v2.md §4.2, §4.3).

Read only and open to every role: ``accounts.registry`` lists every route here as
read-only, so any unsafe method is refused (403), and the views take GET and HEAD only
(405 otherwise). "Today" is the date in the project's ``TIME_ZONE``. The query strings
are read by :func:`ledger.services.grid.parse_query`,
:func:`ledger.services.receivables.parse_arrears_filter` and the dashboard's own parsers
(:func:`finance.templatetags.components.parse_period`,
:func:`ledger.services.dashboard_v2.parse_kind`), which never fail: an unusable value is
dropped. Each export applies the page's query. The dashboard's figures, the grid, the arrears
list and their exports run inside the reports' statement timeout
(:func:`ledger.views.reports.bounded`); a cancelled query answers the page with the sentence
:data:`ledger.views.reports.TOO_SLOW`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.views.decorators.http import require_safe

from accounts.roles import Role, meets, role_of
from accounts.views import forbidden
from finance.dates import month_long_text
from finance.templatetags.components import GLYPHS, PERIODS, badge_family, parse_period
from finance.us_states import US_STATES
from ledger.models import PaymentChannel, PaymentStatus
from ledger.services import dashboard as dashboard_blocks
from ledger.services import dashboard_v2, grid, list_filters, receivables, search
from ledger.services.csv_export import CsvWriter
from ledger.views.operators import csv_response, role_required
from ledger.views.reports import TOO_SLOW, ReportTooSlow, bounded

#: ``?chart=``: the Collections card's Monthly (the default) or Yearly bars.
CHART_MONTHLY = "monthly"
CHART_YEARLY = "yearly"


@dataclass(frozen=True, slots=True)
class DashboardState:
    """What the page's query string chose, each value already checked."""

    period: str
    chart: str
    query: str
    kind: str
    page: str

    def query_string(self, **changes: str) -> str:
        """The page's query string (without the mini calendar's month) with ``changes``;
        empty values left out."""
        values = {
            "period": self.period,
            "chart": self.chart,
            "q": self.query,
            "kind": self.kind,
            "page": self.page,
            **changes,
        }
        return urlencode({name: value for name, value in values.items() if value})


def _state(request: HttpRequest) -> DashboardState:
    period = parse_period(request.GET.get("period"))
    page = request.GET.get("page", "") or ""
    return DashboardState(
        period=period if period != PERIODS[0][0] else "",
        chart=CHART_YEARLY if request.GET.get("chart") == CHART_YEARLY else "",
        query=search.clean_query(request.GET.get("q")),
        kind=dashboard_v2.parse_kind(request.GET.get("kind")),
        page=page if page.isascii() and page.isdigit() else "",
    )


@dataclass(frozen=True, slots=True)
class Figures:
    kpis: dashboard_v2.Kpis
    collections: dashboard_v2.Collections
    tiles: list[dashboard_v2.OperatorTile]
    activity: dashboard_v2.Activity


def _figures(state: DashboardState, today: date) -> Figures:
    """Every figure of the page: the open balances, Needs attention, the P&L's rows and
    Payments by method each read once (Fable's gate on PLAN-015 Batch 1)."""
    yearly = state.chart == CHART_YEARLY
    balances = receivables.open_balances(today)
    needs = dashboard_v2.needs_attention(today, balances)
    read = dashboard_v2.page_reads(state.period, today, yearly)
    return Figures(
        kpis=dashboard_v2.kpis(
            state.period,
            today,
            balances=balances,
            attention_count=dashboard_v2.attention_total(needs),
            read=read,
        ),
        collections=(
            dashboard_v2.collections_by_year(today, read=read)
            if yearly
            else dashboard_v2.collections(today.year, today, read=read)
        ),
        tiles=dashboard_v2.operator_tiles(today, balances=balances),
        activity=dashboard_v2.activity(today, state.query, state.kind, state.page, found=needs),
    )


@role_required
@require_safe
def home(request: HttpRequest) -> HttpResponse:
    """``ledger:home`` (PLAN-015 Batch 1): the header (the period control, Download
    spreadsheet, New invoice and Record payment for billing and above), the four KPIs, the
    Collections card (``?chart=yearly``), the Operators tiles, the Recent activity feed
    (``?q=``, ``?kind=``, ``?page=``) beside the mini calendar (``?cal=``)."""
    today = timezone.localdate()
    state = _state(request)
    cal_month = dashboard_blocks.parse_cal(request.GET.get("cal", "") or "", today)
    figures: Figures | None
    try:
        figures = bounded(lambda: _figures(state, today))
    except ReportTooSlow:
        figures = None
    can_act = meets(role_of(request.user), Role.BILLING)
    return render(
        request,
        "ledger/dashboard/home.html",
        {
            "today": today,
            "month_label": month_long_text(today),
            "state": state,
            "period": state.period or PERIODS[0][0],
            "figures": figures,
            "too_slow": TOO_SLOW if figures is None else "",
            "can_act": can_act,
            "kinds": dashboard_v2.KINDS,
            "export_query": state.query_string(page=""),
            "pager_query": state.query_string(page=""),
            "state_query": state.query_string(),
            "chart_links": {
                CHART_MONTHLY: state.query_string(chart=""),
                CHART_YEARLY: state.query_string(chart=CHART_YEARLY),
            },
            "payments_query": urlencode(
                {
                    "date_from": figures.kpis.window.start.isoformat(),
                    "date_to": figures.kpis.window.end.isoformat(),
                    "status": PaymentStatus.SETTLED,
                }
            )
            if figures is not None
            else "",
            "mini_calendar": dashboard_blocks.mini_calendar(cal_month, today),
        },
    )


#: The spreadsheet's columns: the KPI rows (Section "Figure") then the activity rows.
EXPORT_HEADER = ("Section", "Kind", "Store", "Operator", "Date", "Amount (USD)", "Status", "Detail")
SECTION_FIGURE = "Figure"
SECTION_ACTIVITY = "Activity"


def export_records(
    kpis: dashboard_v2.Kpis, rows: list[dashboard_v2.ActivityRow]
) -> list[list[object]]:
    """The four KPIs (title, figure, sub-line and badge), then every activity row."""
    figures: list[list[object]] = [
        [SECTION_FIGURE, kpis.due.title, "", "", None, kpis.due.amount, "", kpis.due.sub],
        [
            SECTION_FIGURE,
            "Collected",
            "",
            "",
            None,
            kpis.collected.amount,
            kpis.collected.badge,
            kpis.collected.sub,
        ],
        [SECTION_FIGURE, "Overdue", "", "", None, kpis.overdue.amount, "", kpis.overdue.sub],
        [
            SECTION_FIGURE,
            "Bank deposits",
            "",
            "",
            None,
            kpis.bank.amount,
            kpis.bank.badge,
            kpis.bank.sub,
        ],
    ]
    return figures + [
        [SECTION_ACTIVITY, row.activity, row.store, row.operator, row.on, row.amount, row.chip, ""]
        for row in rows
    ]


@role_required
@require_safe
def home_export(request: HttpRequest) -> HttpResponseBase:
    """``ledger:dashboard_export`` (billing and above): the period's KPIs, then the activity
    rows of the page's search and kind, every page (:data:`EXPORT_HEADER`)."""
    if not meets(role_of(request.user), Role.BILLING):
        return forbidden(request)
    today = timezone.localdate()
    state = _state(request)

    def compute() -> list[list[object]]:
        balances = receivables.open_balances(today)
        needs = dashboard_v2.needs_attention(today, balances)
        found = dashboard_v2.kpis(
            state.period,
            today,
            balances=balances,
            attention_count=dashboard_v2.attention_total(needs),
        )
        rows = dashboard_v2.activity_rows(today, state.query, state.kind, found=needs)
        return export_records(found, rows)

    try:
        records = bounded(compute)
    except ReportTooSlow:
        return home(request)
    period = state.period or PERIODS[0][0]
    return csv_response(
        CsvWriter(EXPORT_HEADER).lines(records), f"dashboard-{period}-{today:%Y-%m-%d}.csv"
    )


#: Stands for a charge's id in its page's URL, reversed once per page (PLAN-011 D6).
_CHARGE_PK = "00000000-0000-0000-0000-000000000000"
_NOTE = format_html('<span class="note-marker" title="Has a note">{}</span>', grid.NOTE_MARKER)


def cell_html(cell: grid.Cell, charge_url: str) -> SafeString:
    """One grid cell, as the template wrote it, built here: 12,000 cells through the
    template engine (a ``{% url %}`` each) took longer than the page's 2 s (D6). A cell with a
    status holds the grid cell block (PLAN-012f D6): the amount over the status line (the
    family's icon, the words, the details and the note marker), a link to the cell's first
    charge when it has one; a blank cell holds nothing."""
    css = f"cell cell-{cell.status}" if cell.status else "cell"
    if not cell.status:
        inner = SafeString("")
    else:
        icon = (
            format_html('<span aria-hidden="true">{}</span> ', GLYPHS[family])
            if cell.status != grid.STATUS_NOT_DUE and (family := badge_family(cell.status))
            else ""
        )
        lines = format_html(
            "{}{}",
            format_html('<span class="cell-amount">{}</span>', cell.amount_text)
            if cell.amount_text
            else "",
            format_html(
                '<span class="cell-status">{}{}{}{}</span>',
                icon,
                cell.status_words,
                format_html(" · {}", cell.details) if cell.details else "",
                _NOTE if cell.has_note else "",
            ),
        )
        if cell.charge_id is not None:
            # PLAN-012e D2: a prorated month shows its arithmetic as the link's title.
            inner = format_html(
                '<a class="cell-block" href="{}" aria-label="{}"{}>{}</a>',
                charge_url.replace(_CHARGE_PK, str(cell.charge_id)),
                cell.label,
                format_html(' title="{}"', cell.proration_text) if cell.proration_text else "",
                lines,
            )
        else:
            inner = format_html('<span class="cell-block">{}</span>', lines)
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
    operators = receivables.arrears_operators()
    states = grid.state_choices(built, query)
    return render(
        request,
        "ledger/grid/page.html",
        {
            # PLAN-014 A5: the Filters button's count, the chips and Clear (keeping the words).
            "active_filters": list_filters.active(
                request,
                {
                    "operator": (
                        "Operator",
                        {str(operator.pk): operator.display_name for operator in operators},
                    ),
                    "state": ("State", dict(states)),
                    "channel": ("Channel", dict(PaymentChannel.choices)),
                    "status": ("Status", dict(grid.CELL_STATUSES)),
                },
                accepted=query.as_query(),
            ),
            "query": query,
            # PLAN-012d D12: the month selects offer the current month +/- 24.
            "picker_months": grid.picker_months(query),
            "grid": built,
            "rows": _grid_rows(built) if built is not None else [],
            "too_slow": TOO_SLOW if built is None else "",
            "export_query": urlencode(query.as_query()),
            "operators": operators,
            "states": states,
            "channels": PaymentChannel.choices,
            "statuses": grid.CELL_STATUSES,
            "note_marker": grid.NOTE_MARKER,
            # PLAN-012f D6: the current month's column is tinted.
            "this_month": timezone.localdate().replace(day=1),
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


def _state_choice(raw: str) -> dict[str, str]:
    """The arrears State filter's chip: a US state code (in any case, as the filter reads it)
    by its name; any other text filters to no row and is never echoed as a chip."""
    value = (raw or "").strip()
    name = dict(US_STATES).get(value.upper())
    return {value: name} if name else {}


#: The arrears Sort menu's options, in the order of the columns they sort.
ARREARS_SORTS: tuple[tuple[str, str], ...] = (
    (receivables.SORT_OPERATOR, "Operator"),
    (receivables.SORT_MONTHS, "Months behind"),
    (receivables.SORT_OWED, "Total owed"),
    (receivables.SORT_LAST_PAYMENT, "Last payment"),
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
    operators = receivables.arrears_operators()
    # PLAN-014 A5: the Filters button's count, the chips, Clear (keeping the search and the
    # sort) and the Sort menu (each option in its default direction).
    active_filters = list_filters.active(
        request,
        {
            "operator": (
                "Operator",
                {str(operator.pk): operator.display_name for operator in operators},
            ),
            "state": ("State", _state_choice(selected.state)),
            "bucket": ("Months behind", receivables.BUCKET_LABELS),
        },
        accepted=selected.as_query(),
        keep=("q", "sort", "dir"),
    )
    return render(
        request,
        "ledger/arrears/page.html",
        {
            "active_filters": active_filters,
            "sort_options": list_filters.sort_menu(
                request, active_filters.params, ARREARS_SORTS, selected.sort, drop=("dir",)
            ),
            "selected": selected,
            "report": report,
            "too_slow": TOO_SLOW if report is None else "",
            "export_query": urlencode(selected.as_query()),
            "sort_links": sort_links,
            "operators": operators,
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
