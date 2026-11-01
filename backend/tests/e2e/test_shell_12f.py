"""The approved design's shell in a browser (docs/qa/PLAN-012f-approved-design.md D3, D4, §6 A).

At 390 x 844 with touch: the bottom bar's four items are visible and at least 44 px each; More
opens its sheet, which offers Sign out as a POST form, and Escape closes it; the menu button
opens the navigation as a sheet and its close button shuts it; the search button reveals the
search field. At 1,280 x 800: the sidebar is 232 px wide, the content column takes the rest,
and the grid's twelve months are reached by scrolling its region (beside the sidebar they do
not all fit: the region, not the page, scrolls).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.utils import timezone
from playwright.sync_api import Browser, BrowserContext, Page
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.e2e.test_mobile import a_year_of_charges

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

TAP = 44


@pytest.fixture
def phone(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )
    yield context
    context.close()


@pytest.fixture
def laptop(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    yield context
    context.close()


def signed_in(context: BrowserContext, live_server: LiveServer, username: str) -> Page:
    _, device = make_user(username, Role.BILLING)
    assert device is not None
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    page.wait_for_load_state("load")
    return page


def test_the_bottom_bar_and_the_more_sheet_on_a_phone(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    page = signed_in(phone, live_server, "e2e-shell-phone")
    assert not page.locator("#sidebar").is_visible()
    assert page.locator("#phone-bar").is_visible()
    # The banner reads the short text under the bar.
    banner = page.locator("#sandbox-banner")
    assert banner.inner_text().strip() == "SANDBOX — TEST SYSTEM"
    bar_box = page.locator("#phone-bar").bounding_box()
    banner_box = banner.bounding_box()
    assert bar_box is not None and banner_box is not None
    assert round(bar_box["height"]) == 64 and bar_box["y"] == pytest.approx(0, abs=1)
    assert banner_box["y"] == pytest.approx(64, abs=1)
    for item in ("#tab-dashboard", "#tab-stores", "#tab-grid", "#tab-more"):
        box = page.locator(item).bounding_box()
        assert box is not None and page.locator(item).is_visible(), item
        assert box["height"] >= TAP and box["width"] >= TAP, (item, box)
        assert box["y"] + box["height"] <= 844 + 1, item
    assert page.locator("#tab-dashboard").get_attribute("aria-current") == "page"
    sign_out = page.locator("#more-sign-out")
    assert not sign_out.is_visible()
    page.locator("#tab-more").tap()
    sign_out.wait_for(state="visible")
    assert sign_out.evaluate("el => el.closest('form').method") == "post"
    box = sign_out.bounding_box()
    assert box is not None and box["height"] >= TAP
    page.keyboard.press("Escape")
    sign_out.wait_for(state="hidden")
    # The content clears the bar: the last of the page can scroll above it.
    padding = page.locator("#content").evaluate("el => getComputedStyle(el).paddingBottom")
    assert padding == "90px"
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_the_menu_sheet_and_the_search_field_on_a_phone(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    page = signed_in(phone, live_server, "e2e-shell-menu")
    grid_link = page.locator("#nav-grid")
    assert not grid_link.is_visible()
    menu = page.locator("#nav-menu")
    box = menu.bounding_box()
    assert box is not None and box["height"] >= TAP and box["width"] >= TAP
    menu.tap()
    grid_link.wait_for(state="visible")
    sheet = page.locator("#sidebar").bounding_box()
    assert sheet is not None and sheet["x"] == pytest.approx(0, abs=1) and sheet["width"] <= 390
    assert page.locator("#nav-sign-out").is_visible()
    page.locator("#phone-menu-close").tap()
    grid_link.wait_for(state="hidden")
    # A tap outside an open sheet closes it (the script); the sheet itself works without it.
    menu.tap()
    grid_link.wait_for(state="visible")
    page.mouse.click(380, 400)
    grid_link.wait_for(state="hidden")
    search = page.locator("#phone-search-q")
    assert not search.is_visible()
    page.locator("#phone-search-button").tap()
    search.wait_for(state="visible")
    search.fill("zzz")
    search.press("Enter")
    page.wait_for_url(f"{live_server.url}/search/?q=zzz")


def test_the_sidebar_the_content_column_and_the_grid_on_a_laptop(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    label = a_year_of_charges()
    page = signed_in(laptop, live_server, "e2e-shell-laptop")
    sidebar = page.locator("#sidebar").bounding_box()
    assert sidebar is not None and round(sidebar["width"]) == 232 and sidebar["x"] == 0
    assert round(sidebar["height"]) == 800
    assert not page.locator("#phone-bar").is_visible() and not page.locator("#tab-bar").is_visible()
    content = page.locator("#content").bounding_box()
    assert content is not None and round(content["x"]) == 232 and round(content["width"]) == 1048
    banner = page.locator("#sandbox-banner").bounding_box()
    header = page.locator("#page-header").bounding_box()
    assert banner is not None and header is not None
    assert round(banner["x"]) == 232 and round(banner["width"]) == 1048
    assert banner["y"] == pytest.approx(0, abs=1) and header["y"] >= banner["height"] - 1
    title = page.locator("#page-header h1")
    assert title.evaluate("el => getComputedStyle(el).fontSize") == "20px"
    assert title.evaluate("el => getComputedStyle(el).fontWeight") == "600"
    assert "Inter" in page.locator("body").evaluate("el => getComputedStyle(el).fontFamily")

    page.goto(f"{live_server.url}/grid/")
    page.wait_for_load_state("load")
    region = page.locator("#grid-scroll")
    months = page.locator("thead th.month-col")
    assert months.count() == 12
    # The region is the content column less its 32 px paddings and the grid card's 1 px
    # borders (PLAN-012f D9: the region fills the grid card, whose border it is).
    width = region.evaluate("el => el.getBoundingClientRect().width")
    assert width == pytest.approx(1048 - 64 - 2, abs=1)
    assert page.evaluate("document.documentElement.scrollWidth") <= 1280  # no page scroll
    store_cell = page.locator("tr.grid-row th.store-col").first
    before = store_cell.bounding_box()
    assert before is not None and label in store_cell.inner_text()
    region.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    last = page.locator(f'tr.grid-row td[data-month="{timezone.localdate():%Y-%m}"]')
    last_box, region_box = last.bounding_box(), region.bounding_box()
    assert last_box is not None and region_box is not None
    assert last_box["x"] + last_box["width"] <= region_box["x"] + region_box["width"] + 1
    assert last_box["x"] >= before["x"] + before["width"] - 1
    after = store_cell.bounding_box()
    assert after is not None and abs(after["x"] - before["x"]) < 1
