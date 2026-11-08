"""D's ended-lease sentence and no message with an empty placeholder (owner, 2026-11-02), Opus's
tests.

1. ``{pay_line}``: the current and the ended lease's exact sentences, through the same merge;
   the editor accepts it; ledger 0069 puts it in the seeded D, leaves an edited D alone (and
   audits only that), and reverses (run this file on PostgreSQL too).
2. The holds: a blank support e-mail or company name holds every message that is not internal
   (``settings_incomplete``) and never the internal E; both set, the message is queued; a
   placeholder with no value holds it ``missing_value``, naming it; the hold is checked again
   at the send, where the settings are written into the placeholders.
3. "Send all held for settings": exactly those holds, admin only, POST only, the count
   confirmed, audited.
4. The preview's outcome, and the Admin pages' red banner.
"""

from __future__ import annotations

import importlib
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from accounts.roles import Role
from ledger import merge
from ledger.models import (
    AuditLog,
    CollectionStatus,
    CompanySettings,
    HeldReason,
    Lease,
    LeaseBilling,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OneOffCharge,
    OutboundMessage,
)
from ledger.services import message_templates, messaging, outbox_preview, reminders
from mail.memory import MemoryMailSender
from payments.models import Job, JobKind
from tests.receivables_helpers import a_lease, a_one_off_charge, a_store, an_operator
from tests.test_collections import at
from tests.test_collections import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_collections import memory as memory  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_operator_views import client_for
from tests.test_plan014_e4_rent_months import autopay
from tests.test_plan014_e_reminders import billed_lease, managers

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 20)
SUPPORT = "help@example.com"
CURRENT = (
    "Please pay today at {link} or contact us at help@example.com to arrange payment. "
    "Continued non-payment may affect your lease."
)
ENDED = "Please pay at {link} or contact us at help@example.com to settle this balance."
OUTBOX = "/admin-settings/outbox/"
RELEASE = "/admin-settings/outbox/send-held-for-settings/"


@pytest.fixture(autouse=True)
def _support(clock: Any) -> None:
    support(SUPPORT)
    clock(at(TODAY))


def support(address: str) -> None:
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(support_email=address)


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def overdue(lease: Lease, label: str = "Late fee") -> OneOffCharge:
    assert lease.operator is not None
    return a_one_off_charge(lease.operator, "80.00", day(-10), lease=lease, description=label)


def queue_rule(lease: Lease, charge: OneOffCharge, code: str, rule: str) -> OutboundMessage:
    template = MessageTemplate.objects.get(code=code)
    context = messaging.context_for(lease, charge, rule, today=TODAY)
    message = messaging.queue(rule, lease, charge, template, context)
    assert message is not None
    return message


def queue_d(lease: Lease, charge: OneOffCharge) -> OutboundMessage:
    return queue_rule(lease, charge, "overdue_day10", MessageRule.D)


def jobs_for(message: OutboundMessage) -> int:
    return Job.objects.filter(kind=JobKind.MESSAGE_SEND, entity_id=message.pk).count()


# --- 1. {pay_line} --------------------------------------------------------------------------


def test_pay_line_reads_the_current_and_the_ended_sentence_through_the_merge() -> None:
    lease = billed_lease(3101)
    charge = overdue(lease)
    values = messaging.context_for(lease, charge, MessageRule.D, today=TODAY)
    link = values["pay_link"]
    assert link.startswith("http://localhost:8000/")
    assert values["pay_line"] == CURRENT.format(link=link)
    lease.ended_on = TODAY
    assert messaging.pay_line(lease, TODAY, values) == ENDED.format(link=link)
    lease.ended_on = None
    lease.collection_status = CollectionStatus.ENDED
    assert messaging.pay_line(lease, TODAY, values) == ENDED.format(link=link)


