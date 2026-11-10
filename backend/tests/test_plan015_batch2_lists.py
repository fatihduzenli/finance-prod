"""PLAN-015 Batch 2 (docs/qa/15/PLAN-015-batch-2-lists.md): the list pages in the dashboard's
language — the shared header (the title block with the counts as its muted sub-line, then the
actions: a segmented control where the page has one, "Download spreadsheet" with the dashboard's
icon, the page's other buttons, the primary last), the Payments totals as KPI cards v2 and its
period links, the Operators avatar tile, and the status chips of the Outbox, Bank deposits and
the Invoices tab, with the Bank deposits state as a segmented control."""

from __future__ import annotations

import html as html_module
import re
from datetime import date

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import PaymentStatus
from ledger.services.payments_ledger import PaymentFilter, parse_filter
from ledger.views.payments import period_links
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 10)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str, expect: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == expect, (path, response.status_code)
    return response.content.decode()


def header_of(html: str) -> str:
    start = html.index('<div class="page-header">')
    return html[start : html.index("</header>", start)]


# --- §3.1 The header ------------------------------------------------------------------------------

#: The six pages, the title, the counts sentence with one lease in the data and whether the
#: page has "Download spreadsheet". The seventh list of the plan, the invoices, is the store and
#: operator pages' Invoices tab: its header is the workspace's (Batch 3).
HEADERS = [
    ("/stores/", "Stores", "1 store · 1 with a lease · 0 vacant", True),
    ("/operators/", "Operators", "1 operator", True),
    ("/leases/", "Leases", "1 lease", True),
    ("/payments/", "Payments", "0 payments", True),
    ("/admin-settings/bank-deposits/", "Bank deposits", "0 deposits", False),
    ("/admin-settings/outbox/", "Outbox", "0 messages", False),
]


@pytest.mark.parametrize("role", [Role.BILLING, Role.ADMIN])
@pytest.mark.parametrize(("path", "title", "counts", "export"), HEADERS)
def test_the_list_pages_share_the_dashboards_header(
    role: Role, path: str, title: str, counts: str, export: bool
) -> None:
    a_lease(a_store("2601 HEAD", 2601), an_operator("Head Co"))
    client = client_for(role)[0]
    if path == "/admin-settings/outbox/" and role != Role.ADMIN:
        page(client, path, expect=403)
        return
    header = header_of(page(client, path))
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
    if "btn-primary" in actions:  # the primary "+ New …" comes last
        assert actions.index('id="export-csv"') < actions.index('class="btn btn-primary"')
        assert actions.count('class="btn btn-primary"') == 1
        assert actions.index('class="btn btn-primary"') > actions.rindex('class="btn"')


# --- §3.2 The Payments totals as KPI cards v2 -----------------------------------------------------


def totals_of(html: str) -> str:
    start = html.index('id="payments-totals"')
    return html[start : html.index("</section>", start)]


def figure(totals: str, value_id: str) -> tuple[str, str]:
    """(the figure's tone classes, its text) of the kpi2 value named ``value_id``."""
    found = re.search(rf'<span class="kpi2-value([^"]*)" id="{value_id}">([^<]*)</span>', totals)
    assert found is not None, value_id
    return found[1].strip(), found[2]


CARDS = [
    ("banknote", "Settled", "total-settled"),
    ("circle-half", "Pending", "total-pending"),
    ("alert", "Returned", "total-returned"),
    ("dash", "Refunded", "total-refunded"),
    ("lock", "Deposits held", "total-deposits-held"),
]


