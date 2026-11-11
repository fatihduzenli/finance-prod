"""PLAN-015 Batch 4 (docs/qa/15/PLAN-015-batch-4-reports.md): the reports and the calendar in
the dashboard's language — the report family's and the list-like pages' headers (the title
block, the summary as its muted sub-line), the Profit and loss figures as KPI cards v2, the
P&L's monthly chart in the Collections chart's style, the Aging figure row and the Calendar's
side cards with the card head."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import RentChargeStatus
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db


def page(client: Client, path: str, params: dict[str, str] | None = None) -> str:
    response = client.get(path, params or {})
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def header_of(html: str) -> str:
    start = html.index('<div class="page-header">')
    return html[start : html.index("</header>", start)]


# --- §3.1 The headers -----------------------------------------------------------------------------

#: The report family: the breadcrumb, then the h1, in the title block.
REPORT_FAMILY = [
    ("/reports/pnl/", "Profit and loss"),
    ("/reports/aging/", "Aging"),
    ("/reports/revenue/", "Revenue by month"),
    ("/reports/payments-by-method/", "Payments by method"),
    ("/reports/deposits/", "Bank account check"),
    ("/calendar/", "Calendar"),
]


@pytest.mark.parametrize(("path", "title"), REPORT_FAMILY)
def test_the_report_family_has_the_title_block_with_the_breadcrumb(path: str, title: str) -> None:
    header = header_of(page(client_for(Role.ADMIN)[0], path))
    assert header.startswith(
        '<div class="page-header"><div class="page-title-block">'
        '<nav class="breadcrumb" aria-label="Breadcrumb" id="breadcrumb">'
        '<a href="/reports/">Reports</a> <span aria-hidden="true">›</span> '
        f'<span aria-current="page">{title}</span></nav><h1>{title}</h1>'
    ), header[:400]
    assert 'class="list-counts"' not in header and 'class="page-title"' not in header
    assert 'class="page-date"' not in header


def test_aging_writes_as_of_as_the_headers_sub_line() -> None:
    header = header_of(page(client_for(Role.ADMIN)[0], "/reports/aging/"))
    assert re.search(
        r'<h1>Aging</h1><p class="page-sub" id="aging-as-of">As of [A-Z][a-z]{2} \d{1,2}, \d{4}</p>'
        r"</div>",
        header,
    )


def test_the_reports_index_writes_its_lede_as_the_sub_line() -> None:
    header = header_of(page(client_for(Role.VIEWER)[0], "/reports/"))
    assert header.startswith(
        '<div class="page-header"><div class="page-title-block"><h1>Reports</h1>'
        '<p class="page-sub" id="reports-lede">Every report downloads as a spreadsheet.</p></div>'
    )


#: The list-like pages, their title and counts sentence with one operator behind by $400.
LIST_LIKE = [
    ("/arrears/", {}, "Unpaid balances", "1 operator behind · $400.00 owed", True),
    ("/returns/", {}, "Returned payments", "0 not resolved", True),
    ("/disputes/", {}, "Disputes", "0 disputes in the last 90 days", True),
    ("/search/", {"q": "behind"}, "Search", "1 store · 1 operator", False),
]


@pytest.mark.parametrize(("path", "params", "title", "counts", "export"), LIST_LIKE)
def test_the_list_like_pages_take_the_list_pages_header(
    path: str, params: dict[str, str], title: str, counts: str, export: bool
) -> None:
    lease = a_lease(a_store("Behind", 8401), an_operator("Behind Co"))
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "400.00", day=1)
    header = header_of(page(client_for(Role.ADMIN)[0], path, params))
    assert header.startswith(
        f'<div class="page-header"><div class="page-title-block"><h1>{title}</h1>'
        f'<p class="page-sub" id="list-counts">{counts}</p></div><div class="page-actions">'
    ), header[:300]
    assert 'class="list-counts"' not in header and 'class="page-title"' not in header
    actions = header[header.index('<div class="page-actions">') :]
    if export:
        button = re.search(r'<a class="btn" id="export-csv" href="[^"]+">(.*?)</a>', actions)
        assert button is not None
        assert button[1].startswith('<svg class="icon icon-file" width="16" height="16"')
        assert button[1].endswith("</svg>Download spreadsheet")
    else:
        assert 'id="export-csv"' not in actions
    if "btn-primary" in actions:  # Disputes: "Update from …" is the primary, last
        assert actions.count("btn-primary") == 1
        assert actions.index('id="export-csv"') < actions.index("btn-primary")


def test_search_without_a_query_has_no_sub_line() -> None:
    header = header_of(page(client_for(Role.ADMIN)[0], "/search/"))
    assert '<div class="page-title-block"><h1>Search</h1></div>' in header
    assert 'id="list-counts"' not in header
