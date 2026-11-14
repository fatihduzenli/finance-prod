"""PLAN-015k in a browser (Fable): a reminder e-mail held while mail is not set up is a
read-only row of the Outbox with the author's address, and the reminder's Activity card words
the outcome. With ``QA_SHOTS_DIR`` set, the QA shots of both."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, ViewportSize, expect
from pytest_django.live_server_helper import LiveServer

from ledger.models import AuditLog, CalendarEntry
from ledger.services import calendar
from payments.models import JobEntity
from tests.auth_helpers import make_user
from tests.e2e.test_new_lease import LAPTOP, PHONE, signed_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

ACTION = "calendar.morning_email"


def shot(page: Page, name: str) -> None:
    folder = os.environ.get("QA_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)


def held_reminder(settings: object) -> tuple[CalendarEntry, AuditLog]:
    settings.MAIL_ENABLED = False  # type: ignore[attr-defined]
    from accounts.roles import Role

    author = make_user("reminder-writer", Role.BILLING)[0]
    entry = CalendarEntry.objects.create(
        kind="reminder",
        on=date(2026, 11, 13),
        title="Call the landlord about the roof",
        body="Ask for the repair date.",
        email_morning=True,
        author=author,
    )
    calendar.send_morning_email(JobEntity.CALENDAR_ENTRY, entry.pk, entry.on)
    return entry, AuditLog.objects.get(action=ACTION)


@pytest.mark.parametrize(
    ("viewport", "theme", "tag"), [(LAPTOP, "dark", "laptop"), (PHONE, "light", "phone")]
)
def test_a_held_reminder_is_a_read_only_outbox_row_and_the_activity_says_held(
    live_server: LiveServer,
    browser: Browser,
    settings: object,
    viewport: ViewportSize,
    theme: str,
    tag: str,
) -> None:
    entry, row = held_reminder(settings)
    context = browser.new_context(viewport=viewport)
    page = signed_in(context, live_server, f"e2e-015k-{tag}", theme)
    page.goto(f"{live_server.url}/admin-settings/outbox/")
    staff = page.locator(f"#staff-{row.pk}")
    expect(staff).to_be_visible()
    expect(staff).to_contain_text("reminder-writer@example.com")
    expect(staff).to_contain_text("Reminder: Call the landlord about the roof")
    expect(staff).to_contain_text("Held")
    expect(staff.locator("form")).to_have_count(0)
    expect(staff.get_by_role("button")).to_have_count(0)
    assert "Ask for the repair date." not in page.content()
    shot(page, f"015k-outbox-{tag}-{theme}")
    page.goto(f"{live_server.url}/calendar/{entry.pk}/")
    expect(page.get_by_text("Reminder e-mail held (mail not set up)")).to_be_visible()
    expect(page.get_by_text("Calendar morning email")).to_have_count(0)
    shot(page, f"015k-activity-{tag}-{theme}")
    context.close()
