"""PLAN-014 Batch C in a browser (Fable): the LEASE card's Billing and Bank rows and "Edit
billing details"; the dialog's save; a sharer's "Shared with #…"; New lease's Billing details
card with "Copy from operator" filling the fields only when pressed; the operator form with its
four fields; the bank-entry page on the fake hosted fields. The QA shots (``QA_SHOTS_DIR``) come
from here in both themes at both widths for checkpoint 1."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from django.utils import timezone
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from accounts.themes import set_theme
from ledger.models import Lease, LeaseBilling
from payments import lease_sync
from payments.models import ExternalRef, ProviderSnapshot
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


def a_ref(lease: Lease, kind: str, external_id: str) -> ExternalRef:
    return ExternalRef.objects.create(
        entity_type=lease_sync.ENTITY_LEASE,
        entity_id=lease.pk,
        provider=lease_sync.PROVIDER_KEY,
        external_kind=kind,
        external_id=external_id,
    )


def a_world() -> tuple[Lease, Lease]:
    """Ayşe Kaya runs #1601 (its own customer and bank) and #1602 (sharing #1601's account)."""
    operator = an_operator("Ayşe Kaya")
    operator.email = "ayse@example.com"
    operator.phone = "2125550100"
    operator.address_line1 = "1 Main St"
    operator.city = "Newark"
    operator.state = "NJ"
    operator.postal_code = "07102"
    operator.save()
    primary = a_lease(a_store("1601", number=1601), operator, started=date(2026, 1, 1))
    customer = a_ref(primary, lease_sync.KIND_CUSTOMER, "cust-1601")
    a_ref(primary, lease_sync.KIND_PAYMENT_METHOD, "pm-1601")
    # The method list as the worker's refresh keeps it: a usable bank method, so the account
    # is shareable ("Use the same account as #1601 (····4821)").
    ProviderSnapshot.objects.create(
        external_ref=customer,
        fetched_at=timezone.now(),
        payload=[
            {
                "external_id": "pm-1601",
                "raw_status": "",
                "method": "bank",
                "last_four": "4821",
                "bank_type": "checking",
                "nickname": "",
                "is_default": True,
            }
        ],
    )
    LeaseBilling.objects.create(
        lease=primary,
        company_name="Kaya Repairs LLC",
        email="store1601@example.com",
        bank_last4="4821",
    )
    sharer = a_lease(a_store("1602", number=1602), operator, started=date(2026, 3, 1))
    LeaseBilling.objects.create(lease=sharer, shares_account_of=primary)
    a_rent_charge(primary, date(2026, 9, 1), due="1000.00")
    return primary, sharer


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
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_lease_card_and_the_billing_dialog_on_the_laptop(
    live_server: LiveServer, laptop: BrowserContext, theme: str
) -> None:
    primary, sharer = a_world()
    page = signed_in(laptop, live_server, f"e2e-14c-laptop-{theme}", theme, Role.BILLING)
    page.goto(f"{live_server.url}/stores/{primary.store_id}/")
    card = page.locator("#lease-card")
    assert "Kaya Repairs LLC" in card.locator("#header-billing").inner_text()
    assert "store1601@example.com" in card.locator("#header-billing").inner_text()
    assert "4821" in card.locator("#header-bank").inner_text()
    assert card.locator("#header-bank-shared").count() == 0
    shot(page, f"store-lease-card-billing-{theme}-laptop")
    # The sharer names the account it uses.
    page.goto(f"{live_server.url}/stores/{sharer.store_id}/")
    expect(page.locator("#header-bank-shared")).to_contain_text("Shared with #1601")
    assert page.locator("#lease-bank-entry").count() == 0  # a sharer enters no bank of its own
    # The dialog: save a new billing e-mail; the card follows.
    page.locator("#edit-billing").click()
    page.wait_for_url(re.compile(r"/leases/.+/billing/"))
    expect(page.locator("#billing-save-form")).to_be_visible()
    expect(page.locator("#billing-shares")).to_contain_text("1601")
    assert page.locator("#billing-link-form").count() == 0
    shot(page, f"lease-billing-dialog-{theme}-laptop")
    page.fill("#billing-save-form input[name=email]", "store1602@example.com")
    page.locator("#save-billing").click()
    page.wait_for_url(re.compile(r"/leases/.+/billing/"))
    page.goto(f"{live_server.url}/stores/{sharer.store_id}/")
    expect(page.locator("#header-billing")).to_contain_text("store1602@example.com")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_new_lease_copies_from_the_operator_only_when_asked(
    live_server: LiveServer, laptop: BrowserContext, theme: str
) -> None:
    primary, _ = a_world()
    page = signed_in(laptop, live_server, f"e2e-14c-new-{theme}", theme, Role.ADMIN)
    a_store("1603", number=1603)
    page.goto(f"{live_server.url}/leases/new/")
    billing = page.locator("#billing-card")
    page.select_option("#id_operator", str(primary.operator_id))
    expect(billing.locator("input[name=email]")).to_be_visible()
    assert billing.locator("input[name=email]").input_value() == ""
    page.locator("#copy-from-operator").click()
    assert billing.locator("input[name=email]").input_value() == "ayse@example.com"
    assert billing.locator("input[name=phone]").input_value().endswith("5550100")
    assert billing.locator("input[name=city]").input_value() == "Newark"
    assert billing.locator("input[name=company_name]").input_value() == ""  # never copied
    same = page.locator("input[name=account][value^='same:']")
    expect(same.first).to_be_visible()
    assert (
        "1601" in page.locator("label", has=same.first).inner_text()
        or "1601" in page.locator("#billing-card").inner_text()
    )
    shot(page, f"new-lease-billing-{theme}-laptop")


