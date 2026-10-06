"""The profit and loss report (docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md Batch C: D11,
D12; §6 test plan, §7 performance).

The arithmetic of a known store, unknown costs, two leases in one month, the
first-of-month cost rule, deposits (never counted, except their withheld part in the
resolution month), voided, returned and pending payments, waived charges, cost-only
months, the query bounds, the page, the CSV (equal to the page and formula-safe), the
role gates, the number of queries and the time on 500 stores.
"""

from __future__ import annotations

import csv
import io
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    CollectionStatus,
    DepositResolution,
    DepositResolutionKind,
    FeeType,
    InsuranceCharge,
    InsurancePremium,
    Lease,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    Store,
    StoreCost,
    StoreCostSource,
    StoreStatus,
)
from ledger.services import pnl
from tests.test_operator_views import client_for
from tests.test_setup_charges import deposit_type, fee

pytestmark = pytest.mark.django_db

D = Decimal
JULY = date(2026, 7, 1)
AUGUST = date(2026, 8, 1)
SEPTEMBER = date(2026, 9, 1)
JAN = date(2026, 1, 1)
JULY_NOON = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
PAGE = "/reports/pnl/"
EXPORT = "/reports/pnl/export.csv"


# --- Builders ----------------------------------------------------------------------------


def a_store(label: str = "Store 1796") -> Store:
    return Store.objects.create(external_label=label, status=StoreStatus.ACTIVE)


def an_operator(name: str = "Northwind Stores") -> Operator:
    return Operator.objects.create(display_name=name, email=f"{uuid.uuid4().hex[:8]}@example.com")


def a_lease(
    store: Store,
    operator: Operator | None = None,
    started_on: date | None = JAN,
    ended_on: date | None = None,
    **fields: Any,
) -> Lease:
    values: dict[str, Any] = {
        "store": store,
        "operator": operator if operator is not None else an_operator(),
        "debit_day": 1,
        "payment_channel": PaymentChannel.WIRE,
        "collection_status": CollectionStatus.ENDED if ended_on else CollectionStatus.ACTIVE,
        "started_on": started_on,
        "ended_on": ended_on,
    }
    values.update(fields)
    return Lease.objects.create(**values)


def rent(lease: Lease, month: date, amount: str = "3500.00", **fields: Any) -> RentCharge:
    values: dict[str, Any] = {
        "lease": lease,
        "period_month": month,
        "due_on": month,
        "amount_due": D(amount),
        "status": RentChargeStatus.DUE,
    }
    values.update(fields)
    return RentCharge.objects.create(**values)


def paid(
    charge: RentCharge | OneOffCharge | InsuranceCharge, amount: str, on: date, **fields: Any
) -> Payment:
    values: dict[str, Any] = {
        "method": PaymentMethod.ACH,
        "amount": D(amount),
        "status": PaymentStatus.SETTLED,
        "settled_on": on,
        "initiated_on": on,
    }
    if isinstance(charge, RentCharge):
        values.update(rent_charge=charge, kind=PaymentKind.RENT)
    elif isinstance(charge, OneOffCharge):
        values.update(one_off_charge=charge, kind=PaymentKind.ONE_OFF)
    else:
        values.update(
            insurance_charge=charge, kind=PaymentKind.INSURANCE, policy_year=charge.policy_year
        )
    values.update(fields)
    return Payment.objects.create(**values)


def refund_of(payment: Payment, amount: str, on: date, **fields: Any) -> Payment:
    values: dict[str, Any] = {
        "method": PaymentMethod.ACH,
        "amount": D(amount),
        "status": PaymentStatus.SETTLED,
        "settled_on": on,
        "is_refund": True,
        "parent_payment": payment,
        "kind": payment.kind,
        "policy_year": payment.policy_year,
        "rent_charge": payment.rent_charge,
        "insurance_charge": payment.insurance_charge,
        "one_off_charge": payment.one_off_charge,
    }
    values.update(fields)
    return Payment.objects.create(**values)


def a_setup(
    lease: Lease,
    fee_type: FeeType | None = None,
    price: str = "1000.00",
    cost: str | None = "500.00",
    due: date = date(2026, 7, 10),
    **fields: Any,
) -> OneOffCharge:
    return OneOffCharge.objects.create(
        lease=lease,
        fee_type=fee_type or fee("sign_fee"),
        price=D(price),
        cost=D(cost) if cost is not None else None,
        due_date=due,
        **fields,
    )


def a_cost(
    store: Store,
    landlord: str = "2500.00",
    starts: date = JAN,
    ends: date | None = None,
    **fields: Any,
) -> StoreCost:
    return StoreCost.objects.create(
        store=store,
        landlord_rent=D(landlord),
        effective_from=starts,
        effective_to=ends,
        source=StoreCostSource.STAFF,
        **fields,
    )


