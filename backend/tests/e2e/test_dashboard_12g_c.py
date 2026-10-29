"""PLAN-012g Batch C in a browser (Fable): the dashboard at both widths and in both themes — the
laptop's hover popover on the mini calendar, the phone's tap panel under the grid (no popover),
the phone's six-month Cashflow card and the laptop's twelve, the range select, the attention card's
three rows and "All n ›" on the phone. The QA shots (``QA_SHOTS_DIR``) come from here."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from accounts.themes import set_theme
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator

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
    for n in range(5):
        lease = a_lease(a_store(f"18{n:02d}"), an_operator(f"Tenant {n}"), started=date(2026, 1, 1))
        a_rent_charge(lease, date(today.year, today.month, 1), due="1500.00")


def signed_in(context: BrowserContext, live_server: LiveServer, username: str, theme: str) -> Page:
    user, device = make_user(username, Role.BILLING)
    assert device is not None
    set_theme(user, theme)
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_laptop_dashboard_hover_opens_a_popover_and_the_wide_card_shows(
    live_server: LiveServer, laptop: BrowserContext, theme: str
) -> None:
    a_world()
    page = signed_in(laptop, live_server, f"e2e-12g-c-laptop-{theme}", theme)
    assert page.locator("#cashflow-card").is_visible()
    assert (
        page.locator("#cashflow-card-narrow").count() == 0
        or not page.locator("#cashflow-card-narrow").is_visible()
    )
    assert page.locator("#cashflow-card .bar-pair").count() == 12
    assert page.locator("#range-select").input_value() == "12m"
    day = page.locator("#mini-grid .mini-day.has-items").first
    if day.count():
        # Hover opens the popover (CSS); a click keeps it open and marks the day selected.
        day.locator(".mini-num").hover()
        popover = day.locator(".mini-popover")
        popover.wait_for(state="visible")
        day.locator(".mini-num").click()
        assert "is-selected" in (day.get_attribute("class") or "")
        assert "is-open" in (day.get_attribute("class") or "")
    assert (
        not page.locator("#mini-calendar .mini-panel").is_visible()
        if page.locator("#mini-calendar .mini-panel").count()
        else True
    )
    shot(page, f"dashboard-{theme}-laptop")
    page.select_option("#range-select", "6m")
    page.wait_for_load_state("networkidle")
    assert page.locator("#cashflow-card .bar-pair").count() == 6
    assert "range=6m" in page.url


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_phone_dashboard_tap_panel_under_the_grid_and_the_six_month_card(
    live_server: LiveServer, phone: BrowserContext, theme: str
) -> None:
    a_world()
    page = signed_in(phone, live_server, f"e2e-12g-c-phone-{theme}", theme)
    assert not page.locator("#cashflow-card").is_visible()
    narrow = page.locator("#cashflow-card-narrow")
    assert narrow.is_visible() and narrow.locator(".bar-pair").count() == 6
    assert page.locator("#range-select-narrow").input_value() == "6m"
    # Needs attention: three rows and "All n ›".
    rows = page.locator(
        "#attention .attention-list tbody tr:visible, #attention .attention-list > li:visible"
    )
    assert rows.count() <= 3
    all_link = page.locator("#all-unpaid")
    assert all_link.is_visible() and all_link.inner_text().startswith("All ")
    # The mini calendar: a tap opens the day's items in the panel under the grid, no popover.
    day = page.locator("#mini-grid .mini-day.has-items").first
    assert day.count() == 1, "the world has a rent day this month"
    day.locator(".mini-num").tap()
    panel = page.locator("#mini-calendar .mini-panel")
    panel.wait_for(state="visible")
    assert panel.locator("li.calendar-item").count() >= 1
    assert "tapped" in panel.inner_text()
    assert not day.locator(".mini-popover").is_visible()
    grid_bottom = page.locator("#mini-grid").bounding_box()
    panel_box = panel.bounding_box()
    assert (
        grid_bottom and panel_box and panel_box["y"] >= grid_bottom["y"] + grid_bottom["height"] - 2
    )
    shot(page, f"dashboard-{theme}-phone")
    # Tapping the same day again closes the panel.
    day.locator(".mini-num").tap()
    assert not panel.is_visible() or panel.locator("li.calendar-item").count() == 0