def test_the_operator_form_has_four_fields(live_server: LiveServer, laptop: BrowserContext) -> None:
    page = signed_in(laptop, live_server, "e2e-14c-operator", "light", Role.ADMIN)
    page.goto(f"{live_server.url}/operators/new/")
    expect(page.locator("#id_display_name")).to_be_visible()
    for gone in ("#id_kind_0", "#id_company", "#id_contact_person", "#id_legal_name"):
        assert page.locator(gone).count() == 0, gone
    shot(page, "operator-form-light-laptop")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_store_page_and_the_dialog_on_the_phone(
    live_server: LiveServer, phone: BrowserContext, theme: str
) -> None:
    primary, _ = a_world()
    page = signed_in(phone, live_server, f"e2e-14c-phone-{theme}", theme, Role.BILLING)
    page.goto(f"{live_server.url}/stores/{primary.store_id}/")
    billing_row = page.locator("#header-billing")
    billing_row.scroll_into_view_if_needed()
    expect(billing_row).to_contain_text("store1601@example.com")
    shot(page, f"store-lease-card-billing-{theme}-phone")
    page.locator("#edit-billing").tap()
    page.wait_for_url(re.compile(r"/leases/.+/billing/"))
    expect(page.locator("#billing-save-form")).to_be_visible()
    shot(page, f"lease-billing-dialog-{theme}-phone")


def test_the_bank_entry_page_says_not_set_up_without_hosted_fields(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    primary, _ = a_world()
    page = signed_in(laptop, live_server, "e2e-14c-bank", "light", Role.BILLING)
    page.goto(f"{live_server.url}/leases/{primary.pk}/bank/")
    body = page.locator("main").inner_text()
    if page.locator("input[name=payment_method_id]").count():
        # The fake hosted fields (STAX_PROVIDER=fake): one plain field, no script from a
        # processor host.
        assert not page.locator("script[src*='fattpay']").count()
    else:
        assert "not set up" in body
        assert not page.locator("script[src*='fattpay']").count()
    assert "routing number" not in body.lower()
    assert page.locator("input[name*=routing]").count() == 0
    shot(page, "lease-bank-entry-light-laptop")
