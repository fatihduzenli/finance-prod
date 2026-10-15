"""Installment plans (docs/qa/PLAN-012e-lease-end-transfer-installments.md Batch C: D9 to
D12; §6 "Installments").

The arithmetic, the split rules and the preview's edits, the provider objects of each
collection (the worker run through the queue against a
:class:`~payments.fake.FakeProvider`), the parent left out of what is owed while the plan
is live, a payment on one installment, completion, a returned installment with the NSF
rule and the attention item, reschedule, cancel, the P&L, the ledger and the grid
counting the money once, and the pages with their role gates. Nothing reaches a
network.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    CollectionSchedule,
    InstallmentCadence,
    InstallmentCollection,
    InstallmentPlan,
    InstallmentPlanStatus,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceLineItem,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentChannel,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import (
    attention,
    grid,
    installments,
    nsf,
    one_off_charges,
    payments_ledger,
    pnl,
    receivables,
    rent_run,
    returns,
)
from ledger.services.installments import Line
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind
from tests.job_helpers import run_queued_jobs
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import ach_return, deliver, provider_txn, settle

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
FIRST = date(2026, 10, 20)
CUSTOMER = "cus-one-off-1"
OCTOBER = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


# --- Builders ----------------------------------------------------------------------------


def a_user(role: Role) -> Any:
    return client_for(role)[1]


def a_one_off(
    operator: Operator,
    price: str = "1000.00",
    collection: str = CollectionMethod.MANUAL,
    *,
    lease: Lease | None = None,
    due: date = date(2026, 9, 1),
    category: str = OneOffCategory.ARREARS,
) -> OneOffCharge:
    """A one-off charge with nothing at the payment provider (its invoice never made)."""
    return OneOffCharge.objects.create(
        operator=operator,
        lease=lease,
        category=category,
        collection=collection,
        description="Arrears July to September 2026",
        price=D(price),
        due_date=due,
        status=OneOffChargeStatus.OWED,
    )


def a_rent(
    operator: Operator,
    amount: str = "1000.00",
    *,
    channel: str = PaymentChannel.WIRE,
    month: date = OCTOBER,
    lease: Lease | None = None,
) -> RentCharge:
    held = lease or make_lease(operator=operator, payment_channel=channel)
    return RentCharge.objects.create(
        lease=held,
        period_month=month,
        seq=1,
        due_on=month.replace(day=15),
        amount_due=D(amount),
        status=RentChargeStatus.DUE,
    )


def plan_for(
    parent: RentCharge | OneOffCharge,
    count: int = 3,
    first: date = FIRST,
    cadence: str = InstallmentCadence.MONTHLY,
    *,
    lines: list[Line] | None = None,
    actor: Any = None,
) -> InstallmentPlan:
    proposed, refused = installments.preview(parent, count, first, cadence)
    assert refused is None, refused
    outcome = installments.create_plan(
        parent, count, first, cadence, lines or proposed, actor or a_user(Role.BILLING)
    )
    assert outcome.ok and outcome.plan is not None, outcome.message
    return outcome.plan


def rows_of(plan: InstallmentPlan) -> list[OneOffCharge]:
    return installments.installments_of(plan)


def pay(charge: OneOffCharge, amount: str, on: date = TODAY) -> rent_run.PaymentOutcome:
    return one_off_charges.record_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D(amount),
        paid_on=on,
        note="Paid by Zelle",
        actor=a_user(Role.BILLING),
    )


def calls(fake: FakeProvider, name: str) -> list[tuple[object, ...]]:
    return [arguments for method, arguments in fake.calls if method == name]


def provider_ref(invoice: Invoice) -> str:
    return str(
        ExternalRef.objects.get(
            entity_type="invoice", entity_id=invoice.pk, external_kind="invoice"
        ).external_id
    )


def nothing_written() -> bool:
    return (
        not InstallmentPlan.objects.exists()
        and not OneOffCharge.objects.filter(category=OneOffCategory.INSTALLMENT).exists()
    )


# --- The arithmetic (D9) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "count", "shares"),
    [
        ("1000.00", 3, ["333.33", "333.33", "333.34"]),
        ("100.00", 7, ["14.28"] * 6 + ["14.32"]),
        ("999.99", 12, ["83.33"] * 11 + ["83.36"]),
        # Rounded down to the cent, the remainder on the last: 0.02 and 0.03.
        ("0.05", 2, ["0.02", "0.03"]),
    ],
)
def test_split_gives_equal_shares_with_the_remainder_on_the_last(
    total: str, count: int, shares: list[str]
) -> None:
    found = installments.split(D(total), count)
    assert found == [D(share) for share in shares]
    assert sum(found, D("0.00")) == D(total)


@pytest.mark.parametrize(("total", "count"), [("0.01", 2), ("0.05", 12), ("0.11", 12)])
def test_a_share_below_one_cent_is_refused(total: str, count: int) -> None:
    with pytest.raises(ValueError, match="at least one cent"):
        installments.split(D(total), count)


@pytest.mark.parametrize("count", [1, 13, 0, -2])
def test_a_count_outside_two_to_twelve_is_refused(count: int, fake: FakeProvider) -> None:
    with pytest.raises(ValueError, match="between 2 and 12"):
        installments.split(D("1000.00"), count)
    parent = a_one_off(an_operator(fake))
    outcome = installments.create_plan(
        parent,
        count,
        FIRST,
        InstallmentCadence.MONTHLY,
        [Line(FIRST, D("1000.00"))],
        a_user(Role.BILLING),
    )
    assert not outcome.ok and outcome.message == installments.COUNT_RANGE
    assert nothing_written()


@pytest.mark.parametrize(
    ("first", "cadence", "expected"),
    [
        (
            date(2026, 10, 6),
            InstallmentCadence.WEEKLY,
            [date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20)],
        ),
        (
            date(2026, 12, 25),
            InstallmentCadence.BIWEEKLY,
            [date(2026, 12, 25), date(2027, 1, 8), date(2027, 1, 22)],
        ),
        (
            date(2026, 11, 15),
            InstallmentCadence.MONTHLY,
            [date(2026, 11, 15), date(2026, 12, 15), date(2027, 1, 15)],
        ),
        # The 31st, monthly: clamped to each month's last day, back to the 31st after.
        (
            date(2027, 1, 31),
            InstallmentCadence.MONTHLY,
            [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)],
        ),
        # A leap February.
        (
            date(2027, 12, 31),
            InstallmentCadence.MONTHLY,
            [date(2027, 12, 31), date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31)],
        ),
        (
            date(2028, 2, 29),
            InstallmentCadence.MONTHLY,
            [date(2028, 2, 29), date(2028, 3, 29), date(2028, 4, 29)],
        ),
    ],
)
def test_the_dates_of_each_cadence(first: date, cadence: str, expected: list[date]) -> None:
    assert installments.schedule_dates(first, cadence, len(expected)) == expected


def test_an_unknown_cadence_is_refused(fake: FakeProvider) -> None:
    with pytest.raises(ValueError, match="weekly"):
        installments.schedule_dates(FIRST, "daily", 3)
    parent = a_one_off(an_operator(fake))
    lines, refused = installments.preview(parent, 3, FIRST, "daily")
    assert lines == [] and refused is not None and refused.field == "cadence"


# --- The preview and the edits (D10) ----------------------------------------------------------


def test_a_first_date_in_the_past_is_refused_and_today_is_accepted(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    _, refused = installments.preview(parent, 3, TODAY - timedelta(days=1), "monthly")
    assert refused is not None and refused.message == installments.FIRST_IN_PAST
    past = [Line(TODAY - timedelta(days=1), D("500.00")), Line(FIRST, D("500.00"))]
    outcome = installments.create_plan(
        parent, 2, TODAY - timedelta(days=1), "monthly", past, a_user(Role.BILLING)
    )
    assert not outcome.ok and outcome.message == installments.FIRST_IN_PAST
    assert nothing_written()
    lines, refused = installments.preview(parent, 2, TODAY, "weekly")
    assert refused is None and lines[0].due == TODAY


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        # The sum breaks.
        (
            [("2026-10-20", "400.00"), ("2026-11-20", "300.00"), ("2026-12-20", "299.99")],
            "add up to the balance",
        ),
        # Dates out of order, and two on one day.
        (
            [("2026-11-20", "400.00"), ("2026-10-20", "300.00"), ("2026-12-20", "300.00")],
            "in order",
        ),
        (
            [("2026-10-20", "400.00"), ("2026-10-20", "300.00"), ("2026-12-20", "300.00")],
            "in order",
        ),
        # An installment of nothing, or with a fraction of a cent.
        (
            [("2026-10-20", "0.00"), ("2026-11-20", "500.00"), ("2026-12-20", "500.00")],
            "at least $0.01",
        ),
        (
            [("2026-10-20", "333.333"), ("2026-11-20", "333.333"), ("2026-12-20", "333.334")],
            "at least $0.01",
        ),
        # A line missing.
        ([("2026-10-20", "500.00"), ("2026-11-20", "500.00")], "every installment"),
    ],
)
def test_edits_that_break_the_sum_or_the_order_are_refused(
    fake: FakeProvider, lines: list[tuple[str, str]], message: str
) -> None:
    parent = a_one_off(an_operator(fake))
    edited = [Line(date.fromisoformat(due), D(amount)) for due, amount in lines]
    outcome = installments.create_plan(parent, 3, FIRST, "monthly", edited, a_user(Role.BILLING))
    assert not outcome.ok and message in outcome.message
    assert nothing_written()


@pytest.mark.parametrize("amount", ["1E+400", "1000000.00"])
def test_an_amount_too_large_to_hold_is_refused(fake: FakeProvider, amount: str) -> None:
    parent = a_one_off(an_operator(fake))
    edited = [
        Line(date(2026, 10, 20), D(amount)),
        Line(date(2026, 11, 20), D("500.00")),
        Line(date(2026, 12, 20), D("500.00")),
    ]
    refused = installments.lines_refusal(edited, parent.balance, 3, TODAY)
    assert refused == installments.Outcome(False, installments.LINE_AMOUNT, "lines")
    outcome = installments.create_plan(parent, 3, FIRST, "monthly", edited, a_user(Role.BILLING))
    assert not outcome.ok and outcome.message == installments.LINE_AMOUNT
    assert nothing_written()


def test_an_edit_that_keeps_the_sum_and_the_order_is_accepted(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    edited = [
        Line(date(2026, 10, 20), D("400.00")),
        Line(date(2026, 11, 2), D("350.00")),
        Line(date(2027, 1, 5), D("250.00")),
    ]
    plan = plan_for(parent, lines=edited)
    assert [(row.due_date, row.price) for row in rows_of(plan)] == [
        (line.due, line.amount) for line in edited
    ]
    assert plan.first_due == date(2026, 10, 20)


def test_a_1000_charge_split_in_3_monthly(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    parent = a_one_off(operator)
    plan = plan_for(parent)
    rows = rows_of(plan)
    assert [(row.installment_no, row.due_date, row.price) for row in rows] == [
        (1, date(2026, 10, 20), D("333.33")),
        (2, date(2026, 11, 20), D("333.33")),
        (3, date(2026, 12, 20), D("333.34")),
    ]
    assert {row.category for row in rows} == {OneOffCategory.INSTALLMENT}
    assert [row.description for row in rows] == [
        f"Installment {n} of 3: Arrears July to September 2026" for n in (1, 2, 3)
    ]
    assert {(row.operator_id, row.collection) for row in rows} == {
        (operator.pk, CollectionMethod.MANUAL)
    }
    assert (plan.status, plan.collection, plan.count) == ("active", "manual", 3)
    parent.refresh_from_db()
    # The parent keeps its price and is not touched.
    assert (parent.price, parent.amount_paid, parent.status) == (
        D("1000.00"),
        D("0.00"),
        OneOffChargeStatus.OWED,
    )
    assert AuditLog.objects.filter(
        action="installment_plan.created", entity_id=str(plan.pk)
    ).exists()
    assert AuditLog.objects.filter(
        action="one_off_charge.split_into_installments", entity_id=str(parent.pk)
    ).exists()
    assert (
        AuditLog.objects.filter(
            action="one_off_charge.created", entity_id__in=[str(row.pk) for row in rows]
        ).count()
        == 3
    )


def test_a_partly_paid_charge_splits_its_balance(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    assert pay(parent, "100.00").ok
    parent.refresh_from_db()
    plan = plan_for(parent)
    assert [row.price for row in rows_of(plan)] == [D("300.00")] * 3


# --- Who may be split (D9, D10) ---------------------------------------------------------------


def test_a_charge_with_a_plan_is_not_split_again(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    plan_for(parent)
    assert installments.split_refusal(parent) == installments.HAS_PLAN
    lines, _ = installments.preview(parent, 2, FIRST, "monthly")
    outcome = installments.create_plan(parent, 2, FIRST, "monthly", lines, a_user(Role.BILLING))
    assert not outcome.ok and outcome.message == installments.HAS_PLAN
    assert InstallmentPlan.objects.count() == 1


def test_the_database_keeps_one_plan_per_charge_that_is_not_cancelled(
    fake: FakeProvider,
) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    with pytest.raises(IntegrityError), transaction.atomic():
        InstallmentPlan.objects.create(
            one_off_charge=parent,
            operator=plan.operator,
            count=2,
            first_due=FIRST,
            cadence="monthly",
            collection="manual",
            confirmed_at=NOW,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": OneOffChargeStatus.WAIVED}, installments.CLOSED),
        ({"category": OneOffCategory.REDEBIT}, installments.NOT_SPLITTABLE),
        ({"balance": D("0.00")}, installments.NOTHING_DUE),
    ],
)
def test_a_closed_or_special_charge_is_not_split(
    fake: FakeProvider, change: dict[str, Any], message: str
) -> None:
    parent = a_one_off(an_operator(fake))
    for name, value in change.items():
        setattr(parent, name, value)
    assert installments.split_refusal(parent) == message


def test_a_charge_with_a_live_provider_invoice_or_schedule_is_split_with_a_release(
    fake: FakeProvider, worker: None
) -> None:
    """PLAN-012b-a D2: the split is offered while the request waits for nothing, and it
    names what the confirm releases; before PLAN-012b-a this was refused."""
    operator = an_operator(fake)
    emailed = a_one_off(operator, collection=CollectionMethod.EMAIL)
    Invoice.objects.create(
        invoice_number="INV-2026-9001",
        operator=operator,
        one_off_charge=emailed,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("1000.00"),
        total=D("1000.00"),
    )
    assert installments.split_refusal(emailed) == ""
    from payments import jobs

    jobs.enqueue(JobKind.INVOICE_CREATE, emailed, None, None)
    assert installments.split_refusal(emailed) == installments.REQUEST_WAITING
    run_queued_jobs()
    assert installments.split_refusal(emailed) == ""
    assert installments.release_notice(emailed) == (
        "Invoice INV-2026-9001 at the payment provider will be deleted (the charge stays open "
        "here) and each installment is collected on its own."
    )
    autopay = a_one_off(operator, collection=CollectionMethod.AUTOPAY_DUE, due=FIRST)
    jobs.enqueue(JobKind.SCHEDULE_CREATE, autopay, None, None)
    run_queued_jobs()
    assert installments.split_refusal(autopay) == ""
    assert installments.release_notice(autopay) == installments.RELEASE_NOTICE_SCHEDULE
    nothing_live = a_one_off(operator, collection=CollectionMethod.EMAIL)
    assert installments.release_notice(nothing_live) == ""


def test_a_pending_payment_or_a_debited_rent_month_is_not_split(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    rent = a_rent(operator)
    Payment.objects.create(
        rent_charge=rent,
        kind="rent",
        method=PaymentMethod.ACH,
        amount=D("1000.00"),
        status=PaymentStatus.PENDING,
        initiated_on=TODAY,
    )
    assert installments.split_refusal(rent) == installments.PENDING
    rent.status = RentChargeStatus.PENDING
    assert installments.split_refusal(rent) == installments.PENDING


def test_rent_that_autopay_will_debit_is_split_by_moving_autopay(fake: FakeProvider) -> None:
    """PLAN-012b-b D13: the refusal is gone; the confirm moves the rent autopay."""
    operator = an_operator(fake)
    rent = a_rent(operator, channel=PaymentChannel.PROVIDER_ACH)
    row = CollectionSchedule.objects.create(
        lease=rent.lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=15,
        amount=D("1000.00"),
        state=ScheduleState.ACTIVE,
    )
    ExternalRef.objects.create(
        entity_type="collection_schedule",
        entity_id=row.pk,
        provider="stax_pay",
        external_kind="schedule",
        external_id="fake-schedule-rent",
    )
    assert installments.split_refusal(rent) == ""
    assert installments.release_kind(rent) == JobKind.SCHEDULE_DELETE
    assert installments.release_notice(rent) == installments.RELEASE_NOTICE_AUTOPAY
    # Past its due date the schedule no longer debits it: nothing to move.
    assert installments.split_refusal(rent, date(2026, 10, 16)) == ""
    assert installments.release_kind(rent, date(2026, 10, 16)) == ""


def test_a_deposit_is_not_split(fake: FakeProvider) -> None:
    from ledger.models import FeeType

    operator = an_operator(fake)
    lease = make_lease(operator=operator)
    deposit = FeeType.objects.create(code="deposit-x", name="Security deposit", refundable=True)
    charge = OneOffCharge.objects.create(
        operator=operator,
        lease=lease,
        fee_type=deposit,
        category=OneOffCategory.SETUP,
        collection=CollectionMethod.NONE,
        description="Security deposit",
        price=D("1000.00"),
        due_date=TODAY,
    )
    assert installments.split_refusal(charge) == installments.DEPOSIT


def test_the_collection_needs_the_provider_customer_and_the_bank_method(
    fake: FakeProvider,
) -> None:
    no_customer = an_operator(customer=None)
    parent = a_one_off(no_customer, collection=CollectionMethod.EMAIL)
    assert "no payment provider customer" in installments.split_refusal(parent)
    no_bank = an_operator(fake, bank=None)
    autopay = a_one_off(no_bank, collection=CollectionMethod.AUTOPAY_DUE)
    assert installments.split_refusal(autopay) == installments.NO_BANK_METHOD
    manual = a_one_off(no_customer)
    assert installments.split_refusal(manual) == ""


@pytest.mark.parametrize(
    ("collection", "expected"),
    [
        (CollectionMethod.AUTOPAY_DUE, InstallmentCollection.AUTOPAY_DUE),
        (CollectionMethod.EMAIL, InstallmentCollection.EMAIL),
        (CollectionMethod.CHARGE_NOW, InstallmentCollection.EMAIL),
        (CollectionMethod.MANUAL, InstallmentCollection.MANUAL),
    ],
)
def test_the_collection_is_the_parents(fake: FakeProvider, collection: str, expected: str) -> None:
    parent = a_one_off(an_operator(fake), collection=collection)
    assert installments.collection_of(parent) == expected


def test_rent_is_collected_by_autopay_on_an_ach_lease_and_by_hand_otherwise(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    ach = a_rent(operator, channel=PaymentChannel.PROVIDER_ACH)
    wire = a_rent(an_operator(customer=None), channel=PaymentChannel.WIRE)
    assert installments.collection_of(ach) == InstallmentCollection.AUTOPAY_DUE
    assert installments.collection_of(wire) == InstallmentCollection.MANUAL


# --- The provider objects of each collection (D9) ------------------------------------------------


def test_email_installments_get_one_invoice_each(fake: FakeProvider, worker: None) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.EMAIL)
    plan = plan_for(parent)
    rows = rows_of(plan)
    invoices = [Invoice.objects.get(one_off_charge=row) for row in rows]
    assert [invoice.due_date for invoice in invoices] == [row.due_date for row in rows]
    assert [invoice.total for invoice in invoices] == [row.price for row in rows]
    jobs = Job.objects.filter(entity_id__in=[row.pk for row in rows])
    assert sorted(job.kind for job in jobs) == [JobKind.INVOICE_CREATE] * 3
    run_queued_jobs()
    assert len(calls(fake, "create_invoice")) == 3
    assert len(calls(fake, "send_invoice_email")) == 3
    assert all(provider_ref(invoice) for invoice in invoices)


def test_autopay_installments_get_one_one_occurrence_schedule_each(
    fake: FakeProvider, worker: None
) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.AUTOPAY_DUE, due=FIRST)
    plan = plan_for(parent)
    rows = rows_of(plan)
    assert not Invoice.objects.filter(one_off_charge__in=rows).exists()
    run_queued_jobs()
    created = calls(fake, "create_scheduled_invoice")
    assert len(created) == 3
    rules = [str(arguments[0].rule) for arguments in created]  # type: ignore[attr-defined]
    # The worker runs the three jobs in any order.
    assert sorted(rule[:16] for rule in rules) == [f"DTSTART={row.due_date:%Y%m%d}" for row in rows]
    assert all("COUNT=1" in rule for rule in rules)
    assert all(one_off_charges.provider_schedule(row) for row in rows)


def test_manual_installments_make_nothing_at_the_provider(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    rows = rows_of(plan)
    assert not Job.objects.exists()
    assert not Invoice.objects.filter(one_off_charge__in=rows).exists()
    run_queued_jobs()
    assert fake.calls == []


# --- While the plan is live (D9, D12) ----------------------------------------------------------


def test_the_parent_is_left_out_of_open_balances_aging_arrears_and_attention(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    parent = a_one_off(operator, due=date(2026, 8, 1))
    before = receivables.open_balances(TODAY, operator=operator.pk)
    assert [row.charge_id for row in before] == [parent.pk]
    assert receivables.arrears(TODAY).count == 1
    overdue = [item for item in attention.attention(TODAY).items if item.kind == "overdue"]
    assert len(overdue) == 1
    plan = plan_for(parent)
    rows = receivables.open_balances(TODAY, operator=operator.pk)
    assert [(row.charge_id, row.due_on, row.balance) for row in rows] == [
        (row.pk, row.due_date, row.price) for row in rows_of(plan)
    ]
    assert rows[0].description == "Installment: Installment 1 of 3: Arrears July to September 2026"
    assert receivables.aging(TODAY).totals.total == D("1000.00")
    assert receivables.dashboard(TODAY).outstanding == D("1000.00")
    # Nothing is overdue now: the installments fall due later.
    assert receivables.arrears(TODAY).count == 0
    assert not [item for item in attention.attention(TODAY).items if item.kind == "overdue"]


def test_a_payment_on_installment_2_pays_that_installment_only(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    first, second, third = rows_of(plan)
    assert pay(second, "333.33").ok
    first.refresh_from_db()
    second.refresh_from_db()
    third.refresh_from_db()
    parent.refresh_from_db()
    assert [row.status for row in (first, second, third)] == ["owed", "paid", "owed"]
    assert (parent.amount_paid, parent.status) == (D("0.00"), OneOffChargeStatus.OWED)
    assert installments.progress(plan).text == (
        "Installment plan: 1 of 3 paid, $333.33 of $1,000.00"
    )
    # An installment takes no more than its own balance.
    over = pay(first, "333.34")
    assert not over.ok and over.message == rent_run.PAY_OVER_BALANCE


def test_a_manual_payment_on_the_parent_is_refused_while_the_plan_is_live(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    one_off = a_one_off(operator)
    rent = a_rent(an_operator(customer=None))
    plan_for(one_off)
    plan_for(rent)
    refused = pay(one_off, "10.00")
    assert not refused.ok and refused.message == rent_run.PAY_ON_PLAN
    refused = rent_run.record_manual_payment(
        rent.pk,
        method=PaymentMethod.WIRE,
        amount=D("10.00"),
        paid_on=TODAY,
        note="Wire",
        actor=a_user(Role.BILLING),
    )
    assert not refused.ok and refused.message == rent_run.PAY_ON_PLAN
    assert not Payment.objects.exists()


def test_all_paid_completes_the_plan_and_pays_the_parent_without_a_payment_row(
    fake: FakeProvider,
) -> None:
    parent = a_rent(an_operator(fake))
    plan = plan_for(parent)
    for row, amount in zip(rows_of(plan), ("333.33", "333.33", "333.34"), strict=True):
        assert pay(row, amount).ok
    plan.refresh_from_db()
    parent.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.COMPLETED
    assert (parent.amount_paid, parent.status) == (D("1000.00"), RentChargeStatus.PAID)
    assert not Payment.objects.filter(rent_charge=parent).exists()
    covered = AuditLog.objects.get(action="rent_charge.covered_by_plan", entity_id=str(parent.pk))
    assert covered.after["status"] == "paid" and covered.after["plan"] == str(plan.pk)
    assert covered.after["plan_status"] == "completed"
    assert receivables.open_balances(TODAY) == []


# --- A returned installment (D11) -----------------------------------------------------


def _emailed_plan(fake: FakeProvider) -> tuple[OneOffCharge, InstallmentPlan]:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.EMAIL)
    plan = plan_for(parent)
    run_queued_jobs()
    return parent, plan


def _settle_installment(fake: FakeProvider, row: OneOffCharge, txn: str) -> None:
    invoice = Invoice.objects.get(one_off_charge=row)
    provider_txn(
        fake,
        txn,
        total=row.price,
        invoice_external_id=provider_ref(invoice),
        customer_external_id=CUSTOMER,
    )
    deliver("create_transaction", id=txn)
    settle(fake, txn)
    assert deliver("update_transaction_settled", id=txn) == "processed"


def test_a_returned_installment_raises_the_fee_by_the_rule_and_the_attention_item(
    fake: FakeProvider, worker: None
) -> None:
    parent, plan = _emailed_plan(fake)
    first = rows_of(plan)[0]
    _settle_installment(fake, first, "txn-inst-1")
    first.refresh_from_db()
    assert first.status == OneOffChargeStatus.PAID
    ach_return(fake, "txn-inst-1")
    deliver("update_transaction_settled", id="txn-inst-1")
    first.refresh_from_db()
    plan.refresh_from_db()
    assert first.status == OneOffChargeStatus.OWED
    assert plan.status == InstallmentPlanStatus.ATTENTION
    items = [
        item for item in attention.attention(TODAY).items if item.kind == "installment_returned"
    ]
    assert len(items) == 1
    assert items[0].text == (
        "Installment returned: No store, Arrears July to September 2026, plan 0 of 3 paid"
    )
    assert items[0].url == f"/plans/{plan.pk}/"
    assert AuditLog.objects.filter(
        action="installment_plan.status_changed", entity_id=str(plan.pk)
    ).exists()
    # The NSF rule applies to the returned installment payment as to any other.
    [returned] = Payment.objects.filter(status=PaymentStatus.RETURNED)
    outcome = nsf.reschedule(
        returned.pk,
        redebit_date=date(2026, 10, 27),
        waive_fee=False,
        note="",
        actor=a_user(Role.BILLING),
        confirmed=False,
    )
    assert outcome.ok, outcome.message
    assert outcome.fee is not None and outcome.fee.price == D("100.00")
    assert outcome.redebit is not None and outcome.redebit.price == first.price
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ACTIVE
    assert not [
        item for item in attention.attention(TODAY).items if item.kind == "installment_returned"
    ]


def test_a_return_resolved_with_a_note_clears_the_attention(
    fake: FakeProvider, worker: None
) -> None:
    _, plan = _emailed_plan(fake)
    first = rows_of(plan)[0]
    _settle_installment(fake, first, "txn-inst-2")
    ach_return(fake, "txn-inst-2")
    deliver("update_transaction_settled", id="txn-inst-2")
    [returned] = Payment.objects.filter(status=PaymentStatus.RETURNED)
    assert returns.resolve_return(returned.pk, note="Paid by wire instead", actor=None).ok
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ACTIVE


# --- Reschedule (D11) ----------------------------------------------------------------------------


def test_reschedule_moves_only_the_unpaid_installments_and_the_schedules(
    fake: FakeProvider, worker: None
) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.AUTOPAY_DUE, due=FIRST)
    plan = plan_for(parent)
    run_queued_jobs()
    first, second, third = rows_of(plan)
    assert pay(first, "333.33").ok
    outcome = installments.reschedule(
        plan, date(2026, 12, 1), InstallmentCadence.WEEKLY, a_user(Role.BILLING)
    )
    assert outcome.ok, outcome.message
    for row in (first, second, third):
        row.refresh_from_db()
    assert [row.due_date for row in (first, second, third)] == [
        FIRST,
        date(2026, 12, 1),
        date(2026, 12, 8),
    ]
    run_queued_jobs()
    moved = [
        (str(ref), str(update.rule)[:16])  # type: ignore[attr-defined]
        for ref, update in calls(fake, "update_scheduled_invoice")
    ]
    assert sorted(moved) == sorted(
        [
            (one_off_charges.provider_schedule(second), "DTSTART=20261201"),
            (one_off_charges.provider_schedule(third), "DTSTART=20261208"),
        ]
    )
    assert len(calls(fake, "create_scheduled_invoice")) == 3
    plan.refresh_from_db()
    assert plan.cadence == InstallmentCadence.WEEKLY
    assert AuditLog.objects.filter(
        action="installment_plan.rescheduled", entity_id=str(plan.pk)
    ).exists()
    assert AuditLog.objects.filter(action="one_off_charge.schedule_moved").count() == 2


def test_reschedule_moves_an_emailed_installments_invoice_due_date(
    fake: FakeProvider, worker: None
) -> None:
    _, plan = _emailed_plan(fake)
    outcome = installments.reschedule(
        plan, date(2027, 1, 10), InstallmentCadence.MONTHLY, a_user(Role.BILLING)
    )
    assert outcome.ok, outcome.message
    run_queued_jobs()
    moved = calls(fake, "update_invoice")
    assert sorted(update.due_on for _, update in moved) == [  # type: ignore[attr-defined]
        date(2027, 1, 10),
        date(2027, 2, 10),
        date(2027, 3, 10),
    ]
    assert [invoice.due_date for invoice in Invoice.objects.order_by("due_date")] == [
        date(2027, 1, 10),
        date(2027, 2, 10),
        date(2027, 3, 10),
    ]


def test_reschedule_is_refused_while_a_provider_request_waits_or_in_the_past(
    fake: FakeProvider,
) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.EMAIL)
    plan = plan_for(parent)
    busy = installments.reschedule(plan, FIRST, "monthly", a_user(Role.BILLING))
    assert not busy.ok and busy.message == installments.SHIFT_BUSY
    past = installments.reschedule(plan, TODAY - timedelta(days=1), "monthly", None)
    assert not past.ok and past.message == installments.FIRST_IN_PAST


# --- Cancel (D11) --------------------------------------------------------------------------------


def test_cancel_voids_the_rest_at_the_provider_and_returns_the_remainder_to_the_parent(
    fake: FakeProvider, worker: None
) -> None:
    parent, plan = _emailed_plan(fake)
    first, second, third = rows_of(plan)
    assert pay(first, "333.33").ok
    run_queued_jobs()
    admin = a_user(Role.ADMIN)
    outcome = installments.cancel(plan, "The operator moves out.", admin)
    assert outcome.ok, outcome.message
    assert outcome.message.endswith("open again for $666.67.")
    plan.refresh_from_db()
    parent.refresh_from_db()
    assert (plan.status, plan.note) == (InstallmentPlanStatus.CANCELLED, "The operator moves out.")
    assert (parent.amount_paid, parent.balance, parent.status) == (
        D("333.33"),
        D("666.67"),
        OneOffChargeStatus.PARTIAL,
    )
    # The parent is owed again, the cancelled installments are not, even before the worker.
    rows = receivables.open_balances(TODAY)
    assert [(row.charge_id, row.balance) for row in rows] == [(parent.pk, D("666.67"))]
    assert AuditLog.objects.filter(
        action="one_off_charge.covered_by_plan", entity_id=str(parent.pk)
    ).exists()
    assert AuditLog.objects.filter(action="installment_plan.cancelled").exists()
    run_queued_jobs()
    assert len(calls(fake, "delete_invoice")) == 2
    first.refresh_from_db()
    second.refresh_from_db()
    third.refresh_from_db()
    assert [row.status for row in (first, second, third)] == ["paid", "voided", "voided"]
    assert receivables.open_balances(TODAY) == [rows[0]]
    # A plan may be made again from what is left.
    again = plan_for(parent, count=2)
    assert [row.price for row in rows_of(again)] == [D("333.33"), D("333.34")]


def test_cancel_needs_an_admin_and_a_note_and_no_part_paid_installment(
    fake: FakeProvider,
) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    refused = installments.cancel(plan, "Stop", a_user(Role.BILLING))
    assert not refused.ok and refused.message == installments.CANCEL_ADMIN
    refused = installments.cancel(plan, "   ", a_user(Role.ADMIN))
    assert not refused.ok and refused.message == installments.NOTE_REQUIRED
    refused = installments.cancel(plan, "card 4111 1111 1111 1111", a_user(Role.ADMIN))
    assert not refused.ok and refused.message == installments.NOTE_PAYMENT_DATA
    assert pay(rows_of(plan)[0], "100.00").ok
    refused = installments.cancel(plan, "Stop", a_user(Role.ADMIN))
    assert not refused.ok and refused.message == installments.CANCEL_PARTIAL
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ACTIVE


def test_an_installment_is_not_waived_or_cancelled_on_its_own(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    row = rows_of(plan_for(parent))[0]
    waived = one_off_charges.waive(row.pk, note="Goodwill", actor=a_user(Role.ADMIN))
    assert not waived.ok and waived.message == one_off_charges.WAIVE_INSTALLMENT
    cancelled = one_off_charges.cancel_charge(row.pk, note="Stop", actor=a_user(Role.BILLING))
    assert not cancelled.ok and cancelled.message == one_off_charges.CANCEL_INSTALLMENT


@pytest.mark.parametrize("status", [InstallmentPlanStatus.ACTIVE, InstallmentPlanStatus.ATTENTION])
def test_a_parent_on_a_live_plan_is_not_cancelled_or_waived_on_its_own(
    fake: FakeProvider, status: str
) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.EMAIL)
    plan = plan_for(parent)
    InstallmentPlan.objects.filter(pk=plan.pk).update(status=status)
    assert one_off_charges.cancel_refusal(parent) == one_off_charges.CANCEL_PARENT_ON_PLAN
    cancelled = one_off_charges.cancel_charge(parent.pk, note="Stop", actor=a_user(Role.BILLING))
    assert not cancelled.ok and cancelled.message == one_off_charges.CANCEL_PARENT_ON_PLAN
    waived = one_off_charges.waive(parent.pk, note="Goodwill", actor=a_user(Role.ADMIN))
    assert not waived.ok and waived.message == one_off_charges.WAIVE_PARENT_ON_PLAN
    parent.refresh_from_db()
    assert parent.status == OneOffChargeStatus.OWED
    assert not Job.objects.filter(entity_id=parent.pk).exists()
    assert not AuditLog.objects.filter(
        entity_id=str(parent.pk),
        action__in=[one_off_charges.CANCEL_ACTION, "one_off_charge.waived"],
    ).exists()


def test_a_parent_whose_plan_was_cancelled_may_be_cancelled_or_waived_again(
    fake: FakeProvider,
) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    assert installments.cancel(plan, "Changed our mind.", a_user(Role.ADMIN)).ok
    parent.refresh_from_db()
    assert one_off_charges.cancel_refusal(parent) == ""
    waived = one_off_charges.waive(parent.pk, note="Goodwill", actor=a_user(Role.ADMIN))
    assert waived.ok, waived.message
    parent.refresh_from_db()
    assert parent.status == OneOffChargeStatus.WAIVED


def test_a_parent_whose_plan_completed_is_refused_only_as_a_paid_charge(
    fake: FakeProvider,
) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    for row, amount in zip(rows_of(plan), ("333.33", "333.33", "333.34"), strict=True):
        assert pay(row, amount).ok
    plan.refresh_from_db()
    parent.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.COMPLETED
    assert one_off_charges.cancel_refusal(parent) == one_off_charges.CANCEL_PAID
    waived = one_off_charges.waive(parent.pk, note="Goodwill", actor=a_user(Role.ADMIN))
    assert not waived.ok and waived.message == one_off_charges.WAIVE_SETTLED


def test_the_charge_page_hides_cancel_and_waive_of_a_parent_on_a_live_plan(
    fake: FakeProvider,
) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.EMAIL)
    admin, _ = client_for(Role.ADMIN)
    before = admin.get(f"/one-off-charges/{parent.pk}/").content.decode()
    assert 'id="cancel-charge"' in before and 'id="waive-charge"' in before
    plan = plan_for(parent)
    page = admin.get(f"/one-off-charges/{parent.pk}/").content.decode()
    assert 'id="cancel-charge"' not in page and 'id="waive-charge"' not in page
    assert 'id="close-actions"' not in page
    assert f"/plans/{plan.pk}/" in page
    waive_form = admin.get(f"/one-off-charges/{parent.pk}/waive/").content.decode()
    assert one_off_charges.WAIVE_PARENT_ON_PLAN in waive_form
    cancel_form = admin.get(f"/one-off-charges/{parent.pk}/cancel/").content.decode()
    assert one_off_charges.CANCEL_PARENT_ON_PLAN in cancel_form


def test_the_cancel_and_waive_routes_refuse_a_parent_on_a_live_plan(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake), collection=CollectionMethod.EMAIL)
    plan_for(parent)
    admin, _ = client_for(Role.ADMIN)
    cancelled = admin.post(f"/one-off-charges/{parent.pk}/cancel/", {"note": "Stop"})
    assert cancelled["Location"] == f"/one-off-charges/{parent.pk}/cancel/"
    assert (
        one_off_charges.CANCEL_PARENT_ON_PLAN in admin.get(cancelled["Location"]).content.decode()
    )
    waived = admin.post(f"/one-off-charges/{parent.pk}/waive/", {"note": "Goodwill"})
    assert waived["Location"] == f"/one-off-charges/{parent.pk}/waive/"
    assert one_off_charges.WAIVE_PARENT_ON_PLAN in admin.get(waived["Location"]).content.decode()
    parent.refresh_from_db()
    assert parent.status == OneOffChargeStatus.OWED
    assert not Job.objects.filter(entity_id=parent.pk).exists()


# --- Counted once (D12) -------------------------------------------------------------------------


NOVEMBER = date(2026, 11, 1)
SETTLED_IN_NOVEMBER = date(2026, 11, 10)


def _paid_in_three(
    parent: RentCharge | OneOffCharge, split: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paid in three on 2026-11-10, a month after the parent's month (October)."""
    amounts = ("333.33", "333.33", "333.34")
    rows = rows_of(plan_for(parent)) if split else []
    on = SETTLED_IN_NOVEMBER
    later = datetime(on.year, on.month, on.day, 16, 0, tzinfo=UTC)
    monkeypatch.setattr("django.utils.timezone.now", lambda: later)
    if split:
        for row, amount in zip(rows, amounts, strict=True):
            assert pay(row, amount, on).ok
        return
    for amount in amounts:
        if isinstance(parent, RentCharge):
            assert rent_run.record_manual_payment(
                parent.pk,
                method=PaymentMethod.ZELLE,
                amount=D(amount),
                paid_on=on,
                note="Paid by Zelle",
                actor=None,
            ).ok
        else:
            assert pay(parent, amount, on).ok


