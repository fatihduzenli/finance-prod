"""PLAN-014 Batch E (Fable): the reminder rules' adversarial cases, beside Opus's
``test_reminders.py`` — one run over charges at every offset at once; the day boundary either
side of each rule; the first run on old data (D's occurrence counted from the due date, not from
what was sent); rent on autopay gets A and never C while its schedule is on, C once the schedule
stops; an insurance charge's C names the policy year; a promise pauses B–E and not A, a broken
promise resumes without catch-up; a lease-less one-off bills the first active lease, none →
skipped; E without a managers' list is held; a second run the same day writes nothing; a paid
charge due in three days gets no B; the tick prints the counts."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    CollectionSchedule,
    ContactKind,
    HeldReason,
    Lease,
    LeaseBilling,
    MessageStatus,
    OneOffChargeStatus,
    Operator,
    OutboundMessage,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import contacts, messaging, reminders
from ledger.services.contacts import ContactInput
from payments.models import Job, JobKind
from tests.auth_helpers import make_user
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_collections import mail_set_up
from tests.test_collections import memory as memory  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_ledger_models import new_schedule, saved

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 20)  # a Tuesday


def billed_lease(
    number: int, email: str | None = "store@example.com", started: date | None = None
) -> Lease:
    lease = a_lease(
        a_store(str(number), number=number), an_operator(f"Operator {number}"), started=started
    )
    LeaseBilling.objects.update_or_create(
        lease=lease, defaults={"email": email or "", "company_name": f"Store {number} LLC"}
    )
    return lease


def rent_due(lease: Lease, due_on: date, **fields: Any) -> Any:
    """A rent charge due on ``due_on`` (its period month is the due date's month)."""
    return a_rent_charge(lease, due_on.replace(day=1), day=due_on.day, **fields)


def autopay(lease: Lease, state: str = ScheduleState.ACTIVE) -> CollectionSchedule:
    found: CollectionSchedule = saved(
        new_schedule(
            lease=lease, kind=ScheduleKind.RENT, seq=1, debit_day=lease.debit_day, state=state
        )
    )
    return found


def rules_of(lease: Lease) -> list[tuple[str, int]]:
    return list(
        OutboundMessage.objects.filter(lease=lease)
        .order_by("queued_at", "id")
        .values_list("rule", "occurrence")
    )


def managers(text: str) -> None:
    from ledger.models import CompanySettings

    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(manager_emails=text)


# --- One run, every offset at once ------------------------------------------------------------


def test_one_run_fires_exactly_todays_rule_for_each_charge() -> None:
    managers("boss@example.com")
    expected: dict[int, list[tuple[str, int]]] = {
        -3: [("B", 1)],
        0: [],
        5: [("C", 1)],
        9: [],
        10: [("D", 1)],
        20: [],
        30: [("E", 1)],
        40: [("D", 2)],
        45: [],
        50: [("D", 3)],
        95: [],
        100: [("D", 8)],
    }
    leases = {}
    for offset in expected:
        lease = billed_lease(1000 + offset + 10)
        rent_due(lease, TODAY - timedelta(days=offset))
        leases[offset] = lease
    summary = reminders.queue_due(TODAY)
    for offset, rules in expected.items():
        assert rules_of(leases[offset]) == rules, offset
    assert dict(summary.queued_by_rule) == {"A": 0, "B": 1, "C": 1, "D": 4, "E": 1}
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 7


def test_a_second_run_the_same_day_writes_nothing_and_counts_it() -> None:
    lease = billed_lease(1101)
    rent_due(lease, TODAY - timedelta(days=5))
    first = reminders.queue_due(TODAY)
    second = reminders.queue_due(TODAY)
    assert first.queued_by_rule["C"] == 1 and first.queued == 1
    assert second.queued == 0 and second.skipped >= 1
    assert OutboundMessage.objects.count() == 1
    assert Job.objects.filter(kind=JobKind.MESSAGE_SEND).count() == 1


def test_a_missed_day_is_not_caught_up_later() -> None:
    lease = billed_lease(1102)
    rent_due(lease, TODAY - timedelta(days=5))  # C's day is today
    reminders.queue_due(TODAY + timedelta(days=1))  # the tick ran a day late
    reminders.queue_due(TODAY + timedelta(days=2))
    assert rules_of(lease) == []  # nothing until D's day
    reminders.queue_due(TODAY + timedelta(days=5))
    assert rules_of(lease) == [("D", 1)]


# --- Autopay: A, and never C while the schedule is on ----------------------------------------


def test_rent_on_autopay_gets_a_three_days_before_the_debit_and_nothing_at_five_days_late() -> None:
    lease = billed_lease(1201)
    autopay(lease)
    coming = rent_due(lease, TODAY + timedelta(days=3))
    late = rent_due(lease, TODAY - timedelta(days=5), seq=2)  # the month's second debit
    summary = reminders.queue_due(TODAY)
    assert rules_of(lease) == [("A", 1)]
    assert summary.queued == 1 and summary.queued_by_rule["A"] == 1
    message = OutboundMessage.objects.get(lease=lease)
    assert message.charge_entity_id == coming.pk
    assert "will be debited" in message.rendered_subject
    assert late.pk != coming.pk


@pytest.mark.parametrize("state", [ScheduleState.PAUSED, ScheduleState.ENDED, ScheduleState.FAILED])
def test_rent_whose_schedule_stopped_gets_the_overdue_rules_again(state: str) -> None:
    lease = billed_lease(1202)
    autopay(lease, state)
    rent_due(lease, TODAY - timedelta(days=5))
    reminders.queue_due(TODAY)
    assert rules_of(lease) == [("C", 1)]


def test_a_rent_charge_without_autopay_gets_b_not_a() -> None:
    lease = billed_lease(1203)
    rent_due(lease, TODAY + timedelta(days=3))
    reminders.queue_due(TODAY)
    assert rules_of(lease) == [("B", 1)]


# --- Other charges ------------------------------------------------------------------------------


def test_an_insurance_charge_five_days_late_gets_c_naming_the_policy_year() -> None:
    lease = billed_lease(1301)
    an_insurance_charge(lease, 2026, TODAY - timedelta(days=5), "695.00")
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease)
    assert (message.rule, message.charge_entity_type) == ("C", "insurance_charge")
    assert "Insurance 2026–27" in message.rendered_body
    assert "$695.00" in message.rendered_body


