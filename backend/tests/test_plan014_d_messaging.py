"""PLAN-014 Batch D (Fable): the messaging engine's adversarial cases, beside Opus's
``test_messaging_engine.py`` and ``test_collections.py`` — the owner's six templates letter by
letter against plan §9 and the no-retry variant's one swapped sentence; the placeholder set;
hostile placeholders refused without the text repeated; Outbox mode with the body stored and the
window time recorded on a held row; the sandbox allowlist compared without case; a bad allowlist
line refused without echo; one row per lease per charge per rule per occurrence with a manual
row that never collides; SMS refused with nothing written; a message without a lease refused;
the send window across both DST changes with ``Job.run_after`` honoured by ``claim_next``; the
Outbox, Send now and the store timeline below admin; the preview that queues nothing; a
20 000-character body refused."""

from __future__ import annotations

import importlib
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test import override_settings

from accounts.roles import Role
from ledger import merge
from ledger.models import (
    CompanySettings,
    HeldReason,
    Lease,
    LeaseBilling,
    MessageChannel,
    MessageStatus,
    MessageTemplate,
    OutboundMessage,
    RentCharge,
)
from ledger.services import message_templates, messaging
from payments import jobs
from payments.models import Job
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_collections import mail_set_up
from tests.test_collections import memory as memory  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

NEW_YORK = ZoneInfo("America/New_York")
PLAN = (
    Path(__file__).resolve().parents[2]
    / "docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md"
)
CODES = {
    "A": "reminder_autopay",
    "B": "reminder_due",
    "C": "overdue_day5",
    "D": "overdue_day10",
    "E": "internal_day30",
    "F": "returned_debit",
}
PLACEHOLDERS = {
    "operator_name",
    "store_number",
    "store_name",
    "amount",
    "charge_name",
    "due_date",
    "debit_date",
    "retry_date",
    "days_late",
    "bank_last4",
    "return_reason",
    "pay_link",
    "store_link",
    "support_email",
    "company_name",
}
NO_RETRY = "Please contact us within 2 business days to set up a new payment method."
OUTBOX = "/admin-settings/outbox/"


def a_world(email: str = "store1701@example.com") -> tuple[Lease, RentCharge]:
    lease = a_lease(
        a_store("1701", number=1701), an_operator("Ayşe Kaya"), started=date(2026, 1, 1)
    )
    LeaseBilling.objects.update_or_create(
        lease=lease, defaults={"email": email, "bank_last4": "4821"}
    )
    charge = a_rent_charge(lease, date(2026, 10, 1), due="1500.00")
    return lease, charge


def template(code: str) -> MessageTemplate:
    """A seeded template; written again from the migration when a transactional test flushed
    the table."""
    found = MessageTemplate.objects.filter(code=code).first()
    if found is not None:
        return found
    seed = importlib.import_module("ledger.migrations.0061_messaging_engine")
    for seeded, name, internal, subject, body in seed.TEMPLATES:
        if seeded == code:
            return MessageTemplate.objects.create(
                code=code, name=name, internal=internal, subject=subject, body=body
            )
    raise AssertionError(code)


def at(hour: int, minute: int = 0, day: date = date(2026, 10, 6)) -> datetime:
    """A New York wall-clock moment (2026-10-06 is a Tuesday)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)


def context(lease: Lease, charge: RentCharge, rule: str = "B") -> dict[str, str]:
    return messaging.context_for(lease, charge, rule, today=date(2026, 10, 6))


def queued(
    lease: Lease, charge: RentCharge, rule: str = "B", code: str = "reminder_due", **kwargs: Any
) -> OutboundMessage:
    message = messaging.queue(
        rule, lease, charge, template(code), context(lease, charge, rule), **kwargs
    )
    assert message is not None
    return message


# --- §9: the six templates, letter by letter ------------------------------------------------


def plan_templates() -> dict[str, tuple[str, str]]:
    """The plan's §9 block: letter -> (subject, body)."""
    text = PLAN.read_text(encoding="utf-8")
    block = text[text.index("## 9. The six templates") :]
    block = block[block.index("```") + 3 :]
    block = block[: block.index("```")]
    found: dict[str, tuple[str, str]] = {}
    for part in re.split(r"\n(?=[A-F](?: \(internal\))? — Subject: )", block.strip("\n")):
        subject = part.split("Subject: ", 1)[1].split("\n", 1)[0]
        found[part[0]] = (subject, part.split("\n", 1)[1].strip("\n"))
    return found


