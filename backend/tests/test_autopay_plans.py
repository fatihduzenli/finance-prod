"""Autopay plans (docs/qa/PLAN-012f-a-design-follow-up.md D11, D13 to D15; §6 test plan):
the dates of each cadence, each end rule, the lead, the daily run's idempotency, the worker's
single-occurrence schedule and its failure, pause / resume / stop / change end with their
audit rows, the refusals at setup, the large-debit audit per occurrence, the lease-end and
transfer blockers, the store's History tab and the run page's data. Nothing reaches a
network: the payment provider is a :class:`~payments.fake.FakeProvider`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    AutopayPlan,
    AutopayPlanState,
    CollectionMethod,
    CollectionStatus,
    Invoice,
    InvoiceStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    PaymentChannel,
)
from ledger.services import audit, autopay_plans, lease_end, one_off_charges, transfer
from payments.errors import ProviderRejected
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import CustomerRecord, PaymentMethodRecord, ScheduleInput
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import a_lease, a_store
from tests.test_lease_services import make_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


# --- Builders ---------------------------------------------------------------------------------


def today() -> date:
    return timezone.localdate()


def a_plan_lease(
    fake: FakeProvider | None = None, *, customer: bool = True, bank: bool = True
) -> Lease:
    """An active lease whose operator has its own provider customer and linked bank
    method (each known to ``fake`` when given)."""
    tag = uuid.uuid4().hex[:8]
    email = f"{tag}@example.com"
    customer_id = f"cus-{tag}" if customer else None
    operator = make_operator(email=email, customer=customer_id)
    if customer_id and bank:
        ExternalRef.objects.create(
            entity_type="operator",
            entity_id=operator.pk,
            provider="stax_pay",
            external_kind="payment_method",
            external_id=f"pm-{tag}",
        )
    if fake is not None and customer_id:
        fake.customers[customer_id] = CustomerRecord(
            external_id=customer_id, raw_status="", fetched_at=timezone.now(), operator_email=email
        )
        if bank:
            fake.add_payment_method(
                customer_id,
                PaymentMethodRecord(
                    external_id=f"pm-{tag}", raw_status="", fetched_at=timezone.now(), method="bank"
                ),
            )
    return a_lease(
        a_store(f"Store {tag}"),
        operator,
        channel=PaymentChannel.PROVIDER_ACH,
        started=date(2026, 1, 1),
    )


def setup(lease: Lease | None, **overrides: Any) -> autopay_plans.Outcome:
    values: dict[str, Any] = {
        "item_name": "Sign fee",
        "description": "",
        "quantity": D("1"),
        "unit_amount": D("100.00"),
        "cadence": "monthly",
        "first_on": today() + timedelta(days=10),
        "end_rule": "count",
        "end_count": 3,
        "end_on": None,
        "actor": None,
        "confirmed": False,
        "today": today(),
    }
    values.update(overrides)
    return autopay_plans.create(lease, **values)


def a_plan(lease: Lease, **overrides: Any) -> AutopayPlan:
    outcome = setup(lease, **overrides)
    assert outcome.ok and outcome.plan is not None, outcome.message
    return outcome.plan


def a_scheduled_plan(lease: Lease, **overrides: Any) -> AutopayPlan:
    """A plan whose first occurrence's schedule the worker made (the ``fake`` and
    ``worker`` fixtures)."""
    plan = a_plan(lease, **overrides)
    assert [job.status for job in run_queued_jobs()] == [JobStatus.DONE]
    return plan


def occurrences(plan: AutopayPlan) -> list[OneOffCharge]:
    return list(OneOffCharge.objects.filter(autopay_plan=plan).order_by("period_no"))


def fresh(plan: AutopayPlan) -> AutopayPlan:
    return AutopayPlan.objects.get(pk=plan.pk)


def plan_actions(plan: AutopayPlan) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type="autopay_plan", entity_id=str(plan.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )


def run_until_finished(plan: AutopayPlan, limit: int = 60) -> None:
    """Run the daily step on each occurrence's lead day until the plan stops making."""
    for _ in range(limit):
        current = fresh(plan)
        if current.state != AutopayPlanState.ACTIVE:
            return
        autopay_plans.run_due(current.next_on - timedelta(days=autopay_plans.LEAD_DAYS))
    raise AssertionError("the plan never finished")


def user(role: Role) -> Any:
    return client_for(role)[1]


# --- Dates of each cadence --------------------------------------------------------------------


