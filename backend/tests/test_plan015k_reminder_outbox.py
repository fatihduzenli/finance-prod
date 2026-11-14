"""Opus's tests of docs/qa/15/PLAN-015k-reminder-email-outbox-activity.md §5: the reminder and
promise-to-pay e-mails' audit payload (the address, the subject and the held reason, never the
body), their Activity words, and their read-only rows in the Outbox (the merged list, its
filters, its paging over both sources and its bounded query count). Fable's adversarial file
is separate.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

import mail
from accounts.roles import Role
from finance.dates import NEW_YORK
from ledger.models import (
    AuditLog,
    CalendarEntry,
    ContactEntry,
)
from ledger.services import calendar
from mail.memory import MemoryMailSender
from payments.models import JobEntity
from tests.auth_helpers import make_user
from tests.test_collections import mail_set_up, operator
from tests.test_ledger_models import saved

pytestmark = pytest.mark.django_db

DAY = date(2026, 10, 20)
ACTION = "calendar.morning_email"
OUTBOX = "/admin-settings/outbox/"
BODY = "Bring the spare keys to the back office."
NOTE = (
    "Mail is not set up in this environment: every message is held here. Reminder e-mails to "
    "staff are listed and cannot be sent later."
)


def at(day: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    def set_now(moment: datetime) -> None:
        monkeypatch.setattr(timezone, "now", lambda: moment)

    return set_now


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, settings: Any) -> MemoryMailSender:
    mail_set_up(settings)
    sender = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: sender)
    return sender


def author(name: str = "reminder-author") -> Any:
    return make_user(name, Role.BILLING)[0]


def entry(owner: Any, *, title: str = "Call the landlord", on: date = DAY) -> CalendarEntry:
    return CalendarEntry.objects.create(
        kind="reminder", on=on, title=title, body=BODY, email_morning=True, author=owner
    )


def promise(owner: Any, *, shop: str = "Promising Shop", on: date = DAY) -> ContactEntry:
    return saved(
        ContactEntry(
            operator=operator(display_name=shop),
            kind="promise",
            happened_on=on - timedelta(days=4),
            body="Will pay Friday.",
            promised_date=on,
            promised_amount=Decimal("1500.00"),
            author=owner,
        )
    )


def send(item: CalendarEntry | ContactEntry, on: date = DAY) -> AuditLog:
    kind = JobEntity.CALENDAR_ENTRY if isinstance(item, CalendarEntry) else JobEntity.CONTACT_ENTRY
    calendar.send_morning_email(kind, item.pk, on)
    return AuditLog.objects.filter(action=ACTION, entity_id=str(item.pk)).latest("id")


def staff_row(item: CalendarEntry | ContactEntry | None, after: Any, **fields: Any) -> AuditLog:
    """An audit row of the action as an older (or broken) writer left it."""
    kind = "contact_entry" if isinstance(item, ContactEntry) else "calendar_entry"
    return AuditLog.objects.create(
        action=ACTION,
        entity_type=fields.pop("entity_type", kind),
        entity_id=str(item.pk if item is not None else uuid.uuid4()),
        after=after,
    )


def all_audit_text() -> str:
    return json.dumps(list(AuditLog.objects.values("before", "after")), default=str)


# --- Task 1: the audit payload ----------------------------------------------------------------


def test_a_sent_reminder_stores_the_address_and_subject_and_never_the_body(
    memory: MemoryMailSender,
) -> None:
    row = send(entry(author()))
    assert row.after == {
        "outcome": "sent",
        "on": "2026-10-20",
        "to": "reminder-author@example.com",
        "subject": "Reminder: Call the landlord",
    }
    assert [m.subject for m in memory.sent] == ["Reminder: Call the landlord"]
    assert BODY not in all_audit_text()


def test_a_reminder_held_while_mail_is_not_set_up_stores_the_reason(settings: Any) -> None:
    settings.MAIL_ENABLED = False
    row = send(entry(author()))
    assert row.after == {
        "outcome": "held",
        "on": "2026-10-20",
        "to": "reminder-author@example.com",
        "subject": "Reminder: Call the landlord",
        "reason": "not_set_up",
    }
    assert BODY not in all_audit_text()


def test_a_reminder_held_off_the_sandbox_allowlist_stores_the_reason(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mail_set_up(settings, sandbox=True, allowlist="someone.else@example.com")
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    row = send(entry(author()))
    assert row.after["outcome"] == "held" and row.after["reason"] == "sandbox"
    assert row.after["to"] == "reminder-author@example.com"
    assert memory.sent == []


def test_a_failed_reminder_stores_the_address_subject_and_error(
    memory: MemoryMailSender,
) -> None:
    memory.fail_with = "ConnectTimeout"
    row = send(entry(author()))
    assert row.after == {
        "outcome": "failed",
        "error": "ConnectTimeout",
        "to": "reminder-author@example.com",
        "subject": "Reminder: Call the landlord",
    }
    assert BODY not in all_audit_text()


def test_a_reminder_without_an_address_stores_the_outcome_alone(
    memory: MemoryMailSender,
) -> None:
    owner = author()
    type(owner).objects.filter(pk=owner.pk).update(email="")
    owner.refresh_from_db()
    row = send(entry(owner))
    assert row.after == {"outcome": "no_address"}


def test_a_promise_e_mail_stores_its_subject(memory: MemoryMailSender) -> None:
    row = send(promise(author("promise-author")))
    assert row.entity_type == "contact_entry"
    assert row.after == {
        "outcome": "sent",
        "on": "2026-10-20",
        "to": "promise-author@example.com",
        "subject": "Promise to pay due today: Promising Shop",
    }
    assert "Will pay Friday." not in all_audit_text()


def test_the_rebuilt_subjects_equal_the_sent_ones(memory: MemoryMailSender) -> None:
    item = entry(author(), title="  Two   spaces\tand a tab ")
    send(item)
    assert [m.subject for m in memory.sent] == [calendar.entry_subject(item)]
    assert calendar.entry_subject(item) == "Reminder: Two spaces and a tab"
    long = CalendarEntry(kind="reminder", on=DAY, title="x" * 400)
    assert len(calendar.entry_subject(long)) == 300
