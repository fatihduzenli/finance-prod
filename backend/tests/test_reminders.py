"""The reminder and overdue rules A to E (docs/qa/PLAN-014-per-lease-billing-messaging-portal-
deposits.md E1 to E3, §5 E), Opus's tests: a calendar of days around one charge for each
rule, the autopay exception, the idempotent re-run, the stop on a closed charge, the
promise's pause and its resume without catch-up, installments per row, a one-off charge
without a lease, the first run over old charges, the engine's holds, the tick's line and the
summary. The engine itself (queue, window, send) is tested in ``test_messaging_engine.py``.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from accounts.roles import Role
from ledger.models import (
    CollectionSchedule,
    CompanySettings,
    ContactStatus,
    HeldReason,
    InstallmentPlan,
    Lease,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OneOffCharge,
    Operator,
    OutboundMessage,
    RentCharge,
)
from ledger.services import messaging, reminders, rent_run
from mail.memory import MemoryMailSender
from payments.models import Job, JobKind
from tests.job_helpers import run_queued_jobs
from tests.test_collections import (
    DUE,
    at,
    billed,
    operator,
    rent_charge,
    tick,
)
from tests.test_collections import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_collections import memory as memory  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_ledger_models import (
    make_user,
    new_fee_type,
    new_insurance_charge,
    new_lease,
    new_one_off_charge,
    new_payment,
    new_promise,
    new_rent_charge,
    new_rent_history,
    new_schedule,
    saved,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal


def day(offset: int) -> date:
    return DUE + timedelta(days=offset)


def rules_of(charge: Any) -> list[tuple[str, int]]:
    """(rule, occurrence) of every message about ``charge``, in queue order."""
    return list(
        OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
        .order_by("queued_at", "id")
        .values_list("rule", "occurrence")
    )


def calendar(clock: Any, first: int, last: int) -> dict[int, reminders.Summary]:
    """Run :func:`reminders.queue_due` on each day from ``first`` to ``last`` (offsets from
    the due date), at 10:00 New York; the days that queued something."""
    found = {}
    for offset in range(first, last + 1):
        clock(at(day(offset)))
        summary = reminders.queue_due(day(offset))
        if summary.queued:
            found[offset] = summary
    return found


def by_day(found: dict[int, reminders.Summary]) -> dict[int, dict[str, int]]:
    return {
        offset: {rule: count for rule, count in summary.queued_by_rule.items() if count}
        for offset, summary in found.items()
    }


def autopay(charge: RentCharge, **fields: Any) -> RentCharge:
    saved(new_schedule(lease=charge.lease, **fields))
    return charge


def monthly(charge: RentCharge) -> RentCharge:
    """Give the charge's lease its due day as debit day and a rent in force: A and B for
    rent come from the lease and the month (E4), three days before the month's debit."""
    Lease.objects.filter(pk=charge.lease_id).update(debit_day=charge.due_on.day)
    saved(new_rent_history(lease=charge.lease, amount=charge.amount_due))
    charge.lease.refresh_from_db()
    return charge


def month_rules(lease: Lease) -> list[tuple[str, date | None]]:
    """(rule, month) of every message about a month of ``lease`` (E4), in queue order."""
    return list(
        OutboundMessage.objects.filter(lease=lease, charge_entity_type="lease_month")
        .order_by("queued_at", "id")
        .values_list("rule", "period_month")
    )


OCT, NOV, DEC = date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)


# --- E1: the calendar of each rule ----------------------------------------------------------


def test_b_c_d_and_e_fire_on_their_days_only(clock: Any) -> None:
    # Re-pinned for E4: rent's B is the month's (three days before each month's debit, by
    # the lease), not the charge's; C, D and E stay the charge's.
    charge = monthly(rent_charge())
    found = calendar(clock, -10, 75)
    assert by_day(found) == {
        -3: {"B": 1},
        5: {"C": 1},
        10: {"D": 1},
        28: {"B": 1},
        30: {"E": 1},
        40: {"D": 1},
        50: {"D": 1},
        58: {"B": 1},
        60: {"D": 1},
        70: {"D": 1},
    }
    assert month_rules(charge.lease) == [("B", OCT), ("B", NOV), ("B", DEC)]
    assert rules_of(charge) == [
        ("C", 1),
        ("D", 1),
        ("E", 1),
        ("D", 2),
        ("D", 3),
        ("D", 4),
        ("D", 5),
    ]
    # Never at 20, 30 or 45 for D (the owner's 20-day gap, then every ten days).
    assert not {20, 45} & set(found)
    assert found[30].queued_by_rule["D"] == 0


def test_a_fires_three_days_before_the_debit_of_a_rent_charge_on_autopay_and_nothing_else(
    clock: Any,
) -> None:
    # Re-pinned for E4: A is the lease's and the month's (each month), never the charge's.
    charge = autopay(monthly(rent_charge()))
    found = calendar(clock, -10, 45)
    assert by_day(found) == {-3: {"A": 1}, 28: {"A": 1}}
    assert rules_of(charge) == []
    assert month_rules(charge.lease) == [("A", OCT), ("A", NOV)]
    message = OutboundMessage.objects.get(lease=charge.lease, period_month=OCT)
    assert message.template.code == "reminder_autopay"
    assert "will be debited on Oct 3, 2026" in message.rendered_subject
    assert "$2,450.00" in message.rendered_body