def test_monthly_dates_keep_the_first_day_clamped_to_each_months_end() -> None:
    assert autopay_plans.occurrence_dates(date(2027, 1, 31), "monthly", 5) == [
        date(2027, 1, 31),
        date(2027, 2, 28),
        date(2027, 3, 31),
        date(2027, 4, 30),
        date(2027, 5, 31),
    ]
    assert autopay_plans.occurrence_dates(date(2028, 1, 31), "monthly", 2)[1] == date(2028, 2, 29)


def test_weekly_dates_cross_the_year_boundary() -> None:
    assert autopay_plans.occurrence_dates(date(2026, 12, 21), "weekly", 4) == [
        date(2026, 12, 21),
        date(2026, 12, 28),
        date(2027, 1, 4),
        date(2027, 1, 11),
    ]


def test_yearly_dates_from_29_february_fall_on_28_february_and_back_on_29() -> None:
    assert autopay_plans.occurrence_dates(date(2028, 2, 29), "yearly", 5) == [
        date(2028, 2, 29),
        date(2029, 2, 28),
        date(2030, 2, 28),
        date(2031, 2, 28),
        date(2032, 2, 29),
    ]


def test_an_unknown_cadence_has_no_dates() -> None:
    with pytest.raises(ValueError, match="monthly, weekly or yearly"):
        autopay_plans.occurrence_on(date(2027, 1, 1), "daily", 1)


@pytest.mark.parametrize(
    ("first", "cadence", "day", "expected"),
    [
        (date(2027, 1, 31), "monthly", date(2027, 3, 1), date(2027, 3, 31)),
        (date(2027, 1, 31), "monthly", date(2027, 2, 28), date(2027, 2, 28)),
        (date(2026, 12, 21), "weekly", date(2026, 12, 29), date(2027, 1, 4)),
        (date(2028, 2, 29), "yearly", date(2029, 1, 1), date(2029, 2, 28)),
        (date(2027, 1, 31), "monthly", date(2026, 1, 1), date(2027, 1, 31)),
    ],
)
def test_the_series_date_on_or_after_a_day(
    first: date, cadence: str, day: date, expected: date
) -> None:
    assert autopay_plans.series_on_or_after(first, cadence, day) == expected


# --- Setup and the first occurrence -----------------------------------------------------------


def test_setup_stores_the_plan_and_makes_the_first_occurrence_at_once() -> None:
    lease = a_plan_lease()
    first = today() + timedelta(days=30)
    outcome = setup(
        lease,
        item_name="  Sign fee ",
        description="Front sign",
        quantity=D("2"),
        unit_amount=D("37.50"),
        first_on=first,
    )
    assert outcome.ok and outcome.plan is not None, outcome.message
    plan = fresh(outcome.plan)
    assert (plan.item_name, plan.description, plan.amount) == ("Sign fee", "Front sign", D("75.00"))
    assert (plan.store_id, plan.operator_id, plan.lease_id) == (
        lease.store_id,
        lease.operator_id,
        lease.pk,
    )
    assert (plan.state, plan.made_count, plan.end_count, plan.end_on) == ("active", 1, 3, None)
    assert plan.next_on == autopay_plans.occurrence_on(first, "monthly", 1)
    assert plan.large_debit_confirmed_at is None
    [charge] = occurrences(plan)
    assert outcome.charge is not None and outcome.charge.pk == charge.pk
    assert (charge.category, charge.collection) == (
        OneOffCategory.AUTOPAY_PLAN,
        CollectionMethod.AUTOPAY_DUE,
    )
    assert (charge.period_no, charge.due_date, charge.price) == (1, first, D("75.00"))
    assert charge.description == "Sign fee · 1 of 3"
    assert autopay_plans.made_ordinal_of(plan, charge) == 1
    assert charge.fee_type_id is None and charge.lease_id == lease.pk
    created = AuditLog.objects.get(action="one_off_charge.created", entity_id=str(charge.pk))
    assert created.after["creation_key"] == str(uuid.uuid5(plan.pk, "1"))
    assert created.after["autopay_plan"] == str(plan.pk) and created.after["period_no"] == 1
    assert [job.kind for job in Job.objects.all()] == [JobKind.SCHEDULE_CREATE]
    assert plan_actions(plan) == [autopay_plans.CREATED_ACTION]
    assert "$75.00 monthly" in outcome.message


def test_a_rule_never_ignores_a_count_and_a_date() -> None:
    plan = a_plan(a_plan_lease(), end_rule="never", end_count=4, end_on=today())
    assert (plan.end_count, plan.end_on) == (None, None)


# --- Each end rule ----------------------------------------------------------------------------


