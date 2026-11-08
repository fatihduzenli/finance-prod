"""PLAN-015c task 3 (Opus): the receipt PDF — a bank debit with the cached last four, Zelle
with a masked reference, the applied-to lines, the remaining balance on the store, no receipt
for a payment that is not settled money received, the same bytes for the same data."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ledger.models import (
    InvoiceStatus,
    OneOffCharge,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services.pdf.receipt import (
    NoReceipt,
    has_receipt,
    receipt_filename,
    receipt_number,
    render_receipt,
)
from ledger.validators import validate_no_payment_data
from tests.pdf_helpers import billed_store, builder_invoice, company, pdf_text, settle
from tests.receivables_helpers import a_payment, a_rent_charge, an_invoice

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 11, 30)
THREE = [
    ("Rent — December 2026", "Store #1991 Istanbul · Dec 1–31", "2000.00"),
    ("Insurance 2026–27", "Annual store coverage", "300.00"),
    ("Access fee", "One-time", "500.00"),
]
TRANSACTION = "txn-5c1e-90ab-77fe-4410"


def lines_of(data: bytes) -> list[str]:
    return [line.strip() for line in pdf_text(data).splitlines() if line.strip()]


def paid(charge: OneOffCharge, amount: str) -> None:
    """The charge's stored paid amount, as the payment services recompute it."""
    OneOffCharge.objects.filter(pk=charge.pk).update(amount_paid=D(amount))


def test_a_bank_debit_receipt_names_the_last_four_and_masks_the_transaction_reference() -> None:
    from payments.models import ExternalRef

    company()
    lease = billed_store(last4="6789")
    invoice, charges = builder_invoice(lease, THREE)
    payment = settle(charges[0], "2000.00", date(2026, 11, 28))
    paid(charges[0], "2000.00")
    ExternalRef.objects.create(
        entity_type="payment",
        entity_id=payment.pk,
        provider="stax_pay",
        external_kind="transaction",
        external_id=TRANSACTION,
    )
    data = render_receipt(payment, today=TODAY)
    text = lines_of(data)
    number = receipt_number(payment)
    assert number == f"RCPT-{str(payment.pk)[:8].upper()}"
    for expected in (
        "RECEIPT",
        number,
        "PAID",
        "RECEIVED FROM STORE DATE AMOUNT",
        "Fatih Trading LLC",
        "Fatih Duzenli",
        "#1991 Istanbul",
        "Nov 28, 2026",
        "Bank debit ····6789",
        "txn-…4410",
        f"Invoice {invoice.invoice_number}",
        "Rent — December 2026",
        "Remaining balance on the store",
        "Page 1 of 1",
    ):
        assert expected in text, expected
    assert text[text.index("Method") + 1] == "Bank debit ····6789"
    assert text[text.index("Reference") + 1] == "txn-…4410"
    assert text[text.index("Rent — December 2026") + 1] == "$2,000.00"
    # The other two lines stay open: $800.00 on the store.
    assert text[text.index("Remaining balance on the store") + 1] == "$800.00"
    assert TRANSACTION not in pdf_text(data)
    validate_no_payment_data(pdf_text(data))
    assert receipt_filename(payment) == f"receipt-{number}.pdf"


def test_a_zelle_receipt_masks_its_reference_and_has_no_last_four() -> None:
    company()
    lease = billed_store(last4="6789")
    _, charges = builder_invoice(lease, THREE)
    payment = settle(
        charges[2], "500.00", date(2026, 11, 3), PaymentMethod.ZELLE, reference="ZL-778812-QX"
    )
    text = lines_of(render_receipt(payment, today=TODAY))
    assert text[text.index("Method") + 1] == "Zelle"
    assert text[text.index("Reference") + 1] == "ZL-7…2-QX"
    assert "ZL-778812-QX" not in pdf_text(render_receipt(payment, today=TODAY))
    assert "6789" not in pdf_text(render_receipt(payment, today=TODAY))
    assert text[text.index("Access fee") + 1] == "$500.00"
    assert "Nov 3, 2026" in text


