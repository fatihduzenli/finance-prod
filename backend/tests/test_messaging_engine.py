"""The messaging engine (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md D1 to
D8), Opus's tests of each item: the seven templates byte for byte against plan §9, the
placeholder values, the send window and its job, the channels and held reasons, the Mail
card, the message templates' edit and preview, the Outbox and "Send now", the store's
timeline, the internal message and the managers' list, SMS refused, and ledger migration
0061 in both directions over rows of the old ladder. The re-pinned ladder tests are in
``test_collections.py``.
"""

from __future__ import annotations

import importlib
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import mail
from accounts.roles import Role
from ledger import merge
from ledger.models import (
    AuditLog,
    CompanySettings,
    MessageChannel,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OutboundMessage,
    RentCharge,
)
from ledger.services import leases, mail_settings, message_templates, messaging, timeline
from mail.memory import MemoryMailSender
from payments.models import Job, JobKind
from tests.job_helpers import run_queued_jobs
from tests.test_collections import (
    CEO,
    DUE,
    at,
    billed,
    mail_set_up,
    operator,
    queue_rule,
    rent_charge,
)
from tests.test_collections import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_collections import memory as memory  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_ledger_models import new_lease, new_one_off_charge, saved
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

NEW_YORK = ZoneInfo("America/New_York")
PLAN = (
    Path(__file__).resolve().parents[2]
    / "docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md"
)


# --- D6: the seven templates, byte for byte -------------------------------------------------

#: The plan's letter of each seeded code (§9).
CODES = {
    "A": "reminder_autopay",
    "B": "reminder_due",
    "C": "overdue_day5",
    "D": "overdue_day10",
    "E (internal)": "internal_day30",
    "F": "returned_debit",
}


def plan_templates() -> dict[str, tuple[str, str]]:
    """Code -> (subject, body) as plan §9 writes them, and the no-retry variant as its
    sentence swap says."""
    text = PLAN.read_text(encoding="utf-8")
    section = text[text.index("## 9. The six templates") : text.index("## 10.")]
    start = section.index("```\n") + 4
    block = section[start : section.index("\n```", start)]
    found: dict[str, tuple[str, str]] = {}
    for part in block.split("\n\n"):
        head, *body = part.split("\n")
        match = re.fullmatch(r"(.+?) — Subject: (.*)", head)
        assert match is not None, head
        found[CODES[match.group(1)]] = (match.group(2), "\n".join(body))
    swap = section[section.index("`returned_debit_no_retry`") :]
    old, new = re.findall(r'"([^"]+)"', " ".join(swap.split()))
    f_subject, f_body = found["returned_debit"]
    assert old in f_body
    found["returned_debit_no_retry"] = (f_subject, f_body.replace(old, new))
    return found


def test_the_seven_seeded_templates_equal_plan_section_9_byte_for_byte() -> None:
    expected = plan_templates()
    assert len(expected) == 7
    for code, (subject, body) in expected.items():
        row = MessageTemplate.objects.get(code=code)
        assert row.subject.encode("utf-8") == subject.encode("utf-8"), code
        assert row.body.encode("utf-8") == body.encode("utf-8"), code
        assert (row.active, row.channel) == (True, MessageChannel.EMAIL)
        assert row.internal is (code == "internal_day30")
    migration = importlib.import_module("ledger.migrations.0061_messaging_engine")
    assert {code: (subject, body) for code, _, _, subject, body in migration.TEMPLATES} == expected


# --- D4: the placeholder values -------------------------------------------------------------


def test_the_fifteen_placeholders_of_a_rent_charge(clock: Any) -> None:
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(
        name="Monvera", support_email="help@example.com"
    )
    owner = operator(CEO, display_name="Ana Lima")
    charge = rent_charge(owner=owner)
    lease = charge.lease
    billed(lease, CEO)
    lease.billing.bank_last4 = "6789"
    lease.billing.save()
    clock(at(date(2026, 10, 15)))
    values = messaging.context_for(lease, charge, MessageRule.D, today=date(2026, 10, 15))
    assert set(values) == set(merge.FIELDS)
    pay_link = values.pop("pay_link")
    assert pay_link.startswith("http://localhost:8000/statement/")
    assert values == {
        "operator_name": "Ana Lima",
        "store_number": "900",
        "store_name": "Store 900 Testville",
        "amount": "$2,450.00",
        "charge_name": "Rent October 2026",
        "due_date": "Oct 3, 2026",
        "debit_date": "Oct 3, 2026",
        "retry_date": "",
        "days_late": "12",
        "bank_last4": "6789",
        "return_reason": "",
        "store_link": f"http://localhost:8000/stores/{lease.store_id}/",
        "support_email": "help@example.com",
        "company_name": "Monvera",
    }


