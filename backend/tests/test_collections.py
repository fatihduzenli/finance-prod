"""The collections ladder and the daily tick (docs/qa/PLAN-011b D4, D8, §7).

Days late are New York days: a charge due on the 3rd is 3 days late from 00:00 New York
on the 6th, and still 2 days late at 23:59 on the 5th. The tick queues one row and one
``message_send`` job per due step; the worker (``run_queued_jobs``) renders and sends
through ``mail.get_sender()``, here a :class:`~mail.memory.MemoryMailSender` behind the
real catch-all wrapper. The Gmail client itself is tested in ``test_mail.py``; here it
runs once end to end against ``httpx.MockTransport`` for the envelope and key fences.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email import message_from_bytes, policy
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory
from django.utils import timezone

import mail
from accounts.roles import Role
from ledger import merge
from ledger.models import (
    AuditLog,
    CalendarEntry,
    CollectionStep,
    ContactEntry,
    MessageStatus,
    MessageTemplate,
    OneOffCharge,
    Operator,
    OutboundMessage,
    RentCharge,
)
from ledger.services import collections, statement_links
from mail import gmail
from mail.memory import MemoryMailSender
from payments.models import Job, JobKind
from tests.auth_helpers import make_user
from tests.job_helpers import run_queued_jobs
from tests.test_ledger_models import (
    new_fee_type,
    new_lease,
    new_one_off_charge,
    new_operator,
    new_rent_charge,
    new_setup_charge,
    saved,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

NEW_YORK = ZoneInfo("America/New_York")
CATCH_ALL = "catchall@example.com"
CEO = "ceo@example.com"
DUE = date(2026, 10, 3)


def at(day: date, hour: int = 9, minute: int = 0) -> datetime:
    """A New York wall-clock moment, as an aware UTC datetime."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)
    return local.astimezone(UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the app's "now" (``django.utils.timezone.now``) to a moment."""

    def set_now(moment: datetime) -> None:
        monkeypatch.setattr(timezone, "now", lambda: moment)

    return set_now


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, settings: Any) -> MemoryMailSender:
    settings.MAIL_ENABLED = True
    settings.MAIL_CATCH_ALL = CATCH_ALL
    sender = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: sender)
    return sender


_emails = iter(range(1, 10_000))


def operator(email: str | None = None, **fields: Any) -> Operator:
    address = email if email is not None else f"op{next(_emails)}@example.com"
    return saved(new_operator(email=address or None, **fields))


def rent_charge(due_on: date = DUE, *, owner: Operator | None = None, **fields: Any) -> RentCharge:
    lease = saved(new_lease(operator=owner or operator()))
    values = {
        "due_on": due_on,
        "period_month": due_on.replace(day=1),
        "status": "unpaid",
        "amount_due": Decimal("2450.00"),
    }
    return saved(new_rent_charge(lease=lease, **{**values, **fields}))


def tick() -> str:
    out = StringIO()
    call_command("collections_tick", stdout=out)
    return out.getvalue()


def step(day_offset: int) -> CollectionStep:
    return CollectionStep.objects.select_related("template").get(day_offset=day_offset)


def messages_of(charge: RentCharge | OneOffCharge) -> list[OutboundMessage]:
    return list(
        OutboundMessage.objects.filter(charge_entity_id=charge.pk)
        .select_related("step")
        .order_by("queued_at", "id")
    )


# --- The seed --------------------------------------------------------------------------


def test_the_ladder_is_seeded_3_10_20() -> None:
    assert [
        (row.day_offset, row.template.code, row.active)
        for row in CollectionStep.objects.select_related("template")
    ] == [
        (3, "friendly_reminder", True),
        (10, "formal_notice", True),
        (20, "final_notice", True),
    ]
    for template in MessageTemplate.objects.all():
        assert merge.problems(template.subject) == merge.problems(template.body) == []