def test_a_lease_less_one_off_bills_the_first_active_lease_and_none_is_skipped() -> None:
    lease = billed_lease(1401)
    assert lease.operator is not None
    a_one_off_charge(lease.operator, "250.00", TODAY - timedelta(days=10))
    homeless = an_operator("No Lease")
    a_one_off_charge(homeless, "250.00", TODAY - timedelta(days=10))
    summary = reminders.queue_due(TODAY)
    assert rules_of(lease) == [("D", 1)]
    assert OutboundMessage.objects.count() == 1
    assert summary.skipped >= 1


@pytest.mark.parametrize(
    "closed",
    [
        lambda lease: rent_due(
            lease, TODAY + timedelta(days=3), status=RentChargeStatus.PAID, paid="3500.00"
        ),
        lambda lease: rent_due(lease, TODAY + timedelta(days=3), status=RentChargeStatus.WAIVED),
        lambda lease: a_one_off_charge(
            lease.operator,
            "250.00",
            TODAY - timedelta(days=5),
            lease=lease,
            status=OneOffChargeStatus.VOIDED,
            voided_at=timezone.now(),
            void_note="Entered twice.",
        ),
    ],
)
def test_a_closed_charge_gets_nothing(closed: Any) -> None:
    lease = billed_lease(1402)
    closed(lease)
    summary = reminders.queue_due(TODAY)
    assert OutboundMessage.objects.count() == 0
    assert summary.queued == 0


# --- Promises --------------------------------------------------------------------------------


def promise(lease: Lease, promised: date, happened: date = TODAY) -> None:
    assert lease.operator is not None
    user, _ = make_user(f"promise-taker-{lease.store_id.hex[:6]}", Role.BILLING)
    outcome = contacts.add_entry(
        lease.operator.pk,
        ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=happened,
            body="Will pay.",
            promised_date=promised,
            promised_amount=Decimal("3500.00"),
            lease=lease.pk,
        ),
        actor=user,
    )
    assert outcome.ok, outcome.message


def test_an_open_promise_pauses_b_to_e_and_not_a() -> None:
    managers("boss@example.com")
    paused = billed_lease(1501)
    rent_due(paused, TODAY - timedelta(days=5))
    rent_due(paused, TODAY - timedelta(days=30))
    promise(paused, TODAY + timedelta(days=4))
    on_autopay = billed_lease(1502)
    autopay(on_autopay)
    rent_due(on_autopay, TODAY + timedelta(days=3))
    promise(on_autopay, TODAY + timedelta(days=4))
    summary = reminders.queue_due(TODAY)
    assert rules_of(paused) == []
    assert rules_of(on_autopay) == [("A", 1)]
    assert summary.paused >= 2


