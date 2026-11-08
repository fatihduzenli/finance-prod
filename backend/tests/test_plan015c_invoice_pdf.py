"""PLAN-015c task 2 (Opus): the shared page frame and the invoice PDF — each stamp state, the
figures against the ledger, a long invoice over two pages with its header row and "Page x of
y", the logo (the company's copy, else the brand PNG), the address lines, the pay link, no
payment data in the text, the same bytes for the same data, the fonts."""

from __future__ import annotations

import hashlib
import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pypdf import PdfReader

from ledger.models import (
    CompanySettings,
    Invoice,
    InvoiceStatus,
    Lease,
    OneOffCharge,
    PaymentMethod,
)
from ledger.services.pdf import frame
from ledger.services.pdf.invoice import facts, pdf_filename, render_invoice
from ledger.validators import validate_no_payment_data
from tests.pdf_helpers import (
    billed_store,
    builder_invoice,
    company,
    page_texts,
    pdf_text,
    portal_account,
    settle,
)
from tests.receivables_helpers import a_rent_charge, an_invoice
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 11, 20)
THREE = [
    ("Rent — December 2026", "Store #1991 Istanbul · Dec 1–31", "2000.00"),
    ("Insurance 2026–27", "Annual store coverage", "300.00"),
    ("Access fee", "One-time", "500.00"),
]
#: Inter Regular and Bold 4.1 (extras/ttf of the release, OFL); the brand logo's PNG.
SHA256 = {
    "fonts/Inter-Regular.ttf": "40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82",
    "fonts/Inter-Bold.ttf": "288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f",
    "brand/monvera_logo_dark_transparent.png": (
        "2c105f2f2451899daf368a659e431f94bba4ed5f15d069f31eb51c71a97d1e66"
    ),
}


def lines_of(data: bytes) -> list[str]:
    """The PDF's text lines, stripped."""
    return [line.strip() for line in pdf_text(data).splitlines() if line.strip()]


def three_line_invoice() -> tuple[Invoice, list[OneOffCharge]]:
    company()
    return builder_invoice(billed_store(), THREE)


# --- The stamp and the figures ---------------------------------------------------------------


def test_an_invoice_without_payments_is_unpaid_with_the_whole_total_due() -> None:
    invoice, _ = three_line_invoice()
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert "UNPAID" in printed
    for word in ("PAID", "PARTLY PAID", "VOID"):
        assert word not in printed, word
    # The header, the panel, the three lines and the totals, in the mockup's words.
    for expected in (
        "INVOICE",
        "INV-2026-0012",
        "Global Retail Group",
        "BILL TO STORE ISSUED DUE",
        "Fatih Trading LLC",
        "Fatih Duzenli",
        "#1991 Istanbul",
        "Nov 15, 2026",
        "Dec 1, 2026",
        "ITEM QTY PRICE AMOUNT",
        "Rent — December 2026",
        "Store #1991 Istanbul · Dec 1–31",
        "Insurance 2026–27",
        "Access fee",
        "$2,000.00",
        "Subtotal",
        "$2,800.00",
        "Balance due",
        "How to pay",
        "Questions: billing@example.com",
        "Page 1 of 1",
    ):
        assert expected in printed, expected
    paid = printed.index("Paid")
    assert printed[paid + 1] == "$0.00"
    assert printed[printed.index("Balance due") + 1] == "$2,800.00"


def test_one_partial_payment_is_partly_paid_and_three_partial_payments_each_have_a_row() -> None:
    invoice, charges = three_line_invoice()
    settle(charges[0], "1000.00", date(2026, 11, 2), PaymentMethod.ZELLE)
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert "PARTLY PAID" in printed and "UNPAID" not in printed
    assert printed[printed.index("Paid Nov 2 · Zelle") + 1] == "-$1,000.00"
    assert printed[printed.index("Balance due") + 1] == "$1,800.00"
    settle(charges[0], "1000.00", date(2026, 11, 9), PaymentMethod.WIRE)
    settle(charges[1], "250.00", date(2026, 11, 16))
    printed = lines_of(render_invoice(invoice, today=TODAY))
    rows = [line for line in printed if line.startswith("Paid ")]
    # Oldest first, each with its method's label.
    assert rows == ["Paid Nov 2 · Zelle", "Paid Nov 9 · Wire", "Paid Nov 16 · Bank debit (ACH)"]
    assert printed[printed.index("Paid Nov 16 · Bank debit (ACH)") + 1] == "-$250.00"
    assert "PARTLY PAID" in printed
    assert printed[printed.index("Balance due") + 1] == "$550.00"


