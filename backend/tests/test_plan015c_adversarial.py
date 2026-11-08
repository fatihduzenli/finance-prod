"""Fable's adversarial cases for PLAN-015c (docs/qa/15/PLAN-015c-invoice-pdfs.md §5), beside Opus's
``test_plan015c_*`` files: the two principals never cross (a portal user on the staff download
routes, a staff user on the portal routes), and the rent run's own invoices follow the company
sender switch without a provider e-mail.

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider`; its call list is what we read.
"""

from __future__ import annotations

from datetime import date

import pytest

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CompanySettings,
    InvoiceSender,
    MessageRule,
    OutboundMessage,
    PaymentMethod,
)
from ledger.services.pdf import downloads
from payments.fake import FakeProvider
from tests.pdf_helpers import billed_store, builder_invoice, company, settle
from tests.test_operator_views import client_for
from tests.test_plan015c_email import fake as fake  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_plan015c_email import send_draft
from tests.test_plan015c_email import window_open as window_open  # noqa: PLC0414
from tests.test_plan015c_email import worker as worker  # noqa: PLC0414
from tests.test_plan015c_staff_downloads import THREE, invoice_url, receipt_url
from tests.test_portal import World, active_user, portal_client
from tests.test_portal import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_portal import world as world  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db


def downloads_audited() -> int:
    return AuditLog.objects.filter(
        action__in=[downloads.INVOICE_DOWNLOADED, downloads.RECEIPT_DOWNLOADED]
    ).count()


def test_a_portal_user_on_the_staff_download_routes_is_sent_to_the_staff_sign_in(
    world: World, clock: None
) -> None:
    """The portal's session is not a staff session: the staff routes answer 401 (the staff
    shell's unauthenticated answer), render no PDF and audit no download."""
    company()
    invoice, charges = builder_invoice(billed_store(), THREE)
    payment = settle(charges[0], "1000.00", date(2026, 11, 2), PaymentMethod.ZELLE)
    client = portal_client(active_user(world.a))
    for url in (invoice_url(invoice), receipt_url(payment)):
        response = client.get(url)
        assert response.status_code == 401
        assert not response.content.startswith(b"%PDF")
    assert downloads_audited() == 0


@pytest.mark.parametrize("role", [Role.VIEWER, Role.ADMIN])
def test_a_staff_user_on_the_portal_routes_gets_the_portals_answer_not_a_pdf(
    world: World, clock: None, role: Role
) -> None:
    """A staff session is not a portal session: the portal's PDF routes answer with the portal's
    sign-in or its 404, never a PDF, and audit no download."""
    staff = client_for(role)[0]
    for path in (
        f"/portal/invoices/{world.invoice_a.pk}.pdf",
        "/portal/invoices/00000000-0000-0000-0000-000000000000.pdf",
    ):
        response = staff.get(path)
        assert response.status_code in (302, 404)
        assert not response.content.startswith(b"%PDF")
    assert downloads_audited() == 0


def test_with_the_company_sending_the_builder_path_asks_the_provider_for_no_email(
    fake: FakeProvider, worker: None
) -> None:
    """The one call site of the provider's invoice e-mail is behind the setting: with the company
    sending, the provider's call list holds no e-mail call and exactly one invoice message exists;
    switching back to the processor afterwards does not un-send or re-queue anything."""
    row = CompanySettings.current()
    CompanySettings.objects.filter(pk=row.pk).update(invoice_sender=InvoiceSender.COMPANY)
    invoice = send_draft(fake)
    assert [name for name, _ in fake.calls if name == "send_invoice_email"] == []
    assert OutboundMessage.objects.filter(rule=MessageRule.INVOICE).count() == 1
    CompanySettings.objects.filter(pk=row.pk).update(invoice_sender=InvoiceSender.PROCESSOR)
    assert OutboundMessage.objects.filter(rule=MessageRule.INVOICE).count() == 1
    assert invoice.internal_status == "sent"


def test_without_a_billing_email_the_company_send_queues_a_held_message_and_asks_no_provider(
    fake: FakeProvider, worker: None
) -> None:
    """A lease with no billing e-mail: the company's invoice message is queued and HELD (the
    Outbox shows it; staff add the address and release it, as for any reminder), no provider
    e-mail is asked for, and the invoice is marked sent by the company. Pinned so a change to
    "refuse the send instead" is a decision, not an accident."""
    from ledger import merge
    from ledger.models import LeaseBilling, MessageStatus
    from tests.job_helpers import run_queued_jobs
    from tests.test_invoice_builder import a_draft, sent, two_lines
    from tests.test_plan015c_email import billed, seeded

    row = CompanySettings.current()
    CompanySettings.objects.filter(pk=row.pk).update(invoice_sender=InvoiceSender.COMPANY)
    for code in merge.PDF_CODES:
        seeded(code)
    operator, lease = billed(fake)
    LeaseBilling.objects.filter(lease=lease).update(email="")
    outcome = sent(a_draft(operator, lease, two_lines()))
    run_queued_jobs()
    [message] = OutboundMessage.objects.filter(rule=MessageRule.INVOICE)
    assert message.status == MessageStatus.HELD
    assert [name for name, _ in fake.calls if name == "send_invoice_email"] == []
    assert outcome.invoice is not None