def test_d_for_an_ended_lease_has_one_sentence_and_no_second_contact_us() -> None:
    lease = billed_lease(3102)
    charge = overdue(lease)
    Lease.objects.filter(pk=lease.pk).update(ended_on=day(-1))
    lease.refresh_from_db()
    charge.refresh_from_db()
    message = queue_d(lease, charge)
    body = message.rendered_body
    # The pay link is a signed statement token whose timestamp ticks by the second, so a link
    # computed after the render can differ from the rendered one (a main run of 2026-11-06 saw
    # it): the sentence is checked around the link rather than with a recomputed copy.
    ended = re.escape(ENDED).replace(re.escape("{link}"), r"http://localhost:8000/\S+")
    assert re.search(ended, body), body
    assert "Continued non-payment" not in body
    assert body.count("contact us") == 1
    assert message.status == MessageStatus.QUEUED


def test_the_editor_accepts_pay_line_and_its_preview_renders_it() -> None:
    assert "pay_line" in merge.fields_for("overdue_day10")
    row = MessageTemplate.objects.get(code="overdue_day10")
    assert "{pay_line}" in message_templates.placeholders(row)
    assert message_templates.check(row, row.subject, "Hello\n{pay_line}") == ""
    lease = billed_lease(3103)
    found = message_templates.preview(row, lease.store_id, row.subject, "{pay_line}", TODAY)
    assert found.ok
    assert found.body.startswith("Please pay today at ") and SUPPORT in found.body


# --- Ledger migration 0069 (run this file on PostgreSQL too) ----------------------------------

MIGRATION = importlib.import_module("ledger.migrations.0069_pay_line_and_settings_holds")
BEFORE = ("ledger", "0068_combined_messages_and_lease_line")
AFTER = ("ledger", "0069_pay_line_and_settings_holds")


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


def _seeded_d() -> tuple[str, str]:
    seeded = importlib.import_module("ledger.migrations.0061_messaging_engine").TEMPLATES
    [(subject, body)] = [(s, b) for code, _, _, s, b in seeded if code == "overdue_day10"]
    return subject, body


def _seeded_d_at_0068() -> str:
    return _seeded_d()[1].replace("Continued non-payment may affect your lease.", "{lease_line}")


def _d_at(apps: Any, body: str) -> Any:
    """D with ``body`` (a transactional test before this one flushed the seeded rows)."""
    model = apps.get_model("ledger", "MessageTemplate")
    model.objects.filter(code="overdue_day10").delete()
    return model.objects.create(code="overdue_day10", name="D", subject=_seeded_d()[0], body=body)


def test_0069_adds_no_guarded_column_and_names_the_seeded_text() -> None:
    assert MIGRATION.GUARDED_COLUMNS == {}
    assert MIGRATION.SEEDED in _seeded_d_at_0068()


@pytest.mark.django_db(transaction=True)
def test_0069_puts_pay_line_in_the_seeded_d_writes_no_audit_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    seeded = _seeded_d_at_0068()
    try:
        old = _migrate(BEFORE)
        row = _d_at(old, seeded)
        new = _migrate(AFTER)
        body = new.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body
        assert body == seeded.replace(MIGRATION.SEEDED, "{pay_line}")
        assert "{pay_line}\n{company_name}" in body and "{lease_line}" not in body
        assert not (
            new.get_model("ledger", "AuditLog")
            .objects.filter(action__startswith="message_template.pay_line", entity_id=str(row.pk))
            .exists()
        )
        back = _migrate(BEFORE)
        assert back.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body == seeded
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0069_leaves_an_admin_edited_d_alone_and_says_so() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    edited = "Hello {operator_name}, please pay {amount} today.\n{company_name}"
    try:
        old = _migrate(BEFORE)
        row = _d_at(old, edited)
        new = _migrate(AFTER)
        assert new.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body == edited
        audit = new.get_model("ledger", "AuditLog").objects.get(
            action="message_template.pay_line_skipped", entity_id=str(row.pk)
        )
        assert audit.after == {"code": "overdue_day10", "reason": "edited"}
        assert audit.actor_id is None
        back = _migrate(BEFORE)
        assert back.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body == edited
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0069_reverse_turns_the_new_holds_into_a_known_reason() -> None:
    """Re-pinned (Fable's run-2 finding on PLAN-015d): the held rows are written through the
    models of 0069's state, as the other round trips write theirs, so that a later column of
    a model (``leases.free_rent_days``) cannot break the test. That the guard holds a message
    ``settings_incomplete`` is pinned in section 2."""
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        apps = _migrate(AFTER)
        template = _d_at(apps, _seeded_d_at_0068().replace(MIGRATION.SEEDED, "{pay_line}"))
        operator = apps.get_model("ledger", "Operator").objects.create(
            display_name="Guard Reverse LLC", email="guard-reverse@example.com"
        )
        held = [
            apps.get_model("ledger", "OutboundMessage").objects.create(
                operator=operator,
                template=template,
                rule=MessageRule.D,
                charge_entity_type="one_off_charge",
                charge_entity_id=uuid.uuid4(),
                to_address="guard-reverse@example.com",
                status=MessageStatus.HELD,
                held_reason=reason,
            )
            for reason in (HeldReason.SETTINGS_INCOMPLETE, HeldReason.MISSING_VALUE)
        ]
        back = _migrate(BEFORE)
        rows = back.get_model("ledger", "OutboundMessage").objects.filter(
            pk__in=[row.pk for row in held]
        )
        assert sorted(rows.values_list("status", "held_reason")) == [
            ("held", "not_set_up"),
            ("held", "not_set_up"),
        ]
    finally:
        _migrate(*leaves)
        OutboundMessage.objects.all().delete()


