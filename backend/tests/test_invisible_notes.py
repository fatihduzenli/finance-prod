"""Required notes judged by their visible text (docs/qa/PLAN-010c D12, Batch C): the
manual-payment note (rent and setup charges), the return-resolution note and the
link-invoice note when the totals differ. Each is cleaned by ``visible_text`` before the
emptiness check, in the form and in the service, and the cleaned text is what is stored
and audited. The invisible-character cases are those of ``test_reconciliation.py``; the
bidirectional marks and controls (PLAN-010d Batch A finding) are tested at the end, on
the mismatch note, the return-resolution note and the one-off description and note.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from ledger.forms import (
    LinkInvoiceForm,
    ManualPaymentForm,
    ResolveMismatchForm,
    ResolveReturnForm,
)
from ledger.models import AuditLog, CollectionMethod, Invoice, OneOffCharge, Payment
from ledger.services import one_off_charges as setup_service
from ledger.services import rent_run, returns
from ledger.validators import contains_card_number, visible_text
from payments.fake import FakeProvider
from payments.models import ReconciliationMismatch, ReconciliationTrigger
from payments.reconciliation import mismatches, run_reconciliation
from tests.test_one_off_charges import an_operator
from tests.test_one_off_charges import item as one_off_item
from tests.test_reconciliation import INVISIBLE_NOTES, pending_payment
from tests.test_reconciliation import NOW as RECONCILED_AT
from tests.test_rent_run import charged, health  # noqa: F401 (autouse fixture)
from tests.test_rent_run import pay as pay_rent
from tests.test_setup_charges import a_charge, lease_of, unlinked_invoice
from tests.test_setup_charges import pay as pay_setup
from tests.test_webhook_handlers import make_world, returned_payment

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
    assert not Payment.objects.filter(one_off_charge=charge).exists()
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
    invoice = _differing_invoice(lease_of(charge).operator_id)
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
    invoice = unlinked_invoice(lease_of(charge).operator_id, total="1000.00")
    for note in INVISIBLE_NOTES:
        assert not Invoice.objects.filter(one_off_charge=charge).exists()
        done = setup_service.link_invoice(
            charge.pk, invoice.pk, confirm=False, note=note, actor=None
        )
        assert done.ok
        Invoice.objects.filter(pk=invoice.pk).update(one_off_charge=None)


# --- Bidirectional marks and controls (PLAN-010d Batch A finding) --------------------------

#: Each bidirectional mark or control alone, all of them together, and mixed with spaces.
BIDI_NOTES = (
    *(chr(code) for code in (0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E)),
    *(chr(code) for code in (0x2066, 0x2067, 0x2068, 0x2069)),
    "‎‏‪‫‬‭‮⁦⁧⁨⁩",
    " ‮ ⁧ ‏\t",
)
BIDI_SHOWN = "‮Wire⁩ received.‏"


def test_visible_text_removes_the_bidi_marks() -> None:
    for note in BIDI_NOTES:
        assert visible_text(note) == "", repr(note)
    assert visible_text(BIDI_SHOWN) == CLEANED


def test_the_card_guard_reads_through_the_bidi_marks() -> None:
    for mark in BIDI_NOTES[:11]:
        assert contains_card_number(mark.join(["4111", "1111", "1111", "1111"])), repr(mark)


@pytest.mark.parametrize("note", BIDI_NOTES)
def test_the_mismatch_and_return_resolve_forms_refuse_bidi_marks_alone(note: str) -> None:
    mismatch = ResolveMismatchForm({"note": note})
    assert not mismatch.is_valid()
    assert mismatch.errors["note"] == [mismatches.NOTE_REQUIRED]
    resolve = ResolveReturnForm({"note": note})
    assert not resolve.is_valid()
    assert resolve.errors["note"] == ["A note is required: say how the return was resolved."]


def test_the_mismatch_resolve_service_refuses_bidi_marks_alone(fake: FakeProvider) -> None:
    make_world(fake)
    pending_payment(fake, date(2026, 9, 1))
    run_reconciliation(ReconciliationTrigger.MANUAL, now=RECONCILED_AT)
    found = ReconciliationMismatch.objects.get()
    for note in BIDI_NOTES:
        outcome = mismatches.resolve(found.pk, note=note, actor=None, now=RECONCILED_AT)
        assert outcome == mismatches.ResolveOutcome(False, mismatches.NOTE_REQUIRED), repr(note)
    done = mismatches.resolve(found.pk, note=BIDI_SHOWN, actor=None, now=RECONCILED_AT)
    assert done.ok
    found.refresh_from_db()
    assert found.resolution_note == CLEANED


def test_the_return_resolve_service_refuses_bidi_marks_alone(fake: FakeProvider) -> None:
    _, payment = returned_payment(fake)
    for note in BIDI_NOTES:
        outcome = returns.resolve_return(payment.pk, note=note, actor=None)
        assert outcome == returns.ResolveOutcome(False, returns.RESOLVE_NOTE_REQUIRED), repr(note)
    assert returns.resolve_return(payment.pk, note=BIDI_SHOWN, actor=None).ok
    payment.refresh_from_db()
    assert payment.resolution_note == CLEANED


def test_a_one_off_description_or_note_of_bidi_marks_alone_is_refused() -> None:
    operator = an_operator()
    for text in BIDI_NOTES:
        no_description = one_off_item(description=text)
        outcome = setup_service.create_charge(operator, None, no_description, None)
        assert (outcome.ok, outcome.message, outcome.field) == (
            False,
            setup_service.DESCRIPTION_REQUIRED,
            "description",
        ), repr(text)
        no_note = one_off_item(note=text)
        outcome = setup_service.create_charge(operator, None, no_note, None)
        assert (outcome.ok, outcome.message, outcome.field) == (
            False,
            setup_service.NOTE_REQUIRED,
            "note",
        ), repr(text)
    assert not OneOffCharge.objects.exists()
    kept = setup_service.create_charge(
        operator, None, one_off_item(description=BIDI_SHOWN, note=BIDI_SHOWN), None
    )
    assert kept.ok and kept.charge is not None
    assert (kept.charge.description, kept.charge.note) == (CLEANED, CLEANED)


def test_the_cancel_and_waive_notes_refuse_bidi_marks_alone() -> None:
    operator = an_operator()
    charge = setup_service.create_charge(
        operator, None, one_off_item(collection=CollectionMethod.MANUAL), None
    ).charge
    assert charge is not None
    for note in BIDI_NOTES:
        cancel = setup_service.cancel_charge(charge.pk, note=note, actor=None)
        assert (cancel.ok, cancel.message) == (False, setup_service.VOID_NOTE_REQUIRED)
        waive = setup_service.waive(charge.pk, note=note, actor=None)
        assert (waive.ok, waive.message) == (False, setup_service.WAIVE_NOTE_REQUIRED)
