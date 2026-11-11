"""PLAN-014 Batch A in a browser (Fable): the search + Filters component on the stores list
(results as you type without a navigation, the panel with Apply and Clear, the count and the
chips, the bottom sheet on the phone, Escape) and the payments tiles' tooltip on focus (PLAN-015
Batch 1 removed the Payment status card and its pill test), and the Payments period links keeping
the typed search (PLAN-015 Batch 2). The QA shots (``QA_SHOTS_DIR``) come from here, in both
themes."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from accounts.themes import set_theme
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}


def shot(page: Page, name: str) -> None:
    folder = os.environ.get("QA_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)


@pytest.fixture
def laptop(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport=LAPTOP)
    yield context
    context.close()


@pytest.fixture
def phone(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(
        viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    yield context
    context.close()


def a_world() -> None:
    today = date.today()
    for n, state in enumerate(("NJ", "NJ", "GA", "GA", "TX")):
        lease = a_lease(
            a_store(f"14{n:02d}", number=1400 + n, state=state),
            an_operator(f"Tenant {n}"),
            started=date(2026, 1, 1),
        )
        charge = a_rent_charge(lease, date(today.year, today.month, 1), due="1500.00")
        if n == 0:
            a_payment(charge, "500.00", today)
    a_store("1499", number=1499, state="NJ")  # never leased


def signed_in(
    context: BrowserContext, live_server: LiveServer, username: str, theme: str, role: Role
) -> Page:
    user, device = make_user(username, role)
    assert device is not None
    set_theme(user, theme)
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_stores_list_searches_as_you_type_and_filters_from_the_panel(
    live_server: LiveServer, laptop: BrowserContext, theme: str
) -> None:
    a_world()
    page = signed_in(laptop, live_server, f"e2e-14a-list-{theme}", theme, Role.ADMIN)
    page.goto(f"{live_server.url}/stores/")
    rows = page.locator("#results tbody tr")
    assert rows.count() >= 6
    # Results as you type: no navigation, the region swapped, the URL updated.
    page.fill("#filters input[type=search]", "1499")
    expect(rows).to_have_count(1)
    assert "q=1499" in page.url
    assert "Vacant · never leased" in page.locator("#results").inner_text()
    page.fill("#filters input[type=search]", "")
    expect(rows).to_have_count(6)
    # The panel: hidden, opened by the button, applied with Apply.
    panel = page.locator("#filters-panel")
    toggle = page.locator("#filters .filters-toggle")
    assert not panel.is_visible() and toggle.get_attribute("aria-expanded") == "false"
    toggle.click()
    expect(panel).to_be_visible()
    assert toggle.get_attribute("aria-expanded") == "true"
    shot(page, f"stores-filters-panel-{theme}-laptop")
    page.select_option("#filters-panel select[name=state]", "NJ")
    page.locator("#filters-panel button[type=submit]").click()
    page.wait_for_url(re.compile(r"state=NJ"))
    expect(rows).to_have_count(3)
    assert "Filters · 1" in toggle.inner_text()
    chip = page.locator("#filters .chip", has_text="State: New Jersey")
    expect(chip).to_be_visible()
    shot(page, f"stores-filters-chip-{theme}-laptop")
    # Escape closes an open panel; the chip removes its filter.
    toggle.click()
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    chip.click()
    page.wait_for_url(lambda url: "state=" not in url)
    expect(rows).to_have_count(6)
    assert "Filters · " not in toggle.inner_text()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_phone_panel_is_a_bottom_sheet_with_clear_and_apply(
    live_server: LiveServer, phone: BrowserContext, theme: str
) -> None:
    a_world()
    page = signed_in(phone, live_server, f"e2e-14a-phone-{theme}", theme, Role.BILLING)
    page.goto(f"{live_server.url}/stores/?state=GA&status=active")
    toggle = page.locator("#filters .filters-toggle")
    assert "Filters · 2" in toggle.inner_text()
    toggle.tap()
    panel = page.locator("#filters-panel")
    expect(panel).to_be_visible()
    box = panel.bounding_box()
    # A sheet: fixed over the page, its bottom edge on the phone's tab bar.
    assert box is not None and box["y"] + box["height"] >= PHONE["height"] - 120
    assert (
        page.evaluate("getComputedStyle(document.getElementById('filters-panel')).position")
        == "fixed"
    )
    assert page.locator("#filters-backdrop").is_visible()
    shot(page, f"stores-filters-sheet-{theme}-phone")
    clear = panel.locator("a", has_text="Clear")
    href = clear.get_attribute("href") or ""
    assert "state=" not in href and "status=" not in href
    clear.tap()
    page.wait_for_url(lambda url: "state=" not in url)
    assert "Filters · " not in page.locator("#filters .filters-toggle").inner_text()


def test_the_tiles_tooltip_opens_on_focus(live_server: LiveServer, laptop: BrowserContext) -> None:
    a_world()
    page = signed_in(laptop, live_server, "e2e-14a-tiles", "light", Role.BILLING)
    page.goto(f"{live_server.url}/payments/")
    # PLAN-015 Batch 2 re-pin: the five tiles are five KPI cards v2.
    tiles = page.locator("#payments-totals .kpi2")
    assert tiles.count() == 5
    tooltip = page.locator("#totals-help")
    assert not tooltip.is_visible()
    page.locator("#payment-totals-card button.info").focus()
    expect(tooltip).to_be_visible()
    shot(page, "payments-tiles-light-laptop")


def test_the_payments_period_links_keep_the_search_typed(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    # Fable's round-1 finding on PLAN-015 Batch 2: the search refreshes the list in place, so the
    # period links must be refreshed with it, or "This month" drops the typed search.
    today = date.today()
    for number, label in ((2701, "Harbor Point"), (2702, "Inland Plaza")):
        lease = a_lease(a_store(label, number), an_operator(f"{label} Co"))
        charge = a_rent_charge(lease, date(today.year, today.month, 1), due="900.00")
        a_payment(charge, "300.00", today)
    page = signed_in(laptop, live_server, "e2e-15b2-period", "light", Role.BILLING)
    page.goto(f"{live_server.url}/payments/")
    rows = page.locator("#results tbody tr")
    expect(rows).to_have_count(2)
    page.fill("#filters input[type=search]", "Harbor")
    expect(rows).to_have_count(1)
    month = page.locator("#payments-period a", has_text="This month")
    expect(month).to_have_attribute("href", re.compile(r"q=Harbor"))
    month.click()
    start = date(today.year, today.month, 1).isoformat()
    page.wait_for_url(re.compile(rf"date_from={start}"))
    assert "q=Harbor" in page.url and "date_to=" in page.url
    expect(rows).to_have_count(1)
    assert "Harbor Point" in page.locator("#results").inner_text()
    assert "Inland Plaza" not in page.locator("#results").inner_text()
    expect(page.locator("#payments-period a[aria-current=page]")).to_have_text("This month")