# --- New York days -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("due_on", "offset"),
    [
        (DUE, 3),
        (DUE, 10),
        (DUE, 20),
        # The tick on the day summer time ends (2026-11-01) still counts New York days.
        (date(2026, 10, 29), 3),
    ],
)
def test_a_step_is_due_from_00_00_new_york_and_not_at_23_59_the_day_before(
    clock: Any, memory: MemoryMailSender, due_on: date, offset: int
) -> None:
    charge = rent_charge(due_on)
    clock(at(due_on + timedelta(days=offset - 1), 23, 59))
    tick()
    before = [message.step.day_offset for message in messages_of(charge)]
    assert offset not in before
    assert before == ([] if offset == 3 else [{10: 3, 20: 10}[offset]])
    clock(at(due_on + timedelta(days=offset), 0, 0))
    tick()
    assert [message.step.day_offset for message in messages_of(charge)][-1] == offset


def test_the_tick_queues_the_step_and_the_worker_sends_to_the_catch_all(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator(CEO, display_name="Ceo Stores LLC")
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 6)))
    output = tick()
    assert "Collection e-mails queued: 1 " in output
    assert CEO not in output and "2450" not in output
    [queued] = messages_of(charge)
    assert queued.status == MessageStatus.QUEUED and queued.step.day_offset == 3
    job = Job.objects.get(kind=JobKind.MESSAGE_SEND)
    assert (job.entity_type, job.entity_id, job.requested_by) == (
        "outbound_message",
        queued.pk,
        None,
    )

    [done] = run_queued_jobs()
    assert done.status == "done" and done.result == "Sent to the catch-all address."
    [sent] = memory.sent
    assert sent.to == CATCH_ALL
    assert (
        sent.subject
        == f"[to: {CEO}] Friendly reminder: $2,450.00 for Store 900 Testville is past due"
    )
    assert ("X-Original-To", CEO) in sent.headers
    assert "Hello Ceo Stores LLC," in sent.text
    assert "due on October 3, 2026 and is now 3 days past due" in sent.text
    # PLAN-011b D4: the signed statement link, not the staff page.
    assert "http://localhost:8000/statement/" in sent.text
    assert f"/operators/{owner.pk}/statement/" not in sent.text
    [token] = re.findall(r"http://localhost:8000/statement/([^/\s]+)/", sent.text)
    assert statement_links.operator_for(token) == owner
    assert "Global Retail Groups" in sent.text
    assert sent.html.startswith("<html>") and "<br>" in sent.html

    queued.refresh_from_db()
    assert queued.status == MessageStatus.CATCH_ALL
    assert queued.to_address == CEO
    assert queued.subject.startswith("Friendly reminder: $2,450.00")
    assert len(queued.body_sha256) == 64 and queued.mail_message_id == "memory1"
    assert queued.sent_at is not None
    audit = AuditLog.objects.filter(entity_type="outbound_message", entity_id=str(queued.pk))
    assert list(audit.order_by("id").values_list("action", flat=True)) == [
        "collections.queued",
        "collections.sent",
    ]


def test_a_second_tick_sends_nothing(clock: Any, memory: MemoryMailSender) -> None:
    charge = rent_charge()
    clock(at(date(2026, 10, 6)))
    tick()
    run_queued_jobs()
    output = tick()
    assert "Collection e-mails queued: 0 " in output
    assert run_queued_jobs() == []
    assert len(messages_of(charge)) == 1 and len(memory.sent) == 1
    clock(at(date(2026, 10, 9)))
    tick()
    assert len(messages_of(charge)) == 1


def test_queue_due_twice_at_once_keeps_one_row(clock: Any, memory: MemoryMailSender) -> None:
    charge = rent_charge()
    today = date(2026, 10, 6)
    due = collections.due_steps(today)
    assert len(due) == 1
    first = collections.queue_due(today)
    # A second tick that read the due steps before the first one wrote its row.
    again = collections._queue(due[0].operator, due[0].charge_type, charge.pk, due[0].step)
    assert (first.queued, again) == (1, None)
    assert len(messages_of(charge)) == 1


