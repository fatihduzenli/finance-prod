"""The reports of docs/qa/PLAN-011-dashboard-reports-mobile.md D4 (with D5, D7, D10): the A/R
aging (buckets, the dashboard's total by construction and under random data), revenue by
month, payments by method, deposits versus transactions, the customer statement, the
reports menu, ranges, roles, CSV and exact money."""

from __future__ import annotations

import csv
import io
import random
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    CollectionStatus,
    FeeType,
    InsuranceCharge,
    LegacyStatus,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import pnl, receivables, reports
from payments.models import (
    Deposit,
    MismatchKind,
    ReconciliationMismatch,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
)
from payments.webhooks.handlers import transaction as transaction_handler
from tests.fixtures.baseline_aug_2026 import AS_OF, build_baseline
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
ZERO = D("0.00")
TODAY = date(2026, 10, 6)
JUL, AUG, SEP, OCT = (date(2026, m, 1) for m in (7, 8, 9, 10))
RANGED = ("/reports/revenue/", "/reports/payments-by-method/", "/reports/deposits/")


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def rows_of(response: Any) -> list[list[str]]:
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()
    return list(csv.reader(io.StringIO(body)))


def dicts_of(response: Any) -> list[dict[str, str]]:
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()
    return list(csv.DictReader(io.StringIO(body)))


def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def rng(start: str, end: str) -> reports.ReportRange:
    return reports.parse_range({"from": start, "to": end}, TODAY)


def a_fee_type(refundable: bool) -> FeeType:
    code = f"fee-{uuid.uuid4().hex[:8]}"
    return FeeType.objects.create(code=code, name="Security deposit", refundable=refundable)


def a_deposit_charge(operator: Operator, price: str, due: date, **fields: Any) -> OneOffCharge:
    lease = a_lease(operator=operator)
    return a_one_off_charge(
        operator,
        price,
        due,
        lease=lease,
        category=OneOffCategory.SETUP,
        collection="none",
        description="",
        fee_type=a_fee_type(True),
        **fields,
    )


def one_off_payment(
    charge: OneOffCharge, amount: str, settled_on: date | None, **fields: Any
) -> Payment:
    values: dict[str, Any] = {
        "one_off_charge": charge,
        "kind": PaymentKind.ONE_OFF,
        "method": PaymentMethod.ACH,
        "amount": D(amount),
        "status": PaymentStatus.SETTLED,
        "initiated_on": settled_on,
        "settled_on": settled_on,
    }
    values.update(fields)
    return Payment.objects.create(**values)


def insurance_payment(
    charge: InsuranceCharge, amount: str, settled_on: date, **fields: Any
) -> Payment:
    values: dict[str, Any] = {
        "insurance_charge": charge,
        "kind": PaymentKind.INSURANCE,
        "policy_year": charge.policy_year,
        "method": PaymentMethod.ACH,
        "amount": D(amount),
        "status": PaymentStatus.SETTLED,
        "initiated_on": settled_on,
        "settled_on": settled_on,
    }
    values.update(fields)
    return Payment.objects.create(**values)


def refund_of(payment: Payment, amount: str, settled_on: date) -> Payment:
    return Payment.objects.create(
        rent_charge=payment.rent_charge,
        insurance_charge=payment.insurance_charge,
        one_off_charge=payment.one_off_charge,
        kind=payment.kind,
        policy_year=payment.policy_year,
        method=payment.method,
        note=payment.note,
        amount=D(amount),
        status=PaymentStatus.SETTLED,
        is_refund=True,
        parent_payment=payment,
        initiated_on=settled_on,
        settled_on=settled_on,
    )


def waive(charge: OneOffCharge) -> None:
    OneOffCharge.objects.filter(pk=charge.pk).update(
        status=OneOffChargeStatus.WAIVED, waived_at=timezone.now(), waive_note="Goodwill"
    )


def void(charge: OneOffCharge) -> None:
    OneOffCharge.objects.filter(pk=charge.pk).update(
        status=OneOffChargeStatus.VOIDED, voided_at=timezone.now(), void_note="Cancelled"
    )


# --- A/R aging: buckets -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "bucket"),
    [
        (-30, receivables.AGING_CURRENT),
        (0, receivables.AGING_CURRENT),
        (1, receivables.AGING_1_30),
        (30, receivables.AGING_1_30),
        (31, receivables.AGING_31_60),
        (60, receivables.AGING_31_60),
        (61, receivables.AGING_61_90),
        (90, receivables.AGING_61_90),
        (91, receivables.AGING_OVER_90),
        (4000, receivables.AGING_OVER_90),
    ],
)
def test_the_bucket_of_each_boundary(days: int, bucket: str) -> None:
    assert receivables.aging_bucket(days) == bucket


