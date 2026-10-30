"""Messages about charges and the daily tick (docs/qa/PLAN-011b D4, D8, §7; re-pinned to the
messaging engine of PLAN-014 D2 to D7).

The collections ladder is retired (PLAN-014 D6): the tick no longer queues messages (the
reminder rules are Batch E's), so these tests queue through
:func:`ledger.services.messaging.queue` (a rule's message, rendered then and stored) or
"Send a message now" (a manual row). The worker (``run_queued_jobs``) sends the stored text
through ``mail.get_sender()``, here a :class:`~mail.memory.MemoryMailSender` with mail set
up off the sandbox (the Outbox and the sandbox allowlist have tests of their own here and
in ``test_messaging_engine.py``). The Gmail client itself is tested in ``test_mail.py``;
here it runs once end to end against ``httpx.MockTransport`` for the envelope and key
fences.

Replaced by the engine (D3, D6): the ladder's seed (3, 10, 20), its New York day steps, the
"one step at a time" and "first seen late" rules, the inactive step, the hand send of a
step with its confirmation and the catch-all envelope; their successors are the seven
seeded templates, the send window, the one-row-per-rule constraint, "Send a message now"
and the allowlist.
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
from django.utils import timezone

import mail
from accounts.roles import Role
from ledger import merge
from ledger.models import (
    AuditLog,
    CalendarEntry,
    CompanySettings,
    ContactEntry,
    Lease,
    LeaseBilling,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OneOffCharge,
    Operator,
    OutboundMessage,
    RentCharge,
)
from ledger.services import collections, leases, messaging, statement_links
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
    new_rent_history,
    new_setup_charge,
    saved,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

NEW_YORK = ZoneInfo("America/New_York")
CEO = "ceo@example.com"
SENDER = "billing@example.com"
DUE = date(2026, 10, 3)


def at(day: date, hour: int = 10, minute: int = 0) -> datetime:
    """A New York wall-clock moment, as an aware UTC datetime (10:00: in the send window)."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)
    return local.astimezone(UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the app's "now" (``django.utils.timezone.now``) to a moment."""

    def set_now(moment: datetime) -> None:
        monkeypatch.setattr(timezone, "now", lambda: moment)

    return set_now


def mail_set_up(settings: Any, *, sandbox: bool = False, allowlist: str = "") -> None:
    """Mail set up (PLAN-014 D1): on, a key, a sender; off the sandbox unless asked."""
    settings.MAIL_ENABLED = True
    settings.GMAIL_SERVICE_ACCOUNT_JSON = "{}"
    settings.STAX_ENV = "sandbox" if sandbox else "live"
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(
        sender_email=SENDER, sandbox_allowlist=allowlist
    )


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, settings: Any) -> MemoryMailSender:
    mail_set_up(settings)
    sender = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: sender)
    return sender


_emails = iter(range(1, 10_000))


def operator(email: str | None = None, **fields: Any) -> Operator:
    address = email if email is not None else f"op{next(_emails)}@example.com"
    return saved(new_operator(email=address or None, **fields))


def billed(lease: Lease, email: str | None) -> Lease:
    """Give ``lease`` a billing row with ``email`` (PLAN-014 C5: a message goes to the
    billing e-mail of the lease that bills the charge)."""
    LeaseBilling.objects.update_or_create(lease=lease, defaults={"email": email or ""})
    return lease


def rent_charge(due_on: date = DUE, *, owner: Operator | None = None, **fields: Any) -> RentCharge:
    """A rent charge of a lease billed to its operator's e-mail address."""
    owner = owner or operator()
    lease = billed(saved(new_lease(operator=owner)), owner.email)
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
    # The tick also queues the day's read of the dispute list (PLAN-012 D8, tested in
    # test_disputes.py); these tests are about the mail it queues.
    Job.objects.filter(kind=JobKind.DISPUTES_SYNC).delete()
    return out.getvalue()


