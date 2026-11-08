"""Builders and readers for the invoice and receipt PDF tests (PLAN-015c). Not a test module.

:func:`billed_store` writes an operator, a store #1991 "Istanbul" and its lease with the
billing company "Fatih Trading LLC" and the cached last four; :func:`builder_invoice` an
invoice with stored lines over one-off charges of that lease, as the invoice builder writes
them; :func:`settle` a settled payment of a charge. :func:`pdf_text` reads a PDF's text with
pypdf, :func:`page_count` its pages.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.utils import timezone
from pypdf import PdfReader

from ledger.models import (
    CompanySettings,
    Invoice,
    InvoiceLineItem,
    Lease,
    LeaseBilling,
    OneOffCategory,
    OneOffCharge,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from portal.models import PortalUser
from tests.receivables_helpers import a_lease, a_one_off_charge, a_store, an_operator

D = Decimal
ISSUED = date(2026, 11, 15)
DUE = date(2026, 12, 1)


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def page_texts(data: bytes) -> list[str]:
    return [page.extract_text() for page in PdfReader(io.BytesIO(data)).pages]


def page_count(data: bytes) -> int:
    return len(PdfReader(io.BytesIO(data)).pages)


def company(**fields: str) -> CompanySettings:
    row = CompanySettings.current()
    values = {
        "name": "Global Retail Group",
        "support_email": "billing@example.com",
        "address_line1": "100 Main Street",
        "address_line2": "Suite 200",
        "city": "Paterson",
        "state": "NJ",
        "postal_code": "07501",
    }
    values.update(fields)
    for name, value in values.items():
        setattr(row, name, value)
    row.save()
    return row


def billed_store(last4: str = "6789", operator_name: str = "Fatih Duzenli") -> Lease:
    operator = an_operator(operator_name)
    lease = a_lease(a_store("Istanbul", 1991), operator)
    LeaseBilling.objects.create(
        lease=lease, company_name="Fatih Trading LLC", email="ops@example.com", bank_last4=last4
    )
    return lease


def charge(lease: Lease, price: str, description: str, due: date = DUE) -> OneOffCharge:
    assert lease.operator is not None
    return a_one_off_charge(
        lease.operator,
        price,
        due,
        lease=lease,
        category=OneOffCategory.OTHER,
        description=description,
    )


def builder_invoice(
    lease: Lease,
    lines: list[tuple[str, str, str]],
    number: int = 12,
    **fields: object,
) -> tuple[Invoice, list[OneOffCharge]]:
    """An invoice with one stored line per (description, details, price), each billing its
    own one-off charge of ``lease``; the invoice names the first charge."""
    assert lease.operator is not None
    charges = [charge(lease, price, description) for description, _, price in lines]
    total = sum((D(price) for _, _, price in lines), D("0.00"))
    values: dict[str, object] = {
        "invoice_number": f"INV-2026-{number:04d}",
        "operator": lease.operator,
        "one_off_charge": charges[0],
        "issue_date": ISSUED,
        "due_date": DUE,
        "subtotal": total,
        "total": total,
        "internal_status": "sent",
    }
    values.update(fields)
    invoice = Invoice.objects.create(**values)
    for position, ((description, details, price), billed) in enumerate(
        zip(lines, charges, strict=True), start=1
    ):
        InvoiceLineItem.objects.create(
            invoice=invoice,
            position=position,
            description=description,
            details=details,
            quantity=D(1),
            unit_price=D(price),
            line_total=D(price),
            one_off_charge=billed,
        )
    return invoice, charges


def settle(
    billed: OneOffCharge,
    amount: str,
    on: date,
    method: str = PaymentMethod.ACH,
    **fields: object,
) -> Payment:
    values: dict[str, object] = {
        "one_off_charge": billed,
        "kind": PaymentKind.ONE_OFF,
        "method": method,
        "amount": D(amount),
        "status": PaymentStatus.SETTLED,
        "initiated_on": on,
        "settled_on": on,
    }
    if method != PaymentMethod.ACH:
        values["note"] = "Received by hand"
    values.update(fields)
    return Payment.objects.create(**values)


def portal_account(operator: Operator, email: str = "") -> PortalUser:
    """An accepted portal account of ``operator`` (its pay link then names the portal)."""
    now = timezone.now()
    return PortalUser.objects.create(
        operator=operator,
        email=email or f"portal-{operator.pk.hex[:8]}@example.com",
        password=make_password("a-long-test-password-15c"),
        is_active=True,
        invited_at=now,
        activated_at=now,
    )
