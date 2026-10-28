"""PLAN-012f-c Batch A in a browser (plan §3 D5, D6): on a payment recorded outside the
processor, billing opens "More ▾ › Void payment", the dialog shows over the payment page with
its consequence sentence and the Reason field, Keep closes it with nothing written, and the void
lands back on the payment page reading Voided with who, when and why. The QA screenshots
(``QA_SHOTS_DIR``) come from here at both widths."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import Payment, PaymentMethod, PaymentStatus, RentChargeStatus
from ledger.services import rent_run
from payments.fake import FakeProvider
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.text_helpers import has_token

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}
DAY = date(2026, 10, 15)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def shot(page: Page, name: str) -> None:
    """A QA screenshot when QA_SHOTS_DIR is set (docs/qa/12f-c/shots): never in CI."""
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
    username = "e2e-12fc-a-billing"
    _, device = make_user(username, Role.BILLING)
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


def a_wire_payment() -> Payment:
    lease = a_lease(a_store(), an_operator("Acme Stores"))
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, "1500.00")
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.WIRE,
        amount=Decimal("1500.00"),
        paid_on=DAY,
        note="Wire received from the operator's bank.",
        actor=None,
        today=DAY,
    )
    assert outcome.ok and outcome.payment is not None, outcome.message
    return outcome.payment


def void_through_the_browser(page: Page, live_server: LiveServer, width: str) -> None:
    payment = a_wire_payment()
    detail = live_server.url + reverse("ledger:payments:detail", kwargs={"pk": payment.pk})
    page.goto(detail)
    page.locator("#more-menu summary").click()
    item = page.locator("#void-manual-payment")
    item.wait_for()
    shot(page, f"payment-more-menu-{width}")
    item.click()
    page.wait_for_url(lambda url: "/void-manual/" in url)
    dialog = page.locator("dialog#void-manual-dialog")
    assert dialog.evaluate("d => d.open")
    assert "Voiding returns $1,500.00" in dialog.inner_text()
    shot(page, f"void-manual-dialog-{width}")
    # Keep writes nothing and returns to the payment page.
    page.locator("#void-manual-keep").click()
    page.wait_for_url(detail)
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED
    # Void with a reason.
    page.goto(f"{detail}void-manual/")
    page.fill("#id_reason", "Recorded twice by mistake.")
    page.locator("#void-manual-submit").click()
    page.wait_for_url(detail)
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.VOIDED
    assert payment.void_reason == "Recorded twice by mistake."
    body = page.content()
    assert has_token(body, "Voided") and "Recorded twice by mistake." in body
    assert "void-manual-payment" not in body  # no action left
    shot(page, f"payment-voided-{width}")


def test_billing_voids_a_wire_payment_on_a_laptop(
    fake: FakeProvider, live_server: LiveServer, laptop: BrowserContext
) -> None:
    page = signed_in(laptop, live_server)
    void_through_the_browser(page, live_server, "laptop")


def test_billing_voids_a_wire_payment_on_a_phone(
    fake: FakeProvider, live_server: LiveServer, phone: BrowserContext
) -> None:
    page = signed_in(phone, live_server)
    void_through_the_browser(page, live_server, "phone")
    found = page.locator("dialog#void-manual-dialog")
    page.goto(page.url + "void-manual/")
    box = found.bounding_box()
    assert box is not None and box["width"] >= PHONE["width"] - 2
