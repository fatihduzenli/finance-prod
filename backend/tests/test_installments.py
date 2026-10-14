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


def test_a_charge_with_a_live_provider_invoice_or_schedule_is_not_split(
    fake: FakeProvider, worker: None
) -> None:
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
    assert installments.split_refusal(emailed).startswith("Invoice INV-2026-9001")
    autopay = a_one_off(operator, collection=CollectionMethod.AUTOPAY_DUE, due=FIRST)
    jobs.enqueue(JobKind.SCHEDULE_CREATE, autopay, None, None)
    run_queued_jobs()
    assert installments.split_refusal(autopay) == installments.LIVE_SCHEDULE


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


def test_rent_that_autopay_will_debit_is_not_split(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    rent = a_rent(operator, channel=PaymentChannel.PROVIDER_ACH)
    CollectionSchedule.objects.create(
        lease=rent.lease,
        kind=ScheduleKind.RENT,
        seq=1,
        debit_day=15,
        amount=D("1000.00"),
        state=ScheduleState.ACTIVE,
    )
    assert installments.split_refusal(rent) == installments.AUTOPAY_WILL_DEBIT
    # Past its due date the schedule no longer debits it.
    assert installments.split_refusal(rent, date(2026, 10, 16)) == ""


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