def test_payments_covering_the_total_are_paid_with_nothing_due() -> None:
    invoice, charges = three_line_invoice()
    for billed, (_, _, price) in zip(charges, THREE, strict=True):
        settle(billed, price, date(2026, 11, 28))
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert "PAID" in printed
    for word in ("UNPAID", "PARTLY PAID"):
        assert word not in printed, word
    assert printed[printed.index("Balance due") + 1] == "$0.00"
    found = facts(invoice)
    # The figures are the ledger's: the subtotal is the lines', the balance the total less
    # the payments.
    assert found.subtotal == sum((line.amount for line in found.lines), D("0.00"))
    assert (found.paid, found.balance) == (D("2800.00"), D("0.00"))


def test_returned_voided_pending_and_refund_rows_do_not_count_as_paid() -> None:
    invoice, charges = three_line_invoice()
    settle(charges[0], "500.00", date(2026, 11, 2), status="returned")
    settle(charges[0], "400.00", date(2026, 11, 3), status="voided")
    settle(charges[0], "300.00", date(2026, 11, 4), status="pending")
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert "UNPAID" in printed
    assert not [line for line in printed if line.startswith("Paid ")]
    assert facts(invoice).balance == D("2800.00")


def test_a_void_invoice_is_void_with_nothing_due_its_payments_listed_and_no_pay_panel() -> None:
    invoice, charges = three_line_invoice()
    portal_account(invoice.operator)
    settle(charges[0], "700.00", date(2026, 11, 5), PaymentMethod.CHECK)
    invoice.internal_status = InvoiceStatus.VOID
    invoice.save()
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert "VOID" in printed and "UNPAID" not in printed
    assert printed[printed.index("Paid Nov 5 · Check") + 1] == "-$700.00"
    assert printed[printed.index("Balance due") + 1] == "$0.00"
    assert "How to pay" not in printed
    assert not [line for line in printed if line.startswith("Online:")]


def test_an_invoice_with_tax_shows_it_between_the_subtotal_and_the_payments() -> None:
    company()
    invoice, _ = builder_invoice(billed_store(), THREE[:1], tax=D("140.00"), total=D("2140.00"))
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert printed[printed.index("Subtotal") + 1] == "$2,000.00"
    assert printed[printed.index("Tax") + 1] == "$140.00"
    assert printed[printed.index("Balance due") + 1] == "$2,140.00"


# --- Invoices without stored lines -----------------------------------------------------------


def test_a_rent_run_invoice_without_lines_prints_its_rent_charge_as_one_line() -> None:
    company()
    lease = billed_store()
    assert lease.operator is not None
    rent = a_rent_charge(lease, date(2026, 11, 1), due="1850.00")
    invoice = an_invoice(
        lease.operator,
        77,
        "1850.00",
        rent_charge=rent,
        internal_status=InvoiceStatus.SENT,
        issue_date=date(2026, 10, 25),
        due_date=date(2026, 11, 1),
    )
    printed = lines_of(render_invoice(invoice, today=TODAY))
    row = printed.index("Rent — November 2026")
    assert printed[row + 1 : row + 5] == ["#1991 Istanbul", "1", "$1,850.00", "$1,850.00"]
    assert "#1991 Istanbul" in printed[: printed.index("ITEM QTY PRICE AMOUNT")]
    assert "Fatih Trading LLC" in printed
    assert pdf_filename(invoice) == "INV-2026-0077.pdf"