@pytest.mark.parametrize("state", ["paused", "failed", "ended", "none"])
def test_a_schedule_that_is_not_active_is_not_autopay(clock: Any, state: str) -> None:
    # Re-pinned for E4: the B is the month's; the charge keeps C.
    charge = autopay(monthly(rent_charge()), state=state)
    assert by_day(calendar(clock, -3, 5)) == {-3: {"B": 1}, 5: {"C": 1}}
    assert month_rules(charge.lease) == [("B", OCT)]
    assert rules_of(charge) == [("C", 1)]


def test_an_insurance_schedule_does_not_make_rent_autopay(clock: Any) -> None:
    # Re-pinned for E4: the B is the month's.
    charge = autopay(monthly(rent_charge()), kind="insurance")
    assert by_day(calendar(clock, -3, -3)) == {-3: {"B": 1}}
    assert month_rules(charge.lease) == [("B", OCT)]


def test_a_returned_debit_puts_an_autopay_charge_back_on_the_overdue_rules(clock: Any) -> None:
    # Re-pinned for E4: the A is the month's; the returned charge takes C and D.
    charge = autopay(monthly(rent_charge()))
    assert by_day(calendar(clock, -3, 1)) == {-3: {"A": 1}}
    saved(new_payment(rent_charge=charge, status="returned", amount=D("2450.00")))
    assert by_day(calendar(clock, 2, 10)) == {5: {"C": 1}, 10: {"D": 1}}
    assert month_rules(charge.lease) == [("A", OCT)]
    assert rules_of(charge) == [("C", 1), ("D", 1)]


def test_one_off_and_insurance_charges_take_b_to_e(clock: Any) -> None:
    owner = operator()
    lease = billed(saved(new_lease(operator=owner)), owner.email)
    one_off = saved(new_one_off_charge(operator=owner, lease=lease, due_date=DUE))
    policy = saved(new_insurance_charge(lease=lease, due_on=DUE))
    calendar(clock, -3, 30)
    for charge in (one_off, policy):
        assert rules_of(charge) == [("B", 1), ("C", 1), ("D", 1), ("E", 1)]


def test_e_goes_to_the_managers_once_and_internal(clock: Any, memory: MemoryMailSender) -> None:
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(
        manager_emails="boss@example.com\nlead@example.com"
    )
    charge = rent_charge(due_on=day(-30))
    clock(at(DUE))
    assert reminders.queue_due(DUE).queued_by_rule["E"] == 1
    assert reminders.queue_due(DUE).queued == 0
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert (message.rule, message.template.code) == (MessageRule.E, "internal_day30")
    assert message.template.internal is True
    run_queued_jobs()
    message.refresh_from_db()
    assert message.status == MessageStatus.SENT
    assert sorted(sent.to for sent in memory.sent) == ["boss@example.com", "lead@example.com"]
    assert charge.lease.operator is not None
    assert charge.lease.operator.email not in {sent.to for sent in memory.sent}


def test_e_with_an_empty_managers_list_is_held(clock: Any, memory: MemoryMailSender) -> None:
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(manager_emails="")
    charge = rent_charge(due_on=day(-30))
    clock(at(DUE))
    reminders.queue_due(DUE)
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert (message.status, message.held_reason) == (
        MessageStatus.HELD,
        HeldReason.NO_MANAGER_LIST,
    )
    assert memory.sent == []


# --- Idempotence and the first run ----------------------------------------------------------


def test_a_second_run_the_same_day_queues_nothing_and_counts_it_skipped(clock: Any) -> None:
    charge = rent_charge()
    clock(at(day(5)))
    first = reminders.queue_due(day(5))
    second = reminders.queue_due(day(5))
    assert (first.queued, first.skipped) == (1, 0)
    assert (second.queued, second.skipped, second.paused) == (0, 1, 0)
    assert rules_of(charge) == [("C", 1)]


def test_the_first_run_over_old_overdue_charges_sends_only_todays_rules(clock: Any) -> None:
    today = date(2026, 11, 20)
    late = {
        offset: rent_charge(due_on=today - timedelta(days=offset))
        for offset in (1, 4, 6, 9, 10, 15, 25, 29, 31, 39, 40, 41, 55, 400)
    }
    clock(at(today))
    summary = reminders.queue_due(today)
    assert dict(summary.queued_by_rule) == {"A": 0, "B": 0, "C": 0, "D": 3, "E": 0}
    assert rules_of(late[10]) == [("D", 1)]
    assert rules_of(late[40]) == [("D", 2)]
    assert rules_of(late[400]) == [("D", 38)]
    assert OutboundMessage.objects.count() == 3


# --- E2: closed charges, the promise's pause ------------------------------------------------