# --- 2. The holds -----------------------------------------------------------------------------


def test_settings_incomplete_names_the_blank_settings() -> None:
    row = CompanySettings.read()
    assert messaging.settings_incomplete(row) == []
    row.support_email = ""
    assert messaging.settings_incomplete(row) == ["support_email"]
    row.name = "  "
    assert messaging.settings_incomplete(row) == ["name", "support_email"]


def test_a_blank_support_email_holds_an_operator_message_but_not_the_internal_e() -> None:
    support("")
    managers("boss@example.com")
    lease = billed_lease(3201)
    charge = overdue(lease)
    d = queue_d(lease, charge)
    assert (d.status, d.held_reason) == (MessageStatus.HELD, HeldReason.SETTINGS_INCOMPLETE)
    assert jobs_for(d) == 0
    assert "{support_email}" in d.rendered_body  # the gap shows, it is never a blank
    assert messaging.status_text(d) == (
        "Held (Settings incomplete: set the company name and support e-mail in Admin › "
        "System settings)"
    )
    audit = AuditLog.objects.get(action="message.held", entity_id=str(d.pk))
    assert audit.after["held_reason"] == HeldReason.SETTINGS_INCOMPLETE
    e = queue_rule(lease, charge, "internal_day30", MessageRule.E)
    assert (e.status, e.held_reason) == (MessageStatus.QUEUED, "")
    assert jobs_for(e) == 1


def test_a_blank_company_name_holds_an_operator_message_likewise(monkeypatch: Any) -> None:
    # The database refuses a blank name (a CHECK); the settings as read are blank here.
    row = CompanySettings.read()
    row.name = ""
    monkeypatch.setattr(CompanySettings, "read", classmethod(lambda cls: row))
    managers("boss@example.com")
    lease = billed_lease(3202)
    charge = overdue(lease)
    d = queue_d(lease, charge)
    assert (d.status, d.held_reason) == (MessageStatus.HELD, HeldReason.SETTINGS_INCOMPLETE)
    e = queue_rule(lease, charge, "internal_day30", MessageRule.E)
    assert e.status == MessageStatus.QUEUED


def test_both_set_the_message_is_queued_with_its_job() -> None:
    lease = billed_lease(3203)
    d = queue_d(lease, overdue(lease))
    assert (d.status, d.held_reason) == (MessageStatus.QUEUED, "")
    assert jobs_for(d) == 1
    assert "{" not in d.rendered_body and SUPPORT in d.rendered_body


