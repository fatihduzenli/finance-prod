"""The stores list and the list family (docs/qa/PLAN-012f-a-design-follow-up.md D3; DESIGN-SPEC
§4 Stores list, stores-list-laptop.png), the implementer's cases; the adversarial ones are the QA
owner's file.

The counts line (every store, those with a lease in effect or to come, the rest vacant); the
filters ``state``, ``status``, ``balance`` and ``sort``, each from its fixed list, an unknown
value falling back to the default and never echoed; the Balance filter and sort over every
store, never one page; the balance badge's families; the chevron; the footer's range and pager;
the siblings' structure; the list's query count."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import CollectionStatus, Lease, PaymentChannel, RentChargeStatus, Store
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 16)
LIST = "/stores/"


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def a_numbered_store(label: str, number: int, city: str = "Newark", state: str = "NJ") -> Store:
    store = a_store(label, number, state=state)
    store.city = city
    store.save(update_fields=["city"])
    return store


def world() -> dict[str, Store]:
    """Seven stores, one of each balance family, as the drawing has them."""
    owing = a_lease(a_numbered_store("Owing", 1001), an_operator("Owing Co"), rent="2000.00")
    a_rent_charge(owing, date(2026, 11, 1), RentChargeStatus.DUE, "3000.00", day=1)
    a_lease(a_numbered_store("Paid", 1002), an_operator("Paid Co"), rent="1500.00")
    late = a_lease(
        a_numbered_store("Late", 1003, state="NY"), an_operator("Late Co"), rent="2100.00"
    )
    a_rent_charge(late, date(2026, 10, 1), RentChargeStatus.UNPAID, "2100.00", day=1)
    part = a_lease(
        a_numbered_store("Part", 1004),
        an_operator("Part Co"),
        rent="1200.00",
        channel=PaymentChannel.ZELLE,
    )
    a_rent_charge(part, date(2026, 11, 1), RentChargeStatus.PARTIAL, "1200.00", "541.94", day=1)
    ended = a_lease(a_numbered_store("Ended", 1005), an_operator("Gone Co"))
    Lease.objects.filter(pk=ended.pk).update(
        ended_on=date(2026, 9, 15), collection_status=CollectionStatus.ENDED
    )
    a_numbered_store("Empty", 1006)
    closed = a_numbered_store("Shut", 1007)
    closed.status = "closed"
    closed.save(update_fields=["status"])
    return {store.external_label: store for store in Store.objects.all()}


def page(client: Client, **params: str) -> str:
    response = client.get(LIST, params)
    assert response.status_code == 200
    return response.content.decode()


def rows(html: str) -> dict[str, str]:
    body = html[html.index("<tbody>") : html.index("</tbody>")]
    found = {}
    for row in re.findall(r"<tr>.*?</tr>", body, re.S):
        title = re.search(r'class="cell-title"[^>]*>#\d+ ([^<]+)<', row)
        if title:
            found[title.group(1)] = row
    return found


def order(html: str) -> list[str]:
    return list(rows(html))


def text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# --- The counts line and the footer ---------------------------------------------------------


def test_the_counts_line_counts_every_store_whatever_the_filter(viewer: Client) -> None:
    world()
    for params in ({}, {"q": "owing"}, {"status": "closed"}, {"balance": "overdue"}):
        html = page(viewer, **params)
        counts = re.search(r'<span class="list-counts" id="list-counts">([^<]+)</span>', html)
        assert counts is not None
        assert counts.group(1) == "7 stores · 4 with a lease · 3 vacant", params


def test_the_footer_says_filtered_from_only_under_a_filter(viewer: Client) -> None:
    world()
    assert '<span class="pager-count">1–7 of 7</span>' in page(viewer)
    assert '<span class="pager-count">1–7 of 7</span>' in page(viewer, sort="name")
    filtered = page(viewer, status="vacant")
    assert '<span class="pager-count">1–2 of 2 · filtered from 7</span>' in filtered
    none = page(viewer, q="zzz")
    assert '<span class="pager-count">0 of 0 · filtered from 7</span>' in none
    assert "No stores match." in none


def test_the_pager_at_page_two_of_three(viewer: Client) -> None:
    for number in range(60):
        a_numbered_store(f"P{number:02d}", 3000 + number)
    html = page(viewer, sort="name", page="2")
    footer = html[html.index('<div class="pager list-footer"') :].split("</div>", 1)[0]
    assert '<span class="pager-count">26–50 of 60</span>' in footer
    assert 'rel="prev" href="?sort=name&amp;page=1">‹ Previous</a>' in footer
    assert 'rel="next" href="?sort=name&amp;page=3">Next ›</a>' in footer
    assert '<span class="pager-page" aria-current="page">2</span>' in footer
    assert 'href="?sort=name&amp;page=1" aria-label="Page 1">1</a>' in footer
    assert 'href="?sort=name&amp;page=3" aria-label="Page 3">3</a>' in footer
    last = page(viewer, page="3")
    assert '<span class="btn btn-sm pager-off" aria-disabled="true">Next ›</span>' in last
    assert '<span class="pager-count">51–60 of 60</span>' in last


# --- The table -----------------------------------------------------------------------------


def balance(row: str) -> str:
    match = re.search(r'<td class="store-balance"[^>]*>(.*?)</td>', row, re.S)
    assert match is not None
    return match.group(1)


def test_each_balance_family_and_status_badge(viewer: Client) -> None:
    world()
    found = rows(page(viewer))
    assert "badge-unpaid" in found["Owing"] and "$3,000.00 unpaid" in found["Owing"]
    assert "badge-paid" in found["Paid"] and "Paid up" in found["Paid"]
    assert "badge-overdue" in found["Late"] and "$2,100.00 overdue" in found["Late"]
    assert "badge-partial" in found["Part"] and "$658.06 partly paid" in found["Part"]
    # PLAN-014 A4: without a lease the Balance is "—" and the Status says how long it is vacant.
    assert "badge" not in balance(found["Ended"]) and "—" in balance(found["Ended"])
    assert "badge-neutral" in found["Ended"] and "Vacant · 31 days" in found["Ended"]
    assert "badge-neutral" in found["Empty"] and "Vacant · never leased" in found["Empty"]

    def status(name: str) -> str:
        match = re.search(r'<td class="store-status"[^>]*>(.*?)</td>', found[name], re.S)
        assert match is not None
        return match.group(1)

    assert "badge-paid" in status("Owing") and "Active" in status("Owing")
    assert "badge-neutral" in status("Empty") and "Vacant" in status("Empty")
    assert "badge-unpaid" in status("Shut") and "Closed" in status("Shut")


def test_the_columns_read_operator_rent_channel_and_place(viewer: Client) -> None:
    stores = world()
    found = rows(page(viewer))
    owing = found["Owing"]
    operator = Lease.objects.get(store=stores["Owing"]).operator
    assert operator is not None
    assert f'<a href="/operators/{operator.pk}/">Owing Co</a>' in owing
    assert '<td class="money" data-role="amount" data-label="Rent">$2,000.00</td>' in owing
    assert ">Bank debit</td>" in owing and ">Zelle</td>" in found["Part"]
    assert '<span class="cell-subline">Newark, New Jersey</span>' in owing
    assert ">—</td>" in found["Empty"]  # no rent, no channel


def test_the_chevron_links_to_the_store(viewer: Client) -> None:
    stores = world()
    row = rows(page(viewer))["Paid"]
    chevron = re.search(r'<td class="cell-chevron"[^>]*>(.*?)</td>', row, re.S)
    assert chevron is not None
    assert f'<a href="/stores/{stores["Paid"].pk}/" aria-label="Open #1002 Paid">' in chevron[1]
    assert 'class="icon icon-chevron-right"' in chevron[1]
    assert 'data-role="meta"' in row[row.index('class="cell-chevron"') - 40 :]


# --- The filters ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ["Owing", "Paid", "Late", "Part", "Ended", "Empty", "Shut"]),
        ({"state": "NY"}, ["Late"]),
        ({"status": "active"}, ["Owing", "Paid", "Late", "Part"]),
        ({"status": "vacant"}, ["Ended", "Empty"]),
        ({"status": "closed"}, ["Shut"]),
        ({"balance": "owing"}, ["Owing", "Late", "Part"]),
        ({"balance": "overdue"}, ["Late"]),
        ({"balance": "paid"}, ["Paid"]),
        ({"sort": "name"}, ["Empty", "Ended", "Late", "Owing", "Paid", "Part", "Shut"]),
        ({"sort": "balance"}, ["Owing", "Late", "Part", "Paid", "Ended", "Empty", "Shut"]),
        ({"q": "late"}, ["Late"]),
        ({"q": "owing co", "status": "active"}, ["Owing"]),
    ],
)
def test_each_filter_value(viewer: Client, params: dict[str, str], expected: list[str]) -> None:
    world()
    assert order(page(viewer, **params)) == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("state", "ZZ"),
        ("state", "ny"),
        ("status", "terminated"),
        ("balance", "huge"),
        ("sort", "rent"),
        ("sort", "-number"),
        ("status", "x" * 300),
    ],
)
def test_an_unknown_value_is_the_default_and_never_echoed(
    viewer: Client, name: str, value: str
) -> None:
    world()
    html = page(viewer, **{name: value})
    assert order(html) == ["Owing", "Paid", "Late", "Part", "Ended", "Empty", "Shut"]
    assert "filtered from" not in html
    start = html.index('id="store-filters"')
    form = html[start : html.index("</form>", start)]
    assert f'value="{value}"' not in form and f">{value}<" not in html
    selected = re.findall(r'<option value="([^"]*)" selected>', form)
    assert selected == ["", "", "number"]  # Status All, Balance Any, Sort Store number


def test_the_chosen_filters_are_selected_and_kept_by_the_pager(viewer: Client) -> None:
    for number in range(30):
        store = a_numbered_store(f"Late {number:02d}", 4000 + number, state="TX")
        lease = a_lease(store, an_operator(f"Late Op {number}"))
        a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "100.00", day=1)
    html = page(viewer, state="TX", status="active", balance="overdue", sort="balance")
    start = html.index('id="store-filters"')
    form = html[start : html.index("</form>", start)]
    for value in ("TX", "active", "overdue", "balance"):
        assert f'<option value="{value}" selected>' in form
    assert (
        'href="?state=TX&amp;status=active&amp;balance=overdue&amp;sort=balance&amp;page=2"' in html
    )


def test_the_balance_filter_and_sort_cover_every_store_not_one_page(viewer: Client) -> None:
    for number in range(40):
        a_lease(a_numbered_store(f"Store {number:02d}", 5000 + number), an_operator(f"Op {number}"))
    # The only stores owing are the last two by number: far past the first page.
    for number, due in ((5038, "700.00"), (5039, "900.00")):
        lease = Lease.objects.get(store__store_number=number)
        a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, due, day=1)
    owing = page(viewer, balance="owing")
    assert order(owing) == ["Store 38", "Store 39"]
    assert '<span class="pager-count">1–2 of 2 · filtered from 40</span>' in owing
    by_balance = order(page(viewer, sort="balance"))
    assert by_balance[:2] == ["Store 39", "Store 38"]


def test_a_settled_payment_makes_a_store_paid_up(viewer: Client) -> None:
    lease = a_lease(a_numbered_store("Settled", 6001), an_operator("Settled Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PAID, "500.00", "500.00")
    a_payment(charge, "500.00", date(2026, 10, 2))
    assert "Paid up" in rows(page(viewer))["Settled"]
    assert order(page(viewer, balance="owing")) == []


def test_the_spreadsheet_link_keeps_the_search_only(viewer: Client) -> None:
    world()
    html = page(viewer, q="late", status="active", balance="overdue")
    assert 'id="export-csv" href="/stores/export.csv?q=late"' in html


# --- The query count -------------------------------------------------------------------------


def _queries(client: Client, params: dict[str, str]) -> int:
    with CaptureQueriesContext(connection) as captured:
        assert client.get(LIST, params).status_code == 200
    return len(captured.captured_queries)


def test_the_list_keeps_its_query_budget(viewer: Client) -> None:
    world()
    viewer.get(LIST)  # warm the session
    params = {"q": "co", "status": "active", "balance": "owing", "sort": "balance"}
    small = _queries(viewer, params)
    # 11 before PLAN-012f-a D3 (the session and the shell, the count and the page); +3: the
    # page's one query became one over every store (its lease, operator, rent and the counts
    # line from the same rows), and the balances are receivables.open_balances' four queries.
    # +1 since PLAN-012g A2: the theme row.
    assert small <= 15
    for number in range(25):
        store = a_numbered_store(f"More {number}", 7000 + number)
        lease = a_lease(store, an_operator(f"M{number}"))
        a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "10.00", day=1)
    assert _queries(viewer, params) == small
    assert _queries(viewer, {}) == small


# --- The siblings --------------------------------------------------------------------------


def _first_cells(html: str, table_id: str) -> list[str]:
    start = html.index(f'<table id="{table_id}"')
    body = html[html.index("<tbody>", start) : html.index("</tbody>", start)]
    return re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>", body, re.S)


@pytest.mark.parametrize(
    ("path", "table_id"),
    [
        ("/stores/", "stores"),
        ("/operators/", "operators"),
        ("/leases/", "leases"),
        ("/payments/", "payments"),
    ],
)
def test_the_family_structure_of_each_paged_list(path: str, table_id: str) -> None:
    lease = a_lease(a_numbered_store("Family", 8001), an_operator("Family Co"))
    charge = a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.PAID, "100.00", "100.00")
    a_payment(charge, "100.00", date(2026, 9, 2))
    html = client_for(Role.ADMIN)[0].get(path).content.decode()
    start = html.index('<div class="page-header">')
    header = html[start : html.index("</header>", start)]
    assert re.search(r'<div class="page-title"><h1>[^<]+</h1><span class="list-counts"', header)
    assert 'id="export-csv"' in header
    assert html.count('class="card filter-card"') == 1
    assert 'class="btn btn-dark" type="submit"' in html
    cells = _first_cells(html, table_id)
    assert cells and all('class="cell-title"' in cell for cell in cells), path
    if path != "/operators/":  # an operator without a legal name has no subline
        assert 'class="cell-subline"' in cells[0], path
    card = html[html.index('class="card table-card list-card"') :]
    assert card.index('<div class="pager list-footer"') < card.index("</main>")
    assert re.search(r'<td class="cell-chevron" data-role="meta"[^>]*><a href="/', html)


def test_the_operators_list_reads_the_legal_name_and_the_stores(viewer: Client) -> None:
    operator = an_operator("Count Co")
    operator.legal_name = "Count Company LLC"
    operator.save(update_fields=["legal_name"])
    a_lease(a_numbered_store("One", 8101), operator)
    a_lease(a_numbered_store("Two", 8102), operator)
    ended = a_lease(a_numbered_store("Three", 8103), operator)
    Lease.objects.filter(pk=ended.pk).update(
        ended_on=date(2026, 1, 31), collection_status=CollectionStatus.ENDED
    )
    html = viewer.get("/operators/").content.decode()
    assert '<span class="cell-subline">Count Company LLC</span>' in html
    assert re.search(r'<td class="operator-stores"[^>]*>2</td>', html)
    assert '<span class="list-counts" id="list-counts">1 operator</span>' in html


def test_the_leases_list_reads_the_operator_and_a_status_badge(viewer: Client) -> None:
    a_lease(a_numbered_store("Leased", 8201), an_operator("Lease Co"))
    html = viewer.get("/leases/").content.decode()
    assert '<span class="cell-subline">Lease Co</span>' in html
    status = re.search(r'<td class="lease-status"[^>]*>(.*?)</td>', html, re.S)
    assert status is not None and "badge-paid" in status[1] and "Active" in status[1]


def test_the_other_family_pages_carry_the_counts_and_title_cells() -> None:
    lease = a_lease(a_numbered_store("Behind", 8301), an_operator("Behind Co"))
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "400.00", day=1)
    admin = client_for(Role.ADMIN)[0]
    arrears = admin.get("/arrears/").content.decode()
    assert '<span class="list-counts" id="list-counts">1 operator behind · $400.00 owed' in arrears
    assert 'class="cell-subline">Behind</span>' in arrears
    overdue = re.search(r'<td class="arrears-overdue"[^>]*>(.*?)</td>', arrears, re.S)
    assert overdue is not None and "badge-overdue" in overdue[1] and "$400.00 overdue" in overdue[1]
    returns = admin.get("/returns/").content.decode()
    assert '<span class="list-counts" id="list-counts">0 not resolved</span>' in returns
    assert '<div class="pager list-footer"' in returns
    disputes = admin.get("/disputes/").content.decode()
    assert 'id="list-counts">0 disputes in the last 90 days</span>' in disputes
    search = admin.get("/search/", {"q": "behind"}).content.decode()
    assert 'id="list-counts">1 store · 1 operator</span>' in search
    assert '<span class="cell-subline">' in search and 'class="cell-chevron"' in search