def premium(year: int, cost: str | None, amount: str = "695.00") -> InsurancePremium:
    row, _ = InsurancePremium.objects.update_or_create(
        policy_year=year,
        defaults={"amount": D(amount), "cost": D(cost) if cost is not None else None},
    )
    return row


def insurance(lease: Lease, year: int = 2026, due: date = date(2026, 7, 1)) -> InsuranceCharge:
    return InsuranceCharge.objects.create(
        lease=lease, policy_year=year, due_on=due, amount=D("695.00")
    )


def query(start: date = JULY, end: date | None = None, **fields: Any) -> pnl.PnlQuery:
    return pnl.PnlQuery(from_month=start, to_month=end or start, **fields)


def only(rows: list[pnl.PnlRow]) -> pnl.PnlRow:
    assert len(rows) == 1, rows
    return rows[0]


def month_rows(month: date, start: date = JULY, end: date | None = None) -> list[pnl.PnlRow]:
    return [row for row in pnl.rows(query(start, end)) if row.month == month]


def known_store() -> tuple[Store, Lease, RentCharge, OneOffCharge]:
    """The plan's example: rent 3,500, landlord 2,500, sign fee price 1,000 cost 500
    due in July."""
    store = a_store()
    lease = a_lease(store)
    a_cost(store)
    return store, lease, rent(lease, JULY), a_setup(lease)


# --- Arithmetic (D11) --------------------------------------------------------------------


def test_the_known_store_has_a_billed_margin_of_1500() -> None:
    _, lease, _, _ = known_store()
    row = only(pnl.rows(query()))
    assert (row.operator_id, row.month, row.carries_store_costs) == (lease.operator_id, JULY, True)
    assert (row.rent_billed, row.fees_billed) == (D("3500.00"), D("1000.00"))
    assert (row.landlord_rent, row.other_costs, row.fee_costs) == (
        D("2500.00"),
        D("0.00"),
        D("500.00"),
    )
    assert row.margin_billed == D("1500.00")
    assert row.unknown == () and not row.cost_only


def test_the_collected_basis_counts_what_was_settled_including_a_partial_payment() -> None:
    _, _, charge, sign = known_store()
    paid(charge, "2000.00", date(2026, 7, 5))
    paid(sign, "400.00", date(2026, 7, 20), method=PaymentMethod.WIRE, note="Wire.")
    row = only(pnl.rows(query()))
    assert (row.rent_collected, row.fees_collected) == (D("2000.00"), D("400.00"))
    # 2,000 + 400 - (2,500 + 0 + 500)
    assert row.margin_collected == D("-600.00")
    assert row.margin_billed == D("1500.00")


def test_other_monthly_cost_is_a_cost() -> None:
    store, _, _, _ = known_store()
    StoreCost.objects.filter(store=store).update(other_monthly_cost=D("50.00"))
    row = only(pnl.rows(query()))
    assert (row.other_costs, row.margin_billed) == (D("50.00"), D("1450.00"))


def test_a_payment_counts_in_its_settled_month_not_the_rent_month() -> None:
    _, _, charge, _ = known_store()
    paid(charge, "3500.00", date(2026, 8, 2))
    july, august = (only(month_rows(month, JULY, AUGUST)) for month in (JULY, AUGUST))
    assert (july.rent_billed, july.rent_collected) == (D("3500.00"), D("0.00"))
    assert (august.rent_billed, august.rent_collected) == (D("0.00"), D("3500.00"))


def test_insurance_is_billed_by_due_date_with_the_premium_cost() -> None:
    store = a_store()
    lease = a_lease(store)
    a_cost(store)
    premium(2026, "300.00")
    charge = insurance(lease, 2026, date(2026, 7, 15))
    paid(charge, "695.00", date(2026, 7, 16))
    row = only(pnl.rows(query()))
    assert (row.fees_billed, row.fees_collected, row.fee_costs) == (
        D("695.00"),
        D("695.00"),
        D("300.00"),
    )
    assert row.margin_billed == D("695.00") - D("2500.00") - D("300.00")


def test_an_insurance_charge_without_an_amount_is_not_billed() -> None:
    store = a_store()
    lease = a_lease(store)
    a_cost(store)
    InsuranceCharge.objects.create(lease=lease, policy_year=2026, due_on=JULY, amount=None)
    row = only(pnl.rows(query()))
    assert (row.fees_billed, row.fee_costs) == (D("0.00"), D("0.00"))


# --- Unknown propagation ------------------------------------------------------------------


def test_no_cost_row_makes_landlord_rent_other_costs_and_both_margins_unknown() -> None:
    store = a_store()
    rent(a_lease(store), JULY)
    row = only(pnl.rows(query()))
    assert (row.landlord_rent, row.other_costs) == (None, None)
    assert (row.margin_billed, row.margin_collected) == (None, None)
    assert row.unknown == (pnl.LANDLORD_RENT, pnl.OTHER_COSTS)
    assert row.rent_billed == D("3500.00")


