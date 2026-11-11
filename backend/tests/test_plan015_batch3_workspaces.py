"""PLAN-015 Batch 3 (docs/qa/15/PLAN-015-batch-3-workspaces.md): the store and operator
workspaces in the dashboard's language — the figure row of KPI cards v2 under the action bar on
both pages (§3.1, §3.2) in place of the phone's summary strip, every side card's head in the
card-head shape (§3.3) and the operator's stores as tiles in the dashboard's operator-tile style
(§3.4)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import ContactKind, Operator, PaymentStatus, RentChargeStatus
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_plan012f_d_pages import APP_CSS, phone_block
from tests.test_plan012f_pages import a_store_world
from tests.test_plan012fa_d_operator import MARKUP, an_entry, by_id, operator_page, three_stores

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str, expect: int = 200) -> str:
    response = client.get(path)
    assert response.status_code == expect, (path, response.status_code)
    return response.content.decode()


def header_of(html: str) -> str:
    start = html.index('id="page-header"')
    return html[start : html.index("</header>", start)]


def figure_row(html: str) -> str:
    header = header_of(html)
    start = header.index('<section class="kpi-row kpi2-row" id="summary-strip"')
    return header[start : header.index("</section>", start)]


def figure(row: str, key: str) -> str:
    """The card of ``div#figure-<key>``: up to the next card, or the row's end."""
    start = row.index(f'<div id="figure-{key}">')
    following = row.find('<div id="figure-', start + 1)
    return row[start : following if following != -1 else len(row)]


def titles(row: str) -> list[str]:
    return re.findall(r'<p class="kpi2-title">([^<]+)</p>', row)


def sub_of(card: str) -> str | None:
    found = re.search(r'<p class="kpi2-sub">([^<]*)</p>', card)
    return found.group(1) if found else None


def value_of(card: str) -> tuple[str, str]:
    """(the figure's classes, its text)."""
    found = re.search(r'<span class="(kpi2-value[^"]*)"[^>]*>([^<]*)</span>', card)
    assert found, card
    return found.group(1), found.group(2)


def link_of(card: str) -> tuple[str, str] | None:
    found = re.search(r'<a class="kpi2-link" href="([^"]+)"><span>([^<]+)</span>', card)
    return (found.group(1), found.group(2)) if found else None


def icon_of(card: str) -> str:
    found = re.search(
        r'<span class="kpi2-icon" aria-hidden="true"><svg class="icon icon-([a-z-]+)"', card
    )
    assert found, card
    return found.group(1)


# --- §3.1 The store's figure row ----------------------------------------------------------------


def test_the_store_figure_row_sits_between_the_action_bar_and_the_tabs() -> None:
    lease = a_store_world()
    html = page(client_for(Role.BILLING)[0], f"/stores/{lease.store_id}/")
    header = header_of(html)
    assert header.count('id="summary-strip"') == 1
    assert '<section class="kpi-row kpi2-row" id="summary-strip" aria-label="Summary">' in header
    assert header.index('id="action-bar"') < header.index('id="summary-strip"')
    assert header.index('id="summary-strip"') < header.index('id="store-tabs"')
    assert titles(figure_row(html)) == ["Open balance", "Rent", "Autopay"]
    assert 'class="summary-' not in html  # the strip's cells are gone


def test_the_store_figures_with_a_lease_read_the_header() -> None:
    lease = a_store_world()
    store_url = f"/stores/{lease.store_id}/"
    row = figure_row(page(client_for(Role.BILLING)[0], store_url))

    balance = figure(row, "open-balance")
    assert icon_of(balance) == "alert"
    assert sub_of(balance) == "1 open charge"
    assert value_of(balance) == ("kpi2-value tone-unpaid", "$2,000.00")
    assert 'id="figure-open-balance-value"' in balance
    assert link_of(balance) == (f"{store_url}?tab=charges#tab-charges", "Open charges")

    rent = figure(row, "rent")
    assert icon_of(rent) == "banknote"
    assert sub_of(rent) == "Debit day 1st"
    assert value_of(rent) == ("kpi2-value", "$2,000.00 / month")
    assert link_of(rent) == (f"/leases/{lease.pk}/", "Lease")

    autopay = figure(row, "autopay")
    assert icon_of(autopay) == "calendar"
    assert sub_of(autopay) == "Next debit Nov 1, 2026"  # the NEXT card's rent debit
    assert value_of(autopay) == ("kpi2-value", "Not set up")
    assert link_of(autopay) == (f"{store_url}?tab=schedules#tab-schedules", "Autopay")


