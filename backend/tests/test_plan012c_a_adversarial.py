"""Adversarial cases for PLAN-012c-a (every builder line linked to its invoice; a multi-line
invoice paid line by line), by the QA owner (plan §6). A later line must be payable only
through its own page, the provider must be told "paid" exactly once per covered invoice, the
check command must never write, and a rent charge's invoice resolution must not move."""

from __future__ import annotations

import io
import uuid
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Invoice,
    InvoiceDraft,
    InvoiceLineItem,
    OneOffCharge,
    Payment,
    PaymentMethod,
)
from ledger.services import invoice_builder, rent_run
from ledger.services import one_off_charges as service
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job, JobKind
from tests.job_helpers import run_queued_jobs
from tests.test_invoice_builder import TODAY, a_draft, a_world, line, user
from tests.test_invoice_builder import fake as builder_fake
from tests.test_invoice_builder import fixed_now as builder_fixed_now
from tests.test_invoice_builder import worker as builder_worker
from tests.test_lease_services import make_lease
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture: the webhook path)

pytestmark = pytest.mark.django_db

D = Decimal
fake = builder_fake
fixed_now = builder_fixed_now
worker = builder_worker


def _two_lines(fake: FakeProvider) -> tuple[Invoice, OneOffCharge, OneOffCharge]:
    """The owner's invoice: Damage Repair 300 and a late fee 50, sent and created."""
    operator, lease = a_world(fake)
    draft = a_draft(
        operator,
        lease,
        [line(item="Damage Repair", unit="300.00"), line(item="Late fee", unit="50.00")],
    )
    sent = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert sent.ok and sent.invoice is not None, sent.message
    run_queued_jobs()
    first, second = invoice_builder.sent_charges(InvoiceDraft.objects.get(pk=draft.pk))
    return Invoice.objects.get(pk=sent.invoice.pk), first, second


def _wire(amount: str, reference: str) -> dict[str, str]:
    return {
        "creation_key": str(uuid.uuid4()),
        "method": PaymentMethod.WIRE.value,
        "amount": amount,
        "paid_on": TODAY.isoformat(),
        "reference": reference,
        "note": f"Wire {reference}.",
    }


def _marks(fake: FakeProvider) -> list[tuple[object, ...]]:
    return [a for m, a in fake.calls if m == "mark_invoice_paid_cash_or_check"]


# --- a later line is paid only through its own page ----------------------------------------------


def test_a_later_line_cannot_be_paid_from_another_operators_charge_page(
    fake: FakeProvider, worker: None
) -> None:
    invoice, first, late_fee = _two_lines(fake)
    operator_b = an_operator(fake, customer="cus-b", bank="pm-b")
    lease_b = make_lease(operator=operator_b)
    draft_b = a_draft(operator_b, lease_b, [line(item="B's own line", unit="20.00")])
    sent_b = invoice_builder.send(draft_b.pk, user(Role.BILLING), today=TODAY)
    assert sent_b.ok
    run_queued_jobs()
    [charge_b] = invoice_builder.sent_charges(InvoiceDraft.objects.get(pk=draft_b.pk))
    billing, _ = client_for(Role.BILLING)
    for forged in (
        {"charge": str(late_fee.pk)},
        {"charge": str(late_fee.pk), "invoice": str(invoice.pk)},
        {"one_off_charge": str(late_fee.pk), "lease": str(first.lease_id)},
    ):
        billing.post(
            f"/one-off-charges/{charge_b.pk}/payments/new/", {**_wire("50.00", "FORGED"), **forged}
        )
    run_queued_jobs()
    assert not Payment.objects.filter(one_off_charge__in=[first.pk, late_fee.pk]).exists()
    assert OneOffCharge.objects.get(pk=late_fee.pk).amount_paid == 0
    # B's own invoice may have been marked; A's never was.
    assert all(args[0] != _provider_ref(invoice) for args in _marks(fake))


def _provider_ref(invoice: Invoice) -> str:
    return str(
        ExternalRef.objects.filter(
            entity_type=rent_run.ENTITY_INVOICE, entity_id=invoice.pk, superseded_at__isnull=True
        )
        .values_list("external_id", flat=True)
        .first()
    )


# --- one mark per covered invoice, whatever the order and the timing ----------------------------