def test_a_cost_row_starting_later_in_the_month_does_not_count_for_that_month() -> None:
    store = a_store()
    rent(a_lease(store), JULY)
    a_cost(store, starts=date(2026, 7, 2))
    assert only(pnl.rows(query())).landlord_rent is None


def test_a_null_fee_cost_makes_fee_costs_unknown() -> None:
    _, lease, _, _ = known_store()
    a_setup(lease, fee("architecture_fee"), price="800.00", cost=None)
    row = only(pnl.rows(query()))
    assert row.fees_billed == D("1800.00")
    assert (row.fee_costs, row.margin_billed, row.margin_collected) == (None, None, None)
    assert row.unknown == (pnl.FEE_COSTS,)


@pytest.mark.parametrize("year_cost", [None, "missing"])
def test_a_null_or_missing_premium_cost_makes_fee_costs_unknown(year_cost: str | None) -> None:
    store = a_store()
    lease = a_lease(store)
    a_cost(store)
    year = 2026
    if year_cost is None:
        premium(year, None)
    else:
        year = 2031
        InsurancePremium.objects.filter(policy_year=year).delete()
    insurance(lease, year, date(2026, 7, 1))
    row = only(pnl.rows(query()))
    assert (row.fees_billed, row.fee_costs, row.margin_billed) == (D("695.00"), None, None)


def test_an_unknown_input_makes_the_group_and_the_total_unknown_but_not_other_stores() -> None:
    known_store()
    rent(a_lease(a_store("Store 0437")), JULY)
    found = pnl.rows(query())
    groups = {group.label: group for group in pnl.grouped(found, pnl.GROUP_STORE)}
    assert groups["Store 1796"].margin_billed == D("1500.00")
    assert groups["Store 0437"].margin_billed is None
    total = pnl.total(found)
    assert (total.landlord_rent, total.margin_billed) == (None, None)
    assert total.rent_billed == D("7000.00")


# --- Leases and store costs across a month -------------------------------------------------


def test_two_leases_in_one_month_both_count_and_the_store_cost_counts_once() -> None:
    store = a_store()
    a_cost(store)
    old_operator, new_operator = an_operator("Old Operator"), an_operator("New Operator")
    old = a_lease(store, old_operator, ended_on=date(2026, 7, 14))
    new = a_lease(store, new_operator, started_on=date(2026, 7, 15))
    rent(old, JULY, "1750.00")
    rent(new, JULY, "2000.00")
    found = pnl.rows(query())
    assert {(row.operator, row.rent_billed) for row in found} == {
        ("Old Operator", D("1750.00")),
        ("New Operator", D("2000.00")),
    }
    by_operator = {row.operator: row for row in found}
    # The lease active on the first of the month carries the store's costs.
    assert by_operator["Old Operator"].landlord_rent == D("2500.00")
    assert by_operator["New Operator"].landlord_rent == D("0.00")
    assert not by_operator["New Operator"].carries_store_costs
    [store_group] = pnl.grouped(found, pnl.GROUP_STORE)
    assert (store_group.rent_billed, store_group.landlord_rent) == (D("3750.00"), D("2500.00"))
    assert store_group.margin_billed == D("1250.00")
    assert not any(row.cost_only for row in found)


def test_a_lease_starting_mid_month_carries_the_cost_when_none_was_active_on_the_first() -> None:
    store = a_store()
    a_cost(store)
    lease = a_lease(store, started_on=date(2026, 7, 31))
    row = only(pnl.rows(query()))
    assert (row.operator_id, row.landlord_rent, row.cost_only) == (
        lease.operator_id,
        D("2500.00"),
        False,
    )


def test_the_cost_row_in_effect_on_the_first_of_the_month_is_used() -> None:
    store = a_store()
    lease = a_lease(store)
    a_cost(store, "2500.00", JAN, date(2026, 7, 14))
    a_cost(store, "2800.00", date(2026, 7, 15))
    rent(lease, JULY)
    rent(lease, AUGUST)
    july, august = (only(month_rows(month, JULY, AUGUST)) for month in (JULY, AUGUST))
    assert (july.landlord_rent, august.landlord_rent) == (D("2500.00"), D("2800.00"))


# --- Deposits, voids, returns, waivers -----------------------------------------------------


def test_a_deposit_is_never_counted_and_is_held_in_the_footer() -> None:
    store = a_store()
    lease = a_lease(store)
    a_cost(store)
    deposit = a_setup(lease, deposit_type(), price="2500.00", cost=None)
    paid(deposit, "2500.00", date(2026, 7, 10))
    row = only(pnl.rows(query()))
    assert (row.fees_billed, row.fees_collected, row.fee_costs) == (
        D("0.00"),
        D("0.00"),
        D("0.00"),
    )
    assert row.margin_billed == D("-2500.00")
    assert pnl.deposits_held(query()) == D("2500.00")


