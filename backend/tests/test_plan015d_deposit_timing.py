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
    AuditLog,
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
    detail_cards,
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
    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
    assert lease is not None
    deposit = the_deposit(lease)
    assert deposit.due_date == date(2026, 12, 1)
    assert deposit.pk not in draft_charges(lease)
    assert deposits.card_text(lease) == "$3,000.00 · due Dec 1"
    text, _code = workspace._deposit_text(lease)
    assert text == "$3,000.00 · due Dec 1"


def _portal_charge_ids(lease: Lease, day: date) -> list[Any]:
    """The charge ids the portal offers the lease's operator (its open charges, Pay)."""
    from portal import services as portal_services

    class User:
        operator_id = lease.operator_id

    return [row.charge_id for row in portal_services._open_rows(User(), day)]  # type: ignore[arg-type]


def test_a_legacy_deposit_off_any_invoice_gets_no_reminder_no_portal_pay_and_its_usual_words(
    admin: Client, today: date
) -> None:
    """Fable's run-3 finding: a deposit saved "with the invoice" (every deposit before
    PLAN-015d) is not dated, even on no invoice at all (its draft gone): no B to E, no Pay on
    the portal, the LEASE card's usual words; the card, the reminders and the portal agree."""
    lease = save(admin)
    assert lease is not None
    InvoiceDraft.objects.filter(lease=lease).delete()
    deposit = the_deposit(lease)
    assert deposit.dated is False
    assert deposit.due_date == date(2026, 11, 15)
    for day in (date(2026, 11, 12), date(2026, 11, 15), date(2026, 11, 20), date(2026, 11, 25)):
        assert _rules_for(deposit, day) == [], day
    assert deposit.pk not in _portal_charge_ids(lease, today)
    assert all(row.charge_id != deposit.pk for row in receivables.dated_deposits())
    assert deposits.card_text(lease) == ""
    assert workspace._deposit_text(lease) == ("$3,000.00 · not collected", detail_cards.UNCOLLECTED)


def test_a_deposit_set_on_a_date_is_marked_so_and_the_card_reminders_and_portal_agree(
    admin: Client, today: date
) -> None:
    """The marker is set at creation and read everywhere, whatever becomes of the move-in
    draft (no inference from it)."""
    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
    assert lease is not None
    InvoiceDraft.objects.filter(lease=lease).delete()
    deposit = the_deposit(lease)
    assert deposit.dated is True
    assert _rules_for(deposit, date(2026, 11, 28)) == [MessageRule.B]
    assert deposit.pk in _portal_charge_ids(lease, today)
    assert [row.charge_id for row in receivables.dated_deposits()].count(deposit.pk) == 1
    assert deposits.card_text(lease) == "$3,000.00 · due Dec 1"
    created = AuditLog.objects.get(action=one_off_charges.CREATED_ACTION, entity_id=str(deposit.pk))
    assert created.after["dated"] is True


@pytest.mark.parametrize(
    "timing",
    [
        {},
        {
            "setup_security_deposit_when": "installments",
            "setup_security_deposit_count": "2",
            "setup_security_deposit_first_due": "2026-11-20",
            "setup_security_deposit_cadence": "monthly",
        },
    ],
    ids=["with_invoice", "installments"],
)
def test_with_the_invoice_or_in_installments_the_deposit_is_not_dated(
    admin: Client, today: date, timing: dict[str, str]
) -> None:
    lease = save(admin, **timing)
    assert lease is not None
    assert the_deposit(lease).dated is False


def test_the_lease_cards_deposit_row_reads_its_deposits_in_one_query(
    admin: Client, today: date, django_assert_num_queries: Any
) -> None:
    """The LEASE card's row keeps the page's query budget (PLAN-012f-a D4): what was
    collected, the invoice it rides, its plan and the move-in invoice, in one read."""
    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
    assert lease is not None
    deposit = the_deposit(lease)
    with django_assert_num_queries(1):
        found = deposits.lease_deposits(lease)
    with django_assert_num_queries(0):
        assert deposits.card_text(lease, found) == "$3,000.00 · due Dec 1"
    with django_assert_num_queries(1):
        assert deposits.collected(deposit) == D("0.00")


