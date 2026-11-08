"""The receipt PDF of a settled payment entry (docs/qa/15/PLAN-015c-invoice-pdfs.md §3),
rendered on request from stored data and never stored.

**One receipt per payment entry** (Fable's rework finding, 2026-11-07). An entry recorded
across several charges (a wire against a two-line invoice, the store page's payment form) is
stored as one ``Payment`` row per charge sharing ``allocation_group``; its receipt covers them
all (:func:`entry_of`: the group's rows on the payment's lease that have a receipt, in the
order they were written — ``created_at``, then the smallest id when timestamps tie, so the
entry's first row does not depend on which row the database returns first — the credits
applied left out when the entry received money; a payment without a group is its own entry).
A bank deposit split over several stores gives each store's entry the same group
(``banking.services``), so the lease bounds the entry: a receipt never names another store's
charges. Any row of the entry renders the same receipt, byte for byte.

:func:`render_receipt` draws, inside the :class:`~.frame.Frame` (title RECEIPT, the number
``RCPT-`` and the first eight characters of the id of the entry's first row — the payment's own
for a payment alone; the group's id is shared by a split deposit's stores, so it is not used —
no stored counter — and the stamp PAID):

- the info panel: Received from (the billing lease's company, else the operator's display
  name, then the operator's full name), Store, Date (settled, else initiated), Amount (the
  entry's sum);
- the payment: Method (its label; a bank debit reads "Bank debit ····<last four>" from the
  lease billing's cached last four only) and Reference (the processor's transaction reference,
  or the reference typed for a payment recorded by hand, always through
  ``payments.provider.mask_identifier``; never a routing or account number; left out when
  there is none);
- "Applied to": for each row, the invoice it paid (its own, else the invoice billing its
  charge) once, then the charge's words ("Rent — November 2026", a one-off charge's
  description) with the row's amount — in the invoice's order, never the rows' timestamps
  (:func:`applied_order`);
- "Remaining balance on the store" in the dark box: the open balances of the store billed to
  the payment's operator on ``today`` (``receivables.open_balances``).

Only a settled payment that is money received has a receipt (:func:`has_receipt`): a pending,
returned or voided payment, a refund and money paid back (a deposit or credit refund) have
none, and :func:`render_receipt` raises :class:`NoReceipt` for them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

from django.db.models import Q

from finance.dates import day_text
from ledger.models import (
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    Lease,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services.pdf import frame
from ledger.services.pdf.frame import LEFT, RIGHT, Cell, Frame, Stamp
from ledger.services.pdf.invoice import charge_words, store_text
from ledger.wording import format_amount

ZERO = Decimal("0.00")
TITLE = "Receipt"
BANK_DEBIT = "Bank debit ····{last4}"
REMAINING = "Remaining balance on the store"
#: Kinds that are money paid back to the operator, never received.
MONEY_OUT = (PaymentKind.REFUND_DEPOSIT, PaymentKind.CREDIT_REFUND)


class NoReceipt(Exception):  # noqa: N818 (an outcome)
    """The payment has no receipt (see :func:`has_receipt`)."""


@dataclass(frozen=True, slots=True)
class AppliedLine:
    """One line of "Applied to": an invoice's number or a charge's words, with the amount
    paid on it (``None``: an invoice heading whose charges follow)."""

    text: str
    amount: Decimal | None
    bold: bool


@dataclass(frozen=True, slots=True)
class ReceiptFacts:
    number: str
    received_from: tuple[str, ...]
    store: str
    on: date | None
    amount: Decimal
    method: str
    reference: str
    applied_to: tuple[AppliedLine, ...]
    remaining: Decimal


def has_receipt(payment: Payment) -> bool:
    """Whether ``payment`` has a receipt: settled money received."""
    return (
        payment.status == PaymentStatus.SETTLED
        and not payment.is_refund
        and payment.kind not in MONEY_OUT
    )


def _lease_id(payment: Payment) -> uuid.UUID | None:
    """The lease of the payment's rent, insurance or one-off charge (``None`` otherwise)."""
    for charge in (payment.rent_charge, payment.insurance_charge, payment.one_off_charge):
        if charge is not None:
            return charge.lease_id
    return None


