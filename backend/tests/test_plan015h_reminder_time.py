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
import logging
import os
import re
import signal
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings
from django.utils import timezone

import mail
from accounts.roles import Role
from finance.dates import NEW_YORK, clock_text
from ledger.models import AuditLog, CalendarEntry
from ledger.services import calendar
from mail.memory import MemoryMailSender
from payments.management.commands import worker
from payments.models import Job, JobKind
from tests.auth_helpers import make_user
from tests.job_helpers import run_queued_jobs
from tests.test_collections import mail_set_up
from tests.test_operator_views import client_for
from tests.test_plan012fa_d_calendar import part

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


ADD = "/calendar/new/"


def dialog_post(client: Client, **overrides: str) -> tuple[int, str, list[str]]:
    """The New calendar entry dialog's POST: the status, the body and the errors on the time
    (a refused save shows the page again with the dialog's form)."""
    data = {
        "kind": "reminder",
        "on": TODAY.isoformat(),
        "title": "Call the landlord",
        "body": "",
        "email_morning": "on",
        "email_at": "14:30",
        "link_kind": "",
        "link_id": "",
        **overrides,
    }
    response = client.post(ADD, data)
    form = response.context["entry_form"] if response.status_code == 200 else None
    errors = list(form.errors.get("email_at", [])) if form is not None else []
    return response.status_code, response.content.decode(), errors


def edit_post(client: Client, entry: CalendarEntry, **overrides: str) -> tuple[int, str, list[str]]:
    """The edit page's POST of ``entry`` as stored, with ``overrides``: the status, the body and
    the errors on the time."""
    data = {
        "kind": entry.kind,
        "on": entry.on.isoformat(),
        "title": entry.title,
        "body": entry.body,
        "email_at": f"{entry.email_at:%H:%M}",
        "store": "",
        "operator": "",
        "lease": "",
        **({"email_morning": "on"} if entry.email_morning else {}),
        **overrides,
    }
    response = client.post(f"/calendar/{entry.pk}/edit/", data)
    form = response.context["form"] if response.status_code == 200 else None
    errors = list(form.errors.get("email_at", [])) if form is not None else []
    return response.status_code, response.content.decode(), errors


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


# --- (e) The past time, and a value off the list -------------------------------------------------


@pytest.fixture
def billing_user() -> tuple[Client, User]:
    return client_for(Role.BILLING, "reminder-billing")


