"""Adversarial cases for PLAN-012b-b Batch B (rent billed by invoice, the P&L at lease end),
by the QA owner (plan §6 B). A rent line cannot bill another operator's lease; its amount
stays within the money bounds; a forged month is refused; two rent lines for one month are
refused however they are posted; the invoice's rent charge is never billed or paid twice by
the run; the money of a move-in invoice is counted once."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from accounts.roles import Role
from ledger.models import (
    InvoiceDraft,
    InvoiceDraftLine,
    Lease,
    OneOffCharge,
    Operator,
    PaymentMethod,
    RentCharge,
)
from ledger.services import invoice_builder, pnl, receivables, rent_run
from ledger.services.invoice_builder import LineInput
from payments.fake import FakeProvider
from tests import test_invoice_builder as builder_tests
from tests.job_helpers import run_queued_jobs
from tests.test_invoice_builder import TODAY, a_world, fresh, line, pay, user
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator

pytestmark = pytest.mark.django_db

D = Decimal
OCTOBER = date(2026, 10, 1)
# The builder file's fixtures, re-exported so pytest finds them here.
fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker


#: The move-in lease of the never-billed-twice cases: starts on the 13th, paid by wire.
MOVE_IN = {"payment_channel": "wire", "started_on": date(2026, 10, 13), "debit_day": 1}


def _rent_line(lease: Lease, month: date = OCTOBER, amount: str | None = None) -> LineInput:
    return LineInput(
        "Rent",
        "",
        None,
        D(amount) if amount is not None else None,
        rent_lease=lease.pk,
        rent_month=month,
    )


def _draft(
    operator: Operator, lease: Lease | None, lines: list[LineInput]
) -> invoice_builder.Outcome:
    return invoice_builder.create_draft(
        operator, lease, "Move-in", None, lines, user(Role.BILLING), today=TODAY
    )


def _nothing_saved() -> bool:
    return not InvoiceDraft.objects.exists() and not InvoiceDraftLine.objects.exists()


# --- the line stays within its operator and its bounds -----------------------------------------


def test_a_rent_line_cannot_bill_another_operators_lease(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    other_operator = an_operator(fake, customer="cus-other-b", bank="pm-other-b")
    other_lease = make_lease(operator=other_operator)
    assert other_lease.operator_id != operator.pk
    refused = _draft(operator, lease, [_rent_line(other_lease)])
    assert not refused.ok and _nothing_saved(), refused.message
    # Nor a lease that does not exist.
    ghost = Lease(pk="00000000-0000-0000-0000-000000000000")
    refused = _draft(operator, lease, [_rent_line(ghost)])
    assert not refused.ok and _nothing_saved()


@pytest.mark.parametrize("amount", ["0.00", "0.001", "-1.00", "1000000.00", "NaN"])
def test_a_rent_lines_amount_stays_within_the_money_bounds(fake: FakeProvider, amount: str) -> None:
    operator, lease = a_world(fake)
    refused = _draft(operator, lease, [_rent_line(lease, amount=amount)])
    assert not refused.ok and _nothing_saved(), amount


@pytest.mark.parametrize("month", [date(2026, 10, 13), date(2026, 10, 31), date(1999, 1, 1)])
def test_a_forged_rent_month_is_refused(fake: FakeProvider, month: date) -> None:
    operator, lease = a_world(fake)
    refused = _draft(operator, lease, [_rent_line(lease, month=month)])
    assert not refused.ok and _nothing_saved(), month


def test_two_rent_lines_for_one_month_are_refused_however_they_are_posted(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    refused = _draft(operator, lease, [_rent_line(lease), _rent_line(lease, amount="1.00")])
    assert not refused.ok and _nothing_saved()
    made = _draft(operator, lease, [_rent_line(lease)])
    assert made.ok and made.draft is not None, made.message
    # Through update, with the same month in a different position and a catalogue line between.
    updated = invoice_builder.update_draft(
        made.draft.pk,
        lease,
        "Move-in",
        None,
        [line("Sign fee", "450.00", "1"), _rent_line(lease), _rent_line(lease, amount="5.00")],
        user(Role.BILLING),
        today=TODAY,
    )
    assert not updated.ok
    assert InvoiceDraftLine.objects.filter(draft=made.draft).count() == 1
    # A second draft for the same month is refused too.
    second = _draft(operator, lease, [_rent_line(lease)])
    assert not second.ok and InvoiceDraft.objects.count() == 1


# --- never billed or paid twice ----------------------------------------


def test_the_invoices_rent_charge_is_never_billed_or_paid_twice_by_the_run(
    fake: FakeProvider, worker: None
) -> None:
    # The lease starts on the 13th; the first rent is invoiced before the start. It is the
    # operator's only lease (PLAN-014 C3: it holds the provider account): the run sees it only.
    operator = an_operator(fake, account=MOVE_IN)
    lease = Lease.objects.get(operator=operator)
    made = _draft(operator, lease, [_rent_line(lease)])
    assert made.ok and made.draft is not None, made.message
    [draft_line] = InvoiceDraftLine.objects.filter(draft=made.draft)
    assert draft_line.unit_amount == D("919.35")  # 13–31 Oct, 19 of 31 days of $1,500.00
    assert invoice_builder.send(made.draft.pk, user(Role.BILLING)).ok
    run_queued_jobs()
    charge = RentCharge.objects.get(lease=lease)
    assert charge.period_month == OCTOBER and charge.amount_due == D("919.35")
    assert charge.proration_text and "19 of 31" in charge.proration_text
    invoice = fresh(made.draft).sent_invoice
    assert invoice is not None and rent_run.provider_invoice(charge) is not None
    # The run on the start date and later creates nothing for October.
    for day in (date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 31)):
        report = rent_run.create_charges(day, actor=None)
        assert report.created == () and report.caught_up == (), day
    assert RentCharge.objects.filter(lease=lease).count() == 1
    assert not OneOffCharge.objects.filter(lease=lease).exists()
    # Paid once through the invoice: the rent charge is paid, nothing else is owed.
    pay(fake, invoice, "919.35")
    charge.refresh_from_db()
    assert charge.status == "paid" and charge.balance == D("0.00")
    owed = [row for row in receivables.open_balances(date(2026, 10, 31), operator.pk)]
    assert owed == []
    refused = rent_run.record_manual_payment(
        charge.pk,
        method=PaymentMethod.ZELLE,
        amount=D("1.00"),
        paid_on=date(2026, 10, 31),
        note="Paid by Zelle",
        actor=None,
        today=date(2026, 10, 31),
    )
    assert not refused.ok
    # The P&L bills and collects the month once, as rent.
    query = pnl.parse_query({"from": "2026-10", "to": "2026-10"}, today=date(2026, 10, 31))
    [row] = [r for r in pnl.rows(query) if r.store_id == lease.store_id]
    assert (row.rent_billed, row.rent_collected, row.fees_billed, row.fees_collected) == (
        D("919.35"),
        D("919.35"),
        D("0.00"),
        D("0.00"),
    )


def test_a_move_in_invoice_counts_each_kind_once(fake: FakeProvider, worker: None) -> None:
    from tests.test_setup_charges import fee

    # The operator's only lease (PLAN-014 C3: it holds the provider account): the run sees
    # this one only.
    operator = an_operator(fake, account=MOVE_IN)
    lease = Lease.objects.get(operator=operator)
    deposit_type = fee("deposit", name="Security deposit", refundable=True)
    sign_fee = fee("sign_fee")
    made = _draft(
        operator,
        lease,
        [
            line(deposit_type.name, "3000.00", "1"),
            line(sign_fee.name, "450.00", "1"),
            _rent_line(lease),
        ],
    )
    assert made.ok and made.draft is not None, made.message
    assert invoice_builder.send(made.draft.pk, user(Role.BILLING)).ok
    run_queued_jobs()
    invoice = fresh(made.draft).sent_invoice
    assert invoice is not None and invoice.total == D("4369.35")
    pay(fake, invoice, "4369.35")
    charge = RentCharge.objects.get(lease=lease)
    assert charge.status == "paid"
    query = pnl.parse_query({"from": "2026-10", "to": "2026-10"}, today=date(2026, 10, 31))
    [row] = [r for r in pnl.rows(query) if r.store_id == lease.store_id]
    assert (row.rent_billed, row.rent_collected) == (D("919.35"), D("919.35"))
    assert (row.fees_billed, row.fees_collected) == (D("450.00"), D("450.00"))
    assert pnl.deposits_held(query) == D("3000.00")
    assert rent_run.create_charges(date(2026, 10, 13), actor=None).caught_up == ()