def entry_key(payment: Payment) -> tuple[uuid.UUID, uuid.UUID | None]:
    """The entry the payment belongs to: its allocation group on its lease, else itself."""
    if payment.allocation_group is None:
        return payment.pk, None
    return payment.allocation_group, _lease_id(payment)


def entry_of(payment: Payment) -> list[Payment]:
    """The rows the payment's receipt covers, oldest first: by ``created_at``, then by the
    smallest id when timestamps tie (the first row names the receipt and carries the link)."""
    if payment.allocation_group is None:
        return [payment]
    lease = _lease_id(payment)
    rows = [
        row
        for row in Payment.objects.filter(allocation_group=payment.allocation_group)
        .select_related(
            "invoice", "rent_charge", "one_off_charge", "insurance_charge", "charge_adjustment"
        )
        .order_by("created_at", "pk")
        if has_receipt(row) and _lease_id(row) == lease
    ]
    money = [row for row in rows if row.method != PaymentMethod.CREDIT]
    return money or rows or [payment]


def first_of_entries(payments: Iterable[Payment]) -> set[uuid.UUID]:
    """The ids of the rows that carry their entry's "Receipt PDF" link in a list: the first
    row with a receipt of each entry, in the list's order."""
    seen: set[tuple[uuid.UUID, uuid.UUID | None]] = set()
    first: set[uuid.UUID] = set()
    for payment in payments:
        if not has_receipt(payment) or entry_key(payment) in seen:
            continue
        seen.add(entry_key(payment))
        first.add(payment.pk)
    return first


def _number(first: Payment) -> str:
    return f"RCPT-{str(first.pk)[:8].upper()}"


def receipt_number(payment: Payment) -> str:
    """``RCPT-`` and the first eight characters of the id of the entry's first row, upper
    case."""
    return _number(entry_of(payment)[0])


def receipt_filename(payment: Payment) -> str:
    """``receipt-RCPT-1A2B3C4D.pdf``."""
    return f"receipt-{receipt_number(payment)}.pdf"


def lease_of(payment: Payment) -> Lease | None:
    """The lease of the payment's charge (rent, insurance, one-off, an adjustment's)."""
    for charge in (payment.rent_charge, payment.insurance_charge, payment.one_off_charge):
        if charge is not None and charge.lease is not None:
            return charge.lease
    adjustment = payment.charge_adjustment
    if adjustment is not None and adjustment.from_charge is not None:
        return adjustment.from_charge.lease
    return None


def invoice_of(payment: Payment) -> Invoice | None:
    """The invoice the payment paid: its own, else the (not void) invoice billing its charge
    directly or by a line."""
    if payment.invoice is not None:
        return payment.invoice
    which = Q()
    if payment.rent_charge_id is not None:
        which = Q(rent_charge_id=payment.rent_charge_id) | Q(
            line_items__rent_charge_id=payment.rent_charge_id
        )
    elif payment.one_off_charge_id is not None:
        which = Q(one_off_charge_id=payment.one_off_charge_id) | Q(
            line_items__one_off_charge_id=payment.one_off_charge_id
        )
    elif payment.insurance_charge_id is not None:
        which = Q(insurance_charge_id=payment.insurance_charge_id)
    elif payment.charge_adjustment_id is not None:
        which = Q(charge_adjustment_id=payment.charge_adjustment_id)
    else:
        return None
    return (
        Invoice.objects.filter(which)
        .exclude(internal_status=InvoiceStatus.VOID)
        .order_by("issue_date", "invoice_number")
        .distinct()
        .first()
    )


