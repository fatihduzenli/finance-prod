"""Reschedule a returned debit, with the NSF fee (docs/qa/PLAN-011b D5; §7 items of Batch C).

The service (:mod:`ledger.services.nsf`), the worker's two-line schedule and invoice
(:mod:`payments.one_off_invoices`), the allocation of a payment on the re-debit's invoice
(the transaction handler, through real webhook deliveries to a
:class:`~payments.fake.FakeProvider`), the returns page and its role gates, the attention
list, the calendar, the open balances, the statement and the P&L. Nothing reaches a
network.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.test import override_settings

from accounts.roles import Role
from ledger.models import (
    NSF_FEE_FIRST,
    NSF_FEE_LATER,
    AuditLog,
    CollectionMethod,
    FeeType,
    InsuranceCharge,
    Invoice,
    InvoiceStatus,
    Lease,
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
    ResolutionKind,
)
from ledger.services import (
    attention,
    calendar,
    grid,
    nsf,
    pnl,
    receivables,
    rent_run,
    reports,
    returns,
)
from ledger.services import one_off_charges as one_off_service
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import ScheduleInput
from tests.job_helpers import run_queued_jobs
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import (
    ach_return,
    deliver,
    make_world,
    payment_for,
    provider_txn,
    settle,
)
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
REDEBIT_ON = date(2026, 10, 20)
CUSTOMER = "cus-one-off-1"
CARD = "4111 1111 1111 1111"


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


def a_return(
    fake: FakeProvider,
    amount: str = "1234.56",
    *,
    bank: bool = True,
    operator: Operator | None = None,
    month: date = date(2026, 9, 1),
) -> Payment:
    """A returned ACH rent payment, its charge reopened (as the webhook leaves it)."""
    owner = operator or an_operator(fake, bank="pm-bank-one-off-1" if bank else None)
    lease = Lease.objects.filter(operator=owner).first() or make_lease(operator=owner)
    charge = RentCharge.objects.create(
        lease=lease,
        period_month=month,
        seq=1,
        due_on=month.replace(day=15),
        amount_due=D(amount),
        status=RentChargeStatus.UNPAID,
        needs_review=True,
    )
    return Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D(amount),
        status=PaymentStatus.RETURNED,
        initiated_on=month.replace(day=15),
        returned_at=NOW - timedelta(days=2),
        return_code="R01",
        return_reason="Insufficient Funds",
    )


def a_user(role: Role) -> object:
    return client_for(role)[1]


def reschedule(
    payment: Payment,
    *,
    on: date = REDEBIT_ON,
    waive: bool = False,
    note: str = "",
    role: Role = Role.BILLING,
    confirmed: bool = False,
) -> nsf.Outcome:
    return nsf.reschedule(
        payment.pk,
        redebit_date=on,
        waive_fee=waive,
        note=note,
        actor=a_user(role),  # type: ignore[arg-type]
        confirmed=confirmed,
    )


def created_calls(fake: FakeProvider, name: str) -> list[object]:
    return [args[0] for method, args in fake.calls if method == name]


def charges_of(payment: Payment) -> tuple[OneOffCharge, OneOffCharge]:
    # PLAN-014 F2: a fee made at the return names the payment too.
    redebit = OneOffCharge.objects.get(source_payment=payment, category=OneOffCategory.REDEBIT)
    fee = OneOffCharge.objects.get(companion_of=redebit)
    return redebit, fee


def nothing_written() -> bool:
    return not OneOffCharge.objects.exists() and not Job.objects.exists()


# --- The fee types -----------------------------------------------------------------------


def test_the_nsf_fee_types_are_seeded_and_never_on_the_checklist() -> None:
    # Re-pinned by PLAN-014 F2: ledger migration 0063 makes ``nsf_fee_first`` active (the
    # fee made at the return is charged at its price); ``nsf_fee_later`` stays inactive.
    first = FeeType.objects.get(code=NSF_FEE_FIRST)
    later = FeeType.objects.get(code=NSF_FEE_LATER)
    assert (first.default_price, later.default_price) == (D("100.00"), D("50.00"))
    assert first.active and not later.active
    assert not first.refundable and not later.refundable
    FeeType.objects.filter(code__in=[NSF_FEE_FIRST, NSF_FEE_LATER]).update(active=True)
    codes = {fee_type.code for fee_type in one_off_service.checklist_types()}
    assert NSF_FEE_FIRST not in codes and NSF_FEE_LATER not in codes


# --- Reschedule: the happy path ----------------------------------------------------------


def test_reschedule_makes_the_two_charges_and_one_two_line_schedule(
    fake: FakeProvider, worker: None
) -> None:
    payment = a_return(fake)
    outcome = reschedule(payment, note="Owner agreed by phone.")
    assert outcome.ok, outcome.message
    redebit, fee = charges_of(payment)
    assert (redebit.category, redebit.price, redebit.due_date) == (
        OneOffCategory.REDEBIT,
        D("1234.56"),
        REDEBIT_ON,
    )
    assert redebit.description == "Re-debit of Rent 2026-09"
    assert redebit.collection == CollectionMethod.AUTOPAY_DUE
    assert redebit.note == "Owner agreed by phone."
    assert redebit.lease_id == payment.rent_charge.lease_id  # type: ignore[union-attr]
    assert (fee.category, fee.price, fee.status) == (
        OneOffCategory.NSF_FEE,
        D("100.00"),
        OneOffChargeStatus.OWED,
    )
    assert fee.companion_of_id == redebit.pk and fee.source_payment_id is None
    payment.refresh_from_db()
    assert payment.resolution_kind == ResolutionKind.RESCHEDULED
    assert payment.redebit_date == REDEBIT_ON and payment.resolved_at == NOW
    assert payment.resolution_note == "Owner agreed by phone."
    operator = Operator.objects.get(pk=redebit.operator_id_billed)
    assert operator.nsf_return_count == 1
    # One job, for the re-debit charge; no local invoice before the schedule fires.
    [job] = Job.objects.all()
    assert (job.kind, job.entity_id) == (JobKind.SCHEDULE_CREATE, redebit.pk)
    assert not Invoice.objects.exists()
    assert outcome.message.startswith("Rescheduled: re-debit of $1,334.56 on Oct 20, 2026")
    [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE, done.result
    [schedule] = created_calls(fake, "create_scheduled_invoice")
    assert isinstance(schedule, ScheduleInput)
    lines = schedule.amounts.line_items
    assert [(line.description, line.unit_price) for line in lines] == [
        ("Re-debit of Rent 2026-09", D("1234.56")),
        ("NSF fee, first return: returned debit of Rent 2026-09", D("100.00")),
    ]
    # The total is the sum of the two lines, to the cent.
    assert schedule.amounts.total == schedule.amounts.subtotal == D("1334.56")
    assert schedule.amounts.total == sum((line.total for line in lines), D("0"))
    assert schedule.reference == f"OOC-{str(redebit.pk)[:8]}"
    assert schedule.rule.startswith("DTSTART=20261020T")
    actions = list(AuditLog.objects.order_by("id").values_list("action", flat=True))
    assert actions.count(nsf.ACTION) == 1
    assert actions.count(one_off_service.CREATED_ACTION) == 2
    rescheduled = AuditLog.objects.get(action=nsf.ACTION)
    assert rescheduled.entity_id == str(payment.pk)
    assert rescheduled.before["nsf_return_count"] == 0
    assert rescheduled.after["nsf_return_count"] == 1
    assert rescheduled.after["fee"] == "100.00"


def test_without_a_linked_bank_method_one_two_line_invoice_is_emailed(
    fake: FakeProvider, worker: None
) -> None:
    payment = a_return(fake, "800.00", bank=False)
    outcome = reschedule(payment)
    assert outcome.ok, outcome.message
    redebit, fee = charges_of(payment)
    assert redebit.collection == fee.collection == CollectionMethod.EMAIL
    [invoice] = Invoice.objects.all()
    assert invoice.one_off_charge_id == redebit.pk
    assert (invoice.total, invoice.subtotal, invoice.due_date) == (
        D("900.00"),
        D("900.00"),
        REDEBIT_ON,
    )
    assert invoice.internal_status == InvoiceStatus.DRAFT
    [job] = Job.objects.all()
    assert (job.kind, job.entity_id) == (JobKind.INVOICE_CREATE, redebit.pk)
    assert invoice.invoice_number in outcome.message
    # A note left empty: the payment carries a fixed sentence (the CHECK needs a note).
    payment.refresh_from_db()
    assert payment.resolution_note == "Rescheduled: re-debit on 2026-10-20."
    run_queued_jobs()
    [sent] = created_calls(fake, "create_invoice")
    assert [line.unit_price for line in sent.amounts.line_items] == [D("800.00"), D("100.00")]  # type: ignore[attr-defined]
    assert sent.amounts.total == D("900.00")  # type: ignore[attr-defined]
    assert sent.reference == invoice.invoice_number  # type: ignore[attr-defined]
    assert created_calls(fake, "send_invoice_email")


def test_the_first_return_costs_100_and_every_later_one_50(fake: FakeProvider) -> None:
    first = a_return(fake, "500.00")
    operator = Operator.objects.get(pk=returns.row(first).operator.pk)  # type: ignore[union-attr]
    second = a_return(fake, "500.00", operator=operator, month=date(2026, 8, 1))
    third = a_return(fake, "500.00", operator=operator, month=date(2026, 7, 1))
    assert reschedule(first).ok
    assert reschedule(second).ok
    assert reschedule(third, on=date(2026, 10, 27)).ok
    fees = [charges_of(payment)[1].price for payment in (first, second, third)]
    assert fees == [D("100.00"), D("50.00"), D("50.00")]
    operator.refresh_from_db()
    assert operator.nsf_return_count == 3


def test_the_fee_amounts_are_read_from_the_fee_types(fake: FakeProvider) -> None:
    FeeType.objects.filter(code=NSF_FEE_FIRST).update(default_price=D("75.00"))
    payment = a_return(fake, "500.00")
    assert reschedule(payment).ok
    assert charges_of(payment)[1].price == D("75.00")


def test_a_missing_fee_amount_refuses(fake: FakeProvider) -> None:
    FeeType.objects.filter(code=NSF_FEE_FIRST).update(default_price=None)
    payment = a_return(fake)
    outcome = reschedule(payment)
    assert (outcome.ok, outcome.message) == (False, nsf.FEE_TYPE_MISSING)
    assert nothing_written()


# --- The fee waiver ----------------------------------------------------------------------


def test_an_admin_waives_the_fee_with_a_note_and_the_count_still_rises(
    fake: FakeProvider, worker: None
) -> None:
    payment = a_return(fake, "1000.00")
    outcome = reschedule(
        payment, waive=True, note="Bank error, not the operator's.", role=Role.ADMIN
    )
    assert outcome.ok, outcome.message
    redebit, fee = charges_of(payment)
    assert fee.status == OneOffChargeStatus.WAIVED
    assert fee.waive_note == "Bank error, not the operator's." and fee.waived_at == NOW
    assert Operator.objects.get(pk=redebit.operator_id_billed).nsf_return_count == 1
    assert "NSF fee waived" in outcome.message
    run_queued_jobs()
    [schedule] = created_calls(fake, "create_scheduled_invoice")
    assert [line.unit_price for line in schedule.amounts.line_items] == [D("1000.00")]  # type: ignore[attr-defined]
    assert schedule.amounts.total == D("1000.00")  # type: ignore[attr-defined]
    # The next return is a later one: 50.00.
    second = a_return(
        fake,
        "1000.00",
        operator=Operator.objects.get(pk=redebit.operator_id_billed),
        month=date(2026, 8, 1),
    )
    assert reschedule(second).ok
    assert charges_of(second)[1].price == D("50.00")


def test_a_waiver_needs_a_note(fake: FakeProvider) -> None:
    payment = a_return(fake)
    for note in ("", "   ", "​"):
        outcome = reschedule(payment, waive=True, note=note, role=Role.ADMIN)
        assert (outcome.ok, outcome.message) == (False, nsf.WAIVE_NOTE_REQUIRED)
    assert nothing_written()


def test_billing_cannot_waive_the_fee(fake: FakeProvider) -> None:
    payment = a_return(fake)
    outcome = reschedule(payment, waive=True, note="Please.", role=Role.BILLING)
    assert (outcome.ok, outcome.forbidden) == (False, True)
    assert nothing_written()
    payment.refresh_from_db()
    assert payment.resolved_at is None


# --- Refusals ----------------------------------------------------------------------------


def test_a_date_in_the_past_is_refused_and_today_is_accepted(fake: FakeProvider) -> None:
    payment = a_return(fake)
    outcome = reschedule(payment, on=TODAY - timedelta(days=1))
    assert (outcome.ok, outcome.message) == (False, nsf.DATE_IN_PAST)
    assert nothing_written()
    assert reschedule(payment, on=TODAY).ok


def test_today_is_the_new_york_date(fake: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    # 02:00 UTC on Oct 7 is still Oct 6 in New York: Oct 6 is today, not the past.
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    )
    payment = a_return(fake)
    assert reschedule(payment, on=date(2026, 10, 6)).ok


@pytest.mark.parametrize("status", [PaymentStatus.SETTLED, PaymentStatus.PENDING])
def test_a_payment_that_is_not_returned_is_refused(fake: FakeProvider, status: str) -> None:
    payment = a_return(fake)
    Payment.objects.filter(pk=payment.pk).update(status=status)
    outcome = reschedule(payment)
    assert (outcome.ok, outcome.message) == (False, nsf.NOT_RETURNED)
    assert nothing_written()


def test_a_return_is_rescheduled_once(fake: FakeProvider) -> None:
    payment = a_return(fake)
    assert reschedule(payment).ok
    again = reschedule(payment, on=date(2026, 10, 27))
    assert (again.ok, again.message) == (False, nsf.ALREADY)
    assert OneOffCharge.objects.count() == 2 and Job.objects.count() == 1
    assert Operator.objects.get().nsf_return_count == 1


def test_a_return_resolved_with_a_note_cannot_be_rescheduled(fake: FakeProvider) -> None:
    payment = a_return(fake)
    assert returns.resolve_return(payment.pk, note="Paid by wire.", actor=None).ok
    payment.refresh_from_db()
    assert payment.resolution_kind == ResolutionKind.NOTE and payment.redebit_date is None
    outcome = reschedule(payment)
    assert (outcome.ok, outcome.message) == (False, nsf.ALREADY)
    assert nothing_written()


def test_a_card_number_in_the_note_is_refused(fake: FakeProvider) -> None:
    payment = a_return(fake)
    outcome = reschedule(payment, note=f"Card {CARD}")
    assert (outcome.ok, outcome.message) == (False, nsf.PAYMENT_DATA)
    assert nothing_written()


def test_an_operator_without_a_provider_customer_is_refused(fake: FakeProvider) -> None:
    payment = a_return(fake)
    ExternalRef.objects.filter(entity_type="lease", external_kind="customer").delete()
    outcome = reschedule(payment)
    assert (outcome.ok, outcome.message) == (False, nsf.NO_PROVIDER_CUSTOMER)
    assert nothing_written()


def test_an_archived_operator_is_refused(fake: FakeProvider) -> None:
    payment = a_return(fake)
    Operator.objects.update(archived_at=NOW)
    outcome = reschedule(payment)
    assert (outcome.ok, outcome.message) == (False, nsf.OPERATOR_ARCHIVED)
    assert nothing_written()


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("1000.00"))
def test_a_large_debit_needs_the_confirmation(fake: FakeProvider) -> None:
    payment = a_return(fake, "950.00")
    outcome = reschedule(payment)
    assert (outcome.ok, outcome.needs_confirm) == (False, True)
    assert "$1,050.00" in outcome.message
    assert nothing_written()
    assert reschedule(payment, confirmed=True).ok
    assert AuditLog.objects.filter(action=one_off_service.LARGE_DEBIT_ACTION).count() == 1


def test_a_returned_one_off_charge_is_rescheduled_like_rent(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    source = OneOffCharge.objects.create(
        operator=operator,
        category=OneOffCategory.DAMAGE,
        collection=CollectionMethod.EMAIL,
        description="Broken shelf",
        price=D("300.00"),
        due_date=date(2026, 9, 1),
        status=OneOffChargeStatus.OWED,
    )
    payment = Payment.objects.create(
        one_off_charge=source,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("300.00"),
        status=PaymentStatus.RETURNED,
        returned_at=NOW,
    )
    assert reschedule(payment).ok
    redebit, fee = charges_of(payment)
    assert redebit.description == "Re-debit of Broken shelf"
    assert redebit.lease_id is None and redebit.operator_id == operator.pk
    assert fee.price == D("100.00")
    # The source charge is not owed twice: the re-debit carries its open amount.
    rows = receivables.open_balances(TODAY, operator=operator.pk)
    assert {row.charge_id for row in rows} == {redebit.pk, fee.pk}
    assert receivables.total(rows) == D("400.00")


# --- The webhook makes the fee (PLAN-014 F2) -----------------------------------------------


def test_the_return_webhook_makes_the_fee_and_no_staff_reschedule(fake: FakeProvider) -> None:
    # Re-pinned by PLAN-014 F2 (ADR-0016 supersedes ADR-0010's "NSF only by staff"): the
    # return itself makes the $100.00 fee and counts it; the return stays open in the queue
    # and nothing is rescheduled by staff. The world's operator has no linked bank method,
    # so the R01 gets no automatic re-debit (tests/test_returns_nsf.py covers the retry).
    world = make_world(fake)
    provider_txn(fake)
    deliver("create_transaction", id="txn-0001")
    ach_return(fake)
    assert deliver("update_transaction_settled", id="txn-0001") == "processed"
    payment = payment_for()
    assert payment.status == PaymentStatus.RETURNED and payment.resolved_at is None
    fee = OneOffCharge.objects.get(category=OneOffCategory.NSF_FEE)
    assert (fee.source_payment_id, fee.price) == (payment.pk, D("100.00"))
    world.operator.refresh_from_db()
    assert world.operator.nsf_return_count == 1
    assert not AuditLog.objects.filter(action=nsf.ACTION).exists()


# --- Allocation of a payment on the re-debit's invoice -----------------------------------


def _paid_on_the_redebit_invoice(
    fake: FakeProvider, amount: str, *, return_amount: str = "1500.00"
) -> tuple[OneOffCharge, OneOffCharge]:
    """Reschedule (no bank method: an e-mailed invoice), let the worker create the
    invoice, then deliver a settled ACH payment of ``amount`` on it."""
    payment = a_return(fake, return_amount, bank=False)
    assert reschedule(payment).ok
    run_queued_jobs()
    _settle_on_the_redebit_invoice(fake, payment, amount)
    return charges_of(payment)


def _settle_on_the_redebit_invoice(fake: FakeProvider, payment: Payment, amount: str) -> str:
    """Deliver a settled ACH payment of ``amount`` on the invoice of the re-debit of the
    rescheduled ``payment`` (the worker already created it); its transaction ID."""
    # PLAN-014 F2: a return with a fee made at the return has no companion fee.
    redebit = OneOffCharge.objects.get(source_payment=payment, category=OneOffCategory.REDEBIT)
    invoice = Invoice.objects.get(one_off_charge=redebit)
    ref = ExternalRef.objects.get(
        entity_type="invoice", entity_id=invoice.pk, external_kind="invoice"
    ).external_id
    txn = f"txn-{uuid.uuid4().hex[:8]}"
    provider_txn(fake, txn, total=D(amount), invoice_external_id=ref, customer_external_id=CUSTOMER)
    deliver("create_transaction", id=txn)
    settle(fake, txn)
    assert deliver("update_transaction_settled", id=txn) == "processed"
    return txn


@pytest.mark.parametrize(
    ("amount", "redebit_paid", "redebit_status", "fee_paid", "fee_status"),
    [
        # In full: both paid.
        ("1600.00", "1500.00", OneOffChargeStatus.PAID, "100.00", OneOffChargeStatus.PAID),
        # The re-debit only: the fee stays owed.
        ("1500.00", "1500.00", OneOffChargeStatus.PAID, "0.00", OneOffChargeStatus.OWED),
        # Part of the re-debit: nothing reaches the fee.
        ("600.00", "600.00", OneOffChargeStatus.PARTIAL, "0.00", OneOffChargeStatus.OWED),
        # Past the re-debit into part of the fee.
        ("1540.00", "1500.00", OneOffChargeStatus.PAID, "40.00", OneOffChargeStatus.PARTIAL),
    ],
)
def test_a_payment_on_the_invoice_pays_the_re_debit_first_then_the_fee(
    fake: FakeProvider,
    worker: None,
    amount: str,
    redebit_paid: str,
    redebit_status: str,
    fee_paid: str,
    fee_status: str,
) -> None:
    redebit, fee = _paid_on_the_redebit_invoice(fake, amount)
    [payment] = Payment.objects.filter(status=PaymentStatus.SETTLED)
    assert payment.one_off_charge_id == redebit.pk and payment.amount == D(amount)
    assert (redebit.amount_paid, redebit.status) == (D(redebit_paid), redebit_status)
    assert (fee.amount_paid, fee.status) == (D(fee_paid), fee_status)
    allocated = AuditLog.objects.filter(
        action="one_off_charge.payment_allocated", entity_id=str(fee.pk)
    )
    assert allocated.exists() == (D(fee_paid) > 0)


def test_a_returned_re_debit_payment_takes_the_fee_back(fake: FakeProvider, worker: None) -> None:
    redebit, fee = _paid_on_the_redebit_invoice(fake, "1600.00")
    assert fee.status == OneOffChargeStatus.PAID
    [payment] = Payment.objects.filter(status=PaymentStatus.SETTLED)
    txn = ExternalRef.objects.get(entity_id=payment.pk, external_kind="transaction").external_id
    ach_return(fake, txn)
    deliver("update_transaction_settled", id=txn)
    redebit.refresh_from_db()
    fee.refresh_from_db()
    assert (redebit.amount_paid, redebit.status) == (D("0.00"), OneOffChargeStatus.OWED)
    assert (fee.amount_paid, fee.status) == (D("0.00"), OneOffChargeStatus.OWED)


# --- Cancel and waive of the pair --------------------------------------------------------


def test_the_fee_is_not_cancelled_alone_and_goes_with_its_re_debit(fake: FakeProvider) -> None:
    payment = a_return(fake)
    assert reschedule(payment).ok
    redebit, fee = charges_of(payment)
    assert one_off_service.cancel_refusal(fee) == one_off_service.CANCEL_COMPANION
    assert one_off_service.cancel_charge(fee.pk, note="No.", actor=None).ok is False
    AuditLog.objects.create(
        action=one_off_service.CANCEL_ACTION,
        entity_type="one_off_charge",
        entity_id=str(redebit.pk),
        after={"note": "Operator paid by wire."},
    )
    assert one_off_service.mark_voided(redebit.pk, actor=None, context={}).ok
    fee.refresh_from_db()
    assert fee.status == OneOffChargeStatus.VOIDED and fee.void_note
    # No longer carried: the rent charge is owed again.
    rows = receivables.open_balances(TODAY, operator=redebit.operator_id)
    assert [row.kind for row in rows] == [receivables.KIND_RENT]


# --- The attention list and the calendar -------------------------------------------------


def test_the_attention_item_of_the_return_clears_once_rescheduled(fake: FakeProvider) -> None:
    payment = a_return(fake)

    def returned_items() -> list[attention.Item]:
        found = attention.attention(TODAY)
        return [item for item in found.items if item.kind == attention.RETURNED_DEBIT]

    [item] = returned_items()
    assert item.text.startswith("Returned debit not rescheduled:")
    assert reschedule(payment).ok
    assert returned_items() == []


def test_the_re_debit_date_is_a_calendar_item_without_an_amount(fake: FakeProvider) -> None:
    payment = a_return(fake, "1234.56")
    assert reschedule(payment).ok
    redebit, _ = charges_of(payment)
    found = calendar.items(date(2026, 10, 1), date(2026, 10, 31))
    [item] = [entry for entry in found if entry.kind == calendar.KIND_REDEBIT]
    assert item.on == REDEBIT_ON and item.generated
    assert item.title == "Re-debit: Store 0101 (Northwind Stores)"
    assert "1234" not in item.title and "$" not in item.title
    assert item.url == f"/one-off-charges/{redebit.pk}/"
    assert item.operator_id == redebit.operator_id
    only = calendar.items(
        date(2026, 10, 1), date(2026, 10, 31), calendar.Filters(kinds=frozenset({"redebit"}))
    )
    assert [entry.kind for entry in only] == [calendar.KIND_REDEBIT]
    ics = calendar.ics(found, now=NOW, base_url="https://example.com")
    assert f"UID:redebit-{redebit.pk}@" in ics and not has_token(ics, "1234")
    OneOffCharge.objects.filter(pk=redebit.pk).update(
        status=OneOffChargeStatus.VOIDED, voided_at=NOW, void_note="Cancelled."
    )
    assert not [
        entry
        for entry in calendar.items(date(2026, 10, 1), date(2026, 10, 31))
        if entry.kind == calendar.KIND_REDEBIT
    ]


# --- Open balances, statement and reports ------------------------------------------------


def test_the_amount_is_owed_once_and_nothing_once_paid(fake: FakeProvider, worker: None) -> None:
    payment = a_return(fake, "1500.00", bank=False)
    operator_id = returns.row(payment).operator.pk  # type: ignore[union-attr]
    before = receivables.open_balances(TODAY, operator=operator_id)
    assert receivables.total(before) == D("1500.00")
    assert reschedule(payment).ok
    after = receivables.open_balances(TODAY, operator=operator_id)
    # The rent charge is carried by the re-debit: 1500 + 100, not 3100.
    assert receivables.total(after) == D("1600.00")
    assert {row.description for row in after} == {
        "Re-debit: Re-debit of Rent 2026-09",
        "Returned payment (NSF) fee: NSF fee, first return: returned debit of Rent 2026-09",
    }
    run_queued_jobs()
    redebit, _ = charges_of(payment)
    invoice = Invoice.objects.get(one_off_charge=redebit)
    ref = ExternalRef.objects.get(entity_id=invoice.pk, external_kind="invoice").external_id
    provider_txn(
        fake, "txn-full", total=D("1600.00"), invoice_external_id=ref, customer_external_id=CUSTOMER
    )
    deliver("create_transaction", id="txn-full")
    settle(fake, "txn-full")
    deliver("update_transaction_settled", id="txn-full")
    assert receivables.open_balances(TODAY, operator=operator_id) == []


def test_the_statement_shows_both_lines(fake: FakeProvider) -> None:
    payment = a_return(fake, "1500.00")
    assert reschedule(payment).ok
    operator = Operator.objects.get(pk=returns.row(payment).operator.pk)  # type: ignore[union-attr]
    report = reports.statement(operator, reports.parse_range({}, TODAY), TODAY)
    # Both due on the re-debit date; their order within the day follows their ids.
    descriptions = sorted(item.description for item in report.open_charges)
    assert descriptions == [
        "Re-debit: Re-debit of Rent 2026-09",
        "Returned payment (NSF) fee: NSF fee, first return: returned debit of Rent 2026-09",
    ]
    assert report.open_total == D("1600.00")


def _settled_on_redebit(
    redebit: OneOffCharge, amount: str, on: date, *, refund: bool = False
) -> None:
    Payment.objects.create(
        one_off_charge=redebit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D(amount),
        status=PaymentStatus.SETTLED,
        settled_on=on,
        initiated_on=on,
        is_refund=refund,
        parent_payment=Payment.objects.filter(one_off_charge=redebit).first() if refund else None,
    )


def test_the_pnl_counts_the_re_debit_as_rent_collected_and_the_fee_as_fees(
    fake: FakeProvider,
) -> None:
    payment = a_return(fake, "1500.00")
    assert reschedule(payment).ok
    redebit, _ = charges_of(payment)
    _settled_on_redebit(redebit, "1000.00", date(2026, 10, 20))
    _settled_on_redebit(redebit, "600.00", date(2026, 11, 3))
    october = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 10, 1), date(2026, 10, 1))))
    november = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 11, 1), date(2026, 11, 1))))
    # Billed: the fee (due in October) is a fee; the re-debit is never billed again.
    assert october.fees_billed == D("100.00")
    assert october.rent_billed == D("0.00")
    assert october.rent_collected == D("1000.00") and october.fees_collected == D("0.00")
    # November: 500 finishes the re-debit (rent), 100 pays the fee (fees).
    assert november.rent_collected == D("500.00") and november.fees_collected == D("100.00")
    # The revenue report reads the same split.
    selected = reports.ReportRange(date(2026, 10, 1), date(2026, 11, 1))
    revenue = reports.revenue_by_month(selected)
    assert revenue.total.rent_collected == D("1500.00")
    assert revenue.total.fees_collected == D("100.00")


def test_a_re_debit_of_a_one_off_charge_is_fees_collected(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    source = OneOffCharge.objects.create(
        operator=operator,
        category=OneOffCategory.DAMAGE,
        collection=CollectionMethod.EMAIL,
        description="Broken shelf",
        price=D("300.00"),
        due_date=date(2026, 9, 1),
    )
    payment = Payment.objects.create(
        one_off_charge=source,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("300.00"),
        status=PaymentStatus.RETURNED,
        returned_at=NOW,
    )
    assert reschedule(payment).ok
    redebit, _ = charges_of(payment)
    _settled_on_redebit(redebit, "400.00", date(2026, 10, 20))
    october = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 10, 1), date(2026, 10, 1))))
    assert october.rent_collected == D("0.00")
    assert october.fees_collected == D("400.00")


def test_a_refund_of_a_re_debit_payment_is_taken_back_from_the_re_debit_part(
    fake: FakeProvider,
) -> None:
    payment = a_return(fake, "1500.00")
    assert reschedule(payment).ok
    redebit, _ = charges_of(payment)
    _settled_on_redebit(redebit, "1600.00", date(2026, 10, 20))
    _settled_on_redebit(redebit, "1600.00", date(2026, 11, 3), refund=True)
    october = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 10, 1), date(2026, 10, 1))))
    november = pnl.total(pnl.rows(pnl.PnlQuery(date(2026, 11, 1), date(2026, 11, 1))))
    assert (october.rent_collected, october.fees_collected) == (D("1500.00"), D("100.00"))
    assert (november.rent_collected, november.fees_collected) == (D("-1500.00"), D("-100.00"))


# --- The returns page and its role gates -------------------------------------------------


def _post(role: Role, payment: Payment, **data: str) -> object:
    client, _ = client_for(role)
    values = {"redebit_date": REDEBIT_ON.isoformat(), "note": "", **data}
    return client.post(f"/returns/{payment.pk}/reschedule/", values)


def test_billing_reschedules_from_the_returns_page(fake: FakeProvider) -> None:
    payment = a_return(fake)
    response = _post(Role.BILLING, payment, note="Agreed on the phone.")
    redebit, fee = charges_of(payment)
    assert response.status_code == 302  # type: ignore[attr-defined]
    assert response["Location"] == f"/one-off-charges/{redebit.pk}/"  # type: ignore[index]
    assert fee.status == OneOffChargeStatus.OWED
    # Both charges have their page, as any one-off charge.
    client, _ = client_for(Role.VIEWER)
    for charge in (redebit, fee):
        page = client.get(f"/one-off-charges/{charge.pk}/")
        assert page.status_code == 200
        assert charge.description in page.content.decode()


def test_a_viewer_cannot_reschedule(fake: FakeProvider) -> None:
    payment = a_return(fake)
    assert _post(Role.VIEWER, payment).status_code == 403  # type: ignore[attr-defined]
    assert nothing_written()


def test_billing_asking_for_a_waiver_gets_403(fake: FakeProvider) -> None:
    payment = a_return(fake)
    response = _post(Role.BILLING, payment, waive_fee="on", note="Please waive.")
    assert response.status_code == 403  # type: ignore[attr-defined]
    assert nothing_written()


def test_an_admin_waives_from_the_returns_page(fake: FakeProvider) -> None:
    payment = a_return(fake)
    response = _post(Role.ADMIN, payment, waive_fee="on", note="Bank error.")
    assert response.status_code == 302  # type: ignore[attr-defined]
    assert charges_of(payment)[1].status == OneOffChargeStatus.WAIVED


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"redebit_date": "2026-10-05"}, nsf.DATE_IN_PAST),
        ({"redebit_date": ""}, "Enter the re-debit date."),
        ({"redebit_date": "not-a-date"}, "Enter a valid date."),
    ],
)
def test_the_page_refuses_a_bad_date_with_a_message(
    fake: FakeProvider, data: dict[str, str], message: str
) -> None:
    payment = a_return(fake)
    client, _ = client_for(Role.BILLING)
    response = client.post(f"/returns/{payment.pk}/reschedule/", {"note": "", **data}, follow=True)
    assert response.status_code == 200
    assert message in response.content.decode()
    assert nothing_written()


def test_an_unknown_payment_is_404(fake: FakeProvider) -> None:
    client, _ = client_for(Role.BILLING)
    for key in (str(uuid.uuid4()), "not-a-uuid"):
        response = client.post(f"/returns/{key}/reschedule/", {"redebit_date": "2026-10-20"})
        assert response.status_code == 404


def test_the_returns_page_offers_reschedule_to_billing_and_the_waiver_to_admins(
    fake: FakeProvider,
) -> None:
    payment = a_return(fake)
    pages = {role: client_for(role)[0].get("/returns/").content.decode() for role in Role}
    form = f'id="reschedule-{payment.pk}"'
    waive = f'id="waive-fee-{payment.pk}"'
    assert form not in pages[Role.VIEWER]
    assert form in pages[Role.BILLING] and waive not in pages[Role.BILLING]
    assert form in pages[Role.ADMIN] and waive in pages[Role.ADMIN]
    assert 'min="2026-10-06"' in pages[Role.BILLING]
    assert "$100.00" in pages[Role.VIEWER] and "$50.00" in pages[Role.VIEWER]


# --- The source charge of a re-debit (QA finding on Batch C) -----------------------------


def _source_rent(payment: Payment) -> RentCharge:
    assert payment.rent_charge_id is not None
    return RentCharge.objects.get(pk=payment.rent_charge_id)


def _open_total(payment: Payment) -> Decimal:
    operator = returns.row(payment).operator
    assert operator is not None
    return receivables.total(receivables.open_balances(TODAY, operator=operator.pk))


def _september_cell(charge: RentCharge) -> grid.Cell:
    built = grid.build(grid.parse_query({"from": "2026-09", "to": "2026-10"}, TODAY), TODAY)
    [row] = [row for row in built.rows if row.store.pk == charge.lease.store_id]
    [cell] = [cell for cell in row.cells if cell.month == date(2026, 9, 1)]
    return cell


def _october_debit(charge: RentCharge) -> rent_run.DueDebit:
    """The lease's debit on the run page of its October debit day (September is last
    month there)."""
    assert charge.lease.debit_day is not None
    run = rent_run.leases_due(date(2026, 10, charge.lease.debit_day))
    [item] = [item for item in run.due if item.lease.pk == charge.lease_id]
    return item


@pytest.mark.parametrize(
    ("amount", "rent_paid", "rent_status", "open_total"),
    [
        # In full: the month is paid, nothing is open.
        ("1600.00", "1500.00", RentChargeStatus.PAID, "0.00"),
        # The re-debit only: the month is paid, the fee is still owed.
        ("1500.00", "1500.00", RentChargeStatus.PAID, "100.00"),
        # Part of the re-debit: the month is partly paid.
        ("600.00", "600.00", RentChargeStatus.PARTIAL, "1000.00"),
        # Past the re-debit: the fee part never reaches the month.
        ("1540.00", "1500.00", RentChargeStatus.PAID, "60.00"),
    ],
)
def test_a_settled_re_debit_pays_the_rent_charge_it_collects_again(
    fake: FakeProvider,
    worker: None,
    amount: str,
    rent_paid: str,
    rent_status: str,
    open_total: str,
) -> None:
    payment = a_return(fake, "1500.00", bank=False)
    assert reschedule(payment).ok
    run_queued_jobs()
    assert _open_total(payment) == D("1600.00")
    _settle_on_the_redebit_invoice(fake, payment, amount)
    charge = _source_rent(payment)
    assert (charge.amount_paid, charge.status) == (D(rent_paid), rent_status)
    assert charge.balance == D("1500.00") - D(rent_paid)
    redebit, _ = charges_of(payment)
    [applied] = AuditLog.objects.filter(
        action="rent_charge.payment_applied", entity_id=str(charge.pk)
    )
    assert applied.before is not None and applied.after is not None
    assert (applied.before["amount_paid"], applied.before["status"]) == ("0.00", "unpaid")
    assert (applied.after["amount_paid"], applied.after["status"]) == (rent_paid, rent_status)
    assert applied.after["redebit"] == str(redebit.pk)
    # Owed once: the open balance is what the re-debit and its fee have still to collect.
    assert _open_total(payment) == D(open_total)
    operator = returns.row(payment).operator
    assert operator is not None
    rows = receivables.open_balances(TODAY, operator=operator.pk)
    assert receivables.KIND_RENT not in {row.kind for row in rows}


def test_the_month_turns_green_on_the_grid_and_the_run_page_and_red_again_on_a_return(
    fake: FakeProvider, worker: None
) -> None:
    payment = a_return(fake, "1500.00", bank=False)
    charge = _source_rent(payment)
    assert _september_cell(charge).status == grid.STATUS_UNPAID
    assert reschedule(payment).ok
    run_queued_jobs()
    txn = _settle_on_the_redebit_invoice(fake, payment, "1600.00")
    cell = _september_cell(charge)
    assert (cell.status, cell.amount_paid) == (grid.STATUS_PAID, D("1500.00"))
    debit = _october_debit(charge)
    assert debit.last_month is not None
    assert (debit.last_month.status, debit.last_month.balance) == (
        RentChargeStatus.PAID,
        D("0.00"),
    )
    assert debit.carried_balance == D("0.00")
    client, _ = client_for(Role.VIEWER)
    assert client.get("/grid/?from=2026-09&to=2026-10").status_code == 200
    # The re-debit payment is returned: the month reopens, red again.
    ach_return(fake, txn)
    assert deliver("update_transaction_settled", id=txn) == "processed"
    charge.refresh_from_db()
    assert (charge.amount_paid, charge.status) == (D("0.00"), RentChargeStatus.UNPAID)
    assert _september_cell(charge).status == grid.STATUS_UNPAID
    debit = _october_debit(charge)
    assert debit.last_month is not None
    assert debit.last_month.status == RentChargeStatus.UNPAID
    assert debit.carried_balance == D("1500.00")
    assert (
        AuditLog.objects.filter(
            action="rent_charge.payment_applied", entity_id=str(charge.pk)
        ).count()
        == 2
    )
    # Owed once again: the re-debit and its fee, the rent carried by the re-debit; and
    # (re-pinned by PLAN-014 F2) the $100.00 fee the second return made at once.
    assert _open_total(payment) == D("1700.00")


@pytest.mark.parametrize(
    ("refund", "rent_paid", "rent_status"),
    [
        ("1600.00", "0.00", RentChargeStatus.UNPAID),
        ("200.00", "1400.00", RentChargeStatus.PARTIAL),
    ],
)
def test_a_refund_of_the_re_debit_payment_reopens_the_month(
    fake: FakeProvider, worker: None, refund: str, rent_paid: str, rent_status: str
) -> None:
    payment = a_return(fake, "1500.00", bank=False)
    assert reschedule(payment).ok
    run_queued_jobs()
    txn = _settle_on_the_redebit_invoice(fake, payment, "1600.00")
    provider_txn(
        fake,
        "txn-refund",
        total=D(refund),
        kind="refund",
        parent_external_id=txn,
        invoice_external_id="",
        customer_external_id=CUSTOMER,
    )
    deliver("create_transaction", id="txn-refund")
    settle(fake, "txn-refund")
    assert deliver("update_transaction_settled", id="txn-refund") == "processed"
    charge = _source_rent(payment)
    assert (charge.amount_paid, charge.status) == (D(rent_paid), rent_status)
    redebit, fee = charges_of(payment)
    assert redebit.amount_paid == D(rent_paid)
    assert fee.amount_paid == D("0.00")
    assert _open_total(payment) == D("1600.00") - D(rent_paid)


def test_a_void_of_the_re_debit_payment_reopens_the_month(fake: FakeProvider, worker: None) -> None:
    payment = a_return(fake, "1500.00", bank=False)
    assert reschedule(payment).ok
    run_queued_jobs()
    txn = _settle_on_the_redebit_invoice(fake, payment, "1500.00")
    assert _source_rent(payment).status == RentChargeStatus.PAID
    fake.transactions[txn] = replace(fake.transactions[txn], is_voided=True)
    assert deliver("update_transaction", id=txn) == "processed"
    charge = _source_rent(payment)
    assert (charge.amount_paid, charge.status) == (D("0.00"), RentChargeStatus.UNPAID)
    assert _open_total(payment) == D("1600.00")


def test_a_payment_recorded_on_the_re_debit_pays_the_month(fake: FakeProvider) -> None:
    payment = a_return(fake, "1500.00")
    assert reschedule(payment).ok
    redebit, _ = charges_of(payment)
    outcome = one_off_service.record_payment(
        redebit.pk,
        method=PaymentMethod.ZELLE,
        amount=D("500.00"),
        paid_on=TODAY,
        note="Zelle received.",
        actor=None,
        today=TODAY,
    )
    assert outcome.ok
    charge = _source_rent(payment)
    assert (charge.amount_paid, charge.status) == (D("500.00"), RentChargeStatus.PARTIAL)
    assert AuditLog.objects.filter(
        action="rent_charge.payment_applied", entity_id=str(charge.pk)
    ).exists()
    assert _open_total(payment) == D("1100.00")


def test_a_re_debit_pays_its_insurance_charge(fake: FakeProvider, worker: None) -> None:
    owner = an_operator(fake, bank=None)
    lease = make_lease(operator=owner)
    insurance = InsuranceCharge.objects.create(
        lease=lease, policy_year=2026, due_on=date(2026, 9, 15), amount=D("400.00")
    )
    payment = Payment.objects.create(
        insurance_charge=insurance,
        kind=PaymentKind.INSURANCE,
        policy_year=2026,
        method=PaymentMethod.ACH,
        amount=D("400.00"),
        status=PaymentStatus.RETURNED,
        returned_at=NOW,
    )
    assert _open_total(payment) == D("400.00")
    assert reschedule(payment).ok
    run_queued_jobs()
    assert _open_total(payment) == D("500.00")
    _settle_on_the_redebit_invoice(fake, payment, "300.00")
    [applied] = AuditLog.objects.filter(
        action="insurance_charge.payment_applied", entity_id=str(insurance.pk)
    )
    assert applied.before is not None and applied.after is not None
    assert (applied.before["amount_paid"], applied.after["amount_paid"]) == ("0.00", "300.00")
    # 100 left on the re-debit and the 100 fee; the insurance charge is carried.
    rows = receivables.open_balances(TODAY, operator=owner.pk)
    assert receivables.KIND_INSURANCE not in {row.kind for row in rows}
    assert receivables.total(rows) == D("200.00")
    _settle_on_the_redebit_invoice(fake, payment, "200.00")
    assert receivables.open_balances(TODAY, operator=owner.pk) == []


def test_a_re_debit_pays_its_one_off_charge(fake: FakeProvider, worker: None) -> None:
    operator = an_operator(fake, bank=None)
    source = OneOffCharge.objects.create(
        operator=operator,
        category=OneOffCategory.DAMAGE,
        collection=CollectionMethod.EMAIL,
        description="Broken shelf",
        price=D("300.00"),
        due_date=date(2026, 9, 1),
        status=OneOffChargeStatus.OWED,
    )
    payment = Payment.objects.create(
        one_off_charge=source,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("300.00"),
        status=PaymentStatus.RETURNED,
        returned_at=NOW,
    )
    assert reschedule(payment).ok
    run_queued_jobs()
    _settle_on_the_redebit_invoice(fake, payment, "350.00")
    source.refresh_from_db()
    assert (source.amount_paid, source.status) == (D("300.00"), OneOffChargeStatus.PAID)
    redebit, fee = charges_of(payment)
    assert (fee.amount_paid, fee.status) == (D("50.00"), OneOffChargeStatus.PARTIAL)
    applied = AuditLog.objects.get(
        action="one_off_charge.payment_applied", entity_id=str(source.pk)
    )
    assert applied.after is not None and applied.after["redebit"] == str(redebit.pk)
    assert receivables.total(receivables.open_balances(TODAY, operator=operator.pk)) == D("50.00")


def test_a_re_debit_of_a_returned_re_debit_payment_pays_the_month_and_the_first_fee(
    fake: FakeProvider, worker: None
) -> None:
    payment = a_return(fake, "1500.00", bank=False)
    assert reschedule(payment).ok
    run_queued_jobs()
    txn = _settle_on_the_redebit_invoice(fake, payment, "1600.00")
    ach_return(fake, txn)
    deliver("update_transaction_settled", id=txn)
    second_return = payment_for(txn)
    assert second_return.status == PaymentStatus.RETURNED
    assert reschedule(second_return).ok
    run_queued_jobs()
    # Re-pinned by PLAN-014 F2: the second return made its own $100.00 fee at once, so
    # Debit again adds none: 1600 again on the invoice and the fee of 100 on its own page.
    assert _open_total(payment) == D("1700.00")
    _settle_on_the_redebit_invoice(fake, second_return, "1600.00")
    return_fee = nsf.return_fee(second_return)
    assert return_fee is not None
    assert one_off_service.waive(return_fee.pk, note="First time.", actor=None).ok
    charge = _source_rent(payment)
    assert (charge.amount_paid, charge.status) == (D("1500.00"), RentChargeStatus.PAID)
    first, first_fee = charges_of(payment)
    assert (first.status, first_fee.status) == (OneOffChargeStatus.PAID, OneOffChargeStatus.PAID)
    assert _open_total(payment) == D("0.00")
