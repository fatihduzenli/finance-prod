"""Record a payment with manual allocation, lease credits and Schedule a payment
(docs/qa/PLAN-012d-store-workspace.md §3.2 D6, D6a, D6b, D2 as amended; §6 B; ADR-0013).

The allocations must add up (or the rest is kept as a credit), the single-charge pre-fill,
the credit kept and applied later (method credit), the Stax paid-as-cash marking queued
when a charge's invoice is fully covered (a builder invoice once every line is), the
partial flag, an insurance allocation, the replayed key, the P&L footer, the lease-end
statement's credit line and its refund at confirm, the transfer blocker, receivables
unchanged by credits, the scheduled payment, the header and the Payments tab, the query
budget of the form page. The adversarial cases are the QA owner's
(test_plan012d_b_adversarial.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    FeeType,
    InsuranceCharge,
    Invoice,
    InvoiceDraft,
    InvoiceLineItem,
    Lease,
    LeaseCredit,
    OneOffCategory,
    OneOffCharge,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RefundMethod,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import (
    credits,
    lease_end,
    payments_entry,
    pnl,
    receivables,
    statements,
    transfer,
    workspace,
)
from payments import offline_marking
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind
from payments.provider import InvoiceRecord, OfflinePayment
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
ZERO = D("0.00")
TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
NOTE = "Zelle from the operator, 15 October."


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    with override_settings(STAX_CALLER="worker"):
        yield provider


# --- Builders --------------------------------------------------------------------------------


def a_world(label: str = "") -> Lease:
    """A store with a wire lease from 1 August 2026 and its operator."""
    lease = a_lease(
        a_store(label), an_operator(), started=date(2026, 8, 1), channel=PaymentChannel.WIRE
    )
    assert lease.operator is not None
    return lease


def operator_of(lease: Lease) -> Operator:
    assert lease.operator is not None
    return lease.operator


def ref(entity_type: str, entity_id: uuid.UUID, kind: str, value: str = "") -> str:
    external = value or f"fake-{kind}-{uuid.uuid4().hex[:8]}"
    ExternalRef.objects.create(
        entity_type=entity_type,
        entity_id=entity_id,
        provider="stax_pay",
        external_kind=kind,
        external_id=external,
    )
    return external


def linked(operator: Operator, *, bank: bool = True) -> Operator:
    """The operator with a provider customer and, with ``bank``, a linked bank method."""
    ref("operator", operator.pk, "customer")
    if bank:
        ref("operator", operator.pk, "payment_method")
    return operator


def an_invoice(
    lease: Lease, total: str, *, number: int, live: bool = True, **charge: Any
) -> tuple[Invoice, str]:
    invoice = Invoice.objects.create(
        invoice_number=f"INV-2026-{number:04d}",
        operator=operator_of(lease),
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 5),
        subtotal=D(total),
        total=D(total),
        **charge,
    )
    external = ref("invoice", invoice.pk, "invoice") if live else ""
    return invoice, external


def a_builder_invoice(
    lease: Lease, prices: tuple[str, ...], number: int = 801
) -> tuple[list[OneOffCharge], Invoice, str]:
    """A builder invoice of one-off lines (PLAN-012 D6): linked to the first line, its
    line items naming every line's charge."""
    lines = [
        a_one_off_charge(
            operator_of(lease),
            price,
            date(2026, 9, 10),
            lease=lease,
            collection="email",
            description=f"Line {position}",
        )
        for position, price in enumerate(prices, start=1)
    ]
    total = str(sum((D(price) for price in prices), ZERO))
    invoice, external = an_invoice(lease, total, number=number, one_off_charge=lines[0])
    for position, (line, price) in enumerate(zip(lines, prices, strict=True), start=1):
        InvoiceLineItem.objects.create(
            invoice=invoice,
            position=position,
            description=line.description,
            quantity=D("1"),
            unit_price=D(price),
            line_total=D(price),
            one_off_charge=line,
        )
    return lines, invoice, external


def url(lease: Lease) -> str:
    return reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id})


def form(
    amount: str,
    allocations: dict[Any, str] | None = None,
    *,
    credit: dict[Any, str] | None = None,
    **extra: str,
) -> dict[str, str]:
    data = {
        "creation_key": str(uuid.uuid4()),
        "method": "zelle",
        "amount": amount,
        "paid_on": TODAY.isoformat(),
        "reference": "ZL-4410",
        "note": NOTE,
    }
    for pk, value in (allocations or {}).items():
        data[f"alloc_{pk}"] = value
    for pk, value in (credit or {}).items():
        data[f"credit_{pk}"] = value
    data.update(extra)
    return data