def test_a_charge_first_seen_25_days_late_gets_the_day_20_step_only(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge()
    clock(at(DUE + timedelta(days=25)))
    tick()
    run_queued_jobs()
    assert [message.step.day_offset for message in messages_of(charge)] == [20]
    assert memory.sent[0].subject.startswith("[to: ") and "Final notice" in memory.sent[0].subject
    clock(at(DUE + timedelta(days=40)))
    tick()
    assert len(messages_of(charge)) == 1  # the ladder never goes back down


def test_the_ladder_climbs_one_step_at_a_time(clock: Any, memory: MemoryMailSender) -> None:
    charge = rent_charge()
    for days in (3, 4, 10, 11, 20, 21):
        clock(at(DUE + timedelta(days=days)))
        tick()
        run_queued_jobs()
    assert [message.step.day_offset for message in messages_of(charge)] == [3, 10, 20]
    assert len(memory.sent) == 3


def test_an_inactive_step_is_skipped(clock: Any, memory: MemoryMailSender) -> None:
    CollectionStep.objects.filter(day_offset=3).update(active=False)
    charge = rent_charge()
    clock(at(DUE + timedelta(days=5)))
    tick()
    assert messages_of(charge) == []


# --- Who is never chased -------------------------------------------------------------------


def test_mail_off_records_suppressed_and_sends_nothing(
    clock: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MAIL_ENABLED = False
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    charge = rent_charge()
    clock(at(date(2026, 10, 6)))
    tick()
    [done] = run_queued_jobs()
    assert done.result == "Not sent: mail is off."
    [message] = messages_of(charge)
    assert message.status == MessageStatus.SUPPRESSED and message.sent_at is None
    assert memory.sent == []
    assert AuditLog.objects.filter(
        action="collections.suppressed", entity_id=str(message.pk)
    ).exists()
    tick()
    assert len(messages_of(charge)) == 1  # logged: not sent again when mail comes on


def test_an_operator_handled_personally_is_never_chased(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator(handled_personally=True, handled_personally_note="The owner calls them.")
    charge = rent_charge(owner=owner)
    clock(at(DUE + timedelta(days=21)))
    tick()
    assert messages_of(charge) == []


def test_an_operator_without_an_email_is_chased_once_one_is_added(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator("")
    assert owner.email is None
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 6)))
    tick()
    assert messages_of(charge) == []
    owner.email = "added@example.com"
    owner.save()
    tick()
    run_queued_jobs()
    assert [message.status for message in messages_of(charge)] == [MessageStatus.CATCH_ALL]
    assert memory.sent[0].subject.startswith("[to: added@example.com] ")


def test_an_archived_operator_is_never_chased(clock: Any, memory: MemoryMailSender) -> None:
    owner = operator(archived_at=timezone.now())
    charge = rent_charge(owner=owner)
    clock(at(DUE + timedelta(days=5)))
    tick()
    assert messages_of(charge) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "paid", "amount_paid": Decimal("2450.00")},
        {"status": "waived"},
        {"status": "pending"},
        {"status": "partial", "amount_paid": Decimal("2450.00")},
    ],
)
def test_a_paid_waived_or_pending_rent_charge_is_never_chased(
    clock: Any, memory: MemoryMailSender, fields: dict[str, Any]
) -> None:
    charge = rent_charge(**fields)
    clock(at(DUE + timedelta(days=21)))
    tick()
    assert messages_of(charge) == []


def test_a_partly_paid_rent_charge_is_chased_for_its_balance(
    clock: Any, memory: MemoryMailSender
) -> None:
    rent_charge(status="partial", amount_paid=Decimal("450.00"))
    clock(at(date(2026, 10, 6)))
    tick()
    run_queued_jobs()
    assert "$2,000.00" in memory.sent[0].subject