def test_two_open_charges_are_counted_in_the_plural() -> None:
    lease = a_store_world()
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "2000.00", day=1)
    row = figure_row(page(client_for(Role.BILLING)[0], f"/stores/{lease.store_id}/"))
    balance = figure(row, "open-balance")
    assert sub_of(balance) == "2 open charges"
    assert value_of(balance) == ("kpi2-value tone-unpaid", "$4,000.00")


def test_a_store_without_a_lease_has_a_dash_rent_no_lease_link_and_no_autopay() -> None:
    store = a_store("Vacant", 2001)
    store_url = f"/stores/{store.pk}/"
    row = figure_row(page(client_for(Role.BILLING)[0], store_url))
    assert titles(row) == ["Open balance", "Rent", "Autopay"]

    balance = figure(row, "open-balance")
    assert sub_of(balance) is None  # no open charge: no sub-line
    assert value_of(balance) == ("kpi2-value", "$0.00")  # no tone at zero

    rent = figure(row, "rent")
    assert sub_of(rent) is None and value_of(rent) == ("kpi2-value", "—")
    assert link_of(rent) is None and "/leases/" not in rent

    autopay = figure(row, "autopay")
    assert sub_of(autopay) is None
    assert value_of(autopay) == ("kpi2-value", "Not set up")
    assert link_of(autopay) == (f"{store_url}?tab=schedules#tab-schedules", "Autopay")


def test_a_paid_up_store_shows_zero_without_the_red_tone() -> None:
    lease = a_lease(a_store("Paid", 2002), an_operator("Paid Up LLC"), debit_day=5, rent="1500.00")
    a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PAID, "1500.00", "1500.00", day=5)
    row = figure_row(page(client_for(Role.BILLING)[0], f"/stores/{lease.store_id}/"))
    balance = figure(row, "open-balance")
    assert value_of(balance) == ("kpi2-value", "$0.00") and sub_of(balance) is None
    assert sub_of(figure(row, "rent")) == "Debit day 5th"


def test_a_viewer_sees_the_figures_and_only_pages_a_viewer_reads() -> None:
    lease = a_store_world()
    client = client_for(Role.VIEWER)[0]
    store_url = f"/stores/{lease.store_id}/"
    row = figure_row(page(client, store_url))
    assert titles(row) == ["Open balance", "Rent", "Autopay"]
    hrefs = re.findall(r'<a class="kpi2-link" href="([^"]+)"', row)
    assert hrefs == [
        f"{store_url}?tab=charges#tab-charges",
        f"/leases/{lease.pk}/",
        f"{store_url}?tab=schedules#tab-schedules",
    ]
    for href in hrefs:
        page(client, href.split("#")[0])  # each one opens for the viewer (200)


def test_the_figure_row_shows_at_every_width_and_the_strip_css_is_gone() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".summary-strip" not in css and ".summary-cell" not in css
    assert ".page-header-inner > .kpi2-row { flex: 1 0 100%; margin: 0; }" in css
    assert not re.search(r"\.kpi2-row[^{]*\{[^}]*display:\s*none", css)
    # The phone keeps the dashboard's rules: two cards a row (768 px and narrower), then as
    # many 10rem cards as fit under 720 px (the third goes under the first two at 390 px).
    assert ".kpi-row { grid-template-columns: repeat(2, minmax(0, 1fr));" in phone_block(css)
    narrow = css.split("@media (max-width: 719.98px) {", 1)[1].split("\n}\n", 1)[0]
    assert (
        ".kpi-row.kpi2-row { grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); }"
        in narrow
    )


# --- §3.2 The operator's figure row --------------------------------------------------------------