@pytest.mark.parametrize("kind", ["rent", "damage"])
def test_the_pnl_and_the_ledger_count_a_split_charge_as_the_unsplit_one(
    fake: FakeProvider, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def parent_for() -> RentCharge | OneOffCharge:
        operator = an_operator(fake, customer=None)
        if kind == "rent":
            return a_rent(operator)
        lease = make_lease(operator=operator, payment_channel=PaymentChannel.WIRE)
        return a_one_off(operator, lease=lease, due=OCTOBER, category=OneOffCategory.DAMAGE)

    split, whole = parent_for(), parent_for()
    _paid_in_three(split, True, monkeypatch)
    _paid_in_three(whole, False, monkeypatch)
    query = pnl.parse_query({"from": "2026-09", "to": "2026-12"}, today=SETTLED_IN_NOVEMBER)
    rows = pnl.rows(query)

    def figures(parent: RentCharge | OneOffCharge) -> list[tuple[object, ...]]:
        store = parent.lease.store_id if parent.lease is not None else None
        return [
            (
                row.month,
                row.rent_billed,
                row.rent_collected,
                row.fees_billed,
                row.fees_collected,
            )
            for row in rows
            if row.store_id == store
        ]

    assert figures(split) == figures(whole)
    # Billed in the parent's month (October), collected in the month it settled (November).
    october = [row for row in figures(split) if row[0] == OCTOBER]
    november = [row for row in figures(split) if row[0] == NOVEMBER]
    if kind == "rent":
        assert october == [(OCTOBER, D("1000.00"), D("0.00"), D("0.00"), D("0.00"))]
        assert november == [(NOVEMBER, D("0.00"), D("1000.00"), D("0.00"), D("0.00"))]
    else:
        assert october == [(OCTOBER, D("0.00"), D("0.00"), D("1000.00"), D("0.00"))]
        assert november == [(NOVEMBER, D("0.00"), D("0.00"), D("0.00"), D("1000.00"))]

    def ledger_totals(parent: RentCharge | OneOffCharge) -> payments_ledger.Totals:
        assert parent.lease is not None
        selected = payments_ledger.PaymentFilter(store=parent.lease.store_id)
        return payments_ledger.totals(payments_ledger.ledger_payments(selected))

    assert ledger_totals(split) == ledger_totals(whole)
    assert ledger_totals(split).settled == D("1000.00")


def test_the_ledger_names_the_parent_of_an_installment_payment(fake: FakeProvider) -> None:
    parent = a_rent(an_operator(fake))
    row = rows_of(plan_for(parent))[0]
    assert pay(row, "333.33").ok
    [payment] = payments_ledger.ledger_payments(payments_ledger.PaymentFilter())
    assert payments_ledger.installment_of(payment) == "Rent 2026-10"
    [record] = [payments_ledger.csv_record(item) for item in payments_ledger.rows_for([payment])]
    assert record[payments_ledger.CSV_HEADER.index("Installment of")] == "Rent 2026-10"
    client, _ = client_for(Role.VIEWER)
    html = client.get("/payments/").content.decode()
    assert "Installment 1 of 3: Rent 2026-10" in html
    assert f'<a href="/charges/{parent.pk}/">Rent 2026-10</a>' in html


def test_the_grid_cell_of_a_split_rent_charge_shows_the_plans_progress(
    fake: FakeProvider,
) -> None:
    parent = a_rent(an_operator(fake))
    plan = plan_for(parent)
    assert pay(rows_of(plan)[0], "333.33").ok
    built = grid.build(grid.parse_query({"from": "2026-10", "to": "2026-10"}, today=TODAY), TODAY)
    [cell] = [row.cells[0] for row in built.rows if row.store.pk == parent.lease.store_id]
    assert cell.text == "plan 1 of 3 paid, 333.33 of 1,000.00"
    assert (cell.status, cell.amount_paid) == ("partial", D("333.33"))
    for row, amount in zip(rows_of(plan)[1:], ("333.33", "333.34"), strict=True):
        assert pay(row, amount).ok
    built = grid.build(grid.parse_query({"from": "2026-10", "to": "2026-10"}, today=TODAY), TODAY)
    [cell] = [row.cells[0] for row in built.rows if row.store.pk == parent.lease.store_id]
    assert (cell.text, cell.amount_paid) == ("paid 1,000.00", D("1000.00"))


# --- The pages (D10, D11) -------------------------------------------------------------


def test_split_from_the_charge_page_preview_edit_and_confirm(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/one-off-charges/{parent.pk}/").content.decode()
    assert 'id="split-installments"' in page
    url = f"/one-off-charges/{parent.pk}/split/"
    form = client.get(url).content.decode()
    assert 'id="split-form"' in form and 'id="page-help"' in form
    params = {"count": "3", "first_due": FIRST.isoformat(), "cadence": "monthly"}
    preview = client.get(url, params).content.decode()
    assert preview.count('class="split-line"') == 3
    assert 'value="333.34"' in preview and 'id="preview-help"' in preview
    assert not InstallmentPlan.objects.exists()
    posted = {
        **params,
        "due_1": "2026-10-20",
        "amount_1": "500.00",
        "due_2": "2026-11-20",
        "amount_2": "250.00",
        "due_3": "2026-12-20",
        "amount_3": "200.00",
    }
    refused = client.post(url, posted)
    assert refused.status_code == 302
    assert "amount_3=200.00" in refused["Location"]
    assert not InstallmentPlan.objects.exists()
    back = client.get(refused["Location"]).content.decode()
    assert "add up to the balance" in back and 'value="200.00"' in back
    confirmed = client.post(url, {**posted, "amount_3": "250.00"})
    plan = InstallmentPlan.objects.get()
    assert confirmed["Location"] == f"/plans/{plan.pk}/"
    assert [row.price for row in rows_of(plan)] == [D("500.00"), D("250.00"), D("250.00")]
    detail = client.get(f"/plans/{plan.pk}/").content.decode()
    assert "Installment plan: 0 of 3 paid, $0.00 of $1,000.00" in detail
    assert 'id="plan-reschedule"' in detail and 'id="plan-cancel"' not in detail
    parent_page = client.get(f"/one-off-charges/{parent.pk}/").content.decode()
    assert 'id="record-payment"' not in parent_page and "0 of 3 paid" in parent_page
    installment_page = client.get(f"/one-off-charges/{rows_of(plan)[0].pk}/").content.decode()
    assert 'id="installment-of"' in installment_page


def test_split_from_the_rent_charge_page(fake: FakeProvider) -> None:
    parent = a_rent(an_operator(fake))
    client, _ = client_for(Role.BILLING)
    assert 'id="split-installments"' in client.get(f"/charges/{parent.pk}/").content.decode()
    url = f"/charges/{parent.pk}/split/"
    lines = {f"due_{n}": f"2026-{9 + n}-20" for n in (1, 2, 3)}
    lines |= {"amount_1": "333.33", "amount_2": "333.33", "amount_3": "333.34"}
    response = client.post(
        url, {"count": "3", "first_due": "2026-10-20", "cadence": "monthly", **lines}
    )
    plan = InstallmentPlan.objects.get(rent_charge=parent)
    assert response["Location"] == f"/plans/{plan.pk}/"
    page = client.get(f"/charges/{parent.pk}/").content.decode()
    assert 'id="record-payment"' not in page and 'id="parent-plan-progress"' in page


def test_a_viewer_cannot_split_reschedule_or_cancel(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    other = a_one_off(an_operator(fake, customer=None))
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(f"/one-off-charges/{other.pk}/split/").status_code == 403
    assert viewer.post(f"/one-off-charges/{other.pk}/split/", {"count": "2"}).status_code == 403
    assert viewer.get(f"/plans/{plan.pk}/").status_code == 200
    assert viewer.get(f"/plans/{plan.pk}/reschedule/").status_code == 403
    assert viewer.post(f"/plans/{plan.pk}/cancel/", {"note": "x"}).status_code == 403
    billing, _ = client_for(Role.BILLING)
    assert billing.get(f"/plans/{plan.pk}/cancel/").status_code == 403
    assert billing.post(f"/plans/{plan.pk}/cancel/", {"note": "x"}).status_code == 403
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ACTIVE
    assert not InstallmentPlan.objects.filter(one_off_charge=other).exists()


def test_reschedule_and_cancel_from_the_plan_page(fake: FakeProvider) -> None:
    parent = a_one_off(an_operator(fake))
    plan = plan_for(parent)
    billing, _ = client_for(Role.BILLING)
    form = billing.get(f"/plans/{plan.pk}/reschedule/").content.decode()
    assert 'id="page-help"' in form
    moved = billing.post(
        f"/plans/{plan.pk}/reschedule/", {"first_due": "2026-11-02", "cadence": "biweekly"}
    )
    assert moved["Location"] == f"/plans/{plan.pk}/"
    assert [row.due_date for row in rows_of(plan)] == [
        date(2026, 11, 2),
        date(2026, 11, 16),
        date(2026, 11, 30),
    ]
    admin, _ = client_for(Role.ADMIN)
    assert 'id="page-help"' in admin.get(f"/plans/{plan.pk}/cancel/").content.decode()
    empty = admin.post(f"/plans/{plan.pk}/cancel/", {"note": ""})
    assert empty["Location"] == f"/plans/{plan.pk}/cancel/"
    done = admin.post(f"/plans/{plan.pk}/cancel/", {"note": "Moving out"})
    assert done["Location"] == f"/plans/{plan.pk}/"
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.CANCELLED


def test_the_invoice_page_points_to_the_split_of_its_charge(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    charge = a_one_off(operator, collection=CollectionMethod.EMAIL)
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-9100",
        operator=operator,
        one_off_charge=charge,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("1000.00"),
        total=D("1000.00"),
    )
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=1,
        description="Arrears",
        quantity=D("1"),
        unit_price=D("1000.00"),
        line_total=D("1000.00"),
        one_off_charge=charge,
    )
    draft = InvoiceDraft.objects.create(
        operator=operator,
        status=InvoiceDraftStatus.SENT,
        sent_invoice=invoice,
        created_by=a_user(Role.BILLING),
    )
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/invoices/drafts/{draft.pk}/").content.decode()
    assert f'href="/one-off-charges/{charge.pk}/split/"' in page


# --- Releasing the parent's provider object (PLAN-012b-a D2 to D5) -----------------------------


@pytest.fixture
def ticking(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock one millisecond on at each read, so a charge's latest job is the newest
    (two jobs never share a creation time); the day stays :data:`TODAY`."""
    clock = itertools.count()
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: NOW + timedelta(milliseconds=next(clock))
    )


def live_emailed(
    operator: Operator, number: str = "INV-2026-9001", *, lease: Lease | None = None
) -> tuple[OneOffCharge, Invoice]:
    """An e-mailed one-off parent whose own single-charge invoice is live at the fake."""
    from payments import jobs

    parent = a_one_off(operator, collection=CollectionMethod.EMAIL, lease=lease)
    invoice = Invoice.objects.create(
        invoice_number=number,
        operator=operator,
        one_off_charge=parent,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("1000.00"),
        total=D("1000.00"),
    )
    jobs.enqueue(JobKind.INVOICE_CREATE, parent, None, None)
    run_queued_jobs()
    assert one_off_charges.live_provider_invoice(parent) is not None
    return parent, invoice


def live_autopay(operator: Operator) -> OneOffCharge:
    """An ``autopay_due`` one-off parent whose own schedule is live at the fake."""
    from payments import jobs

    parent = a_one_off(operator, collection=CollectionMethod.AUTOPAY_DUE, due=FIRST)
    jobs.enqueue(JobKind.SCHEDULE_CREATE, parent, None, None)
    run_queued_jobs()
    assert one_off_charges.provider_schedule(parent)
    return parent


def jobs_of(charge: OneOffCharge) -> list[Job]:
    return list(Job.objects.filter(entity_id=charge.pk).order_by("created_at", "id"))


def installment_jobs(plan: InstallmentPlan) -> list[Job]:
    return list(Job.objects.filter(entity_id__in=[row.pk for row in rows_of(plan)]))


def actions_on(entity_id: object) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_id=str(entity_id))
        .order_by("id")
        .values_list("action", flat=True)
    )


def owed_at_price(parent: OneOffCharge) -> bool:
    parent.refresh_from_db()
    return (parent.status, parent.price, parent.amount_paid) == (
        OneOffChargeStatus.OWED,
        D("1000.00"),
        D("0.00"),
    )


def test_a_live_invoice_is_released_and_each_installment_gets_its_own(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments.provider import mask_identifier

    parent, invoice = live_emailed(an_operator(fake))
    ref = provider_ref(invoice)
    client, _ = client_for(Role.BILLING)
    split = client.get(
        f"/one-off-charges/{parent.pk}/split/",
        {"count": "3", "first_due": FIRST.isoformat(), "cadence": "monthly"},
    ).content.decode()
    assert 'id="release-notice"' in split and "Invoice INV-2026-9001 at the payment" in split
    before = len(calls(fake, "create_invoice"))
    plan = plan_for(parent)
    # The confirm queues the release alone; no installment job yet.
    [release] = [job for job in jobs_of(parent) if job.kind == JobKind.INVOICE_DELETE]
    assert release.params == {installments.RELEASE_PARAM: True}
    assert installment_jobs(plan) == []
    requested = AuditLog.objects.get(action=installments.RELEASE_REQUESTED_ACTION)
    assert requested.entity_id == str(parent.pk)
    assert requested.after["plan"] == str(plan.pk)
    assert requested.after["invoice_number"] == "INV-2026-9001"
    assert requested.after["provider_invoice"] == mask_identifier(ref) != ref
    page = client.get(f"/plans/{plan.pk}/").content.decode()
    assert (
        '<p id="release-pending"><strong>Waiting for the payment provider to release invoice '
        "INV-2026-9001.</strong></p>"
    ) in page
    run_queued_jobs()
    release.refresh_from_db()
    assert release.status == "done", release.result
    assert calls(fake, "delete_invoice") == [(ref,)]
    invoice.refresh_from_db()
    assert (invoice.internal_status, invoice.provider_status) == ("void", "DELETED")
    assert owed_at_price(parent)
    assert sorted(job.kind for job in installment_jobs(plan)) == [JobKind.INVOICE_CREATE] * 3
    assert len(calls(fake, "create_invoice")) == before + 3
    assert all(provider_ref(Invoice.objects.get(one_off_charge=row)) for row in rows_of(plan))
    released = AuditLog.objects.get(action=one_off_charges.INVOICE_RELEASED_ACTION)
    assert released.entity_id == str(invoice.pk)
    assert released.after["provider_invoice"] == mask_identifier(ref)
    assert ref not in str(released.after) and ref not in str(requested.after)
    plan_row = AuditLog.objects.get(action=installments.RELEASED_ACTION)
    assert plan_row.entity_id == str(plan.pk) and plan_row.after["queued"] == 3
    assert "one_off_charge.voided" not in actions_on(parent.pk)
    assert one_off_charges.live_provider_invoice(parent) is None
    page = client.get(f"/plans/{plan.pk}/").content.decode()
    assert 'id="release-pending"' not in page
    charge_page = client.get(f"/one-off-charges/{parent.pk}/").content.decode()
    assert (
        '<p class="invoice-released" id="invoice-released">Invoice INV-2026-9001 was released '
        "at the payment provider for the installment plan.</p>"
    ) in charge_page
    plan.refresh_from_db()
    assert plan.status == InstallmentPlanStatus.ACTIVE


def test_a_live_schedule_is_released_and_each_installment_gets_its_own(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    parent = live_autopay(an_operator(fake))
    schedule = one_off_charges.provider_schedule(parent)
    plan = plan_for(parent)
    [release] = [job for job in jobs_of(parent) if job.kind == JobKind.SCHEDULE_DELETE]
    assert release.params == {installments.RELEASE_PARAM: True}
    assert installment_jobs(plan) == []
    assert installments.release_pending(plan) == installments.RELEASE_PENDING_SCHEDULE
    run_queued_jobs()
    release.refresh_from_db()
    assert release.status == "done", release.result
    assert calls(fake, "delete_scheduled_invoice") == [(schedule,)]
    assert one_off_charges.provider_schedule(parent) == ""
    assert ExternalRef.objects.get(external_id=schedule).superseded_at is not None
    assert owed_at_price(parent)
    assert len(calls(fake, "create_scheduled_invoice")) == 1 + 3
    assert all(one_off_charges.provider_schedule(row) for row in rows_of(plan))
    row = AuditLog.objects.get(action=one_off_charges.SCHEDULE_RELEASED_ACTION)
    assert row.entity_id == str(parent.pk) and schedule not in str(row.after)
    assert installments.release_pending(plan) == ""


def test_a_line_of_a_multi_line_live_invoice_is_not_released(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    operator = an_operator(fake)
    first = a_one_off(operator, collection=CollectionMethod.EMAIL)
    second = a_one_off(operator, "200.00", collection=CollectionMethod.EMAIL)
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-9200",
        operator=operator,
        one_off_charge=first,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("1200.00"),
        total=D("1200.00"),
    )
    for position, charge in enumerate((first, second), start=1):
        InvoiceLineItem.objects.create(
            invoice=invoice,
            position=position,
            description=charge.description,
            quantity=D("1"),
            unit_price=charge.price,
            line_total=charge.price,
            one_off_charge=charge,
        )
    ExternalRef.objects.create(
        entity_type="invoice",
        entity_id=invoice.pk,
        provider="stax_pay",
        external_kind="invoice",
        external_id="fake-invoice-shared",
    )
    shared = installments.LIVE_INVOICE_SHARED.format(number="INV-2026-9200")
    assert shared == (
        "Invoice INV-2026-9200 has other lines; it cannot be released for one of them. "
        "Collect it or cancel it first."
    )
    assert installments.split_refusal(first) == shared
    assert installments.split_refusal(second) == shared
    outcome = installments.create_plan(
        first,
        2,
        FIRST,
        "monthly",
        [Line(FIRST, D("500.00")), Line(date(2026, 11, 20), D("500.00"))],
        a_user(Role.BILLING),
    )
    assert (outcome.ok, outcome.message) == (False, shared)
    assert nothing_written() and not Job.objects.exists()
    draft = InvoiceDraft.objects.create(
        operator=operator,
        status=InvoiceDraftStatus.SENT,
        sent_invoice=invoice,
        created_by=a_user(Role.BILLING),
    )
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/invoices/drafts/{draft.pk}/").content.decode()
    assert "To pay in installments, split each line from its charge page." in page


def test_a_schedule_that_generated_its_invoice_is_not_released(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    operator = an_operator(fake)
    parent = live_autopay(operator)
    Invoice.objects.create(
        invoice_number="INV-2026-9300",
        operator=operator,
        one_off_charge=parent,
        issue_date=FIRST,
        due_date=FIRST,
        subtotal=D("1000.00"),
        total=D("1000.00"),
    )
    assert installments.split_refusal(parent) == (
        "The schedule already generated its invoice; collect or cancel it first."
    )
    assert installments.release_kind(parent) == ""


def test_a_rent_parent_with_its_own_live_invoice_is_released(fake: FakeProvider) -> None:
    """PLAN-012b-b D13: the ``LIVE_INVOICE`` refusal for rent is gone."""
    operator = an_operator(fake)
    rent = a_rent(operator)
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-9400",
        operator=operator,
        rent_charge=rent,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("1000.00"),
        total=D("1000.00"),
    )
    ExternalRef.objects.create(
        entity_type="invoice",
        entity_id=invoice.pk,
        provider="stax_pay",
        external_kind="invoice",
        external_id="fake-invoice-rent",
    )
    assert installments.split_refusal(rent) == ""
    assert installments.release_kind(rent) == JobKind.INVOICE_DELETE
    assert installments.release_notice(rent) == installments.RELEASE_NOTICE_INVOICE.format(
        number="INV-2026-9400"
    )


def test_a_failed_release_waits_and_the_retry_reruns_it_with_its_flag(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments.errors import ProviderUnavailable

    parent, invoice = live_emailed(an_operator(fake))
    fake.fail_next("delete_invoice", ProviderUnavailable(503, "unavailable"))
    plan = plan_for(parent)
    [failed] = [job for job in run_queued_jobs() if job.kind == JobKind.INVOICE_DELETE]
    assert failed.status == "failed" and "ProviderUnavailable" in failed.result
    assert installment_jobs(plan) == []
    assert owed_at_price(parent)
    invoice.refresh_from_db()
    assert invoice.internal_status != "void"
    assert one_off_charges.retry_kind(parent) == JobKind.INVOICE_DELETE
    assert one_off_charges.retry_params(parent) == {installments.RELEASE_PARAM: True}
    viewer, _ = client_for(Role.VIEWER)
    plan_page = viewer.get(f"/plans/{plan.pk}/").content.decode()
    assert "Waiting for the payment provider to release invoice INV-2026-9001." in plan_page
    assert (
        'id="retry-provider"' not in viewer.get(f"/one-off-charges/{parent.pk}/").content.decode()
    )
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/one-off-charges/{parent.pk}/").content.decode()
    assert 'id="retry-provider"' in page and "ProviderUnavailable" in page
    client.post(f"/one-off-charges/{parent.pk}/retry/")
    retried = Job.objects.filter(entity_id=parent.pk, kind=JobKind.INVOICE_DELETE).latest(
        "created_at", "id"
    )
    assert retried.pk != failed.pk and retried.params == {installments.RELEASE_PARAM: True}
    run_queued_jobs()
    retried.refresh_from_db()
    assert retried.status == "done", retried.result
    invoice.refresh_from_db()
    assert (invoice.internal_status, invoice.provider_status) == ("void", "DELETED")
    assert owed_at_price(parent)
    assert len(installment_jobs(plan)) == 3
    assert "Waiting for" not in client.get(f"/plans/{plan.pk}/").content.decode()


def test_a_payment_on_the_parent_before_the_release_puts_the_plan_in_attention(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    parent, invoice = live_emailed(an_operator(fake))
    plan = plan_for(parent)
    # A partial payment settles on the parent's old invoice before the worker releases it.
    provider_txn(
        fake,
        "txn-parent-1",
        total=D("400.00"),
        invoice_external_id=provider_ref(invoice),
        customer_external_id=CUSTOMER,
    )
    deliver("create_transaction", id="txn-parent-1")
    settle(fake, "txn-parent-1")
    assert deliver("update_transaction_settled", id="txn-parent-1") == "processed"
    parent.refresh_from_db()
    plan.refresh_from_db()
    assert parent.amount_paid == D("400.00")
    assert plan.status == InstallmentPlanStatus.ATTENTION
    changed = AuditLog.objects.filter(
        action="installment_plan.status_changed", entity_id=str(plan.pk)
    ).latest("id")
    assert changed.after["status"] == "attention" and changed.after["reason"] == "parent_paid"
    [release] = [job for job in run_queued_jobs() if job.kind == JobKind.INVOICE_DELETE]
    assert release.status == "done" and release.result == installments.RELEASED_PARENT_PAID
    assert installment_jobs(plan) == []
    assert not Payment.objects.filter(one_off_charge__plan=plan).exists()
    [item] = [
        item
        for item in attention.attention(TODAY).items
        if item.kind == attention.SPLIT_CHARGE_PAID
    ]
    assert item.text == (
        "Payment on a split charge: No store, Arrears July to September 2026, $400.00 paid "
        "before its invoice was released; cancel or reschedule the plan"
    )
    assert item.url == f"/plans/{plan.pk}/" and item.severity == attention.HIGH
    outcome = installments.cancel(plan, "Paid on the old invoice.", a_user(Role.ADMIN))
    assert outcome.ok, outcome.message
    run_queued_jobs()
    parent.refresh_from_db()
    assert parent.balance == D("600.00") and parent.status == OneOffChargeStatus.PARTIAL
    assert not [
        item
        for item in attention.attention(TODAY).items
        if item.kind == attention.SPLIT_CHARGE_PAID
    ]


def test_the_release_run_twice_changes_nothing_and_queues_no_second_set(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments import jobs

    parent, _ = live_emailed(an_operator(fake))
    plan = plan_for(parent)
    run_queued_jobs()
    queued = len(installment_jobs(plan))
    audited = AuditLog.objects.count()
    provider_calls = len(fake.calls)
    jobs.enqueue(JobKind.INVOICE_DELETE, parent, {installments.RELEASE_PARAM: True}, None)
    [again] = run_queued_jobs()
    assert (again.status, again.result) == ("done", installments.RELEASE_ALREADY)
    assert len(installment_jobs(plan)) == queued == 3
    assert AuditLog.objects.count() == audited
    assert len(fake.calls) == provider_calls
    assert owed_at_price(parent)


def test_a_plan_cancelled_before_the_release_runs_gets_nothing_queued(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    parent, invoice = live_emailed(an_operator(fake))
    created = len(calls(fake, "create_invoice"))
    plan = plan_for(parent)
    assert installments.cancel(plan, "Changed our mind.", a_user(Role.ADMIN)).ok
    done = run_queued_jobs()
    [release] = [
        job for job in done if job.kind == JobKind.INVOICE_DELETE and job.entity_id == parent.pk
    ]
    assert (release.status, release.result) == ("done", installments.RELEASED_PLAN_CLOSED)
    assert calls(fake, "delete_invoice") == [(provider_ref(invoice),)]
    assert len(calls(fake, "create_invoice")) == created
    assert not Job.objects.filter(
        entity_id__in=[row.pk for row in rows_of(plan)], kind=JobKind.INVOICE_CREATE
    ).exists()
    assert owed_at_price(parent)
    assert installments.release_pending(plan) == ""


def test_the_release_runs_on_a_parent_whose_cancel_is_refused(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    parent, invoice = live_emailed(an_operator(fake))
    plan = plan_for(parent)
    assert one_off_charges.cancel_refusal(parent) == one_off_charges.CANCEL_PARENT_ON_PLAN
    [release] = [job for job in run_queued_jobs() if job.kind == JobKind.INVOICE_DELETE]
    assert release.status == "done", release.result
    assert calls(fake, "delete_invoice") == [(provider_ref(invoice),)]
    invoice.refresh_from_db()
    assert invoice.internal_status == "void"
    assert owed_at_price(parent)
    assert len(installment_jobs(plan)) == 3


def test_the_schedule_release_runs_on_a_parent_whose_cancel_is_refused(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    parent = live_autopay(an_operator(fake))
    plan_for(parent)
    assert one_off_charges.cancel_refusal(parent) == one_off_charges.CANCEL_PARENT_ON_PLAN
    [release] = [job for job in run_queued_jobs() if job.kind == JobKind.SCHEDULE_DELETE]
    assert release.status == "done", release.result
    assert owed_at_price(parent)


def test_the_lease_end_blockers_see_no_live_invoice_after_a_release(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from ledger.services import lease_end

    operator = an_operator(fake)
    lease = make_lease(operator=operator)
    parent, _ = live_emailed(operator, lease=lease)
    plan_for(parent)
    assert lease_end.LIVE_INVOICE in [found.kind for found in lease_end.blockers(lease, TODAY)]
    run_queued_jobs()
    found = lease_end.blockers(lease, TODAY)
    # The installments' own invoices are live now; the parent's is not.
    assert not [blocker for blocker in found if "INV-2026-9001" in blocker.text]
    assert lease_end.INSTALLMENT_PLAN in [blocker.kind for blocker in found]


def test_a_released_single_line_builder_invoice_keeps_its_line_links(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from io import StringIO

    from django.core.management import call_command

    parent, invoice = live_emailed(an_operator(fake))
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=1,
        description=parent.description,
        quantity=D("1"),
        unit_price=parent.price,
        line_total=parent.price,
        one_off_charge=parent,
    )
    assert installments.split_refusal(parent) == ""
    plan_for(parent)
    run_queued_jobs()
    invoice.refresh_from_db()
    assert invoice.internal_status == "void"
    call_command("invoice_line_links", "--check", stdout=StringIO())


def test_the_invoice_page_says_the_split_releases_a_live_single_charge_invoice(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    parent, invoice = live_emailed(an_operator(fake))
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=1,
        description=parent.description,
        quantity=D("1"),
        unit_price=parent.price,
        line_total=parent.price,
        one_off_charge=parent,
    )
    assert parent.operator is not None
    draft = InvoiceDraft.objects.create(
        operator=parent.operator,
        status=InvoiceDraftStatus.SENT,
        sent_invoice=invoice,
        created_by=a_user(Role.BILLING),
    )
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/invoices/drafts/{draft.pk}/").content.decode()
    assert (
        "Split into installments from the charge page; the invoice is released at the payment "
        "provider when the plan is confirmed."
    ) in page
    assert f'href="/one-off-charges/{parent.pk}/split/"' in page


def test_a_release_job_on_a_charge_without_a_plan_deletes_nothing(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments import jobs

    parent, invoice = live_emailed(an_operator(fake))
    jobs.enqueue(JobKind.INVOICE_DELETE, parent, {installments.RELEASE_PARAM: True}, None)
    [job] = run_queued_jobs()
    assert (job.status, job.result) == ("failed", installments.RELEASE_NO_PLAN)
    assert calls(fake, "delete_invoice") == []
    assert one_off_charges.live_provider_invoice(parent) is not None


# --- Splitting rent (PLAN-012b-b D13) -----------------------------------------------------------


def autopay_lease(fake: FakeProvider, *, debit_day: int = 20, second: int | None = None) -> Lease:
    """A provider-ACH lease of $1,500.00 whose rent autopay is live at the fake, enabled
    on :data:`NOW` (the 20th's first debit is 20 October)."""
    from ledger.services import leases
    from tests.test_one_off_charges import BANK

    lease = make_lease(
        operator=an_operator(fake),
        payment_channel=PaymentChannel.PROVIDER_ACH,
        debit_day=debit_day,
        second_debit_day=second,
    )
    outcome = leases.enable_autopay(lease.pk, BANK, actor=None, now=NOW)
    assert outcome.ok, outcome.text
    return lease


def rent_due(lease: Lease, due: date, *, seq: int = 1, amount: str = "1500.00") -> RentCharge:
    return RentCharge.objects.create(
        lease=lease,
        period_month=OCTOBER,
        seq=seq,
        due_on=due,
        amount_due=D(amount),
        status=RentChargeStatus.DUE,
    )


def rules(fake: FakeProvider) -> list[str]:
    return [str(arguments[0].rule) for arguments in calls(fake, "create_scheduled_invoice")]  # type: ignore[attr-defined]


def rent_owed(rent: RentCharge) -> bool:
    rent.refresh_from_db()
    return (rent.status, rent.amount_paid) == (RentChargeStatus.DUE, D("0.00"))


def test_a_rent_charge_on_an_autopay_lease_splits_in_one_step_and_autopay_moves_past_the_month(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from ledger.services import leases
    from payments.provider import mask_identifier

    lease = autopay_lease(fake)
    [old] = leases.live_schedules(lease)
    old_ref = leases.schedule_ref(old)
    assert rules(fake) == ["DTSTART=20261020T120000Z;FREQ=MONTHLY"]
    rent = rent_due(lease, date(2026, 10, 20))
    assert installments.split_refusal(rent) == ""
    client, _ = client_for(Role.BILLING)
    split = client.get(
        f"/charges/{rent.pk}/split/",
        {"count": "3", "first_due": FIRST.isoformat(), "cadence": "monthly"},
    ).content.decode()
    assert 'id="release-notice"' in split
    assert "Rent autopay of this lease will skip this month&#x27;s debit" in split
    plan = plan_for(rent)
    assert plan.collection == InstallmentCollection.AUTOPAY_DUE
    [release] = Job.objects.filter(entity_id=rent.pk)
    assert (release.kind, release.entity_type) == (JobKind.SCHEDULE_DELETE, "rent_charge")
    assert release.params == {installments.RELEASE_PARAM: True}
    assert installment_jobs(plan) == []
    requested = AuditLog.objects.get(action=installments.RENT_RELEASE_REQUESTED_ACTION)
    assert requested.entity_id == str(rent.pk) and requested.after["plan"] == str(plan.pk)
    assert requested.after["schedules"] == [mask_identifier(old_ref)]
    assert old_ref not in str(requested.after)
    assert installments.release_pending(plan) == installments.RELEASE_PENDING_AUTOPAY
    run_queued_jobs()
    release.refresh_from_db()
    assert (release.status, release.result) == ("done", installments.RELEASED.format(count=3))
    # The rent schedule is deleted, then created again from next month's debit.
    assert calls(fake, "delete_scheduled_invoice") == [(old_ref,)]
    old.refresh_from_db()
    assert old.state == ScheduleState.ENDED
    [new] = leases.live_schedules(lease)
    new_ref = leases.schedule_ref(new)
    assert new_ref and new_ref != old_ref and new.pk != old.pk
    assert (new.seq, new.debit_day, new.amount, new.state) == (
        1,
        20,
        D("1500.00"),
        ScheduleState.ACTIVE,
    )
    assert rules(fake) == [
        "DTSTART=20261020T120000Z;FREQ=MONTHLY",
        "DTSTART=20261120T120000Z;FREQ=MONTHLY",
        # Then one one-occurrence schedule per installment.
        "DTSTART=20261020T120000Z;FREQ=DAILY;COUNT=1",
        "DTSTART=20261120T120000Z;FREQ=DAILY;COUNT=1",
        "DTSTART=20261220T120000Z;FREQ=DAILY;COUNT=1",
    ]
    assert all(one_off_charges.provider_schedule(row) for row in rows_of(plan))
    moved = AuditLog.objects.get(action=leases.MOVED_ACTION)
    assert (moved.entity_type, moved.entity_id) == ("lease", str(lease.pk))
    assert moved.after == {
        "plan": str(plan.pk),
        "seq": 1,
        "skipped": "2026-10-20",
        "starts": "2026-11-20T12:00:00+00:00",
        "row": str(new.pk),
        "schedule": mask_identifier(new_ref),
    }
    deleted = AuditLog.objects.get(action=leases.DELETED_FOR_MOVE_ACTION)
    assert deleted.after["plan"] == str(plan.pk) and old_ref not in str(deleted.after)
    assert new_ref not in str(moved.after)
    assert rent_owed(rent)
    assert installments.release_pending(plan) == ""
    page = client.get(f"/leases/{lease.pk}/").content.decode()
    assert (
        '<p id="autopay-moved"><strong>Autopay resumes 2026-11-20 (this month is collected in '
        "installments)</strong></p>"
    ) in page
    table = page.split('id="schedules"')[1].split("</table>")[0]
    assert ">2026-11-20</td>" in table and "2026-10-20" not in table
    # Once the plan is no longer live the lease page says nothing more.
    assert installments.cancel(plan, "Paid another way.", a_user(Role.ADMIN)).ok
    assert leases.autopay_move(lease) is None
    assert 'id="autopay-moved"' not in client.get(f"/leases/{lease.pk}/").content.decode()


def test_a_twice_monthly_lease_moves_both_schedules_and_skips_only_the_split_debit(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from ledger.services import leases

    lease = autopay_lease(fake, debit_day=10, second=20)
    refs = sorted(leases.schedule_ref(row) for row in leases.live_schedules(lease))
    assert sorted(rules(fake)) == [
        "DTSTART=20261010T120000Z;FREQ=MONTHLY",
        "DTSTART=20261020T120000Z;FREQ=MONTHLY",
    ]
    rent = rent_due(lease, date(2026, 10, 10), amount="750.00")
    plan = plan_for(rent)
    run_queued_jobs()
    assert sorted(arguments[0] for arguments in calls(fake, "delete_scheduled_invoice")) == refs  # type: ignore[type-var]
    rows = {row.seq: row for row in leases.live_schedules(lease)}
    assert sorted(rows) == [1, 2]
    assert all(leases.schedule_ref(row) not in refs for row in rows.values())
    assert all(row.state == ScheduleState.ACTIVE for row in rows.values())
    # The split debit (the 10th) resumes next month; the 20th keeps this month's debit.
    assert sorted(rules(fake)[2:4]) == [
        "DTSTART=20261020T120000Z;FREQ=MONTHLY",
        "DTSTART=20261110T120000Z;FREQ=MONTHLY",
    ]
    moved = {
        row.after["seq"]: row.after
        for row in AuditLog.objects.filter(action=leases.MOVED_ACTION)
        if isinstance(row.after, dict)
    }
    assert (moved[1]["skipped"], moved[1]["starts"]) == ("2026-10-10", "2026-11-10T12:00:00+00:00")
    assert (moved[2]["skipped"], moved[2]["starts"]) == (None, "2026-10-20T12:00:00+00:00")
    found = leases.autopay_move(lease)
    assert found is not None and found.resumes == date(2026, 11, 10)
    assert len(installment_jobs(plan)) == 3
    assert rent_owed(rent)


def test_a_failure_between_the_delete_and_the_create_fails_the_job_and_retry_completes_it(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from ledger.services import leases
    from payments import one_off_invoices
    from payments.errors import ProviderUnavailable

    lease = autopay_lease(fake)
    [old] = leases.live_schedules(lease)
    rent = rent_due(lease, date(2026, 10, 20))
    plan = plan_for(rent)
    fake.fail_next("create_scheduled_invoice", ProviderUnavailable(503, "unavailable"))
    [failed] = run_queued_jobs()
    assert failed.status == "failed"
    assert failed.result.startswith(one_off_invoices.AUTOPAY_MOVE_FAILED)
    assert "ProviderUnavailable" in failed.result and "Retry from the charge page." in failed.result
    old.refresh_from_db()
    assert old.state == ScheduleState.ENDED
    [left] = leases.live_schedules(lease)
    assert left.state == ScheduleState.FAILED and not leases.schedule_ref(left)
    assert "lease.autopay_failed" in actions_on(lease.pk)
    assert not AuditLog.objects.filter(action=leases.MOVED_ACTION).exists()
    # Nothing voided, nothing queued: the rent charge stays owed and the plan waits.
    assert rent_owed(rent)
    assert installment_jobs(plan) == []
    assert installments.release_pending(plan) == installments.RELEASE_PENDING_AUTOPAY
    viewer, _ = client_for(Role.VIEWER)
    assert 'id="retry-provider"' not in viewer.get(f"/charges/{rent.pk}/").content.decode()
    assert viewer.post(f"/charges/{rent.pk}/retry-release/").status_code == 403
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/charges/{rent.pk}/").content.decode()
    assert 'id="retry-provider"' in page and "ProviderUnavailable" in page
    client.post(f"/charges/{rent.pk}/retry-release/")
    retried = Job.objects.filter(entity_id=rent.pk).latest("created_at", "id")
    assert retried.pk != failed.pk and retried.kind == JobKind.SCHEDULE_DELETE
    assert retried.params == {installments.RELEASE_PARAM: True}
    run_queued_jobs()
    retried.refresh_from_db()
    assert (retried.status, retried.result) == ("done", installments.RELEASED.format(count=3))
    # The delete is not repeated; the failed row is created at last.
    assert len(calls(fake, "delete_scheduled_invoice")) == 1
    [new] = leases.live_schedules(lease)
    assert new.pk == left.pk and new.state == ScheduleState.ACTIVE and leases.schedule_ref(new)
    assert "DTSTART=20261120T120000Z;FREQ=MONTHLY" in rules(fake)
    assert AuditLog.objects.filter(action=leases.MOVED_ACTION).count() == 1
    assert len(installment_jobs(plan)) == 3
    assert rent_owed(rent)
    assert 'id="retry-provider"' not in client.get(f"/charges/{rent.pk}/").content.decode()


def test_a_rent_charge_whose_month_debit_is_pending_is_still_refused(fake: FakeProvider) -> None:
    lease = autopay_lease(fake)
    rent = rent_due(lease, date(2026, 10, 20))
    rent.status = RentChargeStatus.PENDING
    rent.save(update_fields=["status"])
    assert installments.split_refusal(rent) == installments.PENDING
    outcome = installments.create_plan(
        rent,
        2,
        FIRST,
        "monthly",
        [Line(FIRST, D("750.00")), Line(date(2026, 11, 20), D("750.00"))],
        a_user(Role.BILLING),
    )
    assert (outcome.ok, outcome.message) == (False, installments.PENDING)
    assert nothing_written() and not Job.objects.filter(entity_id=rent.pk).exists()


def test_the_lease_end_sees_the_moved_schedule_as_live_until_autopay_is_stopped(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from ledger.services import lease_end, leases

    lease = autopay_lease(fake)
    rent = rent_due(lease, date(2026, 10, 20))
    plan_for(rent)
    run_queued_jobs()
    found = lease_end.blockers(lease, TODAY)
    [autopay] = [blocker for blocker in found if blocker.kind == lease_end.AUTOPAY]
    assert autopay.text == (
        "Rent autopay (debit 1) is active at the payment provider. It must be stopped first."
    )
    assert lease_end.INSTALLMENT_PLAN in [blocker.kind for blocker in found]
    [new] = leases.live_schedules(lease)
    new_ref = leases.schedule_ref(new)
    assert leases.stop_autopay(lease.pk, actor=None).ok
    assert calls(fake, "delete_scheduled_invoice")[-1] == (new_ref,)
    assert lease_end.AUTOPAY not in [blocker.kind for blocker in lease_end.blockers(lease, TODAY)]


def rent_alone_invoice(operator: Operator, rent: RentCharge, number: str) -> Invoice:
    """An invoice builder invoice of one Rent line alone, created and e-mailed at the
    fake through its rent charge's ``invoice_create`` job (PLAN-012b-b D5)."""
    from payments import jobs

    invoice = Invoice.objects.create(
        invoice_number=number,
        operator=operator,
        rent_charge=rent,
        issue_date=TODAY,
        due_date=TODAY,
        memo="Rent 2026-10",
        subtotal=rent.amount_due,
        total=rent.amount_due,
    )
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=1,
        description="Rent 2026-10",
        quantity=D("1"),
        unit_price=rent.amount_due,
        line_total=rent.amount_due,
        rent_charge=rent,
    )
    jobs.enqueue(JobKind.INVOICE_CREATE, rent, None, None)
    [created] = run_queued_jobs()
    assert created.status == "done", created.result
    return invoice


def test_an_emailed_rent_alone_invoice_is_released_and_the_rent_charge_kept(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    from payments.provider import mask_identifier

    operator = an_operator(fake)
    rent = a_rent(operator)
    invoice = rent_alone_invoice(operator, rent, "INV-2026-9500")
    ref = provider_ref(invoice)
    assert installments.split_refusal(rent) == ""
    assert installments.release_kind(rent) == JobKind.INVOICE_DELETE
    plan = plan_for(rent)
    assert plan.collection == InstallmentCollection.MANUAL
    [release] = [
        job for job in Job.objects.filter(entity_id=rent.pk) if job.kind == "invoice_delete"
    ]
    assert release.params == {installments.RELEASE_PARAM: True}
    requested = AuditLog.objects.get(action=installments.RENT_RELEASE_REQUESTED_ACTION)
    assert requested.after["invoice_number"] == "INV-2026-9500"
    assert requested.after["provider_invoice"] == mask_identifier(ref) != ref
    assert installments.release_pending(plan) == installments.RELEASE_PENDING_INVOICE.format(
        number="INV-2026-9500"
    )
    run_queued_jobs()
    release.refresh_from_db()
    # Manual installments: nothing to queue at the provider.
    assert (release.status, release.result) == ("done", installments.RELEASED.format(count=0))
    assert calls(fake, "delete_invoice") == [(ref,)]
    invoice.refresh_from_db()
    assert (invoice.internal_status, invoice.provider_status) == ("void", "DELETED")
    # The links stay as history.
    assert invoice.rent_charge_id == rent.pk
    assert InvoiceLineItem.objects.get(invoice=invoice).rent_charge_id == rent.pk
    released = AuditLog.objects.get(action=one_off_charges.INVOICE_RELEASED_ACTION)
    assert released.entity_id == str(invoice.pk) and ref not in str(released.after)
    assert rent_owed(rent)
    assert installment_jobs(plan) == []
    assert installments.release_pending(plan) == ""


def test_a_rent_line_of_a_mixed_invoice_is_refused(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    rent = a_rent(operator)
    setup = a_one_off(operator, "200.00", collection=CollectionMethod.EMAIL)
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-9600",
        operator=operator,
        one_off_charge=setup,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("1200.00"),
        total=D("1200.00"),
    )
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=1,
        description=setup.description,
        quantity=D("1"),
        unit_price=setup.price,
        line_total=setup.price,
        one_off_charge=setup,
    )
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=2,
        description="Rent 2026-10",
        quantity=D("1"),
        unit_price=rent.amount_due,
        line_total=rent.amount_due,
        rent_charge=rent,
    )
    ExternalRef.objects.create(
        entity_type="invoice",
        entity_id=invoice.pk,
        provider="stax_pay",
        external_kind="invoice",
        external_id="fake-invoice-mixed",
    )
    shared = installments.LIVE_INVOICE_SHARED.format(number="INV-2026-9600")
    assert installments.split_refusal(rent) == shared
    outcome = installments.create_plan(
        rent,
        2,
        FIRST,
        "monthly",
        [Line(FIRST, D("500.00")), Line(date(2026, 11, 20), D("500.00"))],
        a_user(Role.BILLING),
    )
    assert (outcome.ok, outcome.message) == (False, shared)
    assert nothing_written() and not Job.objects.exists()


def test_a_rent_alone_invoice_that_autopay_would_also_debit_is_refused(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    lease = autopay_lease(fake)
    rent = rent_due(lease, date(2026, 10, 20))
    assert lease.operator is not None
    rent_alone_invoice(lease.operator, rent, "INV-2026-9700")
    assert installments.split_refusal(rent) == installments.AUTOPAY_WILL_DEBIT


def test_a_split_of_rent_on_a_wire_lease_is_unchanged(
    fake: FakeProvider, worker: None, ticking: None
) -> None:
    rent = a_rent(an_operator(fake))
    assert installments.release_kind(rent) == "" and installments.release_notice(rent) == ""
    plan = plan_for(rent)
    assert plan.collection == InstallmentCollection.MANUAL
    assert not Job.objects.exists()
    assert not AuditLog.objects.filter(
        action__in=[installments.RENT_RELEASE_REQUESTED_ACTION, installments.RELEASED_ACTION]
    ).exists()
    assert installments.release_pending(plan) == ""
    assert rent_owed(rent)