def test_an_overdue_one_off_charge_is_chased_and_a_void_waived_or_paid_one_is_not(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator()
    chased = saved(new_one_off_charge(operator=owner, due_date=DUE))
    paid = saved(
        new_one_off_charge(
            operator=owner, due_date=DUE, amount_paid=Decimal("300.00"), status="paid"
        )
    )
    waived = saved(
        new_one_off_charge(
            operator=owner,
            due_date=DUE,
            status="waived",
            waived_at=timezone.now(),
            waive_note="Goodwill.",
        )
    )
    voided = saved(
        new_one_off_charge(
            operator=owner,
            due_date=DUE,
            status="voided",
            voided_at=timezone.now(),
            void_note="Cancelled.",
        )
    )
    lease = saved(new_lease(operator=owner))
    deposit = saved(
        new_setup_charge(
            lease=lease,
            operator=owner,
            fee_type=saved(new_fee_type(refundable=True)),
            due_date=DUE,
        )
    )
    clock(at(date(2026, 10, 6)))
    tick()
    run_queued_jobs()
    assert [message.status for message in messages_of(chased)] == [MessageStatus.CATCH_ALL]
    for charge in (paid, waived, voided, deposit):
        assert messages_of(charge) == []
    [sent] = memory.sent
    assert "$300.00 for your account" in sent.subject


def test_a_charge_paid_after_queueing_is_suppressed_at_send(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge()
    clock(at(date(2026, 10, 6)))
    tick()
    RentCharge.objects.filter(pk=charge.pk).update(amount_paid=Decimal("2450.00"), status="paid")
    [done] = run_queued_jobs()
    assert done.result == "Not sent (ChargeClosed)."
    [message] = messages_of(charge)
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "ChargeClosed")
    assert memory.sent == []


def test_an_operator_marked_after_queueing_is_suppressed_at_send(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator()
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 6)))
    tick()
    Operator.objects.filter(pk=owner.pk).update(handled_personally=True)
    run_queued_jobs()
    [message] = messages_of(charge)
    assert (message.status, message.error) == (
        MessageStatus.SUPPRESSED,
        "OperatorHandledPersonally",
    )
    assert memory.sent == []


def test_a_sender_failure_is_recorded_by_class_name(clock: Any, memory: MemoryMailSender) -> None:
    memory.fail_with = "ServerError"
    charge = rent_charge()
    clock(at(date(2026, 10, 6)))
    tick()
    [done] = run_queued_jobs()
    assert (done.status, done.result) == ("failed", "Failed: ServerError.")
    [message] = messages_of(charge)
    assert (message.status, message.error) == (MessageStatus.FAILED, "ServerError")
    assert AuditLog.objects.filter(action="collections.failed", entity_id=str(message.pk)).exists()


def test_without_a_catch_all_outside_production_nothing_is_sent(
    clock: Any, memory: MemoryMailSender, settings: Any
) -> None:
    settings.MAIL_CATCH_ALL = ""
    charge = rent_charge(owner=operator(CEO))
    clock(at(date(2026, 10, 6)))
    tick()
    run_queued_jobs()
    [message] = messages_of(charge)
    assert (message.status, message.error) == (MessageStatus.FAILED, "CatchAllRequired")
    assert memory.sent == []


# --- Templates and merge fields ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "{operator.__dict__}",
        "{__class__}",
        "{operator.__class__.__mro__}",
        "{operator[0]}",
        "{amount!r}",
        "{amount:>99999999}",
        "{amount:{operator}}",
        "{0}",
        "{}",
        "{unknown}",
        "{ operator }",
        "Unbalanced {operator",
        "Unbalanced }",
    ],
)
def test_a_template_with_anything_but_the_merge_fields_is_refused_on_save(text: str) -> None:
    for subject, body in ((text, "Body"), ("Subject", text)):
        template = MessageTemplate(code="test_refused", subject=subject, body=body)
        with pytest.raises(ValidationError):
            template.save()
        with pytest.raises(ValidationError):
            template.full_clean()
    assert not MessageTemplate.objects.filter(code="test_refused").exists()


def test_every_merge_field_is_accepted_and_literal_braces_stay() -> None:
    text = "{operator} {store} {amount} {due_date} {days_late} {statement_link} {company} {{x}}"
    saved(MessageTemplate(code="test_all_fields", subject="Hi {operator}", body=text))
    values = {name: f"<{name}>" for name in merge.MERGE_FIELDS}
    assert merge.render(text, values).endswith("<company> {x}")


def test_a_value_is_never_read_as_a_template() -> None:
    values = {name: "" for name in merge.MERGE_FIELDS}
    values["operator"] = "{company.__class__} {{"
    values["company"] = "SECRET"
    assert merge.render("Hi {operator}", values) == "Hi {company.__class__} {{"