def test_a_count_of_three_makes_three_occurrences_then_finishes() -> None:
    first = today() + timedelta(days=10)
    plan = a_plan(a_plan_lease(), first_on=first, end_count=3)
    run_until_finished(plan)
    made = occurrences(plan)
    assert [row.due_date for row in made] == autopay_plans.occurrence_dates(first, "monthly", 3)
    assert [row.period_no for row in made] == [1, 2, 3]
    finished = fresh(plan)
    assert (finished.state, finished.made_count) == ("finished", 3)
    assert plan_actions(plan)[-1] == autopay_plans.FINISHED_ACTION
    # A finished plan makes nothing more.
    assert autopay_plans.run_due(finished.next_on) == []
    assert len(occurrences(plan)) == 3


def test_an_end_date_finishes_after_the_last_date_on_or_before_it() -> None:
    first = today() + timedelta(days=10)
    plan = a_plan(
        a_plan_lease(),
        cadence="weekly",
        first_on=first,
        end_rule="date",
        end_count=None,
        end_on=first + timedelta(days=16),
    )
    run_until_finished(plan)
    assert [row.due_date for row in occurrences(plan)] == [
        first,
        first + timedelta(days=7),
        first + timedelta(days=14),
    ]
    assert fresh(plan).state == "finished"


def test_an_end_date_on_the_first_date_makes_one_occurrence() -> None:
    first = today() + timedelta(days=10)
    plan = a_plan(a_plan_lease(), end_rule="date", end_count=None, end_on=first)
    assert fresh(plan).state == "finished" and len(occurrences(plan)) == 1


def test_never_keeps_making_occurrences() -> None:
    first = today() + timedelta(days=10)
    plan = a_plan(a_plan_lease(), cadence="yearly", first_on=first, end_rule="never")
    for _ in range(4):
        current = fresh(plan)
        autopay_plans.run_due(current.next_on - timedelta(days=autopay_plans.LEAD_DAYS))
    assert [row.due_date for row in occurrences(plan)] == autopay_plans.occurrence_dates(
        first, "yearly", 5
    )
    assert fresh(plan).state == "active"


# --- The lead and the daily run ---------------------------------------------------------------


def test_an_occurrence_is_made_exactly_lead_days_before_its_date_and_not_before() -> None:
    plan = a_plan(a_plan_lease())
    second = fresh(plan).next_on
    lead = second - timedelta(days=autopay_plans.LEAD_DAYS)
    assert autopay_plans.make_due(plan, lead - timedelta(days=1)) is None
    assert len(occurrences(plan)) == 1
    made = autopay_plans.make_due(plan, lead)
    assert made is not None and (made.due_date, made.period_no) == (second, 2)
    assert autopay_plans.LEAD_DAYS == 7


def test_two_runs_on_the_same_day_make_one_occurrence() -> None:
    plan = a_plan(a_plan_lease())
    lead = fresh(plan).next_on - timedelta(days=autopay_plans.LEAD_DAYS)
    assert len(autopay_plans.run_due(lead)) == 1
    assert autopay_plans.run_due(lead) == []
    assert [row.period_no for row in occurrences(plan)] == [1, 2]


def pin_today(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: day)


def test_the_daily_tick_makes_the_due_occurrence_once(monkeypatch: pytest.MonkeyPatch) -> None:
    first = today() + timedelta(days=3)
    plan = a_plan(a_plan_lease(), cadence="weekly", first_on=first, end_rule="never")
    # On the first date, the second occurrence (first + 7) is within the lead.
    pin_today(monkeypatch, first)
    call_command("collections_tick", stdout=_Sink())
    call_command("collections_tick", stdout=_Sink())
    assert [row.due_date for row in occurrences(plan)] == [first, first + timedelta(days=7)]


class _Sink:
    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> None:
        self.text += text

    def flush(self) -> None:
        return None


def test_the_tick_reports_the_plan_debits_it_created(monkeypatch: pytest.MonkeyPatch) -> None:
    first = today() + timedelta(days=3)
    a_plan(a_plan_lease(), cadence="weekly", first_on=first)
    pin_today(monkeypatch, first)
    sink = _Sink()
    call_command("collections_tick", stdout=sink)
    assert "Autopay plan debits created: 1." in sink.text


def test_a_run_that_missed_the_date_debits_today_and_catches_up() -> None:
    first = today() + timedelta(days=10)
    plan = a_plan(a_plan_lease(), cadence="weekly", first_on=first, end_rule="never")
    late = first + timedelta(days=14)
    made = autopay_plans.run_due(late)
    # The missed second (first + 7) is debited today, with the third (today) and the
    # fourth (within the lead).
    assert [row.due_date for row in made] == [late, late, late + timedelta(days=7)]
    assert [row.period_no for row in made] == [2, 3, 4]
    assert fresh(plan).next_on == first + timedelta(days=28)