def payments_tab(lease: Lease) -> str:
    return workspace.tab_url(lease.store_id, workspace.TAB_PAYMENTS)


def counts() -> tuple[int, int, int, int]:
    return (
        Payment.objects.count(),
        LeaseCredit.objects.count(),
        AuditLog.objects.count(),
        Job.objects.count(),
    )


def refused(client: Client, lease: Lease, data: dict[str, str]) -> str:
    """POST ``data``: refused, back on the form, nothing written; the refusal's text."""
    before = counts()
    response = client.post(url(lease), data)
    assert (response.status_code, response["Location"]) == (302, url(lease)), data
    assert counts() == before
    page = client.get(url(lease)).content.decode()
    return page


def billing() -> Client:
    return client_for(Role.BILLING)[0]


# --- The form (D6) ---------------------------------------------------------------------------


def test_the_form_lists_the_open_charges_oldest_first_with_their_inputs() -> None:
    lease = a_world("2477")
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    other = a_one_off_charge(operator_of(lease), "250.00", date(2026, 8, 20), lease=lease)
    insurance = an_insurance_charge(lease, 2026, date(2026, 10, 1))
    a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.PAID, "1000.00", "1000.00")
    html = billing().get(url(lease)).content.decode()
    order = [html.index(f'name="alloc_{pk}"') for pk in (other.pk, rent.pk, insurance.pk)]
    assert order == sorted(order)
    assert 'id="unallocated"' in html and 'name="keep_credit"' in html
    assert "Store 2477 · Lease from Aug 1, 2026" in html
    # No credit held: no "From credit" inputs; nothing is pre-filled with three charges.
    assert 'name="credit_' not in html and "data-prefill" not in html
    assert 'name="amount"' in html and 'value=""' not in html.split('name="amount"')[1][:40]


def test_one_open_charge_is_prefilled_by_the_script_and_when_the_form_comes_back() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    client = billing()
    html = client.get(url(lease)).content.decode()
    # The amount is empty on the first GET: the script fills min(amount, balance).
    assert f'data-prefill="{rent.balance}"' in html
    # Refused (a note missing), the form comes back with the allocation filled.
    data = form("1200.00", note="")
    response = client.post(url(lease), data)
    assert response.status_code == 302
    again = client.get(url(lease)).content.decode()
    assert f'name="alloc_{rent.pk}"' in again and 'value="1000.00"' in again
    assert "A note is required" in again
    # The server recomputes the unallocated amount for the form it renders again.
    assert 'id="unallocated">$200.00<' in again


def test_allocations_must_add_up_and_stay_within_each_balance() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    other = a_one_off_charge(operator_of(lease), "250.00", date(2026, 9, 20), lease=lease)
    client = billing()
    short = refused(client, lease, form("1250.00", {rent.pk: "1000.00", other.pk: "200.00"}))
    assert "add up to $1,200.00, not the $1,250.00 received" in short
    over = refused(client, lease, form("1000.00", {rent.pk: "1000.00", other.pk: "50.00"}))
    assert "more than the $1,000.00 received" in over
    negative = refused(client, lease, form("1000.00", {rent.pk: "1050.00", other.pk: "-50.00"}))
    assert "cannot be negative" in negative
    above = refused(client, lease, form("1300.00", {rent.pk: "1050.00", other.pk: "250.00"}))
    assert "is more than its balance of $1,000.00" in above
    cents = refused(client, lease, form("10.005", {rent.pk: "10.005"}))
    assert "decimal" in cents.lower()
    zero = refused(client, lease, form("0.00", {}, keep_credit="on"))
    assert "The amount must be more than zero." in zero
    future = refused(client, lease, form("10.00", {rent.pk: "10.00"}, paid_on="2026-10-16"))
    assert "cannot be in the future" in future
    forged = refused(client, lease, form("10.00", {uuid.uuid4(): "10.00"}))
    assert "is not an open charge of this store" in forged
    assert (RentCharge.objects.get(pk=rent.pk).amount_paid, LeaseCredit.objects.count()) == (
        ZERO,
        0,
    )