def test_a_payment_without_a_reference_leaves_the_reference_row_out() -> None:
    company()
    _, charges = builder_invoice(billed_store(), THREE)
    payment = settle(charges[1], "300.00", date(2026, 11, 3), PaymentMethod.CASH)
    text = lines_of(render_receipt(payment, today=TODAY))
    assert text[text.index("Method") + 1] == "Cash"
    assert "Reference" not in text


def test_a_rent_payment_is_applied_to_its_rent_run_invoice_and_its_month() -> None:
    company()
    lease = billed_store()
    assert lease.operator is not None
    rent = a_rent_charge(lease, date(2026, 11, 1), due="1850.00", paid="1850.00", status="paid")
    invoice = an_invoice(
        lease.operator, 77, "1850.00", rent_charge=rent, internal_status=InvoiceStatus.SENT
    )
    payment = a_payment(rent, "1850.00", date(2026, 11, 5))
    text = lines_of(render_receipt(payment, today=TODAY))
    applied = text.index("APPLIED TO")
    assert text[applied + 1 : applied + 4] == [
        f"Invoice {invoice.invoice_number}",
        "Rent — November 2026",
        "$1,850.00",
    ]
    assert text[text.index("Remaining balance on the store") + 1] == "$0.00"


def test_a_payment_of_a_charge_without_an_invoice_names_the_charge_only() -> None:
    company()
    lease = billed_store()
    rent = a_rent_charge(lease, date(2026, 10, 1), due="1850.00")
    payment = a_payment(rent, "850.00", date(2026, 10, 5))
    text = lines_of(render_receipt(payment, today=TODAY))
    applied = text.index("APPLIED TO")
    assert text[applied + 1 : applied + 3] == ["Rent — October 2026", "$850.00"]
    assert not [line for line in text if line.startswith("Invoice ")]
    # The month's rent is still open for the rest.
    assert text[text.index("Remaining balance on the store") + 1] == "$1,850.00"


def test_a_void_invoice_is_not_named_on_the_receipt_of_its_charge() -> None:
    company()
    lease = billed_store()
    assert lease.operator is not None
    rent = a_rent_charge(lease, date(2026, 11, 1), due="1850.00")
    an_invoice(lease.operator, 79, "1850.00", rent_charge=rent, internal_status=InvoiceStatus.VOID)
    payment = a_payment(rent, "100.00", date(2026, 11, 5))
    text = lines_of(render_receipt(payment, today=TODAY))
    assert not [line for line in text if line.startswith("Invoice ")]


@pytest.mark.parametrize(
    "status", [PaymentStatus.RETURNED, PaymentStatus.VOIDED, PaymentStatus.PENDING]
)
def test_a_payment_that_is_not_settled_has_no_receipt(status: str) -> None:
    company()
    _, charges = builder_invoice(billed_store(), THREE)
    payment = settle(charges[0], "100.00", date(2026, 11, 3), status=status)
    assert has_receipt(payment) is False
    with pytest.raises(NoReceipt):
        render_receipt(payment, today=TODAY)


def test_a_refund_and_money_paid_back_have_no_receipt() -> None:
    company()
    lease = billed_store()
    _, charges = builder_invoice(lease, THREE)
    original = settle(charges[0], "100.00", date(2026, 11, 3))
    refund = settle(
        charges[0], "100.00", date(2026, 11, 4), is_refund=True, parent_payment=original
    )
    paid_back = Payment.objects.create(
        one_off_charge=charges[1],
        kind=PaymentKind.REFUND_DEPOSIT,
        method=PaymentMethod.CHECK,
        amount=D("50.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 11, 5),
        note="Deposit back",
    )
    for payment in (refund, paid_back):
        assert has_receipt(payment) is False
        with pytest.raises(NoReceipt):
            render_receipt(payment, today=TODAY)
    assert has_receipt(original) is True


def test_the_same_payment_gives_the_same_bytes() -> None:
    company()
    _, charges = builder_invoice(billed_store(), THREE)
    payment = settle(charges[0], "2000.00", date(2026, 11, 28))
    first = render_receipt(payment, today=TODAY)
    assert first.startswith(b"%PDF")
    assert render_receipt(payment, today=TODAY) == first