def test_render_refuses_what_save_refuses_and_missing_values() -> None:
    values = {name: "x" for name in merge.MERGE_FIELDS}
    with pytest.raises(merge.TemplateRefused):
        merge.render("{operator.__dict__}", values)
    with pytest.raises(merge.TemplateRefused):
        merge.render("{operator}", {"operator": "x"})


def test_the_admin_form_refuses_an_unknown_merge_field() -> None:
    user, _ = make_user("ladder-admin", Role.ADMIN, superuser=True)
    request = RequestFactory().get("/admin/")
    request.user = user
    model_admin = admin.site._registry[MessageTemplate]
    form_class = model_admin.get_form(request)
    form = form_class(
        data={"code": "test_admin", "subject": "Hi {operator}", "body": "{__class__}"}
    )
    assert not form.is_valid()
    assert merge.NOT_ALLOWED in form.errors["body"]
    good = form_class(
        data={"code": "test_admin", "subject": "Hi {operator}", "body": "Owed {amount}."}
    )
    assert good.is_valid(), good.errors
    assert admin.site._registry[CollectionStep] is not None


def test_a_template_stored_around_the_check_is_never_rendered(
    clock: Any, memory: MemoryMailSender
) -> None:
    MessageTemplate.objects.filter(code="friendly_reminder").update(body="{operator.__dict__}")
    charge = rent_charge()
    clock(at(date(2026, 10, 6)))
    tick()
    [done] = run_queued_jobs()
    assert done.result == "Failed: TemplateRefused."
    [message] = messages_of(charge)
    assert (message.status, message.error) == (MessageStatus.FAILED, "TemplateRefused")
    assert memory.sent == []


def test_a_subject_value_cannot_inject_a_header(clock: Any, memory: MemoryMailSender) -> None:
    owner = operator(display_name="Evil\r\nBcc: x@example.com")
    MessageTemplate.objects.filter(code="friendly_reminder").update(subject="Hi {operator}")
    rent_charge(owner=owner)
    clock(at(date(2026, 10, 6)))
    tick()
    run_queued_jobs()
    [sent] = memory.sent
    assert "\n" not in sent.subject and "\r" not in sent.subject


# --- By hand -----------------------------------------------------------------------------


def send_url(charge: RentCharge) -> str:
    return f"/charges/{charge.pk}/send-step/"


def test_billing_sends_a_step_by_hand_and_a_repeat_needs_the_confirmation(
    clock: Any, memory: MemoryMailSender
) -> None:
    clock(at(date(2026, 10, 6)))
    client, user = client_for(Role.BILLING)
    charge = rent_charge()
    step10 = step(10)

    response = client.post(send_url(charge), {"step": str(step10.pk)})
    assert response.status_code == 302
    [first] = messages_of(charge)
    assert (first.manual, first.sent_by, first.step.day_offset) == (False, user, 10)

    client.post(send_url(charge), {"step": str(step10.pk)})
    assert len(messages_of(charge)) == 1  # refused without the confirmation

    client.post(send_url(charge), {"step": str(step10.pk), "confirm": "on"})
    rows = sorted(messages_of(charge), key=lambda row: row.manual)
    assert [(row.manual, row.sent_by) for row in rows] == [(False, user), (True, user)]
    run_queued_jobs()
    assert len(memory.sent) == 2
    assert {row.status for row in messages_of(charge)} == {MessageStatus.CATCH_ALL}

    # The ladder does not send day 10 again; day 3 is below it, so nothing at all.
    clock(at(DUE + timedelta(days=12)))
    tick()
    assert len(messages_of(charge)) == 2