def queue_rule(
    charge: RentCharge | OneOffCharge,
    rule: str = MessageRule.C,
    code: str = "overdue_day5",
    **kwargs: Any,
) -> OutboundMessage | None:
    """Queue ``rule``'s message for ``charge`` as the engine does (Batch E will call it)."""
    lease = leases.billing_lease(charge)
    assert lease is not None
    template = MessageTemplate.objects.get(code=code)
    context = messaging.context_for(lease, charge, rule, today=timezone.localdate())
    return messaging.queue(rule, lease, charge, template, context, **kwargs)


def messages_of(charge: RentCharge | OneOffCharge) -> list[OutboundMessage]:
    return list(
        OutboundMessage.objects.filter(charge_entity_id=str(charge.pk)).order_by("queued_at", "id")
    )


def template_id(code: str = "overdue_day5") -> str:
    return str(MessageTemplate.objects.get(code=code).pk)


# --- The seed --------------------------------------------------------------------------


def test_the_seven_templates_are_seeded_and_the_ladder_is_retired() -> None:
    active = {
        row.code: (row.name, row.internal)
        for row in MessageTemplate.objects.filter(active=True).exclude(code="final_statement")
    }
    assert active == {
        "reminder_autopay": ("A — Rent will be debited", False),
        "reminder_due": ("B — Payment due", False),
        "overdue_day5": ("C — Payment overdue", False),
        "overdue_day10": ("D — Notice: overdue", False),
        "internal_day30": ("E — 30 days overdue (internal)", True),
        "returned_debit": ("F — Payment returned", False),
        "returned_debit_no_retry": ("F — Payment returned (no retry)", False),
    }
    assert set(MessageTemplate.objects.filter(active=False).values_list("code", flat=True)) == {
        "friendly_reminder",
        "formal_notice",
        "final_notice",
    }
    for template in MessageTemplate.objects.filter(active=True):
        fields = merge.fields_for(template.code)
        assert merge.problems(template.subject, fields) == merge.problems(template.body, fields)
        assert merge.problems(template.body, fields) == []
    assert not hasattr(__import__("ledger.models").models, "CollectionStep")


# --- Queue and send ------------------------------------------------------------------------


def test_a_queued_message_is_rendered_stored_and_sent_by_the_worker(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator(CEO, display_name="Ceo Stores LLC")
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 8)))
    queued = queue_rule(charge)
    assert queued is not None and queued.status == MessageStatus.QUEUED
    assert (queued.rule, queued.occurrence, queued.to_address) == ("C", 1, CEO)
    assert queued.rendered_subject == "Payment overdue — #900 Store 900 Testville"
    assert "Hello Ceo Stores LLC," in queued.rendered_body
    assert "We have not yet received $2,450.00 for Rent October 2026" in queued.rendered_body
    assert "which was due on Oct 3, 2026." in queued.rendered_body
    # PLAN-011b D4: the signed statement link is {pay_link} until the portal exists.
    [token] = re.findall(r"http://localhost:8000/statement/([^/\s]+)/", queued.rendered_body)
    assert statement_links.operator_for(token) == owner
    assert len(queued.body_sha256) == 64
    job = Job.objects.get(kind=JobKind.MESSAGE_SEND)
    assert (job.entity_type, job.entity_id, job.requested_by) == (
        "outbound_message",
        queued.pk,
        None,
    )
    assert job.run_after == queued.scheduled_for == at(date(2026, 10, 8))

    [done] = run_queued_jobs()
    assert done.status == "done" and done.result == "Sent."
    [sent] = memory.sent
    assert (sent.to, sent.subject, sent.text) == (
        CEO,
        queued.rendered_subject,
        queued.rendered_body,
    )
    assert sent.html.startswith("<html>") and "<br>" in sent.html
    queued.refresh_from_db()
    assert queued.status == MessageStatus.SENT and queued.mail_message_id == "memory1"
    assert queued.sent_at is not None
    audit = AuditLog.objects.filter(entity_type="outbound_message", entity_id=str(queued.pk))
    assert list(audit.order_by("id").values_list("action", flat=True)) == [
        "message.queued",
        "message.sent",
    ]


