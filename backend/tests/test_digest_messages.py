"""One e-mail per lease and day, the 3-day fold, and D's wording for an ended lease (owner,
2026-11-02), Opus's tests.

1. Grouping: two rules for one lease on one day are one sent message whose subject is the most
   urgent's (D > C > B) and whose body lists every charge with its amount, due date and days
   late; the other row is combined into it and never sent. E and F are never combined. A
   combined message whose own charge closed still goes while another of its charges is open.
2. The fold: C sent on day 0 and a D due on day 2 → the D waits, and goes on day 3 combined with
   what is due that day (or on its own); a charge paid meanwhile is dropped; B on day 1 never
   waits. The job twice on one day writes no new row and sends nothing twice.
3. The preview shows the groups (one row per send, its charges under it) and the fold.
4. D for an ended lease: ``{lease_line}`` reads the current or the ended sentence; ledger 0068
   puts it in D's body only when the seeded sentence is there, and goes both ways.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.roles import Role
from ledger import merge
from ledger.models import (
    AuditLog,
    CollectionStatus,
    Lease,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OneOffCharge,
    OneOffChargeStatus,
    OutboundMessage,
)
from ledger.services import messaging, outbox_preview, reminders
from mail.memory import MemoryMailSender
from payments.models import Job, JobKind
from tests.receivables_helpers import a_one_off_charge, a_rent_charge, an_insurance_charge
from tests.test_collections import at
from tests.test_collections import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_collections import memory as memory  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_operator_views import client_for
from tests.test_plan014_e_reminders import billed_lease, managers

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 20)  # a Tuesday
CURRENT = "Continued non-payment may affect your lease."
ENDED = "Please contact us to settle this balance."


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def one_off(lease: Lease, price: str, due: date, label: str) -> OneOffCharge:
    assert lease.operator is not None
    return a_one_off_charge(lease.operator, price, due, lease=lease, description=label)


def run(clock: Any, on: date) -> reminders.Summary:
    clock(at(on))
    return reminders.queue_due(on)


def rows_of(lease: Lease) -> dict[str, OutboundMessage]:
    return {message.rule: message for message in OutboundMessage.objects.filter(lease=lease)}


def send_jobs() -> list[uuid.UUID]:
    return list(
        Job.objects.filter(kind=JobKind.MESSAGE_SEND)
        .order_by("created_at", "id")
        .values_list("entity_id", flat=True)
    )


def send_all() -> None:
    for message_id in send_jobs():
        messaging.send(message_id)


# --- 1. Grouping ------------------------------------------------------------------------------


def test_two_rules_for_one_lease_on_one_day_are_one_message_listing_both(
    clock: Any, memory: MemoryMailSender
) -> None:
    lease = billed_lease(2101)
    one_off(lease, "80.00", day(-5), "Broken shelf")  # C today
    an_insurance_charge(lease, 2026, day(-10), "695.00")  # D today
    summary = run(clock, TODAY)
    assert dict(summary.queued_by_rule) == {"A": 0, "B": 0, "C": 1, "D": 1, "E": 0}
    found = rows_of(lease)
    d, c = found["D"], found["C"]
    assert (d.status, d.combined_into_id) == (MessageStatus.QUEUED, None)
    assert (c.status, c.combined_into_id) == (MessageStatus.COMBINED, d.pk)
    assert send_jobs() == [d.pk]
    assert d.rendered_subject == "Notice: $695.00 overdue — #2101 2101"
    assert d.rendered_body.endswith(
        "\n\nCharges (2): charge · amount · due date · days late\n"
        "- Insurance 2026–27 · $695.00 · Oct 10, 2026 · 10\n"
        "- Broken shelf · $80.00 · Oct 15, 2026 · 5\n"
        "Total: $775.00"
    )
    assert d.rendered_body.startswith("Hello Operator 2101,\nThe payment of $695.00 for Insurance")
    send_all()
    messaging.send(c.pk)  # its own send, were it asked: nothing goes
    assert len(memory.sent) == 1
    assert memory.sent[0].subject == d.rendered_subject
    c.refresh_from_db()
    assert c.status == MessageStatus.COMBINED and c.sent_at is None
    assert AuditLog.objects.filter(
        action="message.combined", entity_id=str(c.pk), after__into=str(d.pk)
    ).exists()


@pytest.mark.parametrize(
    ("offsets", "subject_rule"),
    [
        ((3, -5), "C"),  # B and C: C
        ((3, -10), "D"),  # B and D: D
        ((-5, -10), "D"),  # C and D: D
    ],
)
def test_the_subject_is_the_most_urgent_rule_s(
    clock: Any, offsets: tuple[int, int], subject_rule: str
) -> None:
    subjects = {
        "C": "Payment overdue — #2102 2102",
        "D": "Notice: $40.00 overdue — #2102 2102",
    }
    lease = billed_lease(2102)
    for number, offset in enumerate(offsets):
        one_off(lease, "40.00", day(offset), f"Charge {number}")
    run(clock, TODAY)
    sent = OutboundMessage.objects.get(lease=lease, combined_into__isnull=True)
    assert sent.rule == subject_rule
    assert sent.rendered_subject == subjects[subject_rule]
    other = OutboundMessage.objects.get(lease=lease, combined_into__isnull=False)
    assert other.combined_into_id == sent.pk and other.rule != subject_rule
    assert "Charges (2)" in sent.rendered_body and "Total: $80.00" in sent.rendered_body
    assert send_jobs() == [sent.pk]


def test_one_rule_alone_is_sent_as_its_template_with_no_list(clock: Any) -> None:
    lease = billed_lease(2103)
    one_off(lease, "80.00", day(-5), "Broken shelf")
    run(clock, TODAY)
    [message] = OutboundMessage.objects.filter(lease=lease)
    assert message.status == MessageStatus.QUEUED
    assert "Charges (" not in message.rendered_body
    assert send_jobs() == [message.pk]


def test_e_is_never_combined_with_the_operator_s_mail(clock: Any) -> None:
    managers("boss@example.com")
    lease = billed_lease(2104)
    one_off(lease, "80.00", day(-30), "Old fee")  # E today
    one_off(lease, "40.00", day(-5), "New fee")  # C today
    run(clock, TODAY)
    found = rows_of(lease)
    assert found["E"].status == MessageStatus.QUEUED and found["E"].combined_into_id is None
    assert found["C"].status == MessageStatus.QUEUED and found["C"].combined_into_id is None
    assert "Charges (" not in found["C"].rendered_body + found["E"].rendered_body
    assert sorted(send_jobs()) == sorted([found["E"].pk, found["C"].pk])


def test_f_stays_separate_from_the_day_s_reminders(clock: Any) -> None:
    lease = billed_lease(2105)
    returned = one_off(lease, "120.00", day(-1), "Returned rent")
    clock(at(TODAY))
    template = MessageTemplate.objects.get(code="returned_debit")
    context = messaging.context_for(
        lease, returned, MessageRule.F, today=TODAY, retry_date=day(3), return_reason="R01"
    )
    f = messaging.queue(MessageRule.F, lease, returned, template, context)
    assert f is not None
    one_off(lease, "40.00", day(-5), "New fee")  # C today
    run(clock, TODAY)
    found = rows_of(lease)
    f.refresh_from_db()
    assert f.status == MessageStatus.QUEUED and not f.combined.exists()
    assert found["C"].combined_into_id is None and "Charges (" not in found["C"].rendered_body
    assert sorted(send_jobs()) == sorted([f.pk, found["C"].pk])


def paid(*charges: OneOffCharge) -> None:
    for charge in charges:
        OneOffCharge.objects.filter(pk=charge.pk).update(
            status=OneOffChargeStatus.PAID, amount_paid=charge.price
        )


def test_a_charge_paid_before_the_send_is_left_out_of_the_list_and_the_total(
    clock: Any, memory: MemoryMailSender
) -> None:
    lease = billed_lease(2106)
    one_off(lease, "80.00", day(-10), "Rent catch-up")  # D today: the subject
    fee = one_off(lease, "40.00", day(-5), "Late fee")  # C today: combined into it
    third = one_off(lease, "25.00", day(-5), "Key copy")  # C today: combined too
    run(clock, TODAY)
    paid(fee)
    send_all()
    [mail_sent] = memory.sent
    found = {m.charge_entity_id: m for m in OutboundMessage.objects.filter(lease=lease)}
    sent = next(m for m in found.values() if m.status == MessageStatus.SENT)
    assert sent.rule == MessageRule.D and mail_sent.subject == sent.rendered_subject
    assert mail_sent.text == sent.rendered_body  # the stored body is what was sent
    assert "Late fee" not in sent.rendered_body
    assert sent.rendered_body.count("Charges (") == 1
    assert "Charges (2): charge · amount · due date · days late" in sent.rendered_body
    assert "- Key copy · $25.00 · Oct 15, 2026 · 5" in sent.rendered_body
    assert sent.rendered_body.endswith("Total: $105.00")
    dropped = found[fee.pk]
    assert (dropped.status, dropped.error, dropped.combined_into_id) == (
        MessageStatus.SUPPRESSED,
        "ChargeClosed",
        None,
    )
    assert found[third.pk].combined_into_id == sent.pk
    assert messaging.status_text(found[third.pk]) == "Combined into the D message: Sent"


def test_the_subject_charge_paid_before_the_send_gives_the_subject_to_the_open_rule(
    clock: Any, memory: MemoryMailSender
) -> None:
    lease = billed_lease(2108)
    rent = one_off(lease, "80.00", day(-10), "Rent catch-up")  # D: the subject at the queue
    one_off(lease, "40.00", day(-5), "Late fee")  # C
    run(clock, TODAY)
    paid(rent)
    send_all()
    [mail_sent] = memory.sent
    found = rows_of(lease)
    assert (found["D"].status, found["D"].error) == (MessageStatus.SUPPRESSED, "ChargeClosed")
    c = found["C"]
    assert (c.status, c.combined_into_id) == (MessageStatus.SENT, None)
    assert mail_sent.subject == c.rendered_subject == "Payment overdue — #2108 2108"
    assert mail_sent.text == c.rendered_body
    assert "Charges (" not in c.rendered_body and "Rent catch-up" not in c.rendered_body
    assert AuditLog.objects.filter(
        action="message.regrouped", entity_id=str(c.pk), after__for=str(found["D"].pk)
    ).exists()


def test_a_released_waiting_group_is_rebuilt_at_the_send_too(
    clock: Any, memory: MemoryMailSender
) -> None:
    _, _, y, z = fold_world()
    run(clock, day(0))
    run(clock, day(2))
    run(clock, day(3))  # Y's D (released) sent with Z's C
    paid(z)
    send_all()
    sent = about(y)
    assert sent.status == MessageStatus.SENT
    assert "Charge Z" not in sent.rendered_body and "Charges (" not in sent.rendered_body
    assert about(z).status == MessageStatus.SUPPRESSED
    assert memory.sent[-1].text == sent.rendered_body


def test_a_combined_message_whose_charges_all_closed_is_suppressed(
    clock: Any, memory: MemoryMailSender
) -> None:
    lease = billed_lease(2107)
    first = one_off(lease, "80.00", day(-10), "First")
    second = one_off(lease, "40.00", day(-5), "Second")
    run(clock, TODAY)
    paid(first, second)
    send_all()
    assert memory.sent == []
    found = rows_of(lease)
    assert (found["D"].status, found["D"].error) == (MessageStatus.SUPPRESSED, "ChargeClosed")
    assert found["C"].status == MessageStatus.COMBINED
    assert messaging.status_text(found["C"]) == (
        "Combined into the D message: Suppressed (ChargeClosed)"
    )


# --- 2. The 3-day fold ------------------------------------------------------------------------


def fold_world() -> tuple[Lease, OneOffCharge, OneOffCharge, OneOffCharge]:
    """C of ``x`` on day 0, D of ``y`` on day 2, C of ``z`` on day 3."""
    lease = billed_lease(2201)
    x = one_off(lease, "80.00", day(-5), "Charge X")
    y = one_off(lease, "60.00", day(2 - 10), "Charge Y")
    z = one_off(lease, "30.00", day(3 - 5), "Charge Z")
    return lease, x, y, z


def about(charge: OneOffCharge) -> OutboundMessage:
    return OutboundMessage.objects.get(charge_entity_id=charge.pk)


def test_a_d_within_three_days_of_a_c_waits_and_goes_combined_on_day_3(
    clock: Any, memory: MemoryMailSender
) -> None:
    lease, x, y, z = fold_world()
    run(clock, day(0))
    send_all()
    assert about(x).status == MessageStatus.SENT
    run(clock, day(1))
    run(clock, day(2))
    waiting = about(y)
    assert waiting.status == MessageStatus.WAITING
    assert waiting.scheduled_for == at(day(3), 9)
    assert len(send_jobs()) == 1  # the C of day 0 only
    assert messaging.status_text(waiting) == "Waiting for the 3-day fold (until Oct 23, 2026)"
    run(clock, day(3))
    sent, folded = about(y), about(z)
    assert sent.status == MessageStatus.QUEUED and sent.rule == MessageRule.D
    assert (folded.status, folded.combined_into_id) == (MessageStatus.COMBINED, sent.pk)
    assert send_jobs()[-1] == sent.pk and len(send_jobs()) == 2
    assert "- Charge Y · $60.00 · Oct 12, 2026 · 11" in sent.rendered_body
    assert "- Charge Z · $30.00 · Oct 18, 2026 · 5" in sent.rendered_body
    send_all()
    assert len(memory.sent) == 2
    assert lease.pk == sent.lease_id


def test_a_waiting_message_goes_on_its_own_when_nothing_else_is_due(clock: Any) -> None:
    lease, x, y, z = fold_world()
    z.delete()
    run(clock, day(0))
    run(clock, day(2))
    assert about(y).status == MessageStatus.WAITING
    summary = run(clock, day(3))
    assert summary.queued == 0
    released = about(y)
    assert released.status == MessageStatus.QUEUED
    assert "Charges (" not in released.rendered_body
    assert send_jobs()[-1] == released.pk
    assert lease.pk == released.lease_id and x.pk != y.pk


def test_the_next_fold_counts_from_the_day_a_waiting_message_went(clock: Any) -> None:
    lease, _, y, z = fold_world()
    z.delete()
    v = one_off(lease, "25.00", day(0), "Charge V")  # C on day 5
    run(clock, day(0))
    run(clock, day(2))  # Y's D waits
    run(clock, day(3))  # and goes
    assert about(y).status == MessageStatus.QUEUED
    run(clock, day(5))  # two days after Y's D went (not three after it was queued)
    later = about(v)
    assert later.status == MessageStatus.WAITING
    assert later.scheduled_for == at(day(6), 9)


def test_a_charge_paid_while_it_waits_is_dropped(clock: Any, memory: MemoryMailSender) -> None:
    _, _, y, z = fold_world()
    z.delete()
    run(clock, day(0))
    run(clock, day(2))
    OneOffCharge.objects.filter(pk=y.pk).update(
        status=OneOffChargeStatus.PAID, amount_paid=D("60.00")
    )
    run(clock, day(3))
    dropped = about(y)
    assert (dropped.status, dropped.error) == (MessageStatus.SUPPRESSED, "ChargeClosed")
    assert len(send_jobs()) == 1
    send_all()
    assert len(memory.sent) == 1  # the C of day 0


def test_b_on_day_1_does_not_wait(clock: Any) -> None:
    lease = billed_lease(2202)
    one_off(lease, "80.00", day(-5), "Charge X")  # C on day 0
    w = an_insurance_charge(lease, 2026, day(1 + 3), "695.00")  # B on day 1
    run(clock, day(0))
    run(clock, day(1))
    b = OutboundMessage.objects.get(charge_entity_id=w.pk)
    assert (b.rule, b.status) == (MessageRule.B, MessageStatus.QUEUED)
    assert len(send_jobs()) == 2


def test_b_and_a_waiting_d_on_the_same_day_go_apart(clock: Any) -> None:
    """B never waits; the D of the same day waits for the fold."""
    lease = billed_lease(2203)
    one_off(lease, "80.00", day(-5), "Charge X")  # C on day 0
    y = one_off(lease, "60.00", day(2 - 10), "Charge Y")  # D on day 2
    w = an_insurance_charge(lease, 2026, day(2 + 3), "695.00")  # B on day 2
    run(clock, day(0))
    run(clock, day(2))
    assert about(y).status == MessageStatus.WAITING
    b = OutboundMessage.objects.get(charge_entity_id=w.pk)
    assert b.status == MessageStatus.QUEUED and "Charges (" not in b.rendered_body


def test_the_job_twice_on_one_day_adds_no_row_and_no_send(clock: Any) -> None:
    fold_world()
    for offset in (0, 0, 2, 2, 3, 3):
        run(clock, day(offset))
    assert OutboundMessage.objects.count() == 3
    assert len(send_jobs()) == 2
    second = run(clock, day(3))
    assert second.queued == 0


def test_the_tick_twice_on_a_combining_day_sends_once(clock: Any) -> None:
    lease = billed_lease(2204)
    one_off(lease, "80.00", day(-5), "C fee")
    one_off(lease, "40.00", day(-10), "D fee")
    clock(at(TODAY))
    for _ in range(2):
        call_command("collections_tick", stdout=StringIO())
    assert OutboundMessage.objects.filter(lease=lease).count() == 2
    assert len(send_jobs()) == 1


# --- 3. The preview ---------------------------------------------------------------------------


def test_the_preview_shows_a_combined_send_with_its_charges(clock: Any) -> None:
    lease = billed_lease(2301)
    one_off(lease, "80.00", day(-5), "Broken shelf")
    an_insurance_charge(lease, 2026, day(-10), "695.00")
    clock(at(TODAY))
    found = outbox_preview.preview(TODAY)
    [group] = [group for group in found.groups if group.first.day == TODAY]
    assert group.combined and group.first.rule == "D"
    assert group.outcome == "Would send combined"
    assert group.charges == "2 charges: Insurance 2026–27, Broken shelf"
    assert group.amount == D("775.00")
    assert [(row.charge, row.amount, row.due, row.days_late) for row in group.rows] == [
        ("Insurance 2026–27", D("695.00"), day(-10), 10),
        ("Broken shelf", D("80.00"), day(-5), 5),
    ]
    admin, _ = client_for(Role.ADMIN)
    page = admin.get("/admin-settings/outbox/preview/").content.decode()
    assert f'id="preview-group-{group.number}" class="preview-group"' in page
    assert "2 charges: Insurance 2026–27, Broken shelf" in page
    assert page.count('class="preview-charge"') >= 2
    assert OutboundMessage.objects.count() == 0


def test_the_preview_shows_the_fold_and_the_day_it_goes(clock: Any) -> None:
    fold_world()
    clock(at(TODAY))
    found = outbox_preview.preview(TODAY, days=4)
    by_day = {group.first.day: group for group in found.groups}
    assert by_day[day(0)].outcome == "Would queue"
    assert by_day[day(2)].outcome == "Waits for the 3-day fold until Oct 23, 2026"
    released = by_day[day(3)]
    assert released.outcome == "Would send combined"
    assert [(row.rule, row.charge, row.queues) for row in released.rows] == [
        ("D", "Charge Y", False),  # queued on day 2, sent on day 3
        ("C", "Charge Z", True),
    ]
    out = StringIO()
    call_command("outbox_preview", "--days", "4", stdout=out)
    lines = [line.split("\t") for line in out.getvalue().splitlines()[1:]]
    day_3 = [line for line in lines if line[0] == "2026-10-23"]
    assert len(day_3) == 2 and day_3[0][8] == day_3[1][8] == str(released.number)
    assert {line[7] for line in day_3} == {"Would send combined"}


def test_the_preview_reads_a_message_already_waiting_in_the_outbox(clock: Any) -> None:
    _, _, y, z = fold_world()
    z.delete()
    run(clock, day(0))
    run(clock, day(2))
    assert about(y).status == MessageStatus.WAITING
    clock(at(day(2)))
    found = outbox_preview.preview(day(2), days=3)
    [group] = [group for group in found.groups if group.first.day == day(3)]
    assert group.outcome == "Would send after the 3-day fold"
    assert [(row.charge, row.queues, row.days_late) for row in group.rows] == [
        ("Charge Y", False, 11)
    ]


# --- 4. D for an ended lease ------------------------------------------------------------------


def test_lease_line_reads_the_current_and_the_ended_sentence() -> None:
    lease = billed_lease(2401)
    assert messaging.lease_line(lease, TODAY) == CURRENT
    lease.ended_on = day(5)  # ending later: still current
    assert messaging.lease_line(lease, TODAY) == CURRENT
    lease.ended_on = TODAY
    assert messaging.lease_line(lease, TODAY) == ENDED
    lease.ended_on = None
    lease.collection_status = CollectionStatus.ENDED
    assert messaging.lease_line(lease, TODAY) == ENDED


def test_d_for_a_current_lease_keeps_the_lease_sentence(clock: Any) -> None:
    lease = billed_lease(2402)
    one_off(lease, "80.00", day(-10), "Late fee")
    run(clock, TODAY)
    body = rows_of(lease)["D"].rendered_body
    assert CURRENT in body and ENDED not in body and "{lease_line}" not in body


def test_d_for_an_ended_lease_asks_to_settle_the_balance(clock: Any) -> None:
    lease = billed_lease(2403)
    a_rent_charge(lease, date(2026, 10, 1), day=10)  # due Oct 10: D today
    Lease.objects.filter(pk=lease.pk).update(
        collection_status=CollectionStatus.ENDED, ended_on=date(2026, 10, 15)
    )
    run(clock, TODAY)
    body = rows_of(lease)["D"].rendered_body
    assert ENDED in body and CURRENT not in body


def test_lease_line_is_a_placeholder_the_template_editor_accepts() -> None:
    assert "lease_line" in merge.fields_for("overdue_day10")
    row = MessageTemplate.objects.get(code="overdue_day10")
    assert "{lease_line}" in row.body
    row.full_clean()


# --- Ledger migration 0068 --------------------------------------------------------------------

MIGRATION_0068 = importlib.import_module("ledger.migrations.0068_combined_messages_and_lease_line")
BEFORE = ("ledger", "0067_lease_billing_bank_name")
AFTER = ("ledger", "0068_combined_messages_and_lease_line")
SEEDED_D = importlib.import_module("ledger.migrations.0061_messaging_engine").TEMPLATES


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


def _seeded_d() -> tuple[str, str]:
    [(subject, body)] = [(s, b) for code, _, _, s, b in SEEDED_D if code == "overdue_day10"]
    return subject, body


def _d_at(apps: Any, body: str) -> Any:
    subject, _ = _seeded_d()
    model = apps.get_model("ledger", "MessageTemplate")
    model.objects.filter(code="overdue_day10").delete()
    return model.objects.create(code="overdue_day10", name="D", subject=subject, body=body)


def test_0068_adds_no_guarded_column() -> None:
    assert MIGRATION_0068.GUARDED_COLUMNS == {}
    assert MIGRATION_0068.SENTENCE == CURRENT


@pytest.mark.django_db(transaction=True)
def test_0068_puts_lease_line_in_the_seeded_d_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    _, seeded = _seeded_d()
    try:
        old = _migrate(BEFORE)
        row = _d_at(old, seeded)
        new = _migrate(AFTER)
        body = new.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body
        assert body == seeded.replace(CURRENT, "{lease_line}")
        audit = new.get_model("ledger", "AuditLog").objects.get(
            action="message_template.lease_line_added", entity_id=str(row.pk)
        )
        assert audit.after == {"code": "overdue_day10", "placeholder": "{lease_line}"}
        back = _migrate(BEFORE)
        assert back.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body == seeded
        again = _migrate(AFTER)
        assert (
            "{lease_line}"
            in again.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body
        )
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0068_leaves_an_admin_edited_d_alone_and_says_so() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    edited = "Hello {operator_name}, please pay {amount} today.\n{company_name}"
    try:
        old = _migrate(BEFORE)
        row = _d_at(old, edited)
        new = _migrate(AFTER)
        assert new.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body == edited
        audit = new.get_model("ledger", "AuditLog").objects.get(
            action="message_template.lease_line_skipped", entity_id=str(row.pk)
        )
        assert audit.after == {"code": "overdue_day10", "reason": "edited"}
        assert (
            not new.get_model("ledger", "AuditLog")
            .objects.filter(action="message_template.lease_line_added", entity_id=str(row.pk))
            .exists()
        )
        back = _migrate(BEFORE)
        assert back.get_model("ledger", "MessageTemplate").objects.get(pk=row.pk).body == edited
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0068_reverse_turns_waiting_and_combined_rows_into_known_statuses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        new = _migrate(AFTER)
        message = new.get_model("ledger", "OutboundMessage")
        operator = new.get_model("ledger", "Operator").objects.create(display_name="Kaya")
        template = new.get_model("ledger", "MessageTemplate").objects.create(
            code="digest_test", subject="Owed", body="Owed."
        )
        values = {
            "operator": operator,
            "template": template,
            "rule": "D",
            "charge_entity_type": "one_off_charge",
            "to_address": "ops@example.com",
            "rendered_subject": "Owed",
            "rendered_body": "Owed.",
        }
        primary = message.objects.create(charge_entity_id=uuid.uuid4(), **values)
        folded = message.objects.create(
            charge_entity_id=uuid.uuid4(), status="combined", combined_into=primary, **values
        )
        waiting = message.objects.create(charge_entity_id=uuid.uuid4(), status="waiting", **values)
        back = _migrate(BEFORE)
        rows = back.get_model("ledger", "OutboundMessage").objects
        assert (rows.get(pk=folded.pk).status, rows.get(pk=folded.pk).error) == (
            "suppressed",
            "Combined",
        )
        assert rows.get(pk=waiting.pk).status == "queued"
        assert rows.get(pk=primary.pk).status == "queued"
    finally:
        _migrate(*leaves)


def test_the_outbox_links_a_combined_row_to_the_message_sent_for_it(clock: Any) -> None:
    lease = billed_lease(2501)
    one_off(lease, "80.00", day(-5), "C fee")
    one_off(lease, "40.00", day(-10), "D fee")
    run(clock, TODAY)
    found = rows_of(lease)
    admin, _ = client_for(Role.ADMIN)
    page = admin.get("/admin-settings/outbox/").content.decode()
    assert f'<a href="#message-{found["D"].pk}">Combined into the D message: Waiting</a>' in page
    assert f'id="message-{found["D"].pk}"' in page
