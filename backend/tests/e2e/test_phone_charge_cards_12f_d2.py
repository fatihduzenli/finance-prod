"""The phone's two-line table card in a browser (PLAN-012f D-2, generalised by PLAN-012f-a B3;
DESIGN-SPEC §6, store-workspace-phone.png), by the QA owner.

At 390 x 844 a charge row of the store page's Charges card is a two-line card: the title and
the amount on the first line, the due date (a meta cell) and the status badge on the second,
the action button full width as the last line; Paid and Balance are not shown unless the row
has both (then they join the meta line); the table's header row is not visible. The stores list
and the dashboard's Recent payments read the same way (one table of each family). At 1,280 x 800
the Charges table shows every column with its header, every cell on the row's line.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, FloatRect, Locator, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import PaymentChannel, RentChargeStatus
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

PHONE: ViewportSize = {"width": 390, "height": 844}
LAPTOP: ViewportSize = {"width": 1280, "height": 800}


@pytest.fixture
def phone(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(
        viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    yield context
    context.close()


@pytest.fixture
def laptop(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport=LAPTOP)
    yield context
    context.close()


def box(locator: Locator) -> FloatRect:
    found = locator.bounding_box()
    assert found is not None, locator
    return found


def signed_in(context: BrowserContext, live_server: LiveServer, suffix: str) -> Page:
    username = f"e2e-12fd2-{suffix}"
    _, device = make_user(username, Role.BILLING)
    assert device is not None
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


def a_store_with_an_unpaid_and_a_partial_charge() -> str:
    lease = a_lease(
        a_store("3201"),
        an_operator("Phone Cards Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    a_rent_charge(
        lease, date(2026, 8, 1), status=RentChargeStatus.PARTIAL, due="2000.00", paid="500.00"
    )
    return reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) + "?tab=charges"


def row_named(table: Locator, iso: str, words: str) -> Locator:
    """The row of "Rent 2026-09" today and "Rent Sep 2026" once the dates change (B4)."""
    found = table.locator("tbody tr", has_text=f"Rent {iso}")
    return found if found.count() else table.locator("tbody tr", has_text=f"Rent {words}")


def two_line_card(row: Locator, page: Page) -> None:
    """The geometry every role card shares at 390: title left and amount right on line 1,
    the meta cells and the badge below, actions (if any) last and full width; the card fits."""
    frame = box(row)
    title = box(row.locator('td[data-role="title"]'))
    amount = row.locator('td[data-role="amount"]')
    if amount.count():
        amount_box = box(amount)
        assert abs(title["y"] - amount_box["y"]) <= 8, (title, amount_box)
        assert title["x"] < amount_box["x"]
        assert amount_box["x"] + amount_box["width"] >= frame["x"] + frame["width"] - 40
    meta = row.locator('td[data-role="meta"]').first
    if meta.count():
        meta_box = box(meta)
        assert meta_box["y"] >= title["y"] + title["height"] - 2, (meta_box, title)
    status = row.locator('td[data-role="status"] .badge')
    if status.count():
        status_box = box(status)
        assert status_box["y"] >= title["y"] + title["height"] - 2
        balance = row.locator('td[data-role="meta-badge"] .badge')
        if balance.count():
            # PLAN-013a C6: the two badges head line 2 together; the meta cells follow on or
            # below that line (the stores list).
            assert abs(status_box["y"] - box(balance)["y"]) <= 2, (status_box, box(balance))
            if meta.count():
                assert box(meta)["y"] >= status_box["y"] - 2
        elif meta.count():
            assert abs(status_box["y"] - box(meta)["y"]) <= 14
    actions = row.locator('td[data-role="actions"] .btn')
    if actions.count():
        button = box(actions.first)
        assert button["width"] >= 0.85 * frame["width"], (button, frame)
        assert button["height"] >= 40
        assert button["y"] >= title["y"] + title["height"]
    assert frame["x"] + frame["width"] <= PHONE["width"] + 1, frame
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def test_at_390_a_charge_row_is_the_two_line_card(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    path = a_store_with_an_unpaid_and_a_partial_charge()
    page = signed_in(phone, live_server, "phone")
    page.goto(f"{live_server.url}{path}")
    table = page.locator("table#store-charges")
    table.wait_for()
    assert not table.locator("thead").is_visible()
    unpaid = row_named(table, "2026-09", "Sep 2026")
    partial = row_named(table, "2026-08", "Aug 2026")
    assert unpaid.count() == 1 and partial.count() == 1
    two_line_card(unpaid, page)
    # Paid and Balance are not shown on a row without a payment, shown on a partial one.
    for cell in unpaid.locator('td[data-role="meta-if-partial"]').all():
        assert not cell.is_visible()
    shown = [
        cell
        for cell in partial.locator('td[data-role="meta-if-partial"]').all()
        if cell.is_visible()
    ]
    assert len(shown) == 2
    text = " ".join(partial.inner_text().split())
    assert "$500.00" in text and "$1,500.00" in text and "Partially paid" in text


def test_at_390_the_stores_list_and_the_recent_payments_read_as_cards(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    a_store_with_an_unpaid_and_a_partial_charge()
    page = signed_in(phone, live_server, "lists")
    page.goto(f"{live_server.url}/stores/")
    rows = page.locator('.table-cards table tbody tr:has(td[data-role="title"])')
    rows.first.wait_for()
    two_line_card(rows.first, page)
    page.goto(f"{live_server.url}/")
    recent = page.locator('#recent-payments-table tbody tr:has(td[data-role="title"])')
    if recent.count():
        two_line_card(recent.first, page)


def test_at_1280_the_table_keeps_every_column_and_its_header(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    path = a_store_with_an_unpaid_and_a_partial_charge()
    page = signed_in(laptop, live_server, "laptop")
    page.goto(f"{live_server.url}{path}")
    table = page.locator("table#store-charges")
    table.wait_for()
    assert table.locator("thead").is_visible()
    unpaid = row_named(table, "2026-09", "Sep 2026")
    row_top = box(unpaid)["y"]
    cells = unpaid.locator("td").all()
    assert len(cells) == 7
    for cell in cells:
        assert cell.is_visible()
        found = box(cell)
        assert abs(found["y"] - row_top) <= 2, (cell, found, row_top)
    header = [" ".join(th.split()) for th in table.locator("thead th").all_inner_texts()]
    assert header[:6] == ["Charge", "Due", "Status", "Amount", "Paid", "Balance"], header
