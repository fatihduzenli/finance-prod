"""The reports menu, the profit and loss report, the Aging report, revenue by month,
payments by method, deposits versus transactions and the customer statement, each with
its CSV export (docs/qa/PLAN-010b D11, D12; PLAN-011 D4, D5, D10).

Read only and open to every role: ``accounts.registry`` lists every route here as
read-only, so any unsafe method is refused (403), and the views take GET and HEAD only
(405 otherwise). The P&L's query string is read by
:func:`ledger.services.pnl.parse_query`, the other reports' range by
:func:`ledger.services.reports.parse_range` (the same bounds); neither fails: an
unusable range is replaced and the page says so. Each export applies its page's query.
The From and To months are plain selects (``ledger/_month_select.html``, the months of
:func:`ledger.services.grid.picker_months`, as on the grid), never HTML month inputs,
which Safari does not render (PLAN-012d, the owner's rule).
The Aging report (PLAN-014 G1) has no range: it is the open balances by store as of
today, so that its grand total without a filter is the dashboard's Unpaid balances. Its
filters (``?state=&operator=&brand=&kind=&deposits=1``, read by
:func:`ledger.services.receivables.parse_aging_filter`) go through the list pages' Filters
component; a store's name opens its
Charges tab of open charges (``?tab=charges&status=open``), a bucket's amount the same tab
with ``&bucket=<key>``, which highlights the charges of that bucket.

**Statement timeout** (PLAN-010c D11): on PostgreSQL every query of the page and of the
export runs inside one transaction with ``SET LOCAL statement_timeout`` set to
``REPORT_STATEMENT_TIMEOUT_MS``. A query the server cancels answers 200 with the page
and the sentence :data:`TOO_SLOW`, and logs the error's class name only. The export
computes all its records inside that transaction before the response starts, then
streams them: a timeout is the page's sentence, never a broken download. Other engines
run the same code without the timeout.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_safe

from finance.us_states import US_STATES
from ledger.models import Brand, Operator
from ledger.services import (
    credits,
    detail_cards,
    grid,
    list_filters,
    pnl,
    receivables,
    reports,
    stores,
    workspace,
)
from ledger.views.operators import csv_response, role_required

logger = logging.getLogger(__name__)

TOO_SLOW = "The report took too long. Try a shorter range, or try again in a minute."
POSTGRESQL = "postgresql"
#: The class name of the error PostgreSQL raises for a cancelled statement.
QUERY_CANCELED = "QueryCanceled"


class ReportTooSlow(Exception):  # noqa: N818 - an outcome, like documents.DocumentHidden
    """A report query was cancelled by the statement timeout."""


def bounded[T](compute: Callable[[], T]) -> T:
    """``compute()`` inside one transaction whose statements PostgreSQL cancels after
    ``REPORT_STATEMENT_TIMEOUT_MS``; :class:`ReportTooSlow` when one was.

    Inside an outer transaction the timeout would outlive this block, so it is put back
    to the session's own value before the block ends.
    """
    timeout_ms = int(settings.REPORT_STATEMENT_TIMEOUT_MS)
    is_postgresql = connection.vendor == POSTGRESQL
    nested = connection.in_atomic_block
    try:
        with transaction.atomic():
            if is_postgresql:
                with connection.cursor() as cursor:
                    cursor.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            result = compute()
            if is_postgresql and nested:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout TO DEFAULT")
            return result
    except OperationalError as error:
        cause = error.__cause__
        if is_postgresql and type(cause).__name__ == QUERY_CANCELED:
            logger.warning(
                "P&L report cancelled by the statement timeout: %s", type(cause).__name__
            )
            raise ReportTooSlow from None
        raise


def _pnl_filters(request: HttpRequest, query: pnl.PnlQuery) -> list_filters.ActiveFilters:
    """PLAN-014 A5: the Filters button's count, the chips and Clear of the P&L (the grouping
    and the rows box; the months are its range, never a chip)."""
    return list_filters.active(
        request,
        {
            "group": ("Group by", dict(pnl.GROUP_CHOICES)),
            "detail": ("Rows", {"1": "Shown"}),
        },
        accepted=query.as_query(),
        defaults={"group": pnl.GROUP_STORE},
    )


def _too_slow(request: HttpRequest, query: pnl.PnlQuery) -> HttpResponse:
    return render(
        request,
        "ledger/reports/pnl.html",
        {
            "query": query,
            "active_filters": _pnl_filters(request, query),
            "too_slow": TOO_SLOW,
            "group_choices": pnl.GROUP_CHOICES,
            "group_label": dict(pnl.GROUP_CHOICES)[query.group],
            "export_query": urlencode(query.as_query()),
            "picker_months": grid.picker_months(query),
        },
    )


@role_required
@require_safe
def pnl_report(request: HttpRequest) -> HttpResponse:
    query = pnl.parse_query(request.GET)

    def compute() -> tuple[list[pnl.PnlRow], list[pnl.PnlGroup], Decimal, Decimal]:
        rows = pnl.rows(query)
        return (
            rows,
            pnl.grouped(rows, query.group),
            pnl.deposits_held(query),
            credits.held(query.end),
        )

    try:
        rows, groups, held, credits_held = bounded(compute)
    except ReportTooSlow:
        return _too_slow(request, query)
    total = pnl.total(rows)
    return render(
        request,
        "ledger/reports/pnl.html",
        {
            "query": query,
            "active_filters": _pnl_filters(request, query),
            "groups": groups,
            "group_lines": _group_lines(groups, query.group),
            "total": total,
            "kpis": _kpis(total, rows),
            "row_count": len(rows),
            "cost_only_count": sum(1 for row in rows if row.cost_only),
            "deposits_held": held,
            "credits_held": credits_held,
            "group_choices": pnl.GROUP_CHOICES,
            "group_label": dict(pnl.GROUP_CHOICES)[query.group],
            "export_query": urlencode(query.as_query()),
            "picker_months": grid.picker_months(query),
        },
    )


def _kpis(total: pnl.Figures, rows: list[pnl.PnlRow]) -> dict[str, Any]:
    """The four cards over the table (DESIGN-SPEC §4 Profit and loss), from the total: Billed
    and Collected are its rent and fees summed (Costs and Margin are the total's own, unknown
    when any input is); ``unknown_stores`` counts the stores with an unknown input, for the
    Margin card's "why" line."""
    return {
        "billed": total.rent_billed + total.fees_billed,
        "collected": total.rent_collected + total.fees_collected,
        "unknown_stores": len({row.store_id for row in rows if row.unknown}),
    }