@pytest.mark.parametrize("order", ["first-then-late", "late-then-first"])
def test_the_provider_is_told_paid_exactly_once_when_both_lines_are_paid_before_the_worker_runs(
    fake: FakeProvider, worker: None, order: str
) -> None:
    invoice, first, late_fee = _two_lines(fake)
    billing, _ = client_for(Role.BILLING)
    sequence = [(first, "300.00"), (late_fee, "50.00")]
    if order == "late-then-first":
        sequence.reverse()
    for charge, amount in sequence:
        response = billing.post(
            f"/one-off-charges/{charge.pk}/payments/new/", _wire(amount, f"W-{amount}")
        )
        assert response.status_code == 302, (charge.description, response.status_code)
    # Nothing has reached the provider yet; now the worker drains the queue.
    assert _marks(fake) == []
    run_queued_jobs()
    run_queued_jobs()
    marks = _marks(fake)
    assert len(marks) == 1 and marks[0][0] == _provider_ref(invoice)
    for charge in (first, late_fee):
        row = OneOffCharge.objects.get(pk=charge.pk)
        assert row.balance == 0 and not service.review_message(row), row.description
    assert Job.objects.filter(kind=JobKind.ONE_OFF_PAYMENT_MARK).count() == 1
    # Nothing more can be recorded or waived on either line now.
    for charge in (first, late_fee):
        page = billing.get(f"/one-off-charges/{charge.pk}/").content.decode()
        assert "payments/new/" not in page
        assert invoice.invoice_number in page
        waived = service.waive(charge.pk, note="no", actor=user(Role.ADMIN))
        assert not waived.ok


def test_the_partial_flag_sits_only_on_the_paying_line_and_the_other_still_shows_the_invoice(
    fake: FakeProvider, worker: None
) -> None:
    invoice, first, late_fee = _two_lines(fake)
    billing, _ = client_for(Role.BILLING)
    assert (
        billing.post(
            f"/one-off-charges/{first.pk}/payments/new/", _wire("300.00", "W-300")
        ).status_code
        == 302
    )
    run_queued_jobs()
    assert _marks(fake) == []
    assert service.review_message(OneOffCharge.objects.get(pk=first.pk))
    assert not service.review_message(OneOffCharge.objects.get(pk=late_fee.pk))
    page = billing.get(f"/one-off-charges/{late_fee.pk}/").content.decode()
    assert invoice.invoice_number in page and "No invoice linked" not in page
    assert f"/one-off-charges/{late_fee.pk}/payments/new/" in page
    assert "link-invoice" not in page and "Link invoice" not in page
    admin_page = client_for(Role.ADMIN)[0].get(f"/one-off-charges/{late_fee.pk}/").content.decode()
    assert str(service.WAIVE_LIVE_INVOICE) in admin_page  # a lazy sentence since PLAN-012c D9


# --- the check command never writes ---------------------------------------------------------


def test_invoice_line_links_check_reads_only_and_names_an_orphan(
    fake: FakeProvider, worker: None
) -> None:
    invoice, first, late_fee = _two_lines(fake)
    before = (
        AuditLog.objects.count(),
        list(InvoiceLineItem.objects.values_list("pk", "one_off_charge")),
        list(Invoice.objects.values_list("pk", "internal_status", "total")),
    )
    out = io.StringIO()
    call_command("invoice_line_links", "--check", stdout=out)
    InvoiceLineItem.objects.filter(invoice=invoice, one_off_charge=late_fee).update(
        one_off_charge=None
    )
    with pytest.raises(CommandError) as raised:
        call_command("invoice_line_links", "--check", stdout=out)
    assert invoice.invoice_number in (str(raised.value) + out.getvalue())
    InvoiceLineItem.objects.filter(invoice=invoice, position=2).update(one_off_charge=late_fee)
    after = (
        AuditLog.objects.count(),
        list(InvoiceLineItem.objects.values_list("pk", "one_off_charge")),
        list(Invoice.objects.values_list("pk", "internal_status", "total")),
    )
    assert after == before


# --- a rent charge's invoice resolution does not move --------------------------------------------


def test_a_rent_charges_provider_invoice_is_resolved_as_before(fake: FakeProvider) -> None:
    from tests.test_webhook_handlers import make_world

    world = make_world(fake)
    linked = rent_run.provider_invoice(world.charge)
    assert linked is not None and linked[0].pk == world.invoice.pk
    direct = (
        ExternalRef.objects.filter(
            entity_type=rent_run.ENTITY_INVOICE,
            entity_id=world.invoice.pk,
            external_kind=rent_run.KIND_INVOICE,
            superseded_at__isnull=True,
        )
        .values_list("external_id", flat=True)
        .first()
    )
    assert linked[1] == direct