def test_charges_at_each_boundary_land_in_their_bucket(today: date) -> None:
    operator = an_operator("Boundary Operator")
    for days in (0, 1, 30, 31, 60, 61, 90, 91):
        due = TODAY - timedelta(days=days)
        a_one_off_charge(operator, f"{days + 1}.00", due)
    report = receivables.aging(TODAY)
    [row] = report.rows
    assert row.buckets == {
        receivables.AGING_CURRENT: D("1.00"),
        receivables.AGING_1_30: D("2.00") + D("31.00"),
        receivables.AGING_31_60: D("32.00") + D("61.00"),
        receivables.AGING_61_90: D("62.00") + D("91.00"),
        receivables.AGING_OVER_90: D("92.00"),
    }
    assert row.total == report.totals.total == D("372.00") and row.count == 8


def build_mixed() -> dict[str, Any]:
    """Every kind of charge, open or not, over two operators and no operator."""
    north = an_operator("North")
    south = an_operator("South")
    lease = a_lease(a_store("100", 100), north, debit_day=5)
    a_rent_charge(lease, JUL, RentChargeStatus.UNPAID, "1000.00")  # 93 days
    a_rent_charge(lease, AUG, RentChargeStatus.PARTIAL, "1000.00", "250.00")  # 62 days
    paid = a_rent_charge(lease, SEP, RentChargeStatus.PAID, "1000.00", "1000.00")
    a_payment(paid, "1000.00", date(2026, 9, 7))
    a_rent_charge(lease, OCT, RentChargeStatus.PENDING, "1000.00")  # due 5 Oct: 1 day
    a_rent_charge(lease, date(2026, 6, 1), RentChargeStatus.WAIVED, "1000.00")
    a_rent_charge(lease, date(2026, 11, 1), RentChargeStatus.DUE, "1000.00")  # current
    insurance = an_insurance_charge(lease, 2026, date(2026, 9, 1))  # 695, 35 days
    insurance_payment(insurance, "195.00", date(2026, 9, 3))
    an_insurance_charge(lease, 2027, date(2027, 9, 1), amount=None, needs_premium=True)
    an_insurance_charge(lease, 2025, date(2025, 9, 1), legacy=True, legacy_status=LegacyStatus.PAID)
    other = a_lease(a_store("200", 200), south, debit_day=1)
    a_rent_charge(other, SEP, RentChargeStatus.UNPAID, "500.00")  # 35 days
    a_one_off_charge(south, "80.00", date(2026, 9, 20), lease=other)  # 16 days
    waive(a_one_off_charge(south, "70.00", date(2026, 9, 20)))
    void(a_one_off_charge(south, "60.00", date(2026, 9, 20)))
    a_deposit_charge(south, "2000.00", date(2026, 9, 1))
    orphan = a_lease(a_store("300", 300), None, status=CollectionStatus.PROSPECTIVE)
    orphan.operator = None
    orphan.save(update_fields=["operator"])
    a_rent_charge(orphan, SEP, RentChargeStatus.UNPAID, "10.00")
    return {"north": north, "south": south}


def test_the_aging_rows_by_operator_skip_waived_voided_paid_and_deposits(today: date) -> None:
    build_mixed()
    report = receivables.aging(TODAY)
    by_name = {row.operator_name or "none": row for row in report.rows}
    assert list(by_name) == ["North", "South", "none"]
    north = by_name["North"]
    assert north.buckets == {
        receivables.AGING_CURRENT: D("1000.00"),
        receivables.AGING_1_30: D("1000.00"),
        receivables.AGING_31_60: D("500.00"),
        receivables.AGING_61_90: D("750.00"),
        receivables.AGING_OVER_90: D("1000.00"),
    }
    assert by_name["South"].total == D("580.00")
    assert by_name["none"].total == D("10.00")
    assert report.totals.total == D("4840.00")
    assert report.totals.total == receivables.dashboard(TODAY).outstanding


def test_the_aging_total_equals_the_dashboard_on_the_baseline() -> None:
    build_baseline()
    report = receivables.aging(AS_OF)
    dashboard = receivables.dashboard(AS_OF)
    assert report.totals.total == dashboard.outstanding
    assert report.totals.count == dashboard.outstanding_count == 47 + 13
    assert sum((row.total for row in report.rows), ZERO) == report.totals.total


def test_the_aging_page_and_csv(today: date) -> None:
    build_mixed()
    client = viewer()
    response = client.get("/reports/aging/")
    page = response.content.decode()
    assert response.status_code == 200
    assert 'id="aging-grand-total">4840.00<' in page
    assert "by due date" in page and "revenue is by settled month" in page
    records = dicts_of(client.get("/reports/aging/export.csv"))
    assert [record["Operator"] for record in records] == ["North", "South", "No operator", "Total"]
    assert records[-1]["Total (USD)"] == "4840.00"
    assert records[0]["Over 90 days (USD)"] == "1000.00"
    assert records[-1]["Open charges"] == "8"


# --- A/R aging: the property test -------------------------------------------------------

ROUNDS = 200
SEED = 20261006


@dataclass
class _Owed:
    due_on: date
    owed: Decimal