def test_a_split_payment_writes_one_payment_per_charge_in_one_group() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    other = a_one_off_charge(operator_of(lease), "250.00", date(2026, 9, 20), lease=lease)
    data = form("1100.00", {rent.pk: "900.00", other.pk: "200.00"})
    response = billing().post(url(lease), data)
    assert (response.status_code, response["Location"]) == (302, payments_tab(lease))
    rows = list(Payment.objects.order_by("amount"))
    assert [(row.amount, row.method, row.status) for row in rows] == [
        (D("200.00"), PaymentMethod.ZELLE, PaymentStatus.SETTLED),
        (D("900.00"), PaymentMethod.ZELLE, PaymentStatus.SETTLED),
    ]
    assert len({row.allocation_group for row in rows}) == 1 and rows[0].allocation_group
    assert {(row.reference, row.note, row.settled_on) for row in rows} == {("ZL-4410", NOTE, TODAY)}
    assert (
        RentCharge.objects.get(pk=rent.pk).status,
        OneOffCharge.objects.get(pk=other.pk).status,
    ) == (
        RentChargeStatus.PARTIAL,
        "partial",
    )
    key = uuid.UUID(data["creation_key"])
    entry = AuditLog.objects.get(action=payments_entry.RECORDED_ACTION)
    assert (entry.entity_type, entry.entity_id) == ("lease", str(lease.pk))
    assert entry.after is not None and entry.after["creation_key"] == str(key)
    assert {line["charge"] for line in entry.after["lines"]} == {str(rent.pk), str(other.pk)}
    recorded = AuditLog.objects.get(action="rent_charge.payment_recorded")
    assert recorded.after is not None
    assert recorded.after["creation_key"] == str(payments_entry.line_key(key, rent.pk))


def test_a_replayed_key_returns_the_first_result_and_writes_nothing() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    client = billing()
    data = form("1000.00", {rent.pk: "1000.00"})
    assert client.post(url(lease), data)["Location"] == payments_tab(lease)
    before = counts()
    again = client.post(url(lease), data)
    assert again["Location"] == payments_tab(lease)
    assert counts() == before
    follow = client.get(payments_tab(lease)).content.decode()
    assert "already recorded" in follow


# --- Credits (D6a, D6b) ---------------------------------------------------------------------


def test_keep_credit_creates_the_credit_shown_on_the_header_and_the_payments_tab() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    open_before = receivables.open_balances(TODAY)
    response = billing().post(url(lease), form("1300.00", {rent.pk: "1000.00"}, keep_credit="on"))
    assert response["Location"] == payments_tab(lease)
    credit = LeaseCredit.objects.get(lease=lease)
    assert (credit.amount, credit.remaining, credit.method, credit.received_on) == (
        D("300.00"),
        D("300.00"),
        PaymentMethod.ZELLE,
        TODAY,
    )
    assert credit.allocation_group == Payment.objects.get().allocation_group
    assert (credit.note, credit.reference) == (NOTE, "ZL-4410")
    assert AuditLog.objects.filter(action=credits.CREATED_ACTION).count() == 1
    assert credits.balance(lease) == D("300.00")
    # Receivables ignore credits: only the rent payment lowered what is owed.
    after = receivables.open_balances(TODAY)
    assert receivables.total(after) == receivables.total(open_before) - D("1000.00")
    viewer = client_for(Role.VIEWER)[0]
    store_page = reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    assert 'id="header-credit">$300.00<' in viewer.get(store_page).content.decode()
    tab = viewer.get(store_page + "?tab=payments").content.decode()
    assert 'id="store-credits"' in tab and "$300.00" in tab and "Not used yet" in tab


def test_the_whole_amount_can_be_kept_as_a_credit_without_an_open_charge() -> None:
    lease = a_world()
    response = billing().post(url(lease), form("500.00", keep_credit="on"))
    assert response["Location"] == payments_tab(lease)
    assert Payment.objects.count() == 0
    assert LeaseCredit.objects.get().remaining == D("500.00")


