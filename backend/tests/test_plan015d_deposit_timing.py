"""PLAN-015d task 4: the deposit's timing on New lease — with the move-in invoice (the default),
due on a date, or in installments (a plan of origin ``deposit``) — its reminders, its LEASE card
row, the portal, the P&L, and lease end and transfer reading what was collected
(docs/qa/15/PLAN-015d §3 (3), §9.4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from ledger.models import (
    InstallmentCollection,
    InstallmentOrigin,
    InstallmentPlan,
    InvoiceDraft,
    Lease,
    MessageRule,
    OneOffCategory,
    OneOffCharge,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import (
    deposits,
    installments,
    invoice_builder,
    one_off_charges,
    pnl,
    receivables,
    reminders,
    workspace,
)
from tests import test_new_lease_page as new_lease
from tests.test_new_lease_page import create_data, deposit_type, post_create, vacant_store

pytestmark = pytest.mark.django_db

D = Decimal
today = new_lease.today
admin = new_lease.admin
TODAY = new_lease.TODAY  # 2026-11-03; the lease starts 2026-11-15


def save(client: Client, **fields: str) -> Lease | None:
    store = vacant_store()
    operator = new_lease.an_operator_with("Deposit Timing", email="timing@example.com")
    price = f"setup_{deposit_type().code}_price"
    data = create_data(store, operator, **{price: "3000.00", **fields})
    post_create(client, store, data)
    return Lease.objects.filter(store=store).first()


def the_deposit(lease: Lease) -> OneOffCharge:
    return OneOffCharge.objects.get(lease=lease, fee_type__refundable=True)


def draft_charges(lease: Lease) -> list[Any]:
    draft = InvoiceDraft.objects.get(lease=lease)
    return [line.existing_charge_id for line in invoice_builder.lines_of(draft)]


# --- the three choices ------------------------------------------------------------------------


def test_by_default_the_deposit_is_a_move_in_line(admin: Client, today: date) -> None:
    lease = save(admin)
    assert lease is not None
    deposit = the_deposit(lease)
    assert deposit.pk in draft_charges(lease)
    assert deposit.due_date == date(2026, 11, 15)
    assert not InstallmentPlan.objects.exists()


def test_due_on_a_date_is_its_own_charge_off_the_move_in_invoice(
    admin: Client, today: date
) -> None:
    lease = save(admin, deposit_when="on_date", deposit_on="2026-12-01")
    assert lease is not None
    deposit = the_deposit(lease)
    assert deposit.due_date == date(2026, 12, 1)
    assert deposit.pk not in draft_charges(lease)
    assert deposits.card_text(lease) == "$3,000.00 · due Dec 1"
    text, _code = workspace._deposit_text(lease)
    assert text == "$3,000.00 · due Dec 1"


def test_in_installments_makes_a_deposit_plan_off_the_move_in_invoice(
    admin: Client, today: date
) -> None:
    lease = save(
        admin,
        deposit_when="installments",
        deposit_count="3",
        deposit_first_due="2026-11-15",
        deposit_cadence="monthly",
    )
    assert lease is not None
    deposit = the_deposit(lease)
    plan = InstallmentPlan.objects.get(one_off_charge=deposit)
    assert (plan.origin, plan.count, plan.first_due, plan.collection) == (
        InstallmentOrigin.DEPOSIT,
        3,
        date(2026, 11, 15),
        InstallmentCollection.MANUAL,
    )
    rows = installments.installments_of(plan)
    assert [(row.due_date, row.price) for row in rows] == [
        (date(2026, 11, 15), D("1000.00")),
        (date(2026, 12, 15), D("1000.00")),
        (date(2027, 1, 15), D("1000.00")),
    ]
    assert deposit.pk not in draft_charges(lease)
    assert deposits.card_text(lease) == "$3,000.00 · 0 of 3 installments paid"


def test_a_first_installment_before_the_start_date_is_refused_with_nothing_saved(
    admin: Client, today: date
) -> None:
    lease = save(
        admin,
        deposit_when="installments",
        deposit_count="3",
        deposit_first_due="2026-11-10",
        deposit_cadence="monthly",
    )
    assert lease is None
    assert not InstallmentPlan.objects.exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"deposit_when": "installments", "deposit_count": "13", "deposit_first_due": "2026-11-15"},
        {"deposit_when": "installments", "deposit_count": "1", "deposit_first_due": "2026-11-15"},
        {"deposit_when": "installments", "deposit_count": "3"},
        {"deposit_when": "on_date"},
        {"deposit_when": "on_date", "deposit_on": "2026-11-02"},
    ],
)
def test_an_incomplete_or_out_of_range_timing_is_refused(
    admin: Client, today: date, fields: dict[str, str]
) -> None:
    assert save(admin, **fields) is None


def test_a_timing_without_a_deposit_amount_is_refused(admin: Client, today: date) -> None:
    store = vacant_store()
    operator = new_lease.an_operator_with("No Deposit", email="nodeposit@example.com")
    post_create(
        admin, store, create_data(store, operator, deposit_when="on_date", deposit_on="2026-12-01")
    )
    assert not Lease.objects.filter(store=store).exists()


def test_the_summary_leaves_a_deposit_off_the_invoice_out_of_the_first_invoice(
    today: date,
) -> None:
    from ledger.forms import LeaseCreateForm

    store = vacant_store()
    price = f"setup_{deposit_type().code}_price"
    on_invoice = LeaseCreateForm(create_data(store, None, **{price: "3000.00"}), today=today)
    dated = LeaseCreateForm(
        create_data(
            store, None, **{price: "3000.00", "deposit_when": "on_date", "deposit_on": "2026-12-01"}
        ),
        today=today,
    )
    on_invoice.is_valid()
    dated.is_valid()
    assert on_invoice.summary()["move_in"] == "$3,000.00"
    assert dated.summary()["move_in"] == "$0.00"


# --- the split refusal is lifted for the deposit plan only -----------------------------------


def test_split_into_installments_still_refuses_a_deposit(admin: Client, today: date) -> None:
    lease = save(admin, deposit_when="on_date", deposit_on="2026-12-01")
    assert lease is not None
    deposit = the_deposit(lease)
    assert installments.split_refusal(deposit, today) == installments.DEPOSIT
    assert installments.split_refusal(deposit, today, origin=InstallmentOrigin.DEPOSIT) == ""


# --- reminders follow the deposit's own due dates --------------------------------------------


def _rules_for(charge: OneOffCharge, day: date) -> list[str]:
    return [
        item.rule.code
        for item in reminders.plan(day)
        if item.row is not None and item.row.charge_id == charge.pk
    ]


def test_a_dated_deposit_gets_b_three_days_before_and_c_after(admin: Client, today: date) -> None:
    lease = save(admin, deposit_when="on_date", deposit_on="2026-12-01")
    assert lease is not None
    deposit = the_deposit(lease)
    assert _rules_for(deposit, date(2026, 11, 28)) == [MessageRule.B]
    assert _rules_for(deposit, date(2026, 12, 6)) == [MessageRule.C]
    assert _rules_for(deposit, date(2026, 11, 29)) == []


def test_a_deposit_on_the_move_in_invoice_never_gets_its_own_reminder(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    assert lease is not None
    deposit = the_deposit(lease)
    assert _rules_for(deposit, date(2026, 11, 12)) == []
    assert all(row.charge_id != deposit.pk for row in receivables.dated_deposits())


def test_a_deposit_installment_gets_the_rules_on_its_own_date(admin: Client, today: date) -> None:
    lease = save(
        admin,
        deposit_when="installments",
        deposit_count="2",
        deposit_first_due="2026-11-20",
        deposit_cadence="monthly",
    )
    assert lease is not None
    plan = InstallmentPlan.objects.get(one_off_charge=the_deposit(lease))
    first = installments.installments_of(plan)[0]
    assert _rules_for(first, date(2026, 11, 17)) == [MessageRule.B]


# --- the portal, the P&L, lease end and transfer -----------------------------------------------


def test_the_portal_offers_a_dated_deposit_as_an_open_charge(admin: Client, today: date) -> None:
    from portal import services as portal_services

    lease = save(admin, deposit_when="on_date", deposit_on="2026-12-01")
    assert lease is not None
    deposit = the_deposit(lease)

    class User:
        operator_id = lease.operator_id

    rows = portal_services._open_rows(User(), today)  # type: ignore[arg-type]
    assert [row.balance for row in rows if row.charge_id == deposit.pk] == [D("3000.00")]


def _pay(charge: OneOffCharge, amount: str, on: date) -> Payment:
    payment = Payment.objects.create(
        one_off_charge=charge,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ZELLE,
        amount=D(amount),
        status=PaymentStatus.SETTLED,
        initiated_on=on,
        settled_on=on,
        note="Paid by Zelle.",
    )
    one_off_charges.recompute(charge)
    return payment


def test_a_deposit_installment_paid_is_not_revenue_in_the_pnl(admin: Client, today: date) -> None:
    lease = save(
        admin,
        deposit_when="installments",
        deposit_count="3",
        deposit_first_due="2026-11-15",
        deposit_cadence="monthly",
    )
    assert lease is not None
    plan = InstallmentPlan.objects.get(one_off_charge=the_deposit(lease))
    first = installments.installments_of(plan)[0]
    assert first.category == OneOffCategory.INSTALLMENT
    _pay(first, "1000.00", date(2026, 11, 15))
    total = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 11, 1), date(2026, 11, 1))))
    assert total.fees_collected == D("0")
    assert deposits.card_text(lease) == "$3,000.00 · 1 of 3 installments paid"


def test_lease_end_and_transfer_still_read_the_deposits_own_settled_payments(
    admin: Client, today: date
) -> None:
    """Pinned (§3 (3)): lease end and transfer keep ``one_off_charges.settled``."""
    lease = save(admin, deposit_when="on_date", deposit_on="2026-12-01")
    assert lease is not None
    deposit = the_deposit(lease)
    assert one_off_charges.settled(deposit) == D("0.00")
    _pay(deposit, "3000.00", date(2026, 11, 20))
    assert one_off_charges.settled(deposit) == D("3000.00")
    assert deposits.card_text(lease) == ""


def test_change_start_with_the_tick_moves_only_the_installments_not_yet_paid(
    admin: Client, today: date
) -> None:
    """Fable's adversarial case (§5): the deposit plan has collected one installment."""
    from ledger.services import start_date
    from tests.test_invoice_builder import user

    lease = save(
        admin,
        deposit_when="installments",
        deposit_count="3",
        deposit_first_due="2026-11-15",
        deposit_cadence="monthly",
    )
    assert lease is not None
    plan = InstallmentPlan.objects.get(one_off_charge=the_deposit(lease))
    first, second, third = installments.installments_of(plan)
    _pay(first, "1000.00", date(2026, 11, 3))
    from accounts.roles import Role

    start_date.apply_change(
        lease.pk,
        date(2026, 11, 20),
        reason="Keys handed over later.",
        choice=None,
        move_deposit=True,
        actor=user(Role.BILLING),
        today=today,
    )
    rows = installments.installments_of(plan)
    assert [row.due_date for row in rows] == [
        date(2026, 11, 15),
        date(2026, 12, 20),
        date(2027, 1, 20),
    ]
    assert second.pk == rows[1].pk and third.pk == rows[2].pk