def test_a_paid_waived_or_voided_charge_gets_nothing(clock: Any) -> None:
    owner = operator()
    lease = billed(saved(new_lease(operator=owner)), owner.email)
    rent_charge(owner=owner, status="paid", amount_paid=D("2450.00"))
    rent_charge(owner=owner, status="waived")
    rent_charge(owner=owner, status="pending")
    for close in ("waived", "voided"):
        saved(new_one_off_charge(operator=owner, lease=lease, due_date=DUE, **closed(close)))
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).queued == 0
    assert OutboundMessage.objects.count() == 0


PRICE = D("300.00")


def closed(status: str, price: Decimal = PRICE) -> dict[str, Any]:
    """The fields of a one-off charge closed so (its CHECKs: a waive or void has a note)."""
    if status == "paid":
        return {"status": status, "amount_paid": price}
    note = {"waived": "waive_note", "voided": "void_note"}[status]
    return {"status": status, f"{status}_at": at(day(1)), note: "Agreed."}


@pytest.mark.parametrize("close", ["paid", "waived", "voided"])
def test_a_charge_closed_before_the_send_is_suppressed(
    clock: Any, memory: MemoryMailSender, close: str
) -> None:
    owner = operator()
    lease = billed(saved(new_lease(operator=owner)), owner.email)
    charge = saved(new_one_off_charge(operator=owner, lease=lease, due_date=DUE))
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).queued_by_rule["C"] == 1
    OneOffCharge.objects.filter(pk=charge.pk).update(**closed(close, charge.price))
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "ChargeClosed")
    assert memory.sent == []


def test_a_rent_charge_paid_before_the_send_is_suppressed(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge()
    clock(at(day(10)))
    reminders.queue_due(day(10))
    RentCharge.objects.filter(pk=charge.pk).update(status="paid", amount_paid=D("2450.00"))
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert message.status == MessageStatus.SUPPRESSED
    assert memory.sent == []


def promise(lease: Lease, promised: date, **fields: Any) -> Any:
    assert lease.operator is not None
    return saved(
        new_promise(operator=lease.operator, lease=lease, promised_date=promised, **fields)
    )


def test_an_open_promise_pauses_b_to_e_for_every_charge_of_the_lease(clock: Any) -> None:
    rent = rent_charge(due_on=day(-5))
    lease = rent.lease
    assert lease.operator is not None
    one_off = saved(new_one_off_charge(operator=lease.operator, lease=lease, due_date=day(3)))
    other = rent_charge(due_on=day(-5))  # another operator's lease: not paused
    promise(lease, day(12))
    clock(at(DUE))
    summary = reminders.queue_due(DUE)
    assert (summary.queued_by_rule["C"], summary.queued_by_rule["B"], summary.paused) == (1, 0, 2)
    assert rules_of(rent) == rules_of(one_off) == []
    assert rules_of(other) == [("C", 1)]
    # Through the promised date itself, D and E too.
    rent_d = rent_charge(owner=lease.operator, due_on=day(2), period_month=date(2026, 11, 1))
    RentCharge.objects.filter(pk=rent_d.pk).update(lease=lease)
    clock(at(day(12)))
    assert reminders.queue_due(day(12)).paused == 1
    assert rules_of(rent_d) == []


def test_an_open_promise_does_not_pause_a(clock: Any) -> None:
    # Re-pinned for E4: the A is the month's.
    charge = autopay(monthly(rent_charge()))
    promise(charge.lease, day(10))
    clock(at(day(-3)))
    summary = reminders.queue_due(day(-3))
    assert (summary.queued_by_rule["A"], summary.paused) == (1, 0)


def test_a_promise_about_no_store_pauses_the_operators_leases(clock: Any) -> None:
    charge = rent_charge()
    assert charge.lease.operator is not None
    saved(new_promise(operator=charge.lease.operator, lease=None, promised_date=day(9)))
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).paused == 1
    assert rules_of(charge) == []


@pytest.mark.parametrize("outcome", [ContactStatus.RESCHEDULED, ContactStatus.BROKEN])
def test_a_closed_or_deleted_promise_pauses_nothing(clock: Any, outcome: str) -> None:
    charge = rent_charge()
    promise(charge.lease, day(9), status=outcome)
    deleted = rent_charge()
    promise(
        deleted.lease,
        day(9),
        deleted_at=at(day(1)),
        deleted_by=make_user(),
        delete_reason="Entered twice",
    )
    clock(at(day(5)))
    summary = reminders.queue_due(day(5))
    assert (summary.queued_by_rule["C"], summary.paused) == (2, 0)


def test_a_broken_promise_resumes_from_the_next_rule_day_without_catch_up(clock: Any) -> None:
    charge = rent_charge()
    promise(charge.lease, day(7))
    found = calendar(clock, -5, 31)
    # C (day 5) was paused; the promise is broken from day 8; D (day 10) goes; C never.
    assert by_day(found) == {10: {"D": 1}, 30: {"E": 1}}
    assert rules_of(charge) == [("D", 1), ("E", 1)]


