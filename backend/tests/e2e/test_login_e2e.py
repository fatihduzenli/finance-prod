"""Full sign-in in a browser: password, then TOTP, then the admin index (PLAN-003 §5)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

BANNER_TEXT = "SANDBOX — test system, no real money"


def test_password_and_totp_lead_to_admin_index(live_server: LiveServer, page: Page) -> None:
    _, device = make_user("e2e-viewer", Role.VIEWER)
    assert device is not None

    page.goto(f"{live_server.url}/admin/")
    assert page.locator("#sign-in-link").is_visible()  # the 401 page, not a redirect
    page.locator("#sign-in-link").click()

    page.fill("#id_auth-username", "e2e-viewer")
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()

    token_field = page.locator("#id_token-otp_token")
    token_field.wait_for(state="visible")
    assert page.locator("#sandbox-banner").is_visible()
    token_field.fill(current_code(device))
    page.get_by_role("button", name="Next").click()

    page.wait_for_url(f"{live_server.url}/admin/")
    assert page.locator("#site-name").is_visible()
    banner = page.locator("#sandbox-banner")
    assert banner.is_visible()
    assert banner.inner_text().strip() == BANNER_TEXT
    assert page.title().startswith("[SANDBOX] ")