def a_busy_operator() -> tuple[Operator, list[str]]:
    """Fatih Düzenli's three stores: $2,000.00 open on 1991 (two charges), 1999 paid, 2475's
    September rent of $2,100.00 open after its debit was returned (one return this year), an
    open promise of $3,000.00 by Nov 1, 2026. Returns the operator and the store ids."""
    operator, (first, second, third) = three_stores()
    a_rent_charge(first, date(2026, 9, 1), due="1500.00", day=1)
    a_rent_charge(first, date(2026, 10, 1), due="500.00", day=1)
    a_rent_charge(second, date(2026, 10, 1), RentChargeStatus.PAID, "1500.00", "1500.00", day=1)
    bounced = a_rent_charge(third, date(2026, 9, 1), due="2100.00", day=1)
    a_payment(bounced, "2100.00", None, status=PaymentStatus.RETURNED)
    bounced.payments.update(returned_at=datetime(2026, 9, 3, 14, 0, tzinfo=UTC))
    an_entry(
        operator,
        ContactKind.PROMISE,
        date(2026, 10, 2),
        "Will pay",
        lease=first,
        promised_date=date(2026, 11, 1),
        promised_amount=Decimal("3000.00"),
    )
    return operator, [str(lease.store_id) for lease in (first, second, third)]


@pytest.mark.parametrize("selected", [None, 1])
def test_the_operator_figure_row_has_its_four_cards_with_or_without_a_store(
    selected: int | None,
) -> None:
    operator, stores = a_busy_operator()
    query = {"store": stores[selected]} if selected is not None else {}
    path = operator_page(operator, **query)
    html = page(client_for(Role.BILLING)[0], path)
    header = header_of(html)
    assert header.count('id="summary-strip"') == 1
    assert header.index('id="operator-actions"') < header.index('id="summary-strip"')
    assert header.index('id="summary-strip"') < header.index('id="operator-tabs-row"')
    row = figure_row(html)
    assert titles(row) == ["Open balance", "Stores", "Returns this year", "Promise"]
    url = f"/operators/{operator.pk}/"
    # The links keep the selected store, as the tab links do (the & escaped in the attribute).
    keep = f"store={stores[selected]}&amp;" if selected is not None else ""

    balance = figure(row, "open-balance")
    assert icon_of(balance) == "alert"
    assert sub_of(balance) == "3 open charges"
    assert value_of(balance) == ("kpi2-value tone-unpaid", "$4,100.00")  # every store's
    assert link_of(balance) == (f"{url}?{keep}tab=charges#tab-charges", "Open charges")

    count = figure(row, "stores")
    assert icon_of(count) == "stores" and sub_of(count) is None
    assert value_of(count) == ("kpi2-value", "3")
    assert link_of(count) is None  # the stores list has no operator filter

    returns = figure(row, "returns")
    assert value_of(returns) == ("kpi2-value tone-unpaid", "1")
    assert link_of(returns) == (f"{url}?{keep}tab=payments#tab-payments", "Payments")

    promise = figure(row, "promise")
    assert icon_of(promise) == "handshake"
    assert value_of(promise) == ("kpi2-value", "$3,000.00 by Nov 1, 2026")
    assert '<span class="kpi2-pill kpi2-pill-pending">Open</span>' in promise
    assert link_of(promise) == (f"{url}?{keep}tab=charges#contacts", "Notes & follow-ups")


def test_an_operator_without_a_store_has_zeros_none_and_no_tiles() -> None:
    operator = an_operator("Nobody")
    html = page(client_for(Role.VIEWER)[0], operator_page(operator))
    row = figure_row(html)
    assert titles(row) == ["Open balance", "Stores", "Returns this year", "Promise"]
    balance = figure(row, "open-balance")
    assert value_of(balance) == ("kpi2-value", "$0.00") and sub_of(balance) is None
    assert value_of(figure(row, "stores")) == ("kpi2-value", "0")
    assert value_of(figure(row, "returns")) == ("kpi2-value", "0")
    promise = figure(row, "promise")
    assert value_of(promise) == ("kpi2-value", "None") and "kpi2-pill" not in promise
    assert 'id="store-strip"' not in html and 'id="no-stores"' in html


