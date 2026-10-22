"""Dependent fields in a browser (docs/qa/PLAN-012c-design-pass.md D8; §6 C).

``ledger/shown_when.js`` shows a field only while the control it depends on allows it, and
does not send it while hidden: the operator form's consents (a phone or an e-mail), the
lease form's second debit day ("Debited twice a month") and the lease-end reason note
(reason Other). ``ledger/payment_allocation.js`` shows the keep-credit tick only while
something is unallocated. The invoice builder's How to collect (PLAN-012e-2 D3) shows each
choice's fields and button only while its radio is chosen. Without JavaScript every field
stays visible and the tick that only drives the script stays hidden.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from playwright.sync_api import Browser, BrowserContext, Page
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import PaymentChannel
from ledger.services import lease_end
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_one_off_charge, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


def _sign_in(page: Page, live_server: LiveServer, username: str) -> None:
    _, device = make_user(username, Role.ADMIN)
    assert device is not None
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    token_field = page.locator("#id_token-otp_token")
    token_field.wait_for(state="visible")
    token_field.fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")


@pytest.fixture
def no_script(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(java_script_enabled=False)
    yield context
    context.close()


def test_the_operator_consents_follow_the_phone_and_the_email(
    live_server: LiveServer, page: Page
) -> None:
    _sign_in(page, live_server, "e2e-d8-operator")
    page.goto(f"{live_server.url}/operators/new/")
    sms, email = page.locator("#id_sms_consent_at"), page.locator("#id_email_consent_at")
    assert not sms.is_visible() and not email.is_visible()
    assert sms.is_disabled() and email.is_disabled()
    page.fill("#id_email", "consent@example.com")
    assert email.is_visible() and email.is_enabled() and not sms.is_visible()
    page.fill("#id_phone", "4155550132")
    assert sms.is_visible() and sms.is_enabled()
    page.fill("#id_phone", "")
    page.fill("#id_sms_phone", "+14155550132")
    assert sms.is_visible()
    page.fill("#id_email", "")
    assert not email.is_visible() and email.is_disabled()


def test_the_second_debit_day_follows_the_twice_monthly_tick(
    live_server: LiveServer, page: Page
) -> None:
    lease = a_lease(a_store("E2E-D8-1"), an_operator("Twice Co"), debit_day=1)
    _sign_in(page, live_server, "e2e-d8-lease")
    page.goto(f"{live_server.url}/leases/{lease.pk}/edit/")
    tick, second = page.locator("#twice-monthly"), page.locator("#id_second_debit_day")
    assert tick.is_visible() and not tick.is_checked()
    assert not second.is_visible() and second.is_disabled()
    tick.check()
    assert second.is_visible() and second.is_enabled()
    second.fill("15")
    page.locator("#save-lease").click()
    page.wait_for_url(f"{live_server.url}/leases/{lease.pk}/")
    lease.refresh_from_db()
    assert lease.second_debit_day == 15
    # Unticked, the hidden day is not sent: the lease goes back to one debit a month.
    page.goto(f"{live_server.url}/leases/{lease.pk}/edit/")
    assert tick.is_checked() and second.is_visible()
    tick.uncheck()
    assert not second.is_visible()
    page.locator("#save-lease").click()
    page.wait_for_url(f"{live_server.url}/leases/{lease.pk}/")
    lease.refresh_from_db()
    assert lease.second_debit_day is None


def test_the_reason_note_shows_while_the_reason_is_other(
    live_server: LiveServer, page: Page
) -> None:
    lease = a_lease(a_store("E2E-D8-2"), an_operator("Ending Co"), started=date(2026, 1, 1))
    end = lease_end.start(lease, None)
    _sign_in(page, live_server, "e2e-d8-terms")
    page.goto(f"{live_server.url}/lease-ends/{end.pk}/terms/")
    note = page.locator("#id_reason_note")
    assert not note.is_visible()
    page.select_option("#id_reason", "other")
    assert note.is_visible() and note.is_enabled()
    page.select_option("#id_reason", "moved_out")
    assert not note.is_visible() and note.is_disabled()


def test_the_keep_credit_tick_shows_while_something_is_unallocated(
    live_server: LiveServer, page: Page
) -> None:
    lease = a_lease(a_store("E2E-D8-3"), an_operator("Paying Co"), channel=PaymentChannel.WIRE)
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "250.00", date(2026, 9, 20), lease=lease)
    _sign_in(page, live_server, "e2e-d8-payment")
    page.goto(f"{live_server.url}/stores/{lease.store_id}/payments/new/")
    field, tick = page.locator("#keep-credit-field"), page.locator("#id_keep_credit")
    assert not field.is_visible() and tick.is_disabled()
    page.fill("#id_amount", "300.00")
    page.fill(f"#id_alloc_{charge.pk}", "250.00")
    assert field.is_visible() and tick.is_enabled()
    page.fill("#id_amount", "250.00")
    assert not field.is_visible()


def test_without_javascript_every_dependent_field_stays_visible(
    live_server: LiveServer, no_script: BrowserContext
) -> None:
    lease = a_lease(a_store("E2E-D8-4"), an_operator("No Script Co"), channel=PaymentChannel.WIRE)
    end = lease_end.start(lease, None)
    page = no_script.new_page()
    _sign_in(page, live_server, "e2e-d8-noscript")
    page.goto(f"{live_server.url}/operators/new/")
    assert page.locator("#id_sms_consent_at").is_visible()
    assert page.locator("#id_email_consent_at").is_visible()
    page.goto(f"{live_server.url}/leases/{lease.pk}/edit/")
    assert page.locator("#id_second_debit_day").is_visible()
    assert not page.locator("#twice-monthly").is_visible()
    page.goto(f"{live_server.url}/lease-ends/{end.pk}/terms/")
    assert page.locator("#id_reason_note").is_visible()
    page.goto(f"{live_server.url}/stores/{lease.store_id}/payments/new/")
    assert page.locator("#keep-credit-field").is_visible()


def test_how_to_collect_shows_the_chosen_choices_fields_and_button(
    live_server: LiveServer, page: Page
) -> None:
    """PLAN-012e-2 D3: the builder's How to collect shows the debit date only for
    Schedule for a date, the received payment only for Record payment now, and the one
    button of the chosen choice, which names the live total."""
    lease = a_lease(a_store("E2E-12E2-1"), an_operator("Collect Co"), started=date(2026, 1, 1))
    _sign_in(page, live_server, "e2e-12e2-collect")
    page.goto(f"{live_server.url}/invoices/new/store/{lease.store_id}/")
    date_field, method = page.locator("#id_schedule_on"), page.locator("#id_method")
    save, send = page.locator("#save-draft"), page.locator("#send-draft")
    schedule, record = page.locator("#schedule-draft"), page.locator("#record-draft")
    assert page.locator("#collect-draft").is_checked()
    assert not date_field.is_visible() and date_field.is_disabled()
    assert not method.is_visible() and method.is_disabled()
    assert save.is_visible() and not send.is_visible() and not record.is_visible()
    page.fill("#id_lines-0-item", "Sign repair")
    page.fill("#id_lines-0-unit_amount", "125.50")
    page.check("#collect-schedule")
    assert date_field.is_visible() and date_field.is_enabled()
    assert not method.is_visible()
    assert schedule.is_visible() and not save.is_visible()
    assert "Schedule $125.50 for" in schedule.inner_text()
    page.fill("#id_schedule_on", "2026-12-01")
    assert schedule.inner_text() == "Schedule $125.50 for 2026-12-01"
    page.check("#collect-record")
    assert method.is_visible() and page.locator("#id_payment_note").is_visible()
    assert not date_field.is_visible() and date_field.is_disabled()
    assert record.inner_text() == "Record $125.50 received"
    page.check("#collect-email")
    assert send.inner_text() == "Send $125.50 by e-mail" and not record.is_visible()


def test_without_javascript_how_to_collect_shows_every_field_and_button(
    live_server: LiveServer, no_script: BrowserContext
) -> None:
    lease = a_lease(
        a_store("E2E-12E2-2"), an_operator("No Script Collect"), started=date(2026, 1, 1)
    )
    page = no_script.new_page()
    _sign_in(page, live_server, "e2e-12e2-noscript")
    page.goto(f"{live_server.url}/invoices/new/store/{lease.store_id}/?collect=schedule")
    assert page.locator("#collect-schedule").is_checked()
    for control in ("#id_schedule_on", "#id_method", "#id_paid_on", "#id_payment_note"):
        assert page.locator(control).is_visible(), control
    for button in ("#save-draft", "#send-draft", "#schedule-draft", "#record-draft"):
        assert page.locator(button).is_visible(), button