def _group_lines(groups: list[pnl.PnlGroup], group: str) -> list[tuple[pnl.PnlGroup, str, str]]:
    """Each group with its first cell's title and second line: by store, the store's title
    ("#1991 Istanbul", PLAN-012f-b E1-1) and the operators of its rows that are not
    cost-only, each once in order ("—" when there are none: a vacant store); by operator or
    month, the group's label and no second line."""
    if group != pnl.GROUP_STORE:
        return [(item, item.label, "") for item in groups]
    return [
        (
            item,
            stores.title_of(item.rows[0].store_number, item.label),
            ", ".join(dict.fromkeys(row.operator for row in item.rows if not row.cost_only)) or "—",
        )
        for item in groups
    ]


@role_required
@require_safe
def pnl_export(request: HttpRequest) -> HttpResponseBase:
    query = pnl.parse_query(request.GET)
    try:
        records = bounded(lambda: list(pnl.csv_records(query)))
    except ReportTooSlow:
        return _too_slow(request, query)
    return csv_response(
        pnl.csv_lines_of(records),
        f"pnl-{query.from_month:%Y-%m}-to-{query.to_month:%Y-%m}-{_stamp()}.csv",
    )


def _stamp() -> str:
    return f"{timezone.localdate():%Y-%m-%d}"


def _span(selected: reports.ReportRange) -> str:
    return f"{selected.from_month:%Y-%m}-to-{selected.to_month:%Y-%m}"


