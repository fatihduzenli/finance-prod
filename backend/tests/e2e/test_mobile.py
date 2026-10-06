"""The app on a phone (docs/qa/PLAN-011-dashboard-reports-mobile.md D9, §5 Batch C, §6):
at a 390 x 844 viewport with touch, sign in with the password and the TOTP code, land on
the dashboard with the sandbox banner in view, open the grid from the "Menu", scroll it
sideways, open a charge; the manifest is linked, the service worker registers, and what it
keeps in the browser's cache is static files only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from django.utils import timezone
from playwright.sync_api import Browser, BrowserContext, Page
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import RentChargeStatus
from ledger.services.pnl import add_months
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

WIDTH, HEIGHT = 390, 844
TAP = 44
STATUSES = (RentChargeStatus.PAID, RentChargeStatus.UNPAID, RentChargeStatus.PARTIAL)


@pytest.fixture
def phone(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(
        viewport={"width": WIDTH, "height": HEIGHT},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
    )
    yield context
    context.close()


def a_year_of_charges() -> str:
    """A store with a charge in each of the grid's default twelve months; the label."""
    this_month = timezone.localdate().replace(day=1)
    first = add_months(this_month, -11)
    lease = a_lease(
        a_store("0101 PHONE TEST", 101, "GA"),
        an_operator("Phone Operator"),
        started=date(2024, 1, 1),
    )
    for n in range(12):
        status = STATUSES[n % len(STATUSES)]
        paid = "3500.00" if status == RentChargeStatus.PAID else "0.00"
        a_rent_charge(lease, add_months(first, n), status, "3500.00", paid)
    return str(lease.store.external_label)


def in_viewport(page: Page, selector: str) -> bool:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return box["y"] >= 0 and box["y"] + box["height"] <= HEIGHT and box["x"] >= 0


def sign_in(page: Page, live_server: LiveServer, username: str, code: str) -> None:
    page.goto(f"{live_server.url}/account/login/")
    # Tapping a field opens the keyboard on a phone; the fields and the button stay usable.
    page.tap("#id_auth-username")
    page.keyboard.type(username)
    page.tap("#id_auth-password")
    page.keyboard.type(TEST_PASSWORD)
    page.get_by_role("button", name="Next").tap()
    token = page.locator("#id_token-otp_token")
    token.wait_for(state="visible")
    assert in_viewport(page, "#sandbox-banner")
    token.tap()
    page.keyboard.type(code)
    page.get_by_role("button", name="Next").tap()


def test_mfa_dashboard_grid_and_charge_on_a_phone(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    _, device = make_user("e2e-phone", Role.VIEWER)
    assert device is not None
    label = a_year_of_charges()
    page = phone.new_page()

    sign_in(page, live_server, "e2e-phone", current_code(device))
    page.wait_for_url(f"{live_server.url}/")
    page.wait_for_load_state("load")

    # The banner is fixed and in view on load, and still after scrolling the page.
    assert page.locator("#sandbox-banner").is_visible()
    assert in_viewport(page, "#sandbox-banner")
    page.mouse.wheel(0, 600)
    assert in_viewport(page, "#sandbox-banner")
    assert page.evaluate("document.documentElement.scrollWidth") <= WIDTH  # no page sideways scroll

    # Installable: the manifest is linked and the worker registers after load.
    assert page.locator('link[rel="manifest"]').get_attribute("href") == "/manifest.webmanifest"
    scope = page.evaluate(
        """async () => {
            await navigator.serviceWorker.ready;
            const registration = await navigator.serviceWorker.getRegistration();
            return registration ? registration.scope : null;
        }"""
    )
    assert scope == f"{live_server.url}/"

    # The menu is folded behind "Menu" on a phone.
    grid_link = page.locator("#nav-grid")
    assert not grid_link.is_visible()
    page.locator("#nav-menu").tap()
    grid_link.wait_for(state="visible")
    menu_box = page.locator("#nav-menu").bounding_box()
    assert menu_box is not None and menu_box["height"] >= TAP
    grid_link.tap()
    page.wait_for_url(f"{live_server.url}/grid/")

    # The grid scrolls sideways inside its own region; the page itself does not.
    region = page.locator(".grid-scroll")
    widths = region.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert widths[0] > widths[1]
    region.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    assert region.evaluate("el => el.scrollLeft") > 0
    store_cell = page.locator("tr.grid-row th.store-col").first
    store_box = store_cell.bounding_box()
    assert store_box is not None and store_box["x"] >= 0  # the first column is sticky
    assert label in store_cell.inner_text()

    # A cell's click target is at least 44 px high; it opens the charge.
    this_month = f"{timezone.localdate():%Y-%m}"
    link = page.locator(f'tr.grid-row td[data-month="{this_month}"] a')
    box = link.bounding_box()
    assert box is not None and box["height"] >= TAP
    link.tap()
    page.wait_for_url(f"{live_server.url}/charges/**")
    assert in_viewport(page, "#sandbox-banner")

    # Only static files are in the worker's cache, after a dashboard, a grid and a charge.
    cached = page.evaluate(
        """async () => {
            const urls = [];
            for (const name of await caches.keys()) {
                const cache = await caches.open(name);
                for (const request of await cache.keys()) {
                    urls.push(new URL(request.url).pathname);
                }
            }
            return urls;
        }"""
    )
    assert cached, "the worker precached nothing"
    assert all(path.startswith("/static/") for path in cached), cached
    assert "/static/finance/app.css" in cached