def test_a_withheld_deposit_counts_in_the_month_it_was_resolved() -> None:
    store = a_store()
    lease = a_lease(store, ended_on=date(2026, 7, 31))
    a_cost(store)
    deposit = a_setup(lease, deposit_type(), price="2500.00", cost=None)
    paid(deposit, "2500.00", date(2026, 1, 10))
    DepositResolution.objects.create(
        setup_charge=deposit,
        kind=DepositResolutionKind.REFUNDED,
        amount=D("2000.00"),
        withheld_amount=D("500.00"),
        resolved_on=date(2026, 9, 3),
        method=PaymentMethod.WIRE,
        note="Kept 500 for repairs.",
    )
    Payment.objects.create(
        one_off_charge=deposit,
        kind=PaymentKind.REFUND_DEPOSIT,
        method=PaymentMethod.WIRE,
        amount=D("2000.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 9, 3),
        note="Deposit paid back.",
    )
    found = pnl.rows(query(JULY, SEPTEMBER))
    september = [row for row in found if row.month == SEPTEMBER]
    revenue = [row for row in september if row.fees_collected]
    assert [(row.fees_collected, row.withheld_deposits) for row in revenue] == [
        (D("500.00"), D("500.00"))
    ]
    july = [row for row in found if row.month == JULY]
    assert sum((row.fees_collected for row in july), D("0")) == D("0.00")
    # Nothing is held once the deposit is resolved; before, all of it was.
    assert pnl.deposits_held(query(JULY, SEPTEMBER)) == D("0.00")
    assert pnl.deposits_held(query(JULY, AUGUST)) == D("2500.00")


def test_a_transferred_deposit_is_held_once() -> None:
    store = a_store()
    operator = an_operator()
    old = a_lease(store, operator, ended_on=date(2026, 6, 30))
    new = a_lease(a_store("Store 0437"), operator)
    deposit = a_setup(old, deposit_type(), price="2500.00", cost=None)
    paid(deposit, "2500.00", date(2026, 1, 10))
    DepositResolution.objects.create(
        setup_charge=deposit,
        kind=DepositResolutionKind.TRANSFERRED,
        amount=D("2500.00"),
        resolved_on=date(2026, 7, 2),
        to_lease=new,
        note="Moved to the new store.",
    )
    moved = a_setup(new, deposit_type(), price="2500.00", cost=None, due=date(2026, 7, 2))
    paid(moved, "2500.00", date(2026, 7, 2), method=PaymentMethod.OTHER, note="Transferred.")
    assert pnl.deposits_held(query()) == D("2500.00")
    assert all(row.fees_collected == D("0.00") for row in pnl.rows(query()))


@pytest.mark.parametrize(
    "status", [PaymentStatus.VOIDED, PaymentStatus.RETURNED, PaymentStatus.PENDING]
)
def test_voided_returned_and_pending_payments_are_not_collected(status: str) -> None:
    _, lease, charge, sign = known_store()
    paid(charge, "3500.00", date(2026, 7, 5), status=status)
    paid(sign, "1000.00", date(2026, 7, 5), status=status)
    charge_2026 = insurance(lease)
    paid(charge_2026, "695.00", date(2026, 7, 5), status=status)
    row = only(pnl.rows(query()))
    assert (row.rent_collected, row.fees_collected) == (D("0.00"), D("0.00"))


def test_a_settled_refund_is_subtracted_in_its_own_month_and_a_voided_one_is_not() -> None:
    _, _, charge, _ = known_store()
    payment = paid(charge, "3500.00", date(2026, 7, 5))
    refund_of(payment, "500.00", date(2026, 8, 3))
    refund_of(payment, "100.00", date(2026, 8, 4), status=PaymentStatus.VOIDED)
    july, august = (only(month_rows(month, JULY, AUGUST)) for month in (JULY, AUGUST))
    assert (july.rent_collected, august.rent_collected) == (D("3500.00"), D("-500.00"))


def test_waived_charges_are_not_billed_but_a_waived_items_cost_still_counts() -> None:
    store = a_store()
    lease = a_lease(store)
    a_cost(store)
    rent(lease, JULY, status=RentChargeStatus.WAIVED)
    a_setup(
        lease,
        status=OneOffChargeStatus.WAIVED,
        waived_at=JULY_NOON,
        waive_note="Waived as a welcome gesture.",
    )
    row = only(pnl.rows(query()))
    assert (row.rent_billed, row.fees_billed, row.fee_costs) == (
        D("0.00"),
        D("0.00"),
        D("500.00"),
    )


# --- Cost-only months ----------------------------------------------------------------------


def test_a_held_store_with_no_active_lease_is_a_cost_only_row() -> None:
    store = a_store()
    a_cost(store)
    old = a_lease(store, ended_on=date(2026, 5, 31))
    rent(old, date(2026, 5, 1))
    row = only(pnl.rows(query()))
    assert (row.operator_id, row.operator, row.cost_only, row.carries_store_costs) == (
        None,
        pnl.NO_OPERATOR,
        True,
        True,
    )
    assert (row.landlord_rent, row.margin_billed, row.margin_collected) == (
        D("2500.00"),
        D("-2500.00"),
        D("-2500.00"),
    )


def test_a_landlord_lease_not_yet_ended_holds_the_store_after_its_cost_row_closed() -> None:
    store = a_store()
    a_cost(store, starts=JAN, ends=date(2026, 6, 30), landlord_lease_end=date(2027, 12, 31))
    row = only(pnl.rows(query()))
    # Held, but no cost row in effect on the first: cost-only with an unknown rent.
    assert (row.cost_only, row.landlord_rent, row.margin_billed) == (True, None, None)


def test_a_store_with_a_lease_or_without_any_cost_row_is_never_cost_only() -> None:
    known_store()
    a_store("Store 0437")
    found = pnl.rows(query())
    assert [row.store for row in found] == ["Store 1796"]
    assert not found[0].cost_only


def test_a_late_payment_of_an_old_lease_in_a_cost_only_month_is_its_own_row() -> None:
    store = a_store()
    a_cost(store)
    old = a_lease(store, an_operator("Old Operator"), ended_on=date(2026, 5, 31))
    paid(rent(old, date(2026, 5, 1)), "3500.00", date(2026, 7, 2))
    found = pnl.rows(query())
    assert [
        (row.operator, row.cost_only, row.rent_collected, row.landlord_rent) for row in found
    ] == [
        (pnl.NO_OPERATOR, True, D("0.00"), D("2500.00")),
        ("Old Operator", False, D("3500.00"), D("0.00")),
    ]
    [group] = pnl.grouped(found, pnl.GROUP_STORE)
    assert (group.margin_collected, group.cost_only_count) == (D("1000.00"), 1)


def test_prospective_leases_and_ended_leases_without_a_date_are_not_active() -> None:
    store = a_store()
    a_cost(store)
    a_lease(store, collection_status=CollectionStatus.PROSPECTIVE, debit_day=None)
    a_lease(store, collection_status=CollectionStatus.ENDED)
    assert only(pnl.rows(query())).cost_only


# --- Groups ----------------------------------------------------------------------------------


def test_groups_by_operator_and_month_sum_the_rows() -> None:
    _, lease, charge, _ = known_store()
    rent(lease, AUGUST)
    paid(charge, "3500.00", date(2026, 7, 3))
    found = pnl.rows(query(JULY, AUGUST))
    [by_operator] = pnl.grouped(found, pnl.GROUP_OPERATOR)
    assert (by_operator.label, by_operator.rent_billed) == ("Northwind Stores", D("7000.00"))
    assert by_operator.margin_billed == D("1500.00") + D("3500.00") - D("2500.00")
    by_month = pnl.grouped(found, pnl.GROUP_MONTH)
    assert [(group.label, group.margin_collected) for group in by_month] == [
        ("2026-07", D("500.00")),
        ("2026-08", D("-2500.00")),
    ]


# --- The query (bounds) ----------------------------------------------------------------------

TODAY = date(2026, 10, 5)


def parse(**params: str) -> pnl.PnlQuery:
    return pnl.parse_query(params, today=TODAY)


def test_the_default_range_is_the_last_three_complete_months_and_the_current_one() -> None:
    selected = parse()
    assert (selected.from_month, selected.to_month, selected.group, selected.detail) == (
        date(2026, 7, 1),
        date(2026, 10, 1),
        pnl.GROUP_STORE,
        False,
    )
    assert selected.messages == () and len(selected.months) == 4
    assert selected.end == date(2026, 10, 31)


def test_a_thousand_year_range_is_capped_to_36_months() -> None:
    selected = parse(**{"from": "2000-01", "to": "2100-12"})
    assert (selected.from_month, selected.to_month) == (date(2098, 1, 1), date(2100, 12, 1))
    assert len(selected.months) == pnl.MAX_MONTHS
    assert selected.messages == ("At most 36 months are shown; showing 2098-01 to 2100-12.",)
    far = parse(**{"from": "1026-01", "to": "2026-01"})
    assert (far.from_month, far.to_month) == (date(2026, 7, 1), date(2026, 10, 1))
    assert far.messages == (
        "The months could not be read (use YYYY-MM); showing 2026-07 to 2026-10.",
    )


def test_exactly_36_months_is_allowed_and_37_is_capped() -> None:
    assert parse(**{"from": "2024-01", "to": "2026-12"}).messages == ()
    capped = parse(**{"from": "2023-12", "to": "2026-12"})
    assert capped.from_month == date(2024, 1, 1) and len(capped.messages) == 1


def test_to_before_from_is_swapped() -> None:
    selected = parse(**{"from": "2026-09", "to": "2026-03"})
    assert (selected.from_month, selected.to_month) == (date(2026, 3, 1), date(2026, 9, 1))
    assert selected.messages == (
        "The end month was before the start month; showing 2026-03 to 2026-09.",
    )


@pytest.mark.parametrize(
    "params",
    [
        {"from": "garbage"},
        {"to": "2026-13"},
        {"from": "2026-00"},
        {"from": "26-07"},
        {"from": "2026-7"},
        {"from": "2026-07-01"},
        {"from": "<script>", "to": "2026-08"},
        {"from": "2026-07", "to": "２０２６-０８"},
        {"from": "9999-12"},
    ],
)
def test_garbage_months_give_the_default_range_with_a_message(params: dict[str, str]) -> None:
    selected = parse(**params)
    assert (selected.from_month, selected.to_month) == (date(2026, 7, 1), date(2026, 10, 1))
    assert len(selected.messages) == 1 and "could not be read" in selected.messages[0]


def test_one_bound_alone_takes_the_other_from_the_default() -> None:
    assert parse(**{"from": "2026-01"}).to_month == date(2026, 10, 1)
    only_to = parse(**{"to": "2026-03"})
    assert (only_to.from_month, only_to.to_month) == (date(2025, 12, 1), date(2026, 3, 1))


def test_an_unknown_group_is_store_with_a_message_and_detail_is_only_1() -> None:
    selected = parse(group="evil", detail="yes")
    assert (selected.group, selected.detail, selected.messages) == (
        pnl.GROUP_STORE,
        False,
        (pnl.BAD_GROUP,),
    )
    assert parse(group="month", detail="1").as_query() == {
        "from": "2026-07",
        "to": "2026-10",
        "group": "month",
        "detail": "1",
    }


# --- The page and the CSV --------------------------------------------------------------------


def get(path: str, params: dict[str, str] | None = None, role: Role = Role.VIEWER) -> Any:
    client, _ = client_for(role)
    return client.get(path, params or {})


def csv_rows(response: Any) -> list[dict[str, str]]:
    body = b"".join(response.streaming_content).decode()
    return list(csv.DictReader(io.StringIO(body)))


def test_the_page_shows_the_known_store_with_its_margin() -> None:
    known_store()
    response = get(PAGE, {"from": "2026-07", "to": "2026-07"})
    assert response.status_code == 200
    page = response.content.decode()
    assert "Store 1796" in page and "1500.00" in page
    [group] = response.context["groups"]
    assert (group.label, group.margin_billed) == ("Store 1796", D("1500.00"))
    assert 'id="deposits-held-amount">0.00<' in page


def test_the_breakdown_lists_the_rows_and_highlights_cost_only_ones() -> None:
    store = a_store()
    a_cost(store)
    rent(a_lease(store, ended_on=date(2026, 7, 31)), JULY)
    params = {"from": "2026-07", "to": "2026-08"}
    page = get(PAGE, params).content.decode()
    assert 'class="pnl-row' not in page and "1 cost-only" in page
    detailed = get(PAGE, {**params, "detail": "1"}).content.decode()
    assert detailed.count('class="pnl-row"') == 1
    assert detailed.count('class="pnl-row cost-only"') == 1
    assert "Store 1796 · No operator · 2026-08" in detailed
    assert "background:#fff6dd" in detailed


def test_unknown_amounts_read_unknown_on_the_page() -> None:
    rent(a_lease(a_store()), JULY)
    page = get(PAGE, {"from": "2026-07", "to": "2026-07"}).content.decode()
    assert '<span class="unknown">unknown</span>' in page
    assert "Unknown: landlord rent, other costs" in page


@pytest.mark.parametrize(
    "params",
    [
        {"from": "1000-01", "to": "3000-12"},
        {"from": "2000-01", "to": "2100-12"},
        {"from": "2026-09", "to": "2026-03"},
        {"from": "%%%", "to": "' OR 1=1 --"},
        {"group": "x" * 5000, "detail": "\x00"},
    ],
)
def test_bad_parameters_answer_200_with_a_message_on_both_routes(params: dict[str, str]) -> None:
    response = get(PAGE, params)
    assert response.status_code == 200
    assert 'id="pnl-messages"' in response.content.decode()
    export = get(EXPORT, params)
    assert export.status_code == 200
    csv_rows(export)


def test_the_csv_equals_the_page_figures_for_every_grouping_and_the_breakdown() -> None:
    _, lease, charge, sign = known_store()
    paid(charge, "2000.00", date(2026, 7, 5))
    paid(sign, "400.00", date(2026, 8, 20))
    rent(lease, AUGUST)
    rent(a_lease(a_store("Store 0437")), JULY, "3000.00")
    for group in ("store", "operator", "month"):
        for detail in ("", "1"):
            params = {"from": "2026-07", "to": "2026-08", "group": group, "detail": detail}
            context = get(PAGE, params).context
            records = csv_rows(get(EXPORT, params))
            expected: list[tuple[str, Any]] = []
            for item in context["groups"]:
                if detail:
                    expected.extend(("detail", row) for row in item.rows)
                else:
                    expected.append((group, item))
            expected.append(("total", context["total"]))
            assert [record["level"] for record in records] == [level for level, _ in expected]
            for record, (_, item) in zip(records, expected, strict=True):
                for name in ("rent_billed", "rent_collected", "fees_billed", "fees_collected"):
                    assert record[name] == f"{getattr(item, name):.2f}"
                for name in (
                    "landlord_rent",
                    "other_costs",
                    "fee_costs",
                    "margin_billed",
                    "margin_collected",
                ):
                    value = getattr(item, name)
                    if value is None:
                        assert (record[name], record[f"{name}_known"]) == ("", "no")
                    else:
                        assert (record[name], record[f"{name}_known"]) == (f"{value:.2f}", "yes")
                assert record["unknown_inputs"] == "; ".join(item.unknown)


def test_the_csv_header_documents_unknown_and_the_cells_are_formula_safe() -> None:
    store = a_store('=HYPERLINK("http://example.com")')
    lease = a_lease(store, an_operator("+Plus Operator"))
    rent(lease, JULY)
    records = csv_rows(get(EXPORT, {"from": "2026-07", "to": "2026-07", "detail": "1"}))
    assert list(records[0]) == list(pnl.CSV_HEADER)
    detail = records[0]
    assert detail["store"] == '\'=HYPERLINK("http://example.com")'
    assert detail["operator"] == "'+Plus Operator"
    assert (detail["landlord_rent"], detail["landlord_rent_known"]) == ("", "no")
    assert (detail["margin_billed"], detail["margin_billed_known"]) == ("", "no")
    assert detail["unknown_inputs"] == "landlord rent; other costs"
    assert detail["cost_only_months"] == "0"
    assert records[-1]["level"] == "total"


def test_the_export_is_a_streamed_csv_attachment() -> None:
    response = get(EXPORT, {"from": "2026-07", "to": "2026-08"})
    assert response.streaming
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith(
        'attachment; filename="pnl-2026-07-to-2026-08-'
    )
    assert response["Cache-Control"] == "no-store"


def test_the_nav_links_to_the_report_next_to_payments() -> None:
    page = get("/payments/").content.decode()
    payments_at = page.index('href="/payments/">Payments</a>')
    assert page.index('href="/reports/pnl/">Profit and loss</a>') > payments_at


# --- Roles (D12) ------------------------------------------------------------------------------


def test_both_routes_are_read_only_in_the_registry() -> None:
    for name in ("ledger:reports:pnl", "ledger:reports:pnl_export"):
        assert name in registry.READ_ONLY_ROUTE_NAMES
        assert registry.rule_for(name) is None


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_can_read_both_routes(role: Role) -> None:
    client, _ = client_for(role)
    assert client.get(PAGE).status_code == 200
    assert client.get(EXPORT).status_code == 200
    assert client.head(PAGE).status_code == 200


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
@pytest.mark.parametrize("path", [PAGE, EXPORT])
def test_no_role_can_post_put_or_delete(role: Role, path: str) -> None:
    client, _ = client_for(role)
    for method in (client.post, client.put, client.patch, client.delete):
        assert method(path).status_code in (403, 405)


def test_a_user_without_a_role_is_refused() -> None:
    client, _ = client_for(None)
    assert client.get(PAGE).status_code == 403
    assert client.get(EXPORT).status_code == 403


# --- Queries and time (§7) --------------------------------------------------------------------


def busy_stores(count: int, months: int = 1, start: date = JULY) -> None:
    """``count`` stores, each with a lease, a cost row, a rent charge and a settled
    payment per month, a setup charge and an insurance charge."""
    sign = fee("sign_fee")
    premium(2026, "300.00")
    operators = Operator.objects.bulk_create(
        Operator(display_name=f"Operator {n:04d}", email=f"op{n:04d}@example.com")
        for n in range(count)
    )
    stores = Store.objects.bulk_create(
        Store(external_label=f"Store {n:04d}", status=StoreStatus.ACTIVE, needs_open_date=True)
        for n in range(count)
    )
    leases = Lease.objects.bulk_create(
        Lease(
            store=store,
            operator=operator,
            debit_day=1,
            payment_channel=PaymentChannel.WIRE,
            collection_status=CollectionStatus.ACTIVE,
            started_on=JAN,
        )
        for store, operator in zip(stores, operators, strict=True)
    )
    StoreCost.objects.bulk_create(
        StoreCost(
            store=store,
            landlord_rent=D("2500.00"),
            effective_from=JAN,
            source=StoreCostSource.STAFF,
        )
        for store in stores
    )
    charges = RentCharge.objects.bulk_create(
        RentCharge(
            lease=lease,
            period_month=pnl.add_months(start, n),
            due_on=pnl.add_months(start, n),
            amount_due=D("3500.00"),
            status=RentChargeStatus.PAID,
        )
        for lease in leases
        for n in range(months)
    )
    Payment.objects.bulk_create(
        Payment(
            rent_charge=charge,
            kind=PaymentKind.RENT,
            method=PaymentMethod.ACH,
            amount=D("3500.00"),
            status=PaymentStatus.SETTLED,
            settled_on=charge.period_month + timedelta(days=2),
        )
        for charge in charges
    )
    setups = OneOffCharge.objects.bulk_create(
        OneOffCharge(
            lease=lease, fee_type=sign, price=D("1000.00"), cost=D("500.00"), due_date=start
        )
        for lease in leases
    )
    Payment.objects.bulk_create(
        Payment(
            one_off_charge=setup,
            kind=PaymentKind.ONE_OFF,
            method=PaymentMethod.ACH,
            amount=D("1000.00"),
            status=PaymentStatus.SETTLED,
            settled_on=start,
        )
        for setup in setups
    )
    InsuranceCharge.objects.bulk_create(
        InsuranceCharge(
            lease=lease, policy_year=2026, due_on=start, amount=D("695.00"), needs_premium=False
        )
        for lease in leases
    )
    _refresh_planner_statistics()


def _refresh_planner_statistics() -> None:
    """ANALYZE the tables just loaded (PostgreSQL only).

    The rows above sit in the test's open transaction. If autovacuum analysed these tables
    earlier in the session while they were empty, the planner believes they still are and
    joins 6,000 payments to 6,000 charges by nested sequential scans: the report query
    then runs for many minutes (CI run of 2026-10-05). A live database keeps its
    statistics current, so the test does the same before it measures anything.
    """
    if connection.vendor != "postgresql":
        return
    tables = (
        Operator,
        Store,
        Lease,
        StoreCost,
        RentCharge,
        Payment,
        OneOffCharge,
        InsuranceCharge,
    )
    with connection.cursor() as cursor:
        for model in tables:
            cursor.execute(f"ANALYZE {connection.ops.quote_name(model._meta.db_table)}")


def queries_for(selected: pnl.PnlQuery) -> int:
    with CaptureQueriesContext(connection) as captured:
        pnl.rows(selected)
    return len(captured)


def test_the_number_of_queries_does_not_depend_on_the_number_of_rows() -> None:
    empty = queries_for(query())
    busy_stores(3, months=2)
    few = queries_for(query(JULY, AUGUST))
    busy_more(20)
    many = queries_for(query(JULY, AUGUST))
    # PLAN-010d D7 added one: the settled payments of arrears charges (rent collected).
    assert empty == few == many == 13


def busy_more(count: int) -> None:
    sign = fee("sign_fee")
    for n in range(count):
        store = a_store(f"Extra {n:03d}")
        lease = a_lease(store)
        a_cost(store)
        paid(rent(lease, JULY), "3500.00", date(2026, 7, 3))
        a_setup(lease, sign)


def test_the_rows_of_many_stores_add_up() -> None:
    busy_stores(5, months=2)
    found = pnl.rows(query(JULY, AUGUST))
    assert len(found) == 10
    total = pnl.total(found)
    assert total.rent_billed == D("35000.00")
    assert total.fees_billed == D("5") * (D("1000.00") + D("695.00"))
    assert total.margin_billed == D("35000.00") + D("8475.00") - D("25000.00") - D("4000.00")


requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="the time limit is set for PostgreSQL"
)


@requires_postgresql
def test_500_stores_by_12_months_in_under_two_seconds() -> None:
    busy_stores(500, months=12, start=JAN)
    selected = query(JAN, date(2026, 12, 1))
    started = time.perf_counter()
    with CaptureQueriesContext(connection) as captured:
        found = pnl.rows(selected)
    elapsed = time.perf_counter() - started
    assert len(found) == 500 * 12
    assert len(captured) == 13  # PLAN-010d D7: arrears payments count as rent collected
    assert elapsed < 2.0, f"rows() took {elapsed:.2f}s"