def test_a_portal_message_is_held_while_the_settings_are_incomplete() -> None:
    support("")
    operator = an_operator("Portal Operator")
    template = MessageTemplate.objects.get(code=merge.PORTAL_INVITE_CODE)
    context = {
        "operator_name": "Portal Operator",
        "company_name": "Monvera",
        "support_email": "",
        "set_password_link": messaging.LINK_ADDED_AT_SEND,
    }
    message = messaging.queue_portal(template, uuid.uuid4(), operator, "op@example.com", context)
    assert (message.status, message.held_reason) == (
        MessageStatus.HELD,
        HeldReason.SETTINGS_INCOMPLETE,
    )


def test_a_placeholder_with_no_value_holds_the_message_and_names_it() -> None:
    lease = a_lease(a_store("No number"), an_operator("Operator 3204"))
    LeaseBilling.objects.update_or_create(lease=lease, defaults={"email": "store@example.com"})
    charge = overdue(lease)
    c = queue_rule(lease, charge, "overdue_day5", MessageRule.C)
    assert (c.status, c.held_reason) == (MessageStatus.HELD, HeldReason.MISSING_VALUE)
    assert "#{store_number} No number" in c.rendered_subject
    assert messaging.missing(c) == ["store_number"]
    assert messaging.status_text(c) == (
        "Held (Missing value: {store_number} — give store No number a number in its settings)"
    )
    assert jobs_for(c) == 0


def test_the_hold_is_checked_again_at_the_send(memory: MemoryMailSender) -> None:
    lease = billed_lease(3205)
    d = queue_d(lease, overdue(lease))
    assert d.status == MessageStatus.QUEUED
    support("")
    outcome = messaging.send(d.pk)
    d.refresh_from_db()
    assert (d.status, d.held_reason) == (MessageStatus.HELD, HeldReason.SETTINGS_INCOMPLETE)
    assert outcome.message == "Held: settings incomplete."
    assert memory.sent == []


def test_a_message_held_while_blank_goes_with_the_settings_written_in(
    memory: MemoryMailSender,
) -> None:
    support("")
    lease = billed_lease(3206)
    d = queue_d(lease, overdue(lease))
    assert "{support_email}" in d.rendered_body
    admin = client_for(Role.ADMIN)[1]
    refused = messaging.send_now(d.pk, admin)
    assert not refused.ok and refused.message == messaging.SEND_NOW_SETTINGS
    support(SUPPORT)
    assert messaging.send_now(d.pk, admin).ok
    messaging.send(d.pk)
    d.refresh_from_db()
    assert d.status == MessageStatus.SENT
    [sent] = memory.sent
    assert "{support_email}" not in sent.text and f"contact us at {SUPPORT}" in sent.text
    assert d.rendered_body == sent.text


def test_a_combined_daily_message_is_held_once_for_the_settings() -> None:
    support("")
    lease = billed_lease(3207)
    overdue(lease, "Insurance gap")
    assert lease.operator is not None
    a_one_off_charge(lease.operator, "40.00", day(-5), lease=lease, description="Shelf")  # C
    reminders.queue_due(TODAY)
    found = {m.rule: m for m in OutboundMessage.objects.filter(lease=lease)}
    d, c = found["D"], found["C"]
    assert (d.status, d.held_reason) == (MessageStatus.HELD, HeldReason.SETTINGS_INCOMPLETE)
    assert (c.status, c.combined_into_id, c.held_reason) == (MessageStatus.COMBINED, d.pk, "")
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 0
    assert messaging.status_text(c).startswith("Combined into the D message: Held (Settings")


# --- 3. Send all held for settings ------------------------------------------------------------


def _two_held_and_one_other() -> tuple[list[OutboundMessage], OutboundMessage]:
    support("")
    first, second = billed_lease(3301), billed_lease(3302)
    held = [queue_d(first, overdue(first)), queue_d(second, overdue(second))]
    support(SUPPORT)
    other = billed_lease(3303, email="")
    sandbox = queue_d(other, overdue(other))
    OutboundMessage.objects.filter(pk=sandbox.pk).update(
        status=MessageStatus.HELD, held_reason=HeldReason.NO_ADDRESS
    )
    return held, sandbox