# --- The menu (D4) ----------------------------------------------------------------------


@role_required
@require_safe
def report_index(request: HttpRequest) -> HttpResponse:
    """``ledger:reports:index``: every report, the P&L included."""
    return render(request, "ledger/reports/index.html")


# --- Aging (PLAN-014 G1) ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgingLine:
    """A row of the Aging table: its title ("#1991 Istanbul", or "No store"), the store's
    Charges tab of open charges, and each bucket's amount with its drill-down link."""

    row: receivables.AgingStoreRow
    title: str
    url: str
    cells: list[tuple[str, Decimal, str]]


def _aging_lines(report: receivables.AgingReport) -> list[AgingLine]:
    lines = []
    for row in report.rows:
        if row.store_id is None:
            lines.append(
                AgingLine(
                    row,
                    row.store_label,
                    "",
                    [(key, row.buckets[key], "") for key, _ in receivables.AGING_BUCKETS],
                )
            )
            continue
        page = reverse("ledger:stores:detail", kwargs={"pk": row.store_id})
        query = {"tab": workspace.TAB_CHARGES, "status": workspace.FILTER_OPEN}
        lines.append(
            AgingLine(
                row,
                stores.title_of(row.store_number, row.store_label),
                f"{page}?{urlencode(query)}#tab-charges",
                [
                    (
                        key,
                        row.buckets[key],
                        f"{page}?{urlencode({**query, 'bucket': key})}#tab-charges",
                    )
                    for key, _ in receivables.AGING_BUCKETS
                ],
            )
        )
    return lines


def _aging_page(
    request: HttpRequest,
    selected: receivables.AgingFilter,
    report: receivables.AgingReport | None,
) -> HttpResponse:
    operators = receivables.arrears_operators()
    brands = list(Brand.objects.order_by("sort_order", "name"))
    brand_choices = {
        receivables.AGING_NO_BRAND: "No brand",
        **{str(brand.pk): brand.name for brand in brands},
    }
    # PLAN-014 A5: the Filters button's count, the chips (accepted values only) and Clear.
    active_filters = list_filters.active(
        request,
        {
            "state": ("State", dict(US_STATES)),
            "operator": (
                "Operator",
                {str(operator.pk): operator.display_name for operator in operators},
            ),
            "brand": ("Brand", brand_choices),
            "kind": ("Charges", dict(receivables.AGING_KINDS)),
            "deposits": ("Deposits", {receivables.AGING_DEPOSITS_ON: "Included"}),
        },
        accepted=selected.as_query(),
        keep=(),
    )
    return render(
        request,
        "ledger/reports/aging.html",
        {
            "selected": selected,
            "active_filters": active_filters,
            "report": report,
            "lines": _aging_lines(report) if report is not None else [],
            "too_slow": TOO_SLOW if report is None else "",
            "buckets": receivables.AGING_BUCKETS,
            "today": timezone.localdate(),
            "export_query": urlencode(selected.as_query()),
            "operators": operators,
            "brands": brands,
            "states": US_STATES,
            "kinds": receivables.AGING_KINDS,
        },
    )


@role_required
@require_safe
def aging_report(request: HttpRequest) -> HttpResponse:
    """``ledger:reports:aging``: the open balances by store as of today, filtered."""
    selected = receivables.parse_aging_filter(request.GET)
    today = timezone.localdate()
    try:
        report = bounded(lambda: receivables.aging(today, selected))
    except ReportTooSlow:
        report = None
    return _aging_page(request, selected, report)


@role_required
@require_safe
def aging_export(request: HttpRequest) -> HttpResponseBase:
    """``ledger:reports:aging_export``: the page's rows and totals, with its filters."""
    selected = receivables.parse_aging_filter(request.GET)
    today = timezone.localdate()
    try:
        report = bounded(lambda: receivables.aging(today, selected))
    except ReportTooSlow:
        return _aging_page(request, selected, None)
    return csv_response(receivables.aging_csv_lines(report), f"aging-{today:%Y-%m-%d}.csv")