def method_text(payment: Payment, lease: Lease | None) -> str:
    """The method's label; a bank debit with the cached last four of the bank it debits."""
    if payment.method == PaymentMethod.ACH:
        from portal.services import bank_last4

        last4 = bank_last4(lease)
        if last4:
            return BANK_DEBIT.format(last4=last4)
    return str(payment.get_method_display())


def reference_text(payment: Payment) -> str:
    """The masked reference: the processor's transaction reference for a bank debit, the
    typed reference otherwise; ``""`` when there is none."""
    from ledger.services.refunds import transaction_reference
    from payments.provider import mask_identifier

    raw = transaction_reference(payment) if payment.method == PaymentMethod.ACH else ""
    raw = raw or " ".join(str(payment.reference or "").split())
    return mask_identifier(raw) if raw else ""


def charge_text(payment: Payment) -> str:
    """The words of the charge the payment paid; ``""`` without one."""
    if payment.rent_charge is not None:
        return charge_words(rent=payment.rent_charge)
    if payment.one_off_charge is not None:
        return charge_words(one_off=payment.one_off_charge)
    if payment.insurance_charge is not None:
        return charge_words(insurance_year=payment.insurance_charge.policy_year)
    if payment.charge_adjustment is not None:
        return charge_words(adjustment=True)
    return ""


#: The order of a row's charge kind when it is billed by no invoice line.
KIND_ORDER = ("rent", "insurance", "one_off", "adjustment", "")


def _charge_kind(payment: Payment) -> str:
    if payment.rent_charge_id is not None:
        return "rent"
    if payment.insurance_charge_id is not None:
        return "insurance"
    if payment.one_off_charge_id is not None:
        return "one_off"
    if payment.charge_adjustment_id is not None:
        return "adjustment"
    return ""


def _line_position(payment: Payment, invoice: Invoice | None) -> int | None:
    """The position of the invoice's line billing the row's rent or one-off charge."""
    if invoice is None:
        return None
    if payment.rent_charge_id is not None:
        which = Q(rent_charge_id=payment.rent_charge_id)
    elif payment.one_off_charge_id is not None:
        which = Q(one_off_charge_id=payment.one_off_charge_id)
    else:
        return None
    return (
        InvoiceLineItem.objects.filter(which, invoice=invoice)
        .order_by("position")
        .values_list("position", flat=True)
        .first()
    )


def applied_order(
    rows: Sequence[Payment],
) -> list[tuple[Payment, Invoice | None]]:
    """The rows with the invoice each paid, in the order a reader expects, never by the rows'
    timestamps: the invoice (by number; rows paying no invoice last), then the position of
    the invoice line billing the row's charge, then the charge's words, then the row's id; a
    row billed by no invoice line sorts after the lines by (charge kind, words, id)."""
    keyed = []
    for row in rows:
        invoice = invoice_of(row)
        position = _line_position(row, invoice)
        key = (
            invoice is None,
            invoice.invoice_number if invoice is not None else "",
            position is None,
            position if position is not None else 0,
            KIND_ORDER.index(_charge_kind(row)) if position is None else 0,
            charge_text(row),
            row.pk,
        )
        keyed.append((key, row, invoice))
    keyed.sort(key=lambda item: item[0])
    return [(row, invoice) for _, row, invoice in keyed]


def applied_to(rows: Sequence[Payment]) -> tuple[AppliedLine, ...]:
    """Each invoice the rows paid once (bold), then the words and amount of each charge it
    billed, in :func:`applied_order`; charges billed by no invoice are bold. A row naming no
    charge adds its amount to its invoice's line."""
    by_invoice: dict[str, list[tuple[str, Decimal]]] = {}
    for row, invoice in applied_order(rows):
        heading = f"Invoice {invoice.invoice_number}" if invoice is not None else ""
        by_invoice.setdefault(heading, []).append((charge_text(row), row.amount))
    lines: list[AppliedLine] = []
    for heading, items in by_invoice.items():
        named = [(text, amount) for text, amount in items if text]
        unnamed = [amount for text, amount in items if not text]
        if heading:
            lines.append(AppliedLine(heading, sum(unnamed, ZERO) if unnamed else None, True))
        lines.extend(AppliedLine(text, amount, not heading) for text, amount in named)
    return tuple(lines)


