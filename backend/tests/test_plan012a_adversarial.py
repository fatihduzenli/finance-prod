"""Adversarial cases for PLAN-012a (line-item kind, fee and surcharge columns, operator
consent), by the QA owner (plan §6). The columns must exist without changing a single
provider request or a single report figure, and consent must be billing's to write."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import IntegrityError, transaction

from accounts.roles import Role
from ledger.models import Invoice, InvoiceLineItem, InvoiceLineKind, Operator
from ledger.services import invoice_builder
from payments.fake import FakeProvider
from payments.provider import Amounts, InvoiceInput, LineItem
from payments.stax.adapter import StaxProvider
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import always, make_rig
from tests.test_invoice_builder import NOW, TODAY, a_draft, a_world, line, user
from tests.test_invoice_builder import fake as builder_fake
from tests.test_invoice_builder import fixed_now as builder_fixed_now
from tests.test_invoice_builder import worker as builder_worker
from tests.test_operator_views import client_for, form_data
from tests.test_rent_run import health  # noqa: F401 (autouse fixture: the webhook path)

pytestmark = pytest.mark.django_db

D = Decimal
BACKEND = Path(__file__).resolve().parents[1]
CARD = "4111 1111 1111 1111"
PAIR = "Routing 123456780 Account 987654321012"
fake = builder_fake
fixed_now = builder_fixed_now
worker = builder_worker
_ = (NOW,)


# --- the provider never hears of fees or a surcharge ------------------------------------------


def test_the_adapter_sends_no_fees_or_surcharge_key_and_the_contract_has_none(
    worker: None,
) -> None:
    rig = make_rig(always(200, {"id": "inv-1", "status": "DRAFT"}))
    amounts = Amounts(
        total=D("100.00"),
        subtotal=D("100.00"),
        tax=D("0.00"),
        line_items=(LineItem("Repair", D("1"), D("100.00")),),
    )
    StaxProvider(rig.transport).create_invoice(InvoiceInput("cus-1", amounts, due_on=TODAY))
    [request] = rig.recorder.requests
    body = request.content.decode().lower()
    assert "fees" not in body and "surcharge" not in body and '"kind"' not in body
    assert not hasattr(amounts, "fees") and not hasattr(amounts, "surcharge")
    stax = (BACKEND / "payments" / "stax").rglob("*.py")
    for path in stax:
        text = path.read_text(encoding="utf-8").lower()
        assert "surcharge" not in text and '"fees"' not in text, path


def test_the_builders_single_create_carries_the_lines_and_the_same_total_as_before(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line(unit="60.00"), line("Second", "40.00")])
    sent = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert sent.ok and sent.invoice is not None, sent.message
    run_queued_jobs()
    [(request,)] = [a for m, a in fake.calls if m == "create_invoice"]
    assert isinstance(request, InvoiceInput)
    assert request.amounts.total == D("100.00") == request.amounts.subtotal
    invoice = Invoice.objects.get(pk=sent.invoice.pk)
    assert (invoice.fees, invoice.surcharge) == (D("0.00"), D("0.00"))
    assert invoice.total == invoice.subtotal + invoice.tax + invoice.fees + invoice.surcharge
    assert set(invoice.line_items.values_list("kind", flat=True)) == {InvoiceLineKind.CHARGE}


# --- the table refuses a total that forgets a part, and a kind nobody writes yet ----------------


def test_fees_cannot_be_added_behind_the_total_and_only_charge_lines_are_written(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()])
    sent = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert sent.ok and sent.invoice is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=sent.invoice.pk).update(fees=D("5.00"))
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=sent.invoice.pk).update(surcharge=D("-1.00"))
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceLineItem.objects.filter(invoice=sent.invoice).update(kind="refund")
    # No production code writes any kind but charge (D4): the only ``kind=`` assignments
    # in ledger/ and payments/ name InvoiceLineKind.CHARGE.
    writes = []
    for path in list((BACKEND / "ledger").rglob("*.py")) + list(
        (BACKEND / "payments").rglob("*.py")
    ):
        if "migrations" in path.parts:
            continue
        for match in re.finditer(
            r"\bkind=InvoiceLineKind\.(\w+)", path.read_text(encoding="utf-8")
        ):
            writes.append((str(path.relative_to(BACKEND)), match.group(1)))
    assert writes and {kind for _, kind in writes} == {"CHARGE"}, writes


def test_no_report_sums_fees_or_surcharge_into_a_figure_yet() -> None:
    """The reports' numbers are unchanged by the migration: nothing under ledger/services
    reads the two new money columns (they are zero, and a later plan decides how they
    show)."""
    literal = re.compile(r"""["'](fees|surcharge)["']""")
    offenders = [
        (path.name, match.group(0))
        for path in (BACKEND / "ledger" / "services").glob("*.py")
        for match in literal.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --- consent is billing's to write, and never a card or account number -------------------------


def _operator(fake: FakeProvider) -> Operator:
    from tests.test_one_off_charges import an_operator

    return an_operator(fake)


def test_a_viewer_cannot_write_consent_and_a_number_in_the_sms_phone_is_refused(
    fake: FakeProvider,
) -> None:
    operator = _operator(fake)
    before = Operator.objects.values("sms_phone", "sms_consent_at", "email_consent_at").get(
        pk=operator.pk
    )
    data = form_data(
        display_name=operator.display_name,
        email=operator.email or "",
        sms_phone="+14045551234",
        sms_consent_at="2026-10-01T10:00:00",
        email_consent_at="2026-10-01T10:00:00",
    )
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(f"/operators/{operator.pk}/edit/", data).status_code == 403
    billing, _ = client_for(Role.BILLING)
    for bad in (CARD.replace(" ", ""), "+" + CARD.replace(" ", ""), PAIR, "+1 404 555 1234"):
        response = billing.post(f"/operators/{operator.pk}/edit/", {**data, "sms_phone": bad})
        assert response.status_code in (200, 302), bad  # the form again, with the sentence
        assert "/edit/" in response.get("Location", "/edit/"), bad
    after = Operator.objects.values("sms_phone", "sms_consent_at", "email_consent_at").get(
        pk=operator.pk
    )
    assert after == before
    assert not [c for c in fake.calls if c[0] in ("update_customer", "create_customer")]


def test_a_consent_time_in_the_future_or_a_forged_field_name_changes_nothing(
    fake: FakeProvider,
) -> None:
    operator = _operator(fake)
    billing, _ = client_for(Role.BILLING)
    data = form_data(
        display_name=operator.display_name,
        email=operator.email or "",
        sms_phone="+14045551234",
        sms_consent_at="2099-01-01T00:00:00",
        email_consent_at="",
    )
    response = billing.post(f"/operators/{operator.pk}/edit/", data)
    assert response.status_code in (200, 302) and "/edit/" in response.get("Location", "/edit/")
    assert Operator.objects.get(pk=operator.pk).sms_consent_at is None
    # Field names that are not on the form are ignored, never written.
    data = form_data(
        display_name=operator.display_name,
        email=operator.email or "",
        sms_consent_given=True,
        consent="yes",
        sms_consent_at="",
        email_consent_at="",
    )
    billing.post(f"/operators/{operator.pk}/edit/", data)
    fresh = Operator.objects.get(pk=operator.pk)
    assert fresh.sms_consent_at is None and fresh.email_consent_at is None


def test_the_operator_csv_neutralises_the_plus_and_the_viewer_reads_consent_without_a_form(
    fake: FakeProvider,
) -> None:
    operator = _operator(fake)
    Operator.objects.filter(pk=operator.pk).update(
        sms_phone="+14045551234", sms_consent_at=NOW, email_consent_at=NOW
    )
    viewer, _ = client_for(Role.VIEWER)
    page = viewer.get(f"/operators/{operator.pk}/").content.decode()
    assert "+14045551234" in page and 'name="sms_phone"' not in page
    export = b"".join(viewer.get("/operators/export.csv")).decode()
    assert "'+14045551234" in export and ",+14045551234" not in export


def test_the_seeded_baseline_totals_do_not_depend_on_the_new_columns(fake: FakeProvider) -> None:
    """Every invoice's total equals the sum of its parts with the new parts at zero, so
    the figures the reports sum (``total``) are what they were before 0031."""
    operator, lease = a_world(fake)
    for amount in ("10.00", "20.00"):
        draft = a_draft(operator, lease, [line(unit=amount)], due=date(2026, 10, 20))
        assert invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY).ok
    rows = list(Invoice.objects.values("total", "subtotal", "tax", "fees", "surcharge"))
    assert rows and all(r["fees"] == 0 and r["surcharge"] == 0 for r in rows)
    assert all(r["total"] == r["subtotal"] + r["tax"] for r in rows)