def test_the_tick_queues_no_message_now_that_the_ladder_is_retired(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge()
    clock(at(DUE + timedelta(days=10)))
    output = tick()
    assert "Collection e-mails queued" not in output
    assert messages_of(charge) == [] and run_queued_jobs() == []


def test_the_tick_creates_the_missing_rent_charges_once(clock: Any) -> None:
    """PLAN-012b-b D3: the daily tick creates the charges, as the run page's button does."""
    today = date(2026, 10, 16)
    started = saved(new_lease(debit_day=1, started_on=date(2026, 10, 13)))
    saved(
        new_rent_history(
            lease=started, amount=Decimal("2000.00"), effective_from=date(2026, 10, 13)
        )
    )
    ended = saved(new_lease(started_on=date(2026, 8, 1), ended_on=date(2026, 9, 30)))
    saved(new_rent_history(lease=ended, effective_from=date(2026, 8, 1)))
    for month in (8, 9):
        saved(
            new_rent_charge(
                lease=ended, period_month=date(2026, month, 1), due_on=date(2026, month, 5)
            )
        )
    clock(at(today))
    output = tick()
    assert "Rent charges created: 0. Created (catch-up): 1. Already created: 0." in output
    [charge] = RentCharge.objects.filter(lease=started)
    assert (charge.period_month, charge.seq, charge.amount_due, charge.due_on) == (
        date(2026, 10, 1),
        1,
        Decimal("1225.81"),
        date(2026, 10, 13),
    )
    created = AuditLog.objects.get(action="rent_charge.created", entity_id=str(charge.pk))
    assert created.actor is None
    assert created.after is not None and created.after["catch_up"] is True
    assert RentCharge.objects.filter(lease=ended).count() == 2
    output = tick()
    assert "Rent charges created: 0. Created (catch-up): 0. Already created: 0." in output
    assert RentCharge.objects.count() == 3


def test_the_same_rule_twice_keeps_one_row_and_the_next_occurrence_is_another(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge()
    clock(at(date(2026, 10, 13)))
    assert queue_rule(charge, MessageRule.D, "overdue_day10") is not None
    assert queue_rule(charge, MessageRule.D, "overdue_day10") is None
    assert queue_rule(charge, MessageRule.D, "overdue_day10", occurrence=2) is not None
    found = sorted((row.rule, row.occurrence) for row in messages_of(charge))
    assert found == [("D", 1), ("D", 2)]
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 2


# --- Who is never mailed -------------------------------------------------------------------


def test_mail_not_set_up_holds_the_message_in_the_outbox(
    clock: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MAIL_ENABLED = False
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    charge = rent_charge()
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    [done] = run_queued_jobs()
    assert done.result == "Held: not sent (mail not set up)."
    [message] = messages_of(charge)
    assert (message.status, message.held_reason) == (MessageStatus.HELD, "not_set_up")
    assert message.sent_at is None and memory.sent == []
    assert AuditLog.objects.filter(action="message.held", entity_id=str(message.pk)).exists()


def test_an_operator_handled_personally_has_its_messages_held(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator()
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    Operator.objects.filter(pk=owner.pk).update(
        handled_personally=True, handled_personally_note="The owner calls them."
    )
    run_queued_jobs()
    [message] = messages_of(charge)
    assert (message.status, message.held_reason) == (MessageStatus.HELD, "handled_personally")
    assert memory.sent == []


def test_a_lease_without_a_billing_email_holds_the_message(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator("")
    assert owner.email is None
    charge = rent_charge(owner=owner)
    assert LeaseBilling.objects.get(lease=charge.lease).email == ""
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    # PLAN-014 C5: the operator's own e-mail is not the billing address.
    Operator.objects.filter(pk=owner.pk).update(email="operator@example.com")
    run_queued_jobs()
    [message] = messages_of(charge)
    assert (message.status, message.held_reason) == (MessageStatus.HELD, "no_address")
    assert memory.sent == []


def test_a_billing_email_added_after_queueing_is_used_at_send(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator("")
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    billed(charge.lease, "added@example.com")
    run_queued_jobs()
    assert [message.status for message in messages_of(charge)] == [MessageStatus.SENT]
    assert memory.sent[0].to == "added@example.com"


def test_an_archived_operator_is_never_mailed(clock: Any, memory: MemoryMailSender) -> None:
    owner = operator()
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    Operator.objects.filter(pk=owner.pk).update(archived_at=timezone.now())
    run_queued_jobs()
    [message] = messages_of(charge)
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "OperatorArchived")
    assert memory.sent == []


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "paid", "amount_paid": Decimal("2450.00")},
        {"status": "waived"},
        {"status": "pending"},
        {"status": "partial", "amount_paid": Decimal("2450.00")},
    ],
)
def test_a_charge_closed_after_queueing_is_suppressed_at_send(
    clock: Any, memory: MemoryMailSender, fields: dict[str, Any]
) -> None:
    charge = rent_charge()
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    RentCharge.objects.filter(pk=charge.pk).update(**fields)
    [done] = run_queued_jobs()
    assert done.result == "Not sent (ChargeClosed)."
    [message] = messages_of(charge)
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "ChargeClosed")
    assert memory.sent == []


def test_a_partly_paid_rent_charge_is_named_for_its_balance(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge(status="partial", amount_paid=Decimal("450.00"))
    clock(at(date(2026, 10, 8)))
    queued = queue_rule(charge)
    assert queued is not None and "$2,000.00" in queued.rendered_body


def test_a_one_off_charge_without_a_lease_is_billed_through_the_first_active_lease(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator()
    # PLAN-014 C5: a one-off charge without a lease is billed through the operator's first
    # active lease (its billing e-mail).
    lease = billed(saved(new_lease(operator=owner)), owner.email)
    chased = saved(new_one_off_charge(operator=owner, due_date=DUE, description="Sign repair"))
    clock(at(date(2026, 10, 8)))
    queued = queue_rule(chased)
    assert queued is not None and queued.lease == lease
    assert "$300.00 for Sign repair" in queued.rendered_body
    run_queued_jobs()
    assert [message.status for message in messages_of(chased)] == [MessageStatus.SENT]
    deposit = saved(
        new_setup_charge(
            lease=lease,
            operator=owner,
            fee_type=saved(new_fee_type(refundable=True)),
            due_date=DUE,
        )
    )
    queue_rule(deposit)
    run_queued_jobs()
    # A deposit is never chased: its message is suppressed at send.
    assert [message.error for message in messages_of(deposit)] == ["ChargeClosed"]


def test_a_sender_failure_is_recorded_by_class_name(clock: Any, memory: MemoryMailSender) -> None:
    memory.fail_with = "ServerError"
    charge = rent_charge()
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    [done] = run_queued_jobs()
    assert (done.status, done.result) == ("failed", "Failed: ServerError.")
    [message] = messages_of(charge)
    assert (message.status, message.error) == (MessageStatus.FAILED, "ServerError")
    assert AuditLog.objects.filter(action="message.failed", entity_id=str(message.pk)).exists()


def test_on_the_sandbox_an_address_off_the_allowlist_is_held_and_never_handed_to_the_sender(
    clock: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    mail_set_up(settings, sandbox=True, allowlist="owner@example.com")
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    held = rent_charge(owner=operator(CEO))
    allowed = rent_charge(owner=operator("Owner@Example.com"))
    clock(at(date(2026, 10, 8)))
    queue_rule(held)
    queue_rule(allowed)
    run_queued_jobs()
    assert [(row.status, row.held_reason) for row in messages_of(held)] == [
        (MessageStatus.HELD, "sandbox")
    ]
    assert [row.status for row in messages_of(allowed)] == [MessageStatus.SENT]
    assert [message.to for message in memory.sent] == ["owner@example.com"]


# --- Templates and placeholders (PLAN-014 D4) -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "{operator_name.__dict__}",
        "{__class__}",
        "{operator_name.__class__.__mro__}",
        "{operator_name[0]}",
        "{amount!r}",
        "{amount:>99999999}",
        "{amount:{operator_name}}",
        "{0}",
        "{}",
        "{unknown}",
        "{ operator_name }",
        "{operator}",
        "{retry_sentence}",
        "Unbalanced {operator_name",
        "Unbalanced }",
    ],
)
def test_a_template_with_anything_but_the_placeholders_is_refused_on_save(text: str) -> None:
    for subject, body in ((text, "Body"), ("Subject", text)):
        template = MessageTemplate(code="test_refused", subject=subject, body=body)
        with pytest.raises(ValidationError):
            template.save()
        with pytest.raises(ValidationError):
            template.full_clean()
    assert not MessageTemplate.objects.filter(code="test_refused").exists()


def test_every_placeholder_is_accepted_and_literal_braces_stay() -> None:
    text = " ".join(f"{{{name}}}" for name in merge.FIELDS) + " {{x}}"
    saved(MessageTemplate(code="test_all_fields", subject="Hi {operator_name}", body=text))
    values = {name: f"<{name}>" for name in merge.FIELDS}
    assert merge.render(text, values).endswith("<company_name> {x}")
    assert len(merge.FIELDS) == 15


def test_a_value_is_never_read_as_a_template() -> None:
    values = {name: "" for name in merge.FIELDS}
    values["operator_name"] = "{company_name.__class__} {{"
    values["company_name"] = "SECRET"
    assert merge.render("Hi {operator_name}", values) == "Hi {company_name.__class__} {{"


def test_render_refuses_what_save_refuses_and_missing_values() -> None:
    values = {name: "x" for name in merge.FIELDS}
    with pytest.raises(merge.TemplateRefused):
        merge.render("{operator_name.__dict__}", values)
    with pytest.raises(merge.TemplateRefused):
        merge.render("{operator_name}", {"operator_name": "x"})


def test_an_unknown_placeholder_is_named_and_nothing_else_is_repeated() -> None:
    fields = merge.FIELDS
    assert merge.problems("Owed {amount} by {unknown_field}.", fields) == [
        "{unknown_field} is not a placeholder this template can use."
    ]
    hostile = "Hi {4111 1111 1111 1111}"
    assert merge.problems(hostile, fields) == [merge.UNKNOWN_UNNAMED]
    assert merge.problems("{amount:>9}", fields) == [
        "Write {amount} as it is: a placeholder takes no format or conversion."
    ]
    # The final statement keeps its own set.
    assert merge.problems("{operator_name}", merge.fields_for("final_statement")) != []
    assert merge.problems("{operator} {refund_due}", merge.fields_for("final_statement")) == []


def test_the_templates_are_no_longer_in_the_django_admin() -> None:
    assert MessageTemplate not in admin.site._registry


def test_a_template_stored_around_the_check_refuses_to_queue(
    clock: Any, memory: MemoryMailSender
) -> None:
    MessageTemplate.objects.filter(code="overdue_day5").update(body="{operator_name.__dict__}")
    charge = rent_charge()
    clock(at(date(2026, 10, 8)))
    with pytest.raises(merge.TemplateRefused):
        queue_rule(charge)
    assert messages_of(charge) == [] and memory.sent == []


def test_a_subject_value_cannot_inject_a_header(clock: Any, memory: MemoryMailSender) -> None:
    owner = operator(display_name="Evil\r\nBcc: x@example.com")
    MessageTemplate.objects.filter(code="overdue_day5").update(subject="Hi {operator_name}")
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 8)))
    queued = queue_rule(charge)
    assert queued is not None and "\n" not in queued.rendered_subject
    run_queued_jobs()
    [sent] = memory.sent
    assert "\n" not in sent.subject and "\r" not in sent.subject