def test_an_occurrence_the_one_off_rules_refuse_is_audited_once_and_the_plan_waits() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    ExternalRef.objects.filter(external_kind="payment_method").update(superseded_at=timezone.now())
    lead = fresh(plan).next_on - timedelta(days=autopay_plans.LEAD_DAYS)
    assert autopay_plans.run_due(lead) == []
    assert autopay_plans.run_due(lead + timedelta(days=1)) == []
    refused = AuditLog.objects.filter(action=autopay_plans.OCCURRENCE_REFUSED_ACTION)
    assert refused.count() == 1
    assert refused.get().after["reason"] == one_off_charges.NO_BANK_METHOD
    current = fresh(plan)
    assert (current.state, current.made_count) == ("active", 1)


# --- The worker: one single-occurrence schedule each ------------------------------------------


def test_each_occurrence_gets_one_single_occurrence_schedule(
    fake: FakeProvider, worker: None
) -> None:
    plan = a_plan(a_plan_lease(fake))
    autopay_plans.make_due(plan, fresh(plan).next_on - timedelta(days=autopay_plans.LEAD_DAYS))
    done = run_queued_jobs()
    assert [job.status for job in done] == [JobStatus.DONE, JobStatus.DONE]
    rules = [
        arguments[0].rule
        for method, arguments in fake.calls
        if method == "create_scheduled_invoice" and isinstance(arguments[0], ScheduleInput)
    ]
    assert rules == [
        f"DTSTART={row.due_date:%Y%m%d}T120000Z;FREQ=DAILY;COUNT=1" for row in occurrences(plan)
    ]


def test_a_failed_schedule_leaves_its_charge_to_retry_and_the_plan_active(
    fake: FakeProvider, worker: None
) -> None:
    plan = a_plan(a_plan_lease(fake))
    fake.fail_next("create_scheduled_invoice", ProviderRejected(422, error_fields=("rule",)))
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    [charge] = occurrences(plan)
    assert AuditLog.objects.filter(
        action="one_off_charge.schedule_failed", entity_id=str(charge.pk)
    ).exists()
    assert one_off_charges.retry_kind(charge) == JobKind.SCHEDULE_CREATE
    assert fresh(plan).state == "active"
    made = autopay_plans.make_due(
        plan, fresh(plan).next_on - timedelta(days=autopay_plans.LEAD_DAYS)
    )
    assert made is not None and made.period_no == 2


# --- Pause, resume, stop, change end ----------------------------------------------------------


def test_pause_cancels_the_pending_occurrence_and_resume_makes_the_next(
    fake: FakeProvider, worker: None
) -> None:
    first = today() + timedelta(days=5)
    plan = a_plan(a_plan_lease(fake), first_on=first, end_count=4)
    run_queued_jobs()
    [pending] = occurrences(plan)
    paused = autopay_plans.pause(plan, user(Role.BILLING))
    assert paused.ok, paused.message
    current = fresh(plan)
    assert (current.state, current.made_count, current.next_on) == ("paused", 0, first)
    assert Job.objects.filter(kind=JobKind.SCHEDULE_DELETE).count() == 1
    run_queued_jobs()
    assert OneOffCharge.objects.get(pk=pending.pk).status == OneOffChargeStatus.VOIDED
    paused_row = AuditLog.objects.get(action=autopay_plans.PAUSED_ACTION)
    assert paused_row.after["cancelled"] == [str(pending.pk)]
    # Paused: the daily run makes nothing.
    assert autopay_plans.run_due(first) == []
    # Resumed after the first date: the next date of the series after that day.
    later = first + timedelta(days=1)
    resumed = autopay_plans.resume(plan, user(Role.BILLING), today=later)
    assert resumed.ok, resumed.message
    current = fresh(plan)
    second = autopay_plans.occurrence_on(first, "monthly", 1)
    assert current.state == "active"
    assert plan_actions(plan)[-1] == autopay_plans.RESUMED_ACTION
    # The second date is weeks away: nothing is due yet, the lead makes it.
    assert resumed.charge is None and current.next_on == second
    made = autopay_plans.make_due(plan, second - timedelta(days=autopay_plans.LEAD_DAYS))
    assert made is not None and made.due_date == second
    assert [row.period_no for row in occurrences(plan)] == [1, 2]
    assert fresh(plan).made_count == 1


