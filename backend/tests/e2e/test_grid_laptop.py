"""The month grid on a laptop (docs/qa/PLAN-011b D2, §7; PLAN-012f D3): at 1,280 x 800 the
twelve month columns (80 px each) and the store column (220 px) are 1,180 px wide; beside the
232 px sidebar the grid's region is 984 px wide (1,280 - 232 - 2 x 32), so the first months
show and the later ones are reached by scrolling the region, never the page. The grid's
scrollbar never hides, the mirrored bar above it shows and moves with it, and the store column
stays where it was when the grid is scrolled."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.utils import timezone
from playwright.sync_api import Browser, BrowserContext
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from tests.auth_helpers import current_code, make_user
from tests.e2e.test_mobile import a_year_of_charges

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

WIDTH, HEIGHT = 1280, 800


@pytest.fixture
def laptop(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT})
    yield context
    context.close()


def test_twelve_months_are_reachable_with_the_store_column_fixed(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    from tests.auth_helpers import TEST_PASSWORD

    _, device = make_user("e2e-laptop", Role.VIEWER)
    assert device is not None
    label = a_year_of_charges()
    page = laptop.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", "e2e-laptop")
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    page.goto(f"{live_server.url}/grid/")
    page.wait_for_load_state("load")

    region = page.locator("#grid-scroll")
    months = page.locator("thead th.month-col")
    assert months.count() == 12
    store_header = page.locator("thead th.store-col")
    header_box = store_header.bounding_box()
    assert header_box is not None and round(header_box["width"]) == 220
    month_box = months.first.bounding_box()
    assert month_box is not None and round(month_box["width"]) == 80
    # Twelve months and the store column are 1,180 px wide; the region beside the sidebar is
    # 984 px (PLAN-012f D3): the table is wider than its region, which scrolls.
    table_width = page.locator("table.grid").evaluate("el => el.getBoundingClientRect().width")
    assert table_width <= 220 + 12 * 80 + 2
    region_width = region.evaluate("el => el.getBoundingClientRect().width")
    assert region_width == pytest.approx(1280 - 232 - 2 * 32, abs=1)
    assert table_width > region.evaluate("el => el.clientWidth")
    # The bar never hides: overflow-x is "scroll" (headless Chromium draws no scrollbar,
    # so its height cannot be measured here).
    assert region.evaluate("el => getComputedStyle(el).overflowX") == "scroll"
    assert page.evaluate("document.documentElement.scrollWidth") <= WIDTH  # no page scroll

    store_cell = page.locator("tr.grid-row th.store-col").first
    before = store_cell.bounding_box()
    assert before is not None and label in store_cell.inner_text()

    # Before any scrolling, the first month column is wholly inside the visible region and the
    # 12th is past its right edge.
    assert region.evaluate("el => el.scrollLeft") == 0
    region_box = region.bounding_box()
    first, twelfth = months.nth(0).bounding_box(), months.nth(11).bounding_box()
    assert region_box is not None and first is not None and twelfth is not None
    assert first["x"] >= before["x"] + before["width"] - 1
    assert first["x"] + first["width"] <= region_box["x"] + region_box["width"] + 1
    assert twelfth["x"] + twelfth["width"] > region_box["x"] + region_box["width"]

    # Scroll to the last month column through the mirrored bar, which shows as the table is
    # wider than its region.
    top = page.locator("#grid-scroll-top")
    assert top.is_visible()
    top.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    page.wait_for_function("() => document.getElementById('grid-scroll').scrollLeft > 0")
    region.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    last_month = f"{timezone.localdate():%Y-%m}"
    last = page.locator(f'tr.grid-row td[data-month="{last_month}"]')
    last_box = last.bounding_box()
    region_box = region.bounding_box()
    assert last_box is not None and region_box is not None
    assert last_box["x"] + last_box["width"] <= region_box["x"] + region_box["width"] + 1
    assert last_box["x"] >= before["x"] + before["width"] - 1  # not under the store column
    after = store_cell.bounding_box()
    assert after is not None and abs(after["x"] - before["x"]) < 1
    assert abs(top.evaluate("el => el.scrollLeft") - region.evaluate("el => el.scrollLeft")) <= 1