def test_a_later_form_applies_the_credit_as_a_credit_payment() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    other = a_one_off_charge(operator_of(lease), "250.00", date(2026, 9, 20), lease=lease)
    client = billing()
    assert client.post(url(lease), form("400.00", {other.pk: "250.00"}, keep_credit="on"))
    credit = LeaseCredit.objects.get()
    html = client.get(url(lease)).content.decode()
    assert 'id="credit-held"' in html and f'name="credit_{rent.pk}"' in html
    # Only the credit: the amount is left empty.
    data = form("", credit={rent.pk: "120.00"})
    response = client.post(url(lease), data)
    assert response["Location"] == payments_tab(lease), client.get(url(lease)).content[:0]
    applied = Payment.objects.get(method=PaymentMethod.CREDIT)
    assert (applied.amount, applied.rent_charge_id, applied.credit_id, applied.kind) == (
        D("120.00"),
        rent.pk,
        credit.pk,
        PaymentKind.RENT,
    )
    credit.refresh_from_db()
    assert credit.remaining == D("30.00")
    assert RentCharge.objects.get(pk=rent.pk).amount_paid == D("120.00")
    assert AuditLog.objects.filter(action=credits.APPLIED_ACTION).count() == 1
    # A partial credit on a charge without a provider invoice marks nothing.
    assert not Job.objects.exists()
    # The ledger names the method "Credit".
    tab = client.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) + "?tab=payments"
    )
    assert '<td data-role="meta" data-label="Method">Credit</td>' in tab.content.decode()
    # More than the credit held is refused, nothing written.
    page = refused(client, lease, form("", credit={rent.pk: "30.01"}))
    assert "more than the credit held" in page


def test_a_credit_covering_a_charges_invoice_queues_the_marking_with_a_credit_memo(
    fake: FakeProvider,
) -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    invoice, external = an_invoice(lease, "1000.00", number=701, rent_charge=rent)
    fake.invoices[external] = InvoiceRecord(
        external_id=external, raw_status="FAKE-SENT", fetched_at=NOW, total=D("1000.00")
    )
    credits.create(
        lease,
        D("1000.00"),
        method=PaymentMethod.WIRE,
        received_on=date(2026, 10, 1),
        reference="",
        note="Kept from an earlier wire.",
        allocation_group=uuid.uuid4(),
        actor=None,
    )
    data = form("", credit={rent.pk: "1000.00"})
    assert billing().post(url(lease), data)["Location"] == payments_tab(lease)
    job = Job.objects.get()
    payment = Payment.objects.get()
    credit = LeaseCredit.objects.get()
    assert (job.kind, job.entity_id, job.params) == (
        JobKind.PAYMENT_MARK,
        rent.pk,
        {"payment": str(payment.pk)},
    )
    assert job.replay_key == payments_entry.credit_key(
        uuid.UUID(data["creation_key"]), credit.pk, rent.pk
    )
    run_queued_jobs()
    [(name, args)] = [call for call in fake.calls if call[0] == "mark_invoice_paid_cash_or_check"]
    mark = args[1]
    assert isinstance(mark, OfflinePayment)
    assert args[0] == external and mark.note.startswith("Paid by credit on 10/15/2026.")
    assert offline_marking.METHOD_WORDS["credit"] == "credit"
    assert Invoice.objects.get(pk=invoice.pk).provider_status == "PAID"


# --- Stax marking per charge (D6a) ----------------------------------------------------------


def test_an_allocation_covering_an_invoice_queues_paid_as_cash_and_a_partial_one_flags() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    other = OneOffCharge.objects.create(
        operator=operator_of(lease),
        lease=lease,
        category=OneOffCategory.DAMAGE,
        collection="email",
        description="Broken shelf",
        price=D("300.00"),
        due_date=date(2026, 9, 20),
    )
    an_invoice(lease, "1000.00", number=702, rent_charge=rent)
    an_invoice(lease, "300.00", number=703, one_off_charge=other)
    data = form("1200.00", {rent.pk: "1000.00", other.pk: "200.00"})
    assert billing().post(url(lease), data)["Location"] == payments_tab(lease)
    [job] = Job.objects.all()
    rent_payment = Payment.objects.get(rent_charge=rent)
    assert (job.kind, job.entity_id, job.params, job.replay_key) == (
        JobKind.PAYMENT_MARK,
        rent.pk,
        {"payment": str(rent_payment.pk)},
        payments_entry.line_key(uuid.UUID(data["creation_key"]), rent.pk),
    )
    flagged = OneOffCharge.objects.get(pk=other.pk)
    assert flagged.needs_review and flagged.review_message == offline_marking.PARTIAL_AMOUNT
    assert AuditLog.objects.filter(action="one_off_charge.provider_not_updated").count() == 1


