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

from dataclasses import dataclass, replace
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString
from django.views.decorators.http import require_safe

from accounts.roles import Role, meets, role_of
from finance.dates import month_text
from finance.templatetags.components import GLYPHS, badge_family
from ledger.models import PaymentChannel, PaymentMethod, PaymentStatus, Store
from ledger.services import attention, grid, one_off_charges, receivables, rent_run, stores
from ledger.services import dashboard as dashboard_blocks
from ledger.views.operators import csv_response, role_required
from ledger.views.reports import TOO_SLOW, ReportTooSlow, bounded
from ledger.wording import format_amount


@role_required
@require_safe
def home(request: HttpRequest) -> HttpResponse:
    """``ledger:home`` (D1; the approved design's layout, PLAN-012f D7, and the staging
    review's, PLAN-012f-b D3 to D6): the four figures, the Cashflow card, what needs
    attention (PLAN-011b D6) beside the Payment status card, the recent payments beside the
    mini calendar, the month's collection and what is coming up, then today's run, the
    failed attempts, the waived charges, the invoices not sent and the last nightly check.
    ``?range=`` (with ``from`` and ``to``) is the Cashflow and Payment status cards' shared
    range and ``?cal=`` the mini calendar's month; an unusable range computes neither card
    and says so. The Cashflow card runs inside the reports' statement timeout."""
    today = timezone.localdate()
    selected = dashboard_blocks.parse_range(request.GET, today)
    range_from, range_to = dashboard_blocks.form_months(request.GET, today)
    cal_month = dashboard_blocks.parse_cal(request.GET.get("cal", "") or "", today)
    cashflow: dashboard_blocks.Cashflow | None = None
    cashflow_message = ""
    if selected is not None:
        try:
            cashflow = bounded(lambda: dashboard_blocks.cashflow(selected))
        except ReportTooSlow:
            cashflow_message = TOO_SLOW
    range_query = urlencode(selected.as_query()) if selected is not None else ""
    role = role_of(request.user)
    data = receivables.dashboard(today)
    leases = attention.dashboard_leases()
    needs = _titled(attention.attention(today, leases=leases))
    debits = dashboard_blocks.pending_debits()
    collection = dashboard_blocks.collection_progress(today)
    run_line = rent_run.run_line(today)
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
            "can_record_outcome": meets(role, Role.BILLING),
            # PLAN-012f D7: a row's action button and "+ New invoice" are billing's; a viewer
            # reads the rows and follows their links.
            "can_act": meets(role, Role.BILLING),
            "payments_query": payments_query,
            "kpis": _kpis(data, collection, debits, run_line, payments_query),
            "collection": collection,
            "coming_up": dashboard_blocks.coming_up(leases, today, debits),
            "recent_payments": dashboard_blocks.recent_payments(today),
            # PLAN-012d D11: today's rent run in one line, from its audit rows.
            "run_line": run_line,
            "waived_live_heading": one_off_charges.WAIVED_LIVE_HEADING,
            "not_sent": [
                (invoice, receivables.invoice_store_label(invoice)) for invoice in data.not_sent
            ],
            "failed_attempts": [
                (invoice, receivables.invoice_store_label(invoice))
                for invoice in data.failed_attempts
            ],
            # PLAN-012f-b D3 to D6: the shared range, its two cards and the mini calendar.
            "range": selected,
            "range_choices": dashboard_blocks.RANGE_CHOICES,
            "range_key": selected.key if selected is not None else dashboard_blocks.RANGE_CUSTOM,
            "range_from": selected.from_month if selected is not None else range_from,
            "range_to": selected.to_month if selected is not None else range_to,
            "range_months": dashboard_blocks.picker_months(selected, range_from, range_to, today),
            "range_message": dashboard_blocks.RANGE_MESSAGE if selected is None else "",
            "range_query": range_query,
            "cashflow": cashflow,
            "cashflow_message": cashflow_message,
            "payment_status": (
                dashboard_blocks.payment_status(selected, today) if selected is not None else None
            ),
            "mini_calendar": dashboard_blocks.mini_calendar(cal_month, today),
        },
    )


def _titled(found: attention.Attention) -> attention.Attention:
    """The items with their store as the store page and the grid title it ("#1991 Istanbul",
    :func:`ledger.services.stores.title`; one query for the stores the items name)."""
    ids = {item.store_id for item in found.items if item.store_id is not None}
    titles = {store.pk: stores.title(store) for store in Store.objects.filter(pk__in=ids)}
    found.items = [
        replace(item, store=titles[item.store_id]) if item.store_id in titles else item
        for item in found.items
    ]
    return found


@dataclass(frozen=True, slots=True)
class Kpi:
    """One of the dashboard's four figures (PLAN-012f D7): ``url`` makes the card a link;
    ``note`` is escaped text, or markup built here with its parts escaped."""

    element_id: str
    label: str
    value: str
    note: str
    tone: str = ""
    url: str = ""


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _kpis(
    data: receivables.Dashboard,
    collection: dashboard_blocks.Collection,
    debits: dashboard_blocks.PendingDebits,
    run_line: rent_run.RunLine | None,
    payments_query: str,
) -> list[Kpi]:
    """Unpaid balances, Collected this month, Pending bank debits, Today's daily charges."""
    share = (
        f"{collection.percent}% of {month_text(collection.month)} rent"
        if collection.percent is not None
        else "—"
    )
    run_page = reverse("ledger:run:today")
    details = format_html('<a href="{}">details</a>', run_page)
    run_note = (
        format_html("created · {} skipped · {}", run_line.skipped, details)
        if run_line is not None
        else format_html("not created yet · {}", details)
    )
    pending_query = urlencode({"status": PaymentStatus.PENDING, "method": PaymentMethod.ACH})
    return [
        Kpi(
            "figure-outstanding",
            "Unpaid balances",
            format_amount(data.outstanding),
            f"{_plural(data.outstanding_stores, 'store')} · {data.overdue_stores} overdue",
            tone="unpaid",
            url=reverse("ledger:arrears:page"),
        ),
        Kpi(
            "figure-paid",
            "Collected this month",
            format_amount(data.paid_this_month),
            share,
            url=f"{reverse('ledger:payments:list')}?{payments_query}",
        ),
        Kpi(
            "figure-pending",
            "Pending bank debits",
            format_amount(debits.amount),
            debits.note,
            url=f"{reverse('ledger:payments:list')}?{pending_query}",
        ),
        Kpi(
            "figure-run",
            "Today's daily charges",
            str(run_line.created) if run_line is not None else "—",
            run_note,
        ),
    ]


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