class _World:
    """Random charges, payments, refunds, waivers, voids and deposits, and what is owed on
    each charge, kept here from the operations alone (the oracle)."""

    def __init__(self, seed: int) -> None:
        self.random = random.Random(seed)  # noqa: S311 - test data, not security
        self.operators = [an_operator(f"Random {n}") for n in range(4)]
        self.owed: dict[uuid.UUID, _Owed] = {}
        self.rent: list[RentCharge] = []
        self.insurance: list[tuple[InsuranceCharge, Decimal]] = []
        self.insurance_payments: list[Payment] = []
        self.one_off: list[OneOffCharge] = []
        self.ran: Counter[str] = Counter()

    def amount(self, low: int = 1, high: int = 500_000) -> Decimal:
        return D(self.random.randint(low, high)) / D(100)

    def due(self) -> date:
        return TODAY + timedelta(days=self.random.randint(-200, 60))

    def operator(self) -> Operator | None:
        return self.random.choice([*self.operators, None])

    def lease(self) -> Any:
        operator = self.operator()
        status = CollectionStatus.ACTIVE if operator else CollectionStatus.PROSPECTIVE
        lease = a_lease(operator=operator or self.operators[0], status=status)
        if operator is None:
            lease.operator = None
            lease.save(update_fields=["operator"])
        return lease

    def add_rent(self) -> None:
        due = self.due()
        amount = self.amount()
        status = self.random.choice(list(RentChargeStatus.values))
        paid: Decimal = {
            str(RentChargeStatus.PAID): amount,
            str(RentChargeStatus.PARTIAL): (amount / 2).quantize(D("0.01")),
        }.get(status, ZERO)
        charge = RentCharge.objects.create(
            lease=self.lease(),
            period_month=due.replace(day=1),
            due_on=due,
            amount_due=amount,
            amount_paid=paid,
            status=status,
        )
        self.rent.append(charge)
        owed = ZERO if status == RentChargeStatus.WAIVED else amount - paid
        self.owed[charge.pk] = _Owed(due, owed)

    def pay_rent(self) -> None:
        open_rent = [c for c in self.rent if self.owed[c.pk].owed > 0]
        if not open_rent:
            return
        charge = self.random.choice(open_rent)
        pay = min(self.amount(), self.owed[charge.pk].owed)
        charge.amount_paid += pay
        charge.status = (
            RentChargeStatus.PAID if charge.amount_paid == charge.amount_due else charge.status
        )
        charge.save(update_fields=["amount_paid", "status"])
        a_payment(charge, str(pay), TODAY)
        self.owed[charge.pk].owed -= pay

    def refund_rent(self) -> None:
        paid = [c for c in self.rent if c.amount_paid > 0 and c.status != "waived"]
        if not paid:
            return
        charge = self.random.choice(paid)
        back = min(self.amount(), charge.amount_paid)
        charge.amount_paid -= back
        charge.status = RentChargeStatus.PARTIAL if charge.amount_paid else RentChargeStatus.UNPAID
        charge.save(update_fields=["amount_paid", "status"])
        self.owed[charge.pk].owed += back

    def waive_rent(self) -> None:
        if not self.rent:
            return
        charge = self.random.choice(self.rent)
        charge.status = RentChargeStatus.WAIVED
        charge.save(update_fields=["status"])
        self.owed[charge.pk].owed = ZERO

    def add_insurance(self) -> None:
        due = self.due()
        if self.random.random() < 0.2:
            InsuranceCharge.objects.create(
                lease=self.lease(), policy_year=due.year, due_on=due, needs_premium=True
            )
            return
        amount = self.amount()
        charge = an_insurance_charge(self.lease(), due.year, due, str(amount), needs_premium=False)
        self.insurance.append((charge, amount))
        self.owed[charge.pk] = _Owed(due, amount)

    def pay_insurance(self) -> None:
        if not self.insurance:
            return
        charge, _ = self.random.choice(self.insurance)
        pay = self.amount()
        settled = self.random.random() < 0.8
        status = PaymentStatus.SETTLED if settled else PaymentStatus.PENDING
        payment = insurance_payment(charge, str(pay), TODAY, status=status)
        if settled:
            self.insurance_payments.append(payment)
            self.owed[charge.pk].owed -= pay

    def refund_insurance(self) -> None:
        if not self.insurance_payments:
            return
        payment = self.random.choice(self.insurance_payments)
        back = min(self.amount(), payment.amount)
        refund_of(payment, str(back), TODAY)
        assert payment.insurance_charge_id is not None
        self.owed[payment.insurance_charge_id].owed += back

    def add_one_off(self) -> None:
        due = self.due()
        price = self.amount()
        status = self.random.choice([OneOffChargeStatus.OWED, OneOffChargeStatus.PARTIAL])
        paid = (price / 3).quantize(D("0.01")) if status == OneOffChargeStatus.PARTIAL else ZERO
        operator = self.random.choice(self.operators)
        lease = self.lease() if self.random.random() < 0.5 else None
        charge = a_one_off_charge(
            operator,
            str(price),
            due,
            lease=lease,
            status=status,
            paid=str(paid),
        )
        self.one_off.append(charge)
        self.owed[charge.pk] = _Owed(due, price - paid)

    def close_one_off(self) -> None:
        live = [c for c in self.one_off if c.status not in ("waived", "voided")]
        if not live:
            return
        charge = self.random.choice(live)
        (waive if self.random.random() < 0.5 else void)(charge)
        charge.status = "waived"
        self.owed[charge.pk].owed = ZERO

    def add_deposit(self) -> None:
        charge = a_deposit_charge(self.operators[0], str(self.amount()), self.due())
        if self.random.random() < 0.5:
            one_off_payment(charge, "1.00", TODAY)
        self.owed[charge.pk] = _Owed(charge.due_date, ZERO)

    def step(self) -> None:
        operation = self.random.choice(
            [
                self.add_rent,
                self.add_rent,
                self.pay_rent,
                self.refund_rent,
                self.waive_rent,
                self.add_insurance,
                self.pay_insurance,
                self.refund_insurance,
                self.add_one_off,
                self.close_one_off,
                self.add_deposit,
            ]
        )
        operation()
        self.ran[operation.__name__] += 1

    def expected(self) -> tuple[Decimal, dict[str, Decimal], int]:
        buckets: dict[str, Decimal] = defaultdict(lambda: ZERO)
        count = 0
        for item in self.owed.values():
            if item.owed > 0:
                count += 1
                bucket = receivables.aging_bucket((TODAY - item.due_on).days)
                buckets[bucket] += item.owed
        return sum(buckets.values(), ZERO), dict(buckets), count