def test_a_builder_invoice_is_marked_once_every_line_is_covered() -> None:
    lease = a_world()
    lines, _, _ = a_builder_invoice(lease, ("120.00", "80.00"))
    data = form("200.00", {lines[0].pk: "120.00", lines[1].pk: "80.00"})
    assert billing().post(url(lease), data)["Location"] == payments_tab(lease)
    [job] = Job.objects.all()
    assert job.kind == JobKind.ONE_OFF_PAYMENT_MARK
    assert job.params == {"payment": str(Payment.objects.get(one_off_charge=lines[1]).pk)}
    # One line alone leaves the invoice uncovered: no marking, the line flagged.
    other = a_world()
    first, _, _ = a_builder_invoice(other, ("120.00", "80.00"), number=802)
    assert billing().post(url(other), form("120.00", {first[0].pk: "120.00"}))
    assert Job.objects.count() == 1
    assert OneOffCharge.objects.get(pk=first[0].pk).review_message == offline_marking.PARTIAL_AMOUNT


def test_an_insurance_allocation_is_an_insurance_payment_and_never_calls_the_provider() -> None:
    lease = a_world()
    charge = an_insurance_charge(lease, 2026, date(2026, 10, 1))
    plain = an_insurance_charge(lease, 2025, date(2025, 10, 1), amount="600.00")
    an_invoice(lease, "695.00", number=704, insurance_charge=charge)
    data = form("995.00", {charge.pk: "695.00", plain.pk: "300.00"})
    assert billing().post(url(lease), data)["Location"] == payments_tab(lease)
    payment = Payment.objects.get(insurance_charge=charge)
    assert (payment.kind, payment.policy_year, payment.amount) == (
        PaymentKind.INSURANCE,
        2026,
        D("695.00"),
    )
    owed = {row.charge_id: row.balance for row in receivables.open_balances(TODAY)}
    assert charge.pk not in owed and owed[plain.pk] == D("300.00")
    assert not Job.objects.exists()
    assert InsuranceCharge.objects.get(pk=charge.pk).needs_review
    assert not InsuranceCharge.objects.get(pk=plain.pk).needs_review
    not_updated = AuditLog.objects.get(action="insurance_charge.provider_not_updated")
    assert not_updated.entity_id == str(charge.pk)
    assert AuditLog.objects.filter(action="insurance_charge.payment_recorded").count() == 2
    # Above the balance (what is left) is refused.
    refused(billing(), lease, form("300.01", {plain.pk: "300.01"}))


# --- Elsewhere (D6b) ------------------------------------------------------------------------


def test_the_pnl_footer_shows_credits_held_and_an_application_counts_as_collected() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    client = billing()
    assert client.post(url(lease), form("300.00", keep_credit="on"))
    assert credits.held(date(2026, 10, 14)) == ZERO
    assert credits.held(TODAY) == D("300.00")
    query = pnl.parse_query({"from": "2026-09", "to": "2026-10"}, today=TODAY)
    rows = [row for row in pnl.rows(query) if row.store_id == lease.store_id]
    assert sum((row.rent_collected for row in rows), ZERO) == ZERO
    assert client.post(url(lease), form("", credit={rent.pk: "200.00"}))
    rows = [row for row in pnl.rows(query) if row.store_id == lease.store_id]
    assert sum((row.rent_collected for row in rows), ZERO) == D("200.00")
    assert credits.held(TODAY) == D("100.00")
    html = client.get(reverse("ledger:reports:pnl") + "?from=2026-09&to=2026-10").content.decode()
    assert 'id="credits-held-amount">100.00<' in html.replace("$", "")


def test_the_lease_end_lists_the_credit_as_a_refund_due_and_pays_it_back() -> None:
    from tests.test_lease_end import ENDED, a_store_lease, draft, settle
    from tests.test_lease_end import TODAY as END_TODAY

    lease = a_store_lease()
    credit = credits.create(
        lease,
        D("150.00"),
        method=PaymentMethod.ZELLE,
        received_on=date(2026, 10, 2),
        reference="ZL-1",
        note="Paid twice.",
        allocation_group=uuid.uuid4(),
        actor=None,
    )
    end = draft(lease, ended_on=ENDED)
    found = statements.final(end, today=END_TODAY)
    assert found.lease_credit == D("150.00") and found.refund_due >= D("150.00")
    assert [line.credit_id for line in found.held_credits] == [credit.pk]
    assert "Credit on the lease" in [row[1] for row in statements._lines(found)]
    assert statements.from_json(found.to_json()) == found
    assert settle(end, RefundMethod.WIRE).ok
    outcome = lease_end.confirm(end, None, confirmed=True, today=END_TODAY)
    assert outcome.ok, outcome.message
    refund = Payment.objects.get(kind=PaymentKind.CREDIT_REFUND)
    assert (refund.amount, refund.method, refund.credit_id, refund.reference) == (
        D("150.00"),
        PaymentMethod.WIRE,
        credit.pk,
        "ZL-77821",
    )
    assert LeaseCredit.objects.get(pk=credit.pk).remaining == ZERO
    assert AuditLog.objects.filter(action=credits.REFUNDED_ACTION).count() == 1
    assert credits.held(END_TODAY) == ZERO
    # Statements kept before the credit line read as having none.
    old = found.to_json()
    del old["held_credits"], old["lease_credit"]
    assert statements.from_json(old).lease_credit == ZERO


