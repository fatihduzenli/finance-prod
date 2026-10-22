"""Adversarial cases for PLAN-012 (contact log maturity, invoice builder, disputes), by the
QA owner (plan §6, "Adversarial (Fable)"). Opus's files prove each feature; these cases try
to make a line worth nothing or less, bill one operator's lease to another, send a bank
form to the payment provider, or show a viewer what is not theirs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    CollectionStatus,
    ContactEntry,
    Dispute,
    DisputeEvidence,
    Document,
    Invoice,
    InvoiceDraft,
    InvoiceDraftLine,
    Lease,
    OneOffCharge,
    Payment,
    RentCharge,
)
from ledger.services import contacts, disputes, invoice_builder
from ledger.services.invoice_builder import LineInput
from payments.errors import ProviderRejected, ValidationFailed
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from payments.provider import InvoiceInput
from payments.stax.adapter import StaxProvider
from tests import test_disputes as dispute_tests
from tests.document_files import JPEG, PNG, make_pdf
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import always, make_rig
from tests.test_contact_log import deleted, entry, promise, table_of
from tests.test_disputes import (
    MASKED,
    NOW,
    PAGE,
    a_dispute,
    a_paid_world,
    add,
    admin,
    ready,
    synced,
    the_dispute,
)
from tests.test_documents_adversarial import PAIR, _pdf_with
from tests.test_invoice_builder import a_draft, a_world, line, user
from tests.test_one_off_charges import calls
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture: the webhook path)
from tests.test_webhook_handlers import TXN

pytestmark = pytest.mark.django_db

# The dispute file's fixtures (pinned clock, fake provider, in-memory storage, worker caller),
# re-exported under their own names so pytest finds them here.
fixed_now = dispute_tests.fixed_now
fake = dispute_tests.fake
storage = dispute_tests.storage
worker = dispute_tests.worker

D = Decimal
TODAY = NOW.date()
CARD = "4111 1111 1111 1111"
CARD_DIGITS = CARD.replace(" ", "")
BACKEND = Path(__file__).resolve().parents[1]


def _nothing_billed() -> None:
    assert Invoice.objects.filter(legacy=False).count() == 0
    assert OneOffCharge.objects.count() == 0


# --- the invoice builder cannot bill nothing, less than nothing or a fraction of a cent ------


@pytest.mark.parametrize(
    "bad",
    [
        line(unit="-1.00"),
        line(unit="0"),
        line(unit="0.001"),
        line(unit="NaN"),
        line(unit="Infinity"),
        line(unit="-Infinity"),
        line(unit="1000000.00"),
        line(quantity="0"),
        line(quantity="-1"),
        line(quantity="0.00001"),
        line(quantity="NaN"),
        line(quantity="Infinity"),
        line(unit="0.01", quantity="0.4"),  # the total rounds to zero
    ],
    ids=lambda item: f"q={item.quantity} u={item.unit_amount}",
)
def test_a_line_worth_nothing_less_than_nothing_or_a_fraction_of_a_cent_is_refused(
    fake: FakeProvider, bad: LineInput
) -> None:
    operator, lease = a_world(fake)
    outcome = invoice_builder.create_draft(
        operator, lease, "memo", None, [line(), bad], user(Role.BILLING), today=TODAY
    )
    assert not outcome.ok and outcome.field is not None and outcome.field.startswith("lines-1-")
    assert InvoiceDraft.objects.count() == 0 and InvoiceDraftLine.objects.count() == 0


def test_an_exponent_form_of_a_cent_amount_and_a_half_cent_total_are_legal_money(
    fake: FakeProvider,
) -> None:
    """``1E+2`` is 100 to the cent and 0.5 x 0.01 rounds half up to one cent: both are
    accepted and stored as plain cents."""
    operator, lease = a_world(fake)
    outcome = invoice_builder.create_draft(
        operator,
        lease,
        "memo",
        None,
        [line(unit="1E+2"), line("Half", "0.01", "0.5")],
        user(Role.BILLING),
        today=TODAY,
    )
    assert outcome.ok and outcome.draft is not None, outcome.message
    first, second = invoice_builder.lines_of(outcome.draft)
    assert (str(first.unit_amount), str(first.line_total)) == ("100.00", "100.00")
    assert (str(second.line_total), str(second.quantity)) == ("0.01", "0.5000")


def test_a_tampered_stored_line_total_is_refused_by_the_table_and_the_lines_sum_to_the_cent(
    fake: FakeProvider, worker: None
) -> None:
    """A draft line's ``line_total`` is derived: the table's CHECK refuses a value written
    behind the service's back; send recomputes quantity times unit, rounded half up, and the
    invoice total is the sum of the line totals and of the charges."""
    operator, lease = a_world(fake)
    draft = a_draft(
        operator, lease, [line(unit="33.33", quantity="3"), line("Half", "0.03", "0.5")]
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceDraftLine.objects.filter(draft=draft).update(line_total=D("1.00"))
    sent = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert sent.ok and sent.invoice is not None, sent.message
    invoice = Invoice.objects.get(pk=sent.invoice.pk)
    totals = sorted(item.line_total for item in invoice.line_items.all())
    assert totals == [D("0.02"), D("99.99")]
    assert invoice.total == D("100.01") == invoice.subtotal
    assert sum(c.price for c in OneOffCharge.objects.all()) == D("100.01")
    run_queued_jobs()
    [(request,)] = calls(fake, "create_invoice")
    assert isinstance(request, InvoiceInput)
    lines_sent = request.amounts.line_items
    assert sum(item.unit_price * item.quantity for item in lines_sent) == D("100.01")
    assert request.amounts.total == D("100.01") and len(lines_sent) == 2


# --- one operator's lease cannot carry another operator's invoice ---------------------------


def test_a_draft_of_operator_a_cannot_be_made_or_sent_against_operator_bs_lease(
    fake: FakeProvider,
) -> None:
    from tests.test_lease_services import make_lease
    from tests.test_one_off_charges import an_operator

    operator_a, _ = a_world(fake)
    lease_b = make_lease(operator=an_operator(fake, customer="cus-other-b", bank="pm-other-b"))
    made = invoice_builder.create_draft(
        operator_a, lease_b, "memo", None, [line()], user(Role.BILLING), today=TODAY
    )
    assert not made.ok and made.message == invoice_builder.LEASE_OTHER_OPERATOR
    assert InvoiceDraft.objects.count() == 0
    # Behind the service's back: the lease swapped after the draft was saved.
    draft = a_draft(operator_a, None, [line()])
    InvoiceDraft.objects.filter(pk=draft.pk).update(lease=lease_b)
    actions: list[Callable[[], invoice_builder.Outcome]] = [
        lambda: invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY),
        lambda: invoice_builder.charge_now(draft.pk, user(Role.ADMIN), confirmed=True, today=TODAY),
    ]
    for action in actions:
        outcome = action()
        assert not outcome.ok and outcome.message == invoice_builder.LEASE_OTHER_OPERATOR
    _nothing_billed()
    assert calls(fake, "create_invoice") == [] and calls(fake, "pay_invoice") == []
    assert not Job.objects.filter(kind=JobKind.INVOICE_CREATE).exists()


# --- an ended lease: arrears by e-mail yes, a debit on a due date never ----------------------


def test_an_ended_lease_takes_arrears_by_email_and_the_builder_never_debits_on_a_due_date(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    Lease.objects.filter(pk=lease.pk).update(
        collection_status=CollectionStatus.ENDED, ended_on=date(2026, 9, 30)
    )
    draft = a_draft(operator, Lease.objects.get(pk=lease.pk), [line("Arrears after move-out")])
    sent = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert sent.ok, sent.message
    [charge] = OneOffCharge.objects.all()
    assert charge.collection == CollectionMethod.EMAIL and charge.lease_id == lease.pk
    run_queued_jobs()
    assert len(calls(fake, "create_invoice")) == 1
    assert calls(fake, "create_schedule") == [] and calls(fake, "pay_invoice") == []
    # D6, restated for PLAN-012e-2 (the builder now offers "Schedule for a date"): a debit on
    # a due date is never made for an ended lease, nor for a draft without a lease.
    second = a_draft(
        operator, Lease.objects.get(pk=lease.pk), [line("Arrears after move-out, again")]
    )
    scheduled = invoice_builder.collect(
        second.pk,
        "schedule",
        actor=user(Role.BILLING),
        today=TODAY,
        due_date=TODAY + timedelta(days=7),
    )
    assert not scheduled.ok and "Stax" not in scheduled.message
    no_lease = a_draft(operator, None, [line("Operator-level arrears")])
    unleased = invoice_builder.collect(
        no_lease.pk,
        "schedule",
        actor=user(Role.BILLING),
        today=TODAY,
        due_date=TODAY + timedelta(days=7),
    )
    assert not unleased.ok
    run_queued_jobs()
    assert calls(fake, "create_schedule") == []
    assert not OneOffCharge.objects.filter(collection=CollectionMethod.AUTOPAY_DUE).exists()


# --- a viewer changes nothing -------------------------------------------------------------------


def test_a_viewer_cannot_make_edit_send_charge_or_cancel_a_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line()])
    viewer, _ = client_for(Role.VIEWER)
    before = (
        InvoiceDraft.objects.get(pk=draft.pk).status,
        list(InvoiceDraftLine.objects.values_list("description", "unit_amount")),
    )
    assert viewer.get(f"/invoices/new/store/{lease.store_id}/").status_code == 403
    assert viewer.get(f"/invoices/new/operator/{operator.pk}/").status_code == 403
    assert viewer.post(f"/invoices/new/store/{lease.store_id}/", {"memo": "x"}).status_code == 403
    for route, data in (
        ("edit", {"memo": "changed", "lines-0-description": "Other", "lines-0-unit_amount": "1"}),
        ("send", {}),
        ("charge-now", {"confirm": "on"}),
        ("cancel", {"note": "n"}),
    ):
        assert viewer.post(f"/invoices/drafts/{draft.pk}/{route}/", data).status_code == 403, route
    assert viewer.get(f"/invoices/drafts/{draft.pk}/charge-now/").status_code == 403
    assert viewer.get(f"/invoices/drafts/{draft.pk}/cancel/").status_code == 403
    after = (
        InvoiceDraft.objects.get(pk=draft.pk).status,
        list(InvoiceDraftLine.objects.values_list("description", "unit_amount")),
    )
    assert after == before
    _nothing_billed()
    assert fake.calls == [c for c in fake.calls if c[0] not in ("create_invoice", "pay_invoice")]


def test_a_viewer_cannot_edit_correct_or_delete_a_contact_entry_and_billing_cannot_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.receivables_helpers import an_operator

    author_client, author = client_for(Role.BILLING)
    operator = an_operator()
    row = entry(operator, actor=author)
    viewer, _ = client_for(Role.VIEWER)
    edit_data = {
        "kind": row.kind,
        "happened_on": row.happened_on.isoformat(),
        "promised_date": "",
        "promised_amount": "",
        "body": "Rewritten by a viewer.",
    }
    assert viewer.post(f"/contacts/{row.pk}/edit/", edit_data).status_code == 403
    assert viewer.post(f"/contacts/{row.pk}/correct/", {"body": "Viewer says."}).status_code == 403
    assert viewer.post(f"/contacts/{row.pk}/delete/", {"reason": "Viewer says."}).status_code == 403
    assert viewer.get(f"/contacts/{row.pk}/edit/").status_code == 403
    assert viewer.get(f"/contacts/{row.pk}/delete/").status_code == 403
    other_billing, _ = client_for(Role.BILLING)
    assert other_billing.post(f"/contacts/{row.pk}/delete/", {"reason": "No."}).status_code == 403
    assert other_billing.post(f"/contacts/{row.pk}/edit/", edit_data).status_code == 403
    # The author, past the window, cannot reopen it with a forged timestamp in the form.
    pinned = row.created_at + timedelta(minutes=16)
    monkeypatch.setattr("django.utils.timezone.now", lambda: pinned)
    late = author_client.post(
        f"/contacts/{row.pk}/edit/",
        {**edit_data, "created_at": pinned.isoformat(), "body": "Late edit."},
    )
    assert late.status_code in (200, 302, 403)
    fresh = ContactEntry.objects.get(pk=row.pk)
    assert fresh.body == row.body and fresh.deleted_at is None
    assert fresh.corrections.count() == 0


def test_the_deletion_reason_and_the_deleted_text_reach_no_viewer_page_or_csv() -> None:
    from tests.receivables_helpers import a_lease, an_operator

    operator = an_operator()
    lease = a_lease(operator=operator)
    marker_body = "Said the cheque is in the post MARKER-BODY-5150."
    reason = "Recorded on the wrong operator MARKER-REASON-9021."
    row = deleted(promise(operator, lease=lease, body=marker_body), reason)
    assert row.deleted_at is not None
    viewer, _ = client_for(Role.VIEWER)
    dump = ""
    for path in (
        f"/operators/{operator.pk}/",
        f"/operators/{operator.pk}/contacts/",
        f"/operators/{operator.pk}/contacts/?status=all&deleted=1",
        f"/operators/{operator.pk}/contacts/export.csv",
        f"/stores/{lease.store_id}/",
        f"/stores/{lease.store_id}/contacts/",
        "/",
    ):
        response = viewer.get(path)
        assert response.status_code == 200, path
        content = response.content if not response.streaming else b"".join(response)
        dump += content.decode()
    assert "MARKER-REASON-9021" not in dump
    assert "MARKER-BODY-5150" not in dump
    # Billing and admin see the entry struck through, with its reason (D2).
    billing, _ = client_for(Role.BILLING)
    page = billing.get(f"/operators/{operator.pk}/").content.decode()
    assert "MARKER-BODY-5150" in table_of(page)
    administrator, _ = client_for(Role.ADMIN)
    assert "MARKER-REASON-9021" in administrator.get(f"/operators/{operator.pk}/").content.decode()
    # The deleted promise counts for nothing: it is no broken promise on the dashboard.
    assert contacts.broken_promises(TODAY, operator.pk) == []


# --- dispute evidence never bypasses the 10c guard ------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(make_pdf([[PAIR]]), id="page-text"),
        pytest.param(_pdf_with(field_value=PAIR), id="form-field"),
        pytest.param(_pdf_with(annotation=PAIR), id="annotation"),
        pytest.param(_pdf_with(title=PAIR), id="title"),
        pytest.param(
            make_pdf([["Routing", dispute_tests.ROUTING], ["Account", dispute_tests.ACCOUNT]]),
            id="split-pages",
        ),
        pytest.param(make_pdf([[CARD]]), id="card"),
    ],
)
def test_a_pdf_with_payment_data_anywhere_never_reaches_the_provider_through_the_route(
    fake: FakeProvider, storage: Any, data: bytes
) -> None:
    dispute = ready(fake)
    client, _ = client_for(Role.ADMIN)
    response = client.post(
        f"{PAGE}{dispute.pk}/evidence/",
        {
            "name": "Statement",
            "file": SimpleUploadedFile("statement.pdf", data, content_type="application/pdf"),
            "acknowledged": "on",
        },
    )
    assert response.status_code == 302
    run_queued_jobs()
    assert calls(fake, "upload_dispute_evidence") == []
    assert fake.evidence == []
    assert DisputeEvidence.objects.count() == 0 and Document.objects.count() == 0
    assert storage.blobs == {}
    assert not Job.objects.filter(kind=JobKind.DISPUTE_EVIDENCE_UPLOAD).exists()
    dump = "".join(str(r.before) + str(r.after) for r in AuditLog.objects.all()).replace(" ", "")
    for number in (CARD_DIGITS, dispute_tests.ROUTING, dispute_tests.ACCOUNT):
        assert number not in dump


def test_an_evidence_name_or_a_dispute_past_respond_by_stores_nothing(
    fake: FakeProvider, storage: Any
) -> None:
    dispute = ready(fake)
    for name in (CARD, PAIR, f"Lease {CARD_DIGITS}"):
        outcome = disputes.add_evidence(
            dispute, dispute_tests.a_file("lease.png", PNG), name, admin(), acknowledged=True
        )
        assert not outcome.ok
    assert Document.objects.count() == 0 and storage.blobs == {}
    # Past the respond-by time, through the route, with every box ticked.
    Dispute.objects.filter(pk=dispute.pk).update(respond_by=NOW - timedelta(seconds=1))
    client, _ = client_for(Role.ADMIN)
    client.post(
        f"{PAGE}{dispute.pk}/evidence/",
        {
            "name": "Receipt",
            "file": SimpleUploadedFile("r.jpg", JPEG, content_type="image/jpeg"),
            "acknowledged": "on",
        },
    )
    run_queued_jobs()
    assert Document.objects.count() == 0 and DisputeEvidence.objects.count() == 0
    assert calls(fake, "upload_dispute_evidence") == []


# --- the page shows no provider identifier and survives hostile provider values -----------------


def test_the_page_and_csv_show_no_unmasked_transaction_or_dispute_id_even_from_the_summary(
    fake: FakeProvider,
) -> None:
    a_paid_world(fake)
    a_dispute(
        fake,
        "dsp-secret-0001",
        status="<script>alert(1)</script>",
        reason='=HYPERLINK("http://evil.example")',
        raw_summary={
            "id": "dsp-secret-0001",
            "transaction_id": TXN,
            "status": "<script>alert(1)</script>",
            "reason": "FRAUD",
            "merchant_id": "merchant-secret-7",
        },
    )
    synced(fake)
    dispute = the_dispute("dsp-secret-0001")
    assert TXN not in str(dispute.raw_summary) and "dsp-secret" not in str(dispute.raw_summary)
    assert "merchant-secret" not in str(dispute.raw_summary)
    for role in (Role.BILLING, Role.ADMIN):
        client, _ = client_for(role)
        page = client.get(PAGE).content.decode()
        export = b"".join(client.get(f"{PAGE}export.csv")).decode()
        for text in (page, export):
            assert TXN not in text and "dsp-secret" not in text and "merchant-secret" not in text
        # The page escapes the provider's words; the CSV keeps them verbatim (markup is inert
        # there) but neutralises a formula.
        assert "<script>alert" not in page and "&lt;script&gt;" in page
        assert MASKED in page
        assert "\"'=HYPERLINK(" in export and ",=HYPERLINK(" not in export
    audit = "".join(str(r.after) + str(r.before) for r in AuditLog.objects.all())
    assert TXN not in audit and "dsp-secret" not in audit


def test_hostile_provider_values_neither_crash_the_sync_nor_enter_the_rows(
    fake: FakeProvider,
) -> None:
    a_paid_world(fake)
    a_dispute(
        fake, "dsp-long", reason="Q" * 300, status="S\r\nTATUS\x00", amount=None, respond_by=None
    )
    a_dispute(
        fake, "dsp-unknown-txn", transaction="txn-nobody", respond_by=NOW - timedelta(days=400)
    )
    a_dispute(fake, "dsp-card", status=CARD_DIGITS)
    try:
        disputes.sync()
    except ValidationError:
        pass  # the Rule 1 guard refused the row that carried a card number
    for row in Dispute.objects.all():
        assert len(row.reason) <= 64 and len(row.status) <= 64
        assert CARD_DIGITS not in row.status and CARD_DIGITS not in str(row.raw_summary)
    client, _ = client_for(Role.BILLING)
    assert client.get(PAGE).status_code == 200
    assert client.get(f"{PAGE}export.csv").status_code == 200


def test_the_sync_changes_no_payment_or_charge_state(fake: FakeProvider) -> None:
    world = a_paid_world(fake)

    def payments_now() -> list[tuple[object, ...]]:
        return list(Payment.objects.values_list("pk", "status", "amount").order_by("pk"))

    payments_before = payments_now()
    charge_before = RentCharge.objects.values("status", "amount_paid").get(pk=world.charge.pk)
    a_dispute(fake, status="LOST", amount=D("1500.00"))
    synced(fake)
    synced(fake)
    assert (
        list(Payment.objects.values_list("pk", "status", "amount").order_by("pk"))
        == payments_before
    )
    assert (
        RentCharge.objects.values("status", "amount_paid").get(pk=world.charge.pk) == charge_before
    )
    assert the_dispute().payment_id == payments_before[0][0]
    assert not AuditLog.objects.filter(
        entity_type__in=("payment", "rent_charge"), action__startswith="dispute"
    ).exists()
    assert AuditLog.objects.filter(action="dispute.synced").count() == 2


def test_a_viewer_and_an_anonymous_visitor_get_nothing_from_the_dispute_routes(
    fake: FakeProvider,
) -> None:
    dispute = ready(fake)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(PAGE).status_code == 403
    assert viewer.get(f"{PAGE}export.csv").status_code == 403
    assert viewer.post(f"{PAGE}refresh/", {}).status_code == 403
    assert (
        viewer.post(
            f"{PAGE}{dispute.pk}/evidence/",
            {"name": "x", "file": SimpleUploadedFile("x.png", PNG), "acknowledged": "on"},
        ).status_code
        == 403
    )
    from django.test import Client

    anonymous = Client()
    for path in (PAGE, f"{PAGE}export.csv"):
        response = anonymous.get(path)
        assert response.status_code in (302, 401, 403)
        assert "dispute-" not in response.content.decode().lower()
    assert anonymous.post(f"{PAGE}refresh/", {}).status_code in (302, 401, 403)
    assert not Job.objects.filter(kind=JobKind.DISPUTES_SYNC, params__trigger="manual").exists()
    assert Document.objects.count() == 0


# --- the multipart body cannot be bent by a name or by the file's bytes -------------------------


def test_a_name_with_a_line_break_sends_nothing_and_a_boundary_in_the_bytes_stays_one_part() -> (
    None
):
    rig = make_rig(always(200, {"id": "file-1"}))
    provider = StaxProvider(rig.transport)
    forged = 'x\r\nContent-Disposition: form-data; name="dispute_id"\r\n\r\nother-dispute'
    with pytest.raises((ValidationFailed, ValueError)):
        provider.upload_dispute_evidence("dsp-1", forged, PNG, "image/png")
    assert rig.recorder.requests == []
    # Bytes that look like a boundary line: the body still parses as the three fields and
    # the file arrives byte for byte.
    tricky = PNG + b"\r\n--finance-00000000000000000000000000000000--\r\n" + b"\x00" * 16
    provider.upload_dispute_evidence("dsp-1", "Receipt", tricky, "image/png")
    [request] = rig.recorder.requests
    parts = dispute_tests._parts(request)
    assert set(parts) == {"dispute_id", "name", "file"}
    assert parts["file"][1] == tricky
    assert parts["dispute_id"][1] == b"dsp-1" and parts["name"][1] == b"Receipt"


def test_a_refusal_sentence_carrying_a_card_number_is_dropped() -> None:
    body = {"message": f"The respond by date has passed; card {CARD} was charged"}
    rig = make_rig(always(400, body))
    with pytest.raises(ProviderRejected) as raised:
        StaxProvider(rig.transport).upload_dispute_evidence("dsp-1", "Receipt", PNG, "image/png")
    assert raised.value.reason == ""
    assert CARD_DIGITS not in str(raised.value) and CARD not in str(raised.value)
    assert len(rig.recorder.requests) == 1


# --- the Stax 400 past respond-by reaches the page as a sentence, never as a traceback -----------


def test_the_providers_past_respond_by_refusal_shows_as_a_sentence_and_the_file_stays_ours(
    fake: FakeProvider, storage: Any
) -> None:
    dispute = ready(fake, respond_by=NOW + timedelta(seconds=30))
    outcome = add(dispute, "lease.png", PNG)
    assert outcome.ok and outcome.evidence is not None, outcome.message
    # The provider's clock passes the respond-by time before the worker sends.
    fake.disputes["dsp-1"] = replace(fake.disputes["dsp-1"], respond_by=NOW - timedelta(seconds=1))
    run_queued_jobs()
    row = DisputeEvidence.objects.get(pk=outcome.evidence.pk)
    assert row.status == "failed" and row.error == "ProviderRejected"
    client, _ = client_for(Role.ADMIN)
    page = client.get(PAGE).content.decode()
    assert "Traceback" not in page and "ProviderRejected(" not in page
    assert "respond by" in page.lower() or "respond-by" in page.lower()
    assert Document.objects.filter(pk=row.document_id).exists()
    assert len(storage.blobs) == 1


# --- the provider interface stays whole (the portability test is the fence; one more check) ----


def test_both_providers_implement_the_dispute_methods_with_the_same_signature() -> None:
    import inspect

    from payments.provider import PaymentProvider

    for name in ("list_disputes", "upload_dispute_evidence"):
        reference = inspect.signature(getattr(PaymentProvider, name))
        for implementation in (StaxProvider, FakeProvider):
            assert inspect.signature(getattr(implementation, name)) == reference, (
                name,
                implementation,
            )