def test_send_all_held_for_settings_releases_exactly_those_holds_audited(
    memory: MemoryMailSender,
) -> None:
    held, other = _two_held_and_one_other()
    client, admin = client_for(Role.ADMIN)
    page = client.get(OUTBOX).content.decode()
    assert 'id="outbox-send-held-for-settings"' in page
    assert "Send all held for settings (2)" in page
    assert "Send all held for settings: 2 held messages → 2 e-mails, 0 superseded?" in page
    response = client.post(RELEASE, {"count": "2"}, follow=True)
    assert "Sent now: 2 held messages → 2 e-mails, 0 superseded." in response.content.decode()
    for message in held:
        message.refresh_from_db()
        assert (message.status, message.held_reason) == (MessageStatus.QUEUED, "")
        assert jobs_for(message) == 1
        audit = AuditLog.objects.get(
            action="message.send_held_for_settings", entity_id=str(message.pk)
        )
        assert audit.actor_id == admin.pk
        assert audit.before == {"status": "held", "held_reason": "settings_incomplete"}
    other.refresh_from_db()
    assert (other.status, other.held_reason) == (MessageStatus.HELD, HeldReason.NO_ADDRESS)
    for message in held:
        messaging.send(message.pk)
    assert len(memory.sent) == 2
    assert all(f"contact us at {SUPPORT}" in sent.text for sent in memory.sent)


def test_send_all_is_refused_for_a_changed_count_while_incomplete_and_when_none(
    memory: MemoryMailSender,
) -> None:
    held, _ = _two_held_and_one_other()
    admin = client_for(Role.ADMIN)[1]
    assert messaging.send_held_for_settings(admin, 3).message == messaging.RELEASE_COUNT_CHANGED
    support("")
    outcome = messaging.send_held_for_settings(admin, 2)
    assert outcome.message == messaging.RELEASE_STILL_INCOMPLETE
    assert all(OutboundMessage.objects.get(pk=m.pk).status == MessageStatus.HELD for m in held)
    assert not AuditLog.objects.filter(action="message.send_held_for_settings").exists()
    support(SUPPORT)
    OutboundMessage.objects.filter(pk__in=[m.pk for m in held]).update(
        held_reason=HeldReason.SANDBOX
    )
    assert messaging.send_held_for_settings(admin, 0).message == messaging.RELEASE_NONE


def test_send_all_is_refused_while_mail_is_not_set_up() -> None:
    _two_held_and_one_other()
    admin = client_for(Role.ADMIN)[1]
    assert messaging.send_held_for_settings(admin, 2).message == messaging.SEND_NOW_NOT_SET_UP


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_send_all_is_admin_only(role: Role, memory: MemoryMailSender) -> None:
    held, _ = _two_held_and_one_other()
    client, _ = client_for(role)
    response = client.post(RELEASE, {"count": "2"})
    assert response.status_code in (302, 403)
    assert all(OutboundMessage.objects.get(pk=m.pk).status == MessageStatus.HELD for m in held)
    assert 'id="outbox-send-held-for-settings"' not in client.get(OUTBOX).content.decode()


def test_send_all_is_post_only(memory: MemoryMailSender) -> None:
    held, _ = _two_held_and_one_other()
    client, _ = client_for(Role.ADMIN)
    assert client.get(RELEASE).status_code == 405
    assert all(OutboundMessage.objects.get(pk=m.pk).status == MessageStatus.HELD for m in held)


def test_the_outbox_filters_by_held_reason() -> None:
    held, other = _two_held_and_one_other()
    client, _ = client_for(Role.ADMIN)
    page = client.get(OUTBOX, {"reason": "settings_incomplete"}).content.decode()
    assert all(f'id="message-{m.pk}"' in page for m in held)
    assert f'id="message-{other.pk}"' not in page
    assert '<option value="missing_value">Missing value</option>' in page


# --- 4. The preview and the banner ------------------------------------------------------------


