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


# --- §3.3 The P&L chart ---------------------------------------------------------------------------

THREE_MONTHS = {"from": "2026-07", "to": "2026-09"}


def three_months() -> None:
    """One store with its landlord rent (2,500) and rent of 3,500 a month: July paid in full,
    August 2,000, September nothing."""
    store = pnl_world.a_store()
    lease = pnl_world.a_lease(store)
    pnl_world.a_cost(store)
    july = pnl_world.rent(lease, pnl_world.JULY)
    august = pnl_world.rent(lease, pnl_world.AUGUST)
    pnl_world.rent(lease, pnl_world.SEPTEMBER)
    pnl_world.paid(july, "3500.00", date(2026, 7, 5))
    pnl_world.paid(august, "2000.00", date(2026, 8, 5))


def chart_card(html: str) -> str:
    start = html.index('<section class="card pnl-chart-card" id="pnl-chart-card"')
    return html[start : html.index("</section>", start)]


def months_of(card: str) -> list[str]:
    return re.findall(r'<g class="collection-bar pnl-month"[^>]*>.*?</g>', card, re.S)


def test_a_three_month_range_draws_three_months_of_two_bars() -> None:
    three_months()
    html = pnl_page(THREE_MONTHS)
    card = chart_card(html)
    assert (
        html.index('id="pnl-kpis"')
        < html.index('id="pnl-chart-card"')
        < html.index('id="pnl-card"')
    )
    assert (
        '<div class="card-head"><h2 class="section-label" id="pnl-chart-title">By month</h2></div>'
    ) in card
    assert 'aria-labelledby="pnl-chart-title"' in html
    assert (
        '<div class="collections-chart pnl-chart" id="pnl-chart" '
        'data-tooltip-fields="collected:Collected,costs:Costs,margin:Margin">'
    ) in card
    assert '<div class="chart-tooltip" id="pnl-tooltip" hidden></div>' in card
    months = months_of(card)
    assert len(months) == 3
    assert [month.count("<rect ") for month in months] == [2, 2, 2]
    assert (
        'data-label="Jul 2026" data-collected="$3,500.00" data-costs="$2,500.00" '
        'data-margin="$1,000.00"'
    ) in months[0]
    assert 'data-collected="$2,000.00" data-costs="$2,500.00" data-margin="-$500.00"' in months[1]
    assert 'data-collected="$0.00" data-costs="$2,500.00" data-margin="-$2,500.00"' in months[2]
    assert (
        "<title>Aug 2026 · Collected $2,000.00 · Costs $2,500.00 · Margin -$500.00</title>"
    ) in months[1]
    assert [re.findall(r'class="bar-label"[^>]*>([^<]+)<', month) for month in months] == [
        ["Jul"],
        ["Aug"],
        ["Sep"],
    ]
    for month in months:
        collected, costs = re.findall(r'fill="(var\(--[a-z-]+\))"', month)[:2]
        assert (collected, costs) == ("var(--chart-income)", "var(--chart-expenses)")
    # September collected nothing: a bar of height 0 on the zero line, the axis still drawn.
    assert (
        '<rect x="' in months[2]
        and 'height="0.0" rx="0.0" fill="var(--chart-income)"' in (months[2])
    )
    assert card.count('class="axis-label"') == 5
    svg = re.search(
        r'<svg class="collections-svg" viewBox="0 0 720.0 232.0" role="img" '
        r'aria-label="([^"]+)">',
        card,
    )
    assert svg is not None
    assert svg[1] == "Collected and costs by month from Jul 2026 to Sep 2026."
    legend = card[card.index('<ul class="collections-legend" id="pnl-chart-legend"') :]
    assert re.findall(r"</svg>([A-Za-z]+)</li>", legend) == ["Collected", "Costs"]
    assert 'fill="var(--chart-expenses)"' in legend
    assert "ledger/charts.js" in html


def test_the_largest_figure_sets_the_scale_and_the_ticks_read_in_thousands() -> None:
    three_months()
    card = chart_card(pnl_page(THREE_MONTHS))
    ticks = re.findall(r'class="axis-label"[^>]*>([^<]+)<', card)
    assert ticks == ["$0k", "$1k", "$2k", "$3k", "$4k"]  # 3,500 in four steps of 1,000
    july = months_of(card)[0]
    # 3,500 of 4,000 on a plot of 190: 166.25 high, from y = 200 - 166.25.
    assert 'y="33.8" width="' in july and 'height="166.3"' in july