def test_resume_makes_the_next_occurrence_at_once_when_it_is_due(
    fake: FakeProvider, worker: None
) -> None:
    first = today() + timedelta(days=3)
    plan = a_scheduled_plan(a_plan_lease(fake), cadence="weekly", first_on=first)
    assert autopay_plans.pause(plan, user(Role.BILLING)).ok
    resumed = autopay_plans.resume(plan, user(Role.BILLING), today=today())
    assert resumed.ok and resumed.charge is not None
    # The cancelled first date is still ahead: the series resumes on it.
    assert (resumed.charge.due_date, resumed.charge.period_no) == (first, 2)
    assert fresh(plan).made_count == 1


def test_after_a_pause_the_next_occurrence_reads_its_made_ordinal_never_beyond_the_count(
    fake: FakeProvider, worker: None
) -> None:
    first = today() + timedelta(days=3)
    plan = a_scheduled_plan(a_plan_lease(fake), cadence="weekly", first_on=first, end_count=3)
    billing = user(Role.BILLING)
    # Pause gives the first period back; resume makes it again as period 2.
    assert autopay_plans.pause(plan, billing).ok
    run_queued_jobs()
    resumed = autopay_plans.resume(plan, billing, today=today())
    assert resumed.ok and resumed.charge is not None
    assert (resumed.charge.period_no, resumed.charge.description) == (2, "Sign fee · 1 of 3")
    run_queued_jobs()
    run_until_finished(plan)
    made = occurrences(plan)
    assert [row.period_no for row in made] == [1, 2, 3, 4]
    assert [row.description for row in made] == [
        "Sign fee · 1 of 3",
        "Sign fee · 1 of 3",
        "Sign fee · 2 of 3",
        "Sign fee · 3 of 3",
    ]
    assert all("4 of 3" not in row.description for row in made)
    assert [autopay_plans.made_ordinal_of(plan, row) for row in made] == [1, 1, 2, 3]
    assert fresh(plan).state == "finished" and fresh(plan).made_count == 3


def test_an_open_ended_occurrence_reads_its_ordinal_alone_and_a_long_item_is_cut() -> None:
    plan = a_plan(a_plan_lease(), item_name="x" * 200, end_rule="never")
    [charge] = occurrences(plan)
    assert charge.description == "x" * 116 + " · 1"
    assert len(charge.description) == 120
    dated = a_plan(
        a_plan_lease(), end_rule="date", end_count=None, end_on=today() + timedelta(days=90)
    )
    assert occurrences(dated)[0].description == "Sign fee · 1"


def test_pause_keeps_an_occurrence_whose_schedule_made_its_invoice() -> None:
    plan = a_plan(a_plan_lease())
    [charge] = occurrences(plan)
    Invoice.objects.create(
        invoice_number="INV-2026-9001",
        operator=plan.operator,
        one_off_charge=charge,
        issue_date=today(),
        due_date=charge.due_date,
        subtotal=charge.price,
        tax=D("0"),
        fees=D("0"),
        surcharge=D("0"),
        total=charge.price,
        internal_status=InvoiceStatus.SENT,
        allow_card=False,
        allow_ach=True,
        legacy=False,
    )
    assert autopay_plans.pause(plan, user(Role.BILLING)).ok
    assert not Job.objects.filter(kind=JobKind.SCHEDULE_DELETE).exists()
    assert fresh(plan).made_count == 1


def test_pause_waits_while_the_occurrences_provider_request_is_queued() -> None:
    plan = a_plan(a_plan_lease())
    refused = autopay_plans.pause(plan, user(Role.BILLING))
    assert (refused.ok, refused.message) == (False, one_off_charges.CANCEL_BUSY)
    current = fresh(plan)
    assert (current.state, current.made_count) == ("active", 1)
    assert plan_actions(plan) == [autopay_plans.CREATED_ACTION]
    assert not AuditLog.objects.filter(action=one_off_charges.CANCEL_ACTION).exists()


def test_pause_and_resume_need_billing_and_the_right_state(
    fake: FakeProvider, worker: None
) -> None:
    plan = a_scheduled_plan(a_plan_lease(fake))
    refused = autopay_plans.pause(plan, user(Role.VIEWER))
    assert (refused.ok, refused.message) == (False, autopay_plans.BILLING_ONLY)
    assert autopay_plans.pause(plan, None).message == autopay_plans.BILLING_ONLY
    assert autopay_plans.resume(plan, user(Role.BILLING)).message == autopay_plans.NOT_PAUSED
    assert autopay_plans.pause(plan, user(Role.BILLING)).ok
    assert autopay_plans.pause(plan, user(Role.BILLING)).message == autopay_plans.NOT_ACTIVE
    assert fresh(plan).state == "paused"


