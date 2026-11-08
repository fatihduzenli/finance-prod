"""PLAN-015c task 4 (Opus): the staff downloads — ``ledger:invoices:pdf`` and
``ledger:payments:receipt``: every role may download (as the print view), the headers are a
stored document's download headers, the bytes are a PDF, each download is audited with the
actor, a rent-run invoice (no draft) downloads, unknown and malformed ids are 404, a payment
without a receipt is 404, an unsafe method is refused; the links on the store's Invoices and
Payments tabs, the invoice draft and preview pages and the payment page."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceStatus,
    Lease,
    Payment,
    PaymentMethod,
)
from ledger.services.pdf import downloads
from ledger.services.pdf.receipt import receipt_filename
from tests.pdf_helpers import billed_store, builder_invoice, company, pdf_text, settle
from tests.receivables_helpers import a_rent_charge, an_invoice
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

THREE = [
    ("Rent — December 2026", "Store #1991 Istanbul · Dec 1–31", "2000.00"),
    ("Insurance 2026–27", "Annual store coverage", "300.00"),
    ("Access fee", "One-time", "500.00"),
]


def invoice_url(invoice: Invoice) -> str:
    return f"/invoices/{invoice.pk}.pdf"


def receipt_url(payment: Payment) -> str:
    return f"/payments/{payment.pk}/receipt.pdf"


def sent_invoice() -> tuple[Invoice, Payment]:
    company()
    invoice, charges = builder_invoice(billed_store(), THREE)
    payment = settle(charges[0], "1000.00", date(2026, 11, 2), PaymentMethod.ZELLE)
    return invoice, payment


def assert_download(response: object, filename: str) -> bytes:
    from django.http import HttpResponse

    assert isinstance(response, HttpResponse)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == f'attachment; filename="{filename}"'
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Security-Policy"] == "sandbox"
    assert response["Cache-Control"] == "private, no-store"
    assert int(response["Content-Length"]) == len(response.content)
    assert response.content.startswith(b"%PDF")
    return bytes(response.content)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_downloads_the_invoice_pdf_and_it_is_audited(role: Role) -> None:
    invoice, _ = sent_invoice()
    client, user = client_for(role)
    data = assert_download(client.get(invoice_url(invoice)), "INV-2026-0012.pdf")
    assert "PARTLY PAID" in pdf_text(data)
    entry = AuditLog.objects.get(action=downloads.INVOICE_DOWNLOADED)
    assert (entry.actor_id, entry.entity_type, entry.entity_id) == (
        user.pk,
        "invoice",
        str(invoice.pk),
    )
    assert entry.after == {"filename": "INV-2026-0012.pdf"}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_downloads_a_receipt_pdf_and_it_is_audited(role: Role) -> None:
    _, payment = sent_invoice()
    client, user = client_for(role)
    data = assert_download(client.get(receipt_url(payment)), receipt_filename(payment))
    assert "RECEIPT" in pdf_text(data)
    entry = AuditLog.objects.get(action=downloads.RECEIPT_DOWNLOADED)
    assert (entry.actor_id, entry.entity_type, entry.entity_id) == (
        user.pk,
        "payment",
        str(payment.pk),
    )


def test_a_rent_run_invoice_without_a_draft_downloads() -> None:
    company()
    lease = billed_store()
    assert lease.operator is not None
    rent = a_rent_charge(lease, date(2026, 11, 1), due="1850.00")
    invoice = an_invoice(
        lease.operator, 77, "1850.00", rent_charge=rent, internal_status=InvoiceStatus.SENT
    )
    assert not InvoiceDraft.objects.filter(sent_invoice=invoice).exists()
    data = assert_download(
        client_for(Role.VIEWER)[0].get(invoice_url(invoice)), "INV-2026-0077.pdf"
    )
    assert "Rent — November 2026" in pdf_text(data)


@pytest.mark.parametrize(
    "path",
    [
        f"/invoices/{uuid.uuid4()}.pdf",
        "/invoices/not-a-uuid.pdf",
        "/invoices/..%2F..%2Fetc%2Fpasswd.pdf",
        f"/payments/{uuid.uuid4()}/receipt.pdf",
        "/payments/not-a-uuid/receipt.pdf",
    ],
)
def test_unknown_and_malformed_ids_are_not_found_and_nothing_is_audited(path: str) -> None:
    response = client_for(Role.ADMIN)[0].get(path)
    assert response.status_code == 404
    assert not AuditLog.objects.filter(
        action__in=[downloads.INVOICE_DOWNLOADED, downloads.RECEIPT_DOWNLOADED]
    ).exists()


@pytest.mark.parametrize("status", ["pending", "returned", "voided"])
def test_a_payment_without_a_receipt_is_not_found(status: str) -> None:
    company()
    _, charges = builder_invoice(billed_store(), THREE)
    payment = settle(charges[0], "100.00", date(2026, 11, 2), status=status)
    assert client_for(Role.ADMIN)[0].get(receipt_url(payment)).status_code == 404
    assert not AuditLog.objects.filter(action=downloads.RECEIPT_DOWNLOADED).exists()


def test_an_unsafe_method_is_refused_and_a_signed_out_visitor_gets_no_pdf() -> None:
    invoice, payment = sent_invoice()
    admin = client_for(Role.ADMIN)[0]
    for url in (invoice_url(invoice), receipt_url(payment)):
        assert admin.post(url).status_code in (403, 405), url
        response = Client().get(url)
        # Refused before the view: the sign-in answer, never the document.
        assert response.status_code == 401, url
        assert b"%PDF" not in response.content
    assert not AuditLog.objects.filter(
        action__in=[downloads.INVOICE_DOWNLOADED, downloads.RECEIPT_DOWNLOADED]
    ).exists()


# --- The links ------------------------------------------------------------------------------


def test_the_store_tabs_link_every_invoice_and_each_receipt() -> None:
    invoice, payment = sent_invoice()
    lease = billed_store_of(invoice)
    returned = settle(
        payment.one_off_charge,  # type: ignore[arg-type]
        "50.00",
        date(2026, 11, 3),
        status="returned",
    )
    viewer = client_for(Role.VIEWER)[0]
    page = viewer.get(f"/stores/{lease.store_id}/?tab=invoices&filter=all").content.decode()
    assert f'href="/invoices/{invoice.pk}.pdf">Download PDF</a>' in page
    page = viewer.get(f"/stores/{lease.store_id}/?tab=payments").content.decode()
    assert f'href="/payments/{payment.pk}/receipt.pdf">Receipt PDF</a>' in page
    assert f"/payments/{returned.pk}/receipt.pdf" not in page


def billed_store_of(invoice: Invoice) -> Lease:
    from ledger.services.pdf.invoice import lease_of

    lease = lease_of(invoice)
    assert lease is not None
    return lease


def test_a_rent_run_invoice_row_without_a_draft_still_links_its_pdf() -> None:
    company()
    lease = billed_store()
    assert lease.operator is not None
    rent = a_rent_charge(lease, date(2026, 11, 1), due="1850.00")
    invoice = an_invoice(
        lease.operator, 78, "1850.00", rent_charge=rent, internal_status=InvoiceStatus.SENT
    )
    page = (
        client_for(Role.VIEWER)[0]
        .get(f"/stores/{lease.store_id}/?tab=invoices&filter=all")
        .content.decode()
    )
    assert f'href="/invoices/{invoice.pk}.pdf">Download PDF</a>' in page


def test_the_sent_draft_and_its_preview_link_the_invoice_pdf_and_a_draft_does_not() -> None:
    invoice, _ = sent_invoice()
    billing, user = client_for(Role.BILLING)
    sent = InvoiceDraft.objects.create(
        operator=invoice.operator,
        status=InvoiceDraftStatus.SENT,
        sent_invoice=invoice,
        created_by=user,
    )
    link = f'href="/invoices/{invoice.pk}.pdf" id="invoice-pdf">Download PDF</a>'
    assert link in billing.get(f"/invoices/drafts/{sent.pk}/").content.decode()
    assert link in billing.get(f"/invoices/drafts/{sent.pk}/preview/").content.decode()
    draft = InvoiceDraft.objects.create(operator=invoice.operator, created_by=user)
    for url in (f"/invoices/drafts/{draft.pk}/", f"/invoices/drafts/{draft.pk}/preview/"):
        assert 'id="invoice-pdf"' not in billing.get(url).content.decode(), url


def test_the_payment_page_links_its_receipt_only_when_it_has_one() -> None:
    _, payment = sent_invoice()
    viewer = client_for(Role.VIEWER)[0]
    page = viewer.get(f"/payments/{payment.pk}/").content.decode()
    assert f'href="/payments/{payment.pk}/receipt.pdf" id="receipt-pdf">Receipt PDF</a>' in page
    Payment.objects.filter(pk=payment.pk).update(status="voided")
    page = viewer.get(f"/payments/{payment.pk}/").content.decode()
    assert 'id="receipt-pdf"' not in page
