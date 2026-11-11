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
from datetime import UTC, date, datetime, time
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from finance.dates import clock_text
from ledger.models import CalendarEntry
from ledger.services import calendar

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("ledger.migrations.0079_calendar_entry_email_at")
BEFORE = ("ledger", "0078_invoice_email_portal_link_only")
AFTER = ("ledger", "0079_calendar_entry_email_at")


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