def test_resume_is_refused_once_the_lease_has_ended(fake: FakeProvider, worker: None) -> None:
    lease = a_plan_lease(fake)
    plan = a_scheduled_plan(lease)
    assert autopay_plans.pause(plan, user(Role.BILLING)).ok
    Lease.objects.filter(pk=lease.pk).update(collection_status=CollectionStatus.ENDED)
    refused = autopay_plans.resume(plan, user(Role.BILLING))
    assert (refused.ok, refused.message) == (False, autopay_plans.LEASE_NOT_ACTIVE)
    assert fresh(plan).state == "paused"


def test_stop_is_admin_only_final_and_cancels_the_pending_occurrence(
    fake: FakeProvider, worker: None
) -> None:
    plan = a_scheduled_plan(a_plan_lease(fake))
    assert autopay_plans.stop(plan, user(Role.BILLING)).message == autopay_plans.ADMIN_ONLY
    assert fresh(plan).state == "active"
    stopped = autopay_plans.stop(plan, user(Role.ADMIN))
    assert stopped.ok, stopped.message
    assert fresh(plan).state == "stopped"
    assert AuditLog.objects.filter(action=one_off_charges.CANCEL_ACTION).count() == 1
    assert plan_actions(plan)[-1] == autopay_plans.STOPPED_ACTION
    assert autopay_plans.resume(plan, user(Role.ADMIN)).message == autopay_plans.NOT_PAUSED
    assert autopay_plans.stop(plan, user(Role.ADMIN)).message == autopay_plans.NOT_LIVE
    assert autopay_plans.run_due(today() + timedelta(days=400)) == []


def test_a_paused_plan_can_be_stopped(fake: FakeProvider, worker: None) -> None:
    plan = a_scheduled_plan(a_plan_lease(fake))
    assert autopay_plans.pause(plan, user(Role.BILLING)).ok
    assert autopay_plans.stop(plan, user(Role.ADMIN)).ok
    assert fresh(plan).state == "stopped"


def test_change_end_rewrites_the_rule_and_finishes_a_met_plan_at_once() -> None:
    plan = a_plan(a_plan_lease(), end_count=6)
    admin = user(Role.ADMIN)
    assert (
        autopay_plans.change_end(
            plan, end_rule="count", end_count=6, end_on=None, actor=admin
        ).message
        == autopay_plans.UNCHANGED
    )
    changed = autopay_plans.change_end(
        plan, end_rule="never", end_count=None, end_on=None, actor=admin
    )
    assert (changed.ok, changed.message) == (True, autopay_plans.END_CHANGED)
    assert (fresh(plan).end_rule, fresh(plan).end_count) == ("never", None)
    autopay_plans.run_due(fresh(plan).next_on - timedelta(days=autopay_plans.LEAD_DAYS))
    assert fresh(plan).made_count == 2
    finished = autopay_plans.change_end(
        plan, end_rule="count", end_count=2, end_on=None, actor=admin
    )
    assert (finished.ok, finished.message) == (True, autopay_plans.FINISHED)
    assert fresh(plan).state == "finished"
    assert plan_actions(plan)[-2:] == [
        autopay_plans.END_CHANGED_ACTION,
        autopay_plans.FINISHED_ACTION,
    ]
    assert (
        autopay_plans.change_end(plan, end_rule="never", end_count=None, end_on=None, actor=admin)
    ).message == autopay_plans.NOT_LIVE


def test_change_end_to_a_date_before_the_next_finishes_the_plan() -> None:
    first = today() + timedelta(days=10)
    plan = a_plan(a_plan_lease(), first_on=first, end_rule="never")
    outcome = autopay_plans.change_end(
        plan, end_rule="date", end_count=None, end_on=first, actor=user(Role.ADMIN)
    )
    assert outcome.message == autopay_plans.FINISHED
    assert (fresh(plan).end_on, fresh(plan).state) == (first, "finished")


@pytest.mark.parametrize(
    ("rule", "count", "end_on", "message"),
    [
        ("weekly", None, None, autopay_plans.CHOOSE_END),
        ("count", 1, None, autopay_plans.END_COUNT),
        ("count", True, None, autopay_plans.END_COUNT),
        ("count", 1000, None, autopay_plans.END_COUNT),
        ("date", None, None, autopay_plans.END_DATE_REQUIRED),
        ("date", None, date(2020, 1, 1), autopay_plans.END_BEFORE_FIRST),
    ],
)
def test_change_end_refuses_a_bad_rule_and_writes_nothing(
    rule: str, count: Any, end_on: date | None, message: str
) -> None:
    plan = a_plan(a_plan_lease())
    before = plan_actions(plan)
    outcome = autopay_plans.change_end(
        plan, end_rule=rule, end_count=count, end_on=end_on, actor=user(Role.ADMIN)
    )
    assert (outcome.ok, outcome.message) == (False, message)
    assert plan_actions(plan) == before
    assert autopay_plans.change_end(
        plan, end_rule="never", end_count=None, end_on=None, actor=user(Role.BILLING)
    ).message == (autopay_plans.ADMIN_ONLY)


