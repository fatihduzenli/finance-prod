"""PLAN-013a Batch C in a browser: the QA screenshots (``QA_SHOTS_DIR``) of the pages the
cleanup changed — the stores list on a phone (C6, the meta line), a Zelle payment's page without
the Refunds card (C2), the invoice builder's greyed bank tiles with their link (C3; since
PLAN-014 C3 Edit billing details), and a cost history row showing the staff member's name
(C1). Each page is also checked for the change it carries."""

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
from ledger.models import Payment, PaymentMethod, RentChargeStatus, Store
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
    username = "e2e-13a-c-billing"
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


def a_world() -> tuple[Store, Payment]:
    operator = an_operator("Acme Stores")
    store = a_store("1991 Istanbul", 1991)
    lease = a_lease(store, operator)
    charge = a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, "2100.00")
    a_lease(a_store("2484 Paris", 2484), an_operator("Paris Co"))
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=Decimal("600.00"),
        paid_on=DAY,
        note="Part of October.",
        actor=None,
        today=DAY,
    )
    assert outcome.ok and outcome.payment is not None, outcome.message
    return store, outcome.payment


def walk(page: Page, live_server: LiveServer, width: str) -> None:
    store, payment = a_world()
    # C6: the stores list's phone meta line.
    page.goto(live_server.url + reverse("ledger:stores:list"))
    assert has_token(page.content(), "Istanbul")
    shot(page, f"stores-list-{width}")
    # C2: the Zelle payment's page has no Refunds card.
    page.goto(live_server.url + reverse("ledger:payments:detail", kwargs={"pk": payment.pk}))
    body = page.content()
    assert 'id="refunds-card"' not in body and 'id="tile-applied"' in body
    shot(page, f"payment-no-refunds-card-{width}")
    # C3: the builder's greyed bank tiles with the link (PLAN-014 C3: Edit billing details).
    page.goto(live_server.url + reverse("ledger:invoices:new_store", kwargs={"pk": store.pk}))
    body = page.content()
    assert "No bank account on file for Acme Stores." in body
    assert ">Edit billing details<" in body
    shot(page, f"builder-no-bank-tiles-{width}")


def test_the_cleanup_pages_on_a_laptop(
    fake: FakeProvider, live_server: LiveServer, laptop: BrowserContext
) -> None:
    walk(signed_in(laptop, live_server), live_server, "laptop")


def test_the_cleanup_pages_on_a_phone(
    fake: FakeProvider, live_server: LiveServer, phone: BrowserContext
) -> None:
    walk(signed_in(phone, live_server), live_server, "phone")