def test_installments_take_the_rules_per_row(clock: Any) -> None:
    owner = operator()
    lease = billed(saved(new_lease(operator=owner)), owner.email)
    parent = saved(
        new_one_off_charge(operator=owner, lease=lease, due_date=day(-60), price=D("900.00"))
    )
    plan = InstallmentPlan.objects.create(
        one_off_charge=parent,
        operator=owner,
        lease=lease,
        count=3,
        first_due=DUE,
        cadence="weekly",
        collection="manual",
        confirmed_at=at(day(-20)),
    )
    rows = [
        saved(
            OneOffCharge(
                operator=owner,
                lease=lease,
                category="installment",
                collection="manual",
                description=f"Installment {number} of 3",
                price=D("300.00"),
                due_date=day(7 * (number - 1)),
                status="owed",
                plan=plan,
                installment_no=number,
            )
        )
        for number in (1, 2, 3)
    ]
    clock(at(day(5)))
    summary = reminders.queue_due(day(5))
    # The first is 5 days late (C), the second due in 2 days, the third in 9 (nothing).
    assert summary.queued == 1
    assert rules_of(rows[0]) == [("C", 1)]
    assert rules_of(rows[1]) == rules_of(rows[2]) == []
    clock(at(day(11)))
    reminders.queue_due(day(11))
    assert rules_of(rows[2]) == [("B", 1)]
    # The parent, owed through its plan, is not an open balance while the plan is live: at
    # 70 days late it would take D.
    clock(at(day(10)))
    reminders.queue_due(day(10))
    assert rules_of(parent) == []
    assert rules_of(rows[0]) == [("C", 1), ("D", 1)]


# --- Billing lease, operator and the engine's holds -----------------------------------------


def test_a_one_off_charge_without_a_lease_goes_through_the_operators_first_lease(
    clock: Any,
) -> None:
    owner = operator()
    lease = billed(saved(new_lease(operator=owner)), "billing.first@example.com")
    charge = saved(new_one_off_charge(operator=owner, lease=None, due_date=DUE))
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).queued_by_rule["C"] == 1
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert (message.lease_id, message.to_address) == (lease.pk, "billing.first@example.com")


def test_a_one_off_charge_with_no_lease_to_bill_is_skipped(clock: Any) -> None:
    owner = operator()
    saved(new_lease(operator=owner, collection_status="ended", ended_on=date(2026, 9, 1)))
    charge = saved(new_one_off_charge(operator=owner, lease=None, due_date=DUE))
    clock(at(day(5)))
    summary = reminders.queue_due(day(5))
    assert (summary.queued, summary.skipped) == (0, 1)
    assert rules_of(charge) == []


def test_a_rent_charge_of_a_lease_without_an_operator_is_skipped(clock: Any) -> None:
    charge = rent_charge()
    Lease.objects.filter(pk=charge.lease_id).update(operator=None, collection_status="paused")
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).skipped == 1
    assert rules_of(charge) == []


def test_an_inactive_template_skips_its_rule(clock: Any) -> None:
    from ledger.models import MessageTemplate

    MessageTemplate.objects.filter(code="overdue_day5").update(active=False)
    charge = rent_charge()
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).skipped == 1
    assert rules_of(charge) == []


def test_a_handled_personally_operator_is_queued_and_held(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator(handled_personally=True, handled_personally_note="The owner calls him.")
    charge = rent_charge(owner=owner)
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).queued == 1
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert (message.status, message.held_reason) == (
        MessageStatus.HELD,
        HeldReason.HANDLED_PERSONALLY,
    )
    assert memory.sent == []


def test_a_handled_personally_operator_still_reaches_the_managers_with_e(
    clock: Any, memory: MemoryMailSender
) -> None:
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(
        manager_emails="boss@example.com"
    )
    owner = operator(handled_personally=True, handled_personally_note="The owner calls him.")
    rent_charge(owner=owner, due_on=day(-30))
    clock(at(DUE))
    reminders.queue_due(DUE)
    run_queued_jobs()
    assert [sent.to for sent in memory.sent] == ["boss@example.com"]


def test_a_lease_without_a_billing_email_is_held_no_address(
    clock: Any, memory: MemoryMailSender
) -> None:
    charge = rent_charge()
    billed(charge.lease, None)
    clock(at(day(5)))
    assert reminders.queue_due(day(5)).queued == 1
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert (message.status, message.held_reason) == (MessageStatus.HELD, HeldReason.NO_ADDRESS)
    assert memory.sent == []


def test_an_archived_operator_is_suppressed_at_the_send(
    clock: Any, memory: MemoryMailSender
) -> None:
    owner = operator()
    charge = rent_charge(owner=owner)
    Operator.objects.filter(pk=owner.pk).update(archived_at=at(day(1)))
    clock(at(day(5)))
    reminders.queue_due(day(5))
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(charge_entity_id=str(charge.pk))
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "OperatorArchived")


def test_c_is_sent_to_the_billing_email_with_its_text(clock: Any, memory: MemoryMailSender) -> None:
    owner = operator("ana@example.com", display_name="Ana Lima")
    charge = rent_charge(owner=owner)
    clock(at(day(5)))
    reminders.queue_due(day(5))
    run_queued_jobs()
    [sent] = memory.sent
    assert sent.to == "ana@example.com"
    assert sent.subject == "Payment overdue — #900 Store 900 Testville"
    assert "We have not yet received $2,450.00 for Rent October 2026" in sent.text
    assert "fee" not in sent.text.lower()  # E3: no late fee
    assert rules_of(charge) == [("C", 1)]


