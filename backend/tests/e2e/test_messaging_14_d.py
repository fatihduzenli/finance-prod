"""PLAN-014 Batch D in a browser (Fable): Admin › Settings' Mail card and the Message templates
list, a template's edit page with its Preview, Admin › Outbox with a held message and its
"Send now" refused while mail is not set up, and the store timeline's e-mail row. The QA shots
(``QA_SHOTS_DIR``) come from here in both themes at both widths."""

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
from ledger.models import LeaseBilling, MessageStatus, MessageTemplate, OutboundMessage
from ledger.services import messaging
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


def seeded_templates() -> None:
    """A transaction test runs after a flush: the seven templates of ledger 0061 are written
    again."""
    import importlib

    seed = importlib.import_module("ledger.migrations.0061_messaging_engine")
    for code, name, internal, subject, body in seed.TEMPLATES:
        MessageTemplate.objects.get_or_create(
            code=code,
            defaults={"name": name, "internal": internal, "subject": subject, "body": body},
        )


def a_world() -> tuple[OutboundMessage, str]:
    """Ayşe Kaya runs #1701; one message of rule B is held (mail is not set up in the test
    environment). Returns the message and the store's page path."""
    seeded_templates()
    operator = an_operator("Ayşe Kaya")
    lease = a_lease(a_store("1701", number=1701), operator, started=date(2026, 1, 1))
    LeaseBilling.objects.create(lease=lease, email="store1701@example.com", bank_last4="4821")
    charge = a_rent_charge(lease, date(2026, 10, 1), due="1500.00")
    template = MessageTemplate.objects.get(code="reminder_due")
    context = messaging.context_for(lease, charge, "B", today=date(2026, 9, 28))
    message = messaging.queue("B", lease, charge, template, context)
    assert message is not None
    messaging.send(message.pk)
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    return message, f"/stores/{lease.store_id}/"


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
def test_settings_mail_templates_and_the_outbox_on_the_laptop(
    live_server: LiveServer, laptop: BrowserContext, theme: str
) -> None:
    message, store_path = a_world()
    page = signed_in(laptop, live_server, f"e2e-14d-laptop-{theme}", theme, Role.ADMIN)
    # The Mail card says mail is not set up and saves an allowlist.
    page.goto(f"{live_server.url}/admin-settings/")
    expect(page.locator("#mail-status")).to_contain_text("not set up")
    page.fill("#mail-allowlist", "owner@example.com")
    page.locator("#mail-form button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    assert page.locator("#mail-allowlist").input_value() == "owner@example.com"
    shot(page, f"admin-settings-mail-{theme}-laptop")
    # The templates list and an edit page with its preview.
    page.locator("#templates-link").click()
    page.wait_for_url(re.compile(r"/admin-settings/templates/"))
    rows = page.locator("#templates-table tbody tr")
    assert rows.count() >= 7
    shot(page, f"message-templates-{theme}-laptop")
    page.locator("#template-reminder_due a").first.click()
    page.wait_for_url(re.compile(r"/admin-settings/templates/.+/"))
    expect(page.locator("#template-placeholders")).to_contain_text("{operator_name}")
    page.select_option("#template-store", index=1)
    page.locator("#template-preview-button").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#preview-body")).to_contain_text("Ayşe Kaya")
    shot(page, f"message-template-edit-preview-{theme}-laptop")
    assert OutboundMessage.objects.count() == 1  # the preview queued nothing
    # The Outbox: the held message, its reason, and Send now refused.
    page.goto(f"{live_server.url}/admin-settings/outbox/")
    expect(page.locator(f"#message-{message.pk}")).to_contain_text("not sent (mail not set up)")
    expect(page.locator("#outbox-not-set-up")).to_be_visible()
    shot(page, f"outbox-{theme}-laptop")
    page.locator(f"#message-{message.pk} button", has_text="Send now").click()
    page.wait_for_load_state("networkidle")
    assert "stays held" in page.locator("main").inner_text()
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    # The store timeline lists the e-mail with its result.
    page.goto(f"{live_server.url}{store_path}?tab=contacts&show=emails")
    expect(page.locator(f"#message-{message.pk}")).to_contain_text("not sent (mail not set up)")
    shot(page, f"store-timeline-email-{theme}-laptop")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_outbox_and_the_mail_card_on_the_phone(
    live_server: LiveServer, phone: BrowserContext, theme: str
) -> None:
    message, _ = a_world()
    page = signed_in(phone, live_server, f"e2e-14d-phone-{theme}", theme, Role.ADMIN)
    page.goto(f"{live_server.url}/admin-settings/outbox/")
    expect(page.locator(f"#message-{message.pk}")).to_be_visible()
    shot(page, f"outbox-{theme}-phone")
    page.goto(f"{live_server.url}/admin-settings/#mail")
    expect(page.locator("#mail-card")).to_be_visible()
    shot(page, f"admin-settings-mail-{theme}-phone")


def test_below_admin_the_outbox_is_refused(live_server: LiveServer, laptop: BrowserContext) -> None:
    a_world()
    page = signed_in(laptop, live_server, "e2e-14d-billing", "light", Role.BILLING)
    response = page.goto(f"{live_server.url}/admin-settings/outbox/")
    assert response is not None and response.status in (403, 404) or "/account/" in page.url
    assert page.locator("#outbox").count() == 0