def remaining_on_store(lease: Lease | None, today: date) -> Decimal:
    """What stays open on the lease's store for its operator on ``today``."""
    from ledger.services import receivables

    if lease is None or lease.operator_id is None:
        return ZERO
    rows = receivables.open_balances(today, operator=lease.operator_id)
    return receivables.total(row for row in rows if row.store_id == lease.store_id)


def facts(payment: Payment, today: date) -> ReceiptFacts:
    from ledger.services.invoice_builder import bill_to

    if not has_receipt(payment):
        raise NoReceipt(str(payment.pk))
    rows = entry_of(payment)
    first = rows[0]
    lease = lease_of(first)
    operator = lease.operator if lease is not None else None
    if operator is None and first.one_off_charge is not None:
        operator = first.one_off_charge.operator
    if operator is None and first.invoice is not None:
        operator = first.invoice.operator
    billed = bill_to(lease, operator) if operator is not None else None
    who = (
        tuple(dict.fromkeys(part for part in (billed.company or billed.name, billed.name) if part))
        if billed is not None
        else ()
    )
    return ReceiptFacts(
        number=_number(first),
        received_from=who,
        store=store_text(lease.store if lease is not None else None),
        on=first.settled_on or first.initiated_on,
        amount=sum((row.amount for row in rows), ZERO),
        method=method_text(first, lease),
        reference=reference_text(first),
        applied_to=applied_to(rows),
        remaining=remaining_on_store(lease, today),
    )


def draw(found: ReceiptFacts, company: frame.CompanyFacts) -> bytes:
    page = Frame(company, TITLE, found.number, Stamp.PAID)
    page.info_panel(
        (
            Cell("Received from", found.received_from or ("",)),
            Cell("Store", (found.store,)),
            Cell("Date", (day_text(found.on) if found.on is not None else "",), bold=False),
            Cell("Amount", (format_amount(found.amount),), colour=frame.TEAL),
        )
    )
    rows = [("Method", found.method)]
    if found.reference:
        rows.append(("Reference", found.reference))
    page.label(LEFT, page.top, "Payment")
    page.rule(page.top - 9)
    page.top -= 9
    for label, value in rows:
        y = page.top - 15
        page.text(LEFT, y, label, size=10, colour=frame.MUTED)
        page.text(RIGHT, y, value, size=10, align="right", width=RIGHT - LEFT - 120)
        page.top -= 22
        page.rule(page.top)
    page.top -= 26
    page.label(LEFT, page.top, "Applied to")
    page.rule(page.top - 9)
    page.top -= 9
    for line in found.applied_to:
        y = page.top - 15
        page.text(
            LEFT, y, line.text, bold=line.bold, size=Fraction("10.5"), width=RIGHT - LEFT - 100
        )
        if line.amount is not None:
            page.text(RIGHT, y, format_amount(line.amount), bold=True, size=10, align="right")
        page.top -= 22
        page.rule(page.top)
    width = 300
    box_top = page.top - 24
    page.panel(RIGHT - width, box_top - 44, width, 44, frame.BAND_TOP)
    page.text(RIGHT - width + 16, box_top - 26, REMAINING, size=10, colour=frame.BAND_TEXT)
    page.text(
        RIGHT - 16,
        box_top - 29,
        format_amount(found.remaining),
        bold=True,
        size=18,
        colour=frame.BAND_TEXT,
        align="right",
    )
    page.top = box_top - 44
    return page.finish()


def render_receipt(payment: Payment, *, today: date) -> bytes:
    """The receipt as a PDF (see the module docstring); :class:`NoReceipt` for a payment
    without one. The remaining balance is the store's on ``today``."""
    return draw(facts(payment, today), frame.company_facts())
