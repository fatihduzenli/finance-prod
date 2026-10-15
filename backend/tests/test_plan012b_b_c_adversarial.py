"""Adversarial cases for PLAN-012b-b Batch C (splitting rent through the release path), by
the QA owner (plan §6 C). The moved rent schedule skips exactly one month; a release job on a
rent charge of another lease, or with forged parameters, does nothing; a late debit of the old
schedule lands on the rent charge and puts the plan on the attention list, never on an
installment; a lease end after the move sees only the new schedule as live, and Stop autopay
clears it."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    InstallmentPlanStatus,
    Invoice,
    Lease,
    OneOffCategory,
    Operator,
    Payment,
    PaymentStatus,
    RentCharge,
)
from ledger.services import attention, installments, lease_end, leases, rent_run
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind
from tests import test_installments as installment_tests
from tests.job_helpers import run_queued_jobs
from tests.test_installments import CUSTOMER, NOW, TODAY, plan_for, rows_of
from tests.test_lease_services import creates, make_lease
from tests.test_one_off_charges import BANK, an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import deliver, provider_invoice, provider_txn, settle

pytestmark = pytest.mark.django_db

D = Decimal
OCTOBER = date(2026, 10, 1)
# The installment file's fixtures, re-exported so pytest finds them here.
fixed_now = installment_tests.fixed_now
fake = installment_tests.fake
worker = installment_tests.worker
ticking = installment_tests.ticking


def _autopay_lease(fake: FakeProvider, operator: Operator | None = None, bank: str = BANK) -> Lease:
    """A provider-ACH lease with rent autopay live at the fake, debit day 20 (after TODAY)."""
    lease = make_lease(operator=operator or an_operator(fake), debit_day=20)
    outcome = leases.enable_autopay(lease.pk, bank, actor=None, now=NOW)
    assert outcome.ok, outcome.message
    return lease


def _october(lease: Lease) -> RentCharge:
    charge, _ = rent_run.create_month(lease, OCTOBER, None)
    assert charge.due_on == date(2026, 10, 20) and charge.due_on >= TODAY
    return charge


def _moved_rules(fake: FakeProvider) -> list[str]:
    return [made.rule for made in creates(fake) if "FREQ=MONTHLY" in made.rule]


def _schedule_calls(fake: FakeProvider, name: str) -> list[tuple[object, ...]]:
    return [arguments for method, arguments in fake.calls if method == name]


# --- the move skips exactly one month -------------------------------------------------------------


def test_the_moved_schedule_starts_next_month_not_later(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    lease = _autopay_lease(fake)
    charge = _october(lease)
    assert installments.split_refusal(charge, TODAY) == ""
    plan = plan_for(charge)
    run_queued_jobs()
    rules = _moved_rules(fake)
    assert len(rules) == 2, rules  # the first enrolment, then the moved schedule
    assert "DTSTART=20261020" in rules[0] and "DTSTART=20261120" in rules[1]
    assert "20261220" not in rules[1]
    live = leases.live_schedules(lease)
    assert len(live) == 1
    moved = AuditLog.objects.filter(action="collection_schedule.moved_for_installments")
    assert moved.count() == 1 and str(plan.pk) in str(moved.get().after)
    # Three one-occurrence schedules for the installments, after the move.
    installment_ids = {str(row.pk) for row in rows_of(plan)}
    queued = Job.objects.filter(kind=JobKind.SCHEDULE_CREATE, entity_id__in=installment_ids)
    assert queued.count() == 3
    charge.refresh_from_db()
    assert charge.status != "voided" and charge.amount_due == charge.amount_due


# --- a release acts on its own lease --------------------------------------------------------------


def test_a_release_on_another_leases_rent_charge_or_with_forged_parameters_does_nothing(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments import jobs

    mine = _autopay_lease(fake)
    other = _autopay_lease(
        fake, operator=an_operator(fake, customer="cus-other-c", bank="pm-c"), bank="pm-c"
    )
    my_charge, other_charge = _october(mine), _october(other)
    other_ref = leases.schedule_ref(leases.live_schedules(other)[0])
    plan = plan_for(my_charge)
    # A forged release on the other lease's charge, which has no plan.
    jobs.enqueue(JobKind.SCHEDULE_DELETE, other_charge, {installments.RELEASE_PARAM: True}, None)
    # And my release carrying the other lease's schedule in its parameters.
    jobs.enqueue(
        JobKind.SCHEDULE_DELETE,
        my_charge,
        {installments.RELEASE_PARAM: True, "schedule": other_ref, "lease": str(other.pk)},
        None,
    )
    run_queued_jobs()
    assert leases.schedule_ref(leases.live_schedules(other)[0]) == other_ref
    deleted = _schedule_calls(fake, "delete_scheduled_invoice")
    assert len(deleted) == 1 and other_ref not in str(deleted)
    assert not other_charge.installment_plans.exists()
    other_charge.refresh_from_db()
    assert other_charge.status == "due"
    assert leases.live_schedules(mine) and len(_moved_rules(fake)) == 3  # two enrolments, one move
    assert plan.installments.count() == 3


# --- a late debit of the old schedule -------------------------------------------------------------


def test_a_late_debit_of_the_old_schedule_lands_on_the_rent_charge_and_flags_the_plan(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    lease = _autopay_lease(fake)
    charge = _october(lease)
    plan = plan_for(charge)
    run_queued_jobs()
    run_queued_jobs()
    # The old schedule had already generated an invoice for this charge; its debit settles late.
    assert lease.operator is not None
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-7777",
        operator=lease.operator,
        rent_charge=charge,
        issue_date=TODAY,
        due_date=charge.due_on,
        subtotal=charge.amount_due,
        total=charge.amount_due,
    )
    ExternalRef.objects.create(
        entity_type="invoice",
        entity_id=invoice.pk,
        provider="stax_pay",
        external_kind="invoice",
        external_id="inv-old-schedule",
    )
    provider_invoice(fake, "inv-old-schedule", total=charge.amount_due, customer=CUSTOMER)
    provider_txn(
        fake,
        "txn-late-debit",
        total=charge.amount_due,
        invoice_external_id="inv-old-schedule",
        customer_external_id=CUSTOMER,
    )
    deliver("create_transaction", id="txn-late-debit")
    settle(fake, "txn-late-debit")
    deliver("update_transaction_settled", id="txn-late-debit")
    landed = Payment.objects.filter(status=PaymentStatus.SETTLED, is_refund=False)
    assert landed.count() == 1 and landed.get().rent_charge_id == charge.pk
    assert not Payment.objects.filter(one_off_charge__plan=plan).exists()
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ATTENTION
    items = [i for i in attention.attention(TODAY).items if i.kind == attention.SPLIT_CHARGE_PAID]
    assert len(items) == 1 and items[0].url == f"/plans/{plan.pk}/"
    charge.refresh_from_db()
    assert charge.status == "paid"
    assert all(row.status == "owed" for row in rows_of(plan))


# --- the lease end after the move ---------------------------------------------------------------


def test_a_lease_end_after_the_move_sees_only_the_new_schedule_and_stop_autopay_clears_it(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    lease = _autopay_lease(fake)
    charge = _october(lease)
    old_ref = leases.schedule_ref(leases.live_schedules(lease)[0])
    plan_for(charge)
    run_queued_jobs()
    autopay = [b for b in lease_end.blockers(lease, TODAY) if b.kind == lease_end.AUTOPAY]
    assert len(autopay) == 1 and "active" in autopay[0].text
    new_ref = leases.schedule_ref(leases.live_schedules(lease)[0])
    assert new_ref != old_ref
    assert old_ref not in str(lease_end.blockers(lease, TODAY))
    admin = client_for(Role.ADMIN)[1]
    assert leases.stop_autopay(lease.pk, actor=admin).ok
    run_queued_jobs()
    assert not [b for b in lease_end.blockers(lease, TODAY) if b.kind == lease_end.AUTOPAY]
    deleted = _schedule_calls(fake, "delete_scheduled_invoice")
    assert {str(a[0]) for a in deleted} == {old_ref, new_ref}
    # The installments' schedules are not the lease's rent schedules and stay as they are.
    assert (
        Job.objects.filter(
            kind=JobKind.SCHEDULE_CREATE, entity_id__in=[str(u) for u in installments_ids(charge)]
        ).count()
        == 3
    )


def installments_ids(charge: RentCharge) -> list[uuid.UUID]:
    plan = charge.installment_plans.get()
    return [row.pk for row in rows_of(plan) if row.category == OneOffCategory.INSTALLMENT]
