"""Opus's tests of docs/qa/15/PLAN-015k-reminder-email-outbox-activity.md §5: the reminder and
promise-to-pay e-mails' audit payload (the address, the subject and the held reason, never the
body), their Activity words, and their read-only rows in the Outbox (the merged list, its
filters, its paging over both sources and its bounded query count). Fable's adversarial file
is separate.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import mail
from accounts.roles import Role
from finance.dates import NEW_YORK
from ledger.models import (
    AuditLog,
    CalendarEntry,
    ContactEntry,
    MessageStatus,
    MessageTemplate,
    OutboundMessage,
    RentCharge,
)
from ledger.services import calendar, detail_cards, outbox_list
from mail.memory import MemoryMailSender
from payments.models import JobEntity
from tests.auth_helpers import make_user
from tests.test_collections import mail_set_up, operator, rent_charge
from tests.test_ledger_models import saved
from tests.test_operator_views import client_for

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


# --- Task 2: the Activity words --------------------------------------------------------------


@pytest.mark.parametrize(
    ("after", "words"),
    [
        (
            {"outcome": "sent", "on": "2026-10-20", "to": "ana@example.com"},
            "Reminder e-mail sent to ana@example.com",
        ),
        ({"outcome": "sent", "on": "2026-10-20"}, "Reminder e-mail sent"),
        ({"outcome": "held", "reason": "not_set_up"}, "Reminder e-mail held (mail not set up)"),
        (
            {"outcome": "held", "reason": "sandbox"},
            "Reminder e-mail held (address not on the allowlist)",
        ),
        ({"outcome": "held", "on": "2026-10-20"}, "Reminder e-mail held"),
        ({"outcome": "held", "reason": "unknown"}, "Reminder e-mail held"),
        ({"outcome": "failed", "error": "ConnectTimeout"}, "Reminder e-mail failed"),
        ({"outcome": "no_address"}, "Reminder e-mail not sent (no address)"),
        ({"outcome": "exploded"}, "Reminder e-mail"),
        ({}, "Reminder e-mail"),
        (None, "Reminder e-mail"),
        (["sent"], "Reminder e-mail"),
        ("sent", "Reminder e-mail"),
    ],
)
def test_each_reminder_outcome_has_its_words(after: Any, words: str) -> None:
    assert detail_cards.morning_email_words("calendar_entry", after) == words


def test_a_promise_e_mail_reads_promise() -> None:
    sent = {"outcome": "sent", "to": "ana@example.com"}
    assert (
        detail_cards.morning_email_words("contact_entry", sent)
        == "Promise e-mail sent to ana@example.com"
    )
    assert (
        detail_cards.morning_email_words("contact_entry", {"outcome": "held", "reason": "sandbox"})
        == "Promise e-mail held (address not on the allowlist)"
    )
    assert detail_cards.morning_email_words("contact_entry", None) == "Promise e-mail"


def test_another_action_keeps_its_words() -> None:
    assert detail_cards.action_words("one_off_charge.payment_recorded") == (
        "Charge payment recorded"
    )
    found = detail_cards.activity([("one_off_charge", "abc")])
    assert found.rows == []


def test_the_calendar_entry_activity_card_words_the_outcome(settings: Any) -> None:
    settings.MAIL_ENABLED = False
    item = entry(author())
    send(item)
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/calendar/{item.pk}/").content.decode()
    card = page[page.index('id="activity-card"') :]
    assert "Reminder e-mail held (mail not set up)" in card
    assert "Calendar morning email" not in page


def test_older_and_broken_rows_render_on_the_activity_card() -> None:
    item = entry(author())
    staff_row(item, {"outcome": "sent", "on": "2026-10-19"})
    staff_row(item, None)
    staff_row(item, ["held"])
    staff_row(item, {"outcome": "mystery"})
    client, _ = client_for(Role.BILLING)
    response = client.get(f"/calendar/{item.pk}/")
    assert response.status_code == 200
    card = response.content.decode()
    card = card[card.index('id="activity-card"') :]
    actions = re.findall(r'<strong class="activity-action">([^<]*)</strong>', card)
    assert sorted(actions) == sorted(
        ["Reminder e-mail sent", "Reminder e-mail", "Reminder e-mail", "Reminder e-mail"]
    )


# --- Task 3: the Outbox ----------------------------------------------------------------------


def outbox(query: str = "") -> str:
    client, _ = client_for(Role.ADMIN)
    response = client.get(OUTBOX + query)
    assert response.status_code == 200
    return response.content.decode()


def row_ids(page: str) -> list[str]:
    table = page[page.index('id="outbox"') :]
    return re.findall(r'<tr id="((?:message|staff)-[^"]+)"', table)


def row_html(page: str, row_id: str) -> str:
    start = page.index(f'<tr id="{row_id}"')
    return page[start : page.index("</tr>", start)]


_occurrence = iter(range(1, 100_000))


ENGINE_OWNER = "engine.owner@example.com"


def engine_message(status: str = MessageStatus.SENT, held_reason: str = "") -> OutboundMessage:
    """One engine message at the clock's time, about the test's one engine charge."""
    charge = RentCharge.objects.filter(lease__operator__email=ENGINE_OWNER).first()
    if charge is None:
        charge = rent_charge(owner=operator(ENGINE_OWNER))
    assert charge.lease.operator is not None
    return OutboundMessage.objects.create(
        operator=charge.lease.operator,
        lease=charge.lease,
        template=MessageTemplate.objects.get(code="overdue_day5"),
        kind="ladder",
        rule="C",
        occurrence=next(_occurrence),
        charge_entity_type="rent_charge",
        charge_entity_id=charge.pk,
        to_address=ENGINE_OWNER,
        rendered_subject="Payment overdue",
        rendered_body="Engine body.",
        status=status,
        held_reason=held_reason,
    )


def test_a_held_reminder_is_a_read_only_row_with_the_author_and_reason(
    settings: Any, clock: Any
) -> None:
    settings.MAIL_ENABLED = False
    clock(at(DAY, 9))
    row = send(entry(author()))
    page = outbox()
    assert NOTE in page
    cells = row_html(page, f"staff-{row.pk}")
    assert "Reminder e-mail" in cells and "Reminder: Call the landlord" in cells
    assert "reminder-author@example.com" in cells and "(current address)" not in cells
    assert "<span>Held</span>" in cells and "not sent (mail not set up)" in cells
    assert "Send now" not in cells and "<form" not in cells and BODY not in page
    assert "1 message" in page


def test_a_promise_e_mail_names_its_operator(memory: MemoryMailSender, clock: Any) -> None:
    clock(at(DAY, 9))
    row = send(promise(author("promise-author")))
    cells = row_html(outbox(), f"staff-{row.pk}")
    assert "Promise to pay e-mail" in cells and "Promising Shop" in cells
    assert "Promise to pay due today: Promising Shop" in cells
    assert "<span>Sent</span>" in cells and "promise-author@example.com" in cells


def test_a_failed_reminder_shows_its_error(memory: MemoryMailSender) -> None:
    memory.fail_with = "ConnectTimeout"
    row = send(entry(author()))
    cells = row_html(outbox(), f"staff-{row.pk}")
    assert "<span>Failed</span>" in cells and "ConnectTimeout" in cells


def test_an_older_row_rebuilds_the_subject_and_marks_the_current_address() -> None:
    item = entry(author())
    row = staff_row(item, {"outcome": "held", "on": "2026-10-20"})
    cells = row_html(outbox(), f"staff-{row.pk}")
    assert "Reminder: Call the landlord" in cells
    assert "reminder-author@example.com" in cells and "(current address)" in cells
    assert "<span>Held</span>" in cells and 'cell-subline">not sent' not in cells


def test_a_deleted_entrys_older_row_shows_dashes() -> None:
    item = entry(author())
    row = staff_row(item, {"outcome": "sent", "on": "2026-10-20"})
    item.delete()
    cells = row_html(outbox(), f"staff-{row.pk}")
    assert '<span class="cell-subline">—</span>' in cells
    assert re.findall(r"<td[^>]*>([^<]*)</td>", cells).count("—") >= 4  # store, rule, to, scheduled


def test_no_address_and_broken_rows_are_not_listed() -> None:
    item = entry(author())
    staff_row(item, {"outcome": "no_address"})
    staff_row(item, None)
    staff_row(item, ["sent"])
    staff_row(item, {"outcome": "unknown"})
    staff_row(item, "sent")
    page = outbox()
    assert row_ids(page) == [] and "No messages yet." in page and "0 messages" in page


def test_the_list_is_newest_first_across_both_sources(clock: Any) -> None:
    item = entry(author())
    clock(at(DAY, 9))
    first = engine_message()
    clock(at(DAY, 10))
    second = staff_row(item, {"outcome": "sent", "to": "a@example.com"})
    clock(at(DAY, 11))
    third = engine_message(MessageStatus.HELD, "not_set_up")
    clock(at(DAY, 12))
    fourth = staff_row(item, {"outcome": "held", "reason": "sandbox"})
    assert row_ids(outbox()) == [
        f"staff-{fourth.pk}",
        f"message-{third.pk}",
        f"staff-{second.pk}",
        f"message-{first.pk}",
    ]


def mixed(count: int, clock: Any) -> list[str]:
    """``count`` rows, alternately engine messages and staff e-mails, a minute apart; their
    row ids newest first."""
    item = entry(author(f"mixed-{uuid.uuid4().hex[:8]}"))
    ids: list[str] = []
    for n in range(count):
        clock(at(DAY, 8) + timedelta(minutes=n))
        if n % 2:
            ids.append(f"staff-{staff_row(item, {'outcome': 'sent', 'to': 'a@example.com'}).pk}")
        else:
            ids.append(f"message-{engine_message().pk}")
    return ids[::-1]


def test_page_two_of_a_mixed_list_holds_the_right_fifty(clock: Any) -> None:
    expected = mixed(120, clock)
    assert row_ids(outbox()) == expected[:50]
    page = outbox("?page=2")
    assert row_ids(page) == expected[50:100]
    assert "120 messages" in page
    assert row_ids(outbox("?page=3")) == expected[100:]
    assert row_ids(outbox("?page=99999999999999999999")) == expected[100:]


def test_the_page_reads_a_fixed_number_of_queries(clock: Any) -> None:
    def queries() -> int:
        client, _ = client_for(Role.ADMIN)
        client.get(OUTBOX)  # the session's first read
        with CaptureQueriesContext(connection) as captured:
            assert client.get(OUTBOX).status_code == 200
        return len(captured)

    mixed(5, clock)
    few = queries()
    mixed(115, clock)
    assert OutboundMessage.objects.count() + AuditLog.objects.filter(action=ACTION).count() == 120
    assert queries() == few


def test_the_keys_query_is_one_union_and_the_rows_are_read_for_the_page_only(
    clock: Any,
) -> None:
    mixed(120, clock)
    with CaptureQueriesContext(connection) as captured:
        shown, found = outbox_list.page("2")
    assert len(found) == 50 and shown.paginator.count == 120
    union = [q["sql"] for q in captured if "UNION" in q["sql"].upper()]
    assert len(union) == 2  # the count and the page's keys
    assert all("LIMIT" in sql.upper() or "COUNT" in sql.upper() for sql in union)


@pytest.fixture
def both(clock: Any) -> dict[str, str]:
    """Engine and staff rows of each status and reason, on two days."""
    item = entry(author())
    found: dict[str, str] = {}
    clock(at(DAY, 9))
    found["m-sent"] = f"message-{engine_message().pk}"
    found["s-sent"] = f"staff-{staff_row(item, {'outcome': 'sent', 'to': 'a@example.com'}).pk}"
    found["m-held-ns"] = f"message-{engine_message(MessageStatus.HELD, 'not_set_up').pk}"
    found["s-held-ns"] = f"staff-{staff_row(item, {'outcome': 'held', 'reason': 'not_set_up'}).pk}"
    clock(at(DAY + timedelta(days=1), 9))
    found["m-held-sb"] = f"message-{engine_message(MessageStatus.HELD, 'sandbox').pk}"
    found["s-held-sb"] = f"staff-{staff_row(item, {'outcome': 'held', 'reason': 'sandbox'}).pk}"
    found["s-held-old"] = f"staff-{staff_row(item, {'outcome': 'held'}).pk}"
    found["m-failed"] = f"message-{engine_message(MessageStatus.FAILED).pk}"
    found["s-failed"] = f"staff-{staff_row(item, {'outcome': 'failed', 'error': 'Boom'}).pk}"
    found["m-queued"] = f"message-{engine_message(MessageStatus.QUEUED).pk}"
    found["m-held-ho"] = f"message-{engine_message(MessageStatus.HELD, 'handled_personally').pk}"
    return found


def listed(query: str, both: dict[str, str]) -> set[str]:
    names = {value: key for key, value in both.items()}
    return {names[row] for row in row_ids(outbox(query))}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?status=sent", {"m-sent", "s-sent"}),
        ("?status=failed", {"m-failed", "s-failed"}),
        (
            "?status=held",
            {"m-held-ns", "s-held-ns", "m-held-sb", "s-held-sb", "s-held-old", "m-held-ho"},
        ),
        ("?status=queued", {"m-queued"}),
        ("?status=combined", set()),
        ("?reason=not_set_up", {"m-held-ns", "s-held-ns"}),
        ("?reason=sandbox", {"m-held-sb", "s-held-sb"}),
        ("?reason=handled_personally", {"m-held-ho"}),
        ("?from=2026-10-20&to=2026-10-20", {"m-sent", "s-sent", "m-held-ns", "s-held-ns"}),
        (
            "?from=2026-10-21",
            {
                "m-held-sb",
                "s-held-sb",
                "s-held-old",
                "m-failed",
                "s-failed",
                "m-queued",
                "m-held-ho",
            },
        ),
        ("?to=2026-10-19", set()),
        ("?status=held&reason=sandbox&from=2026-10-21&to=2026-10-21", {"m-held-sb", "s-held-sb"}),
        ("?status=sent&from=2026-10-21", set()),
        ("?status=held&reason=not_set_up&to=2026-10-20", {"m-held-ns", "s-held-ns"}),
    ],
)
def test_each_filter_selects_rows_of_both_sources(
    query: str, expected: set[str], both: dict[str, str]
) -> None:
    assert listed(query, both) == expected


def test_the_header_counts_both_sources(both: dict[str, str]) -> None:
    assert f"{len(both)} messages" in outbox()
    assert "2 messages" in outbox("?status=sent")
    assert "1 message<" in outbox("?status=queued")


def test_send_now_with_an_audit_key_is_404(settings: Any) -> None:
    settings.MAIL_ENABLED = False
    row = send(entry(author()))
    client, _ = client_for(Role.ADMIN)
    assert client.post(f"{OUTBOX}{row.pk}/send-now/").status_code == 404
    assert client.post(f"{OUTBOX}staff-{row.pk}/send-now/").status_code == 404


def test_the_note_is_shown_only_while_mail_is_not_set_up(memory: MemoryMailSender) -> None:
    assert 'id="outbox-not-set-up"' not in outbox()


def test_the_audit_page_shows_the_address_and_subject_as_plain_text(
    settings: Any, clock: Any
) -> None:
    settings.MAIL_ENABLED = False
    clock(at(DAY, 9))
    item = entry(author(), title="<b>Rent</b> & keys")
    send(item)
    client, _ = client_for(Role.ADMIN)
    response = client.get("/audit/?action=calendar.morning_email&from=2026-10-20&to=2026-10-20")
    assert response.status_code == 200
    page = response.content.decode()
    after = page[page.index('class="audit-after"') :]
    after = after[: after.index("</td>")]
    # The audit page's masking leaves an address and a sentence as they are.
    assert "<strong>to</strong>: <span" in after and "reminder-author@example.com" in after
    assert "Reminder: &lt;b&gt;Rent&lt;/b&gt; &amp; keys" in after and "<b>Rent" not in after
    assert "<strong>reason</strong>: <span" in after and "not_set_up" in after
