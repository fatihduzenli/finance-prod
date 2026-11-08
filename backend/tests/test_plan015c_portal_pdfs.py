"""PLAN-015c task 5 (Opus): the portal's PDFs — "Download PDF" on each invoice row and
"Receipt PDF" on each settled payment row of the operator's own store, the downloads with
their headers and audit rows, and the invoice status "Partly paid" (with the memo line). The
isolation of both routes is tested per route in tests/test_plan014_h_isolation.py."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ledger.models import AuditLog, Invoice, Payment
from ledger.services.pdf import downloads
from ledger.services.pdf.receipt import receipt_filename
from portal import services
from tests.pdf_helpers import pdf_text
from tests.receivables_helpers import a_payment
from tests.test_portal import World, active_user, portal_client
from tests.test_portal import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_portal import world as world  # noqa: PLC0414 (the fixture, re-exported)

pytestmark = pytest.mark.django_db


def test_each_invoice_row_links_its_pdf_and_the_download_is_audited(world: World) -> None:
    user = active_user(world.a)
    client = portal_client(user)
    page = client.get(f"/portal/stores/{world.store_a.pk}/invoices/").content.decode()
    link = f'href="/portal/invoices/{world.invoice_a.pk}.pdf">Download PDF</a>'
    assert link in page
    response = client.get(f"/portal/invoices/{world.invoice_a.pk}.pdf")
    assert response.status_code == 200
    assert response["Content-Disposition"] == 'attachment; filename="INV-2026-0001.pdf"'
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "private, no-store"
    text = pdf_text(response.content)
    assert "INV-2026-0001" in text and "Alphaville" in text
    entry = AuditLog.objects.get(action=downloads.INVOICE_DOWNLOADED)
    assert (entry.actor_id, entry.entity_id) == (None, str(world.invoice_a.pk))
    assert entry.after == {"filename": "INV-2026-0001.pdf", "portal_user": str(user.pk)}


def test_a_settled_payment_row_links_its_receipt_and_a_pending_or_returned_one_does_not(
    world: World,
) -> None:
    settled = Payment.objects.get(rent_charge=world.paid_a)
    pending = a_payment(
        world.rent_a, "100.00", None, status="pending", initiated_on=date(2026, 9, 5)
    )
    returned = a_payment(world.rent_a, "200.00", date(2026, 9, 6), status="returned")
    user = active_user(world.a)
    client = portal_client(user)
    page = client.get(f"/portal/stores/{world.store_a.pk}/payments/").content.decode()
    assert f'href="/portal/payments/{settled.pk}/receipt.pdf">Receipt PDF</a>' in page
    for hidden in (pending, returned):
        assert f"/portal/payments/{hidden.pk}/receipt.pdf" not in page
        assert client.get(f"/portal/payments/{hidden.pk}/receipt.pdf").status_code == 404
    response = client.get(f"/portal/payments/{settled.pk}/receipt.pdf")
    assert response.status_code == 200
    assert response["Content-Disposition"] == (
        f'attachment; filename="{receipt_filename(settled)}"'
    )
    text = pdf_text(response.content)
    # The bank debit's last four is A's, from the lease billing's cached value.
    assert "Bank debit ····4821" in text
    entry = AuditLog.objects.get(action=downloads.RECEIPT_DOWNLOADED)
    assert entry.after == {"filename": receipt_filename(settled), "portal_user": str(user.pk)}


@pytest.mark.parametrize(
    ("paid", "status", "memo"),
    [
        ("0.00", "Open", True),
        ("850.00", "Partly paid", True),
        ("1850.00", "Paid", False),
    ],
)
def test_the_invoice_status_reads_open_partly_paid_or_paid_with_the_memo_while_owed(
    world: World, paid: str, status: str, memo: bool
) -> None:
    Invoice.objects.filter(pk=world.invoice_a.pk).update(amount_paid=Decimal(paid))
    user = active_user(world.a)
    rows = services.invoices(user, world.store_a)
    row = next(item for item in rows if item.number == "INV-2026-0001")
    assert (row.status, row.pk) == (status, world.invoice_a.pk)
    assert bool(row.pay_memo) is memo
    page = portal_client(user).get(f"/portal/stores/{world.store_a.pk}/invoices/")
    assert f"<td>{status}</td>" in page.content.decode()


def test_a_void_invoice_reads_void_without_the_memo_and_still_downloads(world: World) -> None:
    Invoice.objects.filter(pk=world.invoice_a.pk).update(internal_status="void")
    user = active_user(world.a)
    row = next(i for i in services.invoices(user, world.store_a) if i.number == "INV-2026-0001")
    assert (row.status, row.pay_memo) == ("Void", "")
    response = portal_client(user).get(f"/portal/invoices/{world.invoice_a.pk}.pdf")
    assert response.status_code == 200
    assert "VOID" in pdf_text(response.content)
