"""PLAN-012f Batch D in a browser (docs/qa/PLAN-012f-approved-design.md D10, D13; DESIGN-SPEC §6).

The login page at 1,280 x 800: the brand panel on the left half, the banner strip, "Sign in" and
the form in the right half, the banner above the form; at 390 x 844: a short gradient header
with the logo and the headline over the banner strip and the form, which takes the width. A
form's primary button stays in view above the tab bar on a phone while the form is longer than
the screen. A wizard step shows its step pills on a laptop and "Step N of M" on a phone. Every
other page fits a phone's width: nothing scrolls sideways but a scroll region.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext, FloatRect, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import PaymentChannel
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

PHONE: ViewportSize = {"width": 390, "height": 844}
LAPTOP: ViewportSize = {"width": 1280, "height": 800}
TAB_BAR = 64


@pytest.fixture
def phone(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(
        viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    yield context
    context.close()


@pytest.fixture
def laptop(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport=LAPTOP)
    yield context
    context.close()


def box(page: Page, selector: str) -> FloatRect:
    found = page.locator(selector).bounding_box()
    assert found is not None, selector
    return found


def signed_in(context: BrowserContext, live_server: LiveServer, role: Role) -> Page:
    username = f"e2e-12fd-{role.value}"
    _, device = make_user(username, role)
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


def test_the_login_page_on_a_laptop_has_its_two_panels(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    page = laptop.new_page()
    page.goto(f"{live_server.url}/account/login/")
    brand = box(page, "#auth-brand")
    assert brand["x"] == pytest.approx(0, abs=1)
    assert brand["width"] == pytest.approx(LAPTOP["width"] / 2, abs=2)
    assert brand["height"] >= LAPTOP["height"] - 1
    assert page.locator("#auth-points").is_visible() and page.locator("#auth-company").is_visible()
    banner = box(page, "#sandbox-banner")
    heading = box(page, "#sign-in-heading")
    field = box(page, "#id_auth-username")
    button = box(page, "#auth-main .btn-primary")
    # The banner is a strip of the right panel, over "Sign in" and the form, 400 px wide.
    assert banner["x"] >= LAPTOP["width"] / 2 and banner["width"] == pytest.approx(400, abs=2)
    assert banner["y"] + banner["height"] <= heading["y"] < field["y"] < button["y"]
    assert field["width"] == pytest.approx(400, abs=2) and button["width"] == pytest.approx(
        400, abs=2
    )
    assert page.evaluate("document.documentElement.scrollWidth") <= LAPTOP["width"]


def test_the_login_page_on_a_phone_is_a_short_header_over_the_form(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    page = phone.new_page()
    page.goto(f"{live_server.url}/account/login/")
    brand = box(page, "#auth-brand")
    assert brand["width"] == pytest.approx(PHONE["width"], abs=1)
    assert brand["height"] < 200
    assert page.locator("#auth-headline").is_visible()
    assert not page.locator("#auth-points").is_visible()
    banner = box(page, "#sandbox-banner")
    field = box(page, "#id_auth-username")
    assert brand["y"] + brand["height"] <= banner["y"] < field["y"]
    assert field["x"] == pytest.approx(16, abs=1)
    assert field["width"] == pytest.approx(PHONE["width"] - 32, abs=2)
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def test_a_forms_primary_button_stays_in_view_above_the_tab_bar(
    live_server: LiveServer, phone: BrowserContext
) -> None:
    page = signed_in(phone, live_server, Role.ADMIN)
    page.goto(f"{live_server.url}{reverse('ledger:stores:create')}")
    form = box(page, "#store-form")
    assert form["y"] + form["height"] > PHONE["height"]  # longer than the screen
    button = box(page, "#save-store")
    # Sticky 90 px above the bottom: over the tab bar, in view before scrolling.
    assert button["y"] + button["height"] <= PHONE["height"] - TAB_BAR
    assert page.locator("#save-store").is_visible()


def test_a_wizard_step_reads_step_n_of_m_on_a_phone_and_shows_its_pills_on_a_laptop(
    live_server: LiveServer, phone: BrowserContext, laptop: BrowserContext
) -> None:
    from tests.test_lease_end import draft

    lease = a_lease(a_store("0912 STEP", 912), an_operator("Step Co"), channel=PaymentChannel.WIRE)
    end = draft(lease)
    path = reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk})
    small = signed_in(phone, live_server, Role.ADMIN)
    small.goto(f"{live_server.url}{path}")
    assert small.locator("#wizard-step-line").is_visible()
    assert small.locator("#wizard-step-line").inner_text().startswith("Step ")
    assert not small.locator("#wizard-steps").is_visible()
    wide = laptop.new_page()
    wide.context.add_cookies(
        [
            {"name": cookie["name"], "value": cookie["value"], "url": live_server.url}
            for cookie in small.context.cookies()
        ]
    )
    wide.goto(f"{live_server.url}{path}")
    assert wide.locator("#wizard-steps").is_visible()
    assert not wide.locator("#wizard-step-line").is_visible()


def test_the_other_pages_fit_a_phones_width(live_server: LiveServer, phone: BrowserContext) -> None:
    lease = a_lease(a_store("0913 FIT", 913), an_operator("Fit Co"), channel=PaymentChannel.WIRE)
    assert lease.started_on is not None
    charge = a_rent_charge(lease, lease.started_on.replace(day=1))
    payment = a_payment(charge, "100.00", lease.started_on)
    page = signed_in(phone, live_server, Role.ADMIN)
    for path in (
        reverse("ledger:payments:list"),
        reverse("ledger:payments:detail", kwargs={"pk": payment.pk}),
        reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
        reverse("ledger:charges:detail", kwargs={"pk": charge.pk}),
        reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
        reverse("ledger:reports:deposits"),
        reverse("ledger:audit:page"),
        reverse("ledger:run:today"),
    ):
        page.goto(f"{live_server.url}{path}")
        assert page.locator("#breadcrumb, #page-header h1").first.is_visible(), path
        assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"], path
