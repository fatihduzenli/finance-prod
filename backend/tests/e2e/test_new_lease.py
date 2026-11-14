"""PLAN-014b item 1 in a browser (Chromium; WebKit in the CI image only): the New lease page's
live proration line equals ``proration.prorated`` for 2026-11-15 and $2,000.00, the Summary's
First invoice is that plus the move-in charges, the operator search filters the options and the
operator card fills, "+ Add item" reveals a row. The QA shots (``QA_SHOTS_DIR``) come from here
in both themes at both widths."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error,
    Page,
    Playwright,
    ViewportSize,
    expect,
)
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from accounts.themes import set_theme
from ledger.models import FeeType, Store, StoreStatus
from ledger.services import proration
from ledger.wording import format_amount
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1440, "height": 900}
PHONE: ViewportSize = {"width": 390, "height": 844}
BROWSERS = [
    "chromium",
    pytest.param(
        "webkit",
        marks=pytest.mark.skipif(
            os.environ.get("E2E_WEBKIT") != "1", reason="WebKit e2e runs in the CI image"
        ),
    ),
]


@pytest.fixture(params=BROWSERS)
def any_browser(request: pytest.FixtureRequest, playwright: Playwright) -> Iterator[Browser]:
    name: str = request.param
    try:
        browser = getattr(playwright, name).launch()
    except Error as error:
        pytest.skip(f"{name} is not installed (uv run playwright install {name}): {error}")
    yield browser
    browser.close()


def shot(page: Page, name: str) -> None:
    folder = os.environ.get("QA_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)


def signed_in(context: BrowserContext, live_server: LiveServer, username: str, theme: str) -> Page:
    user, device = make_user(username, Role.ADMIN)
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


def a_world() -> tuple[Store, str]:
    """A vacant #437 and Fatih Duzenli with three other stores; the fee types' usual prices
    (a transactional test keeps the migration's catalogue)."""
    store = Store.objects.create(
        external_label="437 Test Store 5", store_number=437, status=StoreStatus.ACTIVE
    )
    operator = an_operator("Fatih Duzenli")
    operator.email, operator.phone = "a@b.com", "2015550142"
    operator.save()
    for number in (1991, 1999, 2475):
        a_lease(a_store(str(number), number=number), operator, started=date(2026, 1, 1))
    an_operator("Ayşe Kaya")
    # A transactional test flushes the migration's catalogue: the reference's items, again.
    for order, (code, name, price, refundable, setup) in enumerate(
        (
            ("security_deposit", "Security deposit", "3000.00", True, True),
            ("sign_fee", "Sign fee", "500.00", False, True),
            ("insurance_2026", "Insurance 2026–27", "300.00", False, True),
            ("access_fee", "Access fee", "1500.00", False, True),
            ("architecture_fee", "Architecture fee", None, False, False),
        )
    ):
        FeeType.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "default_price": Decimal(price) if price else None,
                "refundable": refundable,
                "show_in_setup": setup,
                "active": True,
                "sort_order": order,
            },
        )
    return store, str(operator.pk)


def fill_the_reference(page: Page, operator: str) -> None:
    page.fill("#operator-search", "fatih")
    page.select_option("#id_operator", operator)
    page.fill("#id_started_on", "2026-11-15")
    page.fill("#id_rent", "2000.00")
    page.fill("#id_setup_security_deposit_price", "3000.00")
    page.fill("#id_setup_sign_fee_price", "500.00")
    page.fill("#id_setup_insurance_2026_price", "300.00")


def test_the_proration_line_and_the_summary_match_the_service(
    live_server: LiveServer, any_browser: Browser
) -> None:
    store, operator = a_world()
    context = any_browser.new_context(viewport=LAPTOP)
    page = signed_in(context, live_server, "e2e-new-lease", "light")
    page.goto(f"{live_server.url}/leases/new/?store={store.pk}")
    # The search filters the options; the card fills from the chosen option.
    page.fill("#operator-search", "fatih")
    assert page.locator("#id_operator option:not([hidden])").all_inner_texts()[1:] == [
        "Fatih Duzenli"
    ]
    expect(page.locator("#operator-card")).to_be_hidden()
    fill_the_reference(page, operator)
    expect(page.locator("#operator-card")).to_be_visible()
    assert page.locator("#operator-card-initials").inner_text() == "FD"
    assert page.locator("#operator-card-line").inner_text() == (
        "(201) 555-0142 · a@b.com · 3 other stores: #1991, #1999, #2475"
    )
    expected = proration.prorated(Decimal("2000.00"), date(2026, 11, 1), 15, 30)
    line = page.locator("#prorate-line")
    expect(line).to_have_text(f"Nov 15–30: 16 of 30 days = {format_amount(expected.amount)}")
    assert line.inner_text() == "Nov 15–30: 16 of 30 days = $1,066.67"
    # Re-pinned (Opus, PLAN-015j §9.1): the deposit is typed in its own row; no mirror.
    assert page.locator("#deposit-mirror").count() == 0
    assert page.locator(
        "#setup-security_deposit #id_setup_security_deposit_price"
    ).input_value() == ("3000.00")
    assert page.locator("#summary-first-month").inner_text() == "$1,066.67"
    assert page.locator("#setup-total").inner_text() == "$3,800.00"
    assert page.locator("#summary-total").inner_text() == format_amount(
        expected.amount + Decimal("3800.00")
    )
    assert page.locator("#summary-collected").inner_text() == "Autopay · 1st"
    # Off, the line hides and the first month is the rent.
    page.locator("#id_prorate_first_month").uncheck()
    expect(line).to_be_hidden()
    assert page.locator("#summary-first-month").inner_text() == "$2,000.00"
    # "+ Add item" reveals the hidden row of a fee type outside the checklist.
    extra = page.locator("#setup-architecture_fee")
    expect(extra).to_be_hidden()
    page.locator("#add-item").click()
    expect(extra).to_be_visible()
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", ["laptop", "phone"])
def test_the_page_in_both_themes_and_widths(
    live_server: LiveServer, browser: Browser, theme: str, width: str
) -> None:
    store, operator = a_world()
    context = (
        browser.new_context(viewport=LAPTOP)
        if width == "laptop"
        else browser.new_context(
            viewport=PHONE, device_scale_factor=3, is_mobile=True, has_touch=True
        )
    )
    page = signed_in(context, live_server, f"e2e-new-lease-{theme}-{width}", theme)
    page.goto(f"{live_server.url}/leases/new/?store={store.pk}")
    fill_the_reference(page, operator)
    expect(page.locator("#billing-card")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, f"new-lease-{theme}-{width}")
    context.close()
