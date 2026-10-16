"""The end-of-lease wizard (docs/qa/PLAN-012e-lease-end-transfer-installments.md D3 to D5,
D7, D8, D13; §6 "Lease end"): the last month, the blockers, the statement arithmetic,
the settlement, the confirm and its effects, the PDF and its e-mail, the pages and the
roles. Nothing reaches the payment provider; the adversarial cases are the QA owner's.
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from pypdf import PdfReader

import documents_storage
import mail
from accounts import registry
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    DeductionKind,
    DepositResolution,
    Document,
    DocumentKind,
    InstallmentPlan,
    InstallmentPlanStatus,
    InsuranceCharge,
    Invoice,
    InvoiceStatus,
    Lease,
    LeaseEnd,
    LeaseEndStatus,
    MessageKind,
    MessageStatus,
    OneOffCategory,
    OneOffCharge,
    OutboundMessage,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RefundMethod,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import (
    deposits,
    installments,
    lease_end,
    leases,
    pnl,
    rent_run,
    statements,
)
from ledger.services import one_off_charges as one_off_service
from ledger.services.lease_end import DeductionInput
from ledger.services.leases import AutopayAction
from ledger.validators import validate_no_payment_data
from mail.base import mime_bytes
from mail.memory import MemoryMailSender
from payments import invoice_sync
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import ScheduleRecord
from tests.job_helpers import run_queued_jobs
from tests.test_operator_views import client_for
from tests.test_setup_charges import a_lease, deposit_type, item, pay

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 20)
ENDED = date(2026, 10, 19)
OCTOBER = date(2026, 10, 1)
RENT = D("2000.00")
#: 19 of 31 days of $2,000.00, rounded once (D1).
PRORATED = D("1225.81")
TEXT = "1–19 Oct, 19 of 31 days of $2,000.00 = $1,225.81"
CATCH_ALL = "catchall@example.com"
#: ABA-valid routing number and an account number, synthetic (as the guard tests).
ROUTING_AND_ACCOUNT = "123456780 987654321012"


# --- Fixtures and builders ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    memory = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: memory)
    return memory


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)


@pytest.fixture
def admin() -> tuple[Client, User]:
    return client_for(Role.ADMIN)


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, settings: Any) -> MemoryMailSender:
    settings.MAIL_ENABLED = True
    settings.MAIL_CATCH_ALL = CATCH_ALL
    settings.APP_ENV = "staging"
    sender = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: sender)
    return sender


def a_store_lease(**overrides: Any) -> Lease:
    values: dict[str, Any] = {"rent": RENT, "debit_day": 1}
    values.update(overrides)
    return a_lease(**values)


def october(lease: Lease, paid: str = "0.00", **fields: Any) -> RentCharge:
    charge = RentCharge.objects.create(
        lease=lease,
        period_month=OCTOBER,
        seq=fields.pop("seq", 1),
        due_on=fields.pop("due_on", OCTOBER),
        amount_due=fields.pop("amount_due", RENT),
        status=RentChargeStatus.DUE,
        **fields,
    )
    if D(paid):
        outcome = rent_run.record_manual_payment(
            charge.pk,
            method=PaymentMethod.WIRE,
            amount=D(paid),
            paid_on=OCTOBER,
            note="Wire received.",
            actor=None,
            today=TODAY,
        )
        assert outcome.ok, outcome.message
    return RentCharge.objects.get(pk=charge.pk)


def deposit(lease: Lease, amount: str = "2000.00", paid: str | None = None) -> OneOffCharge:
    outcome = one_off_service.add_charge(lease.pk, item(deposit_type(), amount), actor=None)
    assert outcome.ok and outcome.charge is not None, outcome.message
    if D(paid if paid is not None else amount):
        assert pay(outcome.charge, paid if paid is not None else amount).ok
    return OneOffCharge.objects.get(pk=outcome.charge.pk)


def draft(
    lease: Lease,
    actor: User | None = None,
    *,
    ended_on: date = ENDED,
    reason: str = "moved_out",
    prorate: bool = True,
    note: str = "",
) -> LeaseEnd:
    end = lease_end.start(lease, actor)
    outcome = lease_end.set_terms(
        end, ended_on, reason, prorate, note=note, actor=actor, today=TODAY
    )
    assert outcome.ok, outcome.message
    return LeaseEnd.objects.get(pk=end.pk)


def deductions(end: LeaseEnd, *lines: tuple[str, str, str]) -> lease_end.Outcome:
    return lease_end.set_deductions(
        end,
        [DeductionInput(kind, description, D(amount)) for kind, description, amount in lines],
        actor=None,
        today=TODAY,
    )


def settle(end: LeaseEnd, method: str = RefundMethod.ZELLE, **fields: Any) -> lease_end.Outcome:
    values: dict[str, Any] = {
        "on": TODAY,
        "reference": "ZL-77821",
        "note": "Keys returned.",
        "actor": None,
        "today": TODAY,
    }
    values.update(fields)
    on = values.pop("on")
    reference = values.pop("reference")
    note = values.pop("note")
    return lease_end.set_settlement(end, method, on, reference, note, **values)


def confirm(end: LeaseEnd, actor: User | None = None) -> lease_end.Outcome:
    return lease_end.confirm(end, actor, confirmed=True, today=TODAY)


def figures(end: LeaseEnd) -> statements.Final:
    return statements.final(LeaseEnd.objects.get(pk=end.pk), today=TODAY)


def fresh(row: Any) -> Any:
    return type(row).objects.get(pk=row.pk)


def provider_ref(entity_type: str, entity_id: uuid.UUID, kind: str) -> None:
    ExternalRef.objects.create(
        entity_type=entity_type,
        entity_id=entity_id,
        provider=leases.PROVIDER_KEY,
        external_kind=kind,
        external_id=f"fake-{kind}-{uuid.uuid4().hex[:8]}",
    )


def an_email_charge(lease: Lease, live: bool = True) -> OneOffCharge:
    """A one-off charge sent by e-mail whose invoice has a provider reference."""
    charge = OneOffCharge.objects.create(
        operator=lease.operator,
        lease=lease,
        category=OneOffCategory.DAMAGE,
        collection="email",
        description="Broken shelf",
        price=D("300.00"),
        due_date=OCTOBER,
    )
    assert lease.operator is not None
    invoice = Invoice.objects.create(
        invoice_number=f"INV-2026-{uuid.uuid4().int % 10000:04d}",
        operator=lease.operator,
        one_off_charge=charge,
        issue_date=OCTOBER,
        due_date=OCTOBER,
        subtotal=D("300.00"),
        total=D("300.00"),
        internal_status=InvoiceStatus.SENT if live else InvoiceStatus.VOID,
    )
    provider_ref("invoice", invoice.pk, "invoice")
    return charge


def a_schedule(
    lease: Lease, kind: str = ScheduleKind.RENT, state: str = ScheduleState.ACTIVE
) -> None:
    row = CollectionSchedule.objects.create(
        lease=lease, kind=kind, seq=1, debit_day=1, amount=RENT, state=state
    )
    provider_ref(leases.ENTITY_SCHEDULE, row.pk, leases.KIND_SCHEDULE)


def actions(prefix: str = "lease_end.") -> list[str]:
    return list(
        AuditLog.objects.filter(action__startswith=prefix)
        .order_by("id")
        .values_list("action", flat=True)
    )


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


# --- D3: the last month ----------------------------------------------------------------------


def test_an_unpaid_last_month_is_reduced_to_the_days_used() -> None:
    lease = a_store_lease()
    charge = october(lease)
    end = draft(lease)
    last = figures(end).last_month
    assert last is not None and (last.amount, last.text, last.credit) == (PRORATED, TEXT, D("0"))
    assert confirm(end).ok
    reduced = fresh(charge)
    assert (reduced.amount_due, reduced.status, reduced.proration_text) == (
        PRORATED,
        RentChargeStatus.DUE,
        TEXT,
    )
    entry = AuditLog.objects.get(action="rent_charge.prorated")
    assert entry.before is not None and entry.after is not None
    assert (entry.before["amount_due"], entry.after["amount_due"]) == ("2000.00", "1225.81")


def test_a_paid_last_month_gives_the_excess_back_as_a_credit_refund() -> None:
    lease = a_store_lease()
    charge = october(lease, paid="2000.00")
    end = draft(lease)
    found = figures(end)
    assert (found.credit, found.refund_due, found.open_total) == (D("774.19"), D("774.19"), D(0))
    refused = confirm(end)
    assert not refused.ok and refused.message == lease_end.SETTLEMENT_REQUIRED
    assert settle(end, RefundMethod.WIRE).ok
    assert confirm(end).ok
    after = fresh(charge)
    assert (after.amount_due, after.amount_paid, after.status) == (
        PRORATED,
        PRORATED,
        RentChargeStatus.PAID,
    )
    refund = Payment.objects.get(rent_charge=charge, is_refund=True)
    assert (refund.amount, refund.method, refund.status) == (
        D("774.19"),
        PaymentMethod.WIRE,
        PaymentStatus.SETTLED,
    )
    assert refund.parent_payment is not None and refund.reference == "ZL-77821"
    assert fresh(end).refund_payment == refund


def test_a_partly_paid_month_above_the_prorated_amount_refunds_only_the_excess() -> None:
    lease = a_store_lease()
    charge = october(lease, paid="1500.00")
    end = draft(lease)
    assert figures(end).credit == D("274.19")
    assert settle(end).ok and confirm(end).ok
    assert (fresh(charge).amount_due, fresh(charge).amount_paid) == (PRORATED, PRORATED)


def test_without_a_charge_the_prorated_month_is_created_and_owed() -> None:
    lease = a_store_lease()
    end = draft(lease)
    found = figures(end)
    assert found.open_total == PRORATED
    assert [line.description for line in found.open_lines] == [f"Rent 2026-10: {TEXT}"]
    assert confirm(end).ok
    charge = RentCharge.objects.get(lease=lease, period_month=OCTOBER)
    assert (charge.amount_due, charge.status, charge.proration_text) == (
        PRORATED,
        RentChargeStatus.DUE,
        TEXT,
    )
    assert not Job.objects.exclude(kind=JobKind.LEASE_END_DOCUMENTS).exists()
    assert fresh(end).statement_json["amount_owed"] == "1225.81"


def test_prorate_off_keeps_the_charge_and_creates_a_full_month_when_missing() -> None:
    kept = a_store_lease()
    charge = october(kept)
    end = draft(kept, prorate=False)
    assert figures(end).last_month is not None
    assert confirm(end).ok
    assert fresh(charge).amount_due == RENT and fresh(charge).proration_text is None
    created = a_store_lease()
    other = draft(created, prorate=False)
    assert confirm(other).ok
    assert RentCharge.objects.get(lease=created).amount_due == RENT


def test_prorate_off_creates_nothing_when_the_debit_day_falls_after_the_end_date() -> None:
    lease = a_store_lease(debit_day=25)
    end = draft(lease, prorate=False)
    assert confirm(end).ok
    assert not RentCharge.objects.filter(lease=lease).exists()


def test_a_lease_that_starts_and_ends_in_one_month_is_prorated_between_both_days() -> None:
    lease = a_store_lease(started_on=date(2026, 10, 13))
    end = draft(lease)
    last = figures(end).last_month
    assert last is not None
    assert last.amount == D("451.61")  # 7 of 31 days of 2,000.00
    assert last.text == "13–19 Oct, 7 of 31 days of $2,000.00 = $451.61"


def test_ending_on_the_last_day_is_a_full_month_without_text() -> None:
    lease = a_store_lease()
    charge = october(lease)
    end = draft(lease, ended_on=date(2026, 9, 30))
    september = RentCharge.objects.create(
        lease=lease,
        period_month=date(2026, 9, 1),
        due_on=date(2026, 9, 1),
        amount_due=RENT,
        status=RentChargeStatus.DUE,
    )
    last = figures(end).last_month
    assert last is not None and (last.amount, last.text, last.changes) == (RENT, "", ())
    assert confirm(end).ok
    assert fresh(september).amount_due == RENT
    # A month after the end month is never touched by the wizard nor charged by the run.
    assert fresh(charge).amount_due == RENT
    run = rent_run.leases_due(date(2026, 11, 1))
    assert all(item.lease.pk != lease.pk for item in run.due)


# --- D5(b): blockers ------------------------------------------------------------------------


def test_a_clean_lease_has_no_blocker() -> None:
    assert lease_end.blockers(a_store_lease()) == []


def test_a_live_invoice_blocks_with_a_link_to_its_charge() -> None:
    lease = a_store_lease()
    charge = an_email_charge(lease)
    [found] = lease_end.blockers(lease)
    assert found.kind == lease_end.LIVE_INVOICE
    assert found.url == f"/one-off-charges/{charge.pk}/"
    assert "is live at the payment provider" in found.text


def test_a_voided_invoice_or_a_paid_charge_does_not_block() -> None:
    lease = a_store_lease()
    an_email_charge(lease, live=False)
    paid = an_email_charge(lease)
    OneOffCharge.objects.filter(pk=paid.pk).update(status="paid", amount_paid=D("300.00"))
    assert lease_end.blockers(lease) == []


def a_rent_invoice(charge: RentCharge, *, provider_status: str = "") -> Invoice:
    """A Stax invoice linked to a rent charge, with a current provider reference; its
    provider status written by the sync layer when given."""
    lease = charge.lease
    assert lease.operator is not None
    invoice = Invoice.objects.create(
        invoice_number=f"INV-2026-{uuid.uuid4().int % 10000:04d}",
        operator=lease.operator,
        rent_charge=charge,
        issue_date=OCTOBER,
        due_date=OCTOBER,
        subtotal=charge.amount_due,
        total=charge.amount_due,
        internal_status=InvoiceStatus.SENT,
    )
    provider_ref("invoice", invoice.pk, "invoice")
    if provider_status:
        invoice_sync.record_provider_status(invoice.pk, provider_status)
    return invoice


def test_a_rent_charge_with_a_live_invoice_blocks_with_a_link_to_its_charge() -> None:
    lease = a_store_lease()
    charge = october(lease)
    invoice = a_rent_invoice(charge)
    [found] = lease_end.blockers(lease)
    assert found.kind == lease_end.LIVE_INVOICE
    assert found.url == f"/charges/{charge.pk}/"
    assert found.text.startswith(f"Rent 2026-10: invoice {invoice.invoice_number} is live")


def test_a_rent_charge_with_a_live_invoice_refuses_the_confirm() -> None:
    lease = a_store_lease()
    charge = october(lease)
    end = draft(lease)
    a_rent_invoice(charge)
    outcome = confirm(end)
    assert not outcome.ok and outcome.message == lease_end.BLOCKED.format(count=1)
    assert fresh(charge).amount_due == RENT and fresh(lease).ended_on is None


def test_a_paid_rent_charge_or_a_deleted_invoice_does_not_block() -> None:
    lease = a_store_lease()
    a_rent_invoice(october(lease, paid="2000.00"))
    a_rent_invoice(october(lease, seq=2, due_on=date(2026, 10, 15)), provider_status="DELETED")
    assert lease_end.blockers(lease) == []


def test_an_insurance_charge_with_a_live_invoice_blocks_until_it_is_paid() -> None:
    lease = a_store_lease()
    insurance = InsuranceCharge.objects.create(
        lease=lease, policy_year=2026, due_on=OCTOBER, amount=D("120.00")
    )
    assert lease.operator is not None
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-9001",
        operator=lease.operator,
        insurance_charge=insurance,
        issue_date=OCTOBER,
        due_date=OCTOBER,
        subtotal=D("120.00"),
        total=D("120.00"),
        internal_status=InvoiceStatus.SENT,
    )
    provider_ref("invoice", invoice.pk, "invoice")
    [found] = lease_end.blockers(lease)
    assert found.kind == lease_end.LIVE_INVOICE and found.url == f"/leases/{lease.pk}/"
    assert found.text.startswith("Insurance, policy year 2026: invoice INV-2026-9001 is live")
    Payment.objects.create(
        insurance_charge=insurance,
        kind=PaymentKind.INSURANCE,
        policy_year=2026,
        method=PaymentMethod.WIRE,
        amount=D("120.00"),
        status=PaymentStatus.SETTLED,
        initiated_on=OCTOBER,
        settled_on=OCTOBER,
        note="Wire received.",
    )
    assert lease_end.blockers(lease) == []


def test_a_one_off_provider_schedule_blocks() -> None:
    lease = a_store_lease()
    charge = OneOffCharge.objects.create(
        operator=lease.operator,
        lease=lease,
        category=OneOffCategory.OTHER,
        collection="autopay_due",
        description="Sign",
        price=D("50.00"),
        due_date=date(2026, 11, 1),
    )
    provider_ref("one_off_charge", charge.pk, "schedule")
    assert [item.kind for item in lease_end.blockers(lease)] == [lease_end.ONE_OFF_SCHEDULE]


@pytest.mark.parametrize("state", [ScheduleState.ACTIVE, ScheduleState.PAUSED])
@pytest.mark.parametrize("kind", [ScheduleKind.RENT, ScheduleKind.INSURANCE])
def test_an_active_or_paused_schedule_with_a_reference_blocks(kind: str, state: str) -> None:
    lease = a_store_lease()
    a_schedule(lease, kind, state)
    [found] = lease_end.blockers(lease)
    assert found.kind == lease_end.AUTOPAY and found.url == f"/leases/{lease.pk}/stop-autopay/"
    assert ("paused" if state == ScheduleState.PAUSED else "active") in found.text


def test_a_schedule_without_a_reference_or_ended_does_not_block() -> None:
    lease = a_store_lease()
    CollectionSchedule.objects.create(
        lease=lease, kind=ScheduleKind.RENT, seq=1, debit_day=1, amount=RENT, state="failed"
    )
    a_schedule(lease, state=ScheduleState.ENDED)
    assert lease_end.blockers(lease) == []


def test_a_pending_payment_blocks_with_a_link_to_it() -> None:
    lease = a_store_lease()
    charge = october(lease)
    payment = Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=RENT,
        status=PaymentStatus.PENDING,
        initiated_on=OCTOBER,
    )
    [found] = lease_end.blockers(lease)
    assert found.kind == lease_end.PENDING_PAYMENT and found.url == f"/payments/{payment.pk}/"


def test_every_blocker_is_listed_together() -> None:
    lease = a_store_lease()
    an_email_charge(lease)
    a_schedule(lease)
    Payment.objects.create(
        rent_charge=october(lease),
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=RENT,
        status=PaymentStatus.PENDING,
    )
    assert sorted(item.kind for item in lease_end.blockers(lease)) == [
        lease_end.AUTOPAY,
        lease_end.LIVE_INVOICE,
        lease_end.PENDING_PAYMENT,
    ]


def test_confirm_checks_the_blockers_again_and_writes_nothing() -> None:
    lease = a_store_lease()
    october(lease)
    end = draft(lease)
    an_email_charge(lease)  # appears between the steps
    before = (
        AuditLog.objects.count(),
        Payment.objects.count(),
        RentCharge.objects.get().amount_due,
    )
    outcome = confirm(end)
    assert not outcome.ok and outcome.message == lease_end.BLOCKED.format(count=1)
    assert (
        AuditLog.objects.count(),
        Payment.objects.count(),
        RentCharge.objects.get().amount_due,
    ) == before
    assert fresh(lease).ended_on is None and fresh(end).status == LeaseEndStatus.DRAFT


def a_wire_lease() -> Lease:
    """A lease paid by wire: its charges are collected by hand, so a split makes nothing
    at the payment provider."""
    return a_store_lease(payment_channel=PaymentChannel.WIRE)


def a_manual_charge(lease: Lease) -> OneOffCharge:
    return OneOffCharge.objects.create(
        operator=lease.operator,
        lease=lease,
        category=OneOffCategory.DAMAGE,
        collection="manual",
        description="Broken shelf",
        price=D("300.00"),
        due_date=OCTOBER,
    )


def a_live_plan(parent: RentCharge | OneOffCharge, count: int = 2) -> InstallmentPlan:
    first = date(2026, 10, 25)
    lines, refused = installments.preview(parent, count, first, "monthly", today=TODAY)
    assert refused is None, refused
    made = installments.create_plan(parent, count, first, "monthly", lines, None, today=TODAY)
    assert made.ok and made.plan is not None, made.message
    return made.plan


@pytest.mark.parametrize("parent_kind", ["rent", "one_off"])
def test_a_live_installment_plan_blocks_with_a_link_to_the_plan(parent_kind: str) -> None:
    lease = a_wire_lease()
    parent = october(lease) if parent_kind == "rent" else a_manual_charge(lease)
    plan = a_live_plan(parent)
    [found] = lease_end.blockers(lease)
    assert found.kind == lease_end.INSTALLMENT_PLAN
    assert found.url == f"/plans/{plan.pk}/"
    label = "Rent 2026-10" if isinstance(parent, RentCharge) else parent.label
    assert found.text == (
        f"{label}: an installment plan is live (0 of 2 paid). Let it complete or cancel it first."
    )


def test_a_plan_that_needs_attention_still_blocks() -> None:
    lease = a_wire_lease()
    plan = a_live_plan(a_manual_charge(lease))
    InstallmentPlan.objects.filter(pk=plan.pk).update(status=InstallmentPlanStatus.ATTENTION)
    assert [item.kind for item in lease_end.blockers(lease)] == [lease_end.INSTALLMENT_PLAN]


def test_a_completed_installment_plan_does_not_block() -> None:
    lease = a_wire_lease()
    plan = a_live_plan(a_manual_charge(lease))
    for row in installments.installments_of(plan):
        assert pay(row, str(row.price), paid_on=TODAY, today=TODAY).ok
    assert fresh(plan).status == InstallmentPlanStatus.COMPLETED
    assert lease_end.blockers(lease) == []


def test_a_cancelled_installment_plan_does_not_block() -> None:
    lease = a_wire_lease()
    plan = a_live_plan(october(lease))
    _, admin_user = client_for(Role.ADMIN)
    assert installments.cancel(plan, "Ending the lease.", admin_user).ok
    assert lease_end.blockers(lease) == []


def test_confirm_refuses_while_an_installment_plan_is_live() -> None:
    lease = a_wire_lease()
    charge = a_manual_charge(lease)
    end = draft(lease)
    a_live_plan(charge)  # made between the steps
    before = (AuditLog.objects.count(), Payment.objects.count(), OneOffCharge.objects.count())
    outcome = confirm(end)
    assert not outcome.ok and outcome.message == lease_end.BLOCKED.format(count=1)
    assert (
        AuditLog.objects.count(),
        Payment.objects.count(),
        OneOffCharge.objects.count(),
    ) == before
    assert fresh(lease).ended_on is None and fresh(end).status == LeaseEndStatus.DRAFT


# --- D5(c), (d): the statement and the settlement -------------------------------------------


def test_no_deposit_and_no_deduction_owes_the_open_balance() -> None:
    lease = a_store_lease()
    october(lease)
    found = figures(draft(lease))
    assert (found.deposit_held, found.refund_due, found.amount_owed) == (
        D(0),
        D(0),
        PRORATED,
    )


def test_a_deposit_equal_to_the_deductions_refunds_and_owes_nothing() -> None:
    lease = a_store_lease()
    deposit(lease, "500.00")
    end = draft(lease)
    assert deductions(end, ("damage", "Door", "300.00"), ("cleaning", "", "200.00")).ok
    found = figures(end)
    # Without a rent charge the prorated month is created and owed.
    assert (found.withheld, found.deposit_refund, found.debt) == (D("500.00"), D(0), D(0))
    assert confirm(end).ok
    resolution = DepositResolution.objects.get()
    assert (resolution.kind, resolution.amount, resolution.withheld_amount) == (
        "withheld",
        D("500.00"),
        D("500.00"),
    )


def test_a_deposit_above_the_deductions_refunds_the_rest_by_the_chosen_method() -> None:
    lease = a_store_lease()
    october(lease, paid=str(RENT))
    held = deposit(lease, "2000.00")
    end = draft(lease)
    assert deductions(end, ("cleaning", "Floors", "150.00")).ok
    assert settle(end, RefundMethod.CHECK, reference="Check 1043").ok
    found = figures(end)
    assert (found.deposit_refund, found.credit, found.refund_due) == (
        D("1850.00"),
        D("774.19"),
        D("2624.19"),
    )
    assert confirm(end).ok
    resolution = DepositResolution.objects.get(setup_charge=held)
    assert (resolution.kind, resolution.amount, resolution.withheld_amount) == (
        "refunded",
        D("1850.00"),
        D("150.00"),
    )
    assert resolution.method == PaymentMethod.OTHER  # a check is recorded as other
    refund = Payment.objects.get(one_off_charge=held, kind=PaymentKind.REFUND_DEPOSIT)
    assert (refund.amount, refund.reference) == (D("1850.00"), "Check 1043")
    assert "Paid by check." in refund.note
    assert fresh(end).refund_payment == refund


def test_a_deposit_below_the_deductions_charges_the_lease_end_balance() -> None:
    lease = a_store_lease()
    october(lease, paid="1225.81")  # the prorated month exactly: nothing open
    deposit(lease, "500.00")
    end = draft(lease)
    assert deductions(end, ("damage", "Broken door", "800.00")).ok
    found = figures(end)
    assert (found.withheld, found.debt, found.amount_owed, found.refund_due) == (
        D("500.00"),
        D("300.00"),
        D("300.00"),
        D(0),
    )
    assert confirm(end).ok
    debt = fresh(end).debt_charge
    assert debt is not None
    assert (debt.description, debt.price, debt.category, debt.collection, debt.due_date) == (
        lease_end.DEBT_DESCRIPTION,
        D("300.00"),
        OneOffCategory.OTHER,
        "manual",
        date(2026, 11, 3),
    )
    assert not Invoice.objects.filter(one_off_charge=debt).exists()
    assert not Job.objects.filter(entity_id=debt.pk).exists()


def test_the_offset_pays_the_oldest_open_charges_first_and_is_capped_at_the_balance() -> None:
    lease = a_store_lease()
    september = RentCharge.objects.create(
        lease=lease,
        period_month=date(2026, 9, 1),
        due_on=date(2026, 9, 1),
        amount_due=D("800.00"),
        status=RentChargeStatus.UNPAID,
    )
    charge = october(lease)
    deposit(lease, "2000.00")
    end = draft(lease)
    assert lease_end.default_deductions(end, TODAY) == [
        DeductionInput(DeductionKind.UNPAID_BALANCE, "", D("2025.81"))
    ]
    over = deductions(end, (DeductionKind.UNPAID_BALANCE, "", "2025.82"))
    assert not over.ok and over.message == lease_end.OFFSET_ABOVE_OPEN.format(open="$2,025.81")
    assert not fresh(end).deductions.exists()
    assert deductions(end, (DeductionKind.UNPAID_BALANCE, "", "1000.00")).ok
    assert settle(end).ok
    assert confirm(end).ok
    first, second = fresh(september), fresh(charge)
    assert (first.amount_paid, first.status) == (D("800.00"), RentChargeStatus.PAID)
    assert (second.amount_due, second.amount_paid) == (PRORATED, D("200.00"))
    offsets = Payment.objects.filter(reference=lease_end.OFFSET_REFERENCE).order_by("amount")
    assert [(p.amount, p.method, p.status) for p in offsets] == [
        (D("200.00"), PaymentMethod.OTHER, PaymentStatus.SETTLED),
        (D("800.00"), PaymentMethod.OTHER, PaymentStatus.SETTLED),
    ]
    resolution = DepositResolution.objects.get()
    assert (resolution.withheld_amount, resolution.offset_amount, resolution.amount) == (
        D("1000.00"),
        D("1000.00"),
        D("1000.00"),
    )
    found = statements.from_json(fresh(end).statement_json)
    assert (found.offset_applied, found.still_open, found.amount_owed) == (
        D("1000.00"),
        D("1025.81"),
        D("1025.81"),
    )


def test_an_offset_is_revenue_once_in_the_pnl() -> None:
    lease = a_store_lease()
    september = RentCharge.objects.create(
        lease=lease,
        period_month=date(2026, 9, 1),
        due_on=date(2026, 9, 1),
        amount_due=D("800.00"),
        status=RentChargeStatus.UNPAID,
    )
    deposit(lease, "800.00")
    end = draft(lease, ended_on=date(2026, 9, 30))
    assert deductions(end, (DeductionKind.UNPAID_BALANCE, "", "800.00")).ok
    assert confirm(end).ok
    assert fresh(september).status == RentChargeStatus.PAID
    totals = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 9, 1), date(2026, 10, 1))))
    assert totals.rent_collected == D("800.00")
    assert totals.withheld_deposits == D(0)
    assert totals.fees_collected == D(0)


def test_a_deduction_is_typed_positive_and_guarded() -> None:
    end = draft(a_store_lease())
    cases = [
        (("bogus", "", "1.00"), lease_end.DEDUCTION_KIND),
        (("damage", "", "0.00"), lease_end.DEDUCTION_AMOUNT),
        (("damage", "", "1.001"), lease_end.DEDUCTION_AMOUNT),
        (("other", " ", "1.00"), lease_end.DEDUCTION_DESCRIPTION),
        (("damage", "x" * 201, "1.00"), lease_end.DEDUCTION_TOO_LONG),
    ]
    for line, message in cases:
        outcome = deductions(end, line)
        assert (outcome.ok, outcome.message) == (False, message), line
    assert not fresh(end).deductions.exists()


def test_the_settlement_refuses_a_bad_method_a_future_date_and_payment_data() -> None:
    end = draft(a_store_lease())
    assert settle(end, "ach").message == lease_end.METHOD_INVALID
    assert settle(end, on=None).message == lease_end.REFUND_DATE_REQUIRED
    assert settle(end, on=date(2026, 10, 21)).message == lease_end.REFUND_DATE_FUTURE
    refused = settle(end, reference=ROUTING_AND_ACCOUNT)
    assert (refused.ok, refused.field) == (False, "reference")
    assert fresh(end).refund_reference == ""
    assert settle(end, RefundMethod.WIRE).ok and fresh(end).refund_method == "wire"


@pytest.mark.parametrize(
    ("method", "payment_method"),
    [
        (RefundMethod.ZELLE, PaymentMethod.ZELLE),
        (RefundMethod.WIRE, PaymentMethod.WIRE),
        (RefundMethod.CHECK, PaymentMethod.OTHER),
    ],
)
def test_a_refund_by_each_method(method: str, payment_method: str) -> None:
    lease = a_store_lease()
    october(lease, paid="1225.81")
    held = deposit(lease, "1000.00")
    end = draft(lease)
    assert settle(end, method).ok and confirm(end).ok
    refund = Payment.objects.get(one_off_charge=held, kind=PaymentKind.REFUND_DEPOSIT)
    assert (refund.amount, refund.method, refund.reference) == (
        D("1000.00"),
        payment_method,
        "ZL-77821",
    )


def test_deposits_held_drops_by_the_resolution() -> None:
    lease = a_store_lease()
    october(lease, paid="1225.81")
    deposit(lease, "1000.00")
    query = pnl.PnlQuery(date(2026, 10, 1), date(2026, 10, 1))
    assert pnl.deposits_held(query) == D("1000.00")
    end = draft(lease)
    assert deductions(end, ("cleaning", "", "100.00")).ok and settle(end).ok
    assert confirm(end).ok
    assert pnl.deposits_held(query) == D(0)


# --- Confirm ---------------------------------------------------------------------------------


def test_confirm_needs_the_explicit_tick_and_the_terms() -> None:
    lease = a_store_lease()
    end = lease_end.start(lease, None)
    assert lease_end.confirm(end, None, confirmed=False, today=TODAY).message == (
        lease_end.CONFIRM_REQUIRED
    )
    assert lease_end.confirm(end, None, confirmed="yes", today=TODAY).ok is False  # type: ignore[arg-type]
    assert confirm(end).message == lease_end.TERMS_REQUIRED
    assert fresh(lease).ended_on is None


def test_a_confirmed_lease_end_ends_the_lease_and_keeps_the_figures(admin: Any) -> None:
    _, user = admin
    lease = a_store_lease()
    october(lease, paid="1225.81")
    deposit(lease, "1000.00")
    end = draft(lease, user, reason="other", note="Closed by the landlord.")
    assert settle(end, actor=user).ok
    outcome = lease_end.confirm(end, user, confirmed=True, today=TODAY)
    assert outcome.ok and outcome.message == lease_end.CONFIRMED
    ended = fresh(lease)
    assert (ended.ended_on, ended.collection_status, ended.end_reason) == (
        ENDED,
        CollectionStatus.ENDED,
        "Other: Closed by the landlord.",
    )
    row = fresh(end)
    assert row.status == LeaseEndStatus.CONFIRMED and row.confirmed_at is not None
    assert row.statement_json["refund_due"] == "1000.00"
    assert row.lease.store.status == "active"  # vacancy is derived
    assert actions() == [
        "lease_end.started",
        "lease_end.terms_set",
        "lease_end.settlement_set",
        "lease_end.confirmed",
    ]
    confirmed = AuditLog.objects.get(action="lease_end.confirmed")
    assert confirmed.actor == user and confirmed.after is not None
    assert confirmed.after["refund_due"] == "1000.00"
    ended_row = AuditLog.objects.get(action="lease.ended")
    assert ended_row.after is not None and ended_row.after["reason"] == "other"
    [job] = Job.objects.filter(kind=JobKind.LEASE_END_DOCUMENTS)
    assert (job.entity_type, job.entity_id, job.requested_by) == ("lease_end", row.pk, user)
    again = confirm(row)
    assert not again.ok and again.message == lease_end.NOT_DRAFT


def test_terms_are_refused_before_the_start_in_the_future_and_without_a_reason() -> None:
    lease = a_store_lease()
    end = lease_end.start(lease, None)

    def terms(on: date | None, reason: str = "moved_out", note: str = "") -> str:
        return lease_end.set_terms(
            end, on, reason, True, note=note, actor=None, today=TODAY
        ).message

    assert terms(None) == lease_end.DATE_REQUIRED
    assert terms(date(2026, 10, 21)) == lease_end.DATE_FUTURE
    assert (
        terms(date(2025, 12, 31)) == "The end date cannot be before the lease started (2026-01-01)."
    )
    assert terms(ENDED, "") == lease_end.REASON_REQUIRED
    assert terms(ENDED, "other") == lease_end.REASON_NOTE_REQUIRED
    assert terms(ENDED, "moved_out", "Paid with 4111 1111 1111 1111") == (
        "This text may not contain card or account numbers."
    )
    assert fresh(end).ended_on is None


def test_one_draft_per_lease_and_none_after_the_lease_ended() -> None:
    lease = a_store_lease()
    first = lease_end.start(lease, None)
    assert lease_end.start(lease, None) == first
    assert LeaseEnd.objects.count() == 1
    assert confirm(draft(lease)).ok
    with pytest.raises(ValueError, match="already confirmed"):
        lease_end.start(lease, None)


def test_cancel_deletes_the_draft_and_writes_nothing_else() -> None:
    lease = a_store_lease()
    october(lease)
    deposit(lease, "500.00")
    end = draft(lease)
    assert deductions(end, ("damage", "Door", "100.00")).ok
    payments = Payment.objects.count()
    outcome = lease_end.cancel(end, None)
    assert outcome.ok and not LeaseEnd.objects.exists()
    assert fresh(lease).ended_on is None and Payment.objects.count() == payments
    assert RentCharge.objects.get().amount_due == RENT
    assert actions()[-1] == "lease_end.cancelled"


def test_resolve_deposit_split_writes_one_row_and_refuses_a_split_that_is_not_whole() -> None:
    lease = a_store_lease()
    held = deposit(lease, "1000.00")
    leases.end_lease(lease.pk, ENDED, "Moved out.", actor=None, today=TODAY)

    def split(refunded: str, withheld: str, **fields: Any) -> deposits.Outcome:
        values: dict[str, Any] = {
            "method": PaymentMethod.ZELLE,
            "resolved_on": TODAY,
            "reference": "ZL-1",
            "note": "Settled.",
            "actor": None,
            "today": TODAY,
        }
        values.update(fields)
        return deposits.resolve_deposit_split(held, D(refunded), D(withheld), **values)

    assert split("600.00", "300.00").message.startswith("The refunded and withheld parts")
    assert split("600.00", "400.00", offset=D("400.01")).message == deposits.OFFSET_ABOVE_WITHHELD
    assert split("0.00", "1000.00").message == deposits.METHOD_NOT_REFUND
    assert not DepositResolution.objects.exists()
    outcome = split("600.00", "400.00", offset=D("100.00"))
    assert outcome.ok and outcome.resolution is not None
    assert (
        outcome.resolution.kind,
        outcome.resolution.amount,
        outcome.resolution.withheld_amount,
        outcome.resolution.offset_amount,
    ) == ("refunded", D("600.00"), D("400.00"), D("100.00"))
    assert split("600.00", "400.00").message == deposits.ALREADY_RESOLVED


# --- D7: the PDF -----------------------------------------------------------------------------


def a_confirmed_end(user: User, **lease: Any) -> LeaseEnd:
    store_lease = a_store_lease(**lease)
    october(store_lease, paid="2000.00")
    deposit(store_lease, "1000.00")
    end = draft(store_lease, user)
    assert deductions(end, ("cleaning", "Floors", "150.00")).ok
    assert settle(end, RefundMethod.ZELLE, reference="ZL-77821").ok
    assert lease_end.confirm(end, user, confirmed=True, today=TODAY).ok
    return LeaseEnd.objects.get(pk=end.pk)


def test_the_pdf_is_stored_under_the_store_and_its_text_passes_the_guard(
    admin: Any, storage: MemoryDocumentStorage
) -> None:
    _, user = admin
    end = a_confirmed_end(user)
    documents_job, mail_job = run_queued_jobs()  # the PDF, then the e-mail it queued
    assert (documents_job.kind, documents_job.status) == (
        JobKind.LEASE_END_DOCUMENTS,
        JobStatus.DONE,
    ), documents_job.result
    assert documents_job.result == lease_end.DOCUMENTS_DONE
    assert (mail_job.kind, mail_job.status) == (JobKind.MESSAGE_SEND, JobStatus.DONE)
    row = fresh(end)
    document = row.document
    assert document is not None
    store = row.lease.store
    assert (document.store_id, document.lease_id, document.kind) == (
        store.pk,
        None,
        DocumentKind.FINAL_STATEMENT,
    )
    assert document.content_type == "application/pdf"
    assert document.original_filename == "final-statement-20261019.pdf"
    data = storage.blobs[document.blob_path][0]
    text = pdf_text(data)
    validate_no_payment_data(text)
    for expected in (
        f"Final statement: store {store.external_label}",
        "Lease ended: October 19, 2026",
        "Prorated: 1–19 Oct, 19 of 31 days of $2,000.00 = $1,225.81",
        "Refund due to the operator",
        "$1,624.19",
        "Refund by Zelle on October 20, 2026",
        "Reference: ZL-77821",
        "Prepared on October 20, 2026",
    ):
        assert expected in text, expected
    assert actions()[-3:] == [
        "lease_end.statement_stored",
        "lease_end.statement_queued",
        "lease_end.statement_suppressed",
    ]


def test_the_same_figures_render_the_same_bytes_and_a_retry_adopts_the_stored_document(
    admin: Any,
) -> None:
    _, user = admin
    end = a_confirmed_end(user)
    final = statements.from_json(end.statement_json)
    assert statements.render_pdf(final) == statements.render_pdf(final)
    assert lease_end.produce_documents(end.pk, user)[0]
    stored = fresh(end).document
    LeaseEnd.objects.filter(pk=end.pk).update(document=None)
    assert lease_end.produce_documents(end.pk, user) == (True, lease_end.DOCUMENTS_DONE)
    assert fresh(end).document == stored
    assert Document.objects.count() == 1
    assert OutboundMessage.objects.count() == 1


def test_without_a_user_the_job_stores_nothing(admin: Any) -> None:
    _, user = admin
    end = a_confirmed_end(user)
    ok, text = lease_end.produce_documents(end.pk, None)
    assert not ok and lease_end.NO_UPLOADER in text
    assert not Document.objects.exists()


def test_a_draft_or_an_unknown_end_makes_no_document() -> None:
    end = draft(a_store_lease())
    assert lease_end.produce_documents(end.pk, None) == (False, lease_end.NOT_CONFIRMED)
    assert lease_end.produce_documents(uuid.uuid4(), None) == (False, lease_end.GONE)


def test_the_pdf_is_downloaded_from_the_lease_end_page(admin: Any, pinned: None) -> None:
    client, user = admin
    end = a_confirmed_end(user)
    path = f"/lease-ends/{end.pk}/statement.pdf"
    assert client.get(path)["Location"] == f"/lease-ends/{end.pk}/confirm/"
    run_queued_jobs()
    response = client.get(path)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert b"".join(response.streaming_content).startswith(b"%PDF")
    assert AuditLog.objects.filter(action="document.downloaded").exists()
    page = client.get(f"/lease-ends/{end.pk}/confirm/").content.decode()
    assert path in page and "Lease end" in page


# --- D8: the e-mail ---------------------------------------------------------------------------


def test_with_mail_off_the_statement_e_mail_is_suppressed(admin: Any) -> None:
    _, user = admin
    end = a_confirmed_end(user)
    run_queued_jobs()  # the documents job, then the e-mail it queued
    message = OutboundMessage.objects.get()
    assert (message.kind, message.step, message.document) == (
        MessageKind.FINAL_STATEMENT,
        None,
        fresh(end).document,
    )
    assert (message.charge_entity_type, message.charge_entity_id) == ("lease_end", end.pk)
    assert message.status == MessageStatus.SUPPRESSED
    assert "lease_end.statement_suppressed" in actions("lease_end.statement")


def test_outside_production_the_statement_goes_to_the_catch_all_with_the_pdf(
    admin: Any, memory: MemoryMailSender
) -> None:
    _, user = admin
    end = a_confirmed_end(user)
    run_queued_jobs()
    [sent] = memory.sent
    operator = fresh(end).lease.operator
    assert sent.to == CATCH_ALL and sent.subject.startswith(f"[to: {operator.email}] ")
    assert "Final statement for" in sent.subject
    assert "Refund due to you: $1,624.19" in sent.text
    [attachment] = sent.attachments
    assert (attachment.filename, attachment.content_type) == (
        "final-statement-20261019.pdf",
        "application/pdf",
    )
    assert attachment.data.startswith(b"%PDF")
    raw = mime_bytes(sent, "billing@example.com")
    assert b"multipart/mixed" in raw and b'filename="final-statement-20261019.pdf"' in raw
    message = OutboundMessage.objects.get()
    assert message.status == MessageStatus.CATCH_ALL and message.body_sha256
    assert AuditLog.objects.filter(action="lease_end.statement_sent").exists()


def test_an_operator_without_an_address_is_not_mailed(admin: Any, memory: MemoryMailSender) -> None:
    _, user = admin
    end = a_confirmed_end(user)
    operator = fresh(end).lease.operator
    type(operator).objects.filter(pk=operator.pk).update(email=None)
    run_queued_jobs()
    assert memory.sent == []
    message = OutboundMessage.objects.get()
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "NoAddress")


def test_the_operator_page_log_names_the_final_statement(admin: Any) -> None:
    client, user = admin
    end = a_confirmed_end(user)
    run_queued_jobs()
    page = client.get(f"/operators/{fresh(end).lease.operator_id}/").content.decode()
    assert "Final statement" in page and f"/lease-ends/{end.pk}/confirm/" in page


# --- Pages and roles -------------------------------------------------------------------------


def wizard_paths(end: LeaseEnd) -> list[str]:
    return [
        f"/stores/{end.lease.store_id}/end-lease/",
        *(
            f"/lease-ends/{end.pk}/{name}/"
            for name in ("terms", "blockers", "statement", "settlement", "confirm", "cancel")
        ),
        f"/lease-ends/{end.pk}/statement.pdf",
    ]


def test_the_routes_are_admin_only() -> None:
    for name in ("terms", "statement", "settlement", "confirm", "cancel"):
        rule = registry.rule_for(f"ledger:lease_ends:{name}")
        assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    rule = registry.rule_for("ledger:stores:end_lease")
    assert rule is not None and rule.min_role is Role.ADMIN
    assert {"ledger:lease_ends:blockers", "ledger:lease_ends:pdf"} <= (
        registry.READ_ONLY_ROUTE_NAMES
    )


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_viewer_and_billing_get_403_on_every_step(role: Role) -> None:
    end = draft(a_store_lease())
    client, _ = client_for(role)
    for path in wizard_paths(end):
        assert client.get(path).status_code in (403, 405), path
        assert client.post(path, {"confirmed": "on"}).status_code == 403, path
    assert fresh(end).status == LeaseEndStatus.DRAFT and LeaseEnd.objects.count() == 1
    store_page = client.get(f"/stores/{end.lease.store_id}/").content.decode()
    assert "end-lease/" not in store_page


def test_unknown_or_malformed_ids_are_404(admin: Any) -> None:
    client, _ = admin
    for key in ("nope", str(uuid.uuid4())):
        assert client.get(f"/lease-ends/{key}/terms/").status_code == 404
        assert client.get(f"/stores/{key}/end-lease/").status_code == 404


def test_an_admin_walks_the_wizard_from_the_store_page(admin: Any, pinned: None) -> None:
    client, user = admin
    lease = a_store_lease()
    october(lease, paid="2000.00")
    deposit(lease, "1000.00")
    store_page = f"/stores/{lease.store_id}/"
    page = client.get(store_page).content.decode()
    assert f'href="/stores/{lease.store_id}/end-lease/"' in page and "Vacant since" not in page
    start = client.get(f"/stores/{lease.store_id}/end-lease/").content.decode()
    assert "Start" in start and "The wizard takes you" in start
    response = client.post(f"/stores/{lease.store_id}/end-lease/")
    end = LeaseEnd.objects.get(lease=lease)
    base = f"/lease-ends/{end.pk}"
    assert response["Location"] == f"{base}/terms/"
    for step in ("statement", "settlement", "confirm"):
        assert client.get(f"{base}/{step}/")["Location"] == f"{base}/terms/"
    terms_page = client.get(f"{base}/terms/").content.decode()
    assert 'id="step-help"' in terms_page and "Prorate last month" in terms_page
    response = client.post(
        f"{base}/terms/",
        {"ended_on": ENDED.isoformat(), "reason": "moved_out", "prorate_last_month": "on"},
    )
    assert response["Location"] == f"{base}/blockers/"
    assert 'id="no-blockers"' in client.get(f"{base}/blockers/").content.decode()
    statement_page = client.get(f"{base}/statement/").content.decode()
    assert TEXT in statement_page and "774.19" in statement_page
    # PLAN-012d D10: numbered deduction rows and their live total line.
    assert 'class="line-number" data-label="#">1</td>' in statement_page
    total_line = 'Deductions: <span class="js-total-out" data-total-for="deduction-rows">$'
    assert total_line in statement_page
    assert "ledger/table_total.js" in statement_page
    response = client.post(
        f"{base}/statement/",
        {"kind_1": "cleaning", "description_1": "Floors", "amount_1": "150.00"},
    )
    assert response["Location"] == f"{base}/settlement/"
    response = client.post(
        f"{base}/settlement/",
        {"method": "zelle", "refund_on": TODAY.isoformat(), "reference": "ZL-9", "note": ""},
    )
    assert response["Location"] == f"{base}/confirm/"
    confirm_page = client.get(f"{base}/confirm/").content.decode()
    assert "Are you sure?" in confirm_page and "$1624.19 is recorded as refunded" in confirm_page
    assert client.post(f"{base}/confirm/", {})["Location"] == f"{base}/confirm/"
    assert "Tick the box to confirm." in client.get(f"{base}/confirm/").content.decode()
    assert fresh(lease).ended_on is None
    client.post(f"{base}/confirm/", {"confirmed": "on"})
    assert fresh(lease).ended_on == ENDED
    done = client.get(f"{base}/confirm/").content.decode()
    assert "The lease ended on 2026-10-19" in done and "being prepared" in done
    store_after = client.get(store_page).content.decode()
    assert 'id="vacant-since">Vacant since 2026-10-20' in store_after
    assert 'id="store-end-lease"' not in store_after
    assert f"{base}/confirm/" in store_after
    assert AuditLog.objects.get(action="lease_end.confirmed").actor == user


def test_the_blockers_page_lists_each_item_and_offers_no_way_on(admin: Any, pinned: None) -> None:
    client, _ = admin
    lease = a_store_lease()
    charge = an_email_charge(lease)
    end = draft(lease)
    page = client.get(f"/lease-ends/{end.pk}/blockers/").content.decode()
    assert 'id="blocked"' in page and f"/one-off-charges/{charge.pk}/" in page
    assert f"/lease-ends/{end.pk}/statement/" not in page.split('id="blocker-list"')[1]
    response = client.get(f"/lease-ends/{end.pk}/statement/")
    assert response["Location"] == f"/lease-ends/{end.pk}/blockers/"


def test_a_refused_step_is_held_on_its_form_without_the_card_number(
    admin: Any, pinned: None
) -> None:
    client, _ = admin
    end = draft(a_store_lease())
    card = "4111 1111 1111 1111"
    client.post(
        f"/lease-ends/{end.pk}/settlement/",
        {"method": "zelle", "refund_on": TODAY.isoformat(), "reference": card, "note": ""},
    )
    page = client.get(f"/lease-ends/{end.pk}/settlement/").content.decode()
    assert "may not contain card or account numbers" in page and card not in page
    assert fresh(end).refund_reference == ""


def test_cancel_from_the_page_returns_to_the_store(admin: Any, pinned: None) -> None:
    client, _ = admin
    end = draft(a_store_lease())
    response = client.post(f"/lease-ends/{end.pk}/cancel/")
    assert response["Location"] == f"/stores/{end.lease.store_id}/"
    assert not LeaseEnd.objects.exists()


def test_a_store_without_a_lease_in_effect_offers_no_end_lease(admin: Any, pinned: None) -> None:
    client, _ = admin
    lease = a_store_lease(started_on=date(2026, 12, 1))
    page = client.get(f"/stores/{lease.store_id}/").content.decode()
    assert 'id="store-end-lease"' not in page
    response = client.post(f"/stores/{lease.store_id}/end-lease/")
    assert response["Location"] == f"/stores/{lease.store_id}/"
    assert not LeaseEnd.objects.exists()


def test_vacant_since_follows_the_last_end_and_a_new_lease() -> None:
    lease = a_store_lease()
    store = lease.store
    assert lease_end.vacant_since(store, TODAY) is None
    assert confirm(draft(lease)).ok
    assert lease_end.vacant_since(store, TODAY) == date(2026, 10, 20)
    leases.create_lease(
        {
            "store": store,
            "operator": lease.operator,
            "debit_day": 1,
            "payment_channel": "wire",
            "started_on": date(2026, 10, 20),
            "collection_status": "active",
        },
        rent=RENT,
        creation_key=uuid.uuid4(),
        actor=None,
    )
    assert lease_end.vacant_since(store, TODAY) is None


# --- D5 amendment: Stop autopay --------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def a_provider_schedule(
    fake: FakeProvider,
    lease: Lease,
    kind: str = ScheduleKind.RENT,
    state: str = ScheduleState.ACTIVE,
    *,
    at_provider: bool = True,
) -> tuple[CollectionSchedule, str]:
    """A schedule row linked to a provider schedule ID, which the fake provider holds
    unless ``at_provider`` is ``False`` (deleted there already)."""
    row = CollectionSchedule.objects.create(
        lease=lease, kind=kind, seq=1, debit_day=1, amount=RENT, state=state
    )
    ref = f"fake-schedule-{uuid.uuid4().hex[:8]}"
    ExternalRef.objects.create(
        entity_type=leases.ENTITY_SCHEDULE,
        entity_id=row.pk,
        provider=leases.PROVIDER_KEY,
        external_kind=leases.KIND_SCHEDULE,
        external_id=ref,
    )
    if at_provider:
        fake.schedules[ref] = ScheduleRecord(
            external_id=ref,
            raw_status="",
            fetched_at=timezone.now(),
            customer_external_id="fake-customer",
            total=RENT,
            rule="DTSTART=20261101T120000Z;FREQ=MONTHLY",
            active=state == ScheduleState.ACTIVE,
        )
    return row, ref


def deletes(fake: FakeProvider) -> list[str]:
    return [str(args[0]) for name, args in fake.calls if name == "delete_scheduled_invoice"]


def stop_path(lease: Lease) -> str:
    return f"/leases/{lease.pk}/stop-autopay/"


@pytest.mark.parametrize("state", [ScheduleState.ACTIVE, ScheduleState.PAUSED])
@pytest.mark.parametrize("kind", [ScheduleKind.RENT, ScheduleKind.INSURANCE])
def test_stop_autopay_deletes_the_schedule_at_the_provider_through_the_job(
    fake: FakeProvider, admin: Any, kind: str, state: str
) -> None:
    client, user = admin
    lease = a_store_lease()
    row, ref = a_provider_schedule(fake, lease, kind, state)
    page = client.get(f"/leases/{lease.pk}/").content.decode()
    assert 'id="stop-autopay"' in page and stop_path(lease) in page
    confirm_page = client.get(stop_path(lease)).content.decode()
    assert "Stop autopay: the provider schedule is deleted; re-enrol to start again" in confirm_page
    assert deletes(fake) == []

    response = client.post(stop_path(lease))
    assert response["Location"] == f"/leases/{lease.pk}/"
    [job] = Job.objects.filter(kind=JobKind.AUTOPAY_STOP)
    assert (job.entity_type, job.entity_id, job.requested_by) == ("lease", lease.pk, user)
    assert deletes(fake) == []  # the view never calls the provider
    run_queued_jobs()
    assert fresh(job).status == JobStatus.DONE
    assert deletes(fake) == [ref]
    assert fresh(row).state == ScheduleState.ENDED
    [entry] = AuditLog.objects.filter(action="lease.autopay_stopped")
    assert entry.entity_id == str(lease.pk) and entry.actor == user
    assert entry.before is not None and entry.after is not None
    assert (entry.before["kind"], entry.before["state"], entry.after["state"]) == (
        kind,
        state,
        ScheduleState.ENDED,
    )
    assert ref not in str(entry.after)  # the provider ID is masked
    refreshed = fresh(lease)
    assert (refreshed.collection_status, refreshed.ended_on) == (CollectionStatus.ACTIVE, None)
    assert 'id="stop-autopay"' not in client.get(f"/leases/{lease.pk}/").content.decode()


def test_stop_autopay_deletes_every_live_schedule_once_and_leaves_the_others(
    fake: FakeProvider,
) -> None:
    lease = a_store_lease()
    rent, rent_ref = a_provider_schedule(fake, lease, ScheduleKind.RENT, ScheduleState.ACTIVE)
    cover, cover_ref = a_provider_schedule(
        fake, lease, ScheduleKind.INSURANCE, ScheduleState.PAUSED
    )
    failed = CollectionSchedule.objects.create(
        lease=lease,
        kind=ScheduleKind.RENT,
        seq=2,
        debit_day=15,
        amount=RENT,
        state=ScheduleState.FAILED,
    )
    outcome = leases.stop_autopay(lease.pk, actor=None)
    assert (outcome.action, outcome.ok) == (AutopayAction.STOPPED, True)
    assert sorted(deletes(fake)) == sorted([rent_ref, cover_ref])
    assert (fresh(rent).state, fresh(cover).state) == (ScheduleState.ENDED, ScheduleState.ENDED)
    assert fresh(failed).state == ScheduleState.FAILED
    assert AuditLog.objects.filter(action="lease.autopay_stopped").count() == 2

    again = leases.stop_autopay(lease.pk, actor=None)
    assert (again.action, again.message) == (AutopayAction.ALREADY, leases.NOTHING_TO_STOP)
    assert len(deletes(fake)) == 2


def test_a_404_at_the_provider_counts_as_already_gone(fake: FakeProvider) -> None:
    lease = a_store_lease()
    row, ref = a_provider_schedule(fake, lease, at_provider=False)
    outcome = leases.stop_autopay(lease.pk, actor=None)
    assert outcome.action is AutopayAction.STOPPED
    assert deletes(fake) == [ref]
    assert fresh(row).state == ScheduleState.ENDED
    assert AuditLog.objects.filter(action="lease.autopay_stopped").count() == 1


def test_any_other_provider_failure_keeps_the_schedule_and_fails_the_job(
    fake: FakeProvider,
) -> None:
    lease = a_store_lease()
    row, _ = a_provider_schedule(fake, lease)
    fake.fail_next("delete_scheduled_invoice", ProviderUnavailable(None))
    outcome = leases.stop_autopay(lease.pk, actor=None)
    assert (outcome.action, outcome.message) == (AutopayAction.FAILED, leases.PROVIDER_FAILED)
    assert fresh(row).state == ScheduleState.ACTIVE
    assert not AuditLog.objects.filter(action="lease.autopay_stopped").exists()
    assert AuditLog.objects.filter(action="lease.autopay_stop_failed").count() == 1
    assert [item.kind for item in lease_end.blockers(lease)] == [lease_end.AUTOPAY]


def test_the_wizard_blocker_links_to_stop_autopay_and_goes_after_the_job(
    fake: FakeProvider, admin: Any
) -> None:
    client, _ = admin
    lease = a_store_lease()
    a_provider_schedule(fake, lease, ScheduleKind.RENT, ScheduleState.PAUSED)
    end = draft(lease)
    [found] = lease_end.blockers(lease)
    assert found.url == stop_path(lease)
    assert stop_path(lease) in client.get(f"/lease-ends/{end.pk}/blockers/").content.decode()
    client.post(stop_path(lease))
    run_queued_jobs()
    assert lease_end.blockers(lease) == []
    page = client.get(f"/lease-ends/{end.pk}/blockers/").content.decode()
    assert 'id="no-blockers"' in page


def test_stop_autopay_with_nothing_live_queues_nothing(fake: FakeProvider, admin: Any) -> None:
    client, _ = admin
    lease = a_store_lease()
    page = client.get(stop_path(lease)).content.decode()
    assert 'id="nothing-to-stop"' in page and 'id="confirm-stop-autopay"' not in page
    client.post(stop_path(lease))
    assert not Job.objects.filter(kind=JobKind.AUTOPAY_STOP).exists()
    assert fake.calls == []


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_viewer_and_billing_get_403_on_stop_autopay(fake: FakeProvider, role: Role) -> None:
    lease = a_store_lease()
    row, _ = a_provider_schedule(fake, lease)
    client, _ = client_for(role)
    assert client.get(stop_path(lease)).status_code == 403
    assert client.post(stop_path(lease)).status_code == 403
    assert 'id="stop-autopay"' not in client.get(f"/leases/{lease.pk}/").content.decode()
    assert not Job.objects.filter(kind=JobKind.AUTOPAY_STOP).exists()
    assert fresh(row).state == ScheduleState.ACTIVE and fake.calls == []


def test_the_stop_route_is_admin_only() -> None:
    rule = registry.rule_for("ledger:leases:stop_autopay")
    assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}


# --- D3 amendment: rent charges after the end month ------------------------------------------

BACK_DATED = date(2026, 8, 19)
SEPTEMBER = date(2026, 9, 1)


def a_month(lease: Lease, month: date, paid: str = "0.00") -> RentCharge:
    charge = RentCharge.objects.create(
        lease=lease,
        period_month=month,
        seq=1,
        due_on=month,
        amount_due=RENT,
        status=RentChargeStatus.DUE,
    )
    if D(paid):
        outcome = rent_run.record_manual_payment(
            charge.pk,
            method=PaymentMethod.WIRE,
            amount=D(paid),
            paid_on=month,
            note="Wire received.",
            actor=None,
            today=TODAY,
        )
        assert outcome.ok, outcome.message
    return RentCharge.objects.get(pk=charge.pk)


@pytest.mark.parametrize("paid", ["2000.00", "500.00"])
def test_a_back_dated_end_waives_the_later_months_and_refunds_what_was_paid(
    paid: str, admin: Any, pinned: None
) -> None:
    client, user = admin
    lease = a_store_lease()
    september = a_month(lease, SEPTEMBER)
    october_charge = a_month(lease, OCTOBER, paid=paid)
    [payment] = Payment.objects.filter(rent_charge=october_charge, is_refund=False)
    end = draft(lease, ended_on=BACK_DATED)

    found = figures(end)
    assert [(item.charge_id, item.credit) for item in found.after_end] == [
        (september.pk, D(0)),
        (october_charge.pk, D(paid)),
    ]
    assert found.after_end[0].text == "Waived: September 2026 rent, after the end date"
    assert found.after_end[1].text == "Credit: October 2026 rent paid, after the end date"
    assert {line.charge_id for line in found.open_lines}.isdisjoint(
        {september.pk, october_charge.pk}
    )
    # Only the prorated August charge (19 of 31 days) is open.
    assert found.open_total == PRORATED
    assert (found.credit, found.refund_due) == (D(paid), D(paid))
    statement_page = client.get(f"/lease-ends/{end.pk}/statement/").content.decode()
    assert "Waived: September 2026 rent, after the end date" in statement_page

    refused = confirm(end, user)
    assert not refused.ok and refused.message == lease_end.SETTLEMENT_REQUIRED
    assert fresh(september).status == RentChargeStatus.DUE
    assert settle(end, RefundMethod.WIRE).ok
    assert confirm(end, user).ok

    waived = fresh(september)
    assert (waived.status, waived.review_message, waived.amount_paid) == (
        RentChargeStatus.WAIVED,
        lease_end.AFTER_END_NOTE,
        D(0),
    )
    refunded = fresh(october_charge)
    assert (refunded.status, refunded.review_message, refunded.amount_paid) == (
        RentChargeStatus.WAIVED,
        "After lease end (wizard)",
        D(0),
    )
    [refund] = Payment.objects.filter(rent_charge=october_charge, is_refund=True)
    assert (refund.amount, refund.parent_payment_id, refund.method, refund.status) == (
        D(paid),
        payment.pk,
        PaymentMethod.WIRE,
        PaymentStatus.SETTLED,
    )
    assert Payment.objects.filter(rent_charge=september).count() == 0
    entries = AuditLog.objects.filter(action="rent_charge.waived_after_end").order_by("id")
    assert [entry.entity_id for entry in entries] == [str(september.pk), str(october_charge.pk)]
    assert all(entry.actor == user for entry in entries)
    last = entries.last()
    assert last is not None and last.after is not None
    assert last.after["refund_payments"] == [str(refund.pk)]

    row = fresh(end)
    assert row.refund_payment is not None
    kept = statements.from_json(row.statement_json)
    assert [item.text for item in kept.after_end] == [
        "Waived: September 2026 rent, after the end date",
        "Credit: October 2026 rent paid, after the end date",
    ]
    text = pdf_text(statements.render_pdf(kept))
    assert "Waived: September 2026 rent, after the end date" in text
    assert "Credit: October 2026 rent paid, after the end date" in text
    assert lease_end.blockers(lease) == []


def test_a_waived_later_month_is_left_alone() -> None:
    lease = a_store_lease()
    charge = a_month(lease, SEPTEMBER)
    RentCharge.objects.filter(pk=charge.pk).update(status=RentChargeStatus.WAIVED)
    end = draft(lease, ended_on=BACK_DATED)
    assert figures(end).after_end == ()
    assert confirm(end).ok
    assert not AuditLog.objects.filter(action="rent_charge.waived_after_end").exists()


def test_a_later_month_changed_meanwhile_refuses_the_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = a_store_lease()
    charge = a_month(lease, SEPTEMBER)
    end = draft(lease, ended_on=BACK_DATED)
    real = statements.final

    def stale(row: LeaseEnd, *, today: date | None = None) -> statements.Final:
        found = real(row, today=today)
        RentCharge.objects.filter(pk=charge.pk).update(amount_due=D("1999.00"))
        return found

    monkeypatch.setattr(statements, "final", stale)
    outcome = confirm(end)
    assert not outcome.ok and outcome.message == lease_end.CHANGED_MEANWHILE
    assert fresh(lease).ended_on is None
    assert fresh(charge).status == RentChargeStatus.DUE


def test_the_deductions_total_sums_the_amounts_typed() -> None:
    from ledger.forms import LeaseEndDeductionsForm

    form = LeaseEndDeductionsForm(
        {"amount_1": "150.00", "amount_2": "1,000", "amount_3": "-5", "amount_4": "49.50"}
    )
    assert form.total() == Decimal("199.50")  # the invalid amounts count as nothing
    assert LeaseEndDeductionsForm().total() == Decimal("0.00")