def test_the_aging_total_equals_the_dashboard_under_random_data(today: date) -> None:
    world = _World(SEED)
    for round_number in range(ROUNDS):
        world.step()
        total, buckets, count = world.expected()
        report = receivables.aging(TODAY)
        outstanding = receivables.dashboard(TODAY).outstanding
        assert report.totals.total == outstanding == total, round_number
        assert report.totals.count == count, round_number
        found = {name: amount for name, amount in report.totals.buckets.items() if amount}
        assert found == {name: amount for name, amount in buckets.items() if amount}, round_number
    assert len(world.owed) > 60 and len(world.ran) == 10, world.ran  # every operation ran


# --- Revenue by month -------------------------------------------------------------------


def test_a_charge_paid_in_a_later_month_is_billed_when_due_and_collected_when_settled(
    today: date,
) -> None:
    lease = a_lease(a_store("1"), debit_day=31)
    charge = a_rent_charge(lease, JUL, RentChargeStatus.PAID, "1200.00", "1200.00", day=31)
    a_payment(charge, "1200.00", date(2026, 8, 31))  # day 31: still August
    late = a_rent_charge(lease, AUG, RentChargeStatus.PARTIAL, "1200.00", "300.00", day=31)
    a_payment(late, "300.00", date(2026, 9, 1))  # the first of the next month
    revenue = reports.revenue_by_month(rng("2026-07", "2026-09"))
    july, august, september = revenue.months
    assert (july.rent_billed, july.rent_collected) == (D("1200.00"), ZERO)
    assert (august.rent_billed, august.rent_collected) == (D("1200.00"), D("1200.00"))
    assert (september.rent_billed, september.rent_collected) == (ZERO, D("300.00"))
    assert revenue.total.total_billed == D("2400.00")
    assert revenue.total.total_collected == D("1500.00")
    # The aging sees the August charge by its due date, 36 days past due.
    [row] = receivables.aging(TODAY).rows
    assert row.buckets[receivables.AGING_31_60] == D("900.00")


def test_revenue_splits_rent_insurance_and_other_fees_without_deposits(today: date) -> None:
    operator = an_operator("Split")
    lease = a_lease(a_store("2"), operator)
    a_rent_charge(lease, AUG, RentChargeStatus.WAIVED, "999.00")  # not billed
    insurance = an_insurance_charge(lease, 2026, date(2026, 8, 10), "695.00")
    paid = insurance_payment(insurance, "695.00", date(2026, 8, 12))
    refund_of(paid, "95.00", date(2026, 8, 20))
    fee = a_one_off_charge(operator, "150.00", date(2026, 8, 15), lease=lease, paid="150.00")
    one_off_payment(fee, "150.00", date(2026, 8, 16))
    void(a_one_off_charge(operator, "40.00", date(2026, 8, 15)))  # nowhere
    deposit = a_deposit_charge(operator, "2000.00", date(2026, 8, 1), paid="2000.00")
    one_off_payment(deposit, "2000.00", date(2026, 8, 2))
    [august] = reports.revenue_by_month(rng("2026-08", "2026-08")).months
    assert (august.rent_billed, august.rent_collected) == (ZERO, ZERO)
    assert (august.insurance_billed, august.insurance_collected) == (D("695.00"), D("600.00"))
    assert (august.fees_billed, august.fees_collected) == (D("150.00"), D("150.00"))
    assert (august.total_billed, august.total_collected) == (D("845.00"), D("750.00"))
    totals = pnl.total(pnl.rows(pnl.PnlQuery(AUG, AUG)))
    assert august.total_billed == totals.rent_billed + totals.fees_billed
    assert august.total_collected == totals.rent_collected + totals.fees_collected