def test_a_rent_payment_names_the_invoice_or_only_the_charge_and_is_listed_once() -> None:
    company()
    lease = billed_store()
    assert lease.operator is not None
    rent = a_rent_charge(lease, date(2026, 11, 1), due="1850.00")
    invoice = an_invoice(lease.operator, 78, "1850.00", rent_charge=rent, internal_status="sent")
    from tests.receivables_helpers import a_payment

    by_charge = a_payment(rent, "850.00", date(2026, 11, 3))
    by_invoice = a_payment(rent, "1000.00", date(2026, 11, 4))
    by_invoice.invoice = invoice
    by_invoice.save()
    printed = lines_of(render_invoice(invoice, today=TODAY))
    rows = [line for line in printed if line.startswith("Paid ")]
    assert rows == ["Paid Nov 3 · Bank debit (ACH)", "Paid Nov 4 · Bank debit (ACH)"]
    assert "PAID" in printed
    assert by_charge.invoice_id is None


# --- Pages, logo, address, link --------------------------------------------------------------


def test_forty_lines_run_over_two_pages_with_the_header_row_and_the_page_numbers() -> None:
    company()
    lines = [(f"Item number {index}", "", "10.00") for index in range(1, 41)]
    invoice, _ = builder_invoice(billed_store(), lines)
    pages = page_texts(render_invoice(invoice, today=TODAY))
    assert len(pages) == 2
    for number, page in enumerate(pages, start=1):
        page_lines = [line.strip() for line in page.splitlines()]
        assert "ITEM QTY PRICE AMOUNT" in page_lines
        assert f"Page {number} of 2" in page_lines
        assert "INV-2026-0012" in page_lines  # the band on every page
    first = [line.strip() for line in pages[0].splitlines()]
    second = [line.strip() for line in pages[1].splitlines()]
    assert "Item number 1" in first and "Item number 40" in second
    assert "Balance due" in second and "Balance due" not in first
    every = [line for page in pages for line in page.splitlines() if line.startswith("Item ")]
    assert len(every) == 40


def _images(data: bytes) -> list[tuple[int, int]]:
    reader = PdfReader(io.BytesIO(data))
    sizes = []
    for image in reader.pages[0].images:
        with Image.open(io.BytesIO(image.data)) as opened:
            sizes.append(opened.size)
    return sizes


def test_the_band_shows_the_brand_logo_without_a_copy_and_the_companys_copy_with_one() -> None:
    invoice, _ = three_line_invoice()
    assert _images(render_invoice(invoice, today=TODAY)) == [(940, 320)]
    out = io.BytesIO()
    Image.new("RGB", (60, 30), (10, 120, 200)).save(out, format="PNG")
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(logo_png=out.getvalue())
    assert _images(render_invoice(invoice, today=TODAY)) == [(60, 30)]


def test_an_unreadable_logo_copy_falls_back_to_the_brand_logo() -> None:
    invoice, _ = three_line_invoice()
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(
        logo_png=b"\x89PNG not an image"
    )
    assert _images(render_invoice(invoice, today=TODAY)) == [(940, 320)]


def test_the_band_prints_the_address_lines_that_are_set() -> None:
    invoice, _ = three_line_invoice()
    printed = lines_of(render_invoice(invoice, today=TODAY))
    name = printed.index("Global Retail Group")
    assert printed[name + 1 : name + 3] == [
        "100 Main Street, Suite 200,",
        "Paterson, NJ 07501",
    ]
    company(address_line1="", address_line2="", state="", postal_code="", city="Paterson")
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert printed[printed.index("Global Retail Group") + 1] == "Paterson"
    company(address_line1="", address_line2="", state="", postal_code="", city="")
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert printed[printed.index("Global Retail Group") + 1] == "INVOICE"