def test_a_broken_promise_resumes_the_rules_without_catch_up() -> None:
    lease = billed_lease(1503)
    rent_due(lease, TODAY - timedelta(days=5))  # C's day is today, paused
    promise(lease, TODAY, happened=TODAY - timedelta(days=2))
    reminders.queue_due(TODAY)
    assert rules_of(lease) == []
    # The day after the promised date it is broken: C's day has passed, D's day (+10) comes.
    reminders.queue_due(TODAY + timedelta(days=1))
    assert rules_of(lease) == []
    reminders.queue_due(TODAY + timedelta(days=5))
    assert rules_of(lease) == [("D", 1)]


def test_a_promise_on_another_lease_of_the_same_operator_pauses_that_lease_only() -> None:
    first = billed_lease(1504)
    assert first.operator is not None
    second = a_lease(a_store("1505", number=1505), first.operator)
    LeaseBilling.objects.update_or_create(lease=second, defaults={"email": "two@example.com"})
    rent_due(first, TODAY - timedelta(days=5))
    rent_due(second, TODAY - timedelta(days=5))
    promise(second, TODAY + timedelta(days=2))
    reminders.queue_due(TODAY)
    assert rules_of(first) == [("C", 1)]
    assert rules_of(second) == []


# --- E, the managers, and the held reasons ----------------------------------------------------


def test_e_goes_to_the_managers_once_and_is_held_without_a_list() -> None:
    managers("")
    lease = billed_lease(1601)
    rent_due(lease, TODAY - timedelta(days=30))
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease)
    assert message.rule == "E" and message.template.internal
    with override_settings(MAIL_ENABLED=False):
        messaging.send(message.pk)
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    assert message.held_reason in (HeldReason.NO_MANAGER_LIST, HeldReason.NOT_SET_UP)
    # Tomorrow and at 40 days: no second E; D continues on its own days.
    reminders.queue_due(TODAY + timedelta(days=1))
    reminders.queue_due(TODAY + timedelta(days=10))
    assert rules_of(lease) == [("E", 1), ("D", 2)]


def test_e_reaches_every_manager_and_never_the_operator(settings: Any, memory: Any) -> None:
    mail_set_up(settings)
    managers("a@example.com\nb@example.com")
    lease = billed_lease(1602, email="operator@example.com")
    rent_due(lease, TODAY - timedelta(days=30))
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease)
    messaging.send(message.pk)
    assert sorted(sent.to for sent in memory.sent) == ["a@example.com", "b@example.com"]
    assert all("operator@example.com" != sent.to for sent in memory.sent)


def test_a_lease_without_a_billing_email_queues_and_is_held_no_address() -> None:
    lease = billed_lease(1603, email=None)
    rent_due(lease, TODAY - timedelta(days=5))
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease)
    with override_settings(MAIL_ENABLED=True, GMAIL_SERVICE_ACCOUNT_JSON="{}"):
        messaging.send(message.pk)
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    assert message.held_reason in (HeldReason.NO_ADDRESS, HeldReason.NOT_SET_UP)


def test_an_operator_handled_personally_is_held_not_skipped() -> None:
    lease = billed_lease(1604)
    assert lease.operator is not None
    Operator.objects.filter(pk=lease.operator.pk).update(handled_personally=True)
    rent_due(lease, TODAY - timedelta(days=5))
    reminders.queue_due(TODAY)
    message = OutboundMessage.objects.get(lease=lease)
    with override_settings(MAIL_ENABLED=False):
        messaging.send(message.pk)
    message.refresh_from_db()
    assert message.status == MessageStatus.HELD
    assert message.held_reason in (HeldReason.HANDLED_PERSONALLY, HeldReason.NOT_SET_UP)


# --- The tick ------------------------------------------------------------------------------------


def test_the_tick_queues_the_rules_and_prints_the_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "localdate", lambda *args, **kwargs: TODAY)
    lease = billed_lease(1701, started=TODAY.replace(day=1))  # the rent run catches up Oct only
    rent_due(lease, TODAY - timedelta(days=5))
    out = StringIO()
    call_command("collections_tick", stdout=out)
    assert rules_of(lease) == [("C", 1)]
    text = out.getvalue()
    assert "Reminders queued: A 0, B 0, C 1, D 0, E 0." in text
    Job.objects.filter(kind=JobKind.DISPUTES_SYNC).delete()
