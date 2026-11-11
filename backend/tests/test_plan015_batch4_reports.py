"""PLAN-015 Batch 4 (docs/qa/15/PLAN-015-batch-4-reports.md): the reports and the calendar in
the dashboard's language — the report family's and the list-like pages' headers (the title
block, the summary as its muted sub-line), the Profit and loss figures as KPI cards v2, the
P&L's monthly chart in the Collections chart's style, the Aging figure row and the Calendar's
side cards with the card head."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from accounts.roles import Role
from finance.templatetags.components import kpi2
from ledger.models import RentChargeStatus
from ledger.services import pnl
from ledger.views.reports import _kpis
from tests import test_pnl as pnl_world
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


# --- §3.2 The Profit and loss figures as KPI cards v2 ---------------------------------------------

JULY_ONLY = {"from": "2026-07", "to": "2026-07"}


def pnl_page(params: dict[str, str] | None = None) -> str:
    return page(client_for(Role.VIEWER)[0], pnl_world.PAGE, params or JULY_ONLY)


def card_of(html: str, value_id: str) -> str:
    """The {% kpi2 %} card whose figure carries ``value_id``."""
    at = html.index(f'id="{value_id}"')
    start = html.rindex('<div class="kpi2">', 0, at)
    end = html.index("</section>", at)
    following = html.find('<div class="kpi2">', at, end)
    return html[start : end if following == -1 else following]


def figures_of(html: str) -> str:
    start = html.index('<section class="kpi-row kpi2-row" id="pnl-kpis" aria-label="Figures">')
    return html[start : html.index("</section>", start)]


def test_the_pnl_figures_are_four_kpi2_cards_with_their_icons_and_ids() -> None:
    _, _, charge, _ = pnl_world.known_store()
    pnl_world.paid(charge, "2000.00", date(2026, 7, 5))
    html = pnl_page()
    figures = figures_of(html)
    assert figures.count('<div class="kpi2">') == 4
    assert re.findall(r'<svg class="icon icon-([a-z-]+)"', figures) == [
        "banknote",
        "check",
        "dash",
        "circle-half",
    ]
    assert re.findall(r'<p class="kpi2-title">([^<]+)</p>', figures) == [
        "Billed",
        "Collected",
        "Costs",
        "Margin (collected)",
    ]
    assert re.findall(r'<span class="kpi2-value[^"]*" id="([a-z-]+)">', figures) == [
        "kpi-billed",
        "kpi-collected",
        "kpi-costs",
        "kpi-margin",
    ]
    assert '<p class="kpi2-sub">rent $3,500.00 · fees $1,000.00</p>' in card_of(html, "kpi-billed")
    assert '<span class="kpi2-value tone-paid" id="kpi-collected">$2,000.00</span>' in figures
    assert '<p class="kpi2-sub">landlord $2,500.00 · fee costs $500.00</p>' in card_of(
        html, "kpi-costs"
    )
    assert '<span class="kpi2-value" id="kpi-costs">$3,000.00</span>' in figures
    # The old cards are gone; no card has a footer link while every cost is known.
    assert 'class="kpi"' not in figures and 'class="kpi-row pnl-kpis"' not in html
    assert "kpi2-link" not in figures and 'id="kpi-unknown"' not in html


def test_the_margin_tones_negative_unpaid_positive_and_zero_paid() -> None:
    _, _, charge, sign = pnl_world.known_store()
    assert '<span class="kpi2-value tone-unpaid" id="kpi-margin">-$3,000.00</span>' in pnl_page()
    pnl_world.paid(charge, "2000.00", date(2026, 7, 5))
    pnl_world.paid(sign, "1000.00", date(2026, 7, 12))
    assert '<span class="kpi2-value tone-paid" id="kpi-margin">$0.00</span>' in pnl_page()


def test_an_unknown_cost_is_muted_and_the_margin_card_links_why() -> None:
    pnl_world.known_store()
    unknown = pnl_world.a_lease(pnl_world.a_store("Store 2476"), pnl_world.an_operator("Halid"))
    pnl_world.rent(unknown, pnl_world.JULY)
    html = pnl_page({**JULY_ONLY, "group": "operator"})
    assert '<span class="kpi2-value tone-muted" id="kpi-costs">unknown</span>' in html
    assert "landlord unknown · other unknown · fee costs $500.00" in card_of(html, "kpi-costs")
    margin = card_of(html, "kpi-margin")
    assert '<span class="kpi2-value tone-muted" id="kpi-margin">unknown</span>' in margin
    assert '<p class="kpi2-sub">1 store unknown</p>' in margin
    link = re.search(
        r'<a class="kpi2-link" href="([^"]+)" id="kpi-unknown"><span>Why</span>', margin
    )
    assert link is not None
    # The page's own query, then the explanation's anchor: the browser scrolls, no reload.
    assert link[1] == "/reports/pnl/?from=2026-07&amp;to=2026-07&amp;group=operator#pnl-explained"
    assert 'id="pnl-explained"' in html


def test_a_withheld_deposit_is_the_collected_cards_sub_line() -> None:
    total = pnl.Figures(withheld_deposits=Decimal("250.00"))
    assert _kpis(total, [])["collected_sub"] == "incl. $250.00 withheld deposit"
    assert _kpis(pnl.Figures(), [])["collected_sub"] == ""
    assert _kpis(pnl.Figures(other_costs=Decimal("40.00")), [])["costs_sub"] == (
        "landlord $0.00 · other $40.00 · fee costs $0.00"
    )


def test_an_empty_range_shows_zero_figures_without_links() -> None:
    figures = figures_of(pnl_page())
    assert '<span class="kpi2-value" id="kpi-billed">$0.00</span>' in figures
    assert '<span class="kpi2-value tone-paid" id="kpi-margin">$0.00</span>' in figures
    assert "kpi2-link" not in figures


def test_the_kpi2_link_id_is_an_id_or_a_template_mistake() -> None:
    assert kpi2("check", "x", link="/a/", link_text="A", link_id="why-1")["link_id"] == "why-1"
    assert kpi2("check", "x")["link_id"] == ""
    for hostile in ('a" onclick="x', "A", "1a", "a b", "a_b"):
        with pytest.raises(ValueError):
            kpi2("check", "x", link="/a/", link_text="A", link_id=hostile)


# --- §3.4 The Aging figures -----------------------------------------------------------------------


def aging_figures(html: str) -> str:
    start = html.index('<section class="kpi-row kpi2-row" id="aging-kpis" aria-label="Figures">')
    return html[start : html.index("</section>", start)]


def test_aging_shows_the_unpaid_card_from_the_reports_totals() -> None:
    for number in (8411, 8412):
        lease = a_lease(a_store(f"Aging {number}", number), an_operator(f"Aging Co {number}"))
        a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "400.00", day=1)
    response = client_for(Role.VIEWER)[0].get("/reports/aging/")
    html = response.content.decode()
    figures = aging_figures(html)
    assert figures.count('<div class="kpi2">') == 1  # no oldest day in the report: no Oldest
    assert '<svg class="icon icon-alert"' in figures
    assert '<p class="kpi2-title">Unpaid</p><p class="kpi2-sub">2 stores</p>' in figures
    total = response.context["report"].totals.total
    assert f'<span class="kpi2-value tone-unpaid" id="aging-kpi-unpaid">${total:,.2f}</span>' in (
        figures
    )
    assert html.index('id="aging-kpis"') < html.index('<table id="aging"')
    assert html.index('<div id="results">') < html.index('id="aging-kpis"')


def test_aging_with_nothing_owed_shows_a_muted_zero_and_no_stores() -> None:
    figures = aging_figures(page(client_for(Role.VIEWER)[0], "/reports/aging/"))
    assert '<p class="kpi2-sub">0 stores</p>' in figures
    assert '<span class="kpi2-value tone-muted" id="aging-kpi-unpaid">$0.00</span>' in figures


def test_aging_counts_one_store_once() -> None:
    lease = a_lease(a_store("Aging One", 8421), an_operator("One Co"))
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "400.00", day=1)
    a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.UNPAID, "400.00", day=1, seq=1)
    figures = aging_figures(page(client_for(Role.VIEWER)[0], "/reports/aging/"))
    assert '<p class="kpi2-sub">1 store</p>' in figures