def test_every_month_of_the_range_has_a_line_even_without_charges(today: date) -> None:
    revenue = reports.revenue_by_month(rng("2026-01", "2026-03"))
    assert [item.month for item in revenue.months] == [date(2026, m, 1) for m in (1, 2, 3)]
    assert revenue.total.total_billed == ZERO
    params = {"from": "2026-01", "to": "2026-03"}
    records = dicts_of(viewer().get("/reports/revenue/export.csv", params))
    assert [record["Month"] for record in records] == ["2026-01", "2026-02", "2026-03", "Total"]


def test_a_settlement_at_2359_new_york_on_the_last_day() -> None:
    """The settled date is a date column, written by the transaction handler in the
    project's time zone; the report counts that date's month. 23:59 in New York on
    31 August is 03:59 UTC on 1 September."""
    moment = datetime(2026, 9, 1, 3, 59, tzinfo=UTC)
    settled_on = transaction_handler._local_date(moment)
    assert settled_on == timezone.localdate(moment)
    charge = a_rent_charge(a_lease(), AUG, RentChargeStatus.PAID, "100.00", "100.00")
    a_payment(charge, "100.00", settled_on)
    august, september = reports.revenue_by_month(rng("2026-08", "2026-09")).months
    by_month = {august.month: august.rent_collected, september.month: september.rent_collected}
    assert by_month[settled_on.replace(day=1)] == D("100.00")
    assert sum(by_month.values(), ZERO) == D("100.00")
    # The report follows the stored date exactly: the last day of August counts in August.
    a_payment(charge, "1.00", date(2026, 8, 31))
    [only] = reports.revenue_by_month(rng("2026-08", "2026-08")).months
    assert only.rent_collected - by_month[AUG] == D("1.00")


def test_the_revenue_page_says_settled_month_and_due_date(today: date) -> None:
    page = viewer().get("/reports/revenue/").content.decode()
    assert 'id="revenue-basis"' in page
    assert "by settled month" in page and "aging is by due date" in page


# --- Payments by method -----------------------------------------------------------------


def test_payments_by_method_per_month_net_of_refunds_without_deposits(today: date) -> None:
    lease = a_lease()
    charge = a_rent_charge(lease, AUG, RentChargeStatus.PAID, "3000.00", "3000.00")
    ach = a_payment(charge, "1000.00", date(2026, 8, 5))
    refund_of(ach, "100.00", date(2026, 8, 25))
    Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ZELLE,
        amount=D("500.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 8, 31),
        note="Zelle, marked at the provider as cash",
    )
    Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.WIRE,
        amount=D("700.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 9, 1),
        note="Wire",
    )
    a_payment(charge, "999.00", None, PaymentStatus.PENDING, initiated_on=date(2026, 8, 6))
    a_payment(charge, "888.00", date(2026, 8, 7), PaymentStatus.RETURNED)
    deposit = a_deposit_charge(lease.operator, "2000.00", date(2026, 8, 1))  # type: ignore[arg-type]
    one_off_payment(deposit, "2000.00", date(2026, 8, 2))
    report = reports.payments_by_method(rng("2026-08", "2026-09"))
    august, september = report.months
    assert august.amounts == {
        "ach": D("900.00"),
        "zelle": D("500.00"),
        "wire": ZERO,
        "other": ZERO,
    }
    assert (august.total, august.count) == (D("1400.00"), 2)
    assert (september.amounts["wire"], september.total) == (D("700.00"), D("700.00"))
    assert august.total == receivables.paid_in(AUG, date(2026, 8, 31))
    assert report.total.total == D("2100.00")


def test_the_payments_by_method_page_notes_cards_and_cash(today: date) -> None:
    page = viewer().get("/reports/payments-by-method/").content.decode()
    assert 'id="cards-note"' in page
    assert "Cards do not exist in this app" in page and "cash or check" in page
    records = dicts_of(viewer().get("/reports/payments-by-method/export.csv"))
    assert list(records[0]) == list(reports.PAYMENTS_BY_METHOD_CSV_HEADER)


# --- Deposits versus transactions -------------------------------------------------------


def a_bank_deposit(day: date, gross: str, fees: str, ref: str = "") -> Deposit:
    return Deposit.objects.create(
        provider="stax_pay",
        provider_deposit_id=ref or f"dep-{uuid.uuid4().hex}",
        deposit_date=day,
        gross=D(gross),
        fees=D(fees),
        net=D(gross) - D(fees),
        status="settled",
    )


