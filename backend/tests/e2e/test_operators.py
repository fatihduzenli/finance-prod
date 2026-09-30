"""Create an operator in a browser with a double click (docs/qa/PLAN-006-operators.md §5).

The live server runs in this process, so replacing ``payments.get_provider`` gives the
views an in-memory provider: no request leaves the machine.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import Operator, SyncStatus
from payments.fake import FakeProvider
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


def test_double_click_on_save_creates_one_operator_and_one_customer(
    live_server: LiveServer, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    _, device = make_user("e2e-billing", Role.BILLING)
    assert device is not None

    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", "e2e-billing")
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    token_field = page.locator("#id_token-otp_token")
    token_field.wait_for(state="visible")
    token_field.fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/admin/")

    page.goto(f"{live_server.url}/operators/new/")
    page.fill("#id_display_name", "Double Click Stores")
    page.fill("#id_email", "double.click@example.com")
    page.locator("#save-operator").dblclick()

    page.wait_for_url(re.compile(r".*/operators/[0-9a-f-]{36}/$"))
    page.locator("#sync-status").wait_for(state="visible")
    assert page.locator("#sync-status").inner_text().startswith("Linked")
    assert page.locator("#sandbox-banner").is_visible()

    operator = Operator.objects.get()
    assert operator.display_name == "Double Click Stores"
    assert operator.sync_status == SyncStatus.LINKED
    assert [name for name, _ in provider.calls].count("create_customer") == 1
