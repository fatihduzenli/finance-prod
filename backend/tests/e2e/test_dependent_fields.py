"""Dependent fields in a browser (docs/qa/PLAN-012c-design-pass.md D8; §6 C).

``ledger/shown_when.js`` shows a field only while the control it depends on allows it, and
does not send it while hidden: the operator form's consents (a phone or an e-mail), the
lease form's second debit day ("Debited twice a month") and the lease-end reason note
(reason Other). ``ledger/payment_allocation.js`` shows the keep-credit tick only while
something is unallocated. The invoice builder's How to collect (PLAN-012e-2 D3) shows each
choice's fields and button only while its radio is chosen. Without JavaScript every field
stays visible and the tick that only drives the script stays hidden.

PLAN-012e-3 D3: a manual payment's reference is labelled "Check number" while Check is the
method chosen, and "Reference" otherwise and without JavaScript. D5: the builder's lines
table scrolls inside its region, so the draft page is no wider than a 1,280 px viewport.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from playwright.sync_api import Browser, BrowserContext, Page
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import PaymentChannel
from ledger.services import invoice_builder, lease_end
from ledger.services.invoice_builder import LineInput
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
    # The button is a flex box since PLAN-012f (its spans read as lines in innerText).
    assert "Schedule $125.50 for" in " ".join(schedule.inner_text().split())
    page.fill("#id_schedule_on", "2026-12-01")
    assert " ".join(schedule.inner_text().split()) == "Schedule $125.50 for 2026-12-01"
    page.check("#collect-record")
    assert method.is_visible() and page.locator("#id_payment_note").is_visible()
    assert not date_field.is_visible() and date_field.is_disabled()
    assert " ".join(record.inner_text().split()) == "Record $125.50 received"
    page.check("#collect-email")
    assert " ".join(send.inner_text().split()) == "Send $125.50 by e-mail"
    assert not record.is_visible()


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
    # PLAN-012e-3: without the script the Rent columns stay visible (the region scrolls).
    assert page.locator("#draft-lines th.rent-col").first.is_visible()
    assert page.locator("#id_lines-0-rent_lease").is_visible()


def _reference_label(page: Page) -> str:
    return page.locator('label[for="id_reference"]').inner_text().strip()


def test_the_reference_label_reads_check_number_while_check_is_chosen(
    live_server: LiveServer, page: Page
) -> None:
    lease = a_lease(a_store("E2E-12E3-1"), an_operator("Check Co"), channel=PaymentChannel.WIRE)
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "250.00", date(2026, 9, 20), lease=lease)
    _sign_in(page, live_server, "e2e-12e3-check")
    page.goto(f"{live_server.url}/one-off-charges/{charge.pk}/payments/new/")
    assert _reference_label(page) == "Reference"
    page.select_option("#id_method", "check")
    assert _reference_label(page) == "Check number"
    page.select_option("#id_method", "wire")
    assert _reference_label(page) == "Reference"
    # The store's Add payment, with its allocation script on the same page.
    page.goto(f"{live_server.url}/stores/{lease.store_id}/payments/new/")
    page.select_option("#id_method", "check")
    assert _reference_label(page) == "Check number"
    # The builder's Record payment now.
    page.goto(f"{live_server.url}/invoices/new/store/{lease.store_id}/")
    page.check("#collect-record")
    assert _reference_label(page) == "Reference"
    page.select_option("#id_method", "check")
    assert _reference_label(page) == "Check number"
    page.select_option("#id_method", "zelle")
    assert _reference_label(page) == "Reference"


def test_without_javascript_the_reference_stays_reference_and_its_help_names_the_check(
    live_server: LiveServer, no_script: BrowserContext
) -> None:
    lease = a_lease(a_store("E2E-12E3-2"), an_operator("No Script Check"))
    assert lease.operator is not None
    charge = a_one_off_charge(lease.operator, "250.00", date(2026, 9, 20), lease=lease)
    page = no_script.new_page()
    _sign_in(page, live_server, "e2e-12e3-noscript")
    page.goto(f"{live_server.url}/one-off-charges/{charge.pk}/payments/new/")
    page.select_option("#id_method", "check")
    assert _reference_label(page) == "Reference"
    assert "a check number" in page.locator("#id_reference + .help").inner_text()


def test_the_draft_page_is_no_wider_than_a_laptop_viewport(
    live_server: LiveServer, browser: Browser
) -> None:
    lease = a_lease(a_store("E2E-12E3-3"), an_operator("Wide Lines Co"), started=date(2026, 1, 1))
    assert lease.operator is not None
    user, _ = make_user("e2e-12e3-draft-owner", Role.BILLING)
    lines = [
        LineInput(f"Sign repair on the north wall, panel {number}", "", None, Decimal("75.00"))
        for number in range(1, 4)
    ]
    outcome = invoice_builder.create_draft(lease.operator, lease, "", None, lines, user)
    assert outcome.ok and outcome.draft is not None, outcome.message
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        page = context.new_page()
        _sign_in(page, live_server, "e2e-12e3-draft")
        page.goto(f"{live_server.url}/invoices/drafts/{outcome.draft.pk}/")
        region = page.locator(".table-scroll:has(> #draft-lines)")
        assert region.count() == 1
        assert page.evaluate("document.documentElement.scrollWidth") == 1280
        # No line is Rent: the Rent columns are collapsed and line 1's amount is on screen.
        rent_header = page.locator("#draft-lines th.rent-col").first
        assert not rent_header.is_visible()
        assert page.locator("#draft-lines").get_attribute("class") in (None, "")
        unit_right = page.evaluate(
            "document.getElementById('id_lines-0-unit_amount').getBoundingClientRect().right"
        )
        assert 0 < unit_right <= 1280
        # Choosing Rent on a line opens the columns; another item closes them again.
        page.fill("#id_lines-0-item", "Rent")
        assert rent_header.is_visible()
        assert page.locator("#id_lines-0-rent_month").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth") == 1280
        page.fill("#id_lines-0-item", "Sign repair")
        assert not rent_header.is_visible()
        # The builder's script still runs on the table inside the region.
        assert page.locator("#add-line").is_visible()
        rows = page.locator("#draft-lines tr.draft-line:visible").count()
        page.locator("#add-line").click()
        assert page.locator("#draft-lines tr.draft-line:visible").count() == rows + 1
        assert page.evaluate("document.documentElement.scrollWidth") == 1280
    finally:
        context.close()
