"""PLAN-015h: the calendar reminder's e-mail at a chosen time (docs/qa/15/
PLAN-015h-calendar-reminder-time.md §5 (a)–(g)).

The switch "E-mail me a reminder" with a time from the half hours 6:00 AM to 9:00 PM New York
(9:00 AM by default), stored on the entry as ``email_at`` (migration 0079), shown on the entry's
page and in the day panel, refused for today at a time already past; the worker's reminder check
queues each due reminder of the day once, under the old per-entry-and-day replay key, and the
daily tick queues promises only.
"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
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
from tests.test_collections import mail_set_up

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ledger.migrations.0079_calendar_entry_email_at")
BEFORE = ("ledger", "0078_invoice_email_portal_link_only")
AFTER = ("ledger", "0079_calendar_entry_email_at")


#: A Friday in New York daylight time (UTC-4), the day of the reminders below.
TODAY = date(2026, 10, 23)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """A New York wall-clock moment as an aware UTC datetime."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the app's "now" (``django.utils.timezone.now``) to a moment."""

    def set_now(moment: datetime) -> None:
        monkeypatch.setattr(timezone, "now", lambda: moment)

    return set_now


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, settings: Any) -> MemoryMailSender:
    mail_set_up(settings)
    sender = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: sender)
    return sender


@pytest.fixture
def author() -> User:
    return make_user("reminder-author", Role.BILLING)[0]


def reminder(
    author: User | None, on: date = TODAY, at_time: time = time(14, 30), **fields: Any
) -> CalendarEntry:
    values: dict[str, Any] = {
        "kind": "reminder",
        "on": on,
        "title": "Call the landlord",
        "email_morning": True,
        "email_at": at_time,
        "author": author,
        **fields,
    }
    return CalendarEntry.objects.create(**values)


def calendar_jobs() -> list[Job]:
    return list(Job.objects.filter(kind=JobKind.CALENDAR_EMAIL).order_by("created_at", "id"))


def tick() -> str:
    out = StringIO()
    call_command("collections_tick", stdout=out)
    # The tick also queues the day's read of the dispute list (tested in test_disputes.py).
    Job.objects.filter(kind=JobKind.DISPUTES_SYNC).delete()
    return out.getvalue()