def a_finding(kind: str, entity_type: str, entity_id: uuid.UUID, resolved: bool = False) -> None:
    run = ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.SCHEDULED,
        status=ReconciliationStatus.DONE,
        started_at=datetime(2026, 10, 1, 6, tzinfo=UTC),
    )
    ReconciliationMismatch.objects.create(
        run=run,
        kind=kind,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=f"{kind} detail",
        resolved_at=timezone.now() if resolved else None,
        resolution_note="Checked" if resolved else "",
    )


def test_deposits_against_settled_ach_with_unmatched_items_highlighted(today: date) -> None:
    charge = a_rent_charge(a_lease(), AUG, RentChargeStatus.PAID, "1000.00", "1000.00")
    paid = a_payment(charge, "1000.00", date(2026, 8, 5))
    refund_of(paid, "50.00", date(2026, 8, 20))
    a_bank_deposit(date(2026, 8, 6), "1000.00", "2.50", "dep-0123456789abcdef")
    a_bank_deposit(date(2026, 7, 31), "10.00", "0.00")
    a_finding(MismatchKind.SETTLED_PAYMENT_UNDEPOSITED, "payment", paid.pk)
    a_finding(MismatchKind.DEPOSIT_UNMATCHED, "provider_transaction", uuid.uuid4())
    a_finding(MismatchKind.DEPOSIT_UNMATCHED, "payment", uuid.uuid4(), resolved=True)
    a_finding(MismatchKind.PENDING_TOO_OLD, "payment", uuid.uuid4())
    report = reports.deposits_vs_transactions(rng("2026-08", "2026-08"))
    [august] = report.months
    assert (august.count, august.gross, august.fees, august.net) == (
        1,
        D("1000.00"),
        D("2.50"),
        D("997.50"),
    )
    assert (august.ach_paid, august.ach_refunded, august.difference) == (
        D("1000.00"),
        D("50.00"),
        D("50.00"),
    )
    assert [line.reference for line in report.deposits] == ["dep-…cdef"]
    assert {item.kind for item in report.unmatched} == set(reports.UNMATCHED_KINDS)
    assert len(report.unmatched) == 2
    page = viewer().get("/reports/deposits/", {"from": "2026-08", "to": "2026-08"})
    html = page.content.decode()
    assert html.count('class="unmatched"') == 2
    assert 'href="/reconciliation/"' in html and f'href="/payments/{paid.pk}/"' in html
    assert "dep-0123456789abcdef" not in html
    records = dicts_of(
        viewer().get("/reports/deposits/export.csv", {"from": "2026-08", "to": "2026-08"})
    )
    assert [record["Line"] for record in records] == [
        "Deposit",
        "Month",
        "Total",
        "Unmatched",
        "Unmatched",
    ]
    assert records[1]["Difference (USD)"] == "50.00"
    assert "dep-0123456789abcdef" not in str(records)


# --- The customer statement -------------------------------------------------------------


def build_statement() -> dict[str, Any]:
    operator = an_operator("Statement Operator")
    other = an_operator("Other Operator")
    first = a_lease(a_store("Store A"), operator)
    second = a_lease(a_store("Store B"), operator)
    a_rent_charge(first, SEP, RentChargeStatus.UNPAID, "1000.00")
    paid = a_rent_charge(second, AUG, RentChargeStatus.PAID, "800.00", "800.00")
    a_payment(paid, "800.00", date(2026, 8, 9))
    a_payment(paid, "20.00", None, PaymentStatus.PENDING, initiated_on=date(2026, 9, 9))
    old = a_rent_charge(second, date(2026, 1, 1), RentChargeStatus.PAID, "800.00", "800.00")
    a_payment(old, "800.00", date(2026, 1, 9))  # outside the range
    an_insurance_charge(second, 2026, date(2026, 9, 15))
    deposit = a_deposit_charge(operator, "1500.00", date(2026, 8, 1), paid="1500.00")
    one_off_payment(deposit, "1500.00", date(2026, 8, 2))
    others = a_lease(a_store("Store C"), other)
    theirs = a_rent_charge(others, SEP, RentChargeStatus.PAID, "999.00", "999.00")
    a_payment(theirs, "999.00", date(2026, 9, 9))
    a_rent_charge(others, OCT, RentChargeStatus.UNPAID, "777.00")
    # A charge billed to the other operator on a lease of this one is the other's.
    billed_elsewhere = a_one_off_charge(other, "55.00", date(2026, 9, 1), lease=first)
    one_off_payment(billed_elsewhere, "5.00", date(2026, 9, 2))
    return {"operator": operator, "other": other}


