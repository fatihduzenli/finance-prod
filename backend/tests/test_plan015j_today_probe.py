"""PLAN-015j probe: how the code behaves TODAY for a Prospective lease (a start after today,
``collection_status="prospective"``). A characterization record, not a specification: each
test asserts what happens now, a refusal with its message. Today is 2026-11-03 (the New lease
page tests' ``today``); the lease starts 2026-11-15 unless a test says otherwise."""

from __future__ import annotations

import dataclasses
import html
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client, override_settings

from accounts.roles import Role
from ledger.models import (
    InstallmentCollection,
    InstallmentPlan,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    MessageRule,
    OneOffCharge,
    Payment,
    RentCharge,
)
from ledger.services import (
    charge_timing,
    deposits,
    installments,
    leases,
    move_in,
    one_off_charges,
    payments_entry,
    pnl,
    receivables,
    reminders,
    start_date,
)
from ledger.services.rent_run import PaymentOutcome
from payments.fake import FakeProvider
from payments.models import Job, JobKind, JobStatus
from tests import test_new_lease_page as new_lease
from tests.job_helpers import run_queued_jobs
from tests.test_new_lease_page import (
    connect,
    create_data,
    deposit_type,
    page,
    post_create,
    shared_world,
    vacant_store,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
today = new_lease.today
admin = new_lease.admin
TODAY = new_lease.TODAY  # 2026-11-03
START = date(2026, 11, 15)
LATER = date(2026, 11, 20)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


def billing_user() -> Any:
    return client_for(Role.BILLING, f"billing-{uuid.uuid4().hex[:6]}")[1]


def save(client: Client, operator: Any = None, **fields: str) -> Lease | None:
    """New lease (rent $2,000.00, start Nov 15, autopay channel) with a $3,000.00 deposit and a
    $500.00 sign fee; the lease saved, or ``None`` when the page refused it."""
    store = vacant_store()
    who = operator or new_lease.an_operator_with("Probe Person", email="probe@example.com")
    price = f"setup_{deposit_type().code}_price"
    data = create_data(store, who, **{price: "3000.00", "setup_sign_fee_price": "500", **fields})
    post_create(client, store, data)
    return Lease.objects.filter(store=store).first()


def the_deposit(lease: Lease) -> OneOffCharge:
    return OneOffCharge.objects.get(lease=lease, fee_type__refundable=True, plan__isnull=True)


def the_sign_fee(lease: Lease) -> OneOffCharge:
    return OneOffCharge.objects.get(lease=lease, fee_type__code="sign_fee")


def send_move_in(client: Client, lease: Lease) -> InvoiceDraft:
    draft = InvoiceDraft.objects.get(lease=lease)
    connect(lease)
    response = client.post(f"/invoices/drafts/{draft.pk}/send/?store={lease.store_id}")
    assert response.status_code == 302
    draft.refresh_from_db()
    return draft


def wire(charge: OneOffCharge, amount: str) -> PaymentOutcome:
    """Staff's manual payment on a one-off charge (the service behind the charge's Add
    payment form)."""
    return one_off_charges.record_payment(
        charge.pk,
        method="wire",
        amount=D(amount),
        paid_on=TODAY,
        note="Wire received before the start.",
        actor=billing_user(),
        creation_key=uuid.uuid4(),
        today=TODAY,
    )


def charge_state(charge: OneOffCharge) -> tuple[Any, ...]:
    charge.refresh_from_db()
    return (charge.price, charge.due_date, charge.status, charge.amount_paid, charge.balance)


def payment_rows(charge: OneOffCharge) -> list[tuple[Any, ...]]:
    return list(
        Payment.objects.filter(one_off_charge=charge)
        .order_by("created_at")
        .values_list("pk", "amount", "status", "settled_on", "one_off_charge_id")
    )


def postpone(lease: Lease, new: date = LATER) -> start_date.StartDateChange:
    return start_date.apply_change(
        lease.pk,
        new,
        reason="Keys handed over later.",
        choice=None,
        move_deposit=False,
        actor=billing_user(),
        today=TODAY,
    )


def _rules_for(charge: OneOffCharge, day: date) -> list[str]:
    return [
        item.rule.code
        for item in reminders.plan(day)
        if item.row is not None and item.row.charge_id == charge.pk
    ]


# --- 1. The move-in invoice sent before the start date ---------------------------------------


def _sent(admin: Client) -> tuple[Lease, InvoiceDraft, Invoice]:
    lease = save(admin)
    assert lease is not None and lease.collection_status == "prospective"
    draft = send_move_in(admin, lease)
    assert draft.status == InvoiceDraftStatus.SENT
    invoice = draft.sent_invoice
    assert invoice is not None
    return lease, draft, invoice


def test_q1_the_move_in_invoice_is_sent_before_the_start_and_due_on_the_start(
    admin: Client, today: date
) -> None:
    lease, draft, invoice = _sent(admin)
    lease.refresh_from_db()
    assert lease.collection_status == "prospective"
    assert (draft.due_date, invoice.due_date, invoice.issue_date) == (START, START, TODAY)
    [rent] = RentCharge.objects.filter(lease=lease)
    assert (rent.period_month, rent.due_on, rent.amount_due) == (
        date(2026, 11, 1),
        START,
        D("1066.67"),
    )
    assert rent.billed_by_invoice_id == invoice.pk
    lines = list(invoice.line_items.order_by("position"))
    assert [line.rent_charge_id for line in lines[:1]] == [rent.pk]
    setup = set(OneOffCharge.objects.filter(lease=lease).values_list("pk", flat=True))
    assert {line.one_off_charge_id for line in lines[1:]} == setup and len(setup) == 2
    assert invoice.total == D("1066.67") + D("3000.00") + D("500.00")


def test_q1_activation_on_the_start_date_adds_no_second_rent_or_setup_charge(
    admin: Client, today: date
) -> None:
    lease, _draft, invoice = _sent(admin)
    charges_before = OneOffCharge.objects.filter(lease=lease).count()
    assert leases.activate_due(START).activated == 1
    lease.refresh_from_db()
    assert (lease.collection_status, lease.started_on) == ("active", START)
    [rent] = RentCharge.objects.filter(lease=lease)
    assert (rent.amount_due, rent.billed_by_invoice_id) == (D("1066.67"), invoice.pk)
    assert OneOffCharge.objects.filter(lease=lease).count() == charges_before == 2
    assert Invoice.objects.filter(line_items__rent_charge__lease=lease).distinct().count() == 1


def test_q1_activate_now_adds_no_second_rent_or_setup_charge(admin: Client, today: date) -> None:
    lease, _draft, invoice = _sent(admin)
    leases.activate(lease.pk, actor=billing_user(), today=TODAY)
    lease.refresh_from_db()
    assert (lease.collection_status, lease.started_on) == ("active", TODAY)
    [rent] = RentCharge.objects.filter(lease=lease)
    # The charge keeps the invoice's prorated amount (15-30 Nov) although the start is Nov 3.
    assert (rent.amount_due, rent.due_on, rent.billed_by_invoice_id) == (
        D("1066.67"),
        START,
        invoice.pk,
    )
    assert OneOffCharge.objects.filter(lease=lease).count() == 2


# --- 2. A manual payment on a setup charge before the start date -----------------------------


def test_q2_the_store_page_payment_entry_refuses_a_prospective_lease(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    assert lease is not None
    fee = the_sign_fee(lease)
    assert payments_entry.current_lease(lease.store, TODAY) is None
    outcome = payments_entry.record_entry(
        lease.store,
        payments_entry.EntryInput(
            creation_key=uuid.uuid4(),
            method="wire",
            amount=D("500.00"),
            paid_on=TODAY,
            reference="W-1",
            note="Wire received.",
            allocations={fee.pk: D("500.00")},
        ),
        actor=billing_user(),
        today=TODAY,
    )
    assert (outcome.ok, outcome.message) == (False, payments_entry.NO_LEASE)
    assert payments_entry.NO_LEASE == (
        "This store has no current lease: there is nothing to pay. Nothing was recorded."
    )
    assert not Payment.objects.exists()
    # The page itself sends staff back to the Charges tab with the same sentence.
    billing = client_for(Role.BILLING, "billing-probe-store")[0]
    response = billing.get(f"/stores/{lease.store_id}/payments/new/")
    assert response.status_code == 302
    assert html.escape(payments_entry.NO_LEASE) in page(billing, response["Location"])


@pytest.mark.parametrize("which", ["sign_fee_on_draft", "dated_deposit"])
def test_q2_the_charge_payment_form_records_partial_then_full_before_the_start(
    admin: Client, today: date, which: str
) -> None:
    timing = {"deposit_when": "on_date", "deposit_on": "2026-12-01"}
    lease = save(admin, **(timing if which == "dated_deposit" else {}))
    assert lease is not None and lease.collection_status == "prospective"
    charge = the_deposit(lease) if which == "dated_deposit" else the_sign_fee(lease)
    price = D(charge.price)
    billing = client_for(Role.BILLING, "billing-probe-charge")[0]
    path = f"/one-off-charges/{charge.pk}/payments/new/"
    for amount in ("200.00", str(price - D("200.00"))):
        response = billing.post(
            path,
            {
                "creation_key": str(uuid.uuid4()),
                "method": "wire",
                "amount": amount,
                "paid_on": TODAY.isoformat(),
                "reference": f"W-{amount}",
                "note": "Wire received before the start.",
            },
        )
        assert response.status_code == 302
    charge.refresh_from_db()
    assert (charge.status, charge.amount_paid, charge.balance) == ("paid", price, D("0.00"))
    assert [row[1] for row in payment_rows(charge)] == [D("200.00"), price - D("200.00")]
    lease.refresh_from_db()
    assert lease.collection_status == "prospective"


def test_q2_the_service_records_a_partial_payment_on_the_deposit_of_the_move_in_draft(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    assert lease is not None
    deposit = the_deposit(lease)
    outcome = wire(deposit, "1000.00")
    assert outcome.ok, outcome.message
    assert charge_state(deposit)[2:] == ("partial", D("1000.00"), D("2000.00"))


# --- 3. A deposit "on a date" before the start date ------------------------------------------


@pytest.mark.parametrize("on", ["2026-11-03", "2026-11-10", "2026-11-14"])
def test_q3_a_deposit_dated_before_the_start_is_accepted(
    admin: Client, today: date, on: str
) -> None:
    lease = save(admin, deposit_when="on_date", deposit_on=on)
    assert lease is not None and lease.collection_status == "prospective"
    deposit = the_deposit(lease)
    assert (deposit.dated, deposit.due_date) == (True, date.fromisoformat(on))
    draft = InvoiceDraft.objects.get(lease=lease)
    assert deposit.pk not in [line.existing_charge_id for line in draft.lines.all()]


def test_q3_a_dated_deposit_before_the_start_is_a_receivable_and_gets_reminders(
    admin: Client, today: date
) -> None:
    lease = save(admin, deposit_when="on_date", deposit_on="2026-11-10")
    assert lease is not None
    deposit = the_deposit(lease)
    assert [row.charge_id for row in receivables.dated_deposits()].count(deposit.pk) == 1
    assert deposits.card_text(lease) == "$3,000.00 · due Nov 10"
    # B three days before, C five days after, while the lease is still prospective.
    assert _rules_for(deposit, date(2026, 11, 7)) == [MessageRule.B]
    assert _rules_for(deposit, date(2026, 11, 8)) == []
    assert _rules_for(deposit, date(2026, 11, 15)) == [MessageRule.C]
    lease.refresh_from_db()
    assert lease.collection_status == "prospective"


# --- 4. A deposit "in installments" starting before the start date ----------------------------


def test_q4_a_first_installment_before_the_start_is_accepted_by_the_form(
    admin: Client, today: date
) -> None:
    """Re-pinned (Opus, PLAN-015j §10): the rule FIRST_BEFORE_START is removed; a first
    installment before the lease's start (and not before today) is accepted."""
    store = vacant_store()
    operator = new_lease.an_operator_with("Early Plan", email="early@example.com")
    code = deposit_type().code
    data = create_data(
        store,
        operator,
        **{
            f"setup_{code}_price": "3000.00",
            "deposit_when": "installments",
            "deposit_count": "3",
            "deposit_first_due": "2026-11-10",
            "deposit_cadence": "monthly",
        },
    )
    response = post_create(admin, store, data)
    assert response.status_code == 302
    lease = Lease.objects.get(store=store)
    plan = InstallmentPlan.objects.get(one_off_charge=the_deposit(lease))
    assert (plan.first_due, plan.count) == (date(2026, 11, 10), 3)
    assert [row.due_date for row in installments.installments_of(plan)] == [
        date(2026, 11, 10),
        date(2026, 12, 10),
        date(2027, 1, 10),
    ]
    assert not hasattr(deposits, "FIRST_BEFORE_START")


def test_q4_the_service_accepts_it_too(today: date) -> None:
    """Re-pinned (Opus, PLAN-015j §10): the service no longer refuses a first installment
    before the start; the plan is made in the same transaction as the lease."""
    from ledger.forms import LeaseCreateForm

    store = vacant_store()
    operator = new_lease.an_operator_with("Early Service", email="service@example.com")
    code = deposit_type().code
    data = create_data(
        store,
        operator,
        **{
            f"setup_{code}_price": "3000.00",
            "deposit_when": "installments",
            "deposit_count": "3",
            "deposit_first_due": "2026-11-15",
            "deposit_cadence": "monthly",
        },
    )
    data["store"] = str(store.pk)
    form = LeaseCreateForm(data, today=TODAY)
    assert form.is_valid(), form.errors
    timing = dataclasses.replace(form.timings()[deposit_type().pk], first_due=date(2026, 11, 10))
    lease, created, charges = charge_timing.create_lease(
        form.cleaned_data,
        rent=form.cleaned_data["rent"],
        creation_key=form.cleaned_data["creation_key"],
        items=form.setup_items(),
        actor=None,
        timings={deposit_type().pk: timing},
        billing=form.billing_values(),
        today=TODAY,
    )
    assert created and lease.store_id == store.pk
    [deposit] = [charge for charge in charges if charge.fee_type == deposit_type()]
    plan = InstallmentPlan.objects.get(one_off_charge=deposit)
    assert (plan.first_due, plan.origin) == (date(2026, 11, 10), "deposit")


# --- 5. A deposit plan collected by the processor's bank debit on a prospective lease ----------


def test_q5_a_deposit_plan_on_a_linked_account_is_autopay_due_and_queues_its_schedules(
    admin: Client, today: date, fake: FakeProvider, worker: None
) -> None:
    holder, operator = shared_world()
    lease = save(
        admin,
        operator,
        account=f"same:{holder.pk}",
        deposit_when="installments",
        deposit_count="2",
        deposit_first_due="2026-11-15",
        deposit_cadence="monthly",
    )
    assert lease is not None and lease.collection_status == "prospective"
    assert leases.linked_payment_method(lease) == "pm-1991"
    assert leases.customer_ref(lease) == "cust-1991"
    deposit = OneOffCharge.objects.get(lease=lease, fee_type__refundable=True)
    plan = InstallmentPlan.objects.get(one_off_charge=deposit)
    assert plan.collection == InstallmentCollection.AUTOPAY_DUE
    rows = installments.installments_of(plan)
    assert [(row.collection, row.due_date) for row in rows] == [
        ("autopay_due", date(2026, 11, 15)),
        ("autopay_due", date(2026, 12, 15)),
    ]
    queued = Job.objects.filter(kind=JobKind.SCHEDULE_CREATE).order_by("created_at")
    assert sorted(job.entity_id for job in queued) == sorted(row.pk for row in rows)
    assert {job.status for job in queued} == {JobStatus.QUEUED}
    # The worker creates them at the processor while the lease is still prospective.
    run_queued_jobs()
    done = Job.objects.filter(kind=JobKind.SCHEDULE_CREATE)
    assert {job.status for job in done} == {JobStatus.DONE}, [job.result for job in done]
    assert len([name for name, _ in fake.calls if name == "create_scheduled_invoice"]) == 2
    lease.refresh_from_db()
    assert lease.collection_status == "prospective"
    # Rent autopay is still refused on the same lease.
    assert leases.NOT_ACTIVE in leases.autopay_blockers(lease)


# --- 6. Postponing the start with a setup charge partly or fully paid -------------------------


@pytest.mark.parametrize("amount", ["1200.00", "3000.00"], ids=["partial", "full"])
def test_q6_postponing_leaves_a_paid_dated_deposit_as_it_was(
    admin: Client, today: date, amount: str
) -> None:
    lease = save(admin, deposit_when="on_date", deposit_on="2026-11-10")
    assert lease is not None
    deposit = the_deposit(lease)
    outcome = wire(deposit, amount)
    assert outcome.ok, outcome.message
    before = charge_state(deposit)
    payments_before = payment_rows(deposit)
    change = postpone(lease)
    assert change.outcome == start_date.OUTCOME_REBUILT  # the unsent draft made again
    assert (change.paid_back, change.extra) == (D("0"), D("0"))
    lease.refresh_from_db()
    assert lease.started_on == LATER
    assert charge_state(deposit) == before
    assert before[1] == date(2026, 11, 10)
    assert payment_rows(deposit) == payments_before


def test_q6_postponing_with_a_partly_paid_sent_move_in_invoice(admin: Client, today: date) -> None:
    lease, _draft, invoice = _sent(admin)
    fee = the_sign_fee(lease)
    outcome = wire(fee, "200.00")
    assert outcome.ok, outcome.message
    before = charge_state(fee)
    payments_before = payment_rows(fee)
    [rent] = RentCharge.objects.filter(lease=lease)
    preview = start_date.preview(lease, LATER, today=TODAY)
    assert preview.ok
    assert (preview.draft_state, preview.needs_choice, preview.moves_money) == (
        start_date.DRAFT_PAID,
        False,
        False,
    )
    assert f"The move-in invoice {invoice.invoice_number} is paid: it stays as it is." in (
        preview.effects
    )
    change = postpone(lease)  # billing, no choice: accepted
    assert (change.outcome, change.draft_state) == ("", start_date.DRAFT_PAID)
    assert not Job.objects.filter(kind=JobKind.INVOICE_VOID).exists()
    invoice.refresh_from_db()
    assert invoice.internal_status != "void"
    # The sign fee and its payment are unchanged.
    assert charge_state(fee) == before and payment_rows(fee) == payments_before
    # The invoice's rent charge is re-priced for the new start (20-30 Nov) and re-dated,
    # although the sent invoice still bills $1,066.67 for it.
    rent.refresh_from_db()
    assert (rent.amount_due, rent.due_on, rent.billed_by_invoice_id) == (
        D("733.33"),
        LATER,
        invoice.pk,
    )
    assert invoice.line_items.get(rent_charge=rent).line_total == D("1066.67")
    assert invoice.total == D("4566.67")
    assert move_in.current_draft(lease) is None


# --- 7. The P&L: a sign fee paid before the start date -----------------------------------------


def _november_fees() -> tuple[Decimal, Decimal]:
    found = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 11, 1), date(2026, 11, 1))))
    return found.fees_billed, found.fees_collected


def test_q7_a_sign_fee_paid_before_the_start_is_fees_collected_while_prospective(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    assert lease is not None
    fee = the_sign_fee(lease)
    outcome = wire(fee, "500.00")
    assert outcome.ok, outcome.message
    assert Payment.objects.get(one_off_charge=fee).settled_on == TODAY
    assert _november_fees() == (D("500.00"), D("500.00"))
    assert leases.activate_due(START).activated == 1
    assert _november_fees() == (D("500.00"), D("500.00"))
