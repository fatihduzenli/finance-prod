"""One-off charges (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md Batch A: D1 to D3,
D5 to D7, D9; §6 test plan except Charge now and the audit page).

The service, the worker's provider side (:mod:`payments.one_off_invoices`, run through
the queue as the worker runs it), the invoice webhook's linking, the pages and the role
gates, the P&L by category, and the data migration of the rename. Nothing reaches a
network: the payment provider is a :class:`~payments.fake.FakeProvider`, and the Stax
request bodies are checked against an ``httpx.MockTransport``.
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    CollectionStatus,
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
)
from ledger.services import one_off_charges as service
from ledger.services import pnl
from ledger.services.one_off_charges import ChargeInput
from ledger.wording import provider_name
from payments import jobs, offline_marking, one_off_invoices
from payments.errors import ProviderRejected, ProviderUnavailable
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind, JobStatus
from payments.provider import (
    Amounts,
    CustomerRecord,
    InvoiceInput,
    InvoiceRecord,
    LineItem,
    PaymentMethodRecord,
    ScheduleInput,
)
from payments.stax import mapping
from payments.stax.adapter import StaxProvider
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import always, make_rig
from tests.test_lease_services import make_lease, make_operator
from tests.test_operator_views import client_for
from tests.test_webhook_handlers import deliver
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
CUSTOMER = "cus-one-off-1"
BANK = "pm-bank-one-off-1"
EMAIL = "halid@example.com"
NOTE = "Old balance carried from the spreadsheet."
CARD = "4111 1111 1111 1111"


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


def today() -> date:
    return timezone.localdate()


def an_operator(
    fake: FakeProvider | None = None,
    *,
    customer: str | None = CUSTOMER,
    bank: str | None = BANK,
    email: str = "",
) -> Operator:
    """An operator with an active lease that holds a provider customer (known to the fake)
    and a linked bank method (PLAN-014 C3: the account is the lease's; a charge without a
    lease bills through the operator's first active lease). A later lease of the operator
    made by ``make_lease`` shares this lease's account. A test with two operators gives
    the second its own customer and method."""
    email = email or f"{uuid.uuid4().hex[:8]}-{EMAIL}"
    operator = make_operator(email=email, customer=customer)
    lease = make_lease(operator=operator)
    if bank and customer:
        ExternalRef.objects.create(
            entity_type="lease",
            entity_id=lease.pk,
            provider="stax_pay",
            external_kind="payment_method",
            external_id=bank,
        )
    if fake is not None and customer:
        fake.customers[customer] = CustomerRecord(
            external_id=customer, raw_status="", fetched_at=timezone.now(), operator_email=email
        )
        if bank:
            fake.add_payment_method(
                customer,
                PaymentMethodRecord(
                    external_id=bank, raw_status="", fetched_at=timezone.now(), method="bank"
                ),
            )
    return operator


def item(
    collection: str = CollectionMethod.EMAIL,
    amount: str = "3000.00",
    category: str = OneOffCategory.ARREARS,
    **fields: Any,
) -> ChargeInput:
    values: dict[str, Any] = {
        "fee_type": None,
        "price": D(amount),
        "cost": None,
        "vendor": "",
        "due_date": today() + timedelta(days=10),
        "note": NOTE,
        "category": category,
        "collection": collection,
        "description": "Arrears July to September 2026",
    }
    values.update(fields)
    return ChargeInput(**values)


def admin_user() -> Any:
    return client_for(Role.ADMIN)[1]


def create(
    operator: Operator,
    charge_item: ChargeInput,
    lease: Lease | None = None,
    actor: Any = None,
) -> service.Outcome:
    return service.create_charge(operator, lease, charge_item, actor)


def created(operator: Operator, charge_item: ChargeInput, **kwargs: Any) -> OneOffCharge:
    outcome = create(operator, charge_item, **kwargs)
    assert outcome.ok and outcome.charge is not None, outcome.message
    return outcome.charge


def fresh(charge: OneOffCharge) -> OneOffCharge:
    return OneOffCharge.objects.get(pk=charge.pk)


def invoice_of(charge: OneOffCharge) -> Invoice:
    return Invoice.objects.get(one_off_charge=charge)


def provider_ref(invoice: Invoice) -> str:
    return str(
        ExternalRef.objects.filter(
            entity_type="invoice", entity_id=invoice.pk, external_kind="invoice"
        )
        .values_list("external_id", flat=True)
        .get()
    )


def calls(fake: FakeProvider, name: str) -> list[tuple[object, ...]]:
    return [arguments for method, arguments in fake.calls if method == name]


def actions(charge: OneOffCharge) -> list[str]:
    return list(
        AuditLog.objects.filter(entity_type="one_off_charge", entity_id=str(charge.pk))
        .order_by("id")
        .values_list("action", flat=True)
    )


def latest_job(charge: OneOffCharge) -> Job:
    job = jobs.latest_for(charge)
    assert job is not None
    return job


# --- D2: creation through the service -------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        OneOffCategory.ARREARS,
        OneOffCategory.ADJUSTMENT,
        OneOffCategory.DAMAGE,
        OneOffCategory.OTHER,
    ],
)
def test_each_category_with_email_writes_the_charge_its_draft_invoice_and_the_job(
    fake: FakeProvider, category: str
) -> None:
    operator = an_operator(fake)
    charge = created(operator, item(category=category))
    assert (charge.operator_id, charge.lease_id, charge.category, charge.collection) == (
        operator.pk,
        None,
        category,
        CollectionMethod.EMAIL,
    )
    assert (charge.status, charge.price, charge.fee_type_id) == (
        OneOffChargeStatus.OWED,
        D("3000.00"),
        None,
    )
    invoice = invoice_of(charge)
    assert invoice.invoice_number.startswith(f"INV-{today().year}-")
    assert (invoice.internal_status, invoice.operator_id, invoice.legacy) == (
        InvoiceStatus.DRAFT,
        operator.pk,
        False,
    )
    assert (invoice.subtotal, invoice.tax, invoice.total) == (D("3000.00"), D("0.00"), D("3000.00"))
    assert (invoice.issue_date, invoice.due_date) == (today(), charge.due_date)
    assert invoice.memo == "Arrears July to September 2026"
    job = latest_job(charge)
    assert (job.kind, job.status, job.entity_type) == (
        JobKind.INVOICE_CREATE,
        JobStatus.QUEUED,
        "one_off_charge",
    )
    assert fake.calls == []  # the service never calls the provider
    assert actions(charge) == ["one_off_charge.created"]


def test_autopay_due_writes_no_invoice_and_queues_the_schedule(fake: FakeProvider) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.AUTOPAY_DUE, "500.00"))
    assert not Invoice.objects.filter(one_off_charge=charge).exists()
    assert latest_job(charge).kind == JobKind.SCHEDULE_CREATE


def test_manual_with_a_customer_makes_the_provider_invoice_and_sends_nothing(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    run_queued_jobs()
    invoice = invoice_of(charge)
    assert invoice.internal_status == InvoiceStatus.READY
    assert len(calls(fake, "create_invoice")) == 1
    assert calls(fake, "send_invoice_email") == []


def test_manual_without_a_customer_keeps_a_local_invoice_and_queues_nothing(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake, customer=None)
    outcome = create(operator, item(CollectionMethod.MANUAL))
    assert outcome.ok and outcome.charge is not None
    assert f"not connected to {provider_name()}" in outcome.message
    assert invoice_of(outcome.charge).internal_status == InvoiceStatus.DRAFT
    assert not Job.objects.exists()


@pytest.mark.parametrize(
    "collection",
    [CollectionMethod.EMAIL, CollectionMethod.AUTOPAY_DUE, CollectionMethod.CHARGE_NOW],
)
def test_anything_but_manual_needs_a_provider_customer(fake: FakeProvider, collection: str) -> None:
    operator = an_operator(fake, customer=None)
    outcome = create(operator, item(collection, "100.00"), actor=admin_user())
    assert (outcome.ok, outcome.message) == (False, service.NO_PROVIDER_CUSTOMER)
    assert not OneOffCharge.objects.exists() and not Invoice.objects.exists()


@pytest.mark.parametrize("collection", [CollectionMethod.AUTOPAY_DUE, CollectionMethod.CHARGE_NOW])
def test_a_debit_needs_a_linked_bank_method(fake: FakeProvider, collection: str) -> None:
    operator = an_operator(fake, bank=None)
    outcome = create(operator, item(collection, "100.00"), actor=admin_user())
    assert (outcome.ok, outcome.message) == (False, service.NO_BANK_METHOD)
    assert not OneOffCharge.objects.exists()


def test_charge_now_is_refused_to_billing_and_created_for_an_admin(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    billing = client_for(Role.BILLING)[1]
    refused = create(operator, item(CollectionMethod.CHARGE_NOW, "100.00"), actor=billing)
    assert (refused.ok, refused.message) == (False, service.CHARGE_NOW_ADMIN)
    assert not OneOffCharge.objects.exists()
    charge = created(operator, item(CollectionMethod.CHARGE_NOW, "100.00"), actor=admin_user())
    # In this batch a Charge now charge is created like a manual one; the admin step follows.
    assert invoice_of(charge).internal_status == InvoiceStatus.DRAFT
    assert latest_job(charge).kind == JobKind.INVOICE_CREATE


@pytest.mark.parametrize("amount", ["0.01", "999999.99"])
def test_the_amount_bounds_are_accepted(fake: FakeProvider, amount: str) -> None:
    assert created(an_operator(fake), item(amount=amount)).price == D(amount)


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        ("0.00", service.AMOUNT_NOT_POSITIVE),
        ("-1.00", service.AMOUNT_NOT_POSITIVE),
        ("1000000.00", service.AMOUNT_TOO_LARGE),
        ("10.001", service.NOT_CENTS),
    ],
)
def test_an_amount_outside_the_bounds_is_refused(
    fake: FakeProvider, amount: str, message: str
) -> None:
    outcome = create(an_operator(fake), item(amount=amount))
    assert (outcome.ok, outcome.message, outcome.field) == (False, message, "price")
    assert not OneOffCharge.objects.exists()


def test_the_description_is_required_and_at_most_120_characters(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    assert created(operator, item(description="x" * 120)).description == "x" * 120
    for text, message in (
        ("x" * 121, service.DESCRIPTION_TOO_LONG),
        ("x" * 10_000, service.DESCRIPTION_TOO_LONG),
        ("", service.DESCRIPTION_REQUIRED),
        ("\u200b\u2060 \u00a0", service.DESCRIPTION_REQUIRED),
        (f"Card {CARD}", service.TEXT_PAYMENT_DATA),
    ):
        outcome = create(operator, item(description=text))
        assert (outcome.ok, outcome.message) == (False, message), text
    assert OneOffCharge.objects.count() == 1


def test_the_internal_note_is_mandatory_and_guarded(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    for note, message in (
        ("", service.NOTE_REQUIRED),
        ("\u200b\ufeff", service.NOTE_REQUIRED),
        (f"Paid with {CARD}", service.TEXT_PAYMENT_DATA),
    ):
        outcome = create(operator, item(note=note))
        assert (outcome.ok, outcome.message, outcome.field) == (False, message, "note"), note
    assert not OneOffCharge.objects.exists()


def test_autopay_due_today_is_accepted_and_yesterday_is_refused(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    assert created(operator, item(CollectionMethod.AUTOPAY_DUE, "100.00", due_date=today()))
    outcome = create(
        operator, item(CollectionMethod.AUTOPAY_DUE, "100.00", due_date=today() - timedelta(1))
    )
    assert (outcome.ok, outcome.message) == (False, service.DUE_IN_PAST)


def test_an_ended_lease_takes_arrears_but_not_autopay(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = make_lease(operator=operator)
    Lease.objects.filter(pk=lease.pk).update(
        collection_status=CollectionStatus.ENDED, ended_on=today() - timedelta(days=5)
    )
    lease.refresh_from_db()
    charge = created(operator, item(CollectionMethod.EMAIL), lease=lease)
    assert charge.lease_id == lease.pk
    outcome = create(operator, item(CollectionMethod.AUTOPAY_DUE, "100.00"), lease=lease)
    assert (outcome.ok, outcome.message) == (False, service.LEASE_ENDED)


def test_a_lease_of_another_operator_is_refused(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    other = make_lease(operator=make_operator(email="other@example.com", customer="cus-other"))
    outcome = create(operator, item(), lease=other)
    assert (outcome.ok, outcome.message) == (False, service.LEASE_OTHER_OPERATOR)


@override_settings(LARGE_DEBIT_WARNING_AMOUNT=D("2000.00"))
def test_the_large_debit_box_applies_above_the_threshold_only(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    at = created(operator, item(CollectionMethod.AUTOPAY_DUE, "2000.00"))
    assert "one_off_charge.large_debit_confirmed" not in actions(at)
    refused = create(operator, item(CollectionMethod.AUTOPAY_DUE, "2000.01"))
    assert (refused.ok, refused.field) == (False, "confirm")
    assert "$2,000.01" in refused.message
    confirmed = created(operator, item(CollectionMethod.AUTOPAY_DUE, "2000.01", confirmed=True))
    assert actions(confirmed) == [
        "one_off_charge.created",
        "one_off_charge.large_debit_confirmed",
    ]
    # An e-mailed invoice debits nothing: no box.
    assert created(operator, item(CollectionMethod.EMAIL, "5000.00"))


@override_settings(PROVIDER_MAX_TXN_AMOUNT=D("5000.00"))
def test_the_provider_limit_refuses_a_debit_above_it(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    outcome = create(operator, item(CollectionMethod.AUTOPAY_DUE, "5000.01", confirmed=True))
    assert (outcome.ok, outcome.field) == (False, "price")
    assert "$5,000.00" in outcome.message
    assert created(operator, item(CollectionMethod.AUTOPAY_DUE, "5000.00", confirmed=True))


def test_a_resubmitted_form_creates_one_charge(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    key = uuid.uuid4()
    first = created(operator, item(creation_key=key))
    again = create(operator, item(creation_key=key))
    assert again.ok and again.charge is not None and again.charge.pk == first.pk
    assert again.message == service.ALREADY_CREATED
    assert (OneOffCharge.objects.count(), Invoice.objects.count(), Job.objects.count()) == (1, 1, 1)


def test_an_archived_operator_is_refused(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
    operator.refresh_from_db()
    outcome = create(operator, item())
    assert (outcome.ok, outcome.message) == (False, service.OPERATOR_ARCHIVED)


def test_a_setup_charge_is_still_created_through_the_same_service(fake: FakeProvider) -> None:
    from tests.test_setup_charges import fee

    lease = make_lease(operator=an_operator(fake))
    sign = fee("sign_fee")
    outcome = service.create_charge(
        lease.operator,
        lease,
        ChargeInput(fee_type=sign, price=D("750.00"), cost=None, vendor="", due_date=today()),
        None,
    )
    assert outcome.ok and outcome.charge is not None
    charge = outcome.charge
    assert (charge.category, charge.collection, charge.description, charge.operator_id) == (
        OneOffCategory.SETUP,
        CollectionMethod.NONE,
        sign.name,
        lease.operator_id,
    )
    assert not Invoice.objects.exists() and not Job.objects.exists()


# --- D3: the provider invoice in the worker -------------------------------------------------


def test_an_arrears_email_charge_is_created_sent_and_shown_linked(
    fake: FakeProvider, worker: None
) -> None:
    """The batch's done-when: $3,000 arrears by e-mail, the invoice made with our reference
    and the line item, sent, and the page shows it linked and sent."""
    operator = an_operator(fake)
    charge = created(operator, item())
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    invoice = invoice_of(charge)
    [(sent,)] = calls(fake, "create_invoice")
    assert isinstance(sent, InvoiceInput)
    assert (sent.customer_external_id, sent.reference, sent.memo) == (
        CUSTOMER,
        invoice.invoice_number,
        "",
    )
    assert (sent.amounts.total, sent.amounts.subtotal, sent.amounts.tax) == (
        D("3000.00"),
        D("3000.00"),
        D("0.00"),
    )
    [line] = sent.amounts.line_items
    assert (line.description, line.quantity, line.unit_price) == (
        "Arrears July to September 2026",
        D(1),
        D("3000.00"),
    )
    assert (sent.due_on, sent.invoice_date) == (invoice.due_date, invoice.issue_date)
    ref = provider_ref(invoice)
    assert calls(fake, "send_invoice_email") == [(ref, ())]
    assert (invoice.internal_status, invoice.provider_status) == (InvoiceStatus.SENT, "SENT")
    assert invoice.sent_at is not None
    assert "sent by e-mail" in job.result
    client, _ = client_for(Role.VIEWER)
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert invoice.invoice_number in page and has_token(page, "Sent") and ">SENT<" in page
    assert ref not in page  # the provider ID is masked
    assert f"{ref[:4]}…{ref[-4:]}" in page


def test_the_request_body_carries_our_number_as_reference_ach_only(worker: None) -> None:
    rig = make_rig(always(200, {"id": "inv-body-1", "status": "DRAFT"}))
    provider = StaxProvider(rig.transport)
    charge_input = InvoiceInput(
        customer_external_id=CUSTOMER,
        amounts=Amounts(
            D("3000.00"),
            D("3000.00"),
            D("0.00"),
            (LineItem("Arrears", D(1), D("3000.00")),),
        ),
        due_on=date(2026, 10, 15),
        invoice_date=date(2026, 10, 5),
        reference="INV-2026-0042",
    )
    provider.create_invoice(charge_input)
    [request] = rig.recorder.requests
    body = json.loads(request.content)
    assert body["customer_id"] == CUSTOMER
    assert body["meta"]["reference"] == "INV-2026-0042"
    assert body["meta"]["isACHPaymentEnabled"] is True
    assert body["meta"]["isCCPaymentEnabled"] is False
    assert [item["item"] for item in body["meta"]["lineItems"]] == ["Arrears"]
    assert "memo" not in body["meta"]


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.create_invoice(
            InvoiceInput(
                CUSTOMER,
                Amounts(
                    D("1.00"),
                    D("1.00"),
                    D("0.00"),
                    (LineItem("X", D(1), D("1.00")),),
                ),
                reference="INV-2026-0001",
            )
        ),
        lambda p: p.send_invoice_email("inv-1"),
        lambda p: p.delete_invoice("inv-1"),
        lambda p: p.create_scheduled_invoice(
            ScheduleInput(
                CUSTOMER,
                BANK,
                Amounts(
                    D("1.00"),
                    D("1.00"),
                    D("0.00"),
                    (LineItem("X", D(1), D("1.00")),),
                ),
                "DTSTART=20261015T120000Z;FREQ=DAILY;COUNT=1",
                reference="OOC-12345678",
            )
        ),
    ],
    ids=["create_invoice", "send_invoice_email", "delete_invoice", "create_scheduled_invoice"],
)
def test_a_4xx_on_every_one_off_call_is_sent_once(worker: None, call: Any) -> None:
    rig = make_rig(always(422))
    with pytest.raises(ProviderRejected):
        call(StaxProvider(rig.transport))
    assert len(rig.recorder.requests) == 1
    assert rig.clock.sleeps == []


def test_the_reader_exposes_reference_and_schedule_without_widening_the_summary() -> None:
    record = mapping.invoice_record(
        {
            "id": "inv-9",
            "status": "SENT",
            "schedule_id": "sch-9",
            "meta": {"reference": "INV-2026-0042", "memo": "x"},
        },
        timezone.now(),
    )
    assert (record.reference, record.schedule_id) == ("INV-2026-0042", "sch-9")
    assert "meta" not in record.raw_summary and "reference" not in record.raw_summary
    unsafe = mapping.invoice_record({"id": "inv-10", "meta": {"reference": CARD}}, timezone.now())
    assert unsafe.reference == ""


def test_a_422_on_create_fails_once_and_the_retry_is_a_new_job(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item())
    fake.fail_next("create_invoice", ProviderRejected(422, error_fields=("customer_id",)))
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    # The failure in words (copy list §4.1); the class and the key names in the audit row.
    number = invoice_of(charge).invoice_number
    assert job.result == (
        f"{provider_name()} did not create invoice {number}: Rejected. Retry from the charge page."
    )
    failed = AuditLog.objects.get(action="one_off_charge.provider_invoice_failed")
    assert failed.after == {"failure": "ProviderRejected", "fields": "customer_id"}
    assert len(calls(fake, "create_invoice")) == 1
    assert invoice_of(charge).internal_status == InvoiceStatus.DRAFT
    assert service.retry_kind(charge) == JobKind.INVOICE_CREATE
    client, _ = client_for(Role.BILLING)
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert 'id="retry-provider"' in page and "Failed: " in page
    client.post(f"/one-off-charges/{charge.pk}/retry/")
    retried = latest_job(charge)
    assert retried.pk != job.pk and retried.kind == JobKind.INVOICE_CREATE
    [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE, done.result
    # The retry looked first (an attempt was recorded), found nothing, created once.
    assert len(calls(fake, "list_invoices")) == 1
    assert len(calls(fake, "create_invoice")) == 2


def test_a_create_timeout_is_followed_by_a_lookup_that_finds_the_invoice_by_reference(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    invoice = invoice_of(charge)

    original = fake.create_invoice

    def created_then_lost(invoice_input: InvoiceInput) -> InvoiceRecord:
        original(invoice_input)
        raise ProviderUnavailable(None, "timed out")

    fake.create_invoice = created_then_lost  # type: ignore[method-assign,assignment]
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED and ": Unavailable. Retry" in job.result
    [made] = fake.invoices.values()
    assert made.reference == invoice.invoice_number
    fake.create_invoice = original  # type: ignore[method-assign]
    jobs.enqueue(JobKind.INVOICE_CREATE, charge, None, None)
    [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE and "nothing was created" in done.result
    assert len(calls(fake, "create_invoice")) == 1  # the first one only
    assert provider_ref(invoice) == made.external_id
    assert fresh(charge).needs_review is False


def test_without_a_reference_the_lookup_matches_a_unique_total_and_date(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    invoice = invoice_of(charge)
    AuditLog.objects.create(
        action=one_off_invoices.INVOICE_ATTEMPT_ACTION,
        entity_type="one_off_charge",
        entity_id=str(charge.pk),
    )
    fake.invoices["fake-found-1"] = InvoiceRecord(
        external_id="fake-found-1",
        raw_status="DRAFT",
        fetched_at=timezone.now(),
        customer_external_id=CUSTOMER,
        total=invoice.total,
        raw_summary={"invoice_date_at": invoice.issue_date.isoformat()},
    )
    [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE, done.result
    assert provider_ref(invoice) == "fake-found-1"
    assert calls(fake, "create_invoice") == []


def test_two_candidates_flag_the_charge_and_create_nothing(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    invoice = invoice_of(charge)
    AuditLog.objects.create(
        action=one_off_invoices.INVOICE_ATTEMPT_ACTION,
        entity_type="one_off_charge",
        entity_id=str(charge.pk),
    )
    for n in (1, 2):
        fake.invoices[f"fake-twin-{n}"] = InvoiceRecord(
            external_id=f"fake-twin-{n}",
            raw_status="DRAFT",
            fetched_at=timezone.now(),
            customer_external_id=CUSTOMER,
            total=invoice.total,
            raw_summary={"invoice_date_at": invoice.issue_date.isoformat()},
        )
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert job.result == service.SEVERAL_PROVIDER_INVOICES
    assert calls(fake, "create_invoice") == []
    flagged = fresh(charge)
    assert flagged.needs_review
    assert str(service.SEVERAL_PROVIDER_INVOICES) in service.review_message(flagged)


def test_a_failed_send_leaves_the_invoice_linked_and_the_retry_sends_only(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item())
    fake.fail_next("send_invoice_email", ProviderUnavailable(503, "unavailable"))
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert "sending it failed: Unavailable." in job.result
    invoice = invoice_of(charge)
    assert provider_ref(invoice) and invoice.internal_status == InvoiceStatus.READY
    assert service.retry_kind(charge) == JobKind.INVOICE_SEND
    client, _ = client_for(Role.BILLING)
    client.post(f"/one-off-charges/{charge.pk}/retry/")
    [sent] = run_queued_jobs()
    assert (sent.kind, sent.status) == (JobKind.INVOICE_SEND, JobStatus.DONE)
    assert len(calls(fake, "create_invoice")) == 1
    assert len(calls(fake, "send_invoice_email")) == 2
    assert invoice_of(charge).internal_status == InvoiceStatus.SENT


def test_replaying_the_create_job_after_success_creates_nothing(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item())
    run_queued_jobs()
    fake.calls.clear()
    jobs.enqueue(JobKind.INVOICE_CREATE, charge, None, None)
    [again] = run_queued_jobs()
    assert again.status == JobStatus.DONE and "already" in again.result
    assert fake.calls == []


# --- D2: the one-occurrence schedule ---------------------------------------------------------


def test_autopay_due_creates_one_schedule_on_the_due_date_with_the_linked_method(
    fake: FakeProvider, worker: None
) -> None:
    due = today() + timedelta(days=12)
    charge = created(an_operator(fake), item(CollectionMethod.AUTOPAY_DUE, "450.00", due_date=due))
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    [(schedule,)] = calls(fake, "create_scheduled_invoice")
    assert isinstance(schedule, ScheduleInput)
    assert schedule.rule == f"DTSTART={due:%Y%m%d}T120000Z;FREQ=DAILY;COUNT=1"
    assert (schedule.customer_external_id, schedule.payment_method_external_id) == (CUSTOMER, BANK)
    assert schedule.reference == f"OOC-{str(charge.pk)[:8]}"
    assert schedule.amounts.total == D("450.00")
    assert [line.description for line in schedule.amounts.line_items] == [charge.description]
    assert service.provider_schedule(charge)
    body = mapping.schedule_body(schedule, "https://example.com/bill/")
    assert body["meta"]["reference"] == f"OOC-{str(charge.pk)[:8]}"
    assert body["rule"].endswith(";FREQ=DAILY;COUNT=1")
    assert not Invoice.objects.filter(one_off_charge=charge).exists()


def test_a_schedule_due_today_after_the_debit_hour_starts_at_the_next_hour() -> None:
    now = datetime(2026, 10, 5, 15, 20, tzinfo=UTC)
    assert one_off_invoices.schedule_rule(date(2026, 10, 5), now) == (
        "DTSTART=20261005T160000Z;FREQ=DAILY;COUNT=1"
    )
    assert one_off_invoices.schedule_rule(date(2026, 10, 6), now) == (
        "DTSTART=20261006T120000Z;FREQ=DAILY;COUNT=1"
    )


# --- D5: webhook linking ----------------------------------------------------------------------


def test_a_create_invoice_webhook_naming_our_number_links_to_the_charge(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    invoice = invoice_of(charge)
    fake.invoices["fake-dash-1"] = InvoiceRecord(
        external_id="fake-dash-1",
        raw_status="DRAFT",
        fetched_at=timezone.now(),
        customer_external_id=CUSTOMER,
        total=invoice.total,
        raw_summary={"invoice_date_at": invoice.issue_date.isoformat()},
        reference=invoice.invoice_number,
    )
    outcome = deliver("create_invoice", id="fake-dash-1")
    assert outcome == "processed"
    assert provider_ref(invoice) == "fake-dash-1"
    assert Invoice.objects.count() == 1  # no second row, no rent auto-link
    # The worker's create then finds it linked and creates nothing.
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE and calls(fake, "create_invoice") == []


def test_a_reference_naming_another_operators_invoice_is_not_linked_and_flags(
    fake: FakeProvider,
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    invoice = invoice_of(charge)
    stranger = make_operator(email="stranger@example.com", customer="cus-stranger")
    make_lease(operator=stranger)
    fake.invoices["fake-stranger-1"] = InvoiceRecord(
        external_id="fake-stranger-1",
        raw_status="DRAFT",
        fetched_at=timezone.now(),
        customer_external_id="cus-stranger",
        total=invoice.total,
        raw_summary={"invoice_date_at": invoice.issue_date.isoformat()},
        reference=invoice.invoice_number,
    )
    outcome = deliver("create_invoice", id="fake-stranger-1")
    assert outcome.startswith("ignored")
    assert not ExternalRef.objects.filter(external_id="fake-stranger-1").exists()
    assert not Invoice.objects.filter(operator=stranger).exists()
    flagged = fresh(charge)
    assert flagged.needs_review
    assert str(service.OTHER_OPERATOR_REFERENCE) in service.review_message(flagged)


def test_a_second_provider_invoice_with_a_linked_number_links_nothing(
    fake: FakeProvider, worker: None
) -> None:
    operator = an_operator(fake)
    first = created(operator, item(CollectionMethod.MANUAL))
    second = created(operator, item(CollectionMethod.MANUAL, "100.00"))
    run_queued_jobs()
    one, two = invoice_of(first), invoice_of(second)
    fake.invoices["fake-dup-1"] = InvoiceRecord(
        external_id="fake-dup-1",
        raw_status="DRAFT",
        fetched_at=timezone.now(),
        customer_external_id=CUSTOMER,
        total=one.total,
        reference=one.invoice_number,
    )
    outcome = deliver("create_invoice", id="fake-dup-1")
    assert outcome.startswith("ignored")
    assert provider_ref(one) != "fake-dup-1" and provider_ref(two) != "fake-dup-1"
    assert fresh(first).needs_review and not fresh(second).needs_review


def test_an_invoice_generated_by_the_schedule_gets_its_local_row(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.AUTOPAY_DUE, "450.00"))
    run_queued_jobs()
    schedule = service.provider_schedule(charge)
    fake.invoices["fake-gen-1"] = InvoiceRecord(
        external_id="fake-gen-1",
        raw_status="SENT",
        fetched_at=timezone.now(),
        customer_external_id=CUSTOMER,
        total=D("450.00"),
        raw_summary={"invoice_date_at": charge.due_date.isoformat()},
        schedule_id=schedule,
    )
    assert deliver("create_invoice", id="fake-gen-1") == "processed"
    invoice = invoice_of(charge)
    assert (invoice.total, invoice.issue_date, provider_ref(invoice)) == (
        D("450.00"),
        charge.due_date,
        "fake-gen-1",
    )
    assert invoice.invoice_number.startswith("INV-")
    assert not fresh(charge).needs_review
    # A replay changes nothing.
    assert deliver("create_invoice", id="fake-gen-1") == "processed"
    assert Invoice.objects.filter(one_off_charge=charge).count() == 1


# --- D6: cancel -------------------------------------------------------------------------------


def test_cancel_deletes_the_invoice_and_voids_the_charge_after_the_provider_confirms(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item())
    run_queued_jobs()
    ref = provider_ref(invoice_of(charge))
    client, user = client_for(Role.BILLING)
    response = client.post(f"/one-off-charges/{charge.pk}/cancel/", {"note": "Charged twice."})
    assert response.status_code == 302
    assert fresh(charge).status == OneOffChargeStatus.OWED  # nothing local before the worker
    assert latest_job(charge).kind == JobKind.INVOICE_DELETE
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    assert calls(fake, "delete_invoice") == [(ref,)]
    voided = fresh(charge)
    assert (voided.status, voided.void_note, voided.voided_by_id) == (
        OneOffChargeStatus.VOIDED,
        "Charged twice.",
        user.pk,
    )
    invoice = invoice_of(charge)
    assert (invoice.internal_status, invoice.provider_status) == (InvoiceStatus.VOID, "DELETED")
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert has_token(page, "Voided") and 'id="cancel-charge"' not in page
    assert actions(charge)[-2:] == ["one_off_charge.cancel_requested", "one_off_charge.voided"]


def test_cancel_with_a_404_on_delete_counts_as_deleted(fake: FakeProvider, worker: None) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    run_queued_jobs()
    del fake.invoices[provider_ref(invoice_of(charge))]
    assert service.cancel_charge(charge.pk, note="Duplicate.", actor=None).ok
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    assert fresh(charge).status == OneOffChargeStatus.VOIDED


def test_a_failed_delete_leaves_the_charge_owed_with_the_retry(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    run_queued_jobs()
    fake.fail_next("delete_invoice", ProviderUnavailable(503, "unavailable"))
    assert service.cancel_charge(charge.pk, note="Duplicate.", actor=None).ok
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED and ": Unavailable. Retry" in job.result
    assert fresh(charge).status == OneOffChargeStatus.OWED
    assert service.retry_kind(fresh(charge)) == JobKind.INVOICE_DELETE
    jobs.enqueue(JobKind.INVOICE_DELETE, charge, None, None)
    [again] = run_queued_jobs()
    assert again.status == JobStatus.DONE and fresh(charge).status == OneOffChargeStatus.VOIDED


def test_an_invoice_the_provider_shows_paid_is_never_deleted(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    run_queued_jobs()
    ref = provider_ref(invoice_of(charge))
    assert service.cancel_charge(charge.pk, note="Duplicate.", actor=None).ok
    fake.invoices[ref] = replace(fake.invoices[ref], raw_status="PAID")
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, one_off_invoices.DELETE_PAID)
    assert calls(fake, "delete_invoice") == []
    assert fresh(charge).status == OneOffChargeStatus.OWED


def test_cancel_of_an_unfired_autopay_charge_deletes_its_schedule(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.AUTOPAY_DUE, "450.00"))
    run_queued_jobs()
    schedule = service.provider_schedule(charge)
    outcome = service.cancel_charge(charge.pk, note="Agreed to pay by wire.", actor=None)
    assert outcome.ok and outcome.job is not None
    assert outcome.job.kind == JobKind.SCHEDULE_DELETE
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    assert calls(fake, "delete_scheduled_invoice") == [(schedule,)]
    assert fresh(charge).status == OneOffChargeStatus.VOIDED
    assert service.provider_schedule(charge) == ""  # superseded: nothing links to it now


def test_cancel_is_refused_with_a_pending_payment_or_money_received(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    pending = created(operator, item(CollectionMethod.MANUAL))
    Payment.objects.create(
        one_off_charge=pending,
        invoice=invoice_of(pending),
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("3000.00"),
        status=PaymentStatus.PENDING,
    )
    refused = service.cancel_charge(pending.pk, note="x", actor=None)
    assert (refused.ok, refused.message) == (False, service.CANCEL_PENDING)
    paid = created(operator, item(CollectionMethod.MANUAL, "100.00"))
    assert service.record_payment(
        paid.pk,
        method=PaymentMethod.WIRE,
        amount=D("40.00"),
        paid_on=today(),
        note="Wire.",
        actor=None,
    ).ok
    refused = service.cancel_charge(paid.pk, note="x", actor=None)
    assert (refused.ok, refused.message) == (False, service.CANCEL_PAID)
    assert not Job.objects.filter(kind=JobKind.INVOICE_DELETE).exists()


def test_cancel_needs_a_note_and_is_not_offered_for_a_setup_charge(fake: FakeProvider) -> None:
    from tests.test_setup_charges import a_charge

    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    for note in ("", "\u200b \u00a0", f"Card {CARD}"):
        outcome = service.cancel_charge(charge.pk, note=note, actor=None)
        assert not outcome.ok and outcome.field == "note"
    setup = a_charge()
    outcome = service.cancel_charge(setup.pk, note="No.", actor=None)
    assert (outcome.ok, outcome.message) == (False, service.CANCEL_SETUP)


def test_a_payment_arriving_after_the_request_stops_the_void(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    run_queued_jobs()
    assert service.cancel_charge(charge.pk, note="Duplicate.", actor=None).ok
    Payment.objects.create(
        one_off_charge=charge,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("3000.00"),
        status=PaymentStatus.PENDING,
    )
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, service.CANCEL_PENDING)
    assert calls(fake, "delete_invoice") == []


# --- Pages and roles (D2, D9) ---------------------------------------------------------------


def form(**overrides: Any) -> dict[str, str]:
    values = {
        "creation_key": str(uuid.uuid4()),
        "category": OneOffCategory.ARREARS.value,
        "description": "Arrears July to September 2026",
        "amount": "3000.00",
        "due_date": (today() + timedelta(days=10)).isoformat(),
        "collection": CollectionMethod.EMAIL.value,
        "note": NOTE,
    }
    values.update(overrides)
    return values


def test_the_removed_create_form_answers_410_and_the_pages_offer_new_invoice(
    fake: FakeProvider,
) -> None:
    """PLAN-012 D6: "Create one-off charge" is replaced by New invoice on the store and
    operator pages; its URLs answer 410 Gone with a sentence, and create nothing."""
    operator = an_operator(fake)
    lease = make_lease(operator=operator)
    client, _ = client_for(Role.BILLING)
    # PLAN-012d D2, D4: the store page's bar has Create invoice; the lease page no longer.
    # PLAN-012e-2 D5: the store bar's id is "create-invoice", the operator page's "new-invoice".
    # PLAN-012f-a D7: the operator page offers no New invoice either (invoices are made from
    # the store page); the store bar keeps Create invoice.
    operator_page = client.get(f"/operators/{operator.pk}/").content.decode()
    assert (
        'id="new-one-off-charge"' not in operator_page and 'id="new-invoice"' not in operator_page
    )
    store_page = client.get(f"/stores/{lease.store_id}/").content.decode()
    assert 'id="new-one-off-charge"' not in store_page and 'id="create-invoice"' in store_page
    for path in (f"/operators/{operator.pk}/charges/new/", f"/leases/{lease.pk}/charges/new/"):
        response = client.get(path)
        assert response.status_code == 410, path
        assert "replaced by New invoice" in response.content.decode()
        assert client.post(path, form()).status_code == 410
    assert not OneOffCharge.objects.exists()


def test_the_description_is_escaped_on_the_page(fake: FakeProvider) -> None:
    charge = created(an_operator(fake), item(description="<script>alert(1)</script>"))
    client, _ = client_for(Role.VIEWER)
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_the_charge_page_says_when_the_charge_now_flag_is_off(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    client, admin = client_for(Role.ADMIN)
    charge = created(
        operator, item(CollectionMethod.CHARGE_NOW, "100.00", confirmed=True), actor=admin
    )
    page = client.get(f"/one-off-charges/{charge.pk}/").content.decode()
    # PLAN-010d Batch B: the confirmation step exists; with the flag off it is not offered.
    assert 'id="charge-now-refusal"' in page
    assert service.CHARGE_NOW_DISABLED in page
    assert 'id="charge-now"' not in page


@pytest.mark.parametrize(
    "path",
    [
        "/operators/{operator}/charges/new/",
        "/leases/{lease}/charges/new/",
        "/one-off-charges/{charge}/cancel/",
        "/one-off-charges/{charge}/retry/",
    ],
)
def test_a_viewer_cannot_create_cancel_or_retry(fake: FakeProvider, path: str) -> None:
    operator = an_operator(fake)
    lease = make_lease(operator=operator)
    charge = created(operator, item(CollectionMethod.MANUAL))
    client, _ = client_for(Role.VIEWER)
    url = path.format(operator=operator.pk, lease=lease.pk, charge=charge.pk)
    assert client.post(url, form() | {"note": "x"}).status_code == 403
    assert OneOffCharge.objects.count() == 1
    assert fresh(charge).status == OneOffChargeStatus.OWED


def test_unknown_or_malformed_ids_are_404(fake: FakeProvider) -> None:
    client, _ = client_for(Role.BILLING)
    for path in (
        f"/one-off-charges/{uuid.uuid4()}/cancel/",
        "/one-off-charges/%25/",
    ):
        assert client.get(path).status_code == 404, path


# --- D7: the P&L by category ----------------------------------------------------------------


def _settle(charge: OneOffCharge, amount: str, on: date) -> None:
    Payment.objects.create(
        one_off_charge=charge,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.WIRE,
        amount=D(amount),
        status=PaymentStatus.SETTLED,
        settled_on=on,
        initiated_on=on,
        note="Wire.",
    )


def test_arrears_count_as_rent_damage_as_fees_and_a_voided_charge_in_neither(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    lease = make_lease(operator=operator)
    july = date(2026, 7, 10)
    arrears = created(operator, item(due_date=july, amount="3000.00"), lease=lease)
    damage = created(
        operator,
        item(category=OneOffCategory.DAMAGE, due_date=july, amount="200.00"),
        lease=lease,
    )
    voided = created(operator, item(due_date=july, amount="999.00"), lease=lease)
    OneOffCharge.objects.filter(pk=voided.pk).update(
        status=OneOffChargeStatus.VOIDED, voided_at=timezone.now(), void_note="Duplicate."
    )
    _settle(arrears, "3000.00", july)
    _settle(damage, "200.00", july)
    selected = pnl.parse_query({"from": "2026-07", "to": "2026-07", "group": "store"})
    rows = [row for row in pnl.rows(selected) if row.store_id == lease.store_id]
    assert sum((row.rent_collected for row in rows), D("0")) == D("3000.00")
    assert sum((row.fees_billed for row in rows), D("0")) == D("200.00")
    assert sum((row.fees_collected for row in rows), D("0")) == D("200.00")
    # Rent billed: the lease's rent charges are not made here; only the arrears are billed.
    assert sum((row.rent_billed for row in rows), D("0")) == D("3000.00")
    total = pnl.total(rows)
    assert total.fee_costs == D("0.00")  # no unknown cost from a non-setup charge


def test_an_operator_level_charge_is_on_a_row_without_a_store_that_carries_no_costs(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    july = date(2026, 7, 10)
    created(operator, item(category=OneOffCategory.OTHER, due_date=july, amount="50.00"))
    selected = pnl.parse_query({"from": "2026-07", "to": "2026-07"})
    # The operator's lease (PLAN-014 C3: it holds the account) may add its store's row;
    # the charge is on the one row without a store, and on no other.
    rows = [row for row in pnl.rows(selected) if row.operator_id == operator.pk]
    [row] = [row for row in rows if row.store_id is None]
    assert sum(other.fees_billed for other in rows) == D("50.00")
    assert (row.store_id, row.store, row.fees_billed) == (None, pnl.NO_STORE, D("50.00"))
    assert (row.landlord_rent, row.other_costs, row.margin_billed) == (
        D("0.00"),
        D("0.00"),
        D("50.00"),
    )


# --- D1: the data migration of the rename ---------------------------------------------------

LEDGER_BEFORE = ("ledger", "0016_store_cost_corrections")
PAYMENTS_BEFORE = ("payments", "0009_setup_payment_retry_mark")
RENAME = importlib.import_module("ledger.migrations.0018_one_off_charges_rows")


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


@pytest.mark.django_db(transaction=True)
def test_the_rename_migration_rewrites_rows_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = _migrate(LEDGER_BEFORE, PAYMENTS_BEFORE)
    try:
        operator = old.get_model("ledger", "Operator").objects.create(display_name="Halid LLC")
        store = old.get_model("ledger", "Store").objects.create(
            external_label="Store 0700", status="active"
        )
        lease = old.get_model("ledger", "Lease").objects.create(
            store=store,
            operator=operator,
            collection_status="prospective",
            payment_channel="unknown",
        )
        fee_type = old.get_model("ledger", "FeeType").objects.create(
            code="rename_fee", name="Rename fee"
        )
        charge = old.get_model("ledger", "SetupCharge").objects.create(
            lease=lease, fee_type=fee_type, price=D("100.00"), due_date=date(2026, 10, 1)
        )
        old.get_model("ledger", "Payment").objects.create(
            setup_charge=charge,
            kind="setup",
            method="wire",
            amount=D("100.00"),
            status="settled",
            note="Wire.",
        )
        audit = old.get_model("ledger", "AuditLog")
        audit.objects.create(
            action="setup_charge.created",
            entity_type="setup_charge",
            entity_id=str(charge.pk),
            after={"lease": str(lease.pk)},
        )
        audit.objects.create(
            action="payment.provider_already_paid",
            entity_type="payment",
            entity_id=str(uuid.uuid4()),
            after={"setup_charge": str(charge.pk)},
        )
        old.get_model("payments", "Job").objects.create(
            kind="setup_payment_mark", entity_type="setup_charge", entity_id=charge.pk
        )
        new = _migrate(*leaves)
        row = new.get_model("ledger", "OneOffCharge").objects.get(pk=charge.pk)
        assert (row.operator_id, row.category, row.collection, row.description) == (
            operator.pk,
            "setup",
            "none",
            "Rename fee",
        )
        payment = new.get_model("ledger", "Payment").objects.get()
        assert (payment.kind, payment.one_off_charge_id) == ("one_off", charge.pk)
        logged = new.get_model("ledger", "AuditLog").objects.order_by("id")
        # Ledger 0059 (PLAN-014 C8), later in the chain, audits the lease's billing row.
        assert [(entry.action, entry.entity_type) for entry in logged] == [
            ("one_off_charge.created", "one_off_charge"),
            ("payment.provider_already_paid", "payment"),
            ("lease_billing.migrated", "lease"),
        ]
        assert logged[1].after == {"one_off_charge": str(charge.pk)}
        job = new.get_model("payments", "Job").objects.get()
        assert (job.kind, job.entity_type) == ("one_off_payment_mark", "one_off_charge")
        # Reverse while only setup rows exist: everything goes back.
        back = _migrate(LEDGER_BEFORE, PAYMENTS_BEFORE)
        assert back.get_model("ledger", "Payment").objects.get().kind == "setup"
        assert back.get_model("payments", "Job").objects.get().kind == "setup_payment_mark"
        assert (
            back.get_model("ledger", "AuditLog").objects.filter(entity_type="setup_charge").count()
            == 1
        )
    finally:
        _migrate(*leaves)
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tgenabled FROM pg_trigger WHERE tgname = 'audit_log_append_only'"
            )
            assert cursor.fetchone() == ("O",)  # enabled again
            cursor.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname = %s",
                ["one_off_charges_payment_data_guard"],
            )
            assert cursor.fetchone() == (1,)


@pytest.mark.django_db(transaction=True)
def test_the_rename_migration_refuses_to_reverse_over_a_one_off_charge() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    operator = Operator.objects.create(display_name="Halid LLC")
    OneOffCharge.objects.create(
        operator=operator,
        category=OneOffCategory.DAMAGE,
        collection=CollectionMethod.MANUAL,
        description="Broken shelf",
        price=D("10.00"),
        due_date=date(2026, 10, 1),
    )
    with pytest.raises(RuntimeError, match="refuses to reverse"):
        _migrate(LEDGER_BEFORE, PAYMENTS_BEFORE)
    _migrate(*leaves)
    assert RENAME.CANNOT_REVERSE


# --- Guards: the closed lists and the card-number trigger -----------------------------------


def test_the_closed_lists_take_the_new_values_and_refuse_the_old_ones(fake: FakeProvider) -> None:
    from django.db import IntegrityError, transaction

    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    for kind in (
        JobKind.INVOICE_CREATE,
        JobKind.INVOICE_SEND,
        JobKind.INVOICE_DELETE,
        JobKind.SCHEDULE_CREATE,
        JobKind.SCHEDULE_DELETE,
        JobKind.ONE_OFF_PAYMENT_MARK,
        JobKind.ONE_OFF_PAYMENT_RETRY_MARK,
    ):
        Job.objects.bulk_create([Job(kind=kind, entity_type="one_off_charge", entity_id=charge.pk)])
    for old_kind, entity in (
        ("setup_payment_mark", "one_off_charge"),
        ("one_off_payment_mark", "setup_charge"),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            Job.objects.bulk_create([Job(kind=old_kind, entity_type=entity, entity_id=charge.pk)])
    with pytest.raises(IntegrityError), transaction.atomic():
        Payment.objects.bulk_create(
            [
                Payment(
                    one_off_charge=charge,
                    kind="setup",
                    method=PaymentMethod.WIRE,
                    amount=D("1.00"),
                    status=PaymentStatus.SETTLED,
                    note="Wire.",
                )
            ]
        )


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the trigger exists on PostgreSQL")
@pytest.mark.parametrize("column", ["description", "void_note", "note", "vendor"])
def test_the_trigger_guards_the_new_and_the_renamed_columns(
    fake: FakeProvider, column: str
) -> None:
    from django.db import DatabaseError, transaction

    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    with pytest.raises(DatabaseError), transaction.atomic():
        OneOffCharge.objects.filter(pk=charge.pk).update(**{column: f"x {CARD}"})


# --- More of D3, D5, D6 ---------------------------------------------------------------------


def test_a_send_invoice_webhook_marks_a_one_off_invoice_sent(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL))
    run_queued_jobs()
    invoice = invoice_of(charge)
    ref = provider_ref(invoice)
    assert invoice.internal_status == InvoiceStatus.READY
    fake.invoices[ref] = replace(fake.invoices[ref], raw_status="SENT")
    assert deliver("send_invoice", id=ref) == "processed"
    assert invoice_of(charge).internal_status == InvoiceStatus.SENT


def test_a_schedule_that_already_generated_its_invoice_is_not_voided(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.AUTOPAY_DUE, "450.00"))
    run_queued_jobs()
    schedule = service.provider_schedule(charge)
    assert service.cancel_charge(charge.pk, note="Paid by wire.", actor=None).ok
    fake.invoices["fake-fired-1"] = InvoiceRecord(
        external_id="fake-fired-1",
        raw_status="SENT",
        fetched_at=timezone.now(),
        customer_external_id=CUSTOMER,
        total=D("450.00"),
        schedule_id=schedule,
    )
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, one_off_invoices.SCHEDULE_FIRED)
    assert fresh(charge).status == OneOffChargeStatus.OWED


def test_the_create_pages_refuse_a_viewer(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = make_lease(operator=operator)
    charge = created(operator, item(CollectionMethod.MANUAL))
    viewer, _ = client_for(Role.VIEWER)
    for path in (
        f"/operators/{operator.pk}/charges/new/",
        f"/leases/{lease.pk}/charges/new/",
        f"/one-off-charges/{charge.pk}/cancel/",
    ):
        assert viewer.get(path).status_code == 403, path
    page = viewer.get(f"/one-off-charges/{charge.pk}/").content.decode()
    assert 'id="cancel-charge"' not in page and 'id="retry-provider"' not in page
    for listing in (f"/operators/{operator.pk}/", f"/leases/{lease.pk}/"):
        assert 'id="new-invoice"' not in viewer.get(listing).content.decode()


def test_the_billing_cancel_page_names_the_refusal(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    charge = created(operator, item(CollectionMethod.MANUAL, "100.00"))
    service.record_payment(
        charge.pk,
        method=PaymentMethod.WIRE,
        amount=D("100.00"),
        paid_on=today(),
        note="Wire.",
        actor=None,
    )
    client = client_for(Role.BILLING)[0]
    page = client.get(f"/one-off-charges/{charge.pk}/cancel/").content.decode()
    assert service.CANCEL_PAID.split(":")[0] in page
    client.post(f"/one-off-charges/{charge.pk}/cancel/", {"note": "Try anyway."})
    assert not Job.objects.filter(kind=JobKind.INVOICE_DELETE).exists()
    assert fresh(charge).status == OneOffChargeStatus.PAID


def test_a_wire_payment_recorded_here_marks_the_one_off_invoice_paid_at_the_provider(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item())
    run_queued_jobs()
    client, _ = client_for(Role.BILLING)
    client.post(
        f"/one-off-charges/{charge.pk}/payments/new/",
        {
            "creation_key": str(uuid.uuid4()),
            "method": PaymentMethod.WIRE.value,
            "amount": "3000.00",
            "paid_on": today().isoformat(),
            "reference": "WIRE-778",
            "note": "Wire received for the arrears.",
        },
    )
    assert latest_job(charge).kind == JobKind.ONE_OFF_PAYMENT_MARK
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE, job.result
    [(ref, offline)] = calls(fake, "mark_invoice_paid_cash_or_check")
    assert ref == provider_ref(invoice_of(charge))
    assert getattr(offline, "check_number", "") == "WIRE-778"
    assert "Paid by wire" in getattr(offline, "note", "")
    paid = fresh(charge)
    assert (paid.status, paid.amount_paid) == (OneOffChargeStatus.PAID, D("3000.00"))
    assert invoice_of(charge).provider_status == "PAID"


def test_a_single_charge_paid_in_two_parts_is_still_flagged_and_never_marked(
    fake: FakeProvider, worker: None
) -> None:
    """PLAN-012c-a D2 is the builder invoice's rule: a one-off charge on an invoice of its
    own keeps the whole-payment rule, and its invoices are its own (D1)."""
    charge = created(an_operator(fake), item(amount="300.00"))
    run_queued_jobs()
    assert list(service.invoices_of(charge)) == [invoice_of(charge)]
    assert not service.is_builder_invoice(invoice_of(charge))
    for amount in ("100.00", "200.00"):
        outcome = service.record_payment(
            charge.pk, method="wire", amount=D(amount), paid_on=today(), note="Wire.", actor=None
        )
        assert outcome.ok and not outcome.mark_due
    assert fresh(charge).review_message == offline_marking.PARTIAL_AMOUNT
    assert fresh(charge).status == OneOffChargeStatus.PAID
    assert not calls(fake, "mark_invoice_paid_cash_or_check")