def test_the_pay_link_is_the_stores_portal_pay_page_only_with_a_portal_account() -> None:
    invoice, _ = three_line_invoice()
    text = pdf_text(render_invoice(invoice, today=TODAY))
    assert "Online:" not in text
    assert "Wire or Zelle: put \nINV-2026-0012\n in the memo" in text
    portal_account(invoice.operator)
    lease = billed_store_of(invoice)
    data = render_invoice(invoice, today=TODAY)
    text = pdf_text(data)
    link = f"http://localhost:8000/portal/stores/{lease.store_id}/pay/"
    assert f"Online: {link}" in text
    assert f"Online: {link} — bank debit (ACH)" in " ".join(text.split())
    # The link is clickable.
    annotations: Any = PdfReader(io.BytesIO(data)).pages[0]["/Annots"]
    assert [a.get_object()["/A"]["/URI"] for a in annotations] == [link]


def billed_store_of(invoice: Invoice) -> Lease:
    from ledger.services.pdf.invoice import lease_of

    lease = lease_of(invoice)
    assert lease is not None
    return lease


def test_no_support_email_leaves_the_questions_line_and_the_footer_part_out() -> None:
    invoice, _ = three_line_invoice()
    company(support_email="")
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert not [line for line in printed if line.startswith("Questions:")]
    assert "Global Retail Group" in printed[-2:]


# --- Rule 1, determinism, fonts --------------------------------------------------------------


def test_the_pdf_text_holds_no_card_or_account_number() -> None:
    invoice, charges = three_line_invoice()
    portal_account(invoice.operator)
    settle(charges[0], "1000.00", date(2026, 11, 2), PaymentMethod.ZELLE, reference="ZL-778812")
    text = pdf_text(render_invoice(invoice, today=TODAY))
    validate_no_payment_data(text)
    # The cached last four of the bank is not on an invoice, nor the payment's reference.
    assert_absent(text, "6789")
    assert "ZL-778812" not in text


def test_the_same_data_gives_the_same_bytes() -> None:
    invoice, charges = three_line_invoice()
    settle(charges[0], "1000.00", date(2026, 11, 2))
    first = render_invoice(invoice, today=TODAY)
    assert first.startswith(b"%PDF")
    assert render_invoice(invoice, today=date(2027, 1, 1)) == first
    settle(charges[1], "1.00", date(2026, 11, 3))
    assert render_invoice(invoice, today=TODAY) != first


def test_the_inter_fonts_are_embedded_and_are_the_release_files() -> None:
    for name, digest in SHA256.items():
        path = frame.STATIC / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    assert "SIL Open Font License, Version 1.1" in (frame.FONT_DIR / "Inter-OFL.txt").read_text(
        encoding="utf-8"
    )
    invoice, _ = three_line_invoice()
    page = PdfReader(io.BytesIO(render_invoice(invoice, today=TODAY))).pages[0]
    resources: Any = page["/Resources"]
    fonts = {str(font.get_object()["/BaseFont"]) for font in resources["/Font"].values()}
    assert any(font.endswith("+Inter-Regular") for font in fonts), fonts
    assert any(font.endswith("+Inter-Bold") for font in fonts), fonts


def test_without_the_ttf_files_the_pdf_falls_back_to_helvetica(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(frame, "FONT_DIR", tmp_path)
    monkeypatch.setattr(frame, "_fonts", None)
    monkeypatch.setattr(frame, "_FONT_FILES", {"InterMissing": "Inter-Regular.ttf"})
    assert frame.fonts() == ("Helvetica", "Helvetica-Bold")
    invoice, _ = three_line_invoice()
    printed = lines_of(render_invoice(invoice, today=TODAY))
    assert "INV-2026-0012" in printed and "Balance due" in printed


def test_a_long_company_name_and_description_are_cut_with_an_ellipsis() -> None:
    company(name="A Company Name So Long That It Could Never Fit In The Band Of The Page At All")
    invoice, _ = builder_invoice(billed_store(), [("D" * 120, "", "10.00")])
    printed = lines_of(render_invoice(invoice, today=TODAY))
    cut = [line for line in printed if line.startswith("A Company Name") and line.endswith("…")]
    assert cut, printed
    assert [line for line in printed if line.startswith("DDDD") and line.endswith("…")]
