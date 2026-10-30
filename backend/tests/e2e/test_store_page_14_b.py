"""PLAN-014 Batch B in a browser (Fable): the store page's LEASE card rows and the NOTES card,
writing, pinning and editing a note from the Notes & follow-ups tab, the timeline filter, and
Admin › Settings' Brands card — at both widths and in both themes for the QA shots
(``QA_SHOTS_DIR``)."""

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
from ledger.models import Brand, Store
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import (
    a_lease,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)

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
    store.open_date = date(2025, 7, 1)
    store.save()
    lease = a_lease(store, an_operator("Istanbul Repairs"), started=date(2025, 7, 1))
    a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    an_insurance_charge(lease, 2025, date(2025, 7, 1), "300.00")
    return store


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
    page.get_by_role("button", name="Next").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_store_page_lease_card_notes_and_timeline_on_the_laptop(
    live_server: LiveServer, laptop: BrowserContext, theme: str
) -> None:
    store = a_world()
    page = signed_in(laptop, live_server, f"e2e-14b-laptop-{theme}", theme, Role.BILLING)
    page.goto(f"{live_server.url}/stores/{store.pk}/?tab=contacts")
    card = page.locator("#lease-card")
    assert "Since" not in card.inner_text()
    assert "not billed" in card.locator("#header-sign-fee").inner_text()
    assert "Insurance 2026–27" in card.inner_text()
    assert "Insurance 2025–26 ($300.00) not collected" in card.inner_text()
    assert "Notes & follow-ups" in page.locator("#contacts").inner_text()
    # Write a note, pin it, see the NOTES card.
    page.fill("#note-form textarea", "Owner prefers e-mail after 6 pm.")
    page.locator("#note-form button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    note = page.locator("li.note").first
    expect(note).to_contain_text("Owner prefers e-mail after 6 pm.")
    assert page.locator("#notes-card").count() == 0
    note.locator("button.note-pin").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#notes-card")).to_contain_text("Owner prefers e-mail after 6 pm.")
    shot(page, f"store-notes-{theme}-laptop")
    # The timeline filter.
    page.locator("#timeline-filter a", has_text="E-mails").click()
    page.wait_for_url(re.compile(r"show=emails"))
    assert "No e-mails yet." in page.locator("#contacts-tab, main").first.inner_text()
    page.locator("#timeline-filter a", has_text="Notes").click()
    page.wait_for_url(re.compile(r"show=notes"))
    assert page.locator("li.note").count() == 1
    # Edit marks the note.
    page.locator("li.note a", has_text="Edit").first.click()
    page.wait_for_load_state("networkidle")
    dialog = page.locator("#note-edit-dialog")
    expect(dialog).to_be_visible()
    dialog.locator("textarea[name=body]").fill("Owner prefers e-mail after 7 pm.")
    dialog.locator("button[type=submit]").first.click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("li.note").first).to_contain_text("edited")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_store_page_on_the_phone(
    live_server: LiveServer, phone: BrowserContext, theme: str
) -> None:
    store = a_world()
    page = signed_in(phone, live_server, f"e2e-14b-phone-{theme}", theme, Role.BILLING)
    page.goto(f"{live_server.url}/stores/{store.pk}/?tab=contacts")
    expect(page.locator("#note-form")).to_be_visible()
    assert page.locator("#timeline-filter").is_visible()
    shot(page, f"store-notes-{theme}-phone")
    page.goto(f"{live_server.url}/stores/{store.pk}/")
    expect(page.locator("#lease-card")).to_be_visible()
    shot(page, f"store-lease-card-{theme}-phone")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_admin_settings_brands(live_server: LiveServer, laptop: BrowserContext, theme: str) -> None:
    # A transaction test runs after a flush: the three brands of ledger 0058 are written again.
    for order, name in enumerate(("iFixandRepair", "Techy", "Talk&Fix"), start=1):
        Brand.objects.update_or_create(name=name, defaults={"active": True, "sort_order": order})
    page = signed_in(laptop, live_server, f"e2e-14b-settings-{theme}", theme, Role.ADMIN)
    page.goto(f"{live_server.url}/admin-settings/")
    names = [field.input_value() for field in page.locator("#brands-table input[name=name]").all()]
    assert names[:3] == ["iFixandRepair", "Techy", "Talk&Fix"]
    shot(page, f"admin-settings-brands-{theme}-laptop")
    page.fill("#brand-add input[name=name]", "Phone Hub")
    page.locator("#brand-add button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    names = [field.input_value() for field in page.locator("#brands-table input[name=name]").all()]
    assert "Phone Hub" in names