# --- By hand: "Send a message now" (PLAN-014 D6) ----------------------------------------------


def send_url(charge: RentCharge) -> str:
    return f"/charges/{charge.pk}/send-message/"


def test_billing_sends_a_message_now_and_each_send_is_its_own_manual_row(
    clock: Any, memory: MemoryMailSender
) -> None:
    clock(at(date(2026, 10, 8)))
    client, user = client_for(Role.BILLING)
    charge = rent_charge()

    response = client.post(send_url(charge), {"template": template_id()})
    assert response.status_code == 302
    client.post(send_url(charge), {"template": template_id()})
    rows = messages_of(charge)
    assert [(row.rule, row.manual, row.sent_by) for row in rows] == [
        ("manual", True, user),
        ("manual", True, user),
    ]
    run_queued_jobs()
    assert len(memory.sent) == 2
    assert {row.status for row in messages_of(charge)} == {MessageStatus.SENT}


def test_a_hand_send_is_refused_for_a_closed_charge_an_opted_out_operator_or_no_address(
    clock: Any, memory: MemoryMailSender
) -> None:
    clock(at(date(2026, 10, 8)))
    _, user = client_for(Role.BILLING)
    paid = rent_charge(status="paid", amount_paid=Decimal("2450.00"))
    template = MessageTemplate.objects.get(code="overdue_day5").pk
    outcome = messaging.send_by_hand("rent_charge", paid.pk, template, user)
    assert (outcome.ok, outcome.message) == (False, messaging.HAND_NOT_OPEN)
    opted = rent_charge(
        date(2026, 9, 1),
        owner=operator(handled_personally=True, handled_personally_note="Owner."),
    )
    outcome = messaging.send_by_hand("rent_charge", opted.pk, template, user)
    assert (outcome.ok, outcome.message) == (False, messaging.HAND_OPTED_OUT)
    no_email = rent_charge(date(2026, 9, 1), owner=operator(""))
    outcome = messaging.send_by_hand("rent_charge", no_email.pk, template, user)
    assert (outcome.ok, outcome.message) == (False, messaging.HAND_NO_EMAIL)
    outcome = messaging.send_by_hand("rent_charge", no_email.pk, None, user)
    assert outcome.message == messaging.HAND_UNKNOWN_TEMPLATE
    internal = MessageTemplate.objects.get(code="internal_day30").pk
    outcome = messaging.send_by_hand("rent_charge", no_email.pk, internal, user)
    assert outcome.message == messaging.HAND_UNKNOWN_TEMPLATE
    assert OutboundMessage.objects.count() == 0


