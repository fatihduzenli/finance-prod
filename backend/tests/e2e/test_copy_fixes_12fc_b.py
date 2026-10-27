"""PLAN-012f-c Batch B in a browser: the QA screenshots (``QA_SHOTS_DIR``) of the pages the
copy and small-UI fixes changed, at both widths — Add payment with the greyed card's two
reasons and the "● Due · $…" badge, the payment page with the partial-payment notice and the
staff name under Recorded by, the operator page with a formatted phone number, and the Payments
help topic. Each page is also checked for the fix it carries."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import Operator, Payment, PaymentMethod, RentCharge, RentChargeStatus
from ledger.services import rent_run
from payments import offline_marking
from payments.fake import FakeProvider
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}
DAY = date(2026, 10, 15)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def flag_off() -> Iterator[None]:
    # The bank-debit card is greyed for two reasons at once: no bank method, the flag off.
    with override_settings(STAX_PAY_INVOICE_ENABLED=False):
        yield


def shot(page: Page, name: str) -> None:
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
    username = "e2e-12fc-b-billing"
    user, device = make_user(username, Role.BILLING)
    user.first_name, user.last_name = "Ayse", "Yilmaz"
    user.save(update_fields=["first_name", "last_name"])
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


def a_world() -> tuple[Operator, RentCharge, Payment]:
    operator = an_operator("Acme Stores")
    operator.phone = "4045551234"
    operator.save(update_fields=["phone"])
    lease = a_lease(a_store(), operator)
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, "2100.00")
    actor = make_user("e2e-12fc-b-recorder", Role.BILLING)[0]
    actor.first_name, actor.last_name = "Mehmet", "Kaya"
    actor.save(update_fields=["first_name", "last_name"])
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=Decimal("600.00"),
        paid_on=DAY,
        note="Part of October.",
        actor=actor,
        today=DAY,
    )
    assert outcome.ok and outcome.payment is not None, outcome.message
    # What a partial manual payment on an invoiced charge leaves behind (the marking is
    # skipped and the charge flagged); the unit tests prove the path, this page shows it.
    charge.refresh_from_db()
    charge.needs_review = True
    charge.review_message = offline_marking.PARTIAL_AMOUNT
    charge.save(update_fields=["needs_review", "review_message", "updated_at"])
    return operator, charge, outcome.payment


def walk(page: Page, live_server: LiveServer, width: str) -> None:
    operator, charge, payment = a_world()
    # Add payment: the badge and the two-line reason.
    page.goto(live_server.url + reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk}))
    body = page.content()
    # The badge: "{status} · {balance}", no trailing "due" (the charge is partly paid here).
    assert "Partially paid · $1,500.00" in body and "$1,500.00 due" not in body
    assert "No bank account on file" in body and "Bank debits are also off" in body
    assert "Never an account number." in body
    shot(page, f"add-payment-two-reasons-{width}")
    # The payment page: the staff name and the partial notice.
    page.goto(live_server.url + reverse("ledger:payments:detail", kwargs={"pk": payment.pk}))
    body = page.content()
    # The name is the text; the e-mail is only the hover title.
    assert 'title="e2e-12fc-b-recorder@example.com">Mehmet Kaya</span>' in body
    assert ">e2e-12fc-b-recorder<" not in body
    assert "Partial payment" in body
    shot(page, f"payment-partial-notice-{width}")
    # The operator page: the phone as a person reads it.
    page.goto(live_server.url + reverse("ledger:operators:detail", kwargs={"pk": operator.pk}))
    assert "(404) 555-1234" in page.content()
    shot(page, f"operator-phone-{width}")
    # The Payments help topic.
    page.goto(live_server.url + reverse("finance:help_topic", kwargs={"slug": "payments"}))
    assert "Payments" in page.content()
    shot(page, f"help-payments-{width}")


def test_the_copy_fixes_on_a_laptop(
    fake: FakeProvider, live_server: LiveServer, laptop: BrowserContext
) -> None:
    walk(signed_in(laptop, live_server), live_server, "laptop")


def test_the_copy_fixes_on_a_phone(
    fake: FakeProvider, live_server: LiveServer, phone: BrowserContext
) -> None:
    walk(signed_in(phone, live_server), live_server, "phone")
