"""The profit and loss report and its CSV export (docs/qa/PLAN-010b D11, D12).

Read only and open to every role: ``accounts.registry`` lists both routes as read-only,
so any unsafe method is refused (403), and the views take GET and HEAD only (405
otherwise). The query string is read by :func:`ledger.services.pnl.parse_query`, which
never fails: an unusable range is replaced and the page says so. The export applies
the same query.

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
from collections.abc import Callable
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_safe

from ledger.services import pnl
from ledger.views.operators import role_required

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


def _too_slow(request: HttpRequest, query: pnl.PnlQuery) -> HttpResponse:
    return render(
        request,
        "ledger/reports/pnl.html",
        {
            "query": query,
            "too_slow": TOO_SLOW,
            "group_choices": pnl.GROUP_CHOICES,
            "group_label": dict(pnl.GROUP_CHOICES)[query.group],
            "export_query": urlencode(query.as_query()),
            "toggle_query": _toggle(query),
        },
    )


@role_required
@require_safe
def pnl_report(request: HttpRequest) -> HttpResponse:
    query = pnl.parse_query(request.GET)

    def compute() -> tuple[list[pnl.PnlRow], list[pnl.PnlGroup], Decimal]:
        rows = pnl.rows(query)
        return rows, pnl.grouped(rows, query.group), pnl.deposits_held(query)

    try:
        rows, groups, held = bounded(compute)
    except ReportTooSlow:
        return _too_slow(request, query)
    return render(
        request,
        "ledger/reports/pnl.html",
        {
            "query": query,
            "groups": groups,
            "total": pnl.total(rows),
            "row_count": len(rows),
            "cost_only_count": sum(1 for row in rows if row.cost_only),
            "deposits_held": held,
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
def pnl_export(request: HttpRequest) -> HttpResponseBase:
    query = pnl.parse_query(request.GET)
    try:
        records = bounded(lambda: list(pnl.csv_records(query)))
    except ReportTooSlow:
        return _too_slow(request, query)
    response = StreamingHttpResponse(
        pnl.csv_lines_of(records), content_type="text/csv; charset=utf-8"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="pnl-{query.from_month:%Y-%m}-to-{query.to_month:%Y-%m}'
        f'-{timezone.localdate():%Y-%m-%d}.csv"'
    )
    response["Cache-Control"] = "no-store"
    return response