# --- E3: nothing is created -----------------------------------------------------------------


def test_the_rules_create_no_charge_and_no_fee(clock: Any) -> None:
    rent_charge()
    fee = saved(new_fee_type(code="late_fee_probe"))
    before = (RentCharge.objects.count(), OneOffCharge.objects.count())
    calendar(clock, -3, 60)
    assert (RentCharge.objects.count(), OneOffCharge.objects.count()) == before
    assert not OneOffCharge.objects.filter(fee_type=fee).exists()


# --- The tick and the summary ---------------------------------------------------------------


def test_the_tick_queues_the_days_rules_and_prints_the_counts(clock: Any) -> None:
    rent_charge(due_on=day(-5))
    rent_charge(due_on=day(-10))
    paused = rent_charge(due_on=day(-5))
    promise(paused.lease, day(2))
    clock(at(DUE))
    output = tick()
    assert (
        "Reminders queued: A 0, B 0, C 1, D 1, E 0. Reminders paused by a promise: 1. "
        "Reminders not queued (already queued, or no lease to bill): 0."
    ) in output
    output = tick()
    assert "Reminders queued: A 0, B 0, C 0, D 0, E 0." in output
    assert "Reminders not queued (already queued, or no lease to bill): 2." in output


def test_the_summary_names_every_rule(clock: Any) -> None:
    clock(at(DUE))
    summary = reminders.queue_due(DUE)
    assert summary == reminders.Summary(
        queued_by_rule={"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}, paused=0, skipped=0
    )
    assert summary.queued == 0
    assert reminders.RULES == ("A", "B", "C", "D", "E")
    assert frozenset({"B", "C", "D", "E"}) == reminders.PAUSED_BY_PROMISE


def test_rule_for_reads_autopay_itself_when_not_told(clock: Any) -> None:
    from ledger.services import receivables

    charge = autopay(rent_charge())
    [row] = [r for r in receivables.open_balances(DUE) if r.charge_id == charge.pk]
    # Re-pinned for E4: A and B for rent are never a charge's (rent_months_due).
    assert reminders.rule_for(row, day(-3)) is None
    assert reminders.rule_for(row, day(-3), autopay=False) is None
    assert reminders.rule_for(row, day(5)) is None
    assert reminders.rule_for(row, day(5), autopay=False) == reminders.Rule("C", "overdue_day5", 1)
    assert reminders.rule_for(row, day(40), autopay=False) == reminders.Rule(
        "D", "overdue_day10", 2
    )


# --- E4: A and B for rent keyed to the lease and the month ----------------------------------
#
# The owner's decision of 2026-10-30: rent charges are still made on their day; three days
# before the month's debit the lease gets A (on autopay) or B, once per lease and month.


RENT = D("2450.00")


def a_monthly_lease(
    debit_day: int = 3, *, autopay_on: bool = False, rent: Decimal = RENT, **fields: Any
) -> Lease:
    """A billed lease debited on ``debit_day`` with ``rent`` in force since January, on
    autopay (an active rent schedule of ``rent``) when asked."""
    owner = operator()
    lease = billed(saved(new_lease(operator=owner, debit_day=debit_day, **fields)), owner.email)
    saved(new_rent_history(lease=lease, amount=rent))
    if autopay_on:
        saved(new_schedule(lease=lease, debit_day=debit_day, amount=rent))
    return lease


def run_on(clock: Any, today: date) -> reminders.Summary:
    clock(at(today))
    return reminders.queue_due(today)


A_DAY = date(2026, 9, 30)  # three days before Oct 3


def test_one_a_per_lease_and_month_even_when_the_job_runs_twice(clock: Any) -> None:
    lease = a_monthly_lease(autopay_on=True)
    first = run_on(clock, A_DAY)
    second = run_on(clock, A_DAY)
    assert (first.queued_by_rule["A"], first.skipped) == (1, 0)
    assert (second.queued, second.skipped) == (0, 1)
    [message] = OutboundMessage.objects.filter(lease=lease)
    assert (message.rule, message.charge_entity_type, message.charge_entity_id) == (
        "A",
        "lease_month",
        lease.pk,
    )
    assert message.period_month == OCT
    assert message.template.code == "reminder_autopay"
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 1


def test_a_carries_the_scheduled_amount_date_and_bank_account(clock: Any) -> None:
    lease = a_monthly_lease(autopay_on=True)
    lease.billing.bank_last4 = "6789"
    lease.billing.save()
    # The schedule's amount is what is debited, even when the rent history says otherwise.
    CollectionSchedule.objects.filter(lease=lease).update(amount=D("2400.00"))
    run_on(clock, A_DAY)
    message = OutboundMessage.objects.get(lease=lease)
    assert message.rendered_subject.endswith("will be debited on Oct 3, 2026")
    assert "your rent of $2,400.00" in message.rendered_body
    assert "account ending in 6789 on Oct 3, 2026" in message.rendered_body


def test_no_a_when_autopay_is_paused_the_month_gets_b(clock: Any) -> None:
    lease = a_monthly_lease()
    saved(new_schedule(lease=lease, debit_day=3, state="paused"))
    summary = run_on(clock, A_DAY)
    assert (summary.queued_by_rule["A"], summary.queued_by_rule["B"]) == (0, 1)
    assert month_rules(lease) == [("B", OCT)]


@pytest.mark.parametrize(
    "fields",
    [
        {"ended_on": date(2026, 10, 2)},  # ends the day before the debit
        {"ended_on": date(2026, 9, 15), "collection_status": "ended"},
        {"collection_status": "paused"},
        {"started_on": date(2026, 10, 10)},  # starts after the debit day
    ],
)
def test_no_a_when_the_lease_ends_before_the_debit_or_is_not_billed(
    clock: Any, fields: dict[str, Any]
) -> None:
    lease = a_monthly_lease(autopay_on=True, **fields)
    summary = run_on(clock, A_DAY)
    assert summary.queued == 0
    assert month_rules(lease) == []


def test_a_lease_ending_on_the_debit_day_still_gets_its_a(clock: Any) -> None:
    lease = a_monthly_lease(autopay_on=True, ended_on=date(2026, 10, 3))
    assert run_on(clock, A_DAY).queued_by_rule["A"] == 1
    assert month_rules(lease) == [("A", OCT)]


@pytest.mark.parametrize("status", ["paid", "pending", "waived"])
def test_no_a_for_a_month_already_paid_in_advance(clock: Any, status: str) -> None:
    lease = a_monthly_lease(autopay_on=True)
    paid = {"amount_paid": D("2450.00")} if status == "paid" else {}
    saved(
        new_rent_charge(
            lease=lease, period_month=OCT, due_on=date(2026, 10, 3), status=status, **paid
        )
    )
    summary = run_on(clock, A_DAY)
    assert (summary.queued, summary.skipped) == (0, 1)
    assert month_rules(lease) == []


def test_a_month_whose_charge_exists_and_is_open_still_gets_its_a_once(clock: Any) -> None:
    lease = a_monthly_lease(autopay_on=True)
    charge = saved(new_rent_charge(lease=lease, period_month=OCT, due_on=date(2026, 10, 3)))
    run_on(clock, A_DAY)
    run_on(clock, A_DAY)
    assert month_rules(lease) == [("A", OCT)]
    assert rules_of(charge) == []


def test_b_for_rent_without_autopay_three_days_before_the_due_date_keyed_to_the_month(
    clock: Any,
) -> None:
    lease = a_monthly_lease(rent=D("1800.00"))
    found = calendar(clock, -10, 2)  # Sep 23 to Oct 5
    assert by_day(found) == {-3: {"B": 1}}
    run_on(clock, A_DAY)  # again the same day: nothing more
    [message] = OutboundMessage.objects.filter(lease=lease)
    assert (message.rule, message.charge_entity_type, message.period_month) == (
        "B",
        "lease_month",
        OCT,
    )
    assert message.template.code == "reminder_due"
    assert message.rendered_subject.startswith("Payment due Oct 3, 2026")
    assert "A payment of $1,800.00 for Rent October 2026" in message.rendered_body


def test_the_rent_charge_made_on_its_day_then_takes_c_d_and_e_and_b_stays_one(
    clock: Any,
) -> None:
    lease = a_monthly_lease()
    run_on(clock, A_DAY)
    charge, made = rent_run.create_month(lease, OCT, None)
    assert made and charge.due_on == DUE
    found = calendar(clock, -2, 30)
    assert by_day(found) == {5: {"C": 1}, 10: {"D": 1}, 28: {"B": 1}, 30: {"E": 1}}
    assert rules_of(charge) == [("C", 1), ("D", 1), ("E", 1)]
    assert month_rules(lease) == [("B", OCT), ("B", NOV)]


def test_the_rent_charge_of_an_a_month_whose_debit_is_returned_takes_c_and_a_stays_one(
    clock: Any,
) -> None:
    lease = a_monthly_lease(autopay_on=True)
    run_on(clock, A_DAY)
    charge, _ = rent_run.create_month(lease, OCT, None)
    saved(new_payment(rent_charge=charge, status="returned", amount=D("2450.00")))
    calendar(clock, -3, 10)
    assert rules_of(charge) == [("C", 1), ("D", 1)]
    assert month_rules(lease) == [("A", OCT)]


def test_a_lease_debited_twice_a_month_gets_one_a_for_the_first_debit(clock: Any) -> None:
    lease = a_monthly_lease(autopay_on=True, second_debit_day=18)
    CollectionSchedule.objects.filter(lease=lease).update(amount=D("1225.00"))
    saved(new_schedule(lease=lease, seq=2, debit_day=18, amount=D("1225.00")))
    found = calendar(clock, -3, 20)  # Sep 30 to Oct 23: both debits' -3 days
    assert by_day(found) == {-3: {"A": 1}}
    [message] = OutboundMessage.objects.filter(lease=lease)
    assert "your rent of $1,225.00" in message.rendered_body
    assert message.rendered_subject.endswith("Oct 3, 2026")


def test_a_short_month_moves_the_debit_to_its_last_day(clock: Any) -> None:
    lease = a_monthly_lease(debit_day=31, autopay_on=True)
    assert run_on(clock, date(2026, 11, 27)).queued_by_rule["A"] == 1  # Nov 30 is the 31st
    assert month_rules(lease) == [("A", NOV)]


def test_a_lease_starting_after_its_debit_day_is_first_due_on_its_start(clock: Any) -> None:
    lease = a_monthly_lease(debit_day=3, started_on=date(2026, 10, 12))
    assert run_on(clock, date(2026, 10, 9)).queued_by_rule["B"] == 1
    message = OutboundMessage.objects.get(lease=lease)
    assert message.rendered_subject.startswith("Payment due Oct 12, 2026")


def test_a_lease_without_rent_in_force_gets_no_b(clock: Any) -> None:
    owner = operator()
    lease = billed(saved(new_lease(operator=owner, debit_day=3)), owner.email)
    assert run_on(clock, A_DAY) == reminders.Summary(
        queued_by_rule={"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}, paused=0, skipped=0
    )
    assert month_rules(lease) == []


def test_an_open_promise_pauses_the_months_b_and_not_its_a(clock: Any) -> None:
    on_b = a_monthly_lease()
    promise(on_b, day(5))
    on_a = a_monthly_lease(autopay_on=True)
    promise(on_a, day(5))
    summary = run_on(clock, A_DAY)
    assert (summary.queued_by_rule["A"], summary.queued_by_rule["B"], summary.paused) == (1, 0, 1)
    assert month_rules(on_b) == []
    assert month_rules(on_a) == [("A", OCT)]


def test_rent_months_due_lists_the_month_and_its_rule(clock: Any) -> None:
    on_a = a_monthly_lease(autopay_on=True)
    on_b = a_monthly_lease(rent=D("900.00"))
    found = {month.lease.pk: (month, rule.code) for month, rule in reminders.rent_months_due(A_DAY)}
    assert {key: code for key, (_, code) in found.items()} == {on_a.pk: "A", on_b.pk: "B"}
    month, _ = found[on_b.pk]
    assert (month.month, month.debit_date, month.amount, month.seq) == (OCT, DUE, D("900.00"), 1)


# --- E4: checked again at the send ----------------------------------------------------------


def queued_month(clock: Any, *, autopay_on: bool = True) -> tuple[Lease, OutboundMessage]:
    lease = a_monthly_lease(autopay_on=autopay_on)
    run_on(clock, A_DAY)
    return lease, OutboundMessage.objects.get(lease=lease)


def test_a_lease_month_still_open_at_the_send_is_sent(clock: Any, memory: MemoryMailSender) -> None:
    lease, message = queued_month(clock)
    run_queued_jobs()
    message.refresh_from_db()
    assert message.status == MessageStatus.SENT
    assert [sent.to for sent in memory.sent] == [lease.billing.email]


def test_a_lease_ended_before_the_send_suppresses_the_month(
    clock: Any, memory: MemoryMailSender
) -> None:
    lease, message = queued_month(clock)
    Lease.objects.filter(pk=lease.pk).update(ended_on=date(2026, 10, 1), collection_status="ended")
    run_queued_jobs()
    message.refresh_from_db()
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "LeaseEnded")
    assert memory.sent == []


def test_a_schedule_stopped_before_the_send_suppresses_the_a(
    clock: Any, memory: MemoryMailSender
) -> None:
    lease, message = queued_month(clock)
    CollectionSchedule.objects.filter(lease=lease).update(state="paused")
    run_queued_jobs()
    message.refresh_from_db()
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "ScheduleStopped")
    assert memory.sent == []


