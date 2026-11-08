"""PLAN-015d task 5: the move-in draft rebuilt for a new start date, and the void of a sent,
unpaid invoice — rent alone or the move-in invoice with its deposit — through the
``invoice_void`` job (docs/qa/15/PLAN-015d §3 (1)). The FakeProvider stands in for the
processor; nothing here reaches a real one."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceStatus,
    Lease,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import invoice_builder, move_in, rent_run
from payments.fake import FakeProvider
from payments.models import JobKind, JobStatus
from tests import test_invoice_builder as builder_tests
from tests.job_helpers import run_queued_jobs
from tests.test_builder_lines import a_deposit
from tests.test_invoice_builder import (
    NOVEMBER,
    OCTOBER,
    TODAY,
    a_draft,
    a_rent_lease,
    fresh,
    rent_line,
    sent,
    user,
)
from tests.test_one_off_charges import an_operator, calls

pytestmark = pytest.mark.django_db

fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker
D = Decimal


def a_move_in_draft(fake: FakeProvider) -> tuple[Lease, InvoiceDraft]:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)  # starts Oct 14, rent $1,000.00
    deposit = a_deposit(lease)
    made = move_in.create_draft(
        lease, [deposit], creation_key=uuid.uuid4(), actor=user(Role.BILLING), today=TODAY
    )
    assert made.draft is not None, made.message
    return lease, made.draft


# --- the rebuild -------------------------------------------------------------------------------


def test_the_unsent_draft_is_made_again_for_the_new_start_with_a_new_key(
    fake: FakeProvider,
) -> None:
    lease, old = a_move_in_draft(fake)
    assert move_in.current_draft(lease) == old
    Lease.objects.filter(pk=lease.pk).update(started_on=date(2026, 11, 10))
    lease.refresh_from_db()
    made = move_in.rebuild_draft(lease, user(Role.BILLING), today=TODAY)
    assert made.draft is not None and made.draft.pk != old.pk
    assert not InvoiceDraft.objects.filter(pk=old.pk).exists()
    rent, deposit = invoice_builder.lines_of(made.draft)
    assert (rent.rent_month, rent.unit_amount) == (NOVEMBER, D("700.00"))  # 21 of 30 days
    assert deposit.existing_charge_id is not None
    assert made.draft.due_date == date(2026, 11, 10)
    audit = AuditLog.objects.get(action=invoice_builder.DELETED_ACTION, entity_id=str(old.pk))
    assert audit.before["rebuilt"] is True
    assert not fake.calls


def test_without_a_move_in_draft_nothing_is_rebuilt(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    made = move_in.rebuild_draft(lease, None, today=TODAY)
    assert (made.draft, made.message) == (None, move_in.NO_DRAFT)


# --- the void of a rent-alone invoice -------------------------------------------------------------


def test_a_rent_alone_invoice_is_deleted_once_voided_and_its_charge_void(
    fake: FakeProvider, worker: None
) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    invoice = sent(a_draft(operator, lease, [rent_line(lease)])).invoice
    assert invoice is not None
    run_queued_jobs()
    charge = RentCharge.objects.get(lease=lease, period_month=OCTOBER)
    job = move_in.request_void(invoice, user(Role.BILLING), rent=move_in.RENT_VOID, redraft=False)
    assert (job.kind, job.entity_id) == (JobKind.INVOICE_VOID, charge.pk)
    run_queued_jobs()
    assert fresh(job).status == JobStatus.DONE
    assert len(calls(fake, "delete_invoice")) == 1
    assert fresh(invoice).internal_status == InvoiceStatus.VOID
    charge = fresh(charge)
    assert (charge.status, charge.balance) == (RentChargeStatus.VOID, D("0.00"))
    assert charge.billed_by_invoice_id == invoice.pk  # kept as history
    assert AuditLog.objects.filter(action=rent_run.VOIDED_ACTION, entity_id=str(charge.pk)).exists()
    # A second run deletes nothing more.
    move_in.request_void(fresh(invoice), None, rent=move_in.RENT_VOID, redraft=False)
    run_queued_jobs()
    assert len(calls(fake, "delete_invoice")) == 1


def test_an_invoice_paid_meanwhile_is_not_voided(fake: FakeProvider, worker: None) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    invoice = sent(a_draft(operator, lease, [rent_line(lease)])).invoice
    assert invoice is not None
    run_queued_jobs()
    charge = RentCharge.objects.get(lease=lease, period_month=OCTOBER)
    Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("100.00"),
        status=PaymentStatus.PENDING,
        initiated_on=TODAY,
    )
    job = move_in.request_void(invoice, None, rent=move_in.RENT_VOID, redraft=False)
    run_queued_jobs()
    assert fresh(job).status == JobStatus.FAILED
    assert not calls(fake, "delete_invoice")
    assert fresh(invoice).internal_status != InvoiceStatus.VOID
    assert fresh(charge).status != RentChargeStatus.VOID


# --- the void of the move-in invoice, redrafted for the new start ---------------------------


def test_the_move_in_invoice_is_voided_the_deposit_detached_and_a_new_draft_made(
    fake: FakeProvider, worker: None
) -> None:
    lease, draft = a_move_in_draft(fake)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    deposit = invoice_builder.lines_of(draft)[1].existing_charge
    assert deposit is not None and fresh(deposit).collection == CollectionMethod.EMAIL
    october = RentCharge.objects.get(lease=lease, period_month=OCTOBER)
    # The start moves into November: October's charge never falls due.
    Lease.objects.filter(pk=lease.pk).update(started_on=date(2026, 11, 10))
    lease.rent_history.update(effective_from=date(2026, 11, 10))
    move_in.request_void(invoice, user(Role.BILLING), rent=move_in.RENT_FOLLOW_START, redraft=True)
    run_queued_jobs()
    assert len(calls(fake, "delete_invoice")) == 1
    assert fresh(invoice).internal_status == InvoiceStatus.VOID
    assert fresh(october).status == RentChargeStatus.VOID
    assert fresh(deposit).collection == CollectionMethod.NONE
    new = move_in.current_draft(lease)
    assert new is not None and new.status == InvoiceDraftStatus.DRAFT
    rent, again = invoice_builder.lines_of(new)
    assert (rent.rent_month, rent.unit_amount) == (NOVEMBER, D("700.00"))
    assert again.existing_charge_id == deposit.pk


def test_a_start_moved_within_its_month_reprices_the_months_charge(
    fake: FakeProvider, worker: None
) -> None:
    lease, draft = a_move_in_draft(fake)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    october = RentCharge.objects.get(lease=lease, period_month=OCTOBER)
    assert october.amount_due == D("580.65")
    Lease.objects.filter(pk=lease.pk).update(started_on=date(2026, 10, 21))
    lease.rent_history.update(effective_from=date(2026, 10, 21))
    move_in.request_void(invoice, user(Role.BILLING), rent=move_in.RENT_FOLLOW_START, redraft=True)
    run_queued_jobs()
    october = fresh(october)
    # 21–31 Oct, 11 of 31 days of $1,000.00.
    assert (october.status, october.amount_due, october.due_on) == (
        RentChargeStatus.DUE,
        D("354.84"),
        date(2026, 10, 21),
    )
    new = move_in.current_draft(lease)
    assert new is not None
    assert invoice_builder.lines_of(new)[0].unit_amount == D("354.84")
    assert AuditLog.objects.filter(action=rent_run.REAMOUNTED_ACTION).exists()