def test_the_preview_marks_operator_rows_held_for_the_settings_not_e() -> None:
    support("")
    managers("boss@example.com")
    lease = billed_lease(3401)
    assert lease.operator is not None
    a_one_off_charge(lease.operator, "40.00", day(-5), lease=lease, description="Shelf")  # C
    a_one_off_charge(lease.operator, "70.00", day(-30), lease=lease, description="Old")  # E
    rows = {row.rule: row for row in outbox_preview.preview(TODAY, days=1).rows}
    assert rows["C"].outcome == "Would queue (held: settings incomplete)"
    assert rows["E"].outcome == "Would queue"
    support(SUPPORT)
    rows = {row.rule: row for row in outbox_preview.preview(TODAY, days=1).rows}
    assert rows["C"].outcome == "Would queue"


ADMIN_PAGES = (
    "ledger:settings:index",
    "ledger:settings:templates",
    "ledger:outbox:page",
    "ledger:outbox:preview",
    "banking:page",
    "ledger:run:today",
    "ledger:audit:page",
    "ledger:webhooks:list",
    "ledger:reconciliation:page",
)
BANNER = "Set the company name and support e-mail before mail goes live."


@pytest.mark.parametrize("name", ADMIN_PAGES)
def test_the_banner_is_on_each_admin_page_while_incomplete(name: str) -> None:
    client, _ = client_for(Role.ADMIN)
    support("")
    page = client.get(reverse(name)).content.decode()
    assert BANNER in page and 'id="settings-incomplete"' in page
    assert reverse("admin:ledger_companysettings_change", args=[1]) in page
    support(SUPPORT)
    assert BANNER not in client.get(reverse(name)).content.decode()


@pytest.mark.parametrize(
    ("role", "name"),
    [
        (Role.BILLING, "ledger:run:today"),
        (Role.BILLING, "ledger:reconciliation:page"),
        (Role.BILLING, "banking:page"),
        (Role.VIEWER, "ledger:webhooks:list"),
    ],
)
def test_the_banner_shows_for_every_role_that_opens_the_page_without_the_admin_link(
    role: Role, name: str
) -> None:
    client, _ = client_for(role)
    support("")
    page = client.get(reverse(name)).content.decode()
    assert BANNER in page
    assert 'id="settings-incomplete-link"' not in page


def test_the_banner_is_not_on_other_pages_nor_the_sign_in_page() -> None:
    support("")
    client, _ = client_for(Role.ADMIN)
    assert BANNER not in client.get(reverse("ledger:home")).content.decode()
    assert BANNER not in client.get(reverse("ledger:stores:list")).content.decode()
    from django.test import Client

    assert BANNER not in Client().get(reverse("two_factor:login")).content.decode()


# --- The release is grouped as the daily job groups (Fable's review, 2026-11-02) ----------------

NOV = date(2026, 11, 1)


def held_on(
    clock: Any, on: date, lease: Lease, subject: Any, code: str, rule: str
) -> OutboundMessage:
    """A message queued (and held: the settings are blank) on ``on``."""
    clock(at(on))
    template = MessageTemplate.objects.get(code=code)
    context = messaging.context_for(lease, subject, rule, today=on)
    message = messaging.queue(rule, lease, subject, template, context)
    assert message is not None and message.held_reason == HeldReason.SETTINGS_INCOMPLETE
    return message