# --- Ranged reports: revenue, payments by method, deposits ------------------------------


def _ranged_page(
    request: HttpRequest,
    template: str,
    selected: reports.ReportRange,
    report: object | None,
    **extra: Any,
) -> HttpResponse:
    return render(
        request,
        template,
        {
            "selected": selected,
            "report": report,
            "too_slow": TOO_SLOW if report is None else "",
            "export_query": urlencode(selected.as_query()),
            "picker_months": grid.picker_months(selected),
            **extra,
        },
    )


def _ranged[T](
    request: HttpRequest,
    template: str,
    compute: Callable[[reports.ReportRange], T],
    **extra: Any,
) -> HttpResponse:
    selected = reports.parse_range(request.GET)
    try:
        report: T | None = bounded(lambda: compute(selected))
    except ReportTooSlow:
        report = None
    return _ranged_page(request, template, selected, report, **extra)


def _ranged_export[T](
    request: HttpRequest,
    template: str,
    compute: Callable[[reports.ReportRange], T],
    lines: Callable[[T], Iterator[str]],
    name: str,
    **extra: Any,
) -> HttpResponseBase:
    selected = reports.parse_range(request.GET)
    try:
        report = bounded(lambda: compute(selected))
    except ReportTooSlow:
        return _ranged_page(request, template, selected, None, **extra)
    return csv_response(lines(report), f"{name}-{_span(selected)}-{_stamp()}.csv")


REVENUE = "ledger/reports/revenue.html"
BY_METHOD = "ledger/reports/payments_by_method.html"
DEPOSITS = "ledger/reports/deposits.html"


@role_required
@require_safe
def revenue_report(request: HttpRequest) -> HttpResponse:
    return _ranged(request, REVENUE, reports.revenue_by_month)


@role_required
@require_safe
def revenue_export(request: HttpRequest) -> HttpResponseBase:
    return _ranged_export(
        request, REVENUE, reports.revenue_by_month, reports.revenue_csv_lines, "revenue"
    )


@role_required
@require_safe
def payments_by_method_report(request: HttpRequest) -> HttpResponse:
    return _ranged(request, BY_METHOD, reports.payments_by_method, cards_note=reports.CARDS_NOTE)


@role_required
@require_safe
def payments_by_method_export(request: HttpRequest) -> HttpResponseBase:
    return _ranged_export(
        request,
        BY_METHOD,
        reports.payments_by_method,
        reports.payments_by_method_csv_lines,
        "payments-by-method",
        cards_note=reports.CARDS_NOTE,
    )


@role_required
@require_safe
def deposits_report(request: HttpRequest) -> HttpResponse:
    return _ranged(request, DEPOSITS, reports.deposits_vs_transactions)


@role_required
@require_safe
def deposits_export(request: HttpRequest) -> HttpResponseBase:
    return _ranged_export(
        request,
        DEPOSITS,
        reports.deposits_vs_transactions,
        reports.deposits_csv_lines,
        "deposits",
    )


# --- The customer statement -------------------------------------------------------------

STATEMENT = "ledger/reports/statement.html"


def _operator(pk: str) -> Operator:
    """The operator named in the URL, archived or not; 404 otherwise."""
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Operator, pk=key)


def _statement(operator: Operator) -> Callable[[reports.ReportRange], reports.Statement]:
    today = timezone.localdate()
    return lambda selected: reports.statement(operator, selected, today)


@role_required
@require_safe
def statement_page(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:statement``: printable, every store of the operator."""
    operator = _operator(pk)
    return _ranged(
        request,
        STATEMENT,
        _statement(operator),
        operator=operator,
        # PLAN-014 C6: the company each store's lease bills.
        bill_to=detail_cards.lease_accounts(operator),
    )


@role_required
@require_safe
def statement_export(request: HttpRequest, pk: str) -> HttpResponseBase:
    operator = _operator(pk)
    return _ranged_export(
        request,
        STATEMENT,
        _statement(operator),
        reports.statement_csv_lines,
        f"statement-{operator.pk}",
        operator=operator,
    )