@pytest.mark.parametrize("letter", list("ABCDEF"))
def test_the_seeded_template_is_the_owners_text_byte_for_byte(letter: str) -> None:
    subject, body = plan_templates()[letter]
    row = template(CODES[letter])
    assert row.subject == subject
    assert row.body == body
    assert row.active and row.internal is (letter == "E")
    assert row.channel == MessageChannel.EMAIL


def test_the_no_retry_variant_swaps_one_sentence_only() -> None:
    f_body = template("returned_debit").body
    variant = template("returned_debit_no_retry").body
    retry = next(line for line in f_body.splitlines() if line.startswith("If we do not hear"))
    assert retry not in variant and NO_RETRY in variant
    assert f_body.replace(retry, NO_RETRY) == variant
    assert template("returned_debit_no_retry").subject == template("returned_debit").subject


def test_the_old_ladder_templates_are_inactive_and_the_steps_table_is_gone() -> None:
    for code in ("friendly_reminder", "formal_notice", "final_notice"):
        row = MessageTemplate.objects.filter(code=code).first()
        assert row is None or row.active is False
    assert "collection_steps" not in connection.introspection.table_names()


# --- D4: placeholders -------------------------------------------------------------------------


def test_the_placeholder_set_is_exactly_the_fifteen() -> None:
    assert set(merge.FIELDS) == PLACEHOLDERS
    assert len(merge.FIELDS) == 15


@pytest.mark.parametrize(
    ("bad", "named"),
    [
        ("{unknown}", "{unknown}"),
        ("{0}", "{0}"),
        ("{operator_name.__class__}", ""),
        ("{amount[0]}", ""),
        ("{amount:>99999}", "{amount}"),
        ("{company_name!r}", "{company_name}"),
    ],
)
def test_a_hostile_placeholder_is_refused_on_save_and_the_text_is_not_repeated(
    bad: str, named: str
) -> None:
    row = template("reminder_due")
    secret_line = "Hello {operator_name} 4111 is not here " + bad
    refusal = message_templates.check(row, row.subject, secret_line)
    assert refusal
    assert "Hello" not in refusal and "is not here" not in refusal
    assert "__class__" not in refusal and "[0]" not in refusal and ">99999" not in refusal
    if named:
        assert named in refusal
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(
        f"/admin-settings/templates/{row.pk}/", {"subject": row.subject, "body": secret_line}
    )
    assert response.status_code == 400
    row.refresh_from_db()
    assert bad not in row.body


def test_the_context_holds_every_placeholder_with_safe_values() -> None:
    lease, charge = a_world()
    values = context(lease, charge)
    assert set(values) == PLACEHOLDERS
    assert values["operator_name"] == "Ayşe Kaya"
    assert values["store_number"] == "1701"
    assert values["bank_last4"] == "4821"
    assert values["amount"].startswith("$")
    assert values["store_link"].startswith("http") and "/stores/" in values["store_link"]
    assert values["pay_link"].startswith("http")  # the statement link until Batch H
    assert values["retry_date"] == "" and values["return_reason"] == ""  # F's only


# --- D1, D2: Outbox mode, the allowlist, the sender -------------------------------------------


def test_without_a_key_a_message_is_held_with_its_body_and_its_window_time_stored() -> None:
    lease, charge = a_world()
    with override_settings(MAIL_ENABLED=True, GMAIL_SERVICE_ACCOUNT_JSON=""):
        message = queued(lease, charge, now=at(20))
        messaging.send(message.pk, now=at(20))
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    assert message.held_reason == HeldReason.NOT_SET_UP
    assert "store #1701" in message.rendered_body
    assert message.rendered_subject.startswith("Payment due")
    assert message.to_address == "store1701@example.com"
    # D7: a held row records when the window would have let it go.
    assert message.scheduled_for == at(9, 0, date(2026, 10, 7)).astimezone(UTC)


def test_on_the_sandbox_the_allowlist_is_compared_without_case_and_nothing_else_leaves(
    settings: Any, memory: Any
) -> None:
    mail_set_up(settings, sandbox=True, allowlist="Owner@Example.com\n\n")
    lease, charge = a_world(email="store1701@example.com")
    held = queued(lease, charge, now=at(10))
    messaging.send(held.pk, now=at(10))
    held.refresh_from_db()
    assert held.status == MessageStatus.HELD and held.held_reason == HeldReason.SANDBOX
    assert memory.sent == []
    LeaseBilling.objects.filter(lease=lease).update(email="OWNER@example.com")
    other = a_rent_charge(lease, date(2026, 11, 1), due="1500.00")
    allowed = queued(lease, other, now=at(10))
    messaging.send(allowed.pk, now=at(10))
    allowed.refresh_from_db()
    assert allowed.status == MessageStatus.SENT
    assert [message.to for message in memory.sent] == ["OWNER@example.com"]


