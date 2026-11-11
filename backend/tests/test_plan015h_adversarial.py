"""Fable's adversarial cases for docs/qa/15/PLAN-015h-calendar-reminder-time.md §5: the offered
times are exactly the 31 half hours from 6:00 AM to 9:00 PM; a reminder whose author is inactive
or has no address sends nothing and is audited; a note with the switch on sends like a reminder;
an entry moved to another day after its job was queued sends nothing; two checks at once queue
one job; the sandbox allowlist holds a reminder off the list; the daily tick queues no entry;
the past-time rule reads New York, not UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from threading import Barrier, Thread
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone

import mail
from accounts.roles import Role
from finance.dates import NEW_YORK, clock_text
from ledger.models import AuditLog, CalendarEntry
from ledger.services import calendar
from mail.memory import MemoryMailSender
from payments.models import Job, JobKind
from tests.auth_helpers import make_user
from tests.job_helpers import run_queued_jobs
from tests.test_collections import mail_set_up, tick
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

DAY = date(2026, 10, 20)


def at(day: date, hour: int, minute: int = 0) -> datetime:
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


def reminder(
    author: User | None, *, on: date = DAY, email_at: time = time(14, 30), **fields: Any
) -> CalendarEntry:
    values: dict[str, Any] = {
        "kind": "reminder",
        "on": on,
        "title": "Call the landlord",
        "email_morning": True,
        "email_at": email_at,
        "author": author,
    }
    return CalendarEntry.objects.create(**{**values, **fields})


def author_named(name: str) -> User:
    return make_user(name, Role.BILLING)[0]


# --- The offered times -----------------------------------------------------------------------


def test_the_offered_times_are_the_31_half_hours_from_6_am_to_9_pm() -> None:
    assert len(calendar.REMINDER_TIMES) == 31
    assert calendar.REMINDER_TIMES[0] == time(6, 0) and calendar.REMINDER_TIMES[-1] == time(21, 0)
    assert all(t.minute in (0, 30) and t.second == 0 for t in calendar.REMINDER_TIMES)
    assert list(calendar.REMINDER_TIMES) == sorted(set(calendar.REMINDER_TIMES))
    assert [value for value, _ in calendar.REMINDER_TIME_CHOICES] == [
        f"{t:%H:%M}" for t in calendar.REMINDER_TIMES
    ]
    labels = [label for _, label in calendar.REMINDER_TIME_CHOICES]
    assert labels[0] == "6:00 AM" and labels[12] == "12:00 PM" and labels[-1] == "9:00 PM"
    assert clock_text(time(0, 30)) == "12:30 AM" and clock_text(time(23, 0)) == "11:00 PM"


@pytest.mark.parametrize("value", ["05:30", "21:30", "06:15", "9:00", "09:00:00", "x", "<b>"])
def test_a_time_off_the_list_is_refused_and_nothing_saved(value: str) -> None:
    client = client_for(Role.BILLING)[0]
    response = client.post(
        "/calendar/new/",
        {
            "kind": "reminder",
            "on": "2026-10-21",
            "title": "T",
            "body": "",
            "link_kind": "",
            "link_id": "",
            "email_morning": "on",
            "email_at": value,
        },
    )
    assert response.status_code == 200
    assert "Pick a time from the list." in response.content.decode()
    assert CalendarEntry.objects.count() == 0


# --- Who gets the e-mail -----------------------------------------------------------------------


def test_an_inactive_author_or_one_without_an_address_gets_nothing(
    clock: Any, memory: MemoryMailSender
) -> None:
    gone = author_named("gone-author")
    User.objects.filter(pk=gone.pk).update(is_active=False)
    blank = author_named("blank-author")
    User.objects.filter(pk=blank.pk).update(email="")
    reminder(gone)
    reminder(blank)
    reminder(None)
    clock(at(DAY, 14, 30))
    assert calendar.queue_due_reminders(timezone.now()) == 2
    done = run_queued_jobs()
    assert sorted(job.result for job in done) == [calendar.MORNING_NO_ADDRESS] * 2
    assert memory.sent == []
    audited = AuditLog.objects.filter(action="calendar.morning_email")
    assert audited.count() == 2 and all(row.after == {"outcome": "no_address"} for row in audited)


def test_a_note_with_the_switch_on_is_e_mailed_at_its_time_too(
    clock: Any, memory: MemoryMailSender
) -> None:
    author = author_named("note-author")
    reminder(author, kind="note", title="Bring the keys", email_at=time(6, 0))
    clock(at(DAY, 5, 59))
    assert calendar.queue_due_reminders(timezone.now()) == 0
    clock(at(DAY, 6, 0))
    assert calendar.queue_due_reminders(timezone.now()) == 1
    [sent] = [job for job in run_queued_jobs() if job.kind == JobKind.CALENDAR_EMAIL]
    assert sent.result == "Sent."
    assert [m.subject for m in memory.sent] == ["Note: Bring the keys"]


# --- Once, and only on its day ---------------------------------------------------------------


def test_an_entry_moved_after_its_job_was_queued_sends_nothing(
    clock: Any, memory: MemoryMailSender
) -> None:
    entry = reminder(author_named("moved-author"))
    clock(at(DAY, 14, 45))
    assert calendar.queue_due_reminders(timezone.now()) == 1
    CalendarEntry.objects.filter(pk=entry.pk).update(on=DAY + timedelta(days=1))
    [done] = run_queued_jobs()
    assert done.result == calendar.MORNING_NOT_DUE and memory.sent == []
    # Its new day: queued again under that day's key, once.
    clock(at(DAY + timedelta(days=1), 14, 30))
    assert calendar.queue_due_reminders(timezone.now()) == 1
    assert calendar.queue_due_reminders(timezone.now()) == 0
    assert Job.objects.filter(kind=JobKind.CALENDAR_EMAIL).count() == 2


def test_the_daily_tick_queues_no_entry_and_the_check_queues_it_once(
    clock: Any, memory: MemoryMailSender
) -> None:
    reminder(author_named("tick-author"), email_at=time(9, 0))
    clock(at(DAY, 9, 0))
    out = tick()
    assert "Morning e-mails queued" not in out
    assert Job.objects.filter(kind=JobKind.CALENDAR_EMAIL).count() == 0
    assert calendar.queue_due_reminders(timezone.now()) == 1
    assert tick() and Job.objects.filter(kind=JobKind.CALENDAR_EMAIL).count() == 1
    run_queued_jobs()
    assert len(memory.sent) == 1
    # The time edited after the send: the day's key holds, no second e-mail.
    CalendarEntry.objects.update(email_at=time(15, 0))
    clock(at(DAY, 15, 0))
    assert calendar.queue_due_reminders(timezone.now()) == 0
    run_queued_jobs()
    assert len(memory.sent) == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks: PostgreSQL only")
@pytest.mark.django_db(transaction=True)
def test_two_checks_at_once_queue_one_job_and_neither_fails() -> None:
    reminder(author_named("race-author"), email_at=time(6, 0))
    now = at(DAY, 12, 0)
    barrier = Barrier(2)
    counts: list[int] = []
    errors: list[str] = []

    def check() -> None:
        try:
            barrier.wait()
            counts.append(calendar.queue_due_reminders(now))
        except Exception as error:  # recorded: a thread's failure is invisible otherwise
            errors.append(type(error).__name__)
        finally:
            connection.close()

    threads = [Thread(target=check) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert Job.objects.filter(kind=JobKind.CALENDAR_EMAIL).count() == 1
    # Neither check failed: payments.jobs.enqueue answers a duplicate replay key with the job
    # already made. Each check may still count it as its own (the count feeds a log line only).
    assert errors == [] and 1 <= sum(counts) <= 2


def test_on_the_sandbox_a_reminder_off_the_allowlist_is_held(
    clock: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mail_set_up(settings, sandbox=True, allowlist="someone.else@example.com")
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    reminder(author_named("held-author"))
    clock(at(DAY, 14, 30))
    assert calendar.queue_due_reminders(timezone.now()) == 1
    [done] = run_queued_jobs()
    assert done.result == calendar.MORNING_SENT[mail.HELD] and memory.sent == []


# --- The past-time rule at the edge ----------------------------------------------------------


def test_the_past_time_rule_reads_new_york_not_utc(clock: Any) -> None:
    """At 23:30 UTC on 20 October (19:30 New York) a reminder for the 20th at 8:00 PM is
    still ahead; one at 7:30 PM has passed."""
    client = client_for(Role.BILLING)[0]
    clock(datetime(2026, 10, 20, 23, 30, tzinfo=UTC))
    data = {
        "kind": "reminder",
        "on": "2026-10-20",
        "title": "Edge",
        "body": "",
        "link_kind": "",
        "link_id": "",
        "email_morning": "on",
    }
    refused = client.post("/calendar/new/", {**data, "email_at": "19:30"})
    assert refused.status_code == 200 and calendar.TIME_PASSED in refused.content.decode()
    assert CalendarEntry.objects.count() == 0
    saved = client.post("/calendar/new/", {**data, "email_at": "20:00"})
    assert saved.status_code == 302
    assert CalendarEntry.objects.get().email_at == time(20, 0)
