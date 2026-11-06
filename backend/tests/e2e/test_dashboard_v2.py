"""PLAN-015 Batch 1 in a browser (Chromium; WebKit in the CI image only): the Collections bar's
hover tooltip reads its month, Collected and Due; Monthly → Yearly; the Recent activity feed pages
and searches; at 390 px the KPI cards sit two per row, the operator tiles one per row and the
feed's rows are cards. The QA shots (``QA_SHOTS_DIR``) come from here."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error,
    Page,
    Playwright,
    ViewportSize,
    expect,
)
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from accounts.themes import set_theme
from ledger.models import Payment, PaymentKind, PaymentMethod, PaymentStatus, RentChargeStatus
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 900}
PHONE: ViewportSize = {"width": 390, "height": 844}
BROWSERS = [
    "chromium",
    pytest.param(
        "webkit",
        marks=pytest.mark.skipif(
            os.environ.get("E2E_WEBKIT") != "1", reason="WebKit e2e runs in the CI image"
        ),
    ),
]


@pytest.fixture(params=BROWSERS)
def any_browser(request: pytest.FixtureRequest, playwright: Playwright) -> Iterator[Browser]:
    name: str = request.param
    try:
        browser = getattr(playwright, name).launch()
    except Error as error:
        pytest.skip(f"{name} is not installed (uv run playwright install {name}): {error}")
    yield browser
    browser.close()


@pytest.fixture
def laptop(any_browser: Browser) -> Iterator[BrowserContext]:
    context = any_browser.new_context(viewport=LAPTOP)
    yield context
    context.close()


@pytest.fixture
def phone(any_browser: Browser) -> Iterator[BrowserContext]:
    context = any_browser.new_context(
        viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    yield context
    context.close()


def shot(page: Page, name: str) -> None:
    folder = os.environ.get("QA_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)


def signed_in(context: BrowserContext, live_server: LiveServer, username: str) -> Page:
    user, device = make_user(username, Role.BILLING)
    assert device is not None
    set_theme(user, "dark")
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


def a_world() -> date:
    """Seven payments of the last seven days on two stores (the feed's two pages), this month's
    rent of three operators, one of them overdue since last month. Returns today."""
    today = date.today()
    this_month = today.replace(day=1)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    north = a_lease(a_store("0101 MAPLE AVENUE", 101), an_operator("Northwind Stores"))
    harbor = a_lease(a_store("0114 CEDAR STREET", 114), an_operator("Harbor Lane Retail"))
    pine = a_lease(a_store("0132 BIRCH LANE", 132), an_operator("Pinecrest Markets"))
    a_rent_charge(harbor, last_month, RentChargeStatus.UNPAID, "2450.00", day=1)
    a_rent_charge(pine, this_month, RentChargeStatus.UNPAID, "800.00", day=1)
    for n in range(7):
        lease = north if n % 2 == 0 else pine
        month = date(2025, n + 1, 1)
        charge = a_rent_charge(lease, month, RentChargeStatus.PAID, "100.00", "100.00", day=1)
        Payment.objects.create(
            rent_charge=charge,
            kind=PaymentKind.RENT,
            method=PaymentMethod.ACH,
            amount=Decimal(f"{100 + n}.00"),
            status=PaymentStatus.SETTLED,
            initiated_on=today - timedelta(days=n),
            settled_on=today - timedelta(days=n),
        )
    return today


def test_hovering_a_bar_shows_its_month_collected_and_due(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    a_world()
    page = signed_in(laptop, live_server, "e2e-v2-hover")
    tooltip = page.locator("#collections-tooltip")
    assert not tooltip.is_visible()
    current = page.locator("#collections-chart .collection-bar.is-current")
    label = current.get_attribute("data-label") or ""
    collected = current.get_attribute("data-collected") or ""
    due = current.get_attribute("data-due") or ""
    assert label and collected.startswith("$") and due.startswith("$")
    current.locator("rect").hover()
    expect(tooltip).to_be_visible()
    text = " ".join(tooltip.inner_text().split())
    assert text == f"{label} Collected {collected} Due {due}", text
    shot(page, "dashboard-v2-tooltip-dark-laptop")
    page.mouse.move(5, 5)
    expect(tooltip).to_be_hidden()


def test_monthly_switches_to_yearly_and_back(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    today = a_world()
    page = signed_in(laptop, live_server, "e2e-v2-yearly")
    bars = page.locator("#collections-chart .collection-bar")
    assert bars.count() == today.month
    page.locator("#chart-yearly").click()
    page.wait_for_url(lambda url: "chart=yearly" in url)
    assert bars.count() == 5
    assert page.locator("#chart-yearly").get_attribute("aria-current") == "page"
    assert "Current year" in page.locator("#collections-legend").inner_text()
    page.locator("#chart-monthly").click()
    page.wait_for_url(lambda url: "chart=yearly" not in url)
    assert bars.count() == today.month


def test_the_feed_pages_and_searches(live_server: LiveServer, laptop: BrowserContext) -> None:
    a_world()
    page = signed_in(laptop, live_server, "e2e-v2-feed")
    rows = page.locator("#activity-table tbody tr.feed-row")
    counts = page.locator("#activity-card .pager-count")
    assert rows.count() == 5 and counts.inner_text() == "1–5 of 7"
    # At 1280 the feed is a six-column table: each entry one row, its six cells on one line
    # under the six visible headers (Fable's gate run 2).
    assert page.locator("#activity-table thead th:visible").count() == 6
    for index in range(rows.count()):
        cells = rows.nth(index).locator("td")
        assert cells.count() == 6
        tops = {round((cells.nth(n).bounding_box() or {"y": -1})["y"]) for n in range(6)}
        assert len(tops) == 1, tops
        assert (
            page.evaluate("row => getComputedStyle(row).display", rows.nth(index).element_handle())
            == "table-row"
        )
    page.locator("#activity-card a[rel=next]").click()
    page.wait_for_url(lambda url: "page=2" in url)
    assert rows.count() == 2 and counts.inner_text() == "6–7 of 7"
    page.locator("#activity-q").fill("birch")
    page.locator("#activity-q").press("Enter")
    page.wait_for_url(lambda url: "q=birch" in url)
    assert counts.inner_text() == "1–3 of 3"
    assert all("#132 0132 BIRCH LANE" in text for text in rows.all_inner_texts())
    page.locator("#activity-q").fill("$104.00")
    page.locator("#activity-q").press("Enter")
    page.wait_for_url(lambda url: "q=%24104.00" in url)
    assert rows.count() == 1 and "$104.00" in rows.first.inner_text()
    page.select_option("#activity-kind", "attention")
    page.wait_for_url(lambda url: "kind=attention" in url)
    assert page.locator("#activity-kind").input_value() == "attention"
    shot(page, "dashboard-v2-feed-dark-laptop")


def test_the_phone_layout_two_kpis_a_row_one_tile_a_row_and_feed_cards(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    a_world()
    page = signed_in(phone, live_server, "e2e-v2-phone")
    boxes = [
        page.locator(f"#{name}").bounding_box()
        for name in ("figure-due", "figure-collected", "figure-overdue", "figure-bank")
    ]
    assert all(boxes)
    due, collected, overdue, bank = (box for box in boxes if box)
    assert abs(due["y"] - collected["y"]) < 2 and collected["x"] > due["x"]
    assert abs(overdue["y"] - bank["y"]) < 2 and overdue["y"] > due["y"] + due["height"] - 2
    # The period control: one row of three segments, left-aligned under the title.
    segments = page.locator("#dashboard-actions .period-control button")
    assert segments.count() == 3
    seg_boxes = [segments.nth(n).bounding_box() for n in range(3)]
    assert all(seg_boxes)
    assert len({round(box["y"]) for box in seg_boxes if box}) == 1, seg_boxes
    assert sum(box["width"] for box in seg_boxes if box) < PHONE["width"] - 32
    # No money figure wraps inside itself (its box at most 1.5 lines of its font size).
    figures = page.locator(".kpi2-value")
    assert figures.count() == 4
    for n in range(4):
        box = figures.nth(n).bounding_box()
        size = float(
            page.evaluate(
                "el => parseFloat(getComputedStyle(el).fontSize)", figures.nth(n).element_handle()
            )
        )
        assert box and box["height"] <= 1.5 * size, (n, box, size)
    tiles = page.locator("#operator-tiles > li")
    assert tiles.count() >= 2
    first, second = tiles.nth(0).bounding_box(), tiles.nth(1).bounding_box()
    assert first and second and second["y"] >= first["y"] + first["height"] - 1
    assert abs(first["x"] - second["x"]) < 2
    row = page.locator("#activity-table tbody tr.feed-row").first
    row_box = row.bounding_box()
    assert row_box and row_box["width"] <= PHONE["width"]
    assert page.locator("#activity-table thead").is_hidden()  # the lists' phone cards
    assert page.evaluate("row => getComputedStyle(row).display", row.element_handle()) == "flex"
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= PHONE["width"], width  # no sideways page scroll
    calendar = page.locator("#mini-calendar").bounding_box()
    feed = page.locator("#activity-card").bounding_box()
    assert calendar and feed and calendar["y"] >= feed["y"] + feed["height"] - 2
    shot(page, "dashboard-v2-dark-phone")