def test_in_installments_makes_a_deposit_plan_off_the_move_in_invoice(
    admin: Client, today: date
) -> None:
    lease = save(
        admin,
        setup_security_deposit_when="installments",
        setup_security_deposit_count="3",
        setup_security_deposit_first_due="2026-11-15",
        setup_security_deposit_cadence="monthly",
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


def test_a_first_installment_before_the_start_date_is_accepted(admin: Client, today: date) -> None:
    """Re-pinned (Opus, PLAN-015j §10): before the start date and not before today is
    accepted; the plan starts on that date."""
    lease = save(
        admin,
        setup_security_deposit_when="installments",
        setup_security_deposit_count="3",
        setup_security_deposit_first_due="2026-11-10",
        setup_security_deposit_cadence="monthly",
    )
    assert lease is not None
    plan = InstallmentPlan.objects.get(one_off_charge=the_deposit(lease))
    assert (plan.origin, plan.first_due) == (InstallmentOrigin.DEPOSIT, date(2026, 11, 10))


@pytest.mark.parametrize(
    "fields",
    [
        {
            "setup_security_deposit_when": "installments",
            "setup_security_deposit_count": "13",
            "setup_security_deposit_first_due": "2026-11-15",
        },
        {
            "setup_security_deposit_when": "installments",
            "setup_security_deposit_count": "1",
            "setup_security_deposit_first_due": "2026-11-15",
        },
        {"setup_security_deposit_when": "installments", "setup_security_deposit_count": "3"},
        {"setup_security_deposit_when": "on_date"},
        {"setup_security_deposit_when": "on_date", "setup_security_deposit_on": "2026-11-02"},
    ],
)
def test_an_incomplete_or_out_of_range_timing_is_refused(
    admin: Client, today: date, fields: dict[str, str]
) -> None:
    assert save(admin, **fields) is None


def test_a_timing_without_a_deposit_amount_is_ignored(admin: Client, today: date) -> None:
    """Re-pinned (Opus, PLAN-015j §4): a row without a price ignores its When fields and
    saves nothing (it was refused with DEPOSIT_REQUIRED); the lease is saved without it."""
    store = vacant_store()
    operator = new_lease.an_operator_with("No Deposit", email="nodeposit@example.com")
    post_create(
        admin,
        store,
        create_data(
            store,
            operator,
            setup_security_deposit_when="on_date",
            setup_security_deposit_on="2026-12-01",
        ),
    )
    lease = Lease.objects.get(store=store)
    assert not OneOffCharge.objects.filter(lease=lease).exists()
    assert not InstallmentPlan.objects.exists()


def test_the_summary_leaves_a_deposit_off_the_invoice_out_of_the_first_invoice(
    today: date,
) -> None:
    from ledger.forms import LeaseCreateForm

    store = vacant_store()
    price = f"setup_{deposit_type().code}_price"
    on_invoice = LeaseCreateForm(create_data(store, None, **{price: "3000.00"}), today=today)
    dated = LeaseCreateForm(
        create_data(
            store,
            None,
            **{
                price: "3000.00",
                "setup_security_deposit_when": "on_date",
                "setup_security_deposit_on": "2026-12-01",
            },
        ),
        today=today,
    )
    on_invoice.is_valid()
    dated.is_valid()
    assert on_invoice.summary()["move_in"] == "$3,000.00"
    assert dated.summary()["move_in"] == "$0.00"


# --- the split refusal is lifted for the deposit plan only -----------------------------------


def test_split_into_installments_still_refuses_a_deposit(admin: Client, today: date) -> None:
    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
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
    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
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
        setup_security_deposit_when="installments",
        setup_security_deposit_count="2",
        setup_security_deposit_first_due="2026-11-20",
        setup_security_deposit_cadence="monthly",
    )
    assert lease is not None
    plan = InstallmentPlan.objects.get(one_off_charge=the_deposit(lease))
    first = installments.installments_of(plan)[0]
    assert _rules_for(first, date(2026, 11, 17)) == [MessageRule.B]