@pytest.mark.parametrize(
    "text", ["a@example.com b@example.com", "not-an-address", "a@example.com\nnope"]
)
def test_a_bad_allowlist_line_is_refused_on_save_without_echo(text: str) -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(
        "/admin-settings/mail/",
        {
            "sender_email": "",
            "reply_to_email": "",
            "manager_emails": "",
            "sandbox_allowlist": text,
        },
        follow=True,
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "nope" not in page and "not-an-address" not in page and "b@example.com" not in page
    assert "Sandbox allowlist: write one plain e-mail address per line." in page
    assert CompanySettings.current().sandbox_allowlist == ""


# --- D3: one row per lease per charge per rule per occurrence ---------------------------------


def test_the_same_rule_twice_makes_one_row_occurrence_two_another_and_manual_never_collides() -> (
    None
):
    lease, charge = a_world()
    first = queued(lease, charge, "D", "overdue_day10", now=at(10))
    again = messaging.queue(
        "D", lease, charge, template("overdue_day10"), context(lease, charge, "D"), now=at(10)
    )
    second = queued(lease, charge, "D", "overdue_day10", occurrence=2, now=at(10))
    assert again is None and first.pk != second.pk
    assert OutboundMessage.objects.filter(lease=lease, rule="D").count() == 2
    assert Job.objects.filter(entity_id__in=[first.pk, second.pk]).count() == 2
    _, actor = client_for(Role.BILLING)
    by_hand = [
        queued(lease, charge, "manual", "overdue_day10", manual=True, actor=actor, now=at(10))
        for _ in range(2)
    ]
    assert by_hand[0].pk != by_hand[1].pk


def test_sms_is_refused_at_queue_time_and_nothing_is_written() -> None:
    lease, charge = a_world()
    sms = MessageTemplate.objects.create(
        code="sms_probe", channel=MessageChannel.SMS, subject="Rent", body="Hello {operator_name}"
    )
    with pytest.raises(messaging.ChannelUnavailable, match="SMS is not set up"):
        messaging.queue("B", lease, charge, sms, context(lease, charge), now=at(10))
    assert not OutboundMessage.objects.filter(lease=lease).exists()
    assert not Job.objects.exists()


def test_a_message_without_a_lease_is_refused_and_nothing_is_written() -> None:
    # Every message of the engine belongs to a lease (D3's one-per-lease rule cannot hold on
    # a null lease; PostgreSQL treats nulls as distinct in a unique constraint).
    lease, charge = a_world()
    with pytest.raises(ValueError):
        messaging.queue("B", None, charge, template("reminder_due"), context(lease, charge))
    assert not OutboundMessage.objects.exists()
    assert not Job.objects.exists()


def test_a_lease_without_a_billing_email_is_held_no_address() -> None:
    lease, charge = a_world(email="")
    message = queued(lease, charge, now=at(10))
    messaging.send(message.pk, now=at(10))
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    assert message.held_reason == HeldReason.NO_ADDRESS


# --- D7: the send window ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(10, 30), at(10, 30)),
        (at(9, 0), at(9, 0)),
        (at(8, 59), at(9, 0)),
        (at(17, 59), at(17, 59)),
        (at(18, 0), at(9, 0, date(2026, 10, 7))),
        (at(23, 30), at(9, 0, date(2026, 10, 7))),
        (at(0, 0), at(9, 0)),
        # The night DST ends (2026-11-01 02:00 New York): the next 9:00 is still 9:00 New York.
        (at(20, 0, date(2026, 10, 31)), at(9, 0, date(2026, 11, 1))),
        # The night DST starts (2026-03-08 02:00 New York).
        (at(20, 0, date(2026, 3, 7)), at(9, 0, date(2026, 3, 8))),
    ],
)
def test_the_send_window_schedules_the_next_nine_new_york(
    moment: datetime, expected: datetime
) -> None:
    lease, charge = a_world()
    message = queued(lease, charge, now=moment)
    assert message.scheduled_for == expected.astimezone(UTC)
    job = Job.objects.get(entity_id=message.pk)
    assert job.run_after == message.scheduled_for


def test_the_window_is_an_hour_apart_in_utc_across_the_dst_change() -> None:
    before = messaging.scheduled_for(at(20, 0, date(2026, 10, 31)).astimezone(UTC))
    after = messaging.scheduled_for(at(20, 0, date(2026, 11, 1)).astimezone(UTC))
    assert before.astimezone(NEW_YORK).hour == 9 and after.astimezone(NEW_YORK).hour == 9
    assert before.hour == 13 and after.hour == 14  # 9:00 EDT is 13:00 UTC; 9:00 EST is 14:00


