"""PLAN-015c task 6 (Opus): the invoice and receipt e-mails behind "Invoice e-mails sent by"
(ADR-0020). With ``company`` the worker's send asks the provider for no e-mail and the app's
invoice message is delivered through the memory mail with its PDF; with ``processor`` (the
default) nothing changes: the provider's call, no app message. A Zelle payment queues a
receipt, an ACH payment none; held while mail is off; no hosted pay link (PLAN-015g); the links
point at APP_BASE_URL; the Outbox lists both kinds; a voided
invoice's message is suppressed."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import override_settings

import mail
from accounts.roles import Role
from finance.dates import day_text
from ledger import merge
from ledger.models import (
    AuditLog,
    CompanySettings,
    Invoice,
    InvoiceSender,
    InvoiceStatus,
    LeaseBilling,
    MessageKind,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OutboundMessage,
    PaymentMethod,
    Store,
)
from ledger.services import invoice_messages, message_templates, messaging, rent_run
from mail.memory import MemoryMailSender
from payments.fake import FakeProvider
from payments.models import ExternalRef
from tests.job_helpers import run_queued_jobs
from tests.pdf_helpers import billed_store, company, pdf_text
from tests.receivables_helpers import a_rent_charge
from tests.test_invoice_builder import a_draft, a_world, fresh, sent, two_lines
from tests.test_one_off_charges import calls
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.text_helpers import assert_present

pytestmark = pytest.mark.django_db

D = Decimal
BILLING_EMAIL = "ops-15c@example.com"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


@pytest.fixture(autouse=True)
def window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The send window is tested in test_messaging_engine.py; here a message goes at once."""
    monkeypatch.setattr(messaging, "scheduled_for", lambda now: now)


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch, settings: Any) -> MemoryMailSender:
    """Mail set up off the sandbox (as test_lease_end's): on, a key, a sender address."""
    settings.MAIL_ENABLED = True
    settings.GMAIL_SERVICE_ACCOUNT_JSON = "{}"
    settings.STAX_ENV = "live"
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(
        sender_email="billing@example.com"
    )
    sender = MemoryMailSender()
    monkeypatch.setattr(mail, "_base_sender", lambda: sender)
    return sender


def sender(choice: str) -> None:
    company(support_email="help@example.com")
    CompanySettings.objects.filter(pk=CompanySettings.current().pk).update(invoice_sender=choice)


def seeded(code: str) -> MessageTemplate:
    """The template of 0071 as 0078 leaves it (written again from the migrations when a test
    flushed it)."""
    import importlib

    found = MessageTemplate.objects.filter(code=code).first()
    if found is not None:
        return found
    migration = importlib.import_module("ledger.migrations.0071_invoice_pdfs")
    portal_only = importlib.import_module("ledger.migrations.0078_invoice_email_portal_link_only")
    for seeded_code, name, subject, body in migration.TEMPLATES:
        if seeded_code == code:
            return MessageTemplate.objects.create(
                code=code,
                name=name,
                subject=subject,
                body=portal_only.without_provider_pay_line(body),
            )
    raise AssertionError(code)


def billed(fake: FakeProvider) -> tuple[Any, Any]:
    operator, lease = a_world(fake)
    LeaseBilling.objects.update_or_create(lease=lease, defaults={"email": BILLING_EMAIL})
    Store.objects.filter(pk=lease.store_id).update(store_number=1991)
    return operator, lease


def send_draft(fake: FakeProvider) -> Invoice:
    for code in merge.PDF_CODES:
        seeded(code)
    operator, lease = billed(fake)
    invoice = sent(a_draft(operator, lease, two_lines())).invoice
    assert invoice is not None
    run_queued_jobs()
    found: Invoice = fresh(invoice)
    return found


# --- The invoice e-mail ------------------------------------------------------------------------


def test_with_the_company_sending_no_provider_email_and_the_app_delivers_the_pdf(
    fake: FakeProvider, worker: None, memory: MemoryMailSender
) -> None:
    sender(InvoiceSender.COMPANY)
    invoice = send_draft(fake)
    assert not calls(fake, "send_invoice_email")
    assert invoice.internal_status == InvoiceStatus.SENT
    [message] = OutboundMessage.objects.filter(kind=MessageKind.INVOICE)
    assert (message.rule, message.charge_entity_type, message.charge_entity_id) == (
        MessageRule.INVOICE,
        "invoice",
        invoice.pk,
    )
    assert message.status == MessageStatus.SENT and message.document is None
    [delivered] = memory.sent
    assert delivered.to == BILLING_EMAIL
    due = day_text(invoice.due_date)
    assert delivered.subject == f"Invoice {invoice.invoice_number} — $150.00 due {due}"
    [attachment] = delivered.attachments
    assert (attachment.filename, attachment.content_type) == (
        f"{invoice.invoice_number}.pdf",
        "application/pdf",
    )
    assert attachment.data.startswith(b"%PDF")
    assert invoice.invoice_number in pdf_text(attachment.data)
    # PLAN-015g: no hosted pay link line, nothing shows a gap.
    assert "{provider_pay_link}" not in delivered.text
    assert "Or pay on our payment page" not in delivered.text
    entry = AuditLog.objects.get(action="invoice.sent", entity_id=str(invoice.pk))
    assert entry.after is not None and entry.after["sender"] == "company"