def test_a_viewer_sees_the_operator_figures_and_their_links_open() -> None:
    operator, _ = a_busy_operator()
    client = client_for(Role.VIEWER)[0]
    row = figure_row(page(client, operator_page(operator)))
    hrefs = re.findall(r'<a class="kpi2-link" href="([^"]+)"', row)
    assert len(hrefs) == 3
    for href in hrefs:
        page(client, href.split("#")[0])


# --- §3.4 The store tiles -----------------------------------------------------------------------


def tiles_of(html: str) -> dict[str, str]:
    strip = by_id(html, "store-strip", "ul")
    assert strip.startswith('<ul class="store-tiles" id="store-strip"')
    return {
        found.group(1): found.group(0)
        for found in re.finditer(r'<li id="store-card-([0-9a-f-]+)".*?</li>', strip, re.S)
    }


def chip_of(tile: str) -> tuple[str, str, str]:
    """(the tone, the glyph, the words) of the tile's one chip."""
    found = re.findall(
        r'<span class="status-chip status-chip-([a-z]+)"><svg class="icon icon-([a-z-]+)"'
        r"[^>]*>.*?</svg><span>([^<]+)</span></span>",
        tile,
        re.S,
    )
    assert len(found) == 1, tile
    return found[0]


def test_each_tile_is_one_link_with_the_chip_of_its_balance() -> None:
    operator, (first, second, third) = three_stores()
    a_rent_charge(first, date(2026, 10, 1), due="2000.00", day=1)  # past due on Oct 6
    a_rent_charge(second, date(2026, 10, 1), due="1500.00", day=20)  # due Oct 20: open
    tiles = tiles_of(page(client_for(Role.VIEWER)[0], operator_page(operator)))
    assert list(tiles) == [str(lease.store_id) for lease in (first, second, third)]
    for lease in (first, second, third):
        tile = tiles[str(lease.store_id)]
        assert tile.count("<a ") == 1  # no link inside the link
        assert f'<a class="store-tile tile-v2" href="/stores/{lease.store_id}/">' in tile
    assert chip_of(tiles[str(first.store_id)]) == ("unpaid", "dot", "Overdue")
    assert chip_of(tiles[str(second.store_id)]) == ("pending", "dot", "Open balance")
    assert chip_of(tiles[str(third.store_id)]) == ("paid", "check", "Paid up")
    assert '<span class="store-tile-balance">$0.00</span>' in tiles[str(third.store_id)]


def test_only_the_selected_stores_tile_is_current() -> None:
    operator, leases = three_stores()
    selected = str(leases[1].store_id)
    tiles = tiles_of(page(client_for(Role.VIEWER)[0], operator_page(operator, store=selected)))
    current = [key for key, tile in tiles.items() if 'aria-current="true"' in tile.split(">")[0]]
    assert current == [selected]


def test_a_store_title_with_markup_is_escaped_in_its_tile() -> None:
    operator = an_operator("Markup Stores")
    lease = a_lease(a_store(f"<script>alert(1)</script>{MARKUP}"), operator, debit_day=1)
    html = page(client_for(Role.ADMIN)[0], operator_page(operator))
    tile = tiles_of(html)[str(lease.store_id)]
    assert "<script>alert" not in html and MARKUP not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;img" in tile


def test_the_tile_css_is_the_dashboards_one_class_and_the_old_strip_is_gone() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".operator-tile {" not in css  # moved to .tile-v2, which both tiles carry
    assert ".tile-v2 { display: flex; flex-direction: column;" in css
    assert len(re.findall(r"^\.tile-v2 \{", css, re.M)) == 1
    for gone in (".store-strip", ".store-card"):
        assert gone not in css, gone
    assert (
        ".store-tiles { display: grid; grid-template-columns: "
        "repeat(auto-fill, minmax(13rem, 1fr));" in css
    )
    assert '.store-tiles > li[aria-current="true"] > .tile-v2 { border-color: var(--teal); }' in css
    assert ".workspace-main > .store-tiles { grid-template-columns: minmax(0, 1fr);" in phone_block(
        css
    )