# --- Refusals at setup ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"line_count": 2}, autopay_plans.ONE_LINE),
        ({"rent_line": True}, autopay_plans.RENT),
        ({"item_name": "Rent October 2026, store 12"}, autopay_plans.RENT),
        ({"item_name": "   "}, autopay_plans.ITEM_REQUIRED),
        ({"item_name": "x" * 201}, autopay_plans.ITEM_TOO_LONG),
        ({"item_name": "Card 4111 1111 1111 1111"}, autopay_plans.TEXT_PAYMENT_DATA),
        ({"quantity": D("0")}, autopay_plans.QUANTITY),
        ({"quantity": D("0.00001")}, autopay_plans.QUANTITY),
        ({"unit_amount": D("10.001")}, autopay_plans.UNIT_AMOUNT),
        ({"unit_amount": None}, autopay_plans.UNIT_AMOUNT),
        ({"quantity": D("0.0001"), "unit_amount": D("0.01")}, autopay_plans.AMOUNT_ZERO),
        ({"quantity": D("2"), "unit_amount": D("999999.99")}, autopay_plans.AMOUNT_TOO_LARGE),
        ({"cadence": "MONTHLY"}, autopay_plans.CHOOSE_CADENCE),
        ({"cadence": "biweekly"}, autopay_plans.CHOOSE_CADENCE),
        ({"first_on": None}, autopay_plans.FIRST_REQUIRED),
        ({"first_on": "2026-12-01"}, autopay_plans.FIRST_REQUIRED),
        ({"end_rule": "after"}, autopay_plans.CHOOSE_END),
        ({"end_count": 1}, autopay_plans.END_COUNT),
        ({"end_count": 0}, autopay_plans.END_COUNT),
        ({"end_count": "3"}, autopay_plans.END_COUNT),
        ({"end_rule": "date", "end_count": None}, autopay_plans.END_DATE_REQUIRED),
    ],
)
def test_setup_refuses_and_writes_nothing(overrides: dict[str, Any], message: str) -> None:
    lease = a_plan_lease()
    outcome = setup(lease, **overrides)
    assert (outcome.ok, outcome.message) == (False, message)
    assert not AutopayPlan.objects.exists() and not OneOffCharge.objects.exists()
    assert not Job.objects.exists()


def test_the_first_date_must_be_after_today_and_within_365_days() -> None:
    lease = a_plan_lease()
    day = today()
    assert setup(lease, first_on=day).message == autopay_plans.FIRST_NOT_AFTER_TODAY
    assert setup(lease, first_on=day + timedelta(366)).message == autopay_plans.FIRST_TOO_FAR
    assert setup(lease, first_on=day + timedelta(365)).ok
    first = day + timedelta(days=10)
    assert (
        setup(
            lease,
            item_name="Other",
            first_on=first,
            end_rule="date",
            end_count=None,
            end_on=first - timedelta(days=1),
        ).message
        == autopay_plans.END_BEFORE_FIRST
    )


def test_a_plan_needs_an_active_lease_with_a_customer_and_a_bank_method() -> None:
    assert setup(None).message == autopay_plans.NO_LEASE
    ended = a_plan_lease()
    Lease.objects.filter(pk=ended.pk).update(ended_on=today() - timedelta(days=1))
    assert setup(Lease.objects.get(pk=ended.pk)).message == autopay_plans.LEASE_NOT_ACTIVE
    paused = a_plan_lease()
    Lease.objects.filter(pk=paused.pk).update(collection_status=CollectionStatus.PAUSED)
    assert setup(Lease.objects.get(pk=paused.pk)).message == autopay_plans.LEASE_NOT_ACTIVE
    no_customer = setup(a_plan_lease(customer=False))
    assert no_customer.message == autopay_plans.NO_PROVIDER_CUSTOMER
    assert setup(a_plan_lease(bank=False)).message == autopay_plans.NO_BANK_METHOD
    archived = a_plan_lease()
    Operator.objects.filter(leases=archived).update(archived_at=timezone.now())
    assert setup(Lease.objects.get(pk=archived.pk)).message == autopay_plans.OPERATOR_ARCHIVED
    assert not AutopayPlan.objects.exists()