def test_a_one_off_charge_is_sent_by_hand(clock: Any, memory: MemoryMailSender) -> None:
    clock(at(date(2026, 10, 8)))
    client, _ = client_for(Role.BILLING)
    owner = operator()
    billed(saved(new_lease(operator=owner)), owner.email)  # its first active lease bills it
    charge = saved(new_one_off_charge(operator=owner, due_date=DUE))
    response = client.post(
        f"/one-off-charges/{charge.pk}/send-message/", {"template": template_id()}
    )
    assert response.status_code == 302
    assert [row.charge_entity_type for row in messages_of(charge)] == ["one_off_charge"]


@pytest.mark.parametrize(
    "path",
    ["/charges/{charge}/send-message/", "/operators/{operator}/handled-personally/"],
)
def test_a_viewer_cannot_send_or_mark(path: str, clock: Any, memory: MemoryMailSender) -> None:
    clock(at(date(2026, 10, 8)))
    client, _ = client_for(Role.VIEWER)
    owner = operator()
    charge = rent_charge(owner=owner)
    url = path.format(charge=charge.pk, operator=owner.pk)
    response = client.post(
        url, {"template": template_id(), "handled_personally": "on", "note": "x"}
    )
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
    clock(at(DUE + timedelta(days=5)))
    queue_rule(charge, MessageRule.C, "overdue_day5")
    run_queued_jobs()
    clock(at(DUE + timedelta(days=10)))
    queue_rule(charge, MessageRule.D, "overdue_day10")
    run_queued_jobs()
    client, _ = client_for(Role.VIEWER)
    page = client.get(f"/operators/{owner.pk}/").content.decode()
    log = page[page.index('id="message-log"') :]
    log = log[: log.index("</table>")]
    assert "c***@example.com" in log and "collections.person@example.com" not in log
    assert log.index("D — Notice: overdue") < log.index("C — Payment overdue")
    assert "Sent" in log
    assert 'id="handled-personally-form"' not in page  # viewer: no switch
    charge_page = client.get(f"/charges/{charge.pk}/").content.decode()
    assert "c***@example.com" in charge_page and 'id="send-message"' not in charge_page
    billing, _ = client_for(Role.BILLING)
    assert 'id="send-message"' in billing.get(f"/charges/{charge.pk}/").content.decode()
    assert 'id="handled-personally-form"' in billing.get(f"/operators/{owner.pk}/").content.decode()