def test_a_b_has_no_schedule_to_stop(clock: Any, memory: MemoryMailSender) -> None:
    _, message = queued_month(clock, autopay_on=False)
    assert message.rule == "B"
    run_queued_jobs()
    message.refresh_from_db()
    assert message.status == MessageStatus.SENT


@pytest.mark.parametrize("status", ["paid", "pending", "waived"])
def test_a_month_settled_before_the_send_is_suppressed(
    clock: Any, memory: MemoryMailSender, status: str
) -> None:
    lease, message = queued_month(clock)
    paid = {"amount_paid": D("2450.00")} if status == "paid" else {}
    saved(new_rent_charge(lease=lease, period_month=OCT, due_on=DUE, status=status, **paid))
    run_queued_jobs()
    message.refresh_from_db()
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "MonthSettled")
    assert memory.sent == []


def test_a_month_of_an_archived_operator_is_suppressed(clock: Any) -> None:
    lease, message = queued_month(clock)
    assert lease.operator is not None
    Operator.objects.filter(pk=lease.operator.pk).update(archived_at=at(day(-4)))
    run_queued_jobs()
    message.refresh_from_db()
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "OperatorArchived")


# --- E4: the rows the database keeps --------------------------------------------------------


def _row(lease: Lease, **fields: Any) -> OutboundMessage:
    assert lease.operator is not None
    values: dict[str, Any] = {
        "operator": lease.operator,
        "lease": lease,
        "template": MessageTemplate.objects.get(code="reminder_autopay"),
        "rule": "A",
        "charge_entity_type": "lease_month",
        "charge_entity_id": lease.pk,
        "period_month": OCT,
        "to_address": "x@example.com",
    }
    return OutboundMessage(**{**values, **fields})


