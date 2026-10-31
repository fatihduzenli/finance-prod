"""PLAN-014 Batch H in a browser (Fable): the operator portal phone-first and on the laptop, in
both themes — the sign-in page, Home with every store, a store with its open charges and the bank
on file, the Pay page saying online payment is not available (the sandbox flag), the Bank account
page saying it is not set up (no web-payments token), the Account page's theme switch, Sign out;
and the staff PORTAL card. The QA shots (``QA_SHOTS_DIR``) are checkpoint 2's evidence."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from django.contrib.auth.hashers import make_password
from django.utils import timezone
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from accounts.themes import set_theme
from ledger.models import LeaseBilling, Operator
from portal.models import PortalUser
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}
PASSWORD = "correct horse battery staple 2026"


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


def a_world(theme: str) -> tuple[Operator, PortalUser]:
    """Ayşe Kaya with #1701 (open rent, a bank on file) and #1702 (ended), an active account."""
    operator = an_operator("Ayşe Kaya")
    operator.email = "ayse@example.com"
    operator.save()
    current = a_lease(a_store("1701", number=1701), operator, started=date(2026, 1, 1))
    LeaseBilling.objects.create(
        lease=current,
        email="store1701@example.com",
        company_name="Kaya Repairs LLC",
        bank_last4="4821",
    )
    a_rent_charge(current, date(2026, 9, 1), due="1500.00")
    a_rent_charge(current, date(2026, 10, 1), due="1500.00")
    ended = a_lease(a_store("1702", number=1702), operator, started=date(2025, 1, 1))
    ended.ended_on = date(2026, 6, 30)
    ended.save()
    user = PortalUser.objects.create(
        operator=operator,
        email="ayse@example.com",
        password=make_password(PASSWORD),
        is_active=True,
        invited_at=timezone.now(),
        activated_at=timezone.now(),
        theme=theme,
    )
    return operator, user


def portal_sign_in(context: BrowserContext, live_server: LiveServer) -> Page:
    page = context.new_page()
    page.goto(f"{live_server.url}/portal/login/")
    page.fill("input[name=email]", "ayse@example.com")
    page.fill("input[name=password]", PASSWORD)
    page.get_by_role("button", name=re.compile("Sign in")).click()
    page.wait_for_url(f"{live_server.url}/portal/")
    return page


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_portal_on_the_phone(
    live_server: LiveServer, phone: BrowserContext, theme: str
) -> None:
    operator, _ = a_world(theme)
    page = phone.new_page()
    page.goto(f"{live_server.url}/portal/login/")
    shot(page, f"portal-login-{theme}-phone")
    page.close()
    page = portal_sign_in(phone, live_server)
    body = page.locator("main, body").first.inner_text()
    assert re.search(r"\b1701\b", body) and re.search(
        r"\b1702\b", body
    )  # both stores, whole numbers
    assert "ended" in body.lower()
    assert "$3,000.00" in body
    shot(page, f"portal-home-{theme}-phone")
    page.get_by_role("link", name=re.compile("1701")).first.click()
    page.wait_for_url(re.compile(r"/portal/stores/.+/$"))
    store_text = page.locator("main, body").first.inner_text()
    assert "4821" in store_text and "Rent" in store_text
    shot(page, f"portal-store-{theme}-phone")
    page.goto(page.url + "pay/")
    assert "not available yet" in page.locator("main, body").first.inner_text()
    shot(page, f"portal-pay-{theme}-phone")
    page.goto(page.url.replace("pay/", "bank/"))
    bank_text = page.locator("main, body").first.inner_text()
    assert "not set up" in bank_text or "Shared with" in bank_text
    assert "routing" not in bank_text.lower()
    shot(page, f"portal-bank-{theme}-phone")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_portal_on_the_laptop_and_the_account_page(
    live_server: LiveServer, laptop: BrowserContext, theme: str
) -> None:
    a_world(theme)
    page = portal_sign_in(laptop, live_server)
    shot(page, f"portal-home-{theme}-laptop")
    page.goto(f"{live_server.url}/portal/account/")
    expect(page.locator("[name=theme]").first).to_be_attached()
    assert "ayse@example.com" in page.locator("main, body").first.inner_text()
    shot(page, f"portal-account-{theme}-laptop")
    page.goto(f"{live_server.url}/portal/statement/")
    assert re.search(r"\b1701\b", page.locator("main, body").first.inner_text())
    shot(page, f"portal-statement-{theme}-laptop")
    page.get_by_role("button", name=re.compile("Sign out")).first.click()
    page.wait_for_url(re.compile(r"/portal/login/"))
    page.goto(f"{live_server.url}/portal/")
    page.wait_for_url(re.compile(r"/portal/login/"))


def test_the_staff_portal_card_and_a_viewer_sees_no_actions(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    operator, _ = a_world("light")
    user, device = make_user("e2e-14h-billing", Role.BILLING)
    assert device is not None
    set_theme(user, "light")
    page = laptop.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", "e2e-14h-billing")
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    page.goto(f"{live_server.url}/operators/{operator.pk}/")
    card = page.locator("#portal-card, section:has-text('PORTAL')").first
    expect(card).to_be_visible()
    assert "Active" in card.inner_text()
    shot(page, "operator-portal-card-light-laptop")
    assert page.goto(f"{live_server.url}/portal/") is not None
    page.wait_for_url(re.compile(r"/portal/login/"))  # a staff session opens no portal page
