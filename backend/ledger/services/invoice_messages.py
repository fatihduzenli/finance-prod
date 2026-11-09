"""The invoice and receipt e-mails the app sends with their PDF (docs/qa/15/PLAN-015c-invoice-
pdfs.md §3 "E-mail"; ADR-0020; docs/CLAUDE.md §5).

**Who sends invoices.** :func:`company_sends` reads the company setting "Invoice e-mails sent
by" (``CompanySettings.invoice_sender``). While it is the processor (the default) the app sends
no invoice e-mail and nothing here is called for invoices. When it is the company, the worker's
send of an invoice (``payments.one_off_invoices``) asks the processor for no e-mail and calls
:func:`queue_invoice` instead.

**The messages.** :func:`queue_invoice` queues the template ``invoice_sent`` (kind and rule
``invoice``, about the invoice) and :func:`queue_receipt` the template ``receipt`` (kind and rule
``receipt``, about the payment), each to the billing e-mail of the lease the invoice or the
payment bills, through the messaging engine's own store (``messaging._store``): the same
holds (mail not set up, a company setting blank, the sandbox allowlist), the Outbox, the job,
one row per invoice or payment entry (a second call finds it and queues nothing). Neither stores a
PDF: :func:`attachment` renders it when the message is delivered
(``ledger.services.pdf``), and :func:`closed_reason` suppresses an invoice message whose
invoice was voided, or a receipt whose payment is no longer settled money received, before it
goes.

**The values.** An invoice's: the operator, the store, the number, the balance due, the due
date, ``{pay_link}`` (the portal's Pay page of the store, else a signed statement link, as
every message's), the support e-mail and the company's name. The processor's hosted invoice
page is not linked: it offered card payment (PLAN-015g §3B, docs/CLAUDE.md Rule 1). A receipt's:
the operator, the store, the entry's amount, the day it was paid and what it paid
(``{applied_to}``: the invoices' numbers, else the charges' words).

**Receipts.** One receipt per payment entry (``ledger.services.pdf.receipt.entry_of``): the
message is about the entry's first row, so any row of it finds the one message and queues
nothing more. :func:`queue_receipt_if_due` is called by the services that record a payment by
hand (rent, one-off and insurance charges) for a payment of no entry; an entry's rows (an
``allocation_group``) are queued once by its writer after they are all written
(:func:`queue_entry_receipt`: the store page's payment form, the invoice builder's "Record
payment"). Either way, a settled payment a staff member recorded with a
method in :data:`RECEIPT_METHODS` (Zelle, wire, check, cash) gets the receipt; a payment
through the processor never does (the processor's receipt stays), nor a credit applied, an
``other`` payment (the lease end's deposit offsets) or a payment recorded by no one (an
import, a bank deposit confirmed automatically). It never refuses
the payment: a receipt that cannot be queued (no lease, the template inactive) is logged,
value-free, and skipped.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

import mail
from finance.dates import day_text
from ledger import merge
from ledger.models import (
    ChargeEntityType,
    CompanySettings,
    Invoice,
    InvoiceSender,
    InvoiceStatus,
    Lease,
    MessageKind,
    MessageTemplate,
    OutboundMessage,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import messaging
from ledger.services.pdf import invoice as invoice_pdf
from ledger.services.pdf import receipt as receipt_pdf
from ledger.wording import format_amount
from mail.base import SUBJECT_MAX_LENGTH
from payments import lease_sync

logger = logging.getLogger(__name__)

#: PLAN-015c: the methods recorded outside the processor whose payment gets the app's receipt.
RECEIPT_METHODS: frozenset[str] = frozenset(
    {PaymentMethod.ZELLE, PaymentMethod.WIRE, PaymentMethod.CHECK, PaymentMethod.CASH}
)
#: Why a queued invoice or receipt message was not sent at all: class names, as ``error``.
INVOICE_VOID = "InvoiceVoid"
NO_RECEIPT = "NoReceipt"
NO_LEASE = "An invoice or receipt e-mail needs the lease it bills."


def company_sends() -> bool:
    """Whether the company, not the processor, e-mails the invoices (ADR-0020)."""
    return bool(CompanySettings.read().invoice_sender == InvoiceSender.COMPANY)


def billing_lease_of_invoice(invoice: Invoice) -> Lease | None:
    """The lease whose billing e-mail the invoice goes to: the lease of its charge, else the
    operator's first active lease (the invoice builder's rule)."""
    from ledger.services.invoice_builder import billing_account

    return billing_account(invoice.operator, invoice_pdf.lease_of(invoice))


def invoice_context(invoice: Invoice, lease: Lease, company: CompanySettings) -> dict[str, str]:
    """The values of ``invoice_sent`` (``ledger.merge.INVOICE_FIELDS``)."""
    store = lease.store
    found = invoice_pdf.facts(invoice)
    return {
        "operator_name": str(invoice.operator.display_name),
        "store_number": str(store.store_number) if store.store_number is not None else "",
        "store_name": str(store.external_label),
        "invoice_number": str(invoice.invoice_number),
        "amount": format_amount(found.balance),
        "due_date": day_text(invoice.due_date),
        "pay_link": messaging.pay_link(invoice.operator, store.pk),
        "support_email": str(company.support_email or ""),
        "company_name": str(company.name),
    }


def receipt_context(payment: Payment, lease: Lease, company: CompanySettings) -> dict[str, str]:
    """The values of ``receipt`` (``ledger.merge.RECEIPT_FIELDS``)."""
    store = lease.store
    operator = lease.operator
    rows = receipt_pdf.entry_of(payment)
    numbers = [
        str(paid.invoice_number) for paid in map(receipt_pdf.invoice_of, rows) if paid is not None
    ]
    words = [text for text in map(receipt_pdf.charge_text, rows) if text]
    on = rows[0].settled_on or rows[0].initiated_on
    return {
        "operator_name": str(operator.display_name) if operator is not None else "",
        "store_number": str(store.store_number) if store.store_number is not None else "",
        "store_name": str(store.external_label),
        "amount": format_amount(sum((row.amount for row in rows), Decimal("0.00"))),
        "paid_on": day_text(on) if on is not None else "",
        "applied_to": ", ".join(dict.fromkeys(numbers or words)),
        "support_email": str(company.support_email or ""),
        "company_name": str(company.name),
    }


def _queue(
    kind: str,
    entity_type: str,
    entity_id: uuid.UUID,
    lease: Lease,
    template: MessageTemplate,
    values: Mapping[str, str],
    actor: User | None,
    now: datetime | None,
) -> OutboundMessage | None:
    """Render and store one invoice or receipt message (its rule is its kind,
    ``ledger.models.PDF_MESSAGES``)."""
    messaging.refuse_retired(kind, template)
    messaging.channel_for(template.channel)
    operator = lease.operator
    if operator is None:
        raise ValueError(NO_LEASE)
    shown = messaging.with_gaps(values)
    subject = messaging.one_line(merge.render(template.subject, shown))[:SUBJECT_MAX_LENGTH]
    body = merge.render(template.body, shown)
    message = OutboundMessage(
        operator=operator,
        lease=lease,
        template=template,
        kind=kind,
        channel=template.channel,
        rule=kind,
        charge_entity_type=entity_type,
        charge_entity_id=entity_id,
        to_address=lease_sync.billing_of(lease).email,
        rendered_subject=subject,
        rendered_body=body,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        sent_by=actor,
        scheduled_for=messaging.scheduled_for(now or timezone.now()),
    )
    return messaging.store_message(message, actor)


def _template(code: str) -> MessageTemplate:
    template = MessageTemplate.objects.filter(code=code).first()
    if template is None:
        raise ValueError(messaging.INACTIVE_TEMPLATE)
    return template


def queue_invoice(
    invoice: Invoice, *, actor: User | None = None, now: datetime | None = None
) -> OutboundMessage | None:
    """Queue the invoice's e-mail with its PDF (see the module docstring); ``None`` when it
    is queued already. ``ValueError`` and nothing written without a lease to bill or with the
    template inactive."""
    lease = billing_lease_of_invoice(invoice)
    if lease is None:
        raise ValueError(NO_LEASE)
    company = CompanySettings.read()
    return _queue(
        MessageKind.INVOICE,
        ChargeEntityType.INVOICE,
        invoice.pk,
        lease,
        _template(merge.INVOICE_CODE),
        invoice_context(invoice, lease, company),
        actor,
        now,
    )


def queue_receipt(
    payment: Payment, *, actor: User | None = None, now: datetime | None = None
) -> OutboundMessage | None:
    """Queue the receipt of the payment's entry with its PDF, about the entry's first row;
    ``None`` when it is queued already (by this row or another of the entry). ``ValueError``
    and nothing written for a payment without a receipt or a lease, or with the template
    inactive."""
    if not receipt_pdf.has_receipt(payment):
        raise ValueError(NO_RECEIPT)
    first = receipt_pdf.entry_of(payment)[0]
    lease = receipt_pdf.lease_of(first)
    if lease is None:
        raise ValueError(NO_LEASE)
    company = CompanySettings.read()
    return _queue(
        MessageKind.RECEIPT,
        ChargeEntityType.PAYMENT,
        first.pk,
        lease,
        _template(merge.RECEIPT_CODE),
        receipt_context(payment, lease, company),
        actor,
        now,
    )


def _receipt_due(payment: Payment, actor: User | None) -> bool:
    return (
        actor is not None
        and payment.method in RECEIPT_METHODS
        and payment.status == PaymentStatus.SETTLED
    )


def queue_receipt_if_due(payment: Payment, actor: User | None = None) -> OutboundMessage | None:
    """The receipt of a settled payment a staff member (``actor``) recorded by hand in
    :data:`RECEIPT_METHODS`; ``None`` for any other payment (one recorded by no one: an
    import, a bank deposit confirmed automatically), for a row of an entry (its writer queues
    the entry's one receipt, :func:`queue_entry_receipt`), or when it cannot be queued
    (logged, the payment kept)."""
    if payment.allocation_group is not None:
        return None
    return _queue_receipt_quietly(payment, actor)


def queue_entry_receipt(
    payments: Sequence[Payment], actor: User | None = None
) -> OutboundMessage | None:
    """The one receipt of a payment entry, queued by its writer once all its rows are
    written: the first row due (as :func:`queue_receipt_if_due`) queues it; ``None`` when no
    row is due."""
    for payment in payments:
        if _receipt_due(payment, actor):
            return _queue_receipt_quietly(payment, actor)
    return None


def _queue_receipt_quietly(payment: Payment, actor: User | None) -> OutboundMessage | None:
    if not _receipt_due(payment, actor):
        return None
    try:
        return queue_receipt(payment, actor=actor)
    except (ValueError, merge.TemplateRefused, messaging.ChannelUnavailable) as error:
        logger.warning("No receipt e-mail was queued (%s).", type(error).__name__)
        return None


# --- At the send ------------------------------------------------------------------------------


def closed_reason(message: OutboundMessage) -> str:
    """Why an invoice or receipt message is no longer to go: its invoice void or gone
    (:data:`INVOICE_VOID`), its payment no longer settled money received or gone
    (:data:`NO_RECEIPT`); ``""`` otherwise or for any other message."""
    if message.kind == MessageKind.INVOICE:
        status = (
            Invoice.objects.filter(pk=message.charge_entity_id)
            .values_list("internal_status", flat=True)
            .first()
        )
        return INVOICE_VOID if status in (None, InvoiceStatus.VOID) else ""
    if message.kind == MessageKind.RECEIPT:
        payment = Payment.objects.filter(pk=message.charge_entity_id).first()
        return "" if payment is not None and receipt_pdf.has_receipt(payment) else NO_RECEIPT
    return ""


def attachment(message: OutboundMessage) -> mail.Attachment:
    """The PDF of an invoice or receipt message, rendered now; the model's or the renderer's
    error when it cannot be."""
    today = timezone.localdate()
    if message.kind == MessageKind.INVOICE:
        invoice = Invoice.objects.select_related("operator").get(pk=message.charge_entity_id)
        return mail.Attachment(
            invoice_pdf.pdf_filename(invoice),
            "application/pdf",
            invoice_pdf.render_invoice(invoice, today=today),
        )
    payment = Payment.objects.get(pk=message.charge_entity_id)
    return mail.Attachment(
        receipt_pdf.receipt_filename(payment),
        "application/pdf",
        receipt_pdf.render_receipt(payment, today=today),
    )
