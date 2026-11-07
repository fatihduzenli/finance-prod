"""The invoice PDF (docs/qa/15/PLAN-015c-invoice-pdfs.md §3; the mockups
``docs/design/invoice-template-unpaid.png`` and ``-paid.png``), rendered on request from stored
data and never stored.

:func:`render_invoice` draws, inside the :class:`~.frame.Frame` (title INVOICE, the number, the
stamp):

- the info panel: Bill to (the billing lease's company, else the operator's display name, then
  the operator's full name), Store ("#1991 Istanbul"), Issued, Due;
- the lines (description, its details muted, quantity, price, amount), the header row repeated
  on every page. An invoice made by the invoice builder has its stored lines; any other (the
  rent run's, a one-off charge's, insurance, an adjustment) has one source charge and prints one
  line of it (:func:`charge_words`);
- the totals: Subtotal (and tax, fees and a surcharge when not zero), one row "Paid <Mon D> ·
  <method>" per settled payment, the amount negative ("Paid $0.00" without any), and "Balance
  due" in the dark box;
- "How to pay" (not on a void invoice): the portal's Pay page of the store when the operator can
  sign in to the portal (``portal.links.pay_link``), the wire or Zelle memo, the support e-mail
  when set.

**The figures are the ledger's** (:func:`payments_of`): the settled, non-refund payments that
name the invoice or one of the charges it bills, oldest first. The stamp (:func:`stamp_for`):
VOID for a void invoice; else PAID when they cover the total, PARTLY PAID when they cover part
of it, UNPAID otherwise. The balance is the total less those payments (never below zero), and
$0.00 on a void invoice. No card or account number is ever printed (Rule 1): a payment is named
by its date and method only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Q, QuerySet
from django.db.models.functions import Coalesce

from finance.dates import day_short_text, day_text, month_long_text
from ledger.models import (
    Invoice,
    InvoiceStatus,
    Lease,
    OneOffCharge,
    Payment,
    PaymentStatus,
    RentCharge,
    Store,
)
from ledger.services.pdf import frame
from ledger.services.pdf.frame import LEFT, RIGHT, Cell, Frame, Stamp
from ledger.wording import format_amount

ZERO = Decimal("0.00")
TITLE = "Invoice"
NO_PAYMENTS = "Paid"
PAID_ROW = "Paid {on} · {method}"
ONLINE = "Online: {link} — bank debit (ACH)"
MEMO = "Wire or Zelle: put "
MEMO_AFTER = " in the memo"
QUESTIONS = "Questions: {email}"

# The table's columns: the right edges of quantity, price and amount.
QTY_X = LEFT + 300
PRICE_X = LEFT + 380
AMOUNT_X = RIGHT
DESCRIPTION_WIDTH = 270.0
ROW_WITH_DETAILS = 34.0
ROW_PLAIN = 20.0
#: The space above the totals and above the "How to pay" panel.
GAP = 20.0
TOTALS_WIDTH = 240.0


@dataclass(frozen=True, slots=True)
class Line:
    description: str
    details: str
    quantity: Decimal
    price: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PaidRow:
    on: date | None
    method: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class InvoiceFacts:
    """Everything the invoice PDF prints."""

    number: str
    stamp: Stamp
    bill_to: tuple[str, ...]
    store: str
    issued: date
    due: date
    lines: tuple[Line, ...]
    subtotal: Decimal
    extras: tuple[tuple[str, Decimal], ...]
    total: Decimal
    payments: tuple[PaidRow, ...]
    paid: Decimal
    balance: Decimal
    pay_link: str
    support_email: str
    void: bool


def pdf_filename(invoice: Invoice) -> str:
    """``INV-2026-0012.pdf``."""
    return f"{invoice.invoice_number}.pdf"


# --- The invoice's charges, store and payments ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Charges:
    """The charges an invoice bills, by kind (its lines' and its source charge)."""

    rent: tuple[uuid.UUID, ...]
    one_off: tuple[uuid.UUID, ...]
    insurance: tuple[uuid.UUID, ...]
    adjustment: tuple[uuid.UUID, ...]


def charges_of(invoice: Invoice) -> Charges:
    """The invoice's source charge and its lines' charges."""
    rent = {invoice.rent_charge_id}
    one_off = {invoice.one_off_charge_id}
    for rent_id, one_off_id in invoice.line_items.values_list(
        "rent_charge_id", "one_off_charge_id"
    ):
        rent.add(rent_id)
        one_off.add(one_off_id)
    return Charges(
        rent=tuple(sorted(found for found in rent if found is not None)),
        one_off=tuple(sorted(found for found in one_off if found is not None)),
        insurance=tuple(i for i in (invoice.insurance_charge_id,) if i is not None),
        adjustment=tuple(a for a in (invoice.charge_adjustment_id,) if a is not None),
    )


def payments_of(invoice: Invoice) -> QuerySet[Payment]:
    """The settled, non-refund payments of the invoice or of a charge it bills, oldest
    first."""
    found = charges_of(invoice)
    which = Q(invoice=invoice)
    if found.rent:
        which |= Q(rent_charge_id__in=found.rent)
    if found.one_off:
        which |= Q(one_off_charge_id__in=found.one_off)
    if found.insurance:
        which |= Q(insurance_charge_id__in=found.insurance)
    if found.adjustment:
        which |= Q(charge_adjustment_id__in=found.adjustment)
    return (
        Payment.objects.filter(which, status=PaymentStatus.SETTLED, is_refund=False)
        .annotate(on=Coalesce("settled_on", "initiated_on"))
        .order_by("on", "created_at", "id")
        .distinct()
    )


def lease_of(invoice: Invoice) -> Lease | None:
    """The lease whose charge the invoice bills first (the source charge, else the first
    line's)."""
    if invoice.rent_charge_id is not None:
        return (
            RentCharge.objects.select_related("lease__store").get(pk=invoice.rent_charge_id).lease
        )
    if invoice.one_off_charge_id is not None:
        charge = OneOffCharge.objects.select_related("lease__store").get(
            pk=invoice.one_off_charge_id
        )
        if charge.lease is not None:
            return charge.lease
    if invoice.insurance_charge is not None:
        return invoice.insurance_charge.lease
    adjustment = invoice.charge_adjustment
    if adjustment is not None and adjustment.from_charge is not None:
        return adjustment.from_charge.lease
    for line in invoice.line_items.select_related(
        "rent_charge__lease__store", "one_off_charge__lease__store"
    ).order_by("position"):
        if line.rent_charge is not None:
            return line.rent_charge.lease
        if line.one_off_charge is not None and line.one_off_charge.lease is not None:
            return line.one_off_charge.lease
    return None


def store_text(store: Store | None) -> str:
    """ "#1991 Istanbul" (``ledger.services.stores.title``), or ``""``."""
    from ledger.services import stores

    return stores.title(store) if store is not None else ""


def charge_words(
    rent: RentCharge | None = None,
    one_off: OneOffCharge | None = None,
    insurance_year: int | None = None,
    adjustment: bool = False,
) -> str:
    """What a charge is called on the PDFs: "Rent — November 2026", "Insurance — policy year
    2026", a one-off charge's description (else its fee type's name), "Rent adjustment"."""
    from ledger.services.invoice_builder import charge_label

    if rent is not None:
        words = f"Rent — {month_long_text(rent.period_month)}"
        return words + (f" (debit {rent.seq})" if rent.seq > 1 else "")
    if one_off is not None:
        return charge_label(one_off)
    if insurance_year is not None:
        return f"Insurance — policy year {insurance_year}"
    return "Rent adjustment" if adjustment else "Charge"


def _source_line(invoice: Invoice, store: str) -> Line:
    """The one line of an invoice with no stored lines: its source charge."""
    if invoice.rent_charge is not None:
        description = charge_words(rent=invoice.rent_charge)
        details = store
    elif invoice.one_off_charge is not None:
        description = charge_words(one_off=invoice.one_off_charge)
        details = str(invoice.one_off_charge.get_category_display())
    elif invoice.insurance_charge is not None:
        description = charge_words(insurance_year=invoice.insurance_charge.policy_year)
        details = store
    else:
        description = charge_words(adjustment=invoice.charge_adjustment is not None)
        details = store
    return Line(description, details, Decimal(1), invoice.subtotal, invoice.subtotal)


def lines_of(invoice: Invoice, store: str) -> tuple[Line, ...]:
    stored = [
        Line(
            description=str(item.description),
            details=str(item.details),
            quantity=Decimal(item.quantity),
            price=item.unit_price,
            amount=item.line_total,
        )
        for item in invoice.line_items.order_by("position")
    ]
    return tuple(stored) if stored else (_source_line(invoice, store),)


def stamp_for(void: bool, paid: Decimal, total: Decimal) -> Stamp:
    if void:
        return Stamp.VOID
    if paid >= total:
        return Stamp.PAID
    if paid > 0:
        return Stamp.PARTLY_PAID
    return Stamp.UNPAID


def facts(invoice: Invoice) -> InvoiceFacts:
    from ledger.services.invoice_builder import bill_to
    from portal import links

    company = frame.company_facts()
    lease = lease_of(invoice)
    store = lease.store if lease is not None else None
    shown_store = store_text(store)
    billed = bill_to(lease, invoice.operator)
    who = tuple(
        dict.fromkeys(part for part in (billed.company or billed.name, billed.name) if part)
    )
    rows = tuple(
        PaidRow(getattr(payment, "on", None), str(payment.get_method_display()), payment.amount)
        for payment in payments_of(invoice)
    )
    paid = sum((row.amount for row in rows), ZERO)
    void = invoice.internal_status == InvoiceStatus.VOID
    extras = tuple(
        (label, amount)
        for label, amount in (
            ("Tax", invoice.tax),
            ("Fees", invoice.fees),
            ("Surcharge", invoice.surcharge),
        )
        if amount
    )
    return InvoiceFacts(
        number=str(invoice.invoice_number),
        stamp=stamp_for(void, paid, invoice.total),
        bill_to=who,
        store=shown_store,
        issued=invoice.issue_date,
        due=invoice.due_date,
        lines=lines_of(invoice, shown_store),
        subtotal=invoice.subtotal,
        extras=extras,
        total=invoice.total,
        payments=rows,
        paid=paid,
        balance=ZERO if void else max(invoice.total - paid, ZERO),
        pay_link=(
            links.pay_link(invoice.operator, store.pk) if store is not None and not void else ""
        ),
        support_email=company.support_email,
        void=void,
    )


# --- Drawing ----------------------------------------------------------------------------------


def _quantity(value: Decimal) -> str:
    """``1``, ``2.5``: the quantity without trailing zeros."""
    return f"{value.normalize():f}"


def _paid_on(day: date | None, issued: date) -> str:
    if day is None:
        return ""
    return day_short_text(day) if day.year == issued.year else day_text(day)


def _info_panel(page: Frame, found: InvoiceFacts) -> None:
    page.info_panel(
        (
            Cell("Bill to", found.bill_to),
            Cell("Store", (found.store,)),
            Cell("Issued", (day_text(found.issued),), bold=False),
            Cell("Due", (day_text(found.due),), colour=frame.TEAL),
        )
    )


def _table_header(page: Frame) -> None:
    y = page.top
    page.label(LEFT, y, "Item")
    page.label(QTY_X, y, "Qty", align="right")
    page.label(PRICE_X, y, "Price", align="right")
    page.label(AMOUNT_X, y, "Amount", align="right")
    page.rule(y - 9)
    page.top = y - 9


def _line(page: Frame, line: Line) -> None:
    height = ROW_WITH_DETAILS if line.details else ROW_PLAIN
    if page.top - height < frame.BODY_BOTTOM:
        page.new_page()
        _table_header(page)
    y = page.top - (15 if line.details else 14)
    page.text(LEFT, y, line.description, bold=True, size=10.5, width=DESCRIPTION_WIDTH)
    if line.details:
        page.text(LEFT, y - 13, line.details, size=8.5, colour=frame.MUTED, width=DESCRIPTION_WIDTH)
    middle = y - (4 if line.details else 0)
    page.text(QTY_X, middle, _quantity(line.quantity), size=10, align="right")
    page.text(PRICE_X, middle, format_amount(line.price), size=10, align="right")
    page.text(AMOUNT_X, middle, format_amount(line.amount), bold=True, size=10, align="right")
    page.top -= height
    page.rule(page.top)


@dataclass(frozen=True, slots=True)
class PayLine:
    parts: tuple[tuple[str, bool], ...]
    size: float
    #: The URL this line's text links to (``""``: none).
    link: str = ""


def _pay_lines(found: InvoiceFacts, page: Frame) -> list[PayLine]:
    """The lines of "How to pay"; the portal's link is never cut: its line's font shrinks
    until the link fits the panel."""
    width = RIGHT - LEFT - 32
    lines: list[PayLine] = []
    if found.pay_link:
        size = 9.0
        while size > 6 and frame.text_width(found.pay_link, page.regular, size) > width:
            size -= 0.5
        for text in frame.wrap(ONLINE.format(link=found.pay_link), page.regular, size, width):
            link = found.pay_link if found.pay_link in text else ""
            lines.append(PayLine(((text, False),), size, link))
    lines.append(PayLine(((MEMO, False), (found.number, True), (MEMO_AFTER, False)), 9))
    if found.support_email:
        for text in frame.wrap(QUESTIONS.format(email=found.support_email), page.regular, 9, width):
            lines.append(PayLine(((text, False),), 9))
    return lines


def _total_rows(found: InvoiceFacts) -> list[tuple[str, Decimal]]:
    rows: list[tuple[str, Decimal]] = [("Subtotal", found.subtotal), *found.extras]
    if not found.payments:
        return [*rows, (NO_PAYMENTS, ZERO)]
    return rows + [
        (PAID_ROW.format(on=_paid_on(row.on, found.issued), method=row.method), -row.amount)
        for row in found.payments
    ]


def _totals(page: Frame, found: InvoiceFacts) -> None:
    """The totals at the right, the balance due in the dark box under them."""
    rows = _total_rows(found)
    if page.top - (GAP + 20 * len(rows) + 48) < frame.BODY_BOTTOM:
        page.new_page()
    x = RIGHT - TOTALS_WIDTH
    y = page.top - GAP - 14
    for label, amount in rows:
        page.text(x, y, label, size=10, colour=frame.MUTED, width=TOTALS_WIDTH - 72)
        page.text(RIGHT, y, format_amount(amount), size=10, colour=frame.MUTED, align="right")
        y -= 20
    box_top = y + 4
    page.panel(x, box_top - 44, TOTALS_WIDTH, 44, frame.BAND_TOP)
    page.text(x + 16, box_top - 26, "Balance due", size=10, colour=frame.BAND_TEXT)
    page.text(
        RIGHT - 16,
        box_top - 29,
        format_amount(found.balance),
        bold=True,
        size=18,
        colour=frame.BAND_TEXT,
        align="right",
    )
    page.top = box_top - 44


def _how_to_pay(page: Frame, found: InvoiceFacts) -> None:
    """The "How to pay" panel across the page under the totals (none on a void invoice)."""
    if found.void:
        return
    lines = _pay_lines(found, page)
    height = 36 + 15 * len(lines)
    if page.top - GAP - height < frame.BODY_BOTTOM:
        page.new_page()
    top = page.top - GAP
    page.panel(LEFT, top - height, RIGHT - LEFT, height, frame.PAY_PANEL)
    y = top - 22
    page.text(LEFT + 16, y, "How to pay", bold=True, size=9.5)
    for line in lines:
        y -= 15
        page.rich_line(LEFT + 16, y, line.parts, size=line.size)
        if line.link:
            page.canvas.linkURL(line.link, (LEFT + 16, y - 3, RIGHT - 16, y + 10), relative=0)
    page.top = top - height


def draw(found: InvoiceFacts, company: frame.CompanyFacts) -> bytes:
    page = Frame(company, TITLE, found.number, found.stamp)
    _info_panel(page, found)
    _table_header(page)
    for line in found.lines:
        _line(page, line)
    _totals(page, found)
    _how_to_pay(page, found)
    return page.finish()


def render_invoice(invoice: Invoice, *, today: date) -> bytes:
    """The invoice as a PDF (see the module docstring). ``today`` is the day it is prepared;
    the invoice prints its own dates only, so the same stored data always gives the same
    bytes."""
    return draw(facts(invoice), frame.company_facts())