def test_claim_next_leaves_a_job_until_its_time() -> None:
    lease, charge = a_world()
    message = queued(lease, charge, now=at(20))
    assert jobs.claim_next(now=at(20, 5).astimezone(UTC)) is None
    assert jobs.claim_next(now=at(8, 59, date(2026, 10, 7)).astimezone(UTC)) is None
    claimed = jobs.claim_next(now=at(9, 0, date(2026, 10, 7)).astimezone(UTC))
    assert claimed is not None and claimed.entity_id == message.pk


# --- D5: the Outbox and the timeline ----------------------------------------------------------


def test_the_outbox_lists_a_held_message_and_send_now_is_refused_while_not_set_up() -> None:
    lease, charge = a_world()
    with override_settings(MAIL_ENABLED=False):
        message = queued(lease, charge, now=at(10))
        messaging.send(message.pk, now=at(10))
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(OUTBOX).content.decode()
    assert "not sent (mail not set up)" in page
    assert "store1701@example.com" in page  # the full address: the page is admin's
    assert "#1701" in page and "Payment due" in page
    assert "Mail is not set up in this environment" in page
    with override_settings(MAIL_ENABLED=False):
        response = admin.post(f"{OUTBOX}{message.pk}/send-now/", follow=True)
    assert response.status_code == 200
    assert "the message stays held" in response.content.decode()
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    assert Job.objects.filter(entity_id=message.pk).count() == 1  # nothing queued again


def test_the_outbox_filters_by_status_and_ignores_a_bad_date() -> None:
    lease, charge = a_world()
    with override_settings(MAIL_ENABLED=False):
        message = queued(lease, charge, now=at(10))
        messaging.send(message.pk, now=at(10))
    admin, _ = client_for(Role.ADMIN)
    assert "1 message" in admin.get(f"{OUTBOX}?status=held").content.decode()
    assert "0 messages" in admin.get(f"{OUTBOX}?status=sent").content.decode()
    assert admin.get(f"{OUTBOX}?status=held&from=not-a-day&to=2026-13-40").status_code == 200
    assert admin.get(f"{OUTBOX}?status=drop%20table").status_code == 200


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_below_admin_the_outbox_and_send_now_are_refused(role: Role) -> None:
    lease, charge = a_world()
    with override_settings(MAIL_ENABLED=False):
        message = queued(lease, charge, now=at(10))
        messaging.send(message.pk, now=at(10))
    client, _ = client_for(role)
    assert client.get(OUTBOX).status_code in (302, 403)
    assert client.post(f"{OUTBOX}{message.pk}/send-now/").status_code in (302, 403)
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD


def test_a_message_appears_in_the_stores_timeline_with_its_result_and_a_masked_address() -> None:
    lease, charge = a_world()
    with override_settings(MAIL_ENABLED=False):
        message = queued(lease, charge, now=at(10))
        messaging.send(message.pk, now=at(10))
    billing, _ = client_for(Role.BILLING)
    page = billing.get(f"/stores/{lease.store_id}/?tab=contacts&show=emails").content.decode()
    assert "Payment due" in page
    assert "not sent (mail not set up)" in page
    assert "No e-mails yet." not in page
    assert "store1701@example.com" not in page  # masked below admin


# --- D4: the preview queues nothing; a huge body is refused -----------------------------------


def test_the_preview_renders_with_a_real_store_and_queues_nothing() -> None:
    lease, _ = a_world()
    admin, _ = client_for(Role.ADMIN)
    row = template("reminder_due")
    response = admin.post(
        f"/admin-settings/templates/{row.pk}/preview/",
        {"subject": row.subject, "body": row.body, "store": str(lease.store_id)},
    )
    assert response.status_code == 200
    page = response.content.decode()
    assert "Ayşe Kaya" in page and "#1701" in page
    assert "Nothing was saved or sent." in page
    assert not OutboundMessage.objects.exists()
    assert not Job.objects.exists()


def test_a_template_body_of_twenty_thousand_characters_is_refused_and_nothing_changes() -> None:
    admin, _ = client_for(Role.ADMIN)
    row = template("reminder_due")
    before = row.body
    response = admin.post(
        f"/admin-settings/templates/{row.pk}/", {"subject": row.subject, "body": "x" * 20_000}
    )
    assert response.status_code == 400
    row.refresh_from_db()
    assert row.body == before
