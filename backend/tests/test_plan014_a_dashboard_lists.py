"""PLAN-014 Batch A (Fable): the dashboard without its fifth row, the Payment status period's
preference (remembered per user, a GET never writes; the card itself left with PLAN-015 Batch
1), the vacant rows of the stores list, the search + Filters component on every list page
(count, chips, Clear and Apply, the parameter names unchanged), the five payments tiles, and the
rule that every numeric column header is aligned like its cells — with the adversarial cases of
plan §5 A (a hostile ``ps``, ``ps_at=2026-02-30``, a date far in the past, a 500-character
search, a chip for an unknown value)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from html.parser import HTMLParser

import pytest
from django.test import Client

from accounts.roles import Role
from accounts.themes import payment_status_period_for
from ledger.models import CollectionStatus, PaymentStatus
from ledger.services import dashboard as blocks
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)  # a Tuesday


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    # The dashboard reads django.utils.timezone.localdate (there is no ``today`` name to patch:
    # the earlier raising=False patches pinned nothing, and the tests passed only while the
    # real month was October 2026).
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str = "/", expect: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == expect, (path, response.status_code)
    return response.content.decode()


def section(html: str, element_id: str, end: str = "</section>") -> str:
    start = html.index(f'id="{element_id}"')
    return html[start : html.index(end, start)]


# --- A2: the fifth row is gone ------------------------------------------------------------------


def test_the_dashboard_has_no_coming_up_or_collection_card() -> None:
    # PLAN-015 Batch 1 re-pin: Design v2's rows; the Cashflow, Payment status, Needs attention and
    # Recent payments cards went too (tests/test_dashboard_v2.py pins the new order).
    html = page(client_for(Role.BILLING)[0])
    assert 'id="coming-up"' not in html and 'id="collection-card"' not in html
    assert "Coming up" not in html
    for gone in ("cashflow-card", "payment-status", "attention", "recent-payments"):
        assert f'id="{gone}"' not in html, gone


def test_the_service_no_longer_exports_the_removed_blocks() -> None:
    assert not hasattr(blocks, "coming_up") and not hasattr(blocks, "ComingItem")
    # PLAN-015 Batch 1: the figures and cards of the PLAN-014 page went with them.
    for gone in (
        "collection_progress",
        "pending_debits",
        "recent_payments",
        "cashflow",
        "payment_status",
        "period_for",
    ):
        assert not hasattr(blocks, gone), gone


# --- A3: the period ------------------------------------------------------------------------------
# PLAN-015 Batch 1: the Payment status card and its Weekly | Monthly period are gone (the owner's
# decision 1); their tests went with them. The preference route stays and still saves only on a
# POST; ?ps= on the dashboard is ignored and never echoed.


@pytest.mark.parametrize(
    "query",
    [
        "/?ps=<script>",
        "/?ps_at=2026-02-30",
        "/?ps_at=0001-01-01",
        "/?ps=monthly&ps_at=99999",
        "/?ps=%00",
    ],
)
def test_a_hostile_period_never_breaks_the_page_or_echoes(query: str) -> None:
    html = page(client_for(Role.VIEWER)[0], query)
    assert "<script>" not in section(html, "figures-card")
    assert 'name="period" value="month" aria-pressed="true"' in html


def test_the_choice_is_saved_by_post_only_and_remembered() -> None:
    client, user = client_for(Role.VIEWER)  # a viewer may save a preference
    page(client, "/?ps=monthly")
    assert payment_status_period_for(user) == "weekly"  # a GET never writes
    response = client.post(
        "/settings/payment-status/", {"period": "monthly", "next": "/?ps=monthly"}
    )
    assert response.status_code == 302 and response["Location"] == "/?ps=monthly"
    assert payment_status_period_for(user) == "monthly"
    response = client.post(
        "/settings/payment-status/", {"period": "monthly", "next": "https://evil.example/"}
    )
    assert response.status_code == 302 and response["Location"] == "/"
    assert client.post("/settings/payment-status/", {"period": "yearly"}).status_code == 400
    assert client.post("/settings/payment-status/", {}).status_code == 400
    assert client.get("/settings/payment-status/").status_code == 405
    assert payment_status_period_for(user) == "monthly"  # the bad posts saved nothing


def test_an_anonymous_post_does_not_save() -> None:
    response = Client().post("/settings/payment-status/", {"period": "monthly"})
    assert response.status_code in (302, 401, 403)


# --- A4: vacant rows -----------------------------------------------------------------------------


def stores_html(role: Role) -> str:
    return page(client_for(role)[0], "/stores/")


def row_of(html: str, label: str) -> str:
    match = re.search(
        rf"<tr[^>]*>(?:(?!</tr>).)*?{re.escape(label)}(?:(?!</tr>).)*</tr>", html, re.S
    )
    assert match is not None, label
    return match.group(0)


def test_a_store_never_leased_reads_vacant_never_leased_with_a_dash_balance() -> None:
    a_store("1411", number=1411)
    row = row_of(stores_html(Role.BILLING), "1411")
    assert "Vacant · never leased" in row and "—" in row
    assert "Add lease" not in row  # billing may not create a lease


def test_a_store_whose_lease_ended_reads_the_days_and_an_admin_sees_add_lease() -> None:
    store = a_store("1412", number=1412)
    lease = a_lease(
        store, an_operator("Gone Co"), started=date(2026, 1, 1), status=CollectionStatus.ENDED
    )
    lease.ended_on = TODAY - timedelta(days=23)
    lease.save(update_fields=["ended_on"])
    row = row_of(stores_html(Role.ADMIN), "1412")
    assert "Vacant · 23 days" in row and "—" in row
    assert re.search(
        rf'href="/leases/new/\?store={store.pk}"[^>]*>\s*\+ Add lease', row
    ) or re.search(rf'href="[^"]+\?store={store.pk}"', row)


def test_a_leftover_balance_of_the_previous_operator_shows_as_unpaid() -> None:
    store = a_store("1413", number=1413)
    lease = a_lease(
        store, an_operator("Left Co"), started=date(2026, 1, 1), status=CollectionStatus.ENDED
    )
    lease.ended_on = TODAY - timedelta(days=1)
    lease.save(update_fields=["ended_on"])
    a_rent_charge(lease, date(2026, 9, 1), due="1200.00")
    row = row_of(stores_html(Role.BILLING), "1413")
    assert "$1,200.00" in row and has_token(row, "unpaid") and "Vacant · 1 day" in row
    balance = re.search(r'<td[^>]*class="[^"]*store-balance[^"]*"[^>]*>(.*?)</td>', row, re.S)
    assert balance is not None and "—" not in balance.group(1) and "unpaid" in balance.group(1)


def test_a_lease_ending_today_is_still_the_current_lease() -> None:
    # The list's rule: a lease not ended before today is in effect, so the store is not vacant.
    store = a_store("1414", number=1414)
    lease = a_lease(
        store, an_operator("Today Co"), started=date(2026, 1, 1), status=CollectionStatus.ENDED
    )
    lease.ended_on = TODAY
    lease.save(update_fields=["ended_on"])
    row = row_of(stores_html(Role.BILLING), "1414")
    assert "Vacant" not in row and "Add lease" not in row


# --- A5: search + Filters on every list page ------------------------------------------------------

LIST_PAGES = [
    "/stores/",
    "/operators/",
    "/grid/",
    "/payments/",
    "/leases/",
    "/arrears/",
]


@pytest.mark.parametrize("path", LIST_PAGES)
def test_every_list_page_has_the_search_row_the_panel_and_a_results_region(path: str) -> None:
    html = page(client_for(Role.BILLING)[0], path)
    assert 'id="filters"' in html and 'id="filters-panel"' in html and 'id="results"' in html, path
    form = section(html, "filters", "</form>")
    assert 'type="search"' in form and "filters-toggle" in form, path
    assert re.search(r'aria-expanded="false"', form) and 'aria-controls="filters-panel"' in form, (
        path
    )
    panel = html[html.index('id="filters-panel"') :]
    assert ">Apply<" in panel and ">Clear<" in panel, path
    assert 'type="submit"' in panel, path
    assert "filters.js" in html, path
    assert ">Filter<" not in html, path  # the old button is gone


def test_the_filters_count_and_chips_follow_the_active_filters() -> None:
    a_store("1421", number=1421, state="NJ")
    a_store("1422", number=1422, state="GA")
    client, _ = client_for(Role.VIEWER)
    html = page(client, "/stores/")
    assert re.search(r">Filters<", section(html, "filters", "</form>"))
    assert 'class="chip"' not in html
    html = page(client, "/stores/?state=NJ")
    form = section(html, "filters", "</form>")
    assert "Filters · 1" in form
    chip = re.search(
        r'<a class="chip"[^>]*href="([^"]+)"[^>]*>State: New Jersey\s*(?:<[^>]+>)?\s*×', html
    )
    assert chip is not None and "state=" not in chip.group(1)
    html = page(client, "/stores/?state=NJ&status=vacant&q=14&sort=balance")
    assert "Filters · 2" in section(html, "filters", "</form>")  # search and sort do not count
    clear = re.search(r'<a[^>]*id="filters-clear"[^>]*href="([^"]+)"', html)
    assert clear is not None and "q=14" in clear.group(1) and "sort=balance" in clear.group(1)
    assert "state=" not in clear.group(1) and "status=" not in clear.group(1)


def test_an_unknown_filter_value_makes_no_chip_and_is_never_echoed() -> None:
    html = page(client_for(Role.VIEWER)[0], "/stores/?state=ZZ&status=%3Cscript%3E")
    form = section(html, "filters", "</form>")
    assert 'class="chip"' not in form and ">Filters<" in form
    assert "%3Cscript%3E" not in form and "&lt;script&gt;" not in html


def test_a_long_search_and_a_hostile_one_answer_200() -> None:
    client, _ = client_for(Role.VIEWER)
    assert client.get("/stores/", {"q": "a" * 500}).status_code == 200
    html = page(client, "/stores/?q=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "<script>alert" not in html


def test_the_sort_menu_exists_only_where_a_page_sorts() -> None:
    client, _ = client_for(Role.VIEWER)
    assert "Sort:" in section(page(client, "/stores/"), "filters", "</form>")
    assert "Sort:" in section(page(client, "/arrears/"), "filters", "</form>")
    assert "Sort:" not in section(page(client, "/operators/"), "filters", "</form>")


def test_the_old_stores_list_urls_still_answer_the_same_rows() -> None:
    a_store("1431", number=1431, state="NJ")
    a_store("1432", number=1432, state="GA")
    client, _ = client_for(Role.VIEWER)
    results = section(
        page(client, "/stores/?state=NJ&status=vacant&balance=&sort=number"), "results", "</table>"
    )
    assert "1431" in results and "1432" not in results


# --- A6: the five tiles ---------------------------------------------------------------------------


def kpi_value(totals: str, value_id: str) -> re.Match[str] | None:
    """The ``kpi2-value`` span carrying ``value_id`` (PLAN-015 Batch 2: the tiles became cards)."""
    return re.search(rf'<span class="kpi2-value([^"]*)" id="{value_id}">', totals)


def test_the_payments_totals_are_five_tiles_with_tones_and_a_tooltip() -> None:
    # PLAN-015 Batch 2 re-pin: the five tiles are five {% kpi2 %} cards; the tones moved from
    # the tile (tone-paid, tone-unpaid, is-zero) to the card's figure (tone-paid, tone-unpaid,
    # tone-muted).
    lease = a_lease(a_store("1441"), an_operator("Tiles Co"))
    charge = a_rent_charge(lease, date(2026, 9, 1), due="3500.00")
    a_payment(charge, "100.00", date(2026, 9, 5))
    a_payment(charge, "50.00", None, status=PaymentStatus.RETURNED, initiated_on=date(2026, 9, 6))
    html = page(
        client_for(Role.BILLING)[0],
        "/payments/?date_by=initiated&date_from=2026-09-01&date_to=2026-09-30",
    )
    totals = section(html, "payments-totals")
    assert totals.count('<div class="kpi2">') == 5
    labels = ["Settled", "Pending", "Returned", "Refunded", "Deposits held"]
    assert re.findall(r'<p class="kpi2-title">([^<]+)</p>', totals) == labels
    settled = kpi_value(totals, "total-settled")
    returned = kpi_value(totals, "total-returned")
    refunded = kpi_value(totals, "total-refunded")
    assert settled and "tone-paid" in settled.group(1)
    assert returned and "tone-unpaid" in returned.group(1) and "tone-muted" not in returned.group(1)
    assert refunded and "tone-muted" in refunded.group(1)
    card = section(html, "payment-totals-card")
    assert 'role="tooltip"' in card and 'aria-describedby="totals-help"' in card
    assert "Pending payments are never counted as collected" in card
    assert "$100.00" in totals and "$50.00" in totals


def test_a_zero_returned_tile_is_muted_not_red() -> None:
    html = page(client_for(Role.BILLING)[0], "/payments/")
    returned = kpi_value(section(html, "payments-totals"), "total-returned")
    assert returned and "tone-muted" in returned.group(1) and "tone-unpaid" not in returned.group(1)


# --- A7: numeric headers aligned like their cells ------------------------------------------------

NUMERIC = {"money", "num", "count"}


class _Tables(HTMLParser):
    """Every table's header classes and body cell classes by column (colspan rows skipped)."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[tuple[list[set[str]], list[list[set[str]]]]] = []
        self._row: list[set[str]] | None = None
        self._row_spans = False
        self._in_head = False
        self._cell: set[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table":
            self.tables.append(([], []))
        elif tag == "thead":
            self._in_head = True
        elif tag == "tr":
            self._row, self._row_spans = [], False
        elif tag in ("th", "td") and self._row is not None:
            classes = set((a.get("class") or "").split())
            if a.get("colspan"):
                self._row_spans = True
            self._row.append(classes)
            self._cell = classes

    def handle_endtag(self, tag: str) -> None:
        if tag == "thead":
            self._in_head = False
        elif tag == "tr" and self._row is not None and self.tables:
            head, body = self.tables[-1]
            if not self._row_spans:
                if self._in_head or (not head and not body and self._row):
                    if not head:
                        head.extend(self._row)
                else:
                    body.append(self._row)
            self._row = None


PAGES_WITH_TABLES = [
    "/",
    "/stores/",
    "/operators/",
    "/leases/",
    "/payments/",
    "/grid/",
    "/arrears/",
    "/run/",
    "/reports/pnl/",
    "/reports/aging/",
    "/reports/revenue/",
    "/reports/deposits/",
    "/reports/payments-by-method/",
    "/returns/",
]


def a_world_with_numbers() -> None:
    lease = a_lease(a_store("1451", number=1451), an_operator("Numbers Co"))
    paid = a_rent_charge(lease, date(2026, 9, 1), due="3500.00", paid="3500.00")
    a_payment(paid, "3500.00", date(2026, 9, 5))
    a_rent_charge(lease, date(2026, 10, 1), due="3500.00")
    a_rent_charge(lease, date(2026, 8, 1), due="3500.00", paid="1000.00")


@pytest.mark.parametrize("path", PAGES_WITH_TABLES)
def test_every_numeric_column_header_carries_its_cells_alignment_class(path: str) -> None:
    a_world_with_numbers()
    html = page(client_for(Role.ADMIN)[0], path)
    parser = _Tables()
    parser.feed(html)
    assert parser.tables, path
    for head, body in parser.tables:
        if not head or not body:
            continue
        for column, header in enumerate(head):
            cells = [row[column] for row in body if len(row) == len(head)]
            if not cells:
                continue
            numeric = {cls for row in cells for cls in row & NUMERIC}
            if numeric and all(row & NUMERIC for row in cells):
                assert header & numeric, (path, column, header, numeric)


def test_the_header_parser_sees_a_misaligned_header() -> None:
    parser = _Tables()
    parser.feed(
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        '<tbody><tr><td>x</td><td class="money">1</td></tr></tbody></table>'
    )
    ((head, body),) = parser.tables
    assert head == [set(), set()] and body == [[set(), {"money"}]]