def test_with_the_processor_sending_nothing_changes(
    fake: FakeProvider, worker: None, memory: MemoryMailSender
) -> None:
    sender(InvoiceSender.PROCESSOR)
    invoice = send_draft(fake)
    assert len(calls(fake, "send_invoice_email")) == 1
    assert invoice.internal_status == InvoiceStatus.SENT
    assert not OutboundMessage.objects.filter(kind=MessageKind.INVOICE).exists()
    assert memory.sent == []


def test_the_invoice_email_is_held_while_mail_is_off(fake: FakeProvider, worker: None) -> None:
    sender(InvoiceSender.COMPANY)
    send_draft(fake)
    assert not calls(fake, "send_invoice_email")
    [message] = OutboundMessage.objects.filter(kind=MessageKind.INVOICE)
    assert message.status == MessageStatus.HELD


def test_the_body_links_the_portal_alone_under_app_base_url(
    fake: FakeProvider, worker: None, settings: Any
) -> None:
    # PLAN-015g §3B: the processor's hosted page offered card payment; the e-mail links the
    # portal only, whichever provider is configured.
    from payments.stax.adapter import DEFAULT_PAYMENT_PAGE_URL

    sender(InvoiceSender.COMPANY)
    settings.STAX_PROVIDER = "stax"
    invoice = send_draft(fake)
    [message] = OutboundMessage.objects.filter(kind=MessageKind.INVOICE)
    external = ExternalRef.objects.get(entity_type="invoice", entity_id=invoice.pk).external_id
    body = message.rendered_body
    assert "Or pay on our payment page" not in body
    assert DEFAULT_PAYMENT_PAGE_URL not in body and "#/bill/" not in body
    assert external not in body
    pay = body.split("Pay by bank debit (ACH) here: ", 1)[1].split("\n", 1)[0]
    assert pay.startswith(f"{settings.APP_BASE_URL.rstrip('/')}/")
    assert "Questions? Write to help@example.com." in body
    assert "{" not in body


def test_a_second_queue_of_the_same_invoice_queues_nothing(
    fake: FakeProvider, worker: None
) -> None:
    sender(InvoiceSender.COMPANY)
    invoice = send_draft(fake)
    assert invoice_messages.queue_invoice(invoice) is None
    assert OutboundMessage.objects.filter(kind=MessageKind.INVOICE).count() == 1


def test_a_voided_invoice_is_not_emailed(
    fake: FakeProvider, worker: None, memory: MemoryMailSender, monkeypatch: pytest.MonkeyPatch
) -> None:
    sender(InvoiceSender.COMPANY)
    monkeypatch.setattr(messaging, "scheduled_for", lambda now: now.replace(year=now.year + 1))
    invoice = send_draft(fake)
    [message] = OutboundMessage.objects.filter(kind=MessageKind.INVOICE)
    Invoice.objects.filter(pk=invoice.pk).update(internal_status=InvoiceStatus.VOID)
    messaging.send(message.pk)
    message = fresh(message)
    assert (message.status, message.error) == (MessageStatus.SUPPRESSED, "InvoiceVoid")
    assert memory.sent == []


# --- Receipts ----------------------------------------------------------------------------------


def a_billed_rent_charge() -> Any:
    for code in merge.PDF_CODES:
        seeded(code)
    company(support_email="help@example.com")
    lease = billed_store()
    LeaseBilling.objects.filter(lease=lease).update(email=BILLING_EMAIL)
    return a_rent_charge(lease, date(2026, 11, 1), due="1850.00")


def record(charge: Any, method: str) -> Any:
    outcome = rent_run.record_manual_payment(
        charge.pk,
        method=method,
        amount=D("850.00"),
        paid_on=date(2026, 11, 3),
        note="Received at the office",
        actor=client_for(Role.BILLING)[1],
        reference="ZL-778812-QX",
        defer_mark=True,
    )
    assert outcome.ok, outcome.message
    return outcome.payment