def test_the_totals_are_five_kpi2_cards_with_their_icons_titles_ids_and_tones() -> None:
    lease = a_lease(a_store("2602 KPI", 2602), an_operator("Kpi Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1), due="900.00")
    a_payment(charge, "300.00", date(2026, 10, 2))
    a_payment(charge, "120.00", None, status=PaymentStatus.PENDING, initiated_on=date(2026, 10, 3))
    a_payment(charge, "75.00", None, status=PaymentStatus.RETURNED, initiated_on=date(2026, 10, 4))
    html = page(client_for(Role.BILLING)[0], "/payments/")
    head = html[html.index('id="payment-totals-card"') : html.index('id="payments-totals"')]
    assert (
        '<p class="section-label" id="payment-totals-label">Totals of the 3 payments shown</p>'
        in head
    )
    assert '<div class="card-head" id="payment-totals-card">' in html
    assert 'role="tooltip" id="totals-help"' in head and 'aria-describedby="totals-help"' in head
    totals = totals_of(html)
    assert totals.startswith('id="payments-totals" aria-labelledby="payment-totals-label">')
    assert html[: html.index('id="payments-totals"')].endswith('<section class="kpi-row kpi2-row" ')
    cards = totals.split('<div class="kpi2">')[1:]
    assert len(cards) == 5
    for card, (icon, title, value_id) in zip(cards, CARDS, strict=True):
        assert f'class="icon icon-{icon}"' in card, title
        assert f'<p class="kpi2-title">{title}</p>' in card
        assert "kpi2-sub" not in card and "kpi2-link" not in card  # no note, no link
        assert f'id="{value_id}"' in card
    assert figure(totals, "total-settled") == ("tone-paid", "$300.00")
    assert figure(totals, "total-pending") == ("", "$120.00")
    assert figure(totals, "total-returned") == ("tone-unpaid", "$75.00")
    assert figure(totals, "total-refunded") == ("tone-muted", "$0.00")
    assert figure(totals, "total-deposits-held") == ("tone-muted", "$0.00")
    # filters.js swaps #results: the cards are inside it, so a filter refreshes them.
    results = html.index('<div id="results">')
    assert results < html.index('id="payment-totals-card"') < html.index('<table id="payments"')
    assert 'class="tile' not in totals and 'id="payment-totals-card" class="card"' not in html


def test_with_no_payment_every_figure_is_a_muted_zero_and_the_table_says_so() -> None:
    html = page(client_for(Role.VIEWER)[0], "/payments/")
    totals = totals_of(html)
    for _, _, value_id in CARDS:
        assert figure(totals, value_id) == ("tone-muted", "$0.00"), value_id
    assert "Totals of the 0 payments shown" in html and "No payments match." in html


# --- §3.3 The Payments period links ---------------------------------------------------------------

MONTH = ("2026-11-01", "2026-11-30")
QUARTER = ("2026-10-01", "2026-12-31")  # the quarter at the year's edge
YEAR = ("2026-01-01", "2026-12-31")


def links_of(html: str) -> list[tuple[str, str, bool]]:
    start = html.index('<div class="segmented period-control" id="payments-period"')
    control = html[start : html.index("</div>", start)]
    assert control.startswith(
        '<div class="segmented period-control" id="payments-period" role="group" '
        'aria-label="Period">'
    )
    return [
        (label, html_module.unescape(href), bool(current))
        for href, current, label in re.findall(
            r'<a href="([^"]*)"( aria-current="page")?>([^<]+)</a>', control
        )
    ]


def test_the_four_links_sit_before_download_and_all_time_is_active_without_dates() -> None:
    html = page(client_for(Role.VIEWER)[0], "/payments/")
    actions = header_of(html)[header_of(html).index('<div class="page-actions">') :]
    assert actions.index('id="payments-period"') < actions.index('id="export-csv"')
    assert links_of(html) == [
        ("All time", "/payments/", True),
        ("This month", f"/payments/?date_from={MONTH[0]}&date_to={MONTH[1]}", False),
        ("Quarter", f"/payments/?date_from={QUARTER[0]}&date_to={QUARTER[1]}", False),
        ("Year", f"/payments/?date_from={YEAR[0]}&date_to={YEAR[1]}", False),
    ]


@pytest.mark.parametrize(
    ("dates", "active"),
    [(MONTH, "This month"), (QUARTER, "Quarter"), (YEAR, "Year")],
)
def test_each_period_is_active_when_the_dates_are_exactly_its_window(
    dates: tuple[str, str], active: str
) -> None:
    html = page(client_for(Role.VIEWER)[0], f"/payments/?date_from={dates[0]}&date_to={dates[1]}")
    assert [label for label, _, current in links_of(html) if current] == [active]


@pytest.mark.parametrize(
    "query",
    [
        "date_from=2026-11-01&date_to=2026-11-29",  # one day short of the month
        "date_from=2026-11-01",  # an open end
        "date_to=2026-12-31",
        "date_from=2026-10-01&date_to=2026-11-30",
        "date_from=2025-11-01&date_to=2025-11-30",  # last year's November
        "date_from=garbage&date_to=2026-11-30",
    ],
)
def test_any_other_range_leaves_every_link_inactive(query: str) -> None:
    html = page(client_for(Role.VIEWER)[0], f"/payments/?{query}")
    assert [label for label, _, current in links_of(html) if current] == []


def test_a_garbage_date_is_dropped_by_the_parse_so_the_list_is_all_time() -> None:
    # The existing parse drops an unusable date (the page stays green); the list shown is then
    # every payment, and All time says so. Nothing of the garbage is echoed in a link.
    html = page(client_for(Role.VIEWER)[0], "/payments/?date_from=garbage&date_to=%3Cscript%3E")
    links = links_of(html)
    assert [label for label, _, current in links if current] == ["All time"]
    assert all("garbage" not in href and "script" not in href for _, href, _ in links)


def test_every_link_keeps_the_other_filters_and_date_by_and_drops_the_page() -> None:
    operator = an_operator("Keep Co")
    query = (
        f"date_by=initiated&date_from=2026-11-01&date_to=2026-11-30&method=zelle"
        f"&status=settled&operator={operator.pk}&q=keep&page=2"
    )
    links = links_of(page(client_for(Role.VIEWER)[0], f"/payments/?{query}"))
    kept = f"method=zelle&operator={operator.pk}&status=settled&q=keep"
    assert links == [
        ("All time", f"/payments/?date_by=initiated&{kept}", False),
        (
            "This month",
            f"/payments/?date_by=initiated&date_from={MONTH[0]}&date_to={MONTH[1]}&{kept}",
            True,
        ),
        (
            "Quarter",
            f"/payments/?date_by=initiated&date_from={QUARTER[0]}&date_to={QUARTER[1]}&{kept}",
            False,
        ),
        (
            "Year",
            f"/payments/?date_by=initiated&date_from={YEAR[0]}&date_to={YEAR[1]}&{kept}",
            False,
        ),
    ]


def test_the_windows_follow_today_and_run_no_query() -> None:
    selected = parse_filter({"date_from": "2026-01-01", "date_to": "2026-03-31"})
    with CaptureQueriesContext(connection) as captured:
        links = period_links(selected, date(2026, 2, 28))
    assert captured.captured_queries == []
    assert links == [
        ("All time", "/payments/", False),
        ("This month", "/payments/?date_from=2026-02-01&date_to=2026-02-28", False),
        ("Quarter", "/payments/?date_from=2026-01-01&date_to=2026-03-31", True),
        ("Year", "/payments/?date_from=2026-01-01&date_to=2026-12-31", False),
    ]
    assert period_links(PaymentFilter(), date(2028, 2, 29))[1][1].endswith("date_to=2028-02-29")


def test_the_period_links_add_no_query_to_the_list() -> None:
    client = client_for(Role.VIEWER)[0]
    client.get("/payments/")  # warm the session

    def count(path: str) -> int:
        with CaptureQueriesContext(connection) as captured:
            assert client.get(path).status_code == 200
        return len(captured.captured_queries)

    plain = count("/payments/")
    assert count(f"/payments/?date_from={MONTH[0]}&date_to={MONTH[1]}") == plain
