"""Webhook handlers, the payment state machine, the returns queue and registration
(docs/qa/PLAN-008-webhooks.md Batch B; §6 rows 1 (processing), 3, 4, 5, 6, 8, 10).

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider` whose records the
tests write directly, standing in for the provider's answers to the re-fetch. Every
event goes through the real receiver storage and processor: a handler sees only the
event type and the object ID. No test reaches the network; the adapter tests use an
``httpx.MockTransport``.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db.models import Model
from django.test import override_settings

from accounts import registry
from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    InsuranceCharge,
    Invoice,
    Lease,
    Operator,
    Payment,
    PaymentKind,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
    Store,
    StoreStatus,
)
from ledger.services import rent_run, returns
from payments.errors import ProviderRejected, ProviderUnavailable, ValidationFailed
from payments.fake import FakeProvider
from payments.models import Deposit, ExternalRef, ProviderSnapshot, WebhookEvent
from payments.provider import (
    CustomerRecord,
    DepositRecord,
    InvoiceRecord,
    TransactionRecord,
    WebhookRecord,
)
from payments.stax import mapping, validation
from payments.stax.adapter import StaxProvider
from payments.webhooks import events as webhook_events
from payments.webhooks import handlers, processor, receiver, registration
from payments.webhooks.events import EVENT_NAMES, ignored
from payments.webhooks.handlers import transaction as transaction_handler
from tests.auth_helpers import iter_routes
from tests.stax_helpers import always, make_rig
from tests.test_lease_services import make_lease, make_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import charge_of, enrolled, health  # noqa: F401 (autouse fixture)
from tests.test_rent_run import invoice as schedule_invoice

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SETTLED_AT = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
PERIOD = date(2026, 10, 1)
RENT = D("1500.00")
CUSTOMER = "cus-1"
OTHER_CUSTOMER = "cus-2"
INVOICE = "inv-0001"
TXN = "txn-0001"
TOKEN = "w" * 48
HANDLERS_DIR = Path(__file__).resolve().parents[1] / "payments" / "webhooks" / "handlers"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


# --- Delivering events ------------------------------------------------------------------

_deliveries = iter(range(1, 10**9))


def deliver(event_type: str, body: dict[str, Any] | None = None, **fields: Any) -> str:
    """Store one distinct delivery (as the receiver does) and process it; its outcome."""
    payload = {**(body or {}), **fields, "delivery": next(_deliveries)}
    event_id = receiver.store_event(json.dumps(payload).encode(), event_type)
    assert event_id is not None
    outcome = processor.process_event(event_id)
    assert outcome is not None
    return outcome


def replay_all() -> None:
    for event_id in WebhookEvent.objects.order_by("received_at").values_list("pk", flat=True):
        processor.process_event(event_id, replay=True)


def state() -> dict[str, list[dict[str, Any]]]:
    """Every row a handler may write, without the times that ``auto_now`` moves."""

    def rows(model: type[Model], *exclude: str) -> list[dict[str, Any]]:
        names = [
            field.attname
            for field in model._meta.concrete_fields
            if field.attname not in {"updated_at", *exclude}
        ]
        return list(model.objects.order_by("pk").values(*names))  # type: ignore[attr-defined]

    return {
        "payments": rows(Payment),
        "charges": rows(RentCharge),
        "insurance": rows(InsuranceCharge),
        "invoices": rows(Invoice),
        "operators": rows(Operator),
        "schedules": rows(CollectionSchedule),
        "refs": rows(ExternalRef),
        "snapshots": rows(ProviderSnapshot),
        "deposits": rows(Deposit),
        "audit": rows(AuditLog),
    }


# --- The world: an operator, a lease, a charge and its invoice --------------------------


@dataclass
class World:
    operator: Operator
    lease: Lease
    charge: RentCharge
    invoice: Invoice


def make_world(fake: FakeProvider, *, insurance: bool = False) -> World:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    lease = make_lease(operator=operator)
    charge = RentCharge.objects.create(
        lease=lease,
        period_month=PERIOD,
        seq=1,
        due_on=date(2026, 10, 15),
        amount_due=RENT,
        status=RentChargeStatus.DUE,
    )
    insurance_charge = (
        InsuranceCharge.objects.create(
            lease=lease, policy_year=2026, due_on=date(2026, 10, 15), amount=D("400.00")
        )
        if insurance
        else None
    )
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-0001",
        operator=operator,
        rent_charge=None if insurance else charge,
        insurance_charge=insurance_charge,
        issue_date=date(2026, 10, 15),
        due_date=date(2026, 10, 15),
        subtotal=D("400.00") if insurance else RENT,
        total=D("400.00") if insurance else RENT,
    )
    ExternalRef.objects.create(
        entity_type="invoice",
        entity_id=invoice.pk,
        provider="stax_pay",
        external_kind="invoice",
        external_id=INVOICE,
    )
    provider_invoice(fake, total=invoice.total)
    return World(operator, lease, charge, invoice)


def provider_invoice(
    fake: FakeProvider,
    external_id: str = INVOICE,
    *,
    status: str = "SENT",
    total: Decimal = RENT,
    customer: str = CUSTOMER,
    **fields: Any,
) -> InvoiceRecord:
    record = InvoiceRecord(
        external_id=external_id,
        raw_status=status,
        fetched_at=NOW,
        customer_external_id=customer,
        total=total,
        raw_summary=fields.pop("raw_summary", {"invoice_date_at": "2026-10-15"}),
        **fields,
    )
    fake.invoices[external_id] = record
    return record


def provider_txn(
    fake: FakeProvider,
    external_id: str = TXN,
    *,
    total: Decimal = RENT,
    **fields: Any,
) -> TransactionRecord:
    values: dict[str, Any] = {
        "method": "bank",
        "kind": "charge",
        "invoice_external_id": INVOICE,
        "customer_external_id": CUSTOMER,
        "success": True,
        "created_at": datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
    }
    record = TransactionRecord(
        external_id=external_id,
        raw_status="",
        fetched_at=NOW,
        total=total,
        **{**values, **fields},
    )
    fake.transactions[external_id] = record
    return record


def settle(fake: FakeProvider, external_id: str = TXN) -> None:
    fake.transactions[external_id] = replace(fake.transactions[external_id], settled_at=SETTLED_AT)


def ach_return(fake: FakeProvider, external_id: str = TXN) -> None:
    fake.transactions[external_id] = replace(
        fake.transactions[external_id],
        returned=True,
        return_code="R01",
        return_reason="Insufficient Funds",
    )


def payment_for(external_id: str = TXN) -> Payment:
    ref = ExternalRef.objects.get(external_kind="transaction", external_id=external_id)
    return Payment.objects.get(pk=ref.entity_id)


def fetches(fake: FakeProvider, name: str) -> list[object]:
    return [args[0] for method, args in fake.calls if method == name]


# --- Row 3: the body is never trusted --------------------------------------------------


def test_the_lying_payload_body_says_paid_the_refetch_says_sent(fake: FakeProvider) -> None:
    world = make_world(fake)
    body = {
        "id": INVOICE,
        "status": "PAID",
        "total": "1500.00",
        "total_paid": "1500.00",
        "paid_at": "2026-10-15 12:00:00",
        "invoice_date_at": "2026-10-15",
    }
    assert deliver("update_invoice", body) == "processed"
    world.invoice.refresh_from_db()
    world.charge.refresh_from_db()
    assert world.invoice.provider_status == "SENT"
    assert world.invoice.paid_at is None
    assert world.invoice.amount_paid == D("0.00")
    assert (world.charge.status, world.charge.amount_paid) == (RentChargeStatus.DUE, D("0.00"))
    assert not Payment.objects.exists()
    assert fetches(fake, "get_invoice") == [INVOICE]


def test_the_lying_payload_of_a_transaction_says_settled_the_refetch_says_pending(
    fake: FakeProvider,
) -> None:
    world = make_world(fake)
    provider_txn(fake)
    body = {"id": TXN, "method": "bank", "total": "1500.00", "settled_at": "2026-10-16"}
    assert deliver("update_transaction_settled", body) == ignored("not settled yet")
    world.charge.refresh_from_db()
    assert payment_for().status == PaymentStatus.PENDING
    assert world.charge.status == RentChargeStatus.PENDING


def test_a_handler_takes_only_the_event_type_and_the_object_id() -> None:
    for handler in set(handlers.HANDLERS.values()):
        assert list(inspect.signature(handler).parameters) == [
            "event_type",
            "provider_object_id",
        ], handler


def test_no_handler_module_reads_a_stored_event_or_its_body() -> None:
    files = sorted(HANDLERS_DIR.glob("*.py"))
    assert len(files) >= 7
    for path in files:
        tree = ast.parse(path.read_text())
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom | ast.Import)
            for alias in node.names
        }
        assert "WebhookEvent" not in names | imported, path
        assert "payload" not in attributes, path
        assert not {"json", "receiver", "parse_body"} & imported, path


# --- Row 5: the payment state machine ---------------------------------------------------


def test_created_is_pending_and_the_charge_is_not_paid(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)  # even a settled time does not pay on create
    assert deliver("create_transaction", id=TXN) == "processed"
    payment = payment_for()
    world.charge.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING
    assert (payment.kind, payment.method, payment.amount) == ("rent", "ach", RENT)
    assert payment.invoice_id == world.invoice.pk
    assert payment.rent_charge_id == world.charge.pk
    assert payment.settled_on is None
    assert (world.charge.status, world.charge.amount_paid) == (RentChargeStatus.PENDING, D("0"))
    assert fetches(fake, "get_transaction") == [TXN]
    assert fetches(fake, "get_invoice") == [INVOICE]


def test_settled_pays_the_charge_with_amount_and_status(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake)
    deliver("create_transaction", id=TXN)
    settle(fake)
    assert deliver("update_transaction_settled", id=TXN) == "processed"
    payment = payment_for()
    world.charge.refresh_from_db()
    assert payment.status == PaymentStatus.SETTLED
    assert payment.settled_on == date(2026, 10, 18)
    assert (world.charge.status, world.charge.amount_paid) == (RentChargeStatus.PAID, RENT)
    assert world.charge.balance == D("0.00")
    actions = list(AuditLog.objects.order_by("id").values_list("action", flat=True))
    assert "payment.status_changed" in actions
    assert "rent_charge.payment_applied" in actions


def test_a_partial_settlement_leaves_the_charge_partial(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake, total=D("500.00"), settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    world.charge.refresh_from_db()
    assert (world.charge.status, world.charge.amount_paid) == (
        RentChargeStatus.PARTIAL,
        D("500.00"),
    )


def test_update_transaction_never_settles(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction", id=TXN)
    deliver("transaction", id=TXN)
    world.charge.refresh_from_db()
    assert payment_for().status == PaymentStatus.PENDING
    assert world.charge.status == RentChargeStatus.PENDING


def test_a_return_reopens_the_charge_and_enters_the_queue(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake)
    deliver("create_transaction", id=TXN)
    ach_return(fake)
    assert deliver("update_transaction_settled", id=TXN) == "processed"
    payment = payment_for()
    world.charge.refresh_from_db()
    assert payment.status == PaymentStatus.RETURNED
    assert (payment.return_code, payment.return_reason) == ("R01", "Insufficient Funds")
    assert payment.returned_at is not None
    assert (world.charge.status, world.charge.amount_paid) == (RentChargeStatus.UNPAID, D("0"))
    assert world.charge.needs_review
    assert list(returns.open_returns()) == [payment]
    audit = AuditLog.objects.get(action="rent_charge.ach_returned")
    assert audit.entity_id == str(world.charge.pk)
    assert audit.after is not None and audit.after["return_code"] == "R01"


def test_a_return_after_settlement_takes_the_payment_back(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    world.charge.refresh_from_db()
    assert world.charge.status == RentChargeStatus.PAID
    ach_return(fake)
    deliver("update_transaction_settled", id=TXN)
    world.charge.refresh_from_db()
    assert payment_for().status == PaymentStatus.RETURNED
    assert (world.charge.status, world.charge.amount_paid) == (RentChargeStatus.UNPAID, D("0"))


def test_a_return_with_another_settled_payment_leaves_the_charge_partial(
    fake: FakeProvider,
) -> None:
    world = make_world(fake)
    provider_txn(fake, "txn-a", total=D("500.00"), settled_at=SETTLED_AT)
    provider_txn(fake, "txn-b", total=D("1000.00"))
    deliver("update_transaction_settled", id="txn-a")
    deliver("create_transaction", id="txn-b")
    ach_return(fake, "txn-b")
    deliver("update_transaction_settled", id="txn-b")
    world.charge.refresh_from_db()
    assert (world.charge.status, world.charge.amount_paid) == (
        RentChargeStatus.PARTIAL,
        D("500.00"),
    )


def test_a_settled_event_with_neither_settlement_nor_return_changes_nothing(
    fake: FakeProvider,
) -> None:
    world = make_world(fake)
    provider_txn(fake)
    deliver("create_transaction", id=TXN)
    assert deliver("update_transaction_settled", id=TXN) == ignored("not settled yet")
    world.charge.refresh_from_db()
    assert payment_for().status == PaymentStatus.PENDING
    assert world.charge.status == RentChargeStatus.PENDING


def test_voided(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake)
    deliver("create_transaction", id=TXN)
    fake.transactions[TXN] = replace(fake.transactions[TXN], is_voided=True)
    assert deliver("update_transaction", id=TXN) == "processed"
    world.charge.refresh_from_db()
    assert payment_for().status == PaymentStatus.VOIDED
    assert (world.charge.status, world.charge.amount_paid) == (RentChargeStatus.UNPAID, D("0"))
    settle(fake)
    deliver("update_transaction_settled", id=TXN)
    assert payment_for().status == PaymentStatus.VOIDED  # final


def test_insurance_kind_and_policy_year(fake: FakeProvider) -> None:
    world = make_world(fake, insurance=True)
    provider_txn(fake, total=D("400.00"))
    deliver("create_transaction", id=TXN)
    payment = payment_for()
    assert (payment.kind, payment.policy_year) == (PaymentKind.INSURANCE, 2026)
    assert payment.insurance_charge_id is not None and payment.rent_charge_id is None
    ach_return(fake)
    deliver("update_transaction_settled", id=TXN)
    insurance = InsuranceCharge.objects.get()
    assert insurance.needs_review
    assert AuditLog.objects.filter(action="insurance_charge.ach_returned").count() == 1
    world.charge.refresh_from_db()
    assert world.charge.status == RentChargeStatus.DUE  # the rent charge is untouched


def test_a_settled_event_before_the_created_event(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    deliver("create_transaction", id=TXN)
    world.charge.refresh_from_db()
    assert Payment.objects.count() == 1
    assert payment_for().status == PaymentStatus.SETTLED
    assert world.charge.status == RentChargeStatus.PAID


def test_the_same_settled_event_twice(fake: FakeProvider) -> None:
    make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    before = state()
    deliver("update_transaction_settled", id=TXN)
    assert state() == before


def test_a_failed_transaction_is_not_recorded(fake: FakeProvider) -> None:
    make_world(fake)
    provider_txn(fake, success=False)
    assert deliver("create_transaction", id=TXN) == ignored("unsuccessful")
    assert not Payment.objects.exists()


def test_a_transaction_without_an_invoice_is_recorded_unmatched(fake: FakeProvider) -> None:
    make_world(fake)
    provider_txn(fake, invoice_external_id="")
    deliver("create_transaction", id=TXN)
    payment = payment_for()
    assert (payment.invoice_id, payment.rent_charge_id, payment.kind) == (None, None, "rent")
    assert AuditLog.objects.filter(action="payment.created_from_provider").count() == 1


def test_a_transaction_of_another_customer_is_not_linked(fake: FakeProvider) -> None:
    make_world(fake)
    make_operator(email="other@example.com", customer=OTHER_CUSTOMER)
    provider_txn(fake, customer_external_id=OTHER_CUSTOMER)
    assert deliver("create_transaction", id=TXN) == ignored("customer mismatch")
    assert not Payment.objects.exists()


# --- Row 4: refunds ------------------------------------------------------------------


def test_a_refund_is_a_new_payment_linked_to_its_parent(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    provider_invoice(fake, status="PARTIALLY_REFUNDED", amount_paid=D("1300.00"))
    provider_txn(
        fake,
        "txn-refund",
        total=D("200.00"),
        kind="refund",
        parent_external_id=TXN,
        invoice_external_id="",
    )
    calls_before = len(fetches(fake, "get_invoice"))
    assert deliver("create_transaction", id="txn-refund") == "processed"
    refund = payment_for("txn-refund")
    parent = payment_for()
    assert refund.is_refund and refund.parent_payment_id == parent.pk
    assert (refund.amount, refund.status, refund.rent_charge_id) == (
        D("200.00"),
        PaymentStatus.PENDING,
        world.charge.pk,
    )
    assert refund.invoice_id == world.invoice.pk
    assert len(fetches(fake, "get_invoice")) == calls_before + 1  # parent invoice re-fetched
    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == "PARTIALLY_REFUNDED"
    assert world.invoice.amount_paid == D("1300.00")
    settle(fake, "txn-refund")
    deliver("update_transaction_settled", id="txn-refund")
    world.charge.refresh_from_db()
    assert (world.charge.status, world.charge.amount_paid) == (
        RentChargeStatus.PARTIAL,
        D("1300.00"),
    )


def test_a_refund_whose_parent_we_never_saw_records_the_parent_first(
    fake: FakeProvider,
) -> None:
    make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    provider_txn(fake, "txn-refund", total=D("200.00"), kind="refund", parent_external_id=TXN)
    deliver("create_transaction", id="txn-refund")
    assert payment_for("txn-refund").parent_payment_id == payment_for().pk
    assert payment_for().status == PaymentStatus.PENDING  # created, never settled by a refund


def test_a_refund_with_an_unknown_parent_is_ignored(fake: FakeProvider) -> None:
    make_world(fake)
    provider_txn(fake, "txn-refund", kind="refund", parent_external_id="txn-missing")
    assert deliver("create_transaction", id="txn-refund") == ignored("refund parent unknown")
    assert not Payment.objects.exists()


# --- Row 6: card transactions and other methods -------------------------------------


def test_a_card_transaction_is_ignored(fake: FakeProvider) -> None:
    make_world(fake)
    provider_txn(fake, method="card", settled_at=SETTLED_AT)
    for name in ("create_transaction", "update_transaction_settled"):
        assert deliver(name, id=TXN) == ignored("card")
    assert not Payment.objects.exists()
    assert fetches(fake, "get_invoice") == []


def test_a_transaction_without_a_known_method_is_ignored(fake: FakeProvider) -> None:
    make_world(fake)
    provider_txn(fake, method="")
    assert deliver("create_transaction", id=TXN) == ignored("not ACH")


def test_an_event_without_an_object_id_makes_no_call(fake: FakeProvider) -> None:
    for name in EVENT_NAMES:
        if name == "update_payment_method":
            continue
        assert deliver(name, {"note": "no id"}) == ignored("no object id"), name
    assert fake.calls == []


def test_a_payment_method_event_is_stored_and_not_applied(fake: FakeProvider) -> None:
    assert deliver("update_payment_method", id="pm-1") == ignored("not applied")
    assert fake.calls == []


def test_an_object_the_provider_no_longer_has_is_ignored(fake: FakeProvider) -> None:
    assert deliver("update_invoice", id="inv-gone") == ignored("not at the provider")
    assert deliver("create_transaction", id="txn-gone") == ignored("not at the provider")


def test_a_provider_failure_during_a_refetch_fails_the_attempt(fake: FakeProvider) -> None:
    make_world(fake)
    fake.fail_next("get_invoice", ProviderUnavailable(503))
    assert deliver("update_invoice", id=INVOICE) == "failed"
    event = WebhookEvent.objects.get()
    assert event.error == "ProviderUnavailable"


def test_a_deposit_event_is_identified_by_its_batch_id() -> None:
    assert receiver.object_id({"batch_id": "batch-0001", "batched_at": "2026-10-01"}) == (
        "batch-0001"
    )
    assert receiver.object_id({"id": "a-1", "batch_id": "b-1"}) == "a-1"


# --- Invoices --------------------------------------------------------------------------


def test_an_invoice_update_mirrors_status_times_and_amount_paid(fake: FakeProvider) -> None:
    world = make_world(fake)
    sent = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    provider_invoice(fake, status="PAID", sent_at=sent, paid_at=SETTLED_AT, amount_paid=RENT)
    assert deliver("update_invoice", id=INVOICE) == "processed"
    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == "PAID"
    assert (world.invoice.sent_at, world.invoice.paid_at) == (sent, SETTLED_AT)
    assert world.invoice.viewed_at is None
    assert world.invoice.amount_paid == RENT
    world.charge.refresh_from_db()
    assert world.charge.status == RentChargeStatus.DUE  # a charge is paid only on settled
    audit = AuditLog.objects.get(action="invoice.provider_state_changed")
    assert audit.before is not None and audit.before["provider_status"] == ""
    assert audit.after is not None and audit.after["provider_status"] == "PAID"


def test_an_invoice_total_that_differs_flags_the_charge(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_invoice(fake, total=D("1499.00"))
    deliver("update_invoice", id=INVOICE)
    world.charge.refresh_from_db()
    world.invoice.refresh_from_db()
    assert world.charge.needs_review
    assert world.charge.amount_due == RENT and world.invoice.total == RENT
    assert AuditLog.objects.filter(action="rent_charge.total_mismatch").count() == 1


def test_an_unknown_invoice_of_a_known_customer_is_created_unlinked(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    provider_invoice(
        fake,
        "inv-new",
        status="DRAFT",
        total=D("75.00"),
        raw_summary={"invoice_date_at": "2026-10-20", "due_at": "2026-10-30"},
    )
    # PLAN-008a §2 rule 3: no open charge fits, so it stays unlinked with the count.
    assert deliver("create_invoice", id="inv-new") == "processed: unlinked (0 candidates)"
    invoice = Invoice.objects.get()
    assert (invoice.operator_id, invoice.rent_charge_id) == (operator.pk, None)
    assert (invoice.issue_date, invoice.due_date) == (date(2026, 10, 20), date(2026, 10, 30))
    assert invoice.total == D("75.00") and invoice.provider_status == "DRAFT"
    assert invoice.invoice_number == "INV-2026-0001"
    assert ExternalRef.objects.filter(external_kind="invoice", external_id="inv-new").exists()


def test_an_invoice_of_an_unknown_customer_is_ignored(fake: FakeProvider) -> None:
    provider_invoice(fake, "inv-new", customer="cus-unknown")
    assert deliver("create_invoice", id="inv-new") == ignored("unknown customer")
    assert not Invoice.objects.exists()


def test_a_schedule_invoice_is_linked_to_its_charge_by_the_rent_run_matching(
    fake: FakeProvider,
) -> None:
    row, refs = enrolled(fake)
    rent_run.create_charges(date(2026, 10, 15), actor=None)
    record = schedule_invoice(fake, refs[0], status="SENT")
    fake.invoices[record.external_id] = replace(record, customer_external_id=_customer(row))
    assert deliver("create_invoice", id=record.external_id) == "processed"
    invoice = Invoice.objects.get()
    assert invoice.rent_charge_id == charge_of(row).pk
    assert invoice.provider_status == "SENT"
    assert AuditLog.objects.filter(action="invoice.matched").count() == 1


def test_a_schedule_invoice_before_its_charge_is_left_for_the_rent_run(
    fake: FakeProvider,
) -> None:
    row, refs = enrolled(fake)
    record = schedule_invoice(fake, refs[0])
    fake.invoices[record.external_id] = replace(record, customer_external_id=_customer(row))
    assert deliver("create_invoice", id=record.external_id) == ignored("not matched")
    assert not Invoice.objects.exists()
    rent_run.create_charges(date(2026, 10, 15), actor=None)
    report = rent_run.match_schedule_invoices(actor=None)
    assert report.count(rent_run.MatchAction.LINKED) == 1


def test_a_schedule_invoice_naming_another_operators_customer_is_not_linked(
    fake: FakeProvider,
) -> None:
    row, refs = enrolled(fake)
    rent_run.create_charges(date(2026, 10, 15), actor=None)
    make_operator(email="other@example.com", customer=OTHER_CUSTOMER)
    record = schedule_invoice(fake, refs[0])
    fake.invoices[record.external_id] = replace(record, customer_external_id=OTHER_CUSTOMER)
    assert deliver("create_invoice", id=record.external_id) == ignored("not matched")
    assert not Invoice.objects.exists()


# --- PLAN-008a row 2: send_invoice, and the sent, viewed and paid times -------------------


def _last_event() -> WebhookEvent:
    return WebhookEvent.objects.order_by("-received_at", "-pk").first()  # type: ignore[return-value]


def envelope(invoice_id: str = INVOICE, **invoice: Any) -> dict[str, Any]:
    """A ``send_invoice`` body: an email envelope with the invoice under mergeVars."""
    return {
        "toEmail": "ops@example.com",
        "toName": "Operator",
        "mergeVars": {"user": {"name": "Staff"}, "invoice": {"id": invoice_id, **invoice}},
    }


def test_send_invoice_refetches_and_sets_sent_at_from_the_event_time(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_invoice(fake, status="SENT")
    assert deliver("send_invoice", envelope()) == "processed"
    event = _last_event()
    assert event.provider_object_id == INVOICE
    assert event.object_id_source == "mergeVars.invoice.id"
    assert fetches(fake, "get_invoice") == [INVOICE]
    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == "SENT"
    assert world.invoice.sent_at == event.received_at
    assert (world.invoice.viewed_at, world.invoice.paid_at) == (None, None)
    audit = AuditLog.objects.get(action="invoice.provider_state_changed")
    assert audit.after is not None and audit.after["sent_at"] == event.received_at.isoformat()


def test_a_second_send_event_does_not_move_sent_at(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_invoice(fake, status="SENT")
    deliver("send_invoice", envelope())
    first = _last_event().received_at
    WebhookEvent.objects.update(received_at=first - timedelta(hours=1))  # earlier than any next
    world.invoice.refresh_from_db()
    sent = world.invoice.sent_at
    assert sent == first
    assert deliver("send_invoice", envelope()) == "processed"
    world.invoice.refresh_from_db()
    assert world.invoice.sent_at == sent
    replay_all()
    world.invoice.refresh_from_db()
    assert world.invoice.sent_at == sent


@pytest.mark.parametrize("status", ["VIEWED", "PAID", " sent "])
def test_send_invoice_sets_sent_at_when_the_refetch_shows_it_sent_viewed_or_paid(
    fake: FakeProvider, status: str
) -> None:
    world = make_world(fake)
    provider_invoice(fake, status=status)
    deliver("send_invoice", envelope())
    world.invoice.refresh_from_db()
    assert world.invoice.sent_at == _last_event().received_at


@pytest.mark.parametrize("status", ["DRAFT", "", "VOIDED"])
def test_send_invoice_leaves_sent_at_empty_when_the_refetch_does_not_show_it_sent(
    fake: FakeProvider, status: str
) -> None:
    world = make_world(fake)
    provider_invoice(fake, status=status)
    assert deliver("send_invoice", envelope()) == "processed"
    world.invoice.refresh_from_db()
    assert world.invoice.sent_at is None


def test_the_send_body_status_is_never_read(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_invoice(fake, status="DRAFT")
    body = envelope(status="SENT", sent_at="2026-10-01 00:00:00", total="1.00")
    body["status"] = "PAID"
    deliver("send_invoice", body)
    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == "DRAFT"
    assert (world.invoice.sent_at, world.invoice.paid_at) == (None, None)


def test_a_time_the_provider_reports_is_kept_and_not_derived(fake: FakeProvider) -> None:
    world = make_world(fake)
    sent = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    provider_invoice(fake, status="SENT", sent_at=sent)
    deliver("send_invoice", envelope())
    world.invoice.refresh_from_db()
    assert world.invoice.sent_at == sent


def test_send_invoice_without_an_object_id_makes_no_call(fake: FakeProvider) -> None:
    make_world(fake)
    body = envelope()
    body["mergeVars"]["invoice"]["id"] = "../etc"
    assert deliver("send_invoice", body) == ignored("no object id")
    assert fetches(fake, "get_invoice") == []


def test_viewed_at_comes_from_the_first_update_whose_refetch_shows_viewed(
    fake: FakeProvider,
) -> None:
    world = make_world(fake)
    provider_invoice(fake, status="SENT")
    deliver("update_invoice", id=INVOICE)
    world.invoice.refresh_from_db()
    assert world.invoice.viewed_at is None and world.invoice.sent_at is None
    provider_invoice(fake, status="VIEWED")
    deliver("update_invoice", id=INVOICE)
    viewed = _last_event().received_at
    WebhookEvent.objects.update(received_at=viewed - timedelta(hours=1))
    deliver("update_invoice", id=INVOICE)
    world.invoice.refresh_from_db()
    assert world.invoice.viewed_at == viewed
    assert world.invoice.sent_at is None  # only a send event sets the sent time


def test_paid_at_comes_from_the_first_refetch_showing_paid(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_invoice(fake, status="PAID")
    deliver("update_invoice", id=INVOICE)
    paid = _last_event().received_at
    WebhookEvent.objects.update(received_at=paid - timedelta(hours=1))
    deliver("send_invoice", envelope())
    world.invoice.refresh_from_db()
    assert world.invoice.paid_at == paid
    assert world.invoice.viewed_at is None


def test_the_receipt_time_is_known_only_while_an_event_is_processed(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def spy(event_type: str, provider_object_id: str) -> str:
        seen.append(webhook_events.received_at())
        return "processed"

    monkeypatch.setitem(handlers.HANDLERS, "send_invoice", spy)
    deliver("send_invoice", envelope())
    assert seen == [_last_event().received_at]
    assert webhook_events.received_at() is None


# --- PLAN-008a row 3: auto-link on create_invoice ------------------------------------------

DASHBOARD = "inv-dash-0001"


def open_charge(
    operator: Operator,
    label: str = "Store 0101",
    *,
    amount: Decimal = RENT,
    period: date = PERIOD,
    status: str = RentChargeStatus.DUE,
) -> RentCharge:
    store = Store.objects.create(external_label=label, status=StoreStatus.ACTIVE)
    lease = make_lease(operator=operator, store=store)
    return RentCharge.objects.create(
        lease=lease,
        period_month=period,
        seq=1,
        due_on=period.replace(day=15),
        amount_due=amount,
        status=status,
    )


def dashboard_invoice(
    fake: FakeProvider,
    *,
    total: Decimal = RENT,
    issued: str = "2026-10-15",
    customer: str = CUSTOMER,
    **summary: Any,
) -> InvoiceRecord:
    return provider_invoice(
        fake,
        DASHBOARD,
        status="DRAFT",
        total=total,
        customer=customer,
        raw_summary={"invoice_date_at": issued, "schedule_id": None, **summary},
    )


def test_exactly_one_matching_open_charge_is_linked_and_audited(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    charge = open_charge(operator)
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed"
    invoice = Invoice.objects.get()
    assert invoice.rent_charge_id == charge.pk
    audit = AuditLog.objects.get(action="invoice.auto_linked")
    assert audit.actor is None and audit.entity_id == str(invoice.pk)
    assert audit.after is not None
    assert audit.after["rent_charge"] == str(charge.pk)
    assert audit.after["total"] == "1500.00" and audit.after["period"] == "2026-10"
    assert audit.after["provider_invoice"] == "inv-…0001"
    assert DASHBOARD not in json.dumps(audit.after)
    charge.refresh_from_db()
    assert not charge.needs_review and charge.amount_due == RENT


@pytest.mark.parametrize("status", [RentChargeStatus.UNPAID, RentChargeStatus.PARTIAL])
def test_an_unpaid_or_partial_charge_is_a_candidate(fake: FakeProvider, status: str) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    charge = open_charge(operator, status=status)
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed"
    assert Invoice.objects.get().rent_charge_id == charge.pk


def test_zero_candidates_leaves_it_unlinked_with_the_count(fake: FakeProvider) -> None:
    make_operator(email="ops@example.com", customer=CUSTOMER)
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed: unlinked (0 candidates)"
    assert Invoice.objects.get().rent_charge_id is None
    assert not AuditLog.objects.filter(action="invoice.auto_linked").exists()


def test_two_candidates_leave_it_unlinked_with_the_count(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    first = open_charge(operator, "Store 0101")
    second = open_charge(operator, "Store 0202")
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed: unlinked (2 candidates)"
    assert Invoice.objects.get().rent_charge_id is None
    assert not first.invoices.exists() and not second.invoices.exists()


@pytest.mark.parametrize(
    ("total", "issued"),
    [
        (D("1499.99"), "2026-10-15"),  # total differs from the amount due
        (D("1500.01"), "2026-10-15"),
        (RENT, "2026-11-01"),  # the next month
        (RENT, "2026-09-30"),  # the month before
    ],
)
def test_a_total_or_period_mismatch_is_not_linked(
    fake: FakeProvider, total: Decimal, issued: str
) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    open_charge(operator)
    dashboard_invoice(fake, total=total, issued=issued)
    assert deliver("create_invoice", id=DASHBOARD) == "processed: unlinked (0 candidates)"
    assert Invoice.objects.get().rent_charge_id is None


@pytest.mark.parametrize(
    "status", [RentChargeStatus.PAID, RentChargeStatus.WAIVED, RentChargeStatus.PENDING]
)
def test_a_charge_that_is_not_open_is_not_a_candidate(fake: FakeProvider, status: str) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    open_charge(operator, status=status)
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed: unlinked (0 candidates)"


def test_a_charge_with_an_invoice_or_no_balance_is_not_a_candidate(fake: FakeProvider) -> None:
    world = make_world(fake)  # its charge already has an invoice
    paid = open_charge(world.operator, "Store 0202")
    RentCharge.objects.filter(pk=paid.pk).update(amount_paid=RENT)  # balance 0, still "due"
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed: unlinked (0 candidates)"
    assert Invoice.objects.get(pk__in=_dashboard_invoice_ids()).rent_charge_id is None


def test_another_operators_charge_is_not_a_candidate(fake: FakeProvider) -> None:
    make_operator(email="ops@example.com", customer=CUSTOMER)
    other = make_operator(email="other@example.com", customer=OTHER_CUSTOMER)
    open_charge(other)
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed: unlinked (0 candidates)"


def test_an_invoice_naming_a_schedule_is_never_auto_linked(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    open_charge(operator)
    # The schedule is not one of ours: the rent run's matching does not place it, and
    # the auto-link leaves it alone even though one charge fits.
    dashboard_invoice(fake, schedule_id="sched-unknown")
    assert deliver("create_invoice", id=DASHBOARD) == "processed"
    assert Invoice.objects.get().rent_charge_id is None
    assert not AuditLog.objects.filter(action="invoice.auto_linked").exists()
    assert deliver("update_invoice", id=DASHBOARD) == "processed"
    assert Invoice.objects.get().rent_charge_id is None


def test_update_invoice_links_an_unlinked_invoice_once_a_charge_fits(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    dashboard_invoice(fake)
    assert deliver("create_invoice", id=DASHBOARD) == "processed: unlinked (0 candidates)"
    charge = open_charge(operator)
    assert deliver("update_invoice", id=DASHBOARD) == "processed"
    assert Invoice.objects.get().rent_charge_id == charge.pk


def test_send_invoice_does_not_auto_link(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    open_charge(operator)
    dashboard_invoice(fake)
    assert deliver("send_invoice", envelope(DASHBOARD)) == "processed"
    assert Invoice.objects.get().rent_charge_id is None


def test_a_replayed_auto_link_changes_nothing(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    open_charge(operator)
    dashboard_invoice(fake)
    deliver("create_invoice", id=DASHBOARD)
    before = state()
    replay_all()
    deliver("create_invoice", id=DASHBOARD)
    assert state() == before
    assert AuditLog.objects.filter(action="invoice.auto_linked").count() == 1


def test_a_linked_invoice_is_not_moved_to_another_fitting_charge(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    first = open_charge(operator, "Store 0101")
    dashboard_invoice(fake)
    deliver("create_invoice", id=DASHBOARD)
    open_charge(operator, "Store 0202")  # now two would fit
    assert deliver("update_invoice", id=DASHBOARD) == "processed"
    assert Invoice.objects.get().rent_charge_id == first.pk


def _dashboard_invoice_ids() -> list[uuid.UUID]:
    return list(
        ExternalRef.objects.filter(external_kind="invoice", external_id=DASHBOARD).values_list(
            "entity_id", flat=True
        )
    )


def _customer(lease: Lease) -> str:
    assert lease.operator_id is not None
    return ExternalRef.objects.get(
        entity_type="operator", entity_id=lease.operator_id, external_kind="customer"
    ).external_id


# --- Customers --------------------------------------------------------------------------


def customer_record(email: str, **summary: Any) -> CustomerRecord:
    return CustomerRecord(
        external_id=CUSTOMER,
        raw_status="",
        fetched_at=NOW,
        operator_email=email,
        company_name="Someone Else LLC",
        raw_summary={"id": CUSTOMER, "email": email, **summary},
    )


def test_a_customer_event_never_overwrites_the_operator(fake: FakeProvider) -> None:
    operator = make_operator(email="ops@example.com", customer=CUSTOMER)
    fake.customers[CUSTOMER] = customer_record("OPS@example.com ", company="Someone Else LLC")
    assert deliver("update_customer", id=CUSTOMER) == "processed"
    after = Operator.objects.get()
    assert (after.display_name, after.email, after.company) == (
        operator.display_name,
        "ops@example.com",
        operator.company,
    )
    assert not after.needs_review
    snapshot = ProviderSnapshot.objects.get()
    assert snapshot.payload["company"] == "Someone Else LLC"


def test_a_different_email_flags_the_operator_with_the_message(fake: FakeProvider) -> None:
    make_operator(email="ops@example.com", customer=CUSTOMER)
    fake.customers[CUSTOMER] = customer_record("billing@example.com")
    deliver("update_customer", id=CUSTOMER)
    operator = Operator.objects.get()
    assert operator.email == "ops@example.com"
    assert operator.needs_review
    audit = AuditLog.objects.get(action="operator.provider_email_differs")
    assert audit.after is not None
    assert audit.after["message"] == (
        "Email differs from the provider; receipts go to the provider's address"
    )


def test_a_customer_snapshot_is_added_only_when_it_changes(fake: FakeProvider) -> None:
    make_operator(email="ops@example.com", customer=CUSTOMER)
    fake.customers[CUSTOMER] = customer_record("ops@example.com")
    deliver("update_customer", id=CUSTOMER)
    deliver("update_customer", id=CUSTOMER)
    assert ProviderSnapshot.objects.count() == 1
    fake.customers[CUSTOMER] = customer_record("ops@example.com", phone="4155550132")
    deliver("update_customer", id=CUSTOMER)
    assert ProviderSnapshot.objects.count() == 2


def test_an_unknown_customer_makes_no_call(fake: FakeProvider) -> None:
    assert deliver("create_customer", id="cus-unknown") == ignored("unknown customer")
    assert fake.calls == []


# --- Schedules ----------------------------------------------------------------------------


def _schedule_record(fake: FakeProvider, ref: str, **changes: Any) -> None:
    fake.schedules[ref] = replace(fake.schedules[ref], **changes)


def test_a_schedule_paused_at_the_provider_is_paused_here(fake: FakeProvider) -> None:
    row, refs = enrolled(fake)
    _schedule_record(fake, refs[0], raw_status="PAUSED", active=False)
    assert deliver("update_scheduled_invoice", id=refs[0]) == "processed"
    schedule = CollectionSchedule.objects.get(lease=row, kind=ScheduleKind.RENT)
    assert schedule.state == ScheduleState.PAUSED
    assert AuditLog.objects.filter(action="collection_schedule.state_confirmed").count() == 1
    _schedule_record(fake, refs[0], raw_status="", active=True)
    deliver("update_scheduled_invoice", id=refs[0])
    schedule.refresh_from_db()
    assert schedule.state == ScheduleState.ACTIVE


def test_a_schedule_total_that_differs_flags_it_and_never_changes_the_amount(
    fake: FakeProvider,
) -> None:
    row, refs = enrolled(fake)
    _schedule_record(fake, refs[0], total=D("1499.00"))
    deliver("update_scheduled_invoice", id=refs[0])
    schedule = CollectionSchedule.objects.get(lease=row, kind=ScheduleKind.RENT)
    assert schedule.needs_review
    assert schedule.amount == RENT
    assert schedule.state == ScheduleState.ACTIVE


def test_a_deleted_schedule_is_flagged(fake: FakeProvider) -> None:
    row, refs = enrolled(fake)
    del fake.schedules[refs[0]]
    assert deliver("delete_scheduled_invoice", id=refs[0]) == "processed"
    schedule = CollectionSchedule.objects.get(lease=row, kind=ScheduleKind.RENT)
    assert schedule.needs_review and schedule.state == ScheduleState.ACTIVE


def test_an_unknown_schedule_makes_no_call(fake: FakeProvider) -> None:
    assert deliver("create_scheduled_invoice", id="sch-unknown") == ignored("unknown schedule")
    assert fake.calls == []


# --- Deposits and disputes -----------------------------------------------------------------


def deposit(fake: FakeProvider, **summary: Any) -> None:
    values: dict[str, Any] = {
        "batch_id": "batch-0001",
        "batched_at": "2026-10-01 00:00:00",
        "sum": D("1500.00"),
    }
    fake.deposits = [
        DepositRecord(
            external_id="batch-0001",
            raw_status="",
            fetched_at=NOW,
            raw_summary={**values, "fees": D("12.50"), **summary},
        )
    ]


def test_a_deposit_is_upserted(fake: FakeProvider) -> None:
    deposit(fake)
    with override_settings(TIME_ZONE="UTC"):
        assert deliver("create_deposit", batch_id="batch-0001") == "processed"
    row = Deposit.objects.get()
    assert (row.provider, row.provider_deposit_id) == ("stax_pay", "batch-0001")
    assert (row.deposit_date, row.gross, row.fees, row.net) == (
        date(2026, 10, 1),
        D("1500.00"),
        D("12.50"),
        D("1487.50"),
    )
    deposit(fake, fees=D("10.00"))
    deliver("create_deposit", batch_id="batch-0001")
    row.refresh_from_db()
    assert Deposit.objects.count() == 1 and row.net == D("1490.00")
    assert AuditLog.objects.filter(action="deposit.updated").count() == 1


def test_an_unreadable_or_old_deposit_is_ignored(fake: FakeProvider) -> None:
    deposit(fake, net=D("1.00"))
    assert deliver("create_deposit", batch_id="batch-0001") == ignored("deposit unreadable")
    assert deliver("create_deposit", batch_id="batch-other") == ignored("deposit not recent")
    assert not Deposit.objects.exists()


def test_a_dispute_flags_the_charge_once(fake: FakeProvider) -> None:
    world = make_world(fake)
    provider_txn(fake, settled_at=SETTLED_AT)
    deliver("update_transaction_settled", id=TXN)
    assert deliver("create_dispute", id=TXN) == "processed"
    deliver("create_dispute", id=TXN)
    world.charge.refresh_from_db()
    assert world.charge.needs_review
    assert AuditLog.objects.filter(action="rent_charge.disputed").count() == 1
    assert world.charge.status == RentChargeStatus.PAID  # nothing else changes


def test_a_dispute_of_an_unknown_payment_is_ignored(fake: FakeProvider) -> None:
    assert deliver("create_dispute", id="txn-unknown") == ignored("unknown payment")
    assert fake.calls == []


# --- Row 1 (processing): distinct deliveries and replays change nothing ---------------------


def _everything(fake: FakeProvider) -> list[tuple[str, str]]:
    """One event of every kind against a world where each applies."""
    world = make_world(fake)
    row, refs = enrolled(fake)
    fake.customers[CUSTOMER] = customer_record("billing@example.com")
    provider_txn(fake, settled_at=SETTLED_AT)
    provider_txn(fake, "txn-ret")
    ach_return(fake, "txn-ret")
    provider_txn(fake, "txn-refund", total=D("100.00"), kind="refund", parent_external_id=TXN)
    provider_invoice(fake, status="PAID", amount_paid=RENT, paid_at=SETTLED_AT)
    provider_invoice(fake, "inv-new", total=D("75.00"))
    _schedule_record(fake, refs[0], raw_status="PAUSED", active=False)
    deposit(fake)
    assert world.invoice.pk
    return [
        ("update_customer", CUSTOMER),
        ("update_invoice", INVOICE),
        ("create_invoice", "inv-new"),
        ("update_scheduled_invoice", refs[0]),
        ("create_transaction", TXN),
        ("update_transaction_settled", TXN),
        ("update_transaction_settled", "txn-ret"),
        ("create_transaction", "txn-refund"),
        ("create_deposit", "batch-0001"),
        ("create_dispute", TXN),
    ]


def test_five_distinct_deliveries_of_every_event_leave_the_state_of_the_first(
    fake: FakeProvider,
) -> None:
    events = _everything(fake)
    for name, object_id in events:
        deliver(name, id=object_id)
    first = state()
    assert Payment.objects.count() == 3 and Deposit.objects.count() == 1
    for _ in range(4):
        for name, object_id in events:
            deliver(name, id=object_id)
        assert state() == first


def test_replaying_every_stored_event_changes_nothing(fake: FakeProvider) -> None:
    for name, object_id in _everything(fake):
        deliver(name, id=object_id)
    before = state()
    replay_all()
    replay_all()
    assert state() == before


# --- Row 10: the returns queue ------------------------------------------------------------


def returned_payment(fake: FakeProvider) -> tuple[World, Payment]:
    world = make_world(fake)
    provider_txn(fake)
    ach_return(fake)
    deliver("update_transaction_settled", id=TXN)
    return world, payment_for()


def test_the_queue_lists_unresolved_returns_with_their_details(fake: FakeProvider) -> None:
    world, payment = returned_payment(fake)
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get("/returns/").content.decode()
    assert f'id="return-{payment.pk}"' in page
    for text in (world.operator.display_name, "Store 0101", "$1500.00", "R01", "Insufficient"):
        assert text in page
    assert "Resolve" not in page.split('id="returns"')[1]  # a viewer sees no form


def test_billing_resolves_a_return_with_a_note(fake: FakeProvider) -> None:
    world, payment = returned_payment(fake)
    billing, user = client_for(Role.BILLING)
    charge_before = state()["charges"]
    response = billing.post(f"/returns/{payment.pk}/resolve/", {"note": "Paid by wire."})
    assert response.status_code == 302 and response["Location"] == "/returns/"
    payment.refresh_from_db()
    assert payment.resolved_by_id == user.pk and payment.resolved_at is not None
    assert payment.resolution_note == "Paid by wire."
    assert payment.status == PaymentStatus.RETURNED
    assert state()["charges"] == charge_before
    assert not returns.open_returns().exists()
    assert AuditLog.objects.filter(action="payment.return_resolved").count() == 1
    assert fetches(fake, "pay_invoice") == []
    again = billing.post(f"/returns/{payment.pk}/resolve/", {"note": "Again."}, follow=True)
    assert returns.RESOLVE_ALREADY in again.content.decode()


@pytest.mark.parametrize("note", ["", "   "])
def test_a_resolution_needs_a_note(fake: FakeProvider, note: str) -> None:
    _, payment = returned_payment(fake)
    billing, _ = client_for(Role.BILLING)
    response = billing.post(f"/returns/{payment.pk}/resolve/", {"note": note}, follow=True)
    assert "A note is required" in response.content.decode()
    payment.refresh_from_db()
    assert payment.resolved_at is None


def test_a_note_with_a_card_number_is_refused(fake: FakeProvider) -> None:
    _, payment = returned_payment(fake)
    outcome = returns.resolve_return(payment.pk, note="card 4111 1111 1111 1111", actor=None)
    assert outcome == returns.ResolveOutcome(False, returns.RESOLVE_NOTE_PAYMENT_DATA)
    payment.refresh_from_db()
    assert payment.resolved_at is None


def test_a_viewer_cannot_resolve_and_bad_ids_are_404(fake: FakeProvider) -> None:
    _, payment = returned_payment(fake)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(f"/returns/{payment.pk}/resolve/", {"note": "x"}).status_code == 403
    billing, _ = client_for(Role.BILLING)
    assert billing.post("/returns/not-a-uuid/resolve/", {"note": "x"}).status_code == 404
    assert billing.post(f"/returns/{uuid.uuid4()}/resolve/", {"note": "x"}).status_code == 404
    assert billing.get(f"/returns/{payment.pk}/resolve/").status_code == 405
    payment.refresh_from_db()
    assert payment.resolved_at is None


def test_only_a_returned_payment_can_be_resolved(fake: FakeProvider) -> None:
    make_world(fake)
    provider_txn(fake)
    deliver("create_transaction", id=TXN)
    outcome = returns.resolve_return(payment_for().pk, note="x", actor=None)
    assert outcome.message == returns.RESOLVE_NOT_RETURNED


def test_the_returns_routes_are_classified_and_none_re_debits() -> None:
    routes = iter_routes()
    names = {name for name, _, _ in routes if name and "returns" in name}
    # PLAN-011 D5: the queue's CSV export reads only. PLAN-011b D5: Reschedule (billing)
    # is the one staff action that re-debits, on a date staff enter.
    assert names == {
        "ledger:returns:list",
        "ledger:returns:resolve",
        "ledger:returns:export",
        "ledger:returns:reschedule",
    }
    assert {"ledger:returns:list", "ledger:returns:export"} <= registry.READ_ONLY_ROUTE_NAMES
    for unsafe in ("ledger:returns:resolve", "ledger:returns:reschedule"):
        rule = registry.rule_for(unsafe)
        assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    for name, raw, _ in routes:
        for word in ("debit", "repay", "recharge", "retry-payment"):
            assert word not in f"{name} {raw}".lower(), name


# --- Row 8: registration --------------------------------------------------------------------

STAGING = {
    "APP_ENV": "staging",
    "APP_BASE_URL": "https://staging.example.com",
    "STAX_WEBHOOK_URL_TOKEN": TOKEN,
}
TARGET = f"https://staging.example.com/api/webhooks/stax/{TOKEN}/"


def run_command(name: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        call_command(name, stdout=stdout, stderr=stderr)
        code = 0
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def staging() -> Iterator[None]:
    with override_settings(**STAGING):
        yield


def test_registration_creates_every_event_and_stores_the_ids(
    fake: FakeProvider, staging: None
) -> None:
    code, out, err = run_command("register_webhooks")
    assert (code, err) == (0, "")
    assert sorted(record.event for record in fake.webhooks.values()) == sorted(EVENT_NAMES)
    assert {record.target_url for record in fake.webhooks.values()} == {TARGET}
    refs = ExternalRef.objects.filter(entity_type="webhook_registration", external_kind="webhook")
    assert refs.count() == len(EVENT_NAMES) == 15
    for event in EVENT_NAMES:
        ref = refs.get(entity_id=uuid.uuid5(uuid.NAMESPACE_URL, event))
        assert fake.webhooks[ref.external_id].event == event
        assert f"{event}: created" in out
    assert TOKEN not in out and "fake-webhook-1" not in out


def test_a_rerun_creates_only_the_missing_registrations(fake: FakeProvider, staging: None) -> None:
    run_command("register_webhooks")
    gone = next(iter(fake.webhooks))
    del fake.webhooks[gone]
    fake.webhooks["other-1"] = WebhookRecord(
        "other-1", "", NOW, event="update_invoice", target_url="https://elsewhere.example.com/"
    )
    fake.calls.clear()
    code, out, _ = run_command("register_webhooks")
    assert code == 0
    assert [name for name, _ in fake.calls].count("create_webhook") == 1
    assert out.count(": created") == 1 and out.count(": exists") == 14
    current = ExternalRef.objects.filter(
        entity_type="webhook_registration", superseded_at__isnull=True
    )
    assert current.count() == 15
    assert not current.filter(external_id=gone).exists()


def test_registration_refuses_in_local_and_without_https_or_token(fake: FakeProvider) -> None:
    for overrides, message in (
        ({"APP_ENV": "local"}, registration.REFUSED_LOCAL),
        ({"APP_BASE_URL": "http://staging.example.com"}, registration.NOT_HTTPS),
        ({"APP_BASE_URL": "https://"}, registration.NOT_HTTPS),
        ({"STAX_WEBHOOK_URL_TOKEN": ""}, registration.UNCONFIGURED),
    ):
        with override_settings(**{**STAGING, **overrides}):
            code, out, err = run_command("register_webhooks")
        assert (code, out, err.strip()) == (1, "", message)
    assert fake.calls == []
    with override_settings(APP_ENV="local"):
        assert run_command("unregister_webhooks")[0] == 1


def test_a_provider_failure_keeps_what_was_stored_and_prints_the_class_only(
    fake: FakeProvider, staging: None
) -> None:
    original = fake.create_webhook
    created: list[str] = []

    def flaky(target_url: str, event: str) -> WebhookRecord:
        if len(created) == 3:
            raise ProviderRejected(422, error_fields=("target_url",))
        created.append(event)
        return original(target_url, event)

    fake.create_webhook = flaky  # type: ignore[method-assign]
    code, out, err = run_command("register_webhooks")
    assert code == 2 and err.strip() == "register_webhooks failed: ProviderRejected"
    assert TOKEN not in out + err
    assert ExternalRef.objects.filter(entity_type="webhook_registration").count() == 3


def test_unregister_deletes_by_stored_id(fake: FakeProvider, staging: None) -> None:
    run_command("register_webhooks")
    first = next(iter(fake.webhooks))
    del fake.webhooks[first]  # already deleted at the provider
    code, out, _ = run_command("unregister_webhooks")
    assert code == 0
    assert fake.webhooks == {}
    deleted = [args[0] for name, args in fake.calls if name == "delete_webhook"]
    assert len(deleted) == 15
    assert out.count(": deleted") == 14 and out.count(": already gone") == 1
    assert not ExternalRef.objects.filter(
        entity_type="webhook_registration", superseded_at__isnull=True
    ).exists()
    assert run_command("unregister_webhooks")[1] == "Registrations removed: 0.\n"


# --- The adapter: webhook requests and the new response fields ------------------------------


def test_the_adapter_sends_the_verified_webhook_requests() -> None:
    rig = make_rig(always(200, {"id": "wh-1", "event": "update_invoice", "target_url": TARGET}))
    provider = StaxProvider(rig.transport)
    with override_settings(STAX_CALLER="worker"):
        record = provider.create_webhook(TARGET, "update_invoice")
        provider.delete_webhook("wh-1")
    create, delete = rig.recorder.requests
    assert (create.method, create.url.path) == ("POST", "/webhook")
    assert dict(create.url.params) == {"target_url": TARGET, "event": "update_invoice"}
    assert create.content == b""
    assert (delete.method, delete.url.path) == ("DELETE", "/webhook/wh-1")
    assert (record.external_id, record.event, record.target_url) == (
        "wh-1",
        "update_invoice",
        TARGET,
    )
    assert TOKEN not in repr(record)


@pytest.mark.parametrize(
    "url",
    [
        "http://staging.example.com/api/webhooks/stax/x/",
        "https:///api/",
        "https://user:pw@staging.example.com/",
        "https://staging.example.com/a?b=c",
        "https://staging.example.com/a#b",
        "https://staging.example.com/a b",
        "",
    ],
)
def test_a_webhook_target_that_is_not_plain_https_is_refused(url: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        validation.webhook_target_url(url)
    assert url not in str(refused.value) or not url


def test_a_transaction_maps_method_type_links_settlement_and_return() -> None:
    record = mapping.transaction_record(
        {
            "id": "txn-1",
            "method": "bank",
            "type": "refund",
            "reference_id": "txn-0",
            "invoice_id": "inv-1",
            "customer_id": "cus-1",
            "success": 1,
            "is_voided": False,
            "total": D("12.50"),
            "created_at": "2026-10-15 12:00:00",
            "settled_at": "2026-10-18T14:00:00+00:00",
            "child_transactions": [
                {"meta": {}},
                {"meta": {"achDescription": "R01 Insufficient Funds"}},
            ],
        },
        NOW,
    )
    assert (record.method, record.kind, record.parent_external_id) == ("bank", "refund", "txn-0")
    assert (record.invoice_external_id, record.customer_external_id) == ("inv-1", "cus-1")
    assert (record.success, record.is_voided) == (True, False)
    assert record.created_at == datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    assert record.settled_at == SETTLED_AT
    assert (record.returned, record.return_code, record.return_reason) == (
        True,
        "R01",
        "Insufficient Funds",
    )
    plain = mapping.transaction_record({"id": "t", "settled_at": "not a date"}, NOW)
    assert (plain.settled_at, plain.returned, plain.success) == (None, False, None)


def test_a_return_reason_holding_payment_data_is_dropped() -> None:
    assert mapping.ach_return("R03 account 4111111111111111") == ("R03", "")
    assert mapping.ach_return("No code given") == ("", "No code given")
    assert len(mapping.ach_return("R01 " + "x" * 500)[1]) == mapping.RETURN_REASON_MAX_LENGTH


def test_an_invoice_maps_its_times_and_amount_paid() -> None:
    record = mapping.invoice_record(
        {"id": "i", "sent_at": "2026-10-15 12:00:00", "paid_at": None, "total_paid": "10.00"},
        NOW,
    )
    assert record.sent_at == datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    assert (record.viewed_at, record.paid_at, record.amount_paid) == (None, None, D("10.00"))


def test_transaction_handler_constants_fit_the_outcome_column() -> None:
    max_length = WebhookEvent._meta.get_field("outcome").max_length
    for outcome in (
        transaction_handler.CARD,
        transaction_handler.NO_PARENT,
        transaction_handler.OTHER_OPERATOR,
        transaction_handler.NOT_SETTLED,
        ignored("not at the provider"),
        ignored("deposit unreadable"),
    ):
        assert max_length is not None and len(outcome) <= max_length, outcome


def test_a_stored_registration_the_provider_lists_without_a_url_is_not_created_again(
    fake: FakeProvider, staging: None
) -> None:
    run_command("register_webhooks")
    fake.webhooks = {key: replace(record, target_url="") for key, record in fake.webhooks.items()}
    fake.calls.clear()
    code, out, _ = run_command("register_webhooks")
    assert code == 0 and out.count(": exists") == 15
    assert [name for name, _ in fake.calls] == ["list_webhooks"]


def test_a_stored_registration_for_an_old_url_is_replaced(
    fake: FakeProvider, staging: None
) -> None:
    run_command("register_webhooks")
    old = "https://staging.example.com/api/webhooks/stax/" + "o" * 48 + "/"
    fake.webhooks = {key: replace(record, target_url=old) for key, record in fake.webhooks.items()}
    code, out, _ = run_command("register_webhooks")
    assert code == 0 and out.count(": created") == 15
    current = ExternalRef.objects.filter(
        entity_type="webhook_registration", superseded_at__isnull=True
    )
    assert {fake.webhooks[ref.external_id].target_url for ref in current} == {TARGET}