def migrate_to(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


# --- (a) The field, its constraint and the clock's words ----------------------------------------


@pytest.mark.django_db(transaction=True)
def test_0079_gives_an_existing_entry_nine_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = migrate_to(BEFORE)
        entry = old.get_model("ledger", "CalendarEntry").objects.create(
            kind="reminder", on=date(2026, 11, 20), title="Before 0079", email_morning=True
        )
        new = migrate_to(AFTER)
        row = new.get_model("ledger", "CalendarEntry").objects.get(pk=entry.pk)
        assert (row.email_morning, row.email_at) == (True, time(9, 0))
        back = migrate_to(BEFORE)
        columns = [
            column.name
            for column in connection.introspection.get_table_description(
                connection.cursor(), "calendar_entries"
            )
        ]
        assert "email_at" not in columns
        assert back.get_model("ledger", "CalendarEntry").objects.filter(pk=entry.pk).exists()
    finally:
        migrate_to(*leaves)


def test_a_new_entry_reads_nine_by_default() -> None:
    entry = CalendarEntry.objects.create(kind="note", on=date(2026, 11, 20), title="Default")
    entry.refresh_from_db()
    assert entry.email_at == time(9, 0) == calendar.DEFAULT_REMINDER_TIME


@pytest.mark.parametrize("value", [time(6, 15), time(22, 0), time(5, 30), time(9, 0, 1)])
def test_the_constraint_refuses_a_time_off_the_list(value: time) -> None:
    entry = CalendarEntry.objects.create(kind="note", on=date(2026, 11, 20), title="Off")
    with pytest.raises(IntegrityError), transaction.atomic():
        CalendarEntry.objects.filter(pk=entry.pk).update(email_at=value)
    entry.refresh_from_db()
    assert entry.email_at == time(9, 0)


def test_the_constraint_takes_every_offered_time() -> None:
    entry = CalendarEntry.objects.create(kind="note", on=date(2026, 11, 20), title="Every")
    for value in calendar.REMINDER_TIMES:
        CalendarEntry.objects.filter(pk=entry.pk).update(email_at=value)
        entry.refresh_from_db()
        assert entry.email_at == value


def test_the_offered_times_are_the_31_half_hours_from_six_to_nine_pm() -> None:
    times = calendar.REMINDER_TIMES
    assert len(times) == 31 and times[0] == time(6, 0) and times[-1] == time(21, 0)
    assert all(value.minute in (0, 30) and value.second == 0 for value in times)
    assert list(times) == sorted(set(times))
    assert MIGRATION.REMINDER_TIMES == times
    assert calendar.REMINDER_TIME_CHOICES[0] == ("06:00", "6:00 AM")
    assert calendar.REMINDER_TIME_CHOICES[6] == ("09:00", "9:00 AM")
    assert calendar.REMINDER_TIME_CHOICES[-1] == ("21:00", "9:00 PM")
    assert [value for value, _ in calendar.REMINDER_TIME_CHOICES] == [
        f"{value:%H:%M}" for value in times
    ]


@pytest.mark.parametrize(
    ("value", "words"),
    [
        (time(6, 0), "6:00 AM"),
        (time(9, 0), "9:00 AM"),
        (time(12, 0), "12:00 PM"),
        (time(12, 30), "12:30 PM"),
        (time(14, 30), "2:30 PM"),
        (time(21, 0), "9:00 PM"),
        (time(0, 0), "12:00 AM"),
    ],
)
def test_clock_text(value: time, words: str) -> None:
    assert clock_text(value) == words


@pytest.mark.parametrize("value", [None, "09:00", datetime(2026, 11, 1, 9, 0, tzinfo=UTC)])
def test_clock_text_refuses_anything_but_a_time(value: object) -> None:
    with pytest.raises(TypeError):
        clock_text(value)  # type: ignore[arg-type]


# --- (b) Saved and audited -----------------------------------------------------------------------


def entry_input(**fields: Any) -> calendar.EntryInput:
    values: dict[str, Any] = {
        "kind": "reminder",
        "on": TODAY + timedelta(days=1),
        "title": "Call the landlord",
        "email_morning": True,
        "email_at": time(14, 30),
        **fields,
    }
    return calendar.EntryInput(**values)


def test_the_service_stores_and_audits_the_time(clock: Any, author: User) -> None:
    clock(at(TODAY, 10))
    outcome = calendar.save_entry(entry_input(), actor=author, is_admin=False)
    assert outcome.ok and outcome.entry is not None
    entry = CalendarEntry.objects.get(pk=outcome.entry.pk)
    assert (entry.email_morning, entry.email_at) == (True, time(14, 30))
    [created] = AuditLog.objects.filter(action="calendar_entry.created")
    assert created.after is not None and created.after["email_at"] == "14:30"
    changed = calendar.save_entry(
        entry_input(email_at=time(16, 0)), actor=author, is_admin=False, entry_id=entry.pk
    )
    assert changed.ok
    [row] = AuditLog.objects.filter(action="calendar_entry.changed")
    assert row.before is not None and row.after is not None
    assert (row.before["email_at"], row.after["email_at"]) == ("14:30", "16:00")


def test_the_item_carries_the_time_only_while_the_switch_is_on(author: User) -> None:
    on = reminder(author)
    off = reminder(author, email_morning=False)
    assert calendar.entry_item(on).email_at == time(14, 30)
    assert calendar.entry_item(off).email_at is None


# --- (c) Sent once, at its time ------------------------------------------------------------------


def test_a_reminder_is_queued_at_its_time_and_sent_once(
    author: User, memory: MemoryMailSender, clock: Any
) -> None:
    entry = reminder(author)
    assert calendar.queue_due_reminders(at(TODAY, 14, 15)) == 0
    assert calendar_jobs() == []
    assert calendar.queue_due_reminders(at(TODAY, 14, 30)) == 1
    [job] = calendar_jobs()
    assert (job.entity_type, job.entity_id, job.params) == (
        "calendar_entry",
        entry.pk,
        {"on": TODAY.isoformat()},
    )
    [done] = run_queued_jobs()
    assert done.result == "Sent."
    [sent] = memory.sent
    assert (sent.to, sent.subject) == ("reminder-author@example.com", "Reminder: Call the landlord")
    assert calendar.queue_due_reminders(at(TODAY, 14, 45)) == 0
    for hour in (9, 15, 23):
        clock(at(TODAY, hour))
        assert "Morning e-mails queued" not in tick()
    assert len(calendar_jobs()) == 1
    assert run_queued_jobs() == [] and len(memory.sent) == 1


def test_the_tick_queues_no_entry_and_still_queues_promises(author: User, clock: Any) -> None:
    reminder(author, at_time=time(6, 0))
    clock(at(TODAY, 9))
    line = tick()
    assert "Promise reminders queued: 0." in line and "Morning e-mails queued" not in line
    assert calendar_jobs() == []
    assert calendar.queue_morning_emails(TODAY) == calendar.MorningReport(promises=0)


def test_the_check_takes_only_the_days_due_entries_with_an_author_in_time_order(
    author: User,
) -> None:
    late = reminder(author, at_time=time(14, 0), title="Later")
    early = reminder(author, at_time=time(8, 0), title="Earlier")
    reminder(author, at_time=time(14, 30), title="Not yet")
    reminder(author, email_morning=False, at_time=time(8, 0), title="Switch off")
    reminder(None, at_time=time(8, 0), title="No author")
    reminder(author, on=TODAY + timedelta(days=1), at_time=time(6, 0), title="Tomorrow")
    assert calendar.queue_due_reminders(at(TODAY, 14, 29)) == 2
    assert [job.entity_id for job in calendar_jobs()] == [early.pk, late.pk]


def test_the_time_is_read_in_new_york_whatever_the_zone_of_now(author: User) -> None:
    entry = reminder(author, at_time=time(21, 0))
    # 01:00 UTC on the 24th is 21:00 on the 23rd in New York.
    assert calendar.queue_due_reminders(datetime(2026, 10, 24, 0, 59, tzinfo=UTC)) == 0
    assert calendar.queue_due_reminders(datetime(2026, 10, 24, 1, 0, tzinfo=UTC)) == 1
    [job] = calendar_jobs()
    assert job.entity_id == entry.pk and job.params == {"on": TODAY.isoformat()}


# --- (d) Late after downtime, never for a past day; DST ------------------------------------------


def test_a_reminder_missed_while_down_is_sent_late_the_same_day(
    author: User, memory: MemoryMailSender
) -> None:
    reminder(author)
    assert calendar.queue_due_reminders(at(TODAY, 17, 50)) == 1
    [done] = run_queued_jobs()
    assert done.result == "Sent." and len(memory.sent) == 1


def test_a_reminder_of_yesterday_is_never_queued(author: User) -> None:
    reminder(author, on=TODAY - timedelta(days=1), at_time=time(14, 30))
    for moment in (at(TODAY, 6), at(TODAY, 14, 30), at(TODAY, 23, 59)):
        assert calendar.queue_due_reminders(moment) == 0
    assert calendar_jobs() == []


@pytest.mark.parametrize(
    ("day", "before", "due"),
    [
        # The fall change-over (UTC-4 until 02:00, then UTC-5): 9:00 AM is 14:00 UTC.
        (
            date(2026, 11, 1),
            datetime(2026, 11, 1, 13, 0, tzinfo=UTC),
            datetime(2026, 11, 1, 14, 0, tzinfo=UTC),
        ),
        # The spring change-over (UTC-5 until 02:00, then UTC-4): 9:00 AM is 13:00 UTC.
        (
            date(2026, 3, 8),
            datetime(2026, 3, 8, 12, 0, tzinfo=UTC),
            datetime(2026, 3, 8, 13, 0, tzinfo=UTC),
        ),
    ],
)
def test_a_reminder_on_a_clock_change_day_goes_at_its_wall_clock_time(
    author: User, day: date, before: datetime, due: datetime
) -> None:
    reminder(author, on=day, at_time=time(9, 0))
    assert calendar.queue_due_reminders(before) == 0
    assert calendar.queue_due_reminders(due) == 1