# --- the portal, the P&L, lease end and transfer -----------------------------------------------


def test_the_portal_offers_a_dated_deposit_as_an_open_charge(admin: Client, today: date) -> None:
    from portal import services as portal_services

    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
    assert lease is not None
    deposit = the_deposit(lease)
    operator_id = lease.operator_id

    class User:
        pass

    User.operator_id = operator_id  # type: ignore[attr-defined]

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
        setup_security_deposit_when="installments",
        setup_security_deposit_count="3",
        setup_security_deposit_first_due="2026-11-15",
        setup_security_deposit_cadence="monthly",
    )
    assert lease is not None
    plan = InstallmentPlan.objects.get(one_off_charge=the_deposit(lease))
    first = installments.installments_of(plan)[0]
    assert first.category == OneOffCategory.INSTALLMENT
    _pay(first, "1000.00", date(2026, 11, 15))
    total = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 11, 1), date(2026, 11, 1))))
    assert total.fees_collected == D("0")
    # Re-pinned (Fable's finding 3): the card names what was collected before the progress.
    assert deposits.card_text(lease) == (
        "$3,000.00 · $1,000.00 collected · 1 of 3 installments paid"
    )


def test_lease_end_and_transfer_still_read_the_deposits_own_settled_payments(
    admin: Client, today: date
) -> None:
    """Pinned: ``one_off_charges.settled`` itself is unchanged (Fable's finding 3: lease end
    and transfer read :func:`ledger.services.deposits.collected`, which adds the deposit
    plan's installments; for a dated deposit the two agree)."""
    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
    assert lease is not None
    deposit = the_deposit(lease)
    assert one_off_charges.settled(deposit) == D("0.00")
    _pay(deposit, "3000.00", date(2026, 11, 20))
    assert one_off_charges.settled(deposit) == D("3000.00")
    assert deposits.collected(deposit) == D("3000.00")
    assert deposits.card_text(lease) == ""


def _installment_deposit(admin: Client) -> tuple[Lease, OneOffCharge, list[OneOffCharge]]:
    lease = save(
        admin,
        setup_security_deposit_when="installments",
        setup_security_deposit_count="3",
        setup_security_deposit_first_due="2026-11-15",
        setup_security_deposit_cadence="monthly",
    )
    assert lease is not None
    deposit = the_deposit(lease)
    plan = InstallmentPlan.objects.get(one_off_charge=deposit)
    return lease, deposit, installments.installments_of(plan)


def _held_everywhere(lease: Lease, deposit: OneOffCharge, end: date) -> list[Any]:
    """What lease end, the transfer, the P&L footer and the LEASE card read as held."""
    from ledger.models import LeaseTransfer
    from ledger.services import lease_end, statements, transfer

    [line] = statements._deposit_lines(lease_end.start(lease, None))
    return [
        deposits.collected(deposit),
        line.held,
        transfer.default_deposit(LeaseTransfer(outgoing_lease=lease)),
        pnl.deposits_held(pnl.PnlQuery(date(2026, 11, 1), end)),
        workspace._deposit_text(lease),
    ]


def test_one_of_three_installments_paid_is_held_at_lease_end_on_the_card_and_in_the_pnl(
    admin: Client, today: date
) -> None:
    """Fable's finding 3: a deposit collected by installments reads as collected."""
    lease, deposit, rows = _installment_deposit(admin)
    assert one_off_charges.settled(deposit) == D("0.00")  # nothing on the deposit itself
    _pay(rows[0], "1000.00", date(2026, 11, 15))
    held = D("1000.00")
    assert _held_everywhere(lease, deposit, date(2026, 11, 30)) == [
        held,
        held,
        held,
        held,
        ("$3,000.00 · $1,000.00 collected · 1 of 3 installments paid", "partial"),
    ]