@pytest.mark.parametrize(
    "fields",
    [
        {"period_month": None},  # a lease month names its month
        {"rule": "C"},  # and only A or B
        {"rule": "manual", "manual": True},
        {"charge_entity_type": "rent_charge"},  # no other message carries a month
        {"charge_entity_type": "one_off_charge"},
    ],
)
def test_the_check_holds_a_lease_month_to_its_month_and_rules(fields: dict[str, Any]) -> None:
    lease = a_monthly_lease()
    if fields.get("manual"):
        fields["sent_by"] = make_user()
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(lease, **fields).save()


def test_the_database_keeps_one_row_per_lease_rule_and_month() -> None:
    lease = a_monthly_lease()
    _row(lease).save()
    _row(lease, period_month=NOV).save()  # another month
    _row(lease, rule="B").save()  # another rule
    with pytest.raises(IntegrityError), transaction.atomic():
        _row(lease).save()
    assert OutboundMessage.objects.filter(lease=lease).count() == 3


# --- E4: the pages --------------------------------------------------------------------------


def test_the_operator_page_names_the_month_and_links_to_the_lease(clock: Any) -> None:
    lease, _ = queued_month(clock)
    assert lease.operator is not None
    [row] = messaging.operator_log(lease.operator, messaging.LOG_LIMIT)
    assert (row.charge_type, row.charge_id, row.charge_name) == (
        "lease_month",
        lease.pk,
        "Rent October 2026",
    )
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/operators/{lease.operator.pk}/").content.decode()
    assert f'<a href="/leases/{lease.pk}/">Rent October 2026</a>' in page