def test_a_lease_without_a_last_four_reads_a_dash_and_f_takes_its_dates() -> None:
    charge = rent_charge()
    values = messaging.context_for(
        charge.lease,
        charge,
        MessageRule.F,
        today=date(2026, 10, 5),
        retry_date=date(2026, 10, 8),
        return_reason="Insufficient funds",
    )
    assert (values["bank_last4"], values["retry_date"], values["return_reason"]) == (
        "—",
        "Oct 8, 2026",
        "Insufficient funds",
    )
    assert values["days_late"] == "2"
    early = messaging.context_for(charge.lease, charge, MessageRule.B, today=date(2026, 9, 30))
    assert early["days_late"] == "0"  # never below zero


# --- D7: the send window ---------------------------------------------------------------------


def ny(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


@pytest.mark.parametrize(
    ("queued", "expected"),
    [
        (ny(date(2026, 10, 8), 9, 0), ny(date(2026, 10, 8), 9, 0)),
        (ny(date(2026, 10, 8), 17, 59), ny(date(2026, 10, 8), 17, 59)),
        (ny(date(2026, 10, 8), 18, 0), ny(date(2026, 10, 9), 9, 0)),
        (ny(date(2026, 10, 8), 18, 1), ny(date(2026, 10, 9), 9, 0)),
        (ny(date(2026, 10, 8), 8, 59), ny(date(2026, 10, 8), 9, 0)),
        (ny(date(2026, 10, 8), 0, 0), ny(date(2026, 10, 8), 9, 0)),
        # Summer time ends on Nov 1, 2026: 9:00 New York is 14:00 UTC that day, 13:00 before.
        (ny(date(2026, 10, 31), 20, 0), datetime(2026, 11, 1, 14, 0, tzinfo=UTC)),
        # It starts on Mar 8, 2026: 9:00 New York is 13:00 UTC that day, 14:00 before.
        (ny(date(2026, 3, 7), 23, 0), datetime(2026, 3, 8, 13, 0, tzinfo=UTC)),
    ],
)
def test_the_window_is_9_to_18_new_york(queued: datetime, expected: datetime) -> None:
    assert messaging.scheduled_for(queued) == expected


def test_a_message_queued_after_18_waits_for_the_worker_until_9(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge()
    evening = ny(date(2026, 10, 8), 18, 1)
    clock(evening)
    message = queue_rule(charge)
    assert message is not None
    morning = ny(date(2026, 10, 9), 9, 0)
    assert message.scheduled_for == morning
    assert Job.objects.get(kind=JobKind.MESSAGE_SEND).run_after == morning
    assert run_queued_jobs() == []
    clock(ny(date(2026, 10, 9), 8, 59))
    assert run_queued_jobs() == []
    clock(morning)
    [done] = run_queued_jobs()
    assert done.result == "Sent." and len(memory.sent) == 1


# --- D2, D8: channels, held reasons, SMS -----------------------------------------------------


def test_sms_is_refused_at_queue_time_and_nothing_is_written(clock: Any) -> None:
    MessageTemplate.objects.filter(code="overdue_day5").update(channel=MessageChannel.SMS)
    clock(at(date(2026, 10, 8)))
    with pytest.raises(messaging.ChannelUnavailable) as raised:
        queue_rule(rent_charge())
    assert raised.value.message == "SMS is not set up."
    assert OutboundMessage.objects.count() == Job.objects.count() == 0
    with pytest.raises(messaging.ChannelUnavailable):
        messaging.channel_for("sms")


def test_the_internal_message_goes_to_the_managers_and_never_to_the_operator(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator(CEO, handled_personally=True, handled_personally_note="In person.")
    charge = rent_charge(owner=owner)
    clock(at(date(2026, 11, 2)))
    held = queue_rule(charge, MessageRule.E, "internal_day30")
    run_queued_jobs()
    assert held is not None
    held.refresh_from_db()
    assert (held.status, held.held_reason) == (MessageStatus.HELD, "no_manager_list")
    CompanySettings.objects.update(manager_emails="boss@example.com\nlead@example.com")
    sent = queue_rule(charge, MessageRule.E, "internal_day30", occurrence=2)
    assert sent is not None and sent.to_address == "boss@example.com"
    run_queued_jobs()
    sent.refresh_from_db()
    # Handled personally holds the operator's messages, never the internal one.
    assert sent.status == MessageStatus.SENT
    assert [message.to for message in memory.sent] == ["boss@example.com", "lead@example.com"]
    assert CEO not in [message.to for message in memory.sent]


def test_a_rendered_text_with_payment_data_is_stored_failed_without_its_text(
    clock: Any,
) -> None:
    charge = rent_charge()
    clock(at(date(2026, 10, 8)))
    # No stored value can hold it (the guard refuses on save), so the value is forced.
    context = messaging.context_for(charge.lease, charge, MessageRule.C, today=DUE)
    context["operator_name"] = "4111 1111 1111 1111"
    template = MessageTemplate.objects.get(code="overdue_day5")
    message = messaging.queue(MessageRule.C, charge.lease, charge, template, context)
    assert message is not None
    assert (message.status, message.error) == (MessageStatus.FAILED, "PaymentDataRefused")
    assert message.rendered_body == message.rendered_subject == ""
    assert not Job.objects.filter(kind=JobKind.MESSAGE_SEND).exists()


# --- D1: the Mail card -----------------------------------------------------------------------


def test_the_mail_card_saves_the_four_fields_and_audits_them(settings: Any) -> None:
    client, user = client_for(Role.ADMIN)
    page = client.get("/admin-settings/").content.decode()
    assert 'id="mail-card"' in page and mail_settings.NOT_SET_UP_NO_KEY in page
    response = client.post(
        "/admin-settings/mail/",
        {
            "sender_email": " Billing@Example.com ",
            "reply_to_email": "help@example.com",
            "manager_emails": "Boss@Example.com\n\nlead@example.com\nboss@example.com",
            "sandbox_allowlist": "owner@example.com",
        },
    )
    assert response.status_code == 302 and response["Location"].endswith("#mail")
    row = CompanySettings.read()
    assert (row.sender_email, row.reply_to_email) == ("billing@example.com", "help@example.com")
    assert row.manager_emails == "boss@example.com\nlead@example.com"
    assert row.sandbox_allowlist == "owner@example.com"
    audit = AuditLog.objects.get(action="company_settings.updated")
    assert audit.actor == user and audit.after is not None
    assert audit.after["sender_email"] == "billing@example.com"
    settings.MAIL_ENABLED = True
    settings.GMAIL_SERVICE_ACCOUNT_JSON = '{"private_key": "KEY-VALUE-MARKER"}'
    page = client.get("/admin-settings/").content.decode()
    assert mail_settings.SET_UP in page and "KEY-VALUE-MARKER" not in page


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("sender_email", "a@example.com, b@example.com", "Send as"),
        ("reply_to_email", "Someone <a@example.com>", "Reply-To"),
        ("manager_emails", "a@example.com b@example.com", "Managers"),
        ("sandbox_allowlist", "secret-value@example.com;x@example.com", "Sandbox allowlist"),
        ("manager_emails", "4111111111111111@example.com", "Managers"),
    ],
)
def test_the_mail_card_refuses_a_bad_line_by_field_without_the_value(
    field: str, value: str, label: str
) -> None:
    client, _ = client_for(Role.ADMIN)
    response = client.post("/admin-settings/mail/", {field: value}, follow=True)
    page = response.content.decode()
    assert label in page and value not in page
    assert not AuditLog.objects.filter(action="company_settings.updated").exists()
    assert getattr(CompanySettings.read(), field) == ""


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_below_admin_the_mail_card_and_the_templates_are_refused(role: Role) -> None:
    client, _ = client_for(role)
    template = MessageTemplate.objects.get(code="reminder_due")
    assert (
        client.post("/admin-settings/mail/", {"sender_email": "x@example.com"}).status_code == 403
    )
    assert client.get("/admin-settings/templates/").status_code == 403
    assert client.get(f"/admin-settings/templates/{template.pk}/").status_code == 403
    assert (
        client.post(
            f"/admin-settings/templates/{template.pk}/", {"subject": "S", "body": "B"}
        ).status_code
        == 403
    )
    assert client.get("/admin-settings/outbox/").status_code == 403
    assert CompanySettings.read().sender_email == ""


# --- D4: the message templates ----------------------------------------------------------------


def test_the_templates_list_and_an_edit_saved_and_audited() -> None:
    client, user = client_for(Role.ADMIN)
    page = client.get("/admin-settings/templates/").content.decode()
    for name in ("A — Rent will be debited", "E — 30 days overdue (internal)", "Final statement"):
        assert name in page
    template = MessageTemplate.objects.get(code="reminder_due")
    edit = client.get(f"/admin-settings/templates/{template.pk}/").content.decode()
    assert "Placeholders you can use: {operator_name}, {store_number}" in edit
    response = client.post(
        f"/admin-settings/templates/{template.pk}/",
        {"subject": "Due {due_date}", "body": "Hello {operator_name},\r\nPay {amount}."},
    )
    assert response.status_code == 302
    template.refresh_from_db()
    assert (template.subject, template.body) == (
        "Due {due_date}",
        "Hello {operator_name},\nPay {amount}.",
    )
    audit = AuditLog.objects.get(action="message_template.changed")
    assert audit.actor == user and audit.before is not None and audit.after is not None
    assert audit.before["subject"].startswith("Payment due {due_date}")
    assert audit.after["body"] == "Hello {operator_name},\nPay {amount}."


@pytest.mark.parametrize(
    ("subject", "body", "refusal"),
    [
        (
            "Hi",
            "Owed {unknown_field}.",
            "{unknown_field} is not a placeholder this template can use.",
        ),
        ("Hi {operator}", "Owed.", "{operator} is not a placeholder this template can use."),
        ("", "Owed.", message_templates.SUBJECT_REQUIRED),
        ("Hi", "x" * 20_000, message_templates.BODY_TOO_LONG),
        ("Hi", "card 4111 1111 1111 1111", message_templates.PAYMENT_DATA),
    ],
)
def test_a_refused_edit_names_the_problem_keeps_the_text_and_saves_nothing(
    subject: str, body: str, refusal: str
) -> None:
    client, _ = client_for(Role.ADMIN)
    template = MessageTemplate.objects.get(code="reminder_due")
    before = (template.subject, template.body)
    response = client.post(
        f"/admin-settings/templates/{template.pk}/", {"subject": subject, "body": body}
    )
    assert response.status_code == 400
    page = response.content.decode()
    assert refusal.replace("'", "&#x27;") in page
    template.refresh_from_db()
    assert (template.subject, template.body) == before
    assert not AuditLog.objects.filter(action="message_template.changed").exists()


def test_the_preview_renders_a_real_store_with_sample_amounts_and_queues_nothing() -> None:
    client, _ = client_for(Role.ADMIN)
    owner = operator(display_name="Preview Person")
    lease = billed(saved(new_lease(operator=owner)), owner.email)
    template = MessageTemplate.objects.get(code="returned_debit")
    response = client.post(
        f"/admin-settings/templates/{template.pk}/preview/",
        {"subject": template.subject, "body": template.body, "store": str(lease.store_id)},
    )
    assert response.status_code == 200
    page = response.content.decode()
    preview = page[page.index('id="template-preview"') :]
    assert "Hello Preview Person," in preview
    assert "The bank debit of $1,234.56 for Rent " in preview
    assert "Reason: Insufficient funds" in preview
    assert "#900 Store 900 Testville" in preview
    assert OutboundMessage.objects.count() == Job.objects.count() == 0
    template.refresh_from_db()
    no_store = client.post(
        f"/admin-settings/templates/{template.pk}/preview/",
        {"subject": template.subject, "body": template.body, "store": str(uuid.uuid4())},
    ).content.decode()
    assert message_templates.NO_STORE.replace("'", "&#x27;") in no_store


def test_the_retry_date_sample_skips_the_weekend() -> None:
    assert message_templates.business_days_after(date(2026, 10, 30), 3) == date(2026, 11, 4)


# --- D5: the Outbox and "Send now" ------------------------------------------------------------


def test_the_outbox_lists_every_message_with_its_reason_and_full_address(
    clock: Any, settings: Any
) -> None:
    settings.MAIL_ENABLED = False
    held = rent_charge(owner=operator(CEO))
    clock(at(date(2026, 10, 8)))
    queue_rule(held)
    run_queued_jobs()
    client, _ = client_for(Role.ADMIN)
    page = client.get("/admin-settings/outbox/").content.decode()
    table = page[page.index('id="outbox"') :]
    assert CEO in table and "Held (not sent (mail not set up))" in table
    assert "C — Payment overdue" in table and "Send now" in table
    assert 'id="outbox-not-set-up"' in page
    assert "We have not yet received $2,450.00" in table  # the stored body opens in the row
    assert "1 message" in page
    filtered = client.get("/admin-settings/outbox/?status=sent").content.decode()
    assert CEO not in filtered[filtered.index('id="outbox"') :]
    ranged = client.get("/admin-settings/outbox/?from=2026-10-09&to=2026-10-31").content.decode()
    assert "No messages yet." in ranged
    ranged = client.get("/admin-settings/outbox/?from=2026-10-08&to=2026-10-08").content.decode()
    assert CEO in ranged
    assert (
        client.get("/admin-settings/outbox/?from=not-a-day&status=%3Cscript%3E").status_code == 200
    )


def test_send_now_is_refused_while_not_set_up_or_off_the_allowlist_then_sends(
    clock: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.MAIL_ENABLED = False
    charge = rent_charge(owner=operator(CEO))
    clock(at(date(2026, 10, 8)))
    message = queue_rule(charge)
    assert message is not None
    run_queued_jobs()
    client, user = client_for(Role.ADMIN)
    url = f"/admin-settings/outbox/{message.pk}/send-now/"
    page = client.post(url, follow=True).content.decode()
    assert messaging.SEND_NOW_NOT_SET_UP in page
    memory = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: memory)
    mail_set_up(settings, sandbox=True, allowlist="owner@example.com")
    page = client.post(url, follow=True).content.decode()
    assert "This address is not on the sandbox allowlist" in page
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    CompanySettings.objects.update(sandbox_allowlist=CEO)
    client.post(url)
    run_queued_jobs()
    message.refresh_from_db()
    assert message.status == MessageStatus.SENT and [m.to for m in memory.sent] == [CEO]
    assert AuditLog.objects.filter(action="message.send_now", actor=user).exists()
    refused = client.post(url, follow=True).content.decode()
    assert messaging.SEND_NOW_NOT_HELD in refused


def test_send_now_below_admin_is_refused(clock: Any, settings: Any) -> None:
    settings.MAIL_ENABLED = False
    clock(at(date(2026, 10, 8)))
    message = queue_rule(rent_charge())
    run_queued_jobs()
    assert message is not None
    client, _ = client_for(Role.BILLING)
    assert client.post(f"/admin-settings/outbox/{message.pk}/send-now/").status_code == 403
    assert client.post("/admin-settings/outbox/not-a-uuid/send-now/").status_code == 403


def test_the_outbox_is_linked_for_admins_only() -> None:
    admin, _ = client_for(Role.ADMIN)
    assert 'id="nav-outbox"' in admin.get("/").content.decode()
    billing, _ = client_for(Role.BILLING)
    assert "/admin-settings/outbox/" not in billing.get("/").content.decode()


# --- D5: the store's timeline -----------------------------------------------------------------


def test_every_message_of_a_lease_is_on_its_store_timeline(clock: Any, settings: Any) -> None:
    settings.MAIL_ENABLED = False
    charge = rent_charge(owner=operator("timeline.person@example.com"))
    clock(at(date(2026, 10, 8)))
    queue_rule(charge)
    run_queued_jobs()
    store = charge.lease.store
    [entry] = timeline.entries(store, "emails")
    assert entry.kind == timeline.EMAIL
    assert len(timeline.entries(store, "all")) == 1 and timeline.entries(store, "notes") == []
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get(f"/stores/{store.pk}/?tab=contacts&show=emails").content.decode()
    item = page[page.index(f'id="message-{entry.payload.pk}"') :]
    item = item[: item.index("</li>")]
    assert "C — Payment overdue" in item and "not sent (mail not set up)" in item
    assert "t***@example.com" in item and "timeline.person@example.com" not in item
    assert "<details" in item and "We have not yet received" in item
    assert "No e-mails yet." not in page
    admin, _ = client_for(Role.ADMIN)
    page = admin.get(f"/stores/{store.pk}/?tab=contacts&show=emails").content.decode()
    item = page[page.index(f'id="message-{entry.payload.pk}"') :]
    assert "timeline.person@example.com" in item[: item.index("</li>")]


# --- D6: "Send a message now" offers only what an operator may get ---------------------------


def test_the_charge_page_offers_the_active_operator_templates_only(
    clock: Any, memory: MemoryMailSender
) -> None:
    clock(at(date(2026, 10, 8)))
    MessageTemplate.objects.filter(code="overdue_day10").update(active=False)
    charge = rent_charge()
    billing, _ = client_for(Role.BILLING)
    page = billing.get(f"/charges/{charge.pk}/").content.decode()
    form = page[
        page.index('id="send-message"') : page.index("</form>", page.index('id="send-message"'))
    ]
    assert "C — Payment overdue" in form and "B — Payment due" in form
    for absent in ("D — Notice", "E — 30 days", "Final statement", "Old ladder"):
        assert absent not in form
    lease = leases.billing_lease(charge)
    assert lease is not None
    one_off = saved(new_one_off_charge(lease=lease, operator=lease.operator, due_date=DUE))
    assert 'id="send-message"' in billing.get(f"/one-off-charges/{one_off.pk}/").content.decode()
    RentCharge.objects.filter(pk=charge.pk).update(status="paid", amount_paid=Decimal("2450.00"))
    assert 'id="send-message"' not in billing.get(f"/charges/{charge.pk}/").content.decode()


# --- D3, D6: ledger migration 0061 over the old ladder's rows ----------------------------------

BEFORE = ("ledger", "0060_transfer_incoming_billing")
ENGINE = ("ledger", "0061_messaging_engine")


def _migrate(target: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    return MigrationExecutor(connection).loader.project_state(target).apps


@pytest.mark.django_db(transaction=True)
def test_migration_0061_carries_the_ladder_rows_and_reverses_only_without_new_rules() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(BEFORE)
        step_model = old.get_model("ledger", "CollectionStep")
        # The ladder's seed (0025), written again when a transactional test flushed it.
        seed = importlib.import_module("ledger.migrations.0025_seed_collections_ladder")
        for code, subject, body in seed.TEMPLATES:
            template, _ = old.get_model("ledger", "MessageTemplate").objects.get_or_create(
                code=code, defaults={"subject": subject, "body": body}
            )
            for day, step_code, position in seed.STEPS:
                if step_code == code:
                    step_model.objects.get_or_create(
                        day_offset=day, defaults={"template": template, "position": position}
                    )
        assert [row.day_offset for row in step_model.objects.order_by("day_offset")] == [3, 10, 20]
        operator_model = old.get_model("ledger", "Operator")
        message_model = old.get_model("ledger", "OutboundMessage")
        owner = operator_model.objects.create(display_name="Migrating Operator")
        charge_id = uuid.uuid4()
        for day in (3, 10):
            step = step_model.objects.get(day_offset=day)
            message_model.objects.create(
                operator=owner,
                template=step.template,
                step=step,
                charge_entity_type="rent_charge",
                charge_entity_id=charge_id,
                to_address="old@example.com",
                subject=f"Day {day}",
                status="sent",
            )
        new = _migrate(ENGINE)
        rows = new.get_model("ledger", "OutboundMessage").objects.order_by("occurrence")
        assert [(row.rule, row.occurrence, row.rendered_subject) for row in rows] == [
            ("ladder", 3, "Day 3"),
            ("ladder", 10, "Day 10"),
        ]
        templates = new.get_model("ledger", "MessageTemplate").objects
        assert not templates.get(code="friendly_reminder").active
        assert templates.get(code="reminder_due").active
        assert "collection_steps" not in connection.introspection.table_names()
        # A message of a new rule cannot be lost by going back.
        new_rule = new.get_model("ledger", "OutboundMessage").objects.create(
            operator_id=owner.pk,
            template=templates.get(code="reminder_due"),
            rule="B",
            charge_entity_type="rent_charge",
            charge_entity_id=uuid.uuid4(),
            to_address="new@example.com",
        )
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE)
        new.get_model("ledger", "OutboundMessage").objects.filter(pk=new_rule.pk).delete()
        back = _migrate(BEFORE)
        restored = back.get_model("ledger", "OutboundMessage").objects.select_related("step")
        assert sorted(row.step.day_offset for row in restored) == [3, 10]
        steps = back.get_model("ledger", "CollectionStep").objects.select_related("template")
        assert [(row.day_offset, row.template.code) for row in steps.order_by("day_offset")] == [
            (3, "friendly_reminder"),
            (10, "formal_notice"),
            (20, "final_notice"),
        ]
        assert (
            not back.get_model("ledger", "MessageTemplate")
            .objects.filter(code="reminder_due")
            .exists()
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
        OutboundMessage.objects.all().delete()
    assert MessageTemplate.objects.filter(code="reminder_due", active=True).exists()