def test_two_weeks_of_holds_go_as_one_combined_d_per_lease(
    clock: Any, memory: MemoryMailSender
) -> None:
    support("")
    lease = billed_lease(3501)
    assert lease.operator is not None
    late = a_one_off_charge(
        lease.operator, "80.00", date(2026, 11, 7), lease=lease, description="Late"
    )
    shelf = a_one_off_charge(
        lease.operator, "40.00", date(2026, 11, 14), lease=lease, description="Shelf"
    )
    b = held_on(clock, date(2026, 11, 4), lease, late, "reminder_due", MessageRule.B)
    c = held_on(clock, date(2026, 11, 12), lease, late, "overdue_day5", MessageRule.C)
    d = held_on(clock, date(2026, 11, 17), lease, late, "overdue_day10", MessageRule.D)
    c2 = held_on(clock, date(2026, 11, 19), lease, shelf, "overdue_day5", MessageRule.C)
    other = billed_lease(3502)
    autopay(other)
    month = messaging.RentMonth(other, NOV, date(2026, 11, 5), Decimal("3500.00"))
    a = held_on(clock, date(2026, 11, 2), other, month, "reminder_autopay", MessageRule.A)
    support(SUPPORT)
    clock(at(date(2026, 11, 20)))
    client, _ = client_for(Role.ADMIN)
    page = client.get(OUTBOX).content.decode()
    assert "5 held messages → 1 e-mail, 3 superseded" in page
    response = client.post(RELEASE, {"count": "5"}, follow=True)
    assert "Sent now: 5 held messages → 1 e-mail, 3 superseded." in response.content.decode()
    rows = {m.pk: m for m in OutboundMessage.objects.filter(pk__in=[a.pk, b.pk, c.pk, d.pk, c2.pk])}
    assert (rows[d.pk].status, rows[d.pk].held_reason) == (MessageStatus.QUEUED, "")
    assert (rows[c2.pk].status, rows[c2.pk].combined_into_id) == (MessageStatus.COMBINED, d.pk)
    assert (rows[b.pk].status, rows[b.pk].error) == (MessageStatus.SUPPRESSED, "Superseded")
    assert (rows[c.pk].status, rows[c.pk].error) == (MessageStatus.SUPPRESSED, "Superseded")
    assert (rows[a.pk].status, rows[a.pk].error) == (MessageStatus.SUPPRESSED, "DebitPassed")
    assert jobs_for(rows[d.pk]) == 1
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 1
    assert rows[d.pk].rendered_body.endswith(
        "\n\nCharges (2): charge · amount · due date · days late\n"
        "- Late · $80.00 · Nov 7, 2026 · 13\n"
        "- Shelf · $40.00 · Nov 14, 2026 · 6\n"
        "Total: $120.00"
    )
    messaging.send(d.pk)
    [sent] = memory.sent
    assert sent.to == "store@example.com" and f"contact us at {SUPPORT}" in sent.text
    assert "{support_email}" not in sent.text


def test_a_single_held_row_is_released_unchanged(memory: MemoryMailSender) -> None:
    support("")
    lease = billed_lease(3503)
    d = queue_d(lease, overdue(lease))
    body = d.rendered_body
    support(SUPPORT)
    admin = client_for(Role.ADMIN)[1]
    outcome = messaging.send_held_for_settings(admin, 1)
    assert outcome.message == "Sent now: 1 held message → 1 e-mail, 0 superseded."
    d.refresh_from_db()
    assert (d.status, d.rendered_body) == (MessageStatus.QUEUED, body)
    assert jobs_for(d) == 1


def test_send_now_on_one_held_row_combines_the_lease_s_other_holds(
    clock: Any, memory: MemoryMailSender
) -> None:
    support("")
    lease = billed_lease(3504)
    assert lease.operator is not None
    late = a_one_off_charge(
        lease.operator, "80.00", date(2026, 11, 7), lease=lease, description="Late"
    )
    shelf = a_one_off_charge(
        lease.operator, "40.00", date(2026, 11, 14), lease=lease, description="Shelf"
    )
    d = held_on(clock, date(2026, 11, 17), lease, late, "overdue_day10", MessageRule.D)
    c = held_on(clock, date(2026, 11, 19), lease, shelf, "overdue_day5", MessageRule.C)
    elsewhere = billed_lease(3505)
    apart = queue_d(elsewhere, overdue(elsewhere))
    support(SUPPORT)
    clock(at(date(2026, 11, 20)))
    admin = client_for(Role.ADMIN)[1]
    assert messaging.send_now(c.pk, admin).message == messaging.SEND_NOW_QUEUED
    d.refresh_from_db()
    c.refresh_from_db()
    apart.refresh_from_db()
    assert d.status == MessageStatus.QUEUED and "Charges (2)" in d.rendered_body
    assert (c.status, c.combined_into_id) == (MessageStatus.COMBINED, d.pk)
    assert (apart.status, apart.held_reason) == (
        MessageStatus.HELD,
        HeldReason.SETTINGS_INCOMPLETE,
    )
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 1
