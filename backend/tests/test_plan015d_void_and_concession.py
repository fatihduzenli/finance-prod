"""PLAN-015d task 1: the void rent charge status, the free-rent concession on a rent charge, the
lease's free-rent terms and the deposit plan origin (ledger 0072 to 0074), and the reports that
treat a void charge as not a bill (docs/qa/15/PLAN-015d §3)."""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from ledger.models import (
    NOT_BILLED_RENT,
    FeeType,
    FreeRentFrom,
    InstallmentOrigin,
    Lease,
    OneOffCharge,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import pnl, reports
from tests.test_pnl import a_lease, a_store, paid, rent

pytestmark = pytest.mark.django_db

D = Decimal

MAY = date(2026, 5, 1)
JUNE = date(2026, 6, 1)


def fresh(charge: RentCharge) -> RentCharge:
    return RentCharge.objects.get(pk=charge.pk)


# --- the generated balance --------------------------------------------------------------


def test_the_balance_is_the_amount_due_less_the_concession_and_what_was_paid() -> None:
    lease = a_lease(a_store("Store 9701"))
    charge = rent(lease, MAY, "3000.00", concession=D("1000.00"))
    assert fresh(charge).balance == D("2000.00")
    assert fresh(charge).net_due == D("2000.00")
    RentCharge.objects.filter(pk=charge.pk).update(amount_paid=D("500.00"))
    assert fresh(charge).balance == D("1500.00")


def test_a_void_charge_has_no_balance_whatever_its_amount() -> None:
    lease = a_lease(a_store("Store 9702"))
    charge = rent(lease, MAY, "3000.00", status=RentChargeStatus.VOID)
    assert fresh(charge).balance == D("0.00")
    assert not RentCharge.objects.filter(pk=charge.pk, balance__gt=0).exists()
    assert fresh(charge).amount_due == D("3000.00")


def test_a_charge_without_a_concession_keeps_its_old_balance() -> None:
    lease = a_lease(a_store("Store 9703"))
    charge = rent(lease, MAY, "3000.00")
    assert (fresh(charge).concession, fresh(charge).concession_text) == (D("0.00"), "")
    assert fresh(charge).balance == D("3000.00")


def test_a_concession_above_the_amount_due_is_refused_by_the_database() -> None:
    lease = a_lease(a_store("Store 9704"))
    charge = rent(lease, MAY, "3000.00")
    with pytest.raises(IntegrityError), transaction.atomic():
        RentCharge.objects.filter(pk=charge.pk).update(concession=D("3000.01"))
    with pytest.raises(IntegrityError), transaction.atomic():
        RentCharge.objects.filter(pk=charge.pk).update(concession=D("-0.01"))
    RentCharge.objects.filter(pk=charge.pk).update(concession=D("3000.00"))
    assert fresh(charge).balance == D("0.00")


def test_an_unknown_rent_status_is_still_refused() -> None:
    lease = a_lease(a_store("Store 9705"))
    charge = rent(lease, MAY, "3000.00")
    with pytest.raises(IntegrityError), transaction.atomic():
        RentCharge.objects.filter(pk=charge.pk).update(status="cancelled")


def test_void_and_waived_are_the_statuses_that_are_not_a_bill() -> None:
    assert set(NOT_BILLED_RENT) == {RentChargeStatus.WAIVED, RentChargeStatus.VOID}


# --- the lease's free-rent terms and the deposit origin ---------------------------------


def test_a_lease_has_no_free_rent_by_default() -> None:
    lease = a_lease(a_store("Store 9706"))
    lease.refresh_from_db()
    assert (lease.free_rent_days, lease.free_rent_from) == (0, FreeRentFrom.MOVE_IN)


@pytest.mark.parametrize("days", [-1, 367])
def test_free_rent_days_outside_0_to_366_are_refused(days: int) -> None:
    lease = a_lease(a_store(f"Store 97{days + 10:02d}"))
    with pytest.raises(IntegrityError), transaction.atomic():
        Lease.objects.filter(pk=lease.pk).update(free_rent_days=days)


def test_366_free_days_from_the_next_month_are_accepted() -> None:
    lease = a_lease(a_store("Store 9707"))
    Lease.objects.filter(pk=lease.pk).update(
        free_rent_days=366, free_rent_from=FreeRentFrom.NEXT_MONTH
    )
    lease.refresh_from_db()
    assert (lease.free_rent_days, lease.free_rent_from) == (366, "next_month")
    with pytest.raises(IntegrityError), transaction.atomic():
        Lease.objects.filter(pk=lease.pk).update(free_rent_from="end_of_lease")


def test_the_deposit_origin_is_a_stored_value() -> None:
    assert InstallmentOrigin.DEPOSIT.value == "deposit"


# --- P&L and Revenue treat a void charge as not a bill -----------------------------------


def _sum(rows: Any) -> D:
    return sum((row.rent_billed for row in rows), D("0"))


def test_pnl_rent_billed_leaves_out_void_charges_as_it_leaves_out_waived_ones() -> None:
    lease = a_lease(a_store("Store 9708"))
    rent(lease, MAY, "3000.00")
    rent(lease, MAY, "1500.00", seq=2, status=RentChargeStatus.VOID)
    rent(lease, JUNE, "3000.00", status=RentChargeStatus.WAIVED)
    may = pnl.rows(pnl.PnlQuery(MAY, MAY))
    june = pnl.rows(pnl.PnlQuery(JUNE, JUNE))
    assert _sum(may) == D("3000.00")
    assert _sum(june) == D("0")


def test_the_revenue_report_agrees_with_the_pnl_on_void_charges() -> None:
    lease = a_lease(a_store("Store 9709"))
    kept = rent(lease, MAY, "3000.00")
    rent(lease, JUNE, "3000.00", status=RentChargeStatus.VOID)
    paid(kept, "3000.00", MAY)
    revenue = reports.revenue_by_month(reports.ReportRange(MAY, JUNE))
    by_month = {item.month: item for item in revenue.months}
    assert by_month[MAY].rent_billed == D("3000.00")
    assert by_month[JUNE].rent_billed == D("0")
    assert revenue.total.rent_billed == _sum(pnl.rows(pnl.PnlQuery(MAY, JUNE)))


# --- migrations 0072 to 0074 -------------------------------------------------------------

BEFORE = ("ledger", "0071_invoice_pdfs")
AFTER = ("ledger", "0074_installment_origin_deposit")


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


@pytest.mark.django_db(transaction=True)
def test_0072_to_0074_forward_and_back() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    lease = a_lease(a_store("Store 9710"))
    charge = rent(lease, MAY, "3000.00")
    RentCharge.objects.filter(pk=charge.pk).update(amount_paid=D("1000.00"))
    try:
        old = _migrate(BEFORE)
        old_charge = old.get_model("ledger", "RentCharge").objects.get(pk=charge.pk)
        assert old_charge.balance == D("2000.00")
        assert "concession" not in {
            f.name for f in old.get_model("ledger", "RentCharge")._meta.fields
        }
        assert "free_rent_days" not in {
            f.name for f in old.get_model("ledger", "Lease")._meta.fields
        }
        new = _migrate(AFTER)
        new_charge = new.get_model("ledger", "RentCharge").objects.get(pk=charge.pk)
        assert (new_charge.balance, new_charge.concession) == (D("2000.00"), D("0.00"))
        back = _migrate(BEFORE)
        assert back.get_model("ledger", "RentCharge").objects.get(pk=charge.pk).balance == D(
            "2000.00"
        )
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", [{"status": "void"}, {"concession": D("10.00")}])
def test_0072_reverse_is_refused_while_a_charge_is_void_or_conceded(change: dict[str, Any]) -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    lease = a_lease(a_store("Store 9711"))
    charge = rent(lease, MAY, "3000.00")
    RentCharge.objects.filter(pk=charge.pk).update(**change)
    try:
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(("ledger", "0071_invoice_pdfs"))
        assert RentCharge.objects.filter(pk=charge.pk, **change).exists()
    finally:
        _migrate(*leaves)
        RentCharge.objects.filter(pk=charge.pk).delete()


@pytest.mark.django_db(transaction=True)
def test_0074_reverse_is_refused_while_a_deposit_plan_exists() -> None:
    from django.utils import timezone

    from ledger.models import InstallmentCadence, InstallmentCollection, InstallmentPlan
    from tests.receivables_helpers import a_one_off_charge

    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    lease = a_lease(a_store("Store 9712"))
    assert lease.operator is not None
    deposit = a_one_off_charge(lease.operator, "3000.00", MAY, lease=lease)
    plan = InstallmentPlan.objects.create(
        one_off_charge=deposit,
        operator=lease.operator,
        lease=lease,
        count=3,
        first_due=MAY,
        cadence=InstallmentCadence.MONTHLY,
        collection=InstallmentCollection.MANUAL,
        origin=InstallmentOrigin.DEPOSIT,
        confirmed_at=timezone.now(),
    )
    try:
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(("ledger", "0073_lease_free_rent"))
        assert InstallmentPlan.objects.filter(pk=plan.pk, origin="deposit").exists()
    finally:
        _migrate(*leaves)
        InstallmentPlan.objects.filter(pk=plan.pk).delete()


@pytest.mark.django_db(transaction=True)
def test_0076_marks_no_charge_dated_and_reverses_while_none_is() -> None:
    """Fable's run-3 finding: every charge saved before is not dated (a deposit of before
    rides its invoice as it did); the reverse drops the column while no charge is dated."""
    from tests.receivables_helpers import a_one_off_charge

    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    lease = a_lease(a_store("Store 9713"))
    assert lease.operator is not None
    deposit = a_one_off_charge(lease.operator, "3000.00", MAY, lease=lease)
    try:
        old = _migrate(("ledger", "0075_payment_held_as_credit"))
        assert "dated" not in {f.name for f in old.get_model("ledger", "OneOffCharge")._meta.fields}
        new = _migrate(("ledger", "0076_one_off_charge_dated"))
        assert new.get_model("ledger", "OneOffCharge").objects.get(pk=deposit.pk).dated is False
    finally:
        _migrate(*leaves)


@pytest.mark.django_db(transaction=True)
def test_0076_reverse_is_refused_while_a_deposit_is_dated() -> None:
    from tests.receivables_helpers import a_one_off_charge

    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    lease = a_lease(a_store("Store 9714"))
    assert lease.operator is not None
    deposit = a_one_off_charge(
        lease.operator,
        "3000.00",
        MAY,
        lease=lease,
        category="setup",
        fee_type=FeeType.objects.create(code="deposit-0076", name="Deposit", refundable=True),
        collection="none",
        description="",
    )
    OneOffCharge.objects.filter(pk=deposit.pk).update(dated=True)
    try:
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(("ledger", "0075_payment_held_as_credit"))
        assert OneOffCharge.objects.filter(pk=deposit.pk, dated=True).exists()
    finally:
        _migrate(*leaves)
        OneOffCharge.objects.filter(pk=deposit.pk).delete()


def test_only_a_setup_charge_collected_by_none_may_be_dated() -> None:
    from tests.receivables_helpers import a_one_off_charge

    lease = a_lease(a_store("Store 9715"))
    assert lease.operator is not None
    fee = a_one_off_charge(lease.operator, "80.00", MAY, lease=lease)  # damage, manual
    with pytest.raises(IntegrityError), transaction.atomic():
        OneOffCharge.objects.filter(pk=fee.pk).update(dated=True)


def test_the_new_migrations_declare_their_guarded_columns() -> None:
    m72 = importlib.import_module("ledger.migrations.0072_rent_charge_void_and_concession")
    m34 = importlib.import_module("ledger.migrations.0034_proration")
    assert m72.GUARDED_COLUMNS == {
        "rent_charges": (*m34.GUARDED_COLUMNS["rent_charges"], "concession_text")
    }
    for name in (
        "0073_lease_free_rent",
        "0074_installment_origin_deposit",
        "0076_one_off_charge_dated",
    ):
        assert importlib.import_module(f"ledger.migrations.{name}").GUARDED_COLUMNS == {}