def test_a_new_reminder_today_at_a_time_just_past_is_refused(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, _ = billing_user
    clock(at(TODAY, 14, 30) + timedelta(seconds=1))
    status, _, errors = dialog_post(client, email_at="14:30")
    assert status == 200 and errors == [calendar.TIME_PASSED]
    assert CalendarEntry.objects.count() == 0
    # The same time tomorrow saves; a later time today saves.
    assert dialog_post(client, on=(TODAY + timedelta(days=1)).isoformat())[0] == 302
    assert dialog_post(client, email_at="15:00")[0] == 302
    # The switch off: the time is not a reminder, nothing to refuse.
    status, _, _ = dialog_post(client, email_at="06:00", email_morning="")
    assert status == 302
    assert CalendarEntry.objects.count() == 3


def test_a_reminder_at_this_very_minute_is_refused(clock: Any, author: User) -> None:
    clock(at(TODAY, 14, 30))
    outcome = calendar.save_entry(
        entry_input(on=TODAY, email_at=time(14, 30)), actor=author, is_admin=False
    )
    assert (outcome.ok, outcome.message, outcome.field) == (False, calendar.TIME_PASSED, "email_at")
    assert CalendarEntry.objects.count() == 0 and AuditLog.objects.count() == 0


def test_an_edit_of_only_the_title_of_todays_passed_reminder_saves(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, user = billing_user
    entry = reminder(user, at_time=time(9, 0))
    clock(at(TODAY, 15))
    status, _, _ = edit_post(client, entry, title="Call the landlord again")
    assert status == 302
    entry.refresh_from_db()
    assert (entry.title, entry.email_at) == ("Call the landlord again", time(9, 0))
    # Moving the time to another past one is refused, nothing changed.
    status, body, errors = edit_post(client, entry, email_at="10:00")
    assert status == 200 and errors == [calendar.TIME_PASSED] and calendar.TIME_PASSED in body
    entry.refresh_from_db()
    assert entry.email_at == time(9, 0)
    # Turning the switch on again for a past time, or moving a passed reminder to today, too.
    entry_off = reminder(user, at_time=time(9, 0), email_morning=False, title="Off")
    assert edit_post(client, entry_off, email_morning="on")[2] == [calendar.TIME_PASSED]
    tomorrow = reminder(user, on=TODAY + timedelta(days=1), at_time=time(9, 0), title="Moved")
    assert edit_post(client, tomorrow, on=TODAY.isoformat())[2] == [calendar.TIME_PASSED]
    # A later time today saves.
    assert edit_post(client, entry, email_at="16:00")[0] == 302


CARD = "4111 1111 1111 1111"


@pytest.mark.parametrize("value", ["06:15", "22:00", "9:00", CARD, "x" * 300])
def test_a_time_off_the_list_is_refused_with_its_words(
    billing_user: tuple[Client, User], clock: Any, value: str
) -> None:
    client, user = billing_user
    clock(at(TODAY, 10))
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    status, body, errors = dialog_post(client, on=tomorrow, email_at=value)
    assert status == 200 and errors == ["Pick a time from the list."]
    assert CARD not in body
    entry = reminder(user, on=TODAY + timedelta(days=2))
    status, body, errors = edit_post(client, entry, email_at=value)
    assert status == 200 and errors == ["Pick a time from the list."]
    assert "Pick a time from the list." in body and CARD not in body
    assert CalendarEntry.objects.count() == 1


def test_no_time_sent_is_nine(billing_user: tuple[Client, User], clock: Any) -> None:
    client, _ = billing_user
    clock(at(TODAY, 10))
    status, _, _ = dialog_post(client, on=(TODAY + timedelta(days=1)).isoformat(), email_at="")
    assert status == 302
    assert CalendarEntry.objects.get().email_at == time(9, 0)


def test_the_dialog_and_the_edit_page_store_the_chosen_time(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, _ = billing_user
    clock(at(TODAY, 10))
    assert dialog_post(client, email_at="14:30")[0] == 302
    entry = CalendarEntry.objects.get()
    assert (entry.email_morning, entry.email_at) == (True, time(14, 30))
    assert edit_post(client, entry, email_at="20:30")[0] == 302
    entry.refresh_from_db()
    assert entry.email_at == time(20, 30)


# --- (b) Shown, and (f) the markup ---------------------------------------------------------------


def page_of(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.content.decode()


def options_of(select: str) -> list[tuple[str, str, bool]]:
    return [
        (value, words, bool(selected))
        for value, selected, words in re.findall(
            r'<option value="([^"]*)"( selected)?>([^<]*)</option>', select
        )
    ]


def test_the_detail_page_and_the_day_panel_show_the_time(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, user = billing_user
    clock(at(TODAY, 10))
    entry = reminder(user, at_time=time(14, 30))
    body = page_of(client, f"/calendar/{entry.pk}/")
    assert (
        '<p class="store-line" id="detail-line">Friday, Oct 23, 2026 · Reminder e-mail at '
        "2:30 PM</p>"
    ) in body
    assert "<dt>Reminder e-mail</dt><dd>At 2:30 PM New York time, to the author</dd>" in body
    assert "Morning e-mail" not in body
    panel = part(page_of(client, f"/calendar/?month=2026-10&on={TODAY}"), "calendar-day-panel")
    assert '<span class="day-entry-meta">Reminder · e-mail at 2:30 PM</span>' in panel
    # Off: "No", the plain line, the kind alone in the panel.
    CalendarEntry.objects.filter(pk=entry.pk).update(email_morning=False)
    body = page_of(client, f"/calendar/{entry.pk}/")
    assert '<p class="store-line" id="detail-line">Friday, Oct 23, 2026</p>' in body
    assert "<dt>Reminder e-mail</dt><dd>No</dd>" in body
    panel = part(page_of(client, f"/calendar/?month=2026-10&on={TODAY}"), "calendar-day-panel")
    assert '<span class="day-entry-meta">Reminder</span>' in panel
    assert "e-mail at" not in panel


def test_a_note_with_the_switch_on_shows_its_kind_and_time(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, user = billing_user
    clock(at(TODAY, 10))
    reminder(user, kind="note", at_time=time(6, 0))
    panel = part(page_of(client, f"/calendar/?month=2026-10&on={TODAY}"), "calendar-day-panel")
    assert '<span class="day-entry-meta">Note · e-mail at 6:00 AM</span>' in panel


def test_the_dialogs_row_has_the_switch_and_the_hidden_time(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, _ = billing_user
    clock(at(TODAY, 10))
    dialog = part(page_of(client, "/calendar/"), "calendar-entry")
    row = dialog[dialog.index('<div class="field switch-field entry-morning">') :]
    row = row[: row.index("</select>")]
    assert re.search(
        r'<label class="inline" for="entry-email_morning"><input type="checkbox" '
        r'name="email_morning" class="switch" role="switch" id="entry-email_morning"> '
        r"E-mail me a reminder</label>",
        row,
    )
    assert '<span class="entry-time" id="entry-time-wrap" hidden>' in row
    assert '<label class="visually-hidden" for="entry-email_at">Reminder time</label>' in row
    assert '<select name="email_at" id="entry-email_at">' in row
    options = options_of(row)
    assert [value for value, _, _ in options] == [v for v, _ in calendar.REMINDER_TIME_CHOICES]
    assert len(options) == 31 and [o for o in options if o[2]] == [("09:00", "9:00 AM", True)]
    assert '</select><span class="muted">New York time</span></span>' in dialog
    assert "entry-morning-time" not in dialog and "09:00 New York" not in dialog
    assert "that morning" not in dialog
    page = page_of(client, "/calendar/")
    assert page.index("ledger/calendar_dialog") < page.index("ledger/reminder_time")


def test_a_refused_time_shows_under_the_select_with_the_row_open(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, _ = billing_user
    clock(at(TODAY, 15))
    status, body, _ = dialog_post(client, email_at="14:30")
    assert status == 200
    dialog = part(body, "calendar-entry")
    assert '<div class="field switch-field entry-morning has-error">' in dialog
    assert re.search(
        r'id="entry-email_morning" checked>|checked[^>]*id="entry-email_morning"', dialog
    )
    wrap = part(dialog, "entry-time-wrap")
    assert wrap.startswith('<span class="entry-time" id="entry-time-wrap">')
    assert re.search(
        r'</select><span class="muted">New York time</span><ul class="errorlist"[^>]*><li>'
        + re.escape(calendar.TIME_PASSED)
        + "</li></ul></span>",
        wrap,
    )
    assert ("14:30", "2:30 PM", True) in options_of(wrap)


def test_a_time_off_the_list_shows_its_words_even_with_the_switch_off(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, _ = billing_user
    clock(at(TODAY, 10))
    status, body, _ = dialog_post(client, email_morning="", email_at="06:15")
    assert status == 200
    wrap = part(part(body, "calendar-entry"), "entry-time-wrap")
    assert wrap.startswith('<span class="entry-time" id="entry-time-wrap">')
    assert "Pick a time from the list." in wrap


def test_the_edit_page_shares_the_row_after_the_text(
    billing_user: tuple[Client, User], clock: Any
) -> None:
    client, user = billing_user
    clock(at(TODAY, 10))
    entry = reminder(user, at_time=time(14, 30))
    body = page_of(client, f"/calendar/{entry.pk}/edit/")
    card = part(body, "calendar-entry-card")
    assert card.index('name="body"') < card.index("entry-morning") < card.index('name="store"')
    assert card.count('name="email_morning"') == 1 and card.count('name="email_at"') == 1
    assert re.search(
        r'<label class="inline" for="id_email_morning"><input type="checkbox" '
        r'name="email_morning" class="switch" role="switch" id="id_email_morning" checked> '
        r"E-mail me a reminder</label>",
        card,
    )
    wrap = part(card, "id_time-wrap")
    assert wrap.startswith('<span class="entry-time" id="id_time-wrap">')
    assert [o for o in options_of(wrap) if o[2]] == [("14:30", "2:30 PM", True)]
    assert "ledger/reminder_time.js" in body
    # The switch off: the time waits hidden, the stored time selected.
    CalendarEntry.objects.filter(pk=entry.pk).update(email_morning=False)
    card = part(page_of(client, f"/calendar/{entry.pk}/edit/"), "calendar-entry-card")
    assert '<span class="entry-time" id="id_time-wrap" hidden>' in card


# --- (c) and (g) The worker's reminder check -----------------------------------------------------


@pytest.fixture
def fresh_worker() -> Iterator[None]:
    """A worker process that has not checked yet, as the worker (``--once`` needs it)."""
    worker.reset_reminder_check()
    with override_settings(STAX_CALLER="worker"):
        yield
    worker.reset_reminder_check()


@pytest.mark.usefixtures("fresh_worker")
def test_the_worker_sends_a_reminder_at_its_time_once(
    author: User, memory: MemoryMailSender, clock: Any
) -> None:
    reminder(author)
    clock(at(TODAY, 14, 15))
    assert worker.check_reminders(at(TODAY, 14, 15)) is True
    assert calendar_jobs() == []
    # Within 15 minutes of the last check: no check, even though the time has come.
    assert worker.check_reminders(at(TODAY, 14, 29)) is False
    assert worker.check_reminders(at(TODAY, 14, 30)) is True
    assert len(calendar_jobs()) == 1
    clock(at(TODAY, 14, 31))
    assert worker.tick() is True  # the queued job, sent
    assert len(memory.sent) == 1
    assert worker.check_reminders(at(TODAY, 14, 45)) is True
    clock(at(TODAY, 16))
    tick()
    assert len(calendar_jobs()) == 1 and run_queued_jobs() == [] and len(memory.sent) == 1


@pytest.mark.usefixtures("fresh_worker")
def test_the_first_tick_of_a_late_worker_sends_the_days_missed_reminder(
    author: User, memory: MemoryMailSender, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    reminder(author)
    reminder(author, on=TODAY - timedelta(days=1), title="Yesterday")
    clock(at(TODAY, 17, 50))
    with caplog.at_level(logging.INFO, logger="payments.management.commands.worker"):
        call_command("worker", "--once")
    assert "Reminder check: queued=1" in caplog.text
    [job] = calendar_jobs()
    assert job.status == "done" and job.result == "Sent."
    [sent] = memory.sent
    assert sent.subject == "Reminder: Call the landlord"


@pytest.mark.usefixtures("fresh_worker")
def test_the_check_runs_first_then_every_15_minutes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[datetime] = []

    def queue(now: datetime) -> int:
        seen.append(now)
        return 0

    monkeypatch.setattr(calendar, "queue_due_reminders", queue)
    start = at(TODAY, 8)
    with caplog.at_level(logging.INFO, logger="payments.management.commands.worker"):
        moments = [start + timedelta(seconds=n) for n in (0, 1, 899, 900, 901, 1799, 1800)]
        ran = [worker.check_reminders(moment) for moment in moments]
    assert ran == [True, False, False, True, False, False, True]
    assert seen == [moments[0], moments[3], moments[6]]
    assert "Reminder check" not in caplog.text  # nothing queued: no line
    # A clock that went back runs the check again.
    assert worker.check_reminders(start) is True


@pytest.mark.usefixtures("fresh_worker")
def test_a_failing_check_is_logged_as_the_ticks_and_waits_its_turn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clock: Any
) -> None:
    calls: list[datetime] = []

    def broken(now: datetime) -> int:
        calls.append(now)
        raise RuntimeError(f"detail {CARD}")

    monkeypatch.setattr(calendar, "queue_due_reminders", broken)
    monkeypatch.setattr(worker, "IDLE_SECONDS", 0)
    clock(at(TODAY, 8))
    ticks: list[bool] = []
    original = worker.tick

    def counted() -> bool:
        if len(ticks) == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        ticks.append(True)
        return original()

    monkeypatch.setattr(worker, "tick", counted)
    with caplog.at_level(logging.ERROR):
        call_command("worker")
    # The first tick failed in the check and was logged; the loop went on, and the next ticks
    # within 15 minutes did not run the check again.
    assert len(ticks) == 3 and len(calls) == 1
    assert caplog.text.count("Worker tick failed: RuntimeError") == 1
    assert CARD not in caplog.text


@pytest.mark.usefixtures("fresh_worker")
def test_a_check_after_another_queued_it_adds_no_job(author: User) -> None:
    reminder(author)
    assert calendar.queue_due_reminders(at(TODAY, 14, 30)) == 1
    worker.reset_reminder_check()
    assert worker.check_reminders(at(TODAY, 14, 31)) is True
    assert len(calendar_jobs()) == 1
