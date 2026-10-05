"""Required notes judged by their visible text (docs/qa/PLAN-010c D12, Batch C): the
manual-payment note (rent and setup charges), the return-resolution note and the
link-invoice note when the totals differ. Each is cleaned by ``visible_text`` before the
emptiness check, in the form and in the service, and the cleaned text is what is stored
and audited. The invisible-character cases are those of ``test_reconciliation.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from ledger.forms import LinkInvoiceForm, ManualPaymentForm, ResolveReturnForm
from ledger.models import AuditLog, Invoice, Payment
from ledger.services import rent_run, returns
from ledger.services import setup_charges as setup_service
from payments.fake import FakeProvider
from tests.test_reconciliation import INVISIBLE_NOTES
from tests.test_rent_run import charged, health  # noqa: F401 (autouse fixture)
from tests.test_rent_run import pay as pay_rent
from tests.test_setup_charges import a_charge, unlinked_invoice
from tests.test_setup_charges import pay as pay_setup
from tests.test_webhook_handlers import returned_payment

pytestmark = pytest.mark.django_db

D = Decimal
SHOWN = " ​Wire⁠ received.﻿ "
CLEANED = "Wire received."


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


# --- Forms ---------------------------------------------------------------------------------


def _payment_form(note: str) -> ManualPaymentForm:
    return ManualPaymentForm(
        {
            "creation_key": str(uuid.uuid4()),
            "method": "wire",
            "amount": "10.00",
            "paid_on": "2026-10-15",
            "note": note,
        }
    )


@pytest.mark.parametrize("note", INVISIBLE_NOTES)
def test_the_payment_and_resolve_forms_refuse_a_note_with_nothing_visible(note: str) -> None:
    payment = _payment_form(note)
    assert not payment.is_valid()
    assert payment.errors["note"] == ["A note is required: say what was received."]
    resolve = ResolveReturnForm({"note": note})
    assert not resolve.is_valid()
    assert resolve.errors["note"] == ["A note is required: say how the return was resolved."]


def test_the_forms_keep_the_note_without_invisible_characters() -> None:
    payment = _payment_form(SHOWN)
    assert payment.is_valid() and payment.cleaned_data["note"] == CLEANED
    resolve = ResolveReturnForm({"note": SHOWN})
    assert resolve.is_valid() and resolve.cleaned_data["note"] == CLEANED


@pytest.mark.parametrize("note", INVISIBLE_NOTES)
def test_the_link_form_note_stays_optional_and_is_cleaned_to_nothing(note: str) -> None:
    form = LinkInvoiceForm({"invoice": str(uuid.uuid4()), "note": note})
    assert form.is_valid()
    assert form.cleaned_data["note"] == ""


def test_the_link_form_keeps_a_note_without_invisible_characters() -> None:
    form = LinkInvoiceForm({"invoice": str(uuid.uuid4()), "note": SHOWN})
    assert form.is_valid() and form.cleaned_data["note"] == CLEANED


# --- Services: manual payments -------------------------------------------------------------


def test_a_rent_payment_needs_a_visible_note_and_stores_the_cleaned_one(fake: FakeProvider) -> None:
    charge = charged()
    for note in INVISIBLE_NOTES:
        outcome = pay_rent(charge, "500.00", note=note)
        assert (outcome.ok, outcome.message, outcome.field) == (
            False,
            rent_run.PAY_NOTE_REQUIRED,
            "note",
        ), repr(note)
    assert not Payment.objects.filter(rent_charge=charge).exists()
    done = pay_rent(charge, "500.00", note=SHOWN)
    assert done.ok and done.payment is not None
    assert Payment.objects.get(pk=done.payment.pk).note == CLEANED


def test_a_setup_payment_needs_a_visible_note_and_stores_the_cleaned_one() -> None:
    charge = a_charge()
    for note in INVISIBLE_NOTES:
        outcome = pay_setup(charge, "100.00", note=note)
        assert (outcome.ok, outcome.message) == (False, rent_run.PAY_NOTE_REQUIRED), repr(note)
    assert not Payment.objects.filter(setup_charge=charge).exists()
    done = pay_setup(charge, "100.00", note=SHOWN)
    assert done.ok and done.payment is not None
    assert Payment.objects.get(pk=done.payment.pk).note == CLEANED


# --- Services: return resolution -----------------------------------------------------------


def test_a_return_resolution_needs_a_visible_note_and_stores_the_cleaned_one(
    fake: FakeProvider,
) -> None:
    _, payment = returned_payment(fake)
    for note in INVISIBLE_NOTES:
        outcome = returns.resolve_return(payment.pk, note=note, actor=None)
        assert outcome == returns.ResolveOutcome(False, returns.RESOLVE_NOTE_REQUIRED), repr(note)
    payment.refresh_from_db()
    assert (payment.resolved_at, payment.resolution_note) == (None, "")
    assert returns.resolve_return(payment.pk, note=SHOWN, actor=None).ok
    payment.refresh_from_db()
    assert payment.resolution_note == CLEANED
    [audit] = AuditLog.objects.filter(action="payment.return_resolved")
    assert audit.after is not None and audit.after["resolution_note"] == CLEANED


# --- Services: linking an invoice whose total differs --------------------------------------


def _differing_invoice(operator_id: uuid.UUID | None) -> Invoice:
    return unlinked_invoice(
        operator_id, total="900.00", number=f"INV-2026-{uuid.uuid4().int % 9000 + 1000}"
    )


def test_a_rent_link_with_a_differing_total_needs_a_visible_note(fake: FakeProvider) -> None:
    charge = charged()
    invoice = _differing_invoice(charge.lease.operator_id)
    for note in INVISIBLE_NOTES:
        outcome = rent_run.link_invoice(charge.pk, invoice.pk, confirm=True, note=note, actor=None)
        assert (outcome.ok, outcome.message) == (False, rent_run.LINK_NOTE_REQUIRED), repr(note)
    assert not Invoice.objects.filter(rent_charge=charge).exists()
    done = rent_run.link_invoice(charge.pk, invoice.pk, confirm=True, note=SHOWN, actor=None)
    assert done.ok
    [audit] = AuditLog.objects.filter(action="invoice.linked", entity_id=str(invoice.pk))
    assert audit.after is not None and audit.after["note"] == CLEANED


def test_a_setup_link_with_a_differing_total_needs_a_visible_note() -> None:
    charge = a_charge()
    invoice = _differing_invoice(charge.lease.operator_id)
    for note in INVISIBLE_NOTES:
        outcome = setup_service.link_invoice(
            charge.pk, invoice.pk, confirm=True, note=note, actor=None
        )
        assert (outcome.ok, outcome.message) == (False, rent_run.LINK_NOTE_REQUIRED), repr(note)
    done = setup_service.link_invoice(charge.pk, invoice.pk, confirm=True, note=SHOWN, actor=None)
    assert done.ok
    [audit] = AuditLog.objects.filter(action="invoice.linked", entity_id=str(invoice.pk))
    assert audit.after is not None and audit.after["note"] == CLEANED


def test_a_link_whose_total_matches_needs_no_note() -> None:
    charge = a_charge()
    invoice = unlinked_invoice(charge.lease.operator_id, total="1000.00")
    for note in INVISIBLE_NOTES:
        assert not Invoice.objects.filter(setup_charge=charge).exists()
        done = setup_service.link_invoice(
            charge.pk, invoice.pk, confirm=False, note=note, actor=None
        )
        assert done.ok
        Invoice.objects.filter(pk=invoice.pk).update(setup_charge=None)
