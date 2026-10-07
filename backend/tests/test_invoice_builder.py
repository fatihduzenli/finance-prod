"""The invoice builder (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D6, D7, D9;
§6 items of Batch B; the adversarial cases are the QA owner's file).

The service (:mod:`ledger.services.invoice_builder`), the worker's single create with
every line (:mod:`payments.one_off_invoices`, run through the queue), the allocation of
a payment on the invoice across its lines' charges (real webhook deliveries to a
:class:`~payments.fake.FakeProvider`), cancel, Charge now, the pages and their role gates,
the P&L and the open balances. Nothing reaches a network.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings

from accounts.roles import Role
from ledger.models import (
    NSF_FEE_FIRST,
    AuditLog,
    CollectionMethod,
    FeeType,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceLineItem,
    InvoiceStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentStatus,
    Store,
    StoreStatus,
)
from ledger.services import invoice_builder, nsf, payments_ledger, pnl, receivables, reports
from ledger.services import one_off_charges as one_off_service
from ledger.services.invoice_builder import LineInput
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import InvoiceInput, PayWithStoredMethod
from payments.stax import validation
from tests.job_helpers import run_queued_jobs
from tests.test_charge_now import store_methods
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import CUSTOMER, an_operator, calls
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import ach_return, deliver, provider_txn, settle

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
CARD = "4111 1111 1111 1111"
MEMO = "Works of October at the store."


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


# --- Builders ----------------------------------------------------------------------------


def user(role: Role) -> Any:
    return client_for(role)[1]


def line(
    description: str = "Sign repair",
    unit: str | None = "100.00",
    quantity: str | None = None,
    fee_type: FeeType | None = None,
) -> LineInput:
    return LineInput(
        fee_type=fee_type,
        description=description,
        quantity=D(quantity) if quantity is not None else None,
        unit_amount=D(unit) if unit is not None else None,
    )


def a_world(fake: FakeProvider) -> tuple[Operator, Lease]:
    operator = an_operator(fake)
    return operator, make_lease(operator=operator)


def a_draft(
    operator: Operator,
    lease: Lease | None,
    lines: list[LineInput],
    *,
    due: date | None = None,
    memo: str = MEMO,
) -> InvoiceDraft:
    outcome = invoice_builder.create_draft(
        operator, lease, memo, due, lines, user(Role.BILLING), today=TODAY
    )
    assert outcome.ok and outcome.draft is not None, outcome.message
    return outcome.draft


def sent(draft: InvoiceDraft) -> invoice_builder.Outcome:
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert outcome.ok, outcome.message
    return outcome


def fresh(row: Any) -> Any:
    return type(row).objects.get(pk=row.pk)


def charges_of(draft: InvoiceDraft) -> list[OneOffCharge]:
    return invoice_builder.sent_charges(fresh(draft))


def created_invoices(fake: FakeProvider) -> list[InvoiceInput]:
    return [args[0] for args in calls(fake, "create_invoice")]  # type: ignore[misc]


def provider_ref(invoice: Invoice) -> str:
    return str(
        ExternalRef.objects.get(
            entity_type="invoice", entity_id=invoice.pk, external_kind="invoice"
        ).external_id
    )


def pay(fake: FakeProvider, invoice: Invoice, amount: str, txn: str | None = None) -> str:
    """A settled ACH payment of ``amount`` on the invoice, through the webhooks."""
    txn = txn or f"txn-{uuid.uuid4().hex[:8]}"
    provider_txn(
        fake,
        txn,
        total=D(amount),
        invoice_external_id=provider_ref(invoice),
        customer_external_id=CUSTOMER,
    )
    deliver("create_transaction", id=txn)
    settle(fake, txn)
    assert deliver("update_transaction_settled", id=txn) == "processed"
    return txn


def paid_states(draft: InvoiceDraft) -> list[tuple[Decimal, str]]:
    return [(charge.amount_paid, charge.status) for charge in charges_of(draft)]


def two_lines() -> list[LineInput]:
    return [line("Arrears September", "100.00"), line("Sign repair", "50.00")]


def sent_two(fake: FakeProvider) -> tuple[InvoiceDraft, Invoice]:
    """A two-line draft (100.00 then 50.00) sent, its invoice created by the worker."""
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    return draft, fresh(invoice)


def a_fee_type(code: str, name: str) -> FeeType:
    return FeeType.objects.create(code=code, name=name, sort_order=500)


# --- Drafts: lines, totals, due date (§6) -------------------------------------------------


def test_a_draft_is_local_only_until_it_is_sent(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    assert draft.status == InvoiceDraftStatus.DRAFT and draft.number.startswith("DRAFT-")
    assert [row.line_total for row in invoice_builder.lines_of(draft)] == [D("100.00"), D("50.00")]
    assert not fake.calls and not Job.objects.exists() and not OneOffCharge.objects.exists()
    assert AuditLog.objects.filter(action=invoice_builder.CREATED_ACTION).count() == 1


@pytest.mark.parametrize("count", [1, 20])
def test_one_line_and_twenty_lines_make_one_invoice_with_every_line(
    fake: FakeProvider, worker: None, count: int
) -> None:
    operator, lease = a_world(fake)
    lines = [line(f"Item {index}", f"{10 + index}.01") for index in range(1, count + 1)]
    draft = a_draft(operator, lease, lines)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert len(request.amounts.line_items) == count
    expected = sum((D(f"{10 + index}.01") for index in range(1, count + 1)), D("0"))
    assert request.amounts.total == expected == fresh(invoice).total
    validation.amounts(request.amounts)
    charges = charges_of(draft)
    assert len(charges) == count == InvoiceLineItem.objects.filter(invoice=invoice).count()
    assert [charge.description for charge in charges] == [f"Item {i}" for i in range(1, count + 1)]


def test_quantity_half_and_three_are_totalled_and_named_on_the_charge(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(
        operator,
        lease,
        [line("Cleaning hours", "0.25", quantity="0.5"), line("Poster", "33.33", quantity="3")],
    )
    half, three = invoice_builder.lines_of(draft)
    # 0.5 x 0.25 = 0.125, rounded half up to the cent; 3 x 33.33 = 99.99 exactly.
    assert (half.line_total, three.line_total) == (D("0.13"), D("99.99"))
    assert (half.quantity_text, three.quantity_text) == ("0.5", "3")
    sent(draft)
    first, second = charges_of(draft)
    assert (first.price, first.description) == (D("0.13"), "Cleaning hours (0.5 × 0.25)")
    assert (second.price, second.description) == (D("99.99"), "Poster (3 × 33.33)")
    run_queued_jobs()
    [request] = created_invoices(fake)
    lines = request.amounts.line_items
    # A quantity whose product is not the cent total goes as 1 at the total, named.
    assert (lines[0].description, lines[0].quantity, lines[0].unit_price) == (
        "Cleaning hours (0.5 × 0.25)",
        D(1),
        D("0.13"),
    )
    assert (lines[1].description, lines[1].quantity, lines[1].unit_price) == (
        "Poster",
        D(3),
        D("33.33"),
    )
    assert request.amounts.total == D("100.12")
    validation.amounts(request.amounts)


def test_line_totals_sum_to_the_cent(fake: FakeProvider, worker: None) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Ten cents", "0.10"), line("Twenty cents", "0.20")])
    invoice = sent(draft).invoice
    assert invoice is not None and invoice.total == D("0.30") and invoice.subtotal == D("0.30")
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert (
        request.amounts.total
        == D("0.30")
        == sum((item.total for item in request.amounts.line_items), D("0"))
    )


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (line(unit="0.00"), invoice_builder.LINE_UNIT_AMOUNT),
        (line(unit="-5.00"), invoice_builder.LINE_UNIT_AMOUNT),
        (line(unit="1.005"), invoice_builder.LINE_UNIT_AMOUNT),
        (line(unit=None), invoice_builder.LINE_UNIT_AMOUNT),
        (line(quantity="0"), invoice_builder.LINE_QUANTITY),
        (line(quantity="0.00001"), invoice_builder.LINE_QUANTITY),
        (line(unit="0.01", quantity="0.0001"), invoice_builder.LINE_TOTAL_ZERO),
        (line(unit="999999.99", quantity="2"), invoice_builder.LINE_TOTAL_TOO_LARGE),
        (line(description=""), invoice_builder.LINE_DESCRIPTION_REQUIRED),
        (line(description="x" * 121), invoice_builder.LINE_DESCRIPTION_TOO_LONG),
        (line(description=f"Card {CARD}"), invoice_builder.LINE_PAYMENT_DATA),
    ],
)
def test_a_bad_line_is_refused_with_nothing_saved(
    fake: FakeProvider, bad: LineInput, message: str
) -> None:
    operator, lease = a_world(fake)
    outcome = invoice_builder.create_draft(
        operator, lease, MEMO, None, [line(), bad], user(Role.BILLING), today=TODAY
    )
    assert (outcome.ok, outcome.message) == (False, message.format(line=2))
    assert not InvoiceDraft.objects.exists()


def test_no_line_and_too_many_lines_are_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    billing = user(Role.BILLING)
    assert invoice_builder.create_draft(operator, lease, "", None, [], billing).message == (
        invoice_builder.NO_LINES
    )
    many = [line() for _ in range(invoice_builder.MAX_LINES + 1)]
    assert invoice_builder.create_draft(operator, lease, "", None, many, billing).message == (
        invoice_builder.TOO_MANY_LINES
    )
    assert not InvoiceDraft.objects.exists()


def test_a_card_number_in_the_memo_is_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    outcome = invoice_builder.create_draft(
        operator, lease, f"Pay with {CARD}", None, [line()], user(Role.BILLING)
    )
    assert (outcome.ok, outcome.field) == (False, "memo")
    assert not InvoiceDraft.objects.exists()


def test_a_due_date_in_the_past_is_refused_and_today_is_accepted(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    billing = user(Role.BILLING)
    past = invoice_builder.create_draft(
        operator, lease, "", TODAY - timedelta(days=1), [line()], billing, today=TODAY
    )
    assert (past.ok, past.message) == (False, invoice_builder.DUE_IN_PAST)
    draft = a_draft(operator, lease, [line()], due=TODAY)
    changed = invoice_builder.update_draft(
        draft.pk, lease, "", TODAY - timedelta(days=1), [line()], billing, today=TODAY
    )
    assert (changed.ok, changed.message) == (False, invoice_builder.DUE_IN_PAST)
    assert fresh(draft).due_date == TODAY


def test_without_a_due_date_the_invoice_is_due_fifteen_new_york_days_later(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 02:00 UTC on Oct 7 is still Oct 6 in New York.
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: datetime(2026, 10, 7, 2, 0, tzinfo=UTC)
    )
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()], due=None)
    invoice = sent(draft).invoice
    assert invoice is not None
    assert (invoice.issue_date, invoice.due_date) == (TODAY, TODAY + timedelta(days=15))
    assert {charge.due_date for charge in charges_of(draft)} == {TODAY + timedelta(days=15)}


def test_a_draft_sent_after_its_due_date_passed_is_refused(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()], due=TODAY)
    InvoiceDraft.objects.filter(pk=draft.pk).update(due_date=TODAY - timedelta(days=1))
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.DUE_IN_PAST)
    assert not Invoice.objects.exists() and not OneOffCharge.objects.exists()


def test_update_replaces_the_lines_as_a_set_while_a_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = invoice_builder.update_draft(
        draft.pk, None, "New memo.", None, [line("Only line", "75.00")], user(Role.BILLING)
    )
    assert outcome.ok
    draft = fresh(draft)
    assert (draft.lease_id, draft.memo) == (None, "New memo.")
    assert [row.description for row in invoice_builder.lines_of(draft)] == ["Only line"]
    sent(draft)
    later = invoice_builder.update_draft(draft.pk, lease, "", None, [line()], user(Role.BILLING))
    assert (later.ok, later.message) == (False, invoice_builder.NOT_A_DRAFT)


# --- The fee-type category mapping (D6) ------------------------------------------------------


def test_the_fee_type_decides_the_category(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    damage = a_fee_type("damage", "Damage repair")
    sign_fee = FeeType.objects.get(code="sign_fee")
    draft = a_draft(
        operator,
        lease,
        [
            line("", "300.00", fee_type=damage),
            line("", "150.00", fee_type=sign_fee),
            line("Free text line", "20.00"),
        ],
    )
    assert [row.description for row in invoice_builder.lines_of(draft)] == [
        "Damage repair",
        "Sign fee",
        "Free text line",
    ]
    sent(draft)
    damage_charge, sign_charge, other_charge = charges_of(draft)
    assert (damage_charge.category, damage_charge.fee_type_id) == (OneOffCategory.DAMAGE, None)
    assert (sign_charge.category, sign_charge.fee_type_id) == (OneOffCategory.SETUP, sign_fee.pk)
    assert sign_charge.collection == CollectionMethod.EMAIL
    assert (other_charge.category, other_charge.fee_type_id) == (OneOffCategory.OTHER, None)
    # A billed setup charge is not edited like a checklist one: its invoice is out.
    edited = one_off_service.edit_charge(
        sign_charge.pk,
        cost=None,
        price=D("1.00"),
        vendor="",
        due_date=TODAY,
        actor=None,
    )
    assert (edited.ok, edited.message) == (False, one_off_service.EDIT_ONE_OFF)


def test_an_nsf_or_inactive_fee_type_is_not_offered(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    nsf_fee = FeeType.objects.get(code=NSF_FEE_FIRST)
    inactive = a_fee_type("old_fee", "Old fee")
    FeeType.objects.filter(pk=inactive.pk).update(active=False)
    for fee_type in (nsf_fee, inactive):
        outcome = invoice_builder.create_draft(
            operator, lease, "", None, [line(fee_type=fee_type)], user(Role.BILLING)
        )
        assert outcome.message == invoice_builder.LINE_FEE_TYPE.format(line=1)


def test_a_lease_takes_one_setup_charge_per_fee_type(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    sign_fee = FeeType.objects.get(code="sign_fee")
    twice = invoice_builder.create_draft(
        operator,
        lease,
        "",
        None,
        [line(fee_type=sign_fee), line(fee_type=sign_fee)],
        user(Role.BILLING),
    )
    assert twice.message == invoice_builder.LINE_SAME_FEE_TYPE.format(line=2, name="Sign fee")
    # Already charged on the lease by a first invoice: the second send writes nothing.
    sent(a_draft(operator, lease, [line(fee_type=sign_fee)]))
    second = a_draft(operator, lease, [line("Poster"), line(fee_type=sign_fee)])
    outcome = invoice_builder.send(second.pk, user(Role.BILLING))
    assert not outcome.ok and outcome.message.startswith("Line 2: ")
    assert OneOffCharge.objects.count() == 1 and Invoice.objects.count() == 1
    assert fresh(second).status == InvoiceDraftStatus.DRAFT


# --- Send (D6) ---------------------------------------------------------------------------


def test_the_two_line_draft_end_to_end_through_the_worker(fake: FakeProvider, worker: None) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = sent(draft)
    invoice = outcome.invoice
    assert invoice is not None and outcome.job is not None
    first, second = charges_of(draft)
    assert (first.lease_id, second.lease_id) == (lease.pk, lease.pk)
    assert (first.operator_id, second.operator_id) == (operator.pk, operator.pk)
    assert {first.collection, second.collection} == {CollectionMethod.EMAIL}
    assert Invoice.objects.get() == invoice and invoice.one_off_charge_id == first.pk
    assert (invoice.total, invoice.memo, invoice.due_date) == (
        D("150.00"),
        MEMO,
        TODAY + timedelta(days=15),
    )
    items = list(invoice.line_items.order_by("position"))
    assert [(item.position, item.one_off_charge_id) for item in items] == [
        (1, first.pk),
        (2, second.pk),
    ]
    [job] = Job.objects.all()
    assert (job.kind, job.entity_id) == (JobKind.INVOICE_CREATE, first.pk)
    run_queued_jobs()
    [request] = created_invoices(fake)
    assert [(item.description, item.total) for item in request.amounts.line_items] == [
        ("Arrears September", D("100.00")),
        ("Sign repair", D("50.00")),
    ]
    assert request.amounts.total == D("150.00") and request.reference == invoice.invoice_number
    assert request.memo == MEMO
    validation.amounts(request.amounts)
    assert len(calls(fake, "send_invoice_email")) == 1
    assert fresh(invoice).internal_status == InvoiceStatus.SENT
    draft = fresh(draft)
    assert (draft.status, draft.sent_invoice_id) == (InvoiceDraftStatus.SENT, invoice.pk)


def test_send_twice_makes_one_invoice(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    sent(draft)
    number = Invoice.objects.get().invoice_number
    again = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (again.ok, again.message) == (False, invoice_builder.ALREADY_SENT.format(number=number))
    client, _ = client_for(Role.BILLING)
    assert client.post(f"/invoices/drafts/{draft.pk}/send/").status_code == 302
    assert Invoice.objects.count() == 1 and OneOffCharge.objects.count() == 2
    assert Job.objects.count() == 1


def test_an_operator_without_a_provider_customer_cannot_send(fake: FakeProvider) -> None:
    operator = an_operator(fake, customer=None)
    draft = a_draft(operator, make_lease(operator=operator), [line()])
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.NO_PROVIDER_CUSTOMER)
    assert not OneOffCharge.objects.exists()


def test_a_draft_of_operator_a_cannot_go_on_operator_b_lease(fake: FakeProvider) -> None:
    operator_a, _ = a_world(fake)
    operator_b = an_operator(fake, customer="cus-b", bank="pm-b")
    lease_b = make_lease(operator=operator_b)
    refused = invoice_builder.create_draft(
        operator_a, lease_b, "", None, [line()], user(Role.BILLING)
    )
    assert (refused.ok, refused.message) == (False, invoice_builder.LEASE_OTHER_OPERATOR)
    draft = a_draft(operator_a, None, [line()])
    InvoiceDraft.objects.filter(pk=draft.pk).update(lease=lease_b)
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (False, invoice_builder.LEASE_OTHER_OPERATOR)
    assert not OneOffCharge.objects.exists() and not Invoice.objects.exists()


def test_an_operator_level_draft_makes_operator_level_charges(fake: FakeProvider) -> None:
    operator, _ = a_world(fake)
    draft = a_draft(operator, None, two_lines())
    sent(draft)
    assert {(charge.lease_id, charge.operator_id) for charge in charges_of(draft)} == {
        (None, operator.pk)
    }


# --- Allocation across the lines (D6) -----------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("150.00", [(D("100.00"), "paid"), (D("50.00"), "paid")]),
        ("120.00", [(D("100.00"), "paid"), (D("20.00"), "partial")]),
        ("100.00", [(D("100.00"), "paid"), (D("0.00"), "owed")]),
        ("60.00", [(D("60.00"), "partial"), (D("0.00"), "owed")]),
    ],
)
def test_a_payment_pays_the_lines_in_order(
    fake: FakeProvider, worker: None, amount: str, expected: list[tuple[Decimal, str]]
) -> None:
    draft, invoice = sent_two(fake)
    pay(fake, invoice, amount)
    [payment] = Payment.objects.filter(status=PaymentStatus.SETTLED)
    first, second = charges_of(draft)
    assert payment.one_off_charge_id == first.pk and payment.amount == D(amount)
    assert paid_states(draft) == expected
    allocated = AuditLog.objects.filter(
        action=one_off_service.ALLOCATED_ACTION, entity_id=str(second.pk)
    )
    assert allocated.exists() == (expected[1][0] > 0)


def test_a_return_unwinds_from_the_last_line_first(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    pay(fake, invoice, "120.00")
    later = pay(fake, invoice, "30.00")
    assert paid_states(draft) == [(D("100.00"), "paid"), (D("50.00"), "paid")]
    ach_return(fake, later)
    deliver("update_transaction_settled", id=later)
    assert paid_states(draft) == [(D("100.00"), "paid"), (D("20.00"), "partial")]


def test_a_manual_payment_on_the_first_line_pays_the_invoice_in_order(
    fake: FakeProvider, worker: None
) -> None:
    draft, _ = sent_two(fake)
    first, second = charges_of(draft)
    later = one_off_service.record_payment(
        second.pk, method="wire", amount=D("10.00"), paid_on=TODAY, note="Wire.", actor=None
    )
    assert (later.ok, later.message) == (False, one_off_service.PAY_LINE)
    outcome = one_off_service.record_payment(
        first.pk, method="wire", amount=D("150.00"), paid_on=TODAY, note="Wire.", actor=None
    )
    assert outcome.ok and outcome.mark_due
    assert paid_states(draft) == [(D("100.00"), "paid"), (D("50.00"), "paid")]


# --- Cancel (D6) -------------------------------------------------------------------------


def test_cancel_before_sending_closes_the_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, two_lines())
    outcome = invoice_builder.cancel(draft.pk, user(Role.BILLING))
    assert (outcome.ok, fresh(draft).status) == (True, InvoiceDraftStatus.CANCELLED)
    again = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert (again.ok, again.message) == (False, invoice_builder.ALREADY_CANCELLED)
    assert not fake.calls and not OneOffCharge.objects.exists()


def test_cancel_after_sending_voids_every_line(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    first, second = charges_of(draft)
    assert one_off_service.cancel_refusal(second) == one_off_service.CANCEL_LINE
    assert invoice_builder.cancel(draft.pk, user(Role.BILLING)).message == (
        one_off_service.VOID_NOTE_REQUIRED
    )
    outcome = invoice_builder.cancel(draft.pk, user(Role.BILLING), note="Wrong store.")
    assert outcome.ok and outcome.job is not None and outcome.job.kind == JobKind.INVOICE_DELETE
    run_queued_jobs()
    assert calls(fake, "delete_invoice")
    assert [charge.status for charge in charges_of(draft)] == [
        OneOffChargeStatus.VOIDED,
        OneOffChargeStatus.VOIDED,
    ]
    assert fresh(invoice).internal_status == InvoiceStatus.VOID
    assert receivables.open_balances(TODAY, operator=fresh(draft).operator_id) == []


def test_cancel_after_a_payment_is_refused(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    pay(fake, invoice, "30.00")
    outcome = invoice_builder.cancel(draft.pk, user(Role.BILLING), note="Wrong store.")
    assert (outcome.ok, outcome.message) == (False, one_off_service.CANCEL_PAID)
    assert not Job.objects.filter(kind=JobKind.INVOICE_DELETE).exists()


# --- Charge now (D6, 10d D4) -------------------------------------------------------------


def test_charge_now_on_a_multi_line_invoice_is_one_attempt_with_the_stored_key(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    draft = a_draft(operator, lease, two_lines())
    details = invoice_builder.charge_now_details(draft, user(Role.ADMIN))
    assert (details.refusal, details.total, details.last_four) == ("", D("150.00"), "6789")
    outcome = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=False)
    assert outcome.ok, outcome.message
    first, second = charges_of(draft)
    assert {first.collection, second.collection} == {CollectionMethod.CHARGE_NOW}
    assert first.charge_now_key is not None and second.charge_now_key is None
    run_queued_jobs()
    assert len(created_invoices(fake)) == 1 and not calls(fake, "send_invoice_email")
    [(ref, request)] = calls(fake, "pay_invoice")
    assert isinstance(request, PayWithStoredMethod)
    assert request.idempotency_id == str(fresh(first).charge_now_key)
    assert ref == provider_ref(Invoice.objects.get())
    [payment] = Payment.objects.all()
    assert (payment.one_off_charge_id, payment.amount, payment.status) == (
        first.pk,
        D("150.00"),
        PaymentStatus.PENDING,
    )
    # The pay attempt is queued by the create, once the invoice is at the provider.
    assert sorted(Job.objects.values_list("kind", "status")) == [
        (JobKind.INVOICE_CREATE, JobStatus.DONE),
        (JobKind.INVOICE_PAY, JobStatus.DONE),
    ]


def test_charge_now_above_the_threshold_needs_the_confirmation(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    big = str(D(settings.LARGE_DEBIT_WARNING_AMOUNT))
    draft = a_draft(operator, lease, [line("Arrears", big), line("Fee", "1.00")])
    refused = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=False)
    assert (refused.ok, refused.field) == (False, "confirm")
    assert not OneOffCharge.objects.exists()
    assert invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=True).ok


def test_charge_now_is_refused_when_the_flag_is_off(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    draft = a_draft(operator, lease, [line()])
    outcome = invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=True)
    assert (outcome.ok, outcome.message) == (False, one_off_service.CHARGE_NOW_DISABLED)


# --- Roles (D9) --------------------------------------------------------------------------


def test_a_viewer_cannot_send_and_billing_cannot_charge_now(
    fake: FakeProvider, enabled: None
) -> None:
    operator, lease = a_world(fake)
    store_methods(operator)
    draft = a_draft(operator, lease, two_lines())
    viewer, viewer_user = client_for(Role.VIEWER)
    billing, billing_user = client_for(Role.BILLING)
    assert invoice_builder.send(draft.pk, viewer_user).message == invoice_builder.BILLING_ONLY
    assert invoice_builder.charge_now(draft.pk, billing_user, confirmed=True).message == (
        invoice_builder.CHARGE_NOW_ADMIN
    )
    base = f"/invoices/drafts/{draft.pk}"
    for path in (f"{base}/send/", f"{base}/edit/", f"{base}/cancel/", f"{base}/charge-now/"):
        assert viewer.post(path).status_code == 403, path
    assert viewer.get(f"{base}/").status_code == 403
    assert billing.get(f"{base}/charge-now/").status_code == 403
    assert billing.post(f"{base}/charge-now/", {"confirm": "true"}).status_code == 403
    page = billing.get(f"{base}/").content.decode()
    assert 'id="send-draft"' in page and 'id="draft-charge-now"' not in page
    admin, _ = client_for(Role.ADMIN)
    assert 'id="draft-charge-now"' in admin.get(f"{base}/").content.decode()
    assert not OneOffCharge.objects.exists() and fresh(draft).status == InvoiceDraftStatus.DRAFT


def test_the_preview_and_the_print_view_render_for_a_viewer(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Poster", "33.33", quantity="3")])
    viewer, _ = client_for(Role.VIEWER)
    preview = viewer.get(f"/invoices/drafts/{draft.pk}/preview/")
    printed = viewer.get(f"/invoices/drafts/{draft.pk}/print/")
    assert (preview.status_code, printed.status_code) == (200, 200)
    for text in (preview.content.decode(), printed.content.decode()):
        assert draft.number in text and settings.COMPANY_NAME in text
        assert operator.display_name in text and lease.store.external_label in text
        assert "Poster" in text and "$99.99" in text and MEMO in text
        assert (TODAY + timedelta(days=15)).isoformat() in text
    page = printed.content.decode()
    assert "<nav" not in page and 'id="save-as-pdf"' in page and "@media print" in page
    sent(draft)
    number = Invoice.objects.get().invoice_number
    assert number in viewer.get(f"/invoices/drafts/{draft.pk}/print/").content.decode()


# --- The pages (D6) ----------------------------------------------------------------------


def page_data(lines: list[dict[str, str]], **header: str) -> dict[str, str]:
    data = {
        "creation_key": str(uuid.uuid4()),
        "lease": "",
        "memo": "",
        "due_date": "",
        "lines-TOTAL_FORMS": str(len(lines)),
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": str(invoice_builder.MAX_LINES),
        **header,
    }
    for index, row in enumerate(lines):
        data.update({f"lines-{index}-{name}": value for name, value in row.items()})
    return data


def test_new_invoice_on_the_store_page_preselects_the_lease_and_saves_a_draft(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    client, _ = client_for(Role.BILLING)
    store_page = client.get(f"/stores/{lease.store_id}/").content.decode()
    assert 'id="new-invoice"' in store_page
    path = f"/invoices/new/store/{lease.store_id}/"
    blank = client.get(path).content.decode()
    assert f'<option value="{lease.pk}" selected>' in blank
    assert blank.count('class="draft-line"') == 5  # blank rows without script
    response = client.post(
        path,
        page_data(
            [
                {"description": "Sign repair", "unit_amount": "250.00", "quantity": ""},
                {"description": "", "unit_amount": ""},
                {"description": "Removed", "unit_amount": "1.00", "DELETE": "on"},
            ],
            lease=str(lease.pk),
            memo=MEMO,
        ),
    )
    draft = InvoiceDraft.objects.get()
    assert response["Location"] == f"/invoices/drafts/{draft.pk}/"
    assert (draft.operator_id, draft.lease_id, draft.memo) == (operator.pk, lease.pk, MEMO)
    assert [(row.description, row.line_total) for row in invoice_builder.lines_of(draft)] == [
        ("Sign repair", D("250.00"))
    ]
    page = client.get(response["Location"]).content.decode()
    assert draft.number in page and 'value="Sign repair"' in page


def test_a_store_without_a_lease_today_offers_no_new_invoice(fake: FakeProvider) -> None:
    store = Store.objects.create(external_label="Store 0999", status=StoreStatus.ACTIVE)
    client, _ = client_for(Role.BILLING)
    assert 'id="new-invoice"' not in client.get(f"/stores/{store.pk}/").content.decode()
    response = client.get(f"/invoices/new/store/{store.pk}/")
    assert response.status_code == 302 and response["Location"] == f"/stores/{store.pk}/"


def test_a_hand_over_day_offers_both_leases(fake: FakeProvider) -> None:
    outgoing, lease = a_world(fake)
    incoming = an_operator(fake, customer="cus-in", bank="pm-in")
    second = make_lease(operator=incoming, store=lease.store, started_on=TODAY)
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/invoices/new/store/{lease.store_id}/").content.decode()
    assert f'<option value="{second.pk}" selected>' in page
    assert f'<option value="{lease.pk}">' in page
    assert outgoing.display_name in page


def test_new_invoice_on_the_operator_page_may_be_operator_level(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    client, _ = client_for(Role.BILLING)
    assert 'id="new-invoice"' in client.get(f"/operators/{operator.pk}/").content.decode()
    path = f"/invoices/new/operator/{operator.pk}/"
    assert "No lease (the operator)" in client.get(path).content.decode()
    client.post(path, page_data([{"description": "Late fee", "unit_amount": "25.00"}]))
    draft = InvoiceDraft.objects.get()
    assert (draft.operator_id, draft.lease_id) == (operator.pk, None)
    listing = client.get(f"/operators/{operator.pk}/").content.decode()
    assert f"/invoices/drafts/{draft.pk}/" in listing


def test_a_refused_page_shows_the_sentence_and_keeps_no_card_number(fake: FakeProvider) -> None:
    operator, _ = a_world(fake)
    client, _ = client_for(Role.BILLING)
    path = f"/invoices/new/operator/{operator.pk}/"
    client.post(path, page_data([{"description": f"Card {CARD}", "unit_amount": "5.00"}]))
    page = client.get(path).content.decode()
    assert "card or account numbers" in page and CARD not in page
    client.post(path, page_data([{"description": "x" * 121, "unit_amount": "5.00"}]))
    assert (
        invoice_builder.LINE_DESCRIPTION_TOO_LONG.format(line=1)
        in client.get(path).content.decode()
    )
    assert not InvoiceDraft.objects.exists()


def test_the_draft_page_saves_and_sends_then_shows_the_invoice(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Old line", "10.00")])
    client, _ = client_for(Role.BILLING)
    base = f"/invoices/drafts/{draft.pk}"
    client.post(
        f"{base}/edit/",
        page_data(
            [
                {"description": "Old line", "unit_amount": "10.00", "DELETE": "on"},
                {"description": "Arrears September", "unit_amount": "100.00"},
                {"description": "Sign repair", "unit_amount": "50.00", "quantity": "1"},
            ],
            lease=str(lease.pk),
        ),
    )
    assert [row.description for row in invoice_builder.lines_of(fresh(draft))] == [
        "Arrears September",
        "Sign repair",
    ]
    client.post(f"{base}/send/")
    run_queued_jobs()
    invoice = Invoice.objects.get()
    page = client.get(f"{base}/").content.decode()
    assert invoice.invoice_number in page and 'id="send-draft"' not in page
    for charge in charges_of(draft):
        assert f"/one-off-charges/{charge.pk}/" in page
    shown = invoice.provider_status
    assert shown and f'<dd id="sent-provider-status">{shown}</dd>' in page


def test_the_draft_page_cancels_a_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()])
    client, _ = client_for(Role.BILLING)
    assert client.get(f"/invoices/drafts/{draft.pk}/cancel/").status_code == 200
    client.post(f"/invoices/drafts/{draft.pk}/cancel/")
    assert fresh(draft).status == InvoiceDraftStatus.CANCELLED


# --- The P&L and the open balances (D6) --------------------------------------------------


def test_the_pnl_counts_each_line_by_its_category(fake: FakeProvider, worker: None) -> None:
    operator, lease = a_world(fake)
    arrears = a_fee_type("arrears", "Arrears")
    damage = a_fee_type("damage", "Damage repair")
    draft = a_draft(
        operator,
        lease,
        [line("", "1000.00", fee_type=arrears), line("", "200.00", fee_type=damage)],
    )
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    pay(fake, fresh(invoice), "1100.00")
    selected = pnl.parse_query({"from": "2026-10", "to": "2026-10", "group": "store"})
    rows = [row for row in pnl.rows(selected) if row.store_id == lease.store_id]

    def total(name: str) -> Decimal:
        return sum((getattr(row, name) for row in rows), D("0"))

    assert (total("rent_billed"), total("fees_billed")) == (D("1000.00"), D("200.00"))
    assert (total("rent_collected"), total("fees_collected")) == (D("1000.00"), D("100.00"))


def test_the_open_balances_owe_each_line_once(fake: FakeProvider, worker: None) -> None:
    draft, invoice = sent_two(fake)
    operator_id = fresh(draft).operator_id
    rows = receivables.open_balances(TODAY, operator=operator_id)
    assert sorted(row.balance for row in rows) == [D("50.00"), D("100.00")]
    pay(fake, invoice, "120.00")
    [row] = receivables.open_balances(TODAY, operator=operator_id)
    assert (row.charge_id, row.balance) == (charges_of(draft)[1].pk, D("30.00"))


def test_a_later_line_is_paid_linked_and_charged_only_through_the_first(
    fake: FakeProvider, worker: None
) -> None:
    draft, invoice = sent_two(fake)
    first, second = charges_of(draft)
    linked = one_off_service.link_invoice(
        second.pk, invoice.pk, confirm=True, note="Same invoice.", actor=None
    )
    assert (linked.ok, linked.message) == (False, one_off_service.LINK_LINE)
    assert one_off_service.charge_now_refusal(second) in (
        one_off_service.CHARGE_NOW_DISABLED,
        one_off_service.CHARGE_NOW_NOT_ELIGIBLE,
    )
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/one-off-charges/{second.pk}/").content.decode()
    for hidden in ('id="record-payment"', 'id="link-invoice"', 'id="cancel-charge"'):
        assert hidden not in page, hidden
    first_page = client.get(f"/one-off-charges/{first.pk}/").content.decode()
    assert 'id="record-payment"' in first_page and 'id="edit-charge"' not in first_page


# --- Review findings of Batch B ------------------------------------------------------------


def _with_deposit_second(fake: FakeProvider) -> tuple[Operator, InvoiceDraft, Invoice]:
    """A two-line draft: a 100.00 repair, then the 50.00 security deposit, sent."""
    operator, lease = a_world(fake)
    deposit = FeeType.objects.get(code="security_deposit")
    lines = [line("Sign repair", "100.00"), line("", "50.00", fee_type=deposit)]
    draft = a_draft(operator, lease, lines)
    invoice = sent(draft).invoice
    assert invoice is not None
    run_queued_jobs()
    return operator, draft, fresh(invoice)


@pytest.mark.parametrize(
    ("amount", "held"),
    [("150.00", "50.00"), ("120.00", "20.00"), ("90.00", "0.00")],
    ids=["in-full", "in-part", "first-line-only"],
)
def test_a_deposit_on_the_second_line_is_held_by_what_it_was_allocated(
    fake: FakeProvider, worker: None, amount: str, held: str
) -> None:
    operator, draft, invoice = _with_deposit_second(fake)
    pay(fake, invoice, amount)
    _, second = charges_of(draft)
    assert second.amount_paid == D(held)
    collected = D(amount) - D(held)
    october = {"from": "2026-10", "to": "2026-10"}
    assert pnl.deposits_held(pnl.parse_query(october)) == D(held)
    ledger = payments_ledger.totals(
        payments_ledger.ledger_payments(payments_ledger.parse_filter({}))
    )
    assert (ledger.deposits_held, ledger.settled) == (D(held), collected)
    report = reports.statement(operator, reports.parse_range(october, TODAY), TODAY)
    assert (report.deposits_held, report.paid) == (D(held), collected)
    rows = pnl.rows(pnl.parse_query({**october, "group": "store"}))
    assert sum((row.fees_collected for row in rows), D("0")) == collected


def test_a_refund_on_a_deposit_line_takes_back_from_the_deposit_first(
    fake: FakeProvider, worker: None
) -> None:
    """A refund is taken back from the last line first: here the deposit."""
    operator, draft, invoice = _with_deposit_second(fake)
    txn = pay(fake, invoice, "150.00")
    paid = Payment.objects.get(status=PaymentStatus.SETTLED, is_refund=False)
    refund = Payment.objects.create(
        kind=paid.kind,
        one_off_charge=paid.one_off_charge,
        amount=D("30.00"),
        method=paid.method,
        status=PaymentStatus.SETTLED,
        is_refund=True,
        parent_payment=paid,
        invoice=paid.invoice,
        initiated_on=paid.settled_on,
        settled_on=paid.settled_on,
    )
    # Recorded after the payment (the clock is pinned in this module).
    Payment.objects.filter(pk=refund.pk).update(created_at=NOW + timedelta(minutes=1))
    assert txn
    october = {"from": "2026-10", "to": "2026-10"}
    assert pnl.deposits_held(pnl.parse_query(october)) == D("20.00")
    ledger = payments_ledger.totals(
        payments_ledger.ledger_payments(payments_ledger.parse_filter({}))
    )
    assert (ledger.deposits_held, ledger.settled, ledger.refunded) == (
        D("20.00"),
        D("100.00"),
        D("0.00"),
    )
    report = reports.statement(operator, reports.parse_range(october, TODAY), TODAY)
    assert (report.deposits_held, report.paid, report.refunded) == (
        D("20.00"),
        D("100.00"),
        D("0.00"),
    )


def test_a_rescheduled_return_on_a_two_line_invoice_is_owed_once(
    fake: FakeProvider, worker: None
) -> None:
    """Paid, returned, rescheduled: the re-debit carries the whole payment, so the first
    line's balance and then the later line's are carried, in line order."""
    draft, invoice = sent_two(fake)
    operator_id = fresh(draft).operator_id
    txn = pay(fake, invoice, "150.00")
    assert receivables.open_balances(TODAY, operator=operator_id) == []
    ach_return(fake, txn)
    deliver("update_transaction_settled", id=txn)
    before = receivables.open_balances(TODAY, operator=operator_id)
    assert sorted(row.balance for row in before) == [D("50.00"), D("100.00")]
    returned = Payment.objects.get(status=PaymentStatus.RETURNED)
    outcome = nsf.reschedule(
        returned.pk,
        redebit_date=TODAY + timedelta(days=3),
        waive_fee=False,
        note="Owner agreed by phone.",
        actor=user(Role.BILLING),
        today=TODAY,
    )
    assert outcome.ok, outcome.message
    after = receivables.open_balances(TODAY, operator=operator_id)
    redebit = OneOffCharge.objects.get(category=OneOffCategory.REDEBIT)
    fee = OneOffCharge.objects.get(companion_of=redebit)
    assert {row.charge_id: row.balance for row in after} == {
        redebit.pk: D("150.00"),
        fee.pk: Decimal(fee.price),
    }
    assert receivables.total(after) - Decimal(fee.price) == receivables.total(before)
