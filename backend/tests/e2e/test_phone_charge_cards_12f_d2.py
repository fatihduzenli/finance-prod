"""PLAN-012f D-2 in a browser (docs/qa/PLAN-012f-approved-design.md D-2; DESIGN-SPEC §6,
store-workspace-phone.png), by the QA owner.

At 390 x 844 a charge row of the store page's Charges card is a two-line card: the title and
the amount on the first line, the due date and the status badge on the second, the action
button full width as the last line; Paid and Balance are not shown unless the row has both
(then a third line); the table's header row is not visible. At 1,280 x 800 the same table
shows every column with its header.
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


def test_at_390_a_charge_row_is_the_two_line_card(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    path = a_store_with_an_unpaid_and_a_partial_charge()
    page = signed_in(phone, live_server, "phone")
    page.goto(f"{live_server.url}{path}")
    table = page.locator("table.charges-table")
    table.wait_for()
    assert not table.locator("thead").is_visible()

    unpaid = table.locator("tbody tr", has_text="Rent 2026-09")
    partial = table.locator("tbody tr", has_text="Rent 2026-08")
    assert unpaid.count() == 1 and partial.count() == 1

    row = box(unpaid)
    title = box(unpaid.locator(".charge-cell-title .cell-title"))
    amount = box(unpaid.locator(".charge-cell-amount"))
    due = box(unpaid.locator(".charge-cell-due"))
    badge = box(unpaid.locator(".charge-cell-status .badge"))
    button = box(unpaid.locator(".charge-cell-actions .btn").first)
    # Line 1: the title at the left, the amount at the right, on the same line.
    assert abs(title["y"] - amount["y"]) <= 8, (title, amount)
    assert title["x"] < amount["x"]
    assert amount["x"] + amount["width"] >= row["x"] + row["width"] - 40, (amount, row)
    # Line 2: the due date and the badge, below line 1, on the same line.
    assert due["y"] >= title["y"] + title["height"] - 2, (due, title)
    assert abs(due["y"] - badge["y"]) <= 10, (due, badge)
    assert due["x"] < badge["x"]
    # Paid and Balance are not shown on a row without a payment.
    assert not unpaid.locator(".charge-cell-paid").is_visible()
    assert not unpaid.locator(".charge-cell-balance").is_visible()
    # The action button is the last line and spans the card.
    assert button["y"] >= due["y"] + due["height"] - 2, (button, due)
    assert button["width"] >= 0.85 * row["width"], (button, row)
    assert button["height"] >= 40

    # The partial row shows its payment and its balance on a third line, below the due line.
    paid = box(partial.locator(".charge-cell-paid"))
    balance = box(partial.locator(".charge-cell-balance"))
    partial_due = box(partial.locator(".charge-cell-due"))
    assert paid["y"] >= partial_due["y"] + partial_due["height"] - 2, (paid, partial_due)
    assert abs(paid["y"] - balance["y"]) <= 10, (paid, balance)
    text = " ".join(partial.inner_text().split())
    assert "$500.00" in text and "$1,500.00" in text and "Partially paid" in text

    # Nothing scrolls sideways: the card fits the viewport.
    assert row["x"] + row["width"] <= PHONE["width"] + 1, row
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def test_at_1280_the_table_keeps_every_column_and_its_header(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    path = a_store_with_an_unpaid_and_a_partial_charge()
    page = signed_in(laptop, live_server, "laptop")
    page.goto(f"{live_server.url}{path}")
    table = page.locator("table.charges-table")
    table.wait_for()
    assert table.locator("thead").is_visible()
    unpaid = table.locator("tbody tr", has_text="Rent 2026-09")
    for cell in (
        ".charge-cell-title",
        ".charge-cell-due",
        ".charge-cell-status",
        ".charge-cell-amount",
        ".charge-cell-paid",
        ".charge-cell-balance",
    ):
        assert unpaid.locator(cell).is_visible(), cell
    title = box(unpaid.locator(".charge-cell-title .cell-title"))
    # One table row: every cell starts on the row's line (a cell that became a block, as the
    # first D-2 build's clashing class names made the amount cell, would sit below it).
    row_top = box(unpaid)["y"]
    for cell in (
        ".charge-cell-due",
        ".charge-cell-status",
        ".charge-cell-amount",
        ".charge-cell-paid",
        ".charge-cell-balance",
        ".charge-cell-actions",
    ):
        found = box(unpaid.locator(cell))
        assert abs(found["y"] - row_top) <= 2, (cell, found, row_top)
    assert abs(title["y"] - row_top) <= 20
    amount = box(unpaid.locator(".charge-cell-amount"))
    assert amount["x"] > box(unpaid.locator(".charge-cell-status"))["x"]
    header = [" ".join(th.split()) for th in table.locator("thead th").all_inner_texts()]
    assert header[:6] == ["Charge", "Due", "Status", "Amount", "Paid", "Balance"], header