def test_the_statement_covers_every_store_and_never_another_operator(today: date) -> None:
    built = build_statement()
    statement = reports.statement(built["operator"], rng("2026-08", "2026-10"), TODAY)
    assert {item.store_label for item in statement.open_charges} == {"Store A", "Store B"}
    assert [item.description for item in statement.open_charges] == [
        "Rent 2026-09",
        "Insurance, policy year 2026",
    ]
    assert statement.open_total == D("1695.00") and statement.overdue_total == D("1695.00")
    kinds = [(line.kind, line.payment.amount) for line in statement.history]
    assert kinds == [
        (reports.KIND_DEPOSIT_HELD, D("1500.00")),
        (reports.KIND_PAYMENT, D("800.00")),
        (reports.KIND_PAYMENT, D("20.00")),
    ]
    assert (statement.paid, statement.pending, statement.deposits_held) == (
        D("800.00"),
        D("20.00"),
        D("1500.00"),
    )
    client = viewer()
    page = client.get(f"/operators/{built['operator'].pk}/statement/", {"from": "2026-08"})
    html = page.content.decode()
    assert page.status_code == 200
    for absent in ("Store C", "999.00", "777.00", "55.00", "Other Operator"):
        assert absent not in html, absent
    assert "@media print" in html and "nav, .no-print" in html


def test_the_statement_csv(today: date) -> None:
    built = build_statement()
    path = f"/operators/{built['operator'].pk}/statement/export.csv"
    records = dicts_of(viewer().get(path, {"from": "2026-08", "to": "2026-10"}))
    sections = [record["Section"] for record in records]
    assert sections == ["Open charge"] * 2 + ["Payment history"] * 3 + ["Total"] * 6
    assert records[0]["Balance (USD)"] == "1000.00" and records[0]["Due date"] == "2026-09-05"
    totals = {record["Description"]: record["Amount (USD)"] for record in records[5:]}
    assert totals["Open balance"] == "1695.00"
    assert totals["Deposits held from the range"] == "1500.00"
    assert all("Store C" not in value for record in records for value in record.values())


def test_an_archived_operator_still_has_a_statement(today: date) -> None:
    built = build_statement()
    operator = built["operator"]
    operator.archived_at = timezone.now()
    operator.save(update_fields=["archived_at"])
    client = viewer()
    page = client.get(f"/operators/{operator.pk}/statement/")
    assert page.status_code == 200 and "Archived" in page.content.decode()
    assert rows_of(client.get(f"/operators/{operator.pk}/statement/export.csv"))


def test_the_statement_of_an_unknown_operator_is_404(today: date) -> None:
    client = viewer()
    for pk in (str(uuid.uuid4()), "not-a-uuid"):
        assert client.get(f"/operators/{pk}/statement/").status_code == 404
        assert client.get(f"/operators/{pk}/statement/export.csv").status_code == 404


def test_the_operator_page_links_to_its_statement(today: date) -> None:
    operator = an_operator("Linked")
    page = viewer().get(f"/operators/{operator.pk}/").content.decode()
    assert f'href="/operators/{operator.pk}/statement/"' in page


# --- Ranges, the menu and roles ---------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"from": "1000-01", "to": "3000-12"},
        {"from": "2000-01", "to": "2100-12"},
        {"from": "2026-09", "to": "2026-03"},
        {"from": "%%%", "to": "' OR 1=1 --"},
        {"from": "2026-13", "to": "\x00"},
    ],
)
def test_bad_ranges_answer_200_with_a_message_on_every_ranged_report(
    today: date, params: dict[str, str]
) -> None:
    operator = an_operator("Range")
    client = viewer()
    for path in (*RANGED, f"/operators/{operator.pk}/statement/"):
        response = client.get(path, params)
        assert response.status_code == 200, path
        assert 'id="report-messages"' in response.content.decode(), path
        export = client.get(f"{path}export.csv", params)
        rows_of(export)


def test_the_range_is_capped_at_the_pnl_maximum(today: date) -> None:
    selected = reports.parse_range({"from": "2000-01", "to": "2026-10"}, TODAY)
    assert len(selected.months) == pnl.MAX_MONTHS
    assert selected.to_month == OCT and selected.messages
    default = reports.parse_range({}, TODAY)
    assert (default.from_month, default.to_month, default.messages) == (JUL, OCT, ())


def test_the_menu_lists_every_report_and_the_nav_links_to_it(today: date) -> None:
    page = viewer().get("/reports/").content.decode()
    for path in ("/reports/aging/", *RANGED, "/reports/pnl/", "/operators/"):
        assert f'href="{path}"' in page, path
    assert 'id="nav-reports"' in page


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_report_is_a_read_route_for_every_role(today: date, role: Role) -> None:
    operator = an_operator("Roles")
    client, _ = client_for(role)
    paths = ("/reports/", "/reports/aging/", *RANGED, f"/operators/{operator.pk}/statement/")
    for path in paths:
        assert client.get(path).status_code == 200, (role, path)
        assert client.post(path).status_code == 403, (role, path)


def test_without_a_role_or_session_the_reports_are_refused(today: date) -> None:
    operator = an_operator("Nobody")
    for path in ("/reports/", "/reports/aging/", *RANGED, f"/operators/{operator.pk}/statement/"):
        assert Client().get(path).status_code == 401, path
        assert client_for(None)[0].get(path).status_code == 403, path