def test_a_hand_send_is_refused_for_a_charge_not_overdue_or_an_opted_out_operator(
    clock: Any, memory: MemoryMailSender
) -> None:
    clock(at(date(2026, 10, 3)))
    _, user = client_for(Role.BILLING)
    due_today = rent_charge(DUE)
    outcome = collections.send_step_by_hand("rent_charge", due_today.pk, step(3).pk, user)
    assert (outcome.ok, outcome.message) == (False, collections.HAND_NOT_OVERDUE)
    opted = rent_charge(
        date(2026, 9, 1),
        owner=operator(handled_personally=True, handled_personally_note="Owner."),
    )
    outcome = collections.send_step_by_hand("rent_charge", opted.pk, step(3).pk, user)
    assert (outcome.ok, outcome.message) == (False, collections.HAND_OPTED_OUT)
    no_email = rent_charge(date(2026, 9, 1), owner=operator(""))
    outcome = collections.send_step_by_hand("rent_charge", no_email.pk, step(3).pk, user)
    assert (outcome.ok, outcome.message) == (False, collections.HAND_NO_EMAIL)
    outcome = collections.send_step_by_hand("rent_charge", no_email.pk, None, user)
    assert outcome.message == collections.HAND_UNKNOWN_STEP
    assert OutboundMessage.objects.count() == 0


def test_a_one_off_charge_is_sent_by_hand(clock: Any, memory: MemoryMailSender) -> None:
    clock(at(date(2026, 10, 6)))
    client, _ = client_for(Role.BILLING)
    charge = saved(new_one_off_charge(operator=operator(), due_date=DUE))
    response = client.post(f"/one-off-charges/{charge.pk}/send-step/", {"step": str(step(3).pk)})
    assert response.status_code == 302
    assert [row.charge_entity_type for row in messages_of(charge)] == ["one_off_charge"]


@pytest.mark.parametrize(
    "path",
    ["/charges/{charge}/send-step/", "/operators/{operator}/handled-personally/"],
)
def test_a_viewer_cannot_send_or_mark(path: str, clock: Any, memory: MemoryMailSender) -> None:
    clock(at(date(2026, 10, 6)))
    client, _ = client_for(Role.VIEWER)
    owner = operator()
    charge = rent_charge(owner=owner)
    url = path.format(charge=charge.pk, operator=owner.pk)
    response = client.post(url, {"step": str(step(3).pk), "handled_personally": "on", "note": "x"})
    assert response.status_code == 403
    assert OutboundMessage.objects.count() == 0
    owner.refresh_from_db()
    assert owner.handled_personally is False


# --- Handled personally and the pages -------------------------------------------------------


def test_billing_marks_an_operator_handled_personally_with_a_note() -> None:
    client, _ = client_for(Role.BILLING)
    owner = operator()
    url = f"/operators/{owner.pk}/handled-personally/"
    client.post(url, {"handled_personally": "on", "note": ""})
    owner.refresh_from_db()
    assert owner.handled_personally is False  # a note is required
    client.post(url, {"handled_personally": "on", "note": "Ana calls them every Friday."})
    owner.refresh_from_db()
    assert (owner.handled_personally, owner.handled_personally_note) == (
        True,
        "Ana calls them every Friday.",
    )
    assert AuditLog.objects.filter(
        action="operator.handled_personally_changed", entity_id=str(owner.pk)
    ).exists()
    client.post(url, {"note": "Ana calls them every Friday."})
    owner.refresh_from_db()
    assert owner.handled_personally is False


def test_a_card_number_in_the_note_is_refused() -> None:
    client, _ = client_for(Role.BILLING)
    owner = operator()
    client.post(
        f"/operators/{owner.pk}/handled-personally/",
        {"handled_personally": "on", "note": "card 4111 1111 1111 1111"},
    )
    owner.refresh_from_db()
    assert owner.handled_personally is False and owner.handled_personally_note == ""