def test_the_outbox_and_the_store_timeline_list_the_month(clock: Any) -> None:
    lease, message = queued_month(clock)
    client, _ = client_for(Role.ADMIN)
    outbox = client.get("/admin-settings/outbox/")
    assert outbox.status_code == 200
    text = outbox.content.decode()
    assert f'id="message-{message.pk}"' in text
    assert f'href="/stores/{lease.store_id}/"' in text
    store = client.get(f"/stores/{lease.store_id}/?tab=contacts&show=emails")
    assert store.status_code == 200
    assert f'id="message-{message.pk}"' in store.content.decode()


# --- E4: ledger migration 0062 ----------------------------------------------------------------

ENGINE = ("ledger", "0061_messaging_engine")
RENT_MONTHS = ("ledger", "0062_rent_month_messages")


def _migrate(target: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    return MigrationExecutor(connection).loader.project_state(target).apps


def _columns() -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, "outbound_messages"
            )
        }


@pytest.mark.django_db(transaction=True)
def test_migration_0062_adds_the_month_and_reverses_only_without_lease_month_rows() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(ENGINE)
        assert "period_month" not in _columns()
        owner = old.get_model("ledger", "Operator").objects.create(display_name="Migrating")
        template = old.get_model("ledger", "MessageTemplate").objects.create(
            code="probe_0062", subject="Probe", body="Probe"
        )
        charge_id = uuid.uuid4()
        old.get_model("ledger", "OutboundMessage").objects.create(
            operator=owner,
            template=template,
            rule="C",
            charge_entity_type="rent_charge",
            charge_entity_id=charge_id,
            to_address="old@example.com",
        )
        new = _migrate(RENT_MONTHS)
        assert "period_month" in _columns()
        messages = new.get_model("ledger", "OutboundMessage").objects
        [kept] = messages.all()
        assert (kept.rule, kept.charge_entity_id, kept.period_month) == ("C", charge_id, None)
        # The widened CHECK: a lease month takes its month and A or B; nothing else a month.
        month = date(2026, 10, 1)
        lease_id = uuid.uuid4()
        row = {
            "operator_id": owner.pk,
            "template_id": template.pk,
            "charge_entity_type": "lease_month",
            "charge_entity_id": lease_id,
            "to_address": "new@example.com",
        }
        for bad in (
            {"rule": "A"},
            {"rule": "C", "period_month": month},
            {"rule": "C", "charge_entity_type": "rent_charge", "period_month": month},
        ):
            with pytest.raises(IntegrityError), transaction.atomic():
                messages.create(**{**row, **bad})
        october = messages.create(**row, rule="A", period_month=month)
        messages.create(**row, rule="A", period_month=date(2026, 11, 1))
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(ENGINE)
        assert "period_month" in _columns()
        messages.filter(charge_entity_type="lease_month").delete()
        assert october.pk not in set(messages.values_list("pk", flat=True))
        back = _migrate(ENGINE)
        assert "period_month" not in _columns()
        [restored] = back.get_model("ledger", "OutboundMessage").objects.all()
        assert restored.charge_entity_id == charge_id
        # The type CHECK of 0061 holds again.
        with pytest.raises(IntegrityError), transaction.atomic():
            back.get_model("ledger", "OutboundMessage").objects.create(
                **{**row, "rule": "B", "charge_entity_type": "lease_month"}
            )
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
        OutboundMessage.objects.all().delete()
        MessageTemplate.objects.filter(code="probe_0062").delete()
    assert "period_month" in _columns()
