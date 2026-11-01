"""PLAN-012f-b Batch A, finding 4, in a browser: clicking the dashboard's "+ New invoice" opens the
"Which store?" dialog, typing "1991" lists the store, choosing it lands on that store's invoice
builder, never on the Stores list; Escape closes the dialog and stays on the dashboard; the same
picker opens from the search results page."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import PaymentChannel, Store
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}


def shot(page: Page, name: str) -> None:
    """A QA screenshot when QA_SHOTS_DIR is set (docs/qa/12f-b/shots): never in CI."""
    folder = os.environ.get("QA_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=False)


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


def signed_in(context: BrowserContext, live_server: LiveServer, role: Role) -> Page:
    username = f"e2e-12fb-{role.value}"
    _, device = make_user(username, role)
    assert device is not None
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


def a_world() -> Store:
    store = a_store("1991 Istanbul", 1991)
    a_lease(store, an_operator("Fatih Co"), channel=PaymentChannel.WIRE)
    paris = a_store("2484 Paris", 2484)
    a_lease(paris, an_operator("Paris Co"), channel=PaymentChannel.WIRE)
    return store


def builder_of(live_server: LiveServer, store: Store) -> str:
    return live_server.url + reverse("ledger:invoices:new_store", kwargs={"pk": store.pk})


def test_choosing_1991_lands_on_its_invoice_builder(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    store = a_world()
    page = signed_in(laptop, live_server, Role.BILLING)
    page.locator("#new-invoice").click()
    dialog = page.locator("dialog#store-picker")
    assert dialog.evaluate("d => d.open")
    assert page.url == f"{live_server.url}/"  # no navigation to the Stores list
    page.locator("#store-picker-q").fill("1991")
    row = page.locator("#store-picker-results a", has_text="1991")
    row.first.wait_for()
    shot(page, "store-picker-laptop")
    assert "Istanbul" in row.first.inner_text()
    row.first.click()
    page.wait_for_url(builder_of(live_server, store))
    assert reverse("ledger:stores:list") not in page.url


def test_escape_closes_the_picker_and_stays_on_the_dashboard(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    a_world()
    page = signed_in(laptop, live_server, Role.BILLING)
    page.locator("#new-invoice").click()
    dialog = page.locator("dialog#store-picker")
    assert dialog.evaluate("d => d.open")
    page.keyboard.press("Escape")
    assert not dialog.evaluate("d => d.open")
    assert page.url == f"{live_server.url}/"


def test_a_query_with_no_match_says_so_and_enter_follows_the_highlighted_row(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    store = a_world()
    page = signed_in(laptop, live_server, Role.BILLING)
    page.locator("#new-invoice").click()
    box = page.locator("#store-picker-q")
    box.fill("zzzz")
    page.get_by_text("No store matches").wait_for()
    box.fill("Istan")
    page.locator("#store-picker-results a", has_text="Istanbul").first.wait_for()
    box.press("ArrowDown")
    box.press("Enter")
    page.wait_for_url(builder_of(live_server, store))


def test_the_picker_opens_on_a_phone_as_a_sheet(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    store = a_world()
    page = signed_in(phone, live_server, Role.BILLING)
    page.locator("#new-invoice").tap()
    dialog = page.locator("dialog#store-picker")
    assert dialog.evaluate("d => d.open")
    found = dialog.bounding_box()
    assert found is not None and found["width"] >= PHONE["width"] - 2
    page.locator("#store-picker-q").fill("2484")
    page.locator("#store-picker-results a", has_text="Paris").first.wait_for()
    shot(page, "store-picker-phone")
    page.locator("#store-picker-results a", has_text="Paris").first.tap()
    paris = Store.objects.get(store_number=2484)
    page.wait_for_url(builder_of(live_server, paris))
    assert store.pk != paris.pk