def test_mask_address() -> None:
    assert collections.mask_address("ana@example.com") == "a***@example.com"
    assert collections.mask_address("broken") == "***"


# --- Morning e-mails ------------------------------------------------------------------------


def test_the_morning_email_goes_to_the_author(clock: Any, memory: MemoryMailSender) -> None:
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
    assert done.result == "Sent."
    [sent] = memory.sent
    assert sent.to == "morning-author@example.com"
    assert sent.subject == "Reminder: Call the landlord"
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
    assert (sent.to, sent.subject) == (
        "promise-author@example.com",
        "Promise to pay due today: Promising Shop",
    )
    assert "$1,500.00 by Oct 9, 2026" in sent.text
    assert "Promise reminders queued: 0." in tick()


def test_on_the_sandbox_a_morning_email_off_the_allowlist_is_held(
    clock: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PLAN-014 D2: the allowlist covers every path, the morning e-mails included.
    mail_set_up(settings, sandbox=True, allowlist="someone.else@example.com")
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    today = date(2026, 10, 20)
    author, _ = make_user("sandbox-author", Role.BILLING)
    saved(CalendarEntry(kind="note", on=today, title="Held", email_morning=True, author=author))
    clock(at(today, 9, 0))
    tick()
    [done] = run_queued_jobs()
    assert done.status == "done" and done.result == calendar_held()
    assert memory.sent == []


def calendar_held() -> str:
    from ledger.services import calendar

    return calendar.MORNING_SENT[mail.HELD]


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
    """Every row the engine and the jobs wrote, as text."""
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
    mail_set_up(settings)
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
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
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


def test_end_to_end_through_the_gmail_client_the_envelope_is_the_recipient(
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
    mail_set_up(settings, sandbox=True, allowlist=CEO)
    built = gmail.GmailApiSender(
        info,
        SENDER,
        reply_to="help@example.com",
        transport=httpx.MockTransport(answer),
        sleep=lambda _: None,
    )
    monkeypatch.setattr(mail, "_base_sender", lambda: built)
    caplog.set_level(logging.DEBUG)
    owner = operator(CEO)
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    run_queued_jobs()
    [message] = messages_of(charge)
    assert (message.status, message.mail_message_id) == (MessageStatus.SENT, "18f0e2e")
    send = requests[-1]
    raw = json.loads(send.content)["raw"]
    envelope = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    assert (envelope["From"], envelope["To"], envelope["Reply-To"]) == (
        SENDER,
        CEO,
        "help@example.com",
    )
    assert str(envelope["Subject"]) == message.rendered_subject
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
