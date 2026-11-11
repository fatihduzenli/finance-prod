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
from ledger.models import (
    CollectionStatus,
    ContactKind,
    Lease,
    Operator,
    PaymentStatus,
    RentChargeStatus,
)
from ledger.services import store_costs
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
    # PLAN-015 Batch 5 §9.1: the Autopay card's "Set up" link carries its id.
    found = re.search(
        r'<a class="kpi2-link" href="([^"]+)"(?: id="[a-z-]+")?><span>([^<]+)</span>', card
    )
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
    # The figure is an amount, never a sentence: the words go to the sub-line.
    assert sub_of(rent) == "per month · debit day 1st"
    assert value_of(rent) == ("kpi2-value", "$2,000.00")
    assert link_of(rent) == (f"/leases/{lease.pk}/", "Lease")

    autopay = figure(row, "autopay")
    assert icon_of(autopay) == "calendar"
    assert sub_of(autopay) == "Next debit Nov 1, 2026"  # the NEXT card's rent debit
    assert value_of(autopay) == ("kpi2-value", "Not set up")
    # PLAN-015 Batch 5 §9.1 (re-pinned): the LEASE card's "Set up" link moved here.
    assert link_of(autopay) == (f"{store_url}?tab=schedules#tab-schedules", "Set up")
    assert 'id="autopay-set-up"' in autopay


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
    # PLAN-015 Batch 5 §9.1 (re-pinned): no lease, nothing to set up: "Manage".
    assert link_of(autopay) == (f"{store_url}?tab=schedules#tab-schedules", "Manage")
    assert 'id="autopay-set-up"' not in autopay


def test_a_paid_up_store_shows_zero_without_the_red_tone() -> None:
    lease = a_lease(a_store("Paid", 2002), an_operator("Paid Up LLC"), debit_day=5, rent="1500.00")
    a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.PAID, "1500.00", "1500.00", day=5)
    row = figure_row(page(client_for(Role.BILLING)[0], f"/stores/{lease.store_id}/"))
    balance = figure(row, "open-balance")
    assert value_of(balance) == ("kpi2-value", "$0.00") and sub_of(balance) is None
    assert sub_of(figure(row, "rent")) == "per month · debit day 5th"


