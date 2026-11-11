"""PLAN-015 Batch 3 (docs/qa/15/PLAN-015-batch-3-workspaces.md): the store and operator
workspaces in the dashboard's language — the figure row of KPI cards v2 under the action bar on
both pages (§3.1, §3.2) in place of the phone's summary strip, every side card's head in the
card-head shape (§3.3) and the operator's stores as tiles in the dashboard's operator-tile style
(§3.4)."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import RentChargeStatus
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_plan012f_d_pages import APP_CSS, phone_block
from tests.test_plan012f_pages import a_store_world

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
