"""PLAN-012f-b slice C2 in a browser (plan §10 D19): on a rent charge, Add payment offers the
bank-debit card; today's debit opens the Charge now dialog over the rent charge; confirming it
lands on the rent charge, which shows the open debit beside its payments. The QA screenshots
(``QA_SHOTS_DIR``) come from here at both widths; nothing is written at the provider (the fake)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import OneOffCharge, RentCharge
from payments.fake import FakeProvider
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.test_rent_debit import a_rent

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


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


def signed_in(context: BrowserContext, live_server: LiveServer) -> Page:
    username = "e2e-12fb-c2-admin"
    _, device = make_user(username, Role.ADMIN)
    assert device is not None
    page = context.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


def add_payment_of(live_server: LiveServer, rent: RentCharge) -> str:
    return live_server.url + reverse("ledger:charges:record_payment", kwargs={"pk": rent.pk})


def detail_of(live_server: LiveServer, rent: RentCharge) -> str:
    return live_server.url + reverse("ledger:charges:detail", kwargs={"pk": rent.pk})


def debit_today_through_the_browser(
    page: Page, live_server: LiveServer, rent: RentCharge, width: str
) -> None:
    page.goto(add_payment_of(live_server, rent))
    card = page.locator("#how-debit")
    assert card.is_checked() and card.is_enabled()
    assert "A debit is already pending." not in page.content()
    shot(page, f"rent-add-payment-{width}")
    page.locator("#save-payment").click()
    page.wait_for_url(lambda url: "/charge-now/" in url)
    dialog = page.locator("dialog#charge-now-dialog")
    assert dialog.evaluate("d => d.open")
    assert "$1,500.00" in dialog.inner_text()
    assert OneOffCharge.objects.count() == 0  # the confirmation's GET wrote nothing
    shot(page, f"rent-charge-now-{width}")
    page.locator("#charge-now-submit").click()
    page.wait_for_url(detail_of(live_server, rent))
    debit = OneOffCharge.objects.get(source_rent_charge=rent)
    line = page.locator("#open-debit")
    assert "Bank debit of" in line.inner_text() and "$1,500.00" in line.inner_text()
    shot(page, f"rent-charge-open-debit-{width}")
    # The card is greyed with the owner's sentence while the debit is open.
    page.goto(add_payment_of(live_server, rent))
    assert page.locator("#how-debit").is_disabled()
    assert "A debit is already pending." in page.content()
    assert debit.price == rent.amount_due


def test_todays_debit_from_add_payment_on_a_laptop(
    fake: FakeProvider, live_server: LiveServer, laptop: BrowserContext
) -> None:
    rent = a_rent(fake, "1500.00")
    page = signed_in(laptop, live_server)
    debit_today_through_the_browser(page, live_server, rent, "laptop")


def test_todays_debit_from_add_payment_on_a_phone(
    fake: FakeProvider, live_server: LiveServer, phone: BrowserContext
) -> None:
    rent = a_rent(fake, "1500.00")
    page = signed_in(phone, live_server)
    debit_today_through_the_browser(page, live_server, rent, "phone")
    # The dialog is a full-width sheet on a phone: re-open it to measure.
    page.goto(f"{live_server.url}{reverse('ledger:charges:charge_now', kwargs={'pk': rent.pk})}")
    found = page.locator("dialog#charge-now-dialog").bounding_box()
    assert found is not None and found["width"] >= PHONE["width"] - 2
