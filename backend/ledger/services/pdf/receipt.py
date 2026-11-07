"""The receipt PDF of a settled payment (docs/qa/15/PLAN-015c-invoice-pdfs.md §3), rendered on
request from stored data and never stored.

:func:`render_receipt` draws, inside the :class:`~.frame.Frame` (title RECEIPT, the number
``RCPT-`` and the payment id's first eight characters — no stored counter — and the stamp
PAID):

- the info panel: Received from (the billing lease's company, else the operator's display
  name, then the operator's full name), Store, Date (settled, else initiated), Amount;
- the payment: Method (its label; a bank debit reads "Bank debit ····<last four>" from the
  lease billing's cached last four only) and Reference (the processor's transaction reference,
  or the reference typed for a payment recorded by hand, always through
  ``payments.provider.mask_identifier``; never a routing or account number; left out when
  there is none);
- "Applied to": the invoice the payment paid (its own, else the invoice billing its charge)
  and the charge's words ("Rent — November 2026", a one-off charge's description);
- "Remaining balance on the store" in the dark box: the open balances of the store billed to
  the payment's operator on ``today`` (``receivables.open_balances``).

Only a settled payment that is money received has a receipt (:func:`has_receipt`): a pending,
returned or voided payment, a refund and money paid back (a deposit or credit refund) have
none, and :func:`render_receipt` raises :class:`NoReceipt` for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Q

from finance.dates import day_text
from ledger.models import (
    Invoice,
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
class ReceiptFacts:
    number: str
    received_from: tuple[str, ...]
    store: str
    on: date | None
    amount: Decimal
    method: str
    reference: str
    applied_to: tuple[str, ...]
    remaining: Decimal


def has_receipt(payment: Payment) -> bool:
    """Whether ``payment`` has a receipt: settled money received."""
    return (
        payment.status == PaymentStatus.SETTLED
        and not payment.is_refund
        and payment.kind not in MONEY_OUT
    )


def receipt_number(payment: Payment) -> str:
    """``RCPT-`` and the payment id's first eight characters, upper case."""
    return f"RCPT-{str(payment.pk)[:8].upper()}"


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


def applied_to(payment: Payment) -> tuple[str, ...]:
    """The invoice's number, then the charge's words."""
    invoice = invoice_of(payment)
    lines = [f"Invoice {invoice.invoice_number}"] if invoice is not None else []
    if payment.rent_charge is not None:
        lines.append(charge_words(rent=payment.rent_charge))
    elif payment.one_off_charge is not None:
        lines.append(charge_words(one_off=payment.one_off_charge))
    elif payment.insurance_charge is not None:
        lines.append(charge_words(insurance_year=payment.insurance_charge.policy_year))
    elif payment.charge_adjustment is not None:
        lines.append(charge_words(adjustment=True))
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
    lease = lease_of(payment)
    operator = lease.operator if lease is not None else None
    if operator is None and payment.one_off_charge is not None:
        operator = payment.one_off_charge.operator
    if operator is None and payment.invoice is not None:
        operator = payment.invoice.operator
    billed = bill_to(lease, operator) if operator is not None else None
    who = (
        tuple(dict.fromkeys(part for part in (billed.company or billed.name, billed.name) if part))
        if billed is not None
        else ()
    )
    return ReceiptFacts(
        number=receipt_number(payment),
        received_from=who,
        store=store_text(lease.store if lease is not None else None),
        on=payment.settled_on or payment.initiated_on,
        amount=payment.amount,
        method=method_text(payment, lease),
        reference=reference_text(payment),
        applied_to=applied_to(payment),
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
    for index, line in enumerate(found.applied_to):
        y = page.top - 15
        page.text(LEFT, y, line, bold=index == 0, size=10.5, width=RIGHT - LEFT - 100)
        if index == len(found.applied_to) - 1:
            page.text(RIGHT, y, format_amount(found.amount), bold=True, size=10, align="right")
        page.top -= 22
        page.rule(page.top)
    width = 300.0
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
