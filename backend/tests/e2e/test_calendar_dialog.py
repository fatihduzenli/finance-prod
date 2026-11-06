"""The New calendar entry dialog in a browser (owner, 2026-11-06): "+ New entry" opens it over
the Calendar page without leaving it, Cancel closes it; a click on a day opens it with that day
as the Date and its words beside the input; typing in the link field lists the matches and
choosing one shows the chip (its × brings the field back); Save closes the dialog and the entry
is on its day, linked to the store's lease and operator; on a phone the dialog is a sheet at the
bottom of the screen."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from django.utils import timezone
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from finance.dates import day_text
from ledger.models import CalendarEntry, Lease, Store
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_store, an_operator

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


def a_world() -> tuple[Store, Lease]:
    store = a_store("Elm Park", number=1601)
    lease = a_lease(store, an_operator("Istanbul Repairs"), started=date(2025, 7, 1))
    return store, lease


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


def test_new_entry_on_the_laptop(live_server: LiveServer, laptop: BrowserContext) -> None:
    store, lease = a_world()
    today = timezone.localdate()
    page = signed_in(laptop, live_server, "e2e-calendar-entry-laptop")
    calendar_url = f"{live_server.url}/calendar/"
    page.goto(calendar_url)
    dialog = page.locator("#calendar-entry")
    expect(dialog).to_be_hidden()
    # "+ New entry" opens the dialog on the day shown (today) without leaving the page.
    page.locator("#calendar-new-entry").click()
    expect(dialog).to_be_visible()
    assert page.url == calendar_url
    expect(dialog.locator("[name=on]")).to_have_value(today.isoformat())
    expect(dialog.locator("#entry-day")).to_have_text(day_text(today))
    expect(dialog.locator("#save-calendar-entry")).to_be_in_viewport()
    shot(page, "calendar-entry-laptop")
    # Cancel closes it, still on the page.
    dialog.locator("#cancel-calendar-entry").click()
    expect(dialog).to_be_hidden()
    assert page.url == calendar_url
    # A click on a day opens it with that day.
    chosen = today.replace(day=20)
    page.locator(f'.calendar-day[data-day="{chosen.isoformat()}"] .calendar-num-link').click()
    expect(dialog).to_be_visible()
    assert page.url == calendar_url
    expect(dialog.locator("[name=on]")).to_have_value(chosen.isoformat())
    expect(dialog.locator("#entry-day")).to_have_text(day_text(chosen))
    # The date's words follow the input.
    dialog.locator("[name=on]").fill(chosen.replace(day=21).isoformat())
    expect(dialog.locator("#entry-day")).to_have_text(day_text(chosen.replace(day=21)))
    dialog.locator("[name=on]").fill(chosen.isoformat())
    # Reminder, a title, the morning e-mail.
    dialog.locator("label.kind-card", has_text="Reminder").click()
    dialog.locator("[name=title]").fill("Collect the keys")
    dialog.locator("label.inline", has_text="E-mail me that morning").click()
    # The link: typing lists the store and its lease; choosing the store shows the chip.
    dialog.locator("#entry-link").fill("1601")
    results = dialog.locator("#entry-link-results li button")
    expect(results).to_have_count(2)
    expect(results.first).to_contain_text("#1601 Elm Park")
    expect(results.first).to_contain_text("Store")
    results.first.click()
    chip = dialog.locator("#entry-link-chip")
    expect(chip).to_be_visible()
    expect(chip).to_contain_text("Store: #1601 Elm Park")
    expect(dialog.locator("#entry-link")).to_be_hidden()
    expect(dialog.locator("[name=link_kind]")).to_have_value("store")
    expect(dialog.locator("[name=link_id]")).to_have_value(str(store.pk))
    shot(page, "calendar-entry-chip-laptop")
    # The × removes it and brings the field back; choose again.
    dialog.locator("#entry-link-remove").click()
    expect(chip).to_be_hidden()
    expect(dialog.locator("#entry-link")).to_be_visible()
    expect(dialog.locator("[name=link_id]")).to_have_value("")
    dialog.locator("#entry-link").fill("elm")
    expect(results.first).to_contain_text("#1601 Elm Park")
    results.first.click()
    expect(chip).to_be_visible()
    # Save: the dialog closes and the entry is on its day.
    dialog.locator("#save-calendar-entry").click()
    page.wait_for_url(f"{live_server.url}/calendar/?month={chosen:%Y-%m}#entry-*")
    expect(page.locator("#calendar-entry")).to_be_hidden()
    entry = CalendarEntry.objects.get()
    day = page.locator(f'.calendar-day[data-day="{chosen.isoformat()}"]')
    expect(day.locator(f"#entry-{entry.pk}")).to_have_text("Collect the keys")
    assert (entry.kind, entry.on, entry.email_morning) == ("reminder", chosen, True)
    assert (entry.store_id, entry.lease_id, entry.operator_id) == (
        store.pk,
        lease.pk,
        lease.operator_id,
    )
    shot(page, "calendar-entry-saved-laptop")


def test_new_entry_is_a_sheet_on_the_phone(live_server: LiveServer, phone: BrowserContext) -> None:
    a_world()
    page = signed_in(phone, live_server, "e2e-calendar-entry-phone")
    page.goto(f"{live_server.url}/calendar/new/?on={timezone.localdate():%Y-%m-%d}")
    dialog = page.locator("#calendar-entry")
    expect(dialog).to_be_visible()
    box = dialog.bounding_box()
    assert box is not None
    assert box["width"] == pytest.approx(PHONE["width"], abs=1)
    assert box["y"] + box["height"] == pytest.approx(PHONE["height"], abs=1)
    assert box["y"] > 0
    shot(page, "calendar-entry-phone")
    # The foot is reachable inside the sheet.
    save = dialog.locator("#save-calendar-entry")
    save.scroll_into_view_if_needed()
    expect(save).to_be_in_viewport()
    # Cancel goes back to the Calendar on that day.
    dialog.locator("#cancel-calendar-entry").click()
    page.wait_for_url(f"{live_server.url}/calendar/?month=*")
    expect(page.locator("#calendar-entry")).to_be_hidden()
    # From the page itself, "+ New entry" opens the sheet.
    page.locator("#calendar-new-entry").click()
    expect(dialog).to_be_visible()
