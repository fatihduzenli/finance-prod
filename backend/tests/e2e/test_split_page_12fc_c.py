"""PLAN-012f-c Batch C in a browser (plan D13): the Split into installments page in the Add
payment family — the stepper, the live preview that rebuilds as the fields change, the Summary
that follows it, and the confirm that creates the plan. The QA screenshots (``QA_SHOTS_DIR``)
come from here at both widths."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import InstallmentPlan, OneOffCharge
from payments.fake import FakeProvider
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_one_off_charge, a_store
from tests.test_one_off_charges import an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}


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
    username = "e2e-12fc-c-billing"
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


def a_charge(fake: FakeProvider) -> OneOffCharge:
    """A $1,000 one-off charge collected by e-mail, of an operator connected to the
    processor (the fake), so its installments are invoices there."""
    operator = an_operator(fake)
    lease = a_lease(a_store(), operator)
    return a_one_off_charge(operator, "1000.00", date(2026, 9, 20), lease=lease, collection="email")


def split_through_the_browser(
    page: Page, live_server: LiveServer, charge: OneOffCharge, width: str
) -> None:
    page.goto(live_server.url + reverse("ledger:one_off_charges:split", kwargs={"pk": charge.pk}))
    page.locator("#count-more").wait_for()
    assert page.locator("#split-preview").is_hidden()
    rows = page.locator("tr.split-line")
    assert rows.count() == 3
    # $1,000 in three: the last row carries the rounding cent.
    assert page.locator("tr.split-line.is-rounding").count() == 1
    shot(page, f"split-page-{width}")
    # + rebuilds the preview: four rows, the tile and the button follow.
    page.locator("#count-more").click()
    page.wait_for_function("document.querySelectorAll('tr.split-line').length === 4")
    assert "4 ×" in page.locator("#split-installments").inner_text()
    assert "Create 4" in page.locator("#split-confirm").inner_text().replace("\n", " ")
    # $1,000 in four is exact: no rounding row.
    assert page.locator("tr.split-line.is-rounding").count() == 0
    shot(page, f"split-page-four-{width}")
    # Weekly: the dates seven days apart.
    page.locator("label[for='id_cadence_1']").click()
    page.wait_for_timeout(600)
    dates = [page.locator("tr.split-line input[type=date]").nth(i).input_value() for i in range(4)]
    first, second = date.fromisoformat(dates[0]), date.fromisoformat(dates[1])
    assert (second - first).days == 7
    # Create the plan.
    page.locator("#split-confirm").click()
    page.wait_for_url(lambda url: "/split/" not in url)
    plan = InstallmentPlan.objects.get(one_off_charge=charge)
    assert plan.cadence == "weekly"
    assert OneOffCharge.objects.filter(plan=plan).count() == 4


def test_split_into_installments_on_a_laptop(
    fake: FakeProvider, live_server: LiveServer, laptop: BrowserContext
) -> None:
    charge = a_charge(fake)
    split_through_the_browser(signed_in(laptop, live_server), live_server, charge, "laptop")


def test_split_into_installments_on_a_phone(
    fake: FakeProvider, live_server: LiveServer, phone: BrowserContext
) -> None:
    charge = a_charge(fake)
    split_through_the_browser(signed_in(phone, live_server), live_server, charge, "phone")