def test_the_operator_page_lists_the_log_newest_first_with_masked_addresses(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator("collections.person@example.com")
    charge = rent_charge(owner=owner)
    for days in (3, 10):
        clock(at(DUE + timedelta(days=days)))
        tick()
        run_queued_jobs()
    client, _ = client_for(Role.VIEWER)
    page = client.get(f"/operators/{owner.pk}/").content.decode()
    log = page[page.index('id="message-log"') :]
    log = log[: log.index("</table>")]
    assert "c***@example.com" in log and "collections.person@example.com" not in log
    assert log.index("Day 10") < log.index("Day 3")
    assert "Sent to the catch-all address" in log
    assert 'id="handled-personally-form"' not in page  # viewer: no switch
    charge_page = client.get(f"/charges/{charge.pk}/").content.decode()
    assert "c***@example.com" in charge_page and 'id="send-step"' not in charge_page
    billing, _ = client_for(Role.BILLING)
    assert 'id="send-step"' in billing.get(f"/charges/{charge.pk}/").content.decode()
    assert 'id="handled-personally-form"' in billing.get(f"/operators/{owner.pk}/").content.decode()


def test_mask_address() -> None:
    assert collections.mask_address("ana@example.com") == "a***@example.com"
    assert collections.mask_address("broken") == "***"


# --- Morning e-mails ------------------------------------------------------------------------


def test_the_morning_email_goes_to_the_author_through_the_catch_all(
    clock: Any, memory: MemoryMailSender
) -> None:
    today = date(2026, 10, 20)
    author, _ = make_user("morning-author", Role.BILLING)
    asked = saved(
        CalendarEntry(
            kind="reminder",
            on=today,
            title="Call the landlord",
            body="About the roof.",
            email_morning=True,
            author=author,
        )
    )
    saved(CalendarEntry(kind="note", on=today, title="No e-mail", author=author))
    saved(
        CalendarEntry(
            kind="note",
            on=today + timedelta(days=1),
            title="Tomorrow",
            email_morning=True,
            author=author,
        )
    )
    clock(at(today, 9, 0))
    assert "Morning e-mails queued: 1." in tick()
    [job] = Job.objects.filter(kind=JobKind.CALENDAR_EMAIL)
    assert (job.entity_type, job.entity_id, job.params) == (
        "calendar_entry",
        asked.pk,
        {"on": "2026-10-20"},
    )
    [done] = run_queued_jobs()
    assert done.result == "Sent to the catch-all address."
    [sent] = memory.sent
    assert sent.to == CATCH_ALL
    assert sent.subject == "[to: morning-author@example.com] Reminder: Call the landlord"
    assert "About the roof." in sent.text and f"/calendar/{asked.pk}/" in sent.text
    assert "Morning e-mails queued: 0." in tick()
    assert run_queued_jobs() == []


def test_a_promise_due_today_reminds_its_author(clock: Any, memory: MemoryMailSender) -> None:
    today = date(2026, 10, 9)
    author, _ = make_user("promise-author", Role.BILLING)
    owner = operator(display_name="Promising Shop")
    saved(
        ContactEntry(
            operator=owner,
            kind="promise",
            happened_on=date(2026, 10, 5),
            body="Will pay Friday.",
            promised_date=today,
            promised_amount=Decimal("1500.00"),
            author=author,
        )
    )
    clock(at(today, 9, 0))
    assert "Promise reminders queued: 1." in tick()
    run_queued_jobs()
    [sent] = memory.sent
    assert (
        sent.subject == "[to: promise-author@example.com] Promise to pay due today: Promising Shop"
    )
    assert "$1,500.00 by 2026-10-09" in sent.text
    assert "Promise reminders queued: 0." in tick()


def test_a_morning_email_for_an_entry_moved_or_unflagged_sends_nothing(
    clock: Any, memory: MemoryMailSender
) -> None:
    today = date(2026, 10, 20)
    author, _ = make_user("moved-author", Role.BILLING)
    entry = saved(
        CalendarEntry(kind="note", on=today, title="Moved", email_morning=True, author=author)
    )
    clock(at(today, 9, 0))
    tick()
    CalendarEntry.objects.filter(pk=entry.pk).update(on=today + timedelta(days=2))
    [done] = run_queued_jobs()
    assert done.status == "done" and "no longer asks" in done.result
    assert memory.sent == []


# --- The key fences (D3, §7 adversarial) ---------------------------------------------------


def _everything_stored() -> str:
    """Every row the ladder and the jobs wrote, as text."""
    parts = [
        json.dumps(list(OutboundMessage.objects.values()), default=str),
        json.dumps(
            list(AuditLog.objects.values("action", "before", "after", "entity_id")), default=str
        ),
        json.dumps(list(Job.objects.values("params", "result")), default=str),
    ]
    return "\n".join(parts)


def test_a_marker_key_never_reaches_a_log_a_row_a_page_or_an_error(
    clock: Any, settings: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "PRIVATE-KEY-MARKER-do-not-use-L3X"
    settings.MAIL_ENABLED = True
    settings.MAIL_CATCH_ALL = CATCH_ALL
    settings.GMAIL_SENDER = "billing@example.com"
    settings.GMAIL_SERVICE_ACCOUNT_JSON = json.dumps(
        {
            "type": "service_account",
            "client_email": "mailer@finance-test.iam.example.com",
            "private_key": marker,
            "private_key_id": "MARKER-KEY-ID-do-not-use-1",
            "token_uri": gmail.TOKEN_URI,
        }
    )
    monkeypatch.setattr(mail, "_senders", {})
    caplog.set_level(logging.DEBUG)
    owner = operator(CEO)
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 6)))
    tick()
    [done] = run_queued_jobs()
    [message] = messages_of(charge)
    assert message.status == MessageStatus.FAILED
    assert message.error and " " not in message.error
    client, _ = client_for(Role.ADMIN)
    pages = client.get(f"/operators/{owner.pk}/").content.decode()
    pages += client.get(f"/charges/{charge.pk}/").content.decode()
    pages += client.get("/audit/").content.decode()
    for text in (caplog.text, _everything_stored(), pages, done.result, message.error):
        assert marker not in text
        assert "MARKER-KEY-ID" not in text