def test_costs_above_collected_and_twelve_months_fit_without_overlapping_labels() -> None:
    store = pnl_world.a_store()
    lease = pnl_world.a_lease(store, started_on=date(2025, 1, 1))
    pnl_world.a_cost(store, starts=date(2025, 1, 1))
    for month in range(1, 13):
        pnl_world.rent(lease, date(2025, month, 1), "1000.00")
    card = chart_card(pnl_page({"from": "2025-01", "to": "2025-12"}))
    months = months_of(card)
    assert len(months) == 12
    assert all('data-margin="-$2,500.00"' in month for month in months)  # nothing collected
    centres = [float(x) for x in re.findall(r'class="bar-label" x="([0-9.]+)"', card)]
    assert len(centres) == 12 and centres == sorted(centres)
    assert min(b - a for a, b in zip(centres, centres[1:], strict=False)) >= 28
    assert max(centres) < 720


def test_a_long_range_labels_every_other_month() -> None:
    store = pnl_world.a_store()
    lease = pnl_world.a_lease(store, started_on=date(2024, 1, 1))
    pnl_world.a_cost(store, starts=date(2024, 1, 1))
    pnl_world.rent(lease, date(2024, 1, 1), "1000.00")
    card = chart_card(pnl_page({"from": "2024-01", "to": "2026-12"}))
    assert len(months_of(card)) == 36
    labels = re.findall(r'class="bar-label" x="([0-9.]+)"[^>]*>([^<]+)<', card)
    assert len(labels) == 18 and labels[0][1] == "Jan" and labels[1][1] == "Mar"
    assert card.count("<title>") == 36  # every month keeps its title


def test_an_unknown_cost_month_draws_no_cost_bar() -> None:
    three_months()
    # A store without a landlord rent, leased in August only: August's costs are unknown.
    unknown = pnl_world.a_lease(
        pnl_world.a_store("Store 2476"),
        pnl_world.an_operator("Halid"),
        started_on=pnl_world.AUGUST,
        ended_on=date(2026, 8, 31),
    )
    pnl_world.rent(unknown, pnl_world.AUGUST)
    months = months_of(chart_card(pnl_page(THREE_MONTHS)))
    assert months[1].count("<rect ") == 1
    assert 'data-costs="unknown" data-margin="unknown"' in months[1]
    assert "· Costs unknown · Margin unknown</title>" in months[1]
    assert months[0].count("<rect ") == months[2].count("<rect ") == 2


def test_one_month_or_nothing_billed_or_collected_shows_no_chart() -> None:
    assert 'id="pnl-chart-card"' not in pnl_page(THREE_MONTHS)  # nothing at all
    three_months()
    assert 'id="pnl-chart-card"' not in pnl_page(JULY_ONLY)
    assert 'id="pnl-chart-card"' not in pnl_page({"from": "2025-01", "to": "2025-03"})
    assert 'id="pnl-chart-card"' in pnl_page(THREE_MONTHS)


def test_the_chart_stays_with_the_rows_shown_and_any_grouping() -> None:
    three_months()
    for params in ({"detail": "1"}, {"group": "operator"}, {"group": "month"}):
        assert len(months_of(chart_card(pnl_page({**THREE_MONTHS, **params})))) == 3, params


def test_the_chart_rows_are_grouped_from_the_rows_already_read() -> None:
    three_months()
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client = client_for(Role.VIEWER)[0]
    client.get(pnl_world.PAGE, JULY_ONLY)  # the session's first reads
    counts = []
    for params in (JULY_ONLY, THREE_MONTHS):
        with CaptureQueriesContext(connection) as captured:
            response = client.get(pnl_world.PAGE, params)
        assert response.status_code == 200
        counts.append(len(captured))
    assert counts[0] == counts[1], counts
    months = response.context["chart_months"]
    assert [(m.label, m.title, m.collected, m.costs, m.margin) for m in months] == [
        ("Jul", "Jul 2026", Decimal("3500.00"), Decimal("2500.00"), Decimal("1000.00")),
        ("Aug", "Aug 2026", Decimal("2000.00"), Decimal("2500.00"), Decimal("-500.00")),
        ("Sep", "Sep 2026", Decimal("0.00"), Decimal("2500.00"), Decimal("-2500.00")),
    ]


def test_a_month_without_rows_is_zero_in_the_range_order() -> None:
    store = pnl_world.a_store()
    lease = pnl_world.a_lease(store, started_on=pnl_world.JULY, ended_on=date(2026, 7, 31))
    pnl_world.paid(pnl_world.rent(lease, pnl_world.JULY), "100.00", date(2026, 7, 5))
    response = client_for(Role.VIEWER)[0].get(pnl_world.PAGE, {"from": "2026-06", "to": "2026-08"})
    months = response.context["chart_months"]
    assert [m.title for m in months] == ["Jun 2026", "Jul 2026", "Aug 2026"]
    assert months[0].collected == months[2].collected == 0
    assert months[1].collected == Decimal("100.00")