def test_a_cancelled_report_answers_200_with_the_sentence(
    today: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ledger.views import reports as report_views

    def too_slow(compute: Any) -> Any:
        raise report_views.ReportTooSlow

    monkeypatch.setattr(report_views, "bounded", too_slow)
    operator = an_operator("Slow")
    client = viewer()
    for path in ("/reports/aging/", *RANGED, f"/operators/{operator.pk}/statement/"):
        for target in (path, f"{path}export.csv"):
            response = client.get(target)
            assert response.status_code == 200 and not response.streaming, target
            assert report_views.TOO_SLOW in response.content.decode(), target


# --- Exact money (D7) -------------------------------------------------------------------


def test_tenths_add_to_exactly_thirty_cents_through_every_report_csv(today: date) -> None:
    operator = an_operator("Cents")
    lease = a_lease(a_store("Cents"), operator)
    for amount, month in (("0.10", JUL), ("0.20", AUG)):
        charge = a_rent_charge(lease, month, RentChargeStatus.UNPAID, amount)
        a_payment(charge, amount, date(2026, 8, 10))
    for amount, day in (("0.10", 3), ("0.20", 4)):
        a_bank_deposit(date(2026, 8, day), amount, "0.00")
    client = viewer()
    params = {"from": "2026-07", "to": "2026-08"}
    expected = D("0.10") + D("0.20")
    assert expected == D("0.30")

    aging = dicts_of(client.get("/reports/aging/export.csv"))
    assert D(aging[-1]["Total (USD)"]) == expected
    revenue = dicts_of(client.get("/reports/revenue/export.csv", params))
    assert D(revenue[-1]["Rent billed (USD)"]) == expected
    assert D(revenue[-1]["Rent collected (USD)"]) == expected
    by_method = dicts_of(client.get("/reports/payments-by-method/export.csv", params))
    assert D(by_method[-1]["ACH (USD)"]) == expected
    deposits = dicts_of(client.get("/reports/deposits/export.csv", params))
    total = next(record for record in deposits if record["Line"] == "Total")
    assert D(total["Gross (USD)"]) == D(total["Settled ACH payments (USD)"]) == expected
    statement = dicts_of(client.get(f"/operators/{operator.pk}/statement/export.csv", params))
    totals = {r["Description"]: r for r in statement if r["Section"] == "Total"}
    assert D(totals["Open balance"]["Amount (USD)"]) == expected
    assert D(totals["Paid in the range"]["Amount (USD)"]) == expected
    pnl_rows = dicts_of(client.get("/reports/pnl/export.csv", params))
    assert D(pnl_rows[-1]["Rent billed (USD)"]) == expected
    arrears = dicts_of(client.get("/arrears/export.csv"))
    assert D(arrears[0]["Total owed (USD)"]) == expected
    payments = dicts_of(client.get("/payments/export.csv"))
    assert sum((D(row["Amount (USD)"]) for row in payments), ZERO) == expected
    charges = dicts_of(client.get("/charges/export.csv", {"lease": str(lease.pk)}))
    assert sum((D(row["Balance (USD)"]) for row in charges), ZERO) == expected


# --- Queries ----------------------------------------------------------------------------


def _counts(operator: Operator) -> dict[str, int]:
    selected = rng("2026-01", "2026-10")
    found = {}
    calls: dict[str, Callable[[], object]] = {
        "aging": lambda: receivables.aging(TODAY),
        "revenue": lambda: reports.revenue_by_month(selected),
        "by_method": lambda: reports.payments_by_method(selected),
        "deposits": lambda: reports.deposits_vs_transactions(selected),
        "statement": lambda: reports.statement(operator, selected, TODAY),
    }
    for name, call in calls.items():
        with CaptureQueriesContext(connection) as captured:
            call()
        found[name] = len(captured.captured_queries)
    return found


def _more(operator: Operator, count: int) -> None:
    for n in range(count):
        lease = a_lease(a_store(), operator)
        for month in (JUL, AUG, SEP):
            charge = a_rent_charge(lease, month, RentChargeStatus.PAID, "100.00", "100.00")
            a_payment(charge, "100.00", month.replace(day=9))
        an_insurance_charge(lease, 2026, date(2026, 9, 1))
        a_one_off_charge(operator, "10.00", date(2026, 9, 1), lease=lease)
        a_bank_deposit(date(2026, 8, 1 + n % 28), "100.00", "1.00")


def test_the_report_query_counts_do_not_grow_with_the_data(today: date) -> None:
    operator = an_operator("Counted")
    _more(operator, 2)
    small = _counts(operator)
    _more(operator, 15)
    assert _counts(operator) == small
    assert small == {"aging": 3, "revenue": 13, "by_method": 1, "deposits": 3, "statement": 4}


def test_a_pending_charge_is_owed_and_current_until_due(today: date) -> None:
    """Aging follows open_balances exactly: a pending rent charge is still owed."""
    lease = a_lease()
    a_rent_charge(lease, OCT, RentChargeStatus.PENDING, "500.00", day=10)
    assert receivables.aging(TODAY).totals.buckets[receivables.AGING_CURRENT] == D("500.00")
