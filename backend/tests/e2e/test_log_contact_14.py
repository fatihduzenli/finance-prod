"""The "Log contact" dialog in a browser (owner, 2026-11-01): "+ Log contact" on the store's Notes
& follow-ups tab opens the dialog; Call shows the outcome chips and hides the promise fields;
a chip saves without a note and the entry is first in the timeline; Promise to pay shows its
fields with the amount at the store's open balance; a second click clears a chip; the date's
day follows the input; on a phone the dialog is a sheet at the bottom of the screen."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import ContactEntry, Store
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


def a_world() -> Store:
    store = a_store("1601", number=1601)
    lease = a_lease(store, an_operator("Istanbul Repairs"), started=date(2025, 7, 1))
    a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    return store


def signed_in(context: BrowserContext, live_server: LiveServer, username: str) -> Page:
    user, device = make_user(username, Role.BILLING)
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


def open_dialog(page: Page, live_server: LiveServer, store: Store) -> None:
    page.goto(f"{live_server.url}/stores/{store.pk}/?tab=contacts")
    page.locator("#add-store-contact").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#log-contact-dialog")).to_be_visible()


def test_log_contact_on_the_laptop(live_server: LiveServer, laptop: BrowserContext) -> None:
    store = a_world()
    page = signed_in(laptop, live_server, "e2e-log-contact-laptop")
    open_dialog(page, live_server, store)
    dialog = page.locator("#log-contact-dialog")
    # Call is chosen: the chips show, the promise fields do not.
    expect(dialog.locator("#log-contact-outcome")).to_be_visible()
    expect(dialog.locator("[name=promised_amount]")).to_be_hidden()
    expect(dialog.locator("[name=promised_date]")).to_be_hidden()
    shot(page, "log-contact-call-laptop")
    # The day under Date follows the input.
    dialog.locator("[name=happened_on]").fill("2026-10-30")
    expect(dialog.locator("#log-contact-day")).to_have_text("Oct 30, 2026")
    # A second click clears a chip.
    chip = dialog.locator("label.outcome-chip", has_text="Answered")
    chip.click()
    assert dialog.locator("#log-call_outcome_0").is_checked()
    chip.click()
    assert not dialog.locator("#log-call_outcome_0").is_checked()
    # Promise to pay: its fields show with the open balance, the chips hide.
    dialog.locator("label.kind-card", has_text="Promise to pay").click()
    expect(dialog.locator("#log-contact-outcome")).to_be_hidden()
    expect(dialog.locator("[name=promised_amount]")).to_be_visible()
    balance = json.loads(page.locator("#promise-amounts").text_content() or "{}")
    assert len(balance) == 1
    expect(dialog.locator("[name=promised_amount]")).to_have_value(next(iter(balance.values())))
    shot(page, "log-contact-promise-laptop")
    # Back to Call, choose No answer, save without a note.
    dialog.locator("label.kind-card", has_text="Call").click()
    expect(dialog.locator("[name=promised_amount]")).to_be_hidden()
    dialog.locator("[name=happened_on]").fill("2026-11-01")
    dialog.locator("label.outcome-chip", has_text="No answer").click()
    dialog.locator("#save-contact").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#log-contact-dialog")).to_have_count(0)
    first = page.locator("#contact-entries li.contact-entry").first
    expect(first).to_contain_text("No answer.")
    entry = ContactEntry.objects.get()
    assert (entry.body, entry.promised_amount) == ("No answer.", None)


def test_log_contact_is_a_sheet_on_the_phone(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    store = a_world()
    page = signed_in(phone, live_server, "e2e-log-contact-phone")
    open_dialog(page, live_server, store)
    box = page.locator("#log-contact-dialog").bounding_box()
    assert box is not None
    assert box["width"] == pytest.approx(PHONE["width"], abs=1)
    assert box["y"] + box["height"] == pytest.approx(PHONE["height"], abs=1)
    assert box["y"] > 0
    shot(page, "log-contact-phone")
    # Cancel goes back to the tab.
    page.locator("#cancel-contact").click()
    page.wait_for_url(f"{live_server.url}/stores/{store.pk}/?tab=contacts#tab-contacts")