def test_a_transfer_is_refused_while_a_credit_remains() -> None:
    from tests.test_transfer import a_clean_lease, begin

    lease = a_clean_lease()
    row = begin(lease)
    assert transfer.blockers(row) == [] or all(
        item.kind != transfer.CREDIT_HELD for item in transfer.blockers(row)
    )
    credits.create(
        lease,
        D("75.50"),
        method=PaymentMethod.OTHER,
        received_on=date(2026, 10, 1),
        reference="",
        note="Overpaid.",
        allocation_group=uuid.uuid4(),
        actor=None,
    )
    [blocker] = [
        item for item in transfer.balance_blockers(row) if item.kind == transfer.CREDIT_HELD
    ]
    assert blocker.text == (
        "A credit of $75.50 remains on the outgoing operator: refund it in the lease end first."
    )
    assert blocker.url == workspace.tab_url(lease.store_id, workspace.TAB_PAYMENTS)


def test_cash_reports_leave_credits_out() -> None:
    lease = a_world()
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    client = billing()
    assert client.post(url(lease), form("300.00", {rent.pk: "100.00"}, keep_credit="on"))
    assert client.post(url(lease), form("", credit={rent.pk: "200.00"}))
    assert receivables.paid_in(date(2026, 10, 1), date(2026, 10, 31)) == D("100.00")
    from ledger.services import reports

    selected = reports.parse_range({"from": "2026-10", "to": "2026-10"}, today=TODAY)
    by_method = reports.payments_by_method(selected)
    assert by_method.total.amounts == {
        "ach": ZERO,
        "zelle": D("100.00"),
        "wire": ZERO,
        "check": ZERO,
        "other": ZERO,
    }


# --- Schedule a payment (D2, Batch B; the invoice builder's since PLAN-012e-2 D4) ----------


def schedule_url(lease: Lease) -> str:
    return reverse("ledger:stores:schedule_payment", kwargs={"pk": lease.store_id})


def builder_url(lease: Lease) -> str:
    """Where Schedule a payment goes: the store's New invoice page, Schedule for a date
    chosen (PLAN-012e-2 D4)."""
    return (
        reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})
        + f"?store={lease.store_id}&lease={lease.pk}&collect=schedule"
    )


def schedule_form(lease: Lease, item: str = "Sign repair", **extra: str) -> dict[str, str]:
    """The builder's form with one line and Schedule for a date (PLAN-012e-2 D3)."""
    data = {
        "creation_key": str(uuid.uuid4()),
        "lease": str(lease.pk),
        "memo": "",
        "due_date": "",
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "50",
        "lines-0-item": item,
        "lines-0-description": extra.pop("description", ""),
        "lines-0-unit_amount": extra.pop("amount", "150.00"),
        "collect": "schedule",
        "schedule_on": "2026-10-20",
    }
    data.update(extra)
    return data