@pytest.mark.parametrize(
    "method", [PaymentMethod.ZELLE, PaymentMethod.WIRE, PaymentMethod.CHECK, PaymentMethod.CASH]
)
def test_a_payment_recorded_by_hand_queues_its_receipt_and_it_goes_with_the_pdf(
    method: str, memory: MemoryMailSender
) -> None:
    payment = record(a_billed_rent_charge(), method)
    [message] = OutboundMessage.objects.filter(kind=MessageKind.RECEIPT)
    assert (message.rule, message.charge_entity_type, message.charge_entity_id) == (
        MessageRule.RECEIPT,
        "payment",
        payment.pk,
    )
    assert message.rendered_subject == "Receipt for $850.00"
    assert "we received $850.00 on Nov 3, 2026 for Rent — November 2026" in message.rendered_body
    run_queued_jobs()
    [delivered] = memory.sent
    [attachment] = delivered.attachments
    assert attachment.filename.startswith("receipt-RCPT-") and attachment.data.startswith(b"%PDF")
    text = pdf_text(attachment.data)
    assert_present(text, "RECEIPT")
    assert "ZL-778812-QX" not in text


def test_an_ach_payment_queues_no_receipt() -> None:
    from tests.receivables_helpers import a_payment

    ach = a_payment(a_billed_rent_charge(), "100.00", date(2026, 11, 3))
    assert invoice_messages.queue_receipt_if_due(ach, client_for(Role.BILLING)[1]) is None
    assert not OutboundMessage.objects.filter(kind=MessageKind.RECEIPT).exists()


def test_a_payment_recorded_by_no_one_queues_no_receipt() -> None:
    outcome = rent_run.record_manual_payment(
        a_billed_rent_charge().pk,
        method=PaymentMethod.ZELLE,
        amount=D("850.00"),
        paid_on=date(2026, 11, 3),
        note="Imported",
        actor=None,
        defer_mark=True,
    )
    assert outcome.ok and outcome.payment is not None
    assert not OutboundMessage.objects.filter(kind=MessageKind.RECEIPT).exists()


def test_an_other_method_payment_queues_no_receipt() -> None:
    record(a_billed_rent_charge(), PaymentMethod.OTHER)
    assert not OutboundMessage.objects.filter(kind=MessageKind.RECEIPT).exists()


def test_the_receipt_is_held_while_mail_is_off() -> None:
    record(a_billed_rent_charge(), PaymentMethod.ZELLE)
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(kind=MessageKind.RECEIPT)
    assert message.status == MessageStatus.HELD


def test_a_receipt_that_cannot_be_queued_keeps_the_payment() -> None:
    charge = a_billed_rent_charge()
    MessageTemplate.objects.filter(code=merge.RECEIPT_CODE).update(active=False)
    payment = record(charge, PaymentMethod.ZELLE)
    assert payment.pk is not None
    assert not OutboundMessage.objects.filter(kind=MessageKind.RECEIPT).exists()


def test_a_voided_payment_s_receipt_is_suppressed(memory: MemoryMailSender) -> None:
    from ledger.models import Payment

    payment = record(a_billed_rent_charge(), PaymentMethod.ZELLE)
    [message] = OutboundMessage.objects.filter(kind=MessageKind.RECEIPT)
    Payment.objects.filter(pk=payment.pk).update(status="voided")
    messaging.send(message.pk)
    assert (fresh(message).status, fresh(message).error) == (MessageStatus.SUPPRESSED, "NoReceipt")
    assert memory.sent == []


# --- The Outbox and the templates' preview -----------------------------------------------------


def test_the_outbox_lists_the_invoice_and_the_receipt_with_their_kind(
    fake: FakeProvider, worker: None
) -> None:
    sender(InvoiceSender.COMPANY)
    send_draft(fake)
    record(a_billed_rent_charge(), PaymentMethod.ZELLE)
    page = client_for(Role.ADMIN)[0].get("/admin-settings/outbox/").content.decode()
    assert ">Invoice e-mailed</td>" in page
    assert ">Receipt e-mailed</td>" in page


def test_the_template_preview_renders_both_with_sample_values() -> None:
    lease = billed_store()
    for code in merge.PDF_CODES:
        template = seeded(code)
        found = message_templates.preview(
            template, lease.store_id, template.subject, template.body, date(2026, 11, 7)
        )
        assert found.ok, (code, found.message)
        assert "{" not in found.body


def test_the_settings_page_names_the_provider_in_the_sender_choice() -> None:
    row = CompanySettings.current()
    row.processor_name = "PayCo"
    row.save()
    page = client_for(Role.ADMIN)[0].get("/admin-settings/").content.decode()
    assert 'id="invoice-sender-processor" checked> PayCo' in page