@pytest.fixture(scope="module")
def generated_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def test_end_to_end_through_the_gmail_client_the_envelope_is_the_catch_all(
    clock: Any,
    settings: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    generated_key: str,
) -> None:
    token = "FAKE-END-TO-END-TOKEN-do-not-use"
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == gmail.TOKEN_URI:
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        return httpx.Response(200, json={"id": "18f0e2e"})

    info = {
        "type": "service_account",
        "client_email": "mailer@finance-test.iam.example.com",
        "private_key": generated_key,
        "token_uri": gmail.TOKEN_URI,
    }
    settings.MAIL_ENABLED = True
    settings.MAIL_CATCH_ALL = CATCH_ALL
    settings.APP_ENV = "staging"
    built = gmail.GmailApiSender(
        info, "billing@example.com", transport=httpx.MockTransport(answer), sleep=lambda _: None
    )
    monkeypatch.setattr(mail, "_base_sender", lambda: built)
    caplog.set_level(logging.DEBUG)
    owner = operator(CEO)
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 6)))
    tick()
    run_queued_jobs()
    [message] = messages_of(charge)
    assert (message.status, message.mail_message_id) == (MessageStatus.CATCH_ALL, "18f0e2e")
    send = requests[-1]
    raw = json.loads(send.content)["raw"]
    envelope = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    assert envelope["To"] == CATCH_ALL
    assert envelope["X-Original-To"] == CEO
    assert str(envelope["Subject"]).startswith(f"[to: {CEO}] ")
    assert CEO not in str(envelope["To"])
    body_lines = [line for line in generated_key.splitlines() if "-----" not in line]
    stored = _everything_stored()
    for text in (caplog.text, stored):
        assert token not in text
        assert all(line not in text for line in body_lines)


def test_job_kinds_are_registered() -> None:
    from payments import jobs
    from payments.jobs import runners

    assert jobs.ENTITY_OF_KIND[JobKind.MESSAGE_SEND] == "outbound_message"
    assert jobs.ENTITY_OF_KIND[JobKind.CALENDAR_EMAIL] == "calendar_entry"
    assert {JobKind.MESSAGE_SEND, JobKind.CALENDAR_EMAIL} <= set(runners.RUNNERS)
    promise = saved(
        ContactEntry(
            operator=operator(),
            kind="promise",
            happened_on=DUE,
            body="Soon.",
            promised_date=DUE,
            promised_amount=Decimal("1.00"),
        )
    )
    assert (
        jobs.enqueue(JobKind.CALENDAR_EMAIL, promise, {"on": "2026-10-03"}, None).entity_type
        == "contact_entry"
    )
    with pytest.raises(ValueError):
        jobs.enqueue(JobKind.MESSAGE_SEND, promise, None, None)
