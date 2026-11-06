"""The Edit lease page in a browser (docs/qa/15/PLAN-015a-edit-lease.md §4 task 4).

``ledger/edit_lease.js`` lists each pending change in the Changes card ("old → new") and keeps
"Save changes" disabled while nothing changed; reverting a change disables it again. "Change
rent" reveals the rent fields. On a phone the page is one column, no wider than the screen.
"""

from __future__ import annotations

from datetime import date

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import Lease
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1440, "height": 900}
PHONE: ViewportSize = {"width": 390, "height": 844}


def signed_in(context: BrowserContext, live_server: LiveServer, username: str) -> Page:
    _, device = make_user(username, Role.ADMIN)
    assert device is not None
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    # The dashboard's own requests finish before the test opens the next page.
    page.wait_for_load_state("networkidle")
    return page


def a_world() -> Lease:
    return a_lease(
        a_store("Istanbul", 1991), an_operator("Fatih Co"), started=date(2026, 1, 13), debit_day=1
    )


def changes(page: Page) -> list[str]:
    return [text.strip() for text in page.locator("#changes-list li").all_text_contents()]


def test_a_rent_change_is_listed_and_enables_save_and_reverting_disables_it(
    live_server: LiveServer, browser: Browser
) -> None:
    lease = a_world()
    context = browser.new_context(viewport=LAPTOP)
    page = signed_in(context, live_server, "e2e-edit-lease-rent")
    page.goto(f"{live_server.url}/leases/{lease.pk}/edit/")
    save = page.locator("#save-lease")
    expect(save).to_be_disabled()
    expect(page.locator("#changes-empty")).to_be_visible()
    expect(page.locator("#rent-change-fields")).to_be_hidden()
    page.locator("#change-rent").click()
    expect(page.locator("#rent-change-fields")).to_be_visible()
    page.fill("#id_rent", "3650")
    page.fill("#id_rent_effective_from", "2026-12-01")
    assert changes(page) == ["Monthly rent $3,500.00 → $3,650.00 from Dec 1, 2026"]
    expect(save).to_be_enabled()
    expect(page.locator("#changes-empty")).to_be_hidden()
    page.fill("#id_rent", "")
    assert changes(page) == []
    expect(save).to_be_disabled()
    # The rent typed as it is now is no change either.
    page.fill("#id_rent", "3500.00")
    assert changes(page) == []
    page.fill("#id_rent", "3650.00")
    save.click()
    page.wait_for_url(f"{live_server.url}/leases/{lease.pk}/")
    context.close()


def test_a_debit_day_change_reads_old_to_new_and_the_channel_too(
    live_server: LiveServer, browser: Browser
) -> None:
    lease = a_world()
    context = browser.new_context(viewport=LAPTOP)
    page = signed_in(context, live_server, "e2e-edit-lease-day")
    page.goto(f"{live_server.url}/leases/{lease.pk}/edit/")
    page.select_option("#id_debit_day", "5")
    assert changes(page) == ["Debit day 1st → 5th"]
    page.locator('label[for="id_payment_channel_1"]').click()
    assert changes(page) == ["Debit day 1st → 5th", "Payment channel Autopay → E-mail invoice"]
    page.locator("#twice-monthly").check()
    page.select_option("#id_second_debit_day", "20")
    assert "Second debit day — → 20th" in changes(page)
    page.locator("#twice-monthly").uncheck()
    assert "Second debit day — → 20th" not in changes(page)
    page.select_option("#id_debit_day", "1")
    page.locator('label[for="id_payment_channel_0"]').click()
    assert changes(page) == []
    expect(page.locator("#save-lease")).to_be_disabled()
    page.select_option("#id_debit_day", "5")
    page.locator("#save-lease").click()
    page.wait_for_url(f"{live_server.url}/leases/{lease.pk}/")
    lease.refresh_from_db()
    assert lease.debit_day == 5
    context.close()


def test_the_page_is_one_column_on_a_phone(live_server: LiveServer, browser: Browser) -> None:
    lease = a_world()
    context = browser.new_context(
        viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    page = signed_in(context, live_server, "e2e-edit-lease-phone")
    page.goto(f"{live_server.url}/leases/{lease.pk}/edit/")
    expect(page.locator("#lease-changes")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    cards = page.locator("#lease-card").bounding_box(), page.locator("#rent-card").bounding_box()
    assert cards[0] is not None and cards[1] is not None
    assert cards[1]["y"] > cards[0]["y"] and abs(cards[1]["x"] - cards[0]["x"]) < 1
    context.close()