def test_the_rent_sub_line_names_both_debit_days_or_none() -> None:
    twice = a_lease(
        a_store("Twice", 2003), an_operator("Twice LLC"), debit_day=1, second_debit_day=15
    )
    rent = figure(
        figure_row(page(client_for(Role.BILLING)[0], f"/stores/{twice.store_id}/")), "rent"
    )
    assert value_of(rent) == ("kpi2-value", "$3,500.00")
    assert sub_of(rent) == "per month · debit day 1st and 15th"

    # A paused lease may have no debit day (an active one must: leases_active_has_day_and_operator).
    dayless = a_lease(
        a_store("Dayless", 2004), an_operator("Dayless LLC"), status=CollectionStatus.PAUSED
    )
    Lease.objects.filter(pk=dayless.pk).update(debit_day=None)
    rent = figure(
        figure_row(page(client_for(Role.BILLING)[0], f"/stores/{dayless.store_id}/")), "rent"
    )
    assert value_of(rent) == ("kpi2-value", "$3,500.00")
    assert sub_of(rent) == "per month"


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
    # A lone last card of a workspace's row (the store page's Autopay) spans the row on a phone;
    # the operator's four cards are two rows of two. Scoped to the page header's row, so the
    # Payments totals (five cards, not in a page header) keep their grid.
    assert (
        ".page-header-inner > .kpi2-row > :last-child:nth-child(odd) { grid-column: 1 / -1; }"
        in phone_block(css)
    )
    assert css.count(":last-child:nth-child(odd)") == 1
    narrow = css.split("@media (max-width: 719.98px) {", 1)[1].split("\n}\n", 1)[0]
    assert (
        ".kpi-row.kpi2-row { grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); }"
        in narrow
    )
    # A figure with words wraps between them on a phone, so the page never scrolls sideways
    # (the 390 px baselines keep their 390 px width).
    assert ".page-header-inner > .kpi2-row .kpi2-value { white-space: normal; }" in narrow
    assert narrow.index(".kpi2-value { font-size: 22px; white-space: nowrap;") < narrow.index(
        ".page-header-inner > .kpi2-row .kpi2-value"
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
    # The figure is an amount, never a sentence: the day goes to the sub-line.
    assert value_of(promise) == ("kpi2-value", "$3,000.00")
    assert sub_of(promise) == "by Nov 1, 2026"
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
    assert sub_of(promise) is None
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
    tone, glyph, words = found[0]
    return str(tone), str(glyph), str(words)


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


# --- §3.3 The side cards' heads -------------------------------------------------------------------

#: The side cards of the store page (and of the operator page's selected store): (the card's
#: id, its label, the head's link id or None).
STORE_SIDE = [
    ("lease-card", "Lease", None),
    ("notes-card", "Notes", "notes-card-all"),
    ("next-card", "Next", None),
    ("store-cost", "Store cost", "change-cost"),
    ("documents-card", "Documents", "documents-upload"),
]
OPERATOR_SIDE = [
    ("contact-card", "Contact", None),
    ("payment-card", "Payment", None),
    ("recent-contact-card", "Recent contact", "recent-add-contact"),
    ("portal-card", "Portal", None),
]


def head_of(html: str, card_id: str) -> str:
    """The card's first child, which must be its head: ``div.card-head`` up to its close."""
    opened = re.search(rf'<(?:div|section) class="card" id="{card_id}"[^>]*>\s*', html)
    assert opened, card_id
    rest = html[opened.end() :]
    assert rest.startswith('<div class="card-head"><h2 class="section-label"'), card_id
    return rest[: rest.index("</div>") + len("</div>")]


def assert_heads(html: str, cards: list[tuple[str, str, str | None]]) -> None:
    for card_id, label, link_id in cards:
        head = head_of(html, card_id)
        assert re.search(rf'<h2 class="section-label"( id="[a-z-]+")?>{label}</h2>', head), head
        links = re.findall(r'<a class="card-link"[^>]* id="([a-z-]+)"', head)
        assert links == ([link_id] if link_id else []), (card_id, head)
        if link_id:  # the link sits after the label, at the right
            assert head.index("</h2>") < head.index(f'id="{link_id}"')


def test_every_store_side_card_opens_with_its_head() -> None:
    from tests.test_notes_card import a_note

    lease = a_store_world()
    a_note(lease.store, "Called about the deposit.", minutes=1)
    assert store_costs.set_cost(lease.store, Decimal("1500.00"), date(2026, 1, 1)).ok
    html = page(client_for(Role.ADMIN)[0], f"/stores/{lease.store_id}/")
    side = html[html.index('<aside class="workspace-side"') :]
    assert_heads(side, STORE_SIDE)
    assert "card-header" not in side  # the side column's old header row is gone
    # The labelled-by ids stay on their h2.
    for card_id, heading in (
        ("notes-card", "notes-card-heading"),
        ("store-cost", "store-cost-heading"),
    ):
        assert f'id="{card_id}" aria-labelledby="{heading}"' in side
        assert f'<h2 class="section-label" id="{heading}">' in head_of(side, card_id)
    # The History details card is as it was, under STORE COST.
    assert '<div class="card" id="cost-history-card">\n  <details id="cost-history">' in side


def test_a_viewer_sees_the_heads_without_the_links_it_may_not_use() -> None:
    lease = a_store_world()
    html = page(client_for(Role.VIEWER)[0], f"/stores/{lease.store_id}/")
    side = html[html.index('<aside class="workspace-side"') :]
    assert_heads(side, [(card_id, label, None) for card_id, label, _ in STORE_SIDE])


def test_every_operator_side_card_and_the_selected_stores_open_with_their_heads() -> None:
    operator, leases = three_stores()
    html = page(client_for(Role.ADMIN)[0], operator_page(operator, store=str(leases[0].store_id)))
    side = html[html.index('<aside class="workspace-side operator-side"') :]
    assert_heads(side, OPERATOR_SIDE)
    for card_id, label, link_id in STORE_SIDE:
        if card_id == "notes-card":  # no note yet: no "All notes" link
            link_id = None
        assert_heads(side, [(card_id, label, link_id)])
    assert "card-header" not in side
    for card_id in ("contact-card", "payment-card", "recent-contact-card", "portal-card"):
        heading = re.search(rf'id="{card_id}" aria-labelledby="([a-z-]+)"', side)
        assert heading and f'id="{heading.group(1)}">' in head_of(side, card_id), card_id


def test_the_side_heads_take_the_old_header_rows_spacing() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".workspace-header .card-head { margin-bottom: var(--space-3); }" in css
    assert ".workspace-header .card-head .section-label { margin: 0; }" in css
    assert ".operator-side .card-head { margin-bottom: var(--space-3); }" in css
    assert ".operator-side .card-head .section-label { margin: 0; }" in css
    for gone in (".workspace-header .card-header", ".operator-side .card-header", ".note-list-all"):
        assert gone not in css, gone