def test_schedule_a_payment_creates_an_autopay_charge_and_queues_its_schedule() -> None:
    lease = a_world()
    linked(operator_of(lease))
    client = billing()
    opened = client.get(schedule_url(lease))
    assert (opened.status_code, opened["Location"]) == (302, builder_url(lease))
    html = client.get(builder_url(lease)).content.decode()
    assert 'id="collect-schedule" checked' in html and 'id="id_schedule_on"' in html
    data = schedule_form(lease, description="North wall")
    response = client.post(builder_url(lease), data)
    tab = workspace.tab_url(lease.store_id, workspace.TAB_CHARGES)
    assert (response.status_code, response["Location"]) == (302, tab)
    charge = OneOffCharge.objects.get()
    assert (
        charge.collection,
        charge.category,
        charge.description,
        charge.price,
        charge.due_date,
        charge.lease_id,
    ) == (
        CollectionMethod.AUTOPAY_DUE,
        OneOffCategory.OTHER,
        "Sign repair: North wall",
        D("150.00"),
        date(2026, 10, 20),
        lease.pk,
    )
    job = Job.objects.get()
    assert (job.kind, job.entity_id) == (JobKind.SCHEDULE_CREATE, charge.pk)
    # The same key again creates nothing.
    client.post(builder_url(lease), data)
    assert (OneOffCharge.objects.count(), Job.objects.count()) == (1, 1)


def test_a_catalogue_item_keeps_its_fee_type() -> None:
    lease = a_world()
    linked(operator_of(lease))
    fee = FeeType.objects.create(code=f"sign-{uuid.uuid4().hex[:6]}", name="Sign", active=True)
    assert billing().post(builder_url(lease), schedule_form(lease, "  sign "))
    charge = OneOffCharge.objects.get()
    assert (charge.category, charge.fee_type_id, charge.description) == (
        OneOffCategory.SETUP,
        fee.pk,
        "Sign",
    )


@pytest.mark.parametrize(
    ("customer", "bank", "sentence"),
    [(False, False, "not connected to Stax"), (True, False, "no linked bank method")],
)
def test_schedule_a_payment_is_refused_without_a_customer_or_a_bank_method(
    customer: bool, bank: bool, sentence: str
) -> None:
    lease = a_world()
    if customer:
        linked(operator_of(lease), bank=bank)
    client = billing()
    response = client.post(builder_url(lease), schedule_form(lease))
    # The draft is kept, with the sentence on its page.
    draft = InvoiceDraft.objects.get()
    assert response["Location"].startswith(f"/invoices/drafts/{draft.pk}/")
    assert not OneOffCharge.objects.exists() and not Job.objects.exists()
    assert sentence in client.get(response["Location"]).content.decode()


def test_schedule_a_payment_refuses_today_and_needs_the_large_debit_tick(settings: Any) -> None:
    settings.LARGE_DEBIT_WARNING_AMOUNT = D("1000.00")
    lease = a_world()
    linked(operator_of(lease))
    client = billing()
    for day in ("2026-10-14", TODAY.isoformat()):
        past = client.post(builder_url(lease), schedule_form(lease, schedule_on=day))
        assert past["Location"] == builder_url(lease)
    assert not InvoiceDraft.objects.exists()
    large = client.post(builder_url(lease), schedule_form(lease, amount="1500.00"))
    draft = InvoiceDraft.objects.get()
    assert large["Location"].startswith(f"/invoices/drafts/{draft.pk}/")
    assert not OneOffCharge.objects.exists()
    ticked = client.post(
        f"/invoices/drafts/{draft.pk}/edit/",
        schedule_form(lease, amount="1500.00", confirm_large_debit="on"),
    )
    assert ticked["Location"] == workspace.tab_url(lease.store_id, workspace.TAB_CHARGES)
    assert OneOffCharge.objects.get().price == D("1500.00")


def test_viewers_cannot_open_either_form_and_a_vacant_store_has_nothing_to_pay() -> None:
    lease = a_world()
    viewer = client_for(Role.VIEWER)[0]
    assert viewer.get(url(lease)).status_code == 403
    assert viewer.get(schedule_url(lease)).status_code == 403
    vacant = a_store()
    for name in ("ledger:stores:record_payment", "ledger:stores:schedule_payment"):
        response = billing().get(reverse(name, kwargs={"pk": vacant.pk}))
        assert response["Location"] == workspace.tab_url(vacant.pk, workspace.TAB_CHARGES)


# --- The query budget --------------------------------------------------------------------------


def test_the_form_page_reads_a_fixed_number_of_queries() -> None:
    lease = a_world()
    client = billing()

    def measured() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(url(lease)).status_code == 200
        return len(queries)

    a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    few = measured()
    for month in (8, 10):
        a_rent_charge(lease, date(2026, month, 1), due="1000.00")
    for day in (3, 4, 5):
        a_one_off_charge(operator_of(lease), "20.00", date(2026, 9, day), lease=lease)
    an_insurance_charge(lease, 2026, date(2026, 10, 1))
    many = measured()
    assert many == few and many <= 25, (few, many)