def test_all_installments_paid_hold_the_whole_deposit(admin: Client, today: date) -> None:
    lease, deposit, rows = _installment_deposit(admin)
    for row, day in zip(rows, (15, 16, 17), strict=True):
        _pay(row, "1000.00", date(2026, 11, day))
    held = D("3000.00")
    assert _held_everywhere(lease, deposit, date(2026, 11, 30)) == [
        held,
        held,
        held,
        held,
        ("$3,000.00 · collected", "collected"),
    ]
    # Lease end resolves it whole: what is refunded and withheld is what was collected.
    assert deposits.collected(deposit) == held


def test_a_dated_deposit_reads_its_own_payments_as_before(admin: Client, today: date) -> None:
    lease = save(
        admin, setup_security_deposit_when="on_date", setup_security_deposit_on="2026-12-01"
    )
    assert lease is not None
    deposit = the_deposit(lease)
    _pay(deposit, "1200.00", date(2026, 11, 20))
    held = D("1200.00")
    assert _held_everywhere(lease, deposit, date(2026, 11, 30)) == [
        held,
        held,
        held,
        held,
        ("$3,000.00 · $1,200.00 collected", "partial"),
    ]
    assert deposits.collected(deposit) == one_off_charges.settled(deposit)


def test_change_start_with_the_tick_moves_only_the_installments_not_yet_paid(
    admin: Client, today: date
) -> None:
    """Fable's adversarial case (§5): the deposit plan has collected one installment."""
    from ledger.services import start_date
    from tests.test_invoice_builder import user

    lease = save(
        admin,
        setup_security_deposit_when="installments",
        setup_security_deposit_count="3",
        setup_security_deposit_first_due="2026-11-15",
        setup_security_deposit_cadence="monthly",
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


def _ledger_and_reports(end: date) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """The payments ledger's deposits held and collected, the P&L footer's deposits held,
    and the payments-by-method report's total, for November 2026."""
    from ledger.services import payments_ledger, reports

    found = payments_ledger.totals(payments_ledger.ledger_payments(payments_ledger.PaymentFilter()))
    by_method = reports.payments_by_method(reports.ReportRange(date(2026, 11, 1), end))
    return (
        found.deposits_held,
        found.settled,
        pnl.deposits_held(pnl.PnlQuery(date(2026, 11, 1), end)),
        sum(by_method.total.amounts.values(), D("0")),
    )


def test_the_payments_ledger_and_the_reports_hold_deposit_installments_as_the_pnl_does(
    admin: Client, today: date
) -> None:
    """Fable's review: the payments ledger's deposits held and the reports' deposit
    exclusion count a deposit plan's installments, so the three figures agree."""
    lease, deposit, rows = _installment_deposit(admin)
    _pay(rows[0], "1000.00", date(2026, 11, 15))
    held = deposits.collected(deposit)
    assert held == D("1000.00")
    # Held in the ledger and the P&L, never collected or counted by method.
    assert _ledger_and_reports(date(2026, 11, 1)) == (held, D("0.00"), held, D("0"))


def test_a_withheld_installment_deposit_is_released_in_the_ledger_and_the_pnl(
    admin: Client, today: date
) -> None:
    lease, deposit, rows = _installment_deposit(admin)
    for row, day in zip(rows, (15, 16, 17), strict=True):
        _pay(row, "1000.00", date(2026, 11, day))
    assert _ledger_and_reports(date(2026, 11, 1))[0::2] == (D("3000.00"), D("3000.00"))
    Lease.objects.filter(pk=lease.pk).update(ended_on=date(2026, 11, 20))
    outcome = deposits.resolve_deposit_split(
        deposit,
        D("0.00"),
        D("3000.00"),
        method="",
        resolved_on=date(2026, 11, 20),
        reference="",
        note="Withheld for damages.",
        actor=None,
        today=date(2026, 11, 20),
    )
    assert outcome.ok, outcome.message
    # Withheld: no longer held, in either.
    assert _ledger_and_reports(date(2026, 11, 1))[0::2] == (D("0.00"), D("0.00"))