def test_one_live_plan_per_lease_and_item(fake: FakeProvider, worker: None) -> None:
    lease = a_plan_lease(fake)
    plan = a_scheduled_plan(lease)
    again = setup(lease)
    assert (again.ok, again.message) == (False, autopay_plans.ALREADY_LIVE.format(item="Sign fee"))
    assert setup(lease, item_name="Sign rental").ok
    assert autopay_plans.stop(plan, user(Role.ADMIN)).ok
    assert setup(lease).ok


@override_settings(PROVIDER_MAX_TXN_AMOUNT=D("5000.00"))
def test_a_debit_above_the_provider_limit_is_refused() -> None:
    outcome = setup(a_plan_lease(), unit_amount=D("5000.01"), confirmed=True)
    assert not outcome.ok and outcome.field == "quantity"
    assert not AutopayPlan.objects.exists()


# --- Large debits -----------------------------------------------------------------------------


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("500.00"))
def test_a_large_debit_needs_the_confirmation_and_each_occurrence_is_audited_against_it() -> None:
    lease = a_plan_lease()
    refused = setup(lease, unit_amount=D("500.01"))
    assert not refused.ok and refused.field == "confirm"
    assert not AutopayPlan.objects.exists()
    # At the threshold itself: no confirmation needed, none stored.
    at = a_plan(lease, item_name="At threshold", unit_amount=D("500.00"))
    assert at.large_debit_confirmed_at is None
    assert not AuditLog.objects.filter(action=one_off_charges.LARGE_DEBIT_ACTION).exists()
    billing = user(Role.BILLING)
    plan = a_plan(lease, unit_amount=D("500.01"), confirmed=True, actor=billing)
    assert plan.large_debit_confirmed_at is not None
    autopay_plans.run_due(fresh(plan).next_on - timedelta(days=autopay_plans.LEAD_DAYS))
    rows = AuditLog.objects.filter(action=one_off_charges.LARGE_DEBIT_ACTION).order_by("id")
    assert [row.entity_id for row in rows] == [str(row.pk) for row in occurrences(plan)]
    for period, row in enumerate(rows, start=1):
        assert row.after["autopay_plan"] == str(plan.pk)
        assert row.after["period_no"] == period
        assert row.after["plan_confirmed_at"] == plan.large_debit_confirmed_at.isoformat()
        assert row.after["confirmed_by"] == billing.pk
    # The daily run's occurrence has no actor; the setup's has the person who confirmed.
    assert [row.actor_id for row in rows] == [billing.pk, None]


# --- Elsewhere: blockers, History, the run page -----------------------------------------------


def test_a_live_plan_blocks_the_lease_end_and_the_transfer_until_it_is_stopped(
    fake: FakeProvider, worker: None
) -> None:
    lease = a_plan_lease(fake)
    plan = a_scheduled_plan(lease)
    found = [item for item in lease_end.blockers(lease) if item.kind == lease_end.AUTOPAY_PLAN]
    assert [item.text for item in found] == [
        "Sign fee: a repeating debit is set up; stop it first."
    ]
    assert found[0].url == f"/stores/{lease.store_id}/?tab=schedules#tab-schedules"
    row = transfer.start(lease.store, None, today=today())
    assert lease_end.AUTOPAY_PLAN in {item.kind for item in transfer.blockers(row, today())}
    assert autopay_plans.pause(plan, user(Role.BILLING)).ok
    assert lease_end.AUTOPAY_PLAN in {item.kind for item in lease_end.blockers(lease)}
    assert autopay_plans.stop(plan, user(Role.ADMIN)).ok
    assert lease_end.AUTOPAY_PLAN not in {item.kind for item in lease_end.blockers(lease)}


def test_the_plans_rows_are_on_its_store_history() -> None:
    lease = a_plan_lease()
    plan = a_plan(lease)
    other = a_plan(a_plan_lease())
    rows = audit.rows_for_store(lease.store)
    plan_rows = [row for row in rows if row.entity_type == "autopay_plan"]
    assert [row.action for row in plan_rows] == [autopay_plans.CREATED_ACTION]
    assert other.pk != plan.pk
    assert len(plan_rows) == 1


def test_the_run_page_has_the_plan_debits_made_today() -> None:
    plan = a_plan(a_plan_lease())
    client, _ = client_for(Role.VIEWER)
    response = client.get("/run/")
    assert response.status_code == 200
    assert [row.pk for row in response.context["autopay_charges"]] == [
        row.pk for row in occurrences(plan)
    ]