def test_the_too_slow_page_has_no_chart(monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.views import reports

    three_months()

    def cancelled(compute: object) -> object:
        raise reports.ReportTooSlow

    monkeypatch.setattr(reports, "bounded", cancelled)
    html = pnl_page(THREE_MONTHS)
    assert 'id="pnl-too-slow"' in html and 'id="pnl-chart-card"' not in html


def test_a_month_label_never_carries_user_text() -> None:
    store = pnl_world.a_store("<script>alert(1)</script>")
    lease = pnl_world.a_lease(store, pnl_world.an_operator("<b>Op</b>"))
    pnl_world.a_cost(store)
    pnl_world.paid(pnl_world.rent(lease, pnl_world.JULY), "10.00", date(2026, 7, 5))
    card = chart_card(pnl_page({"from": "2026-07", "to": "2026-08"}))
    assert "script" not in card and "Op" not in card
    assert set(re.findall(r'data-label="([^"]+)"', card)) == {"Jul 2026", "Aug 2026"}


def test_the_pnl_tag_escapes_and_draws_nothing_for_no_months() -> None:
    from types import SimpleNamespace

    from finance.templatetags.charts import pnl_bars

    assert pnl_bars([]) == ""
    hostile = SimpleNamespace(
        label='<b>"x', title="<i>", collected=Decimal("5"), costs=None, margin=None
    )
    drawn = str(pnl_bars([hostile]))
    assert "<b>" not in drawn and "<i>" not in drawn and "&lt;b&gt;&quot;x" in drawn


def test_the_costs_key_is_the_quiet_expenses_colour() -> None:
    from django.template import Context, Template

    key = Template('{% load charts %}{% chart_key "costs" %}').render(Context())
    assert 'fill="var(--chart-expenses)"' in key


def test_the_dashboard_chart_names_its_tooltip_fields() -> None:
    html = page(client_for(Role.VIEWER)[0], "/")
    assert (
        '<div class="collections-chart" id="collections-chart" '
        'data-tooltip-fields="collected:Collected,due:Due">'
    ) in html
    assert html.index("ledger/charts.js") < html.index("ledger/dashboard.js")


# --- §3.5 The Calendar's side cards ---------------------------------------------------------------


def side_card(html: str, card_id: str) -> str:
    start = html.index(f'id="{card_id}"')
    return html[start : html.index("</section>", start)]


def test_the_calendar_side_cards_open_with_the_card_head() -> None:
    html = page(client_for(Role.BILLING)[0], "/calendar/")
    day = side_card(html, "calendar-day-panel")
    assert re.search(
        r'aria-labelledby="calendar-day-title">\s*<div class="card-head day-panel-head">'
        r'<h2 class="section-label" id="calendar-day-title">[^<]+</h2>'
        r'<a class="btn btn-sm" id="panel-add-entry" href="/calendar/new/\?on=[0-9-]+" '
        r'data-dialog-open="calendar-entry"',
        day,
    )
    nxt = side_card(html, "calendar-next-days")
    assert re.search(
        r'aria-labelledby="calendar-next-title">\s*<div class="card-head">'
        r'<h2 class="section-label" id="calendar-next-title">Next 7 days</h2></div>',
        nxt,
    )
    feed = side_card(html, "calendar-feed-card")
    assert re.search(
        r'aria-labelledby="calendar-feed-title">\s*<div class="card-head">'
        r'<h2 class="section-label" id="calendar-feed-title">Feed</h2>'
        r'<form method="post" action="/settings/calendar-feed/new/">'
        r'<input type="hidden" name="csrfmiddlewaretoken" value="[^"]+">'
        r'<button class="link-action card-link" type="submit" id="calendar-feed-create">'
        r"Get the link</button></form></div>",
        feed,
    )
    actions = feed[feed.index('<div class="feed-actions">') :]
    assert actions.startswith('<div class="feed-actions"><a id="calendar-feed-settings"')
    assert feed.count("Get the link") == 1


def test_with_a_feed_the_head_has_no_link_and_reset_stays_in_the_actions() -> None:
    from ledger.services import calendar

    client, user = client_for(Role.VIEWER)
    calendar.create_feed(user)
    feed = side_card(page(client, "/calendar/"), "calendar-feed-card")
    assert '<h2 class="section-label" id="calendar-feed-title">Feed</h2></div>' in feed
    actions = feed[feed.index('<div class="feed-actions">') :]
    assert (
        'id="calendar-feed-reset">Reset token</button></form><span aria-hidden="true">·</span>'
        in (actions)
    )
    assert "Get the link" not in feed


def test_a_viewer_has_the_day_panel_head_without_add_entry() -> None:
    day = side_card(page(client_for(Role.VIEWER)[0], "/calendar/"), "calendar-day-panel")
    assert re.search(
        r'<div class="card-head day-panel-head"><h2 class="section-label" '
        r'id="calendar-day-title">[^<]+</h2></div>',
        day,
    )
