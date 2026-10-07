"""Adversarial cases for PLAN-012b (the builder's Item combo box), by the QA owner (plan
§6). The resolver must never hand a typed name a fee type staff cannot pick, the catalogue
must not grow behind billing's back or twice at once, and what Stax receives must be the
item's text and the detail, as text."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from django.db import connections

from accounts.roles import Role
from ledger.models import (
    NSF_FEE_CODES,
    AuditLog,
    FeeType,
    InvoiceDraft,
    InvoiceDraftLine,
    InvoiceLineItem,
    OneOffCategory,
    OneOffCharge,
)
from ledger.services import invoice_builder
from payments.fake import FakeProvider
from payments.provider import Amounts, InvoiceInput, LineItem
from payments.stax.adapter import StaxProvider
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import always, make_rig
from tests.test_invoice_builder import TODAY, a_draft, a_world, line, user
from tests.test_invoice_builder import fake as builder_fake
from tests.test_invoice_builder import fixed_now as builder_fixed_now
from tests.test_invoice_builder import worker as builder_worker
from tests.test_ledger_models import requires_postgresql
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture: the webhook path)

pytestmark = pytest.mark.django_db

D = Decimal
CARD = "4111 1111 1111 1111"
SCRIPT = "<script>alert(1)</script> Sign"
fake = builder_fake
fixed_now = builder_fixed_now
worker = builder_worker


def _nsf_types() -> list[FeeType]:
    rows = []
    for code in NSF_FEE_CODES:
        row, _ = FeeType.objects.get_or_create(
            code=code, defaults={"name": code.replace("_", " ").title(), "active": True}
        )
        rows.append(row)
    return rows


# --- names staff cannot pick never resolve ----------------------------------------------------


@pytest.mark.parametrize("spelling", [str.lower, str.upper, lambda s: f"  {s}\t", str.title])
def test_an_nsf_fee_type_name_never_links_however_it_is_typed(
    fake: FakeProvider, spelling: object
) -> None:
    operator, lease = a_world(fake)
    nsf = _nsf_types()
    before = FeeType.objects.count()
    for fee in nsf:
        typed = spelling(fee.name)  # type: ignore[operator]
        made = invoice_builder.create_draft(
            operator, None, "memo", None, [line(item=typed)], user(Role.BILLING), today=TODAY
        )
        assert made.ok and made.draft is not None, made.message
        [stored] = invoice_builder.lines_of(made.draft)
        assert stored.fee_type_id is None, typed
        # ... and saving it to the catalogue is refused: the name is held and not offered.
        saved = invoice_builder.create_draft(
            operator,
            None,
            "memo",
            None,
            [line(item=typed, save_item=True)],
            user(Role.BILLING),
            today=TODAY,
        )
        assert not saved.ok and "not offered" in saved.message
    assert FeeType.objects.count() == before


def test_an_inactive_catalogue_name_is_a_typed_other_line_and_cannot_be_saved_over(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    FeeType.objects.create(code="old-sign", name="Old sign", active=False)
    made = invoice_builder.create_draft(
        operator, lease, "memo", None, [line(item="old sign")], user(Role.BILLING), today=TODAY
    )
    assert made.ok and made.draft is not None, made.message
    [stored] = invoice_builder.lines_of(made.draft)
    assert stored.fee_type_id is None
    assert invoice_builder.category_for(stored.fee_type) == OneOffCategory.OTHER
    refused = invoice_builder.create_draft(
        operator,
        lease,
        "memo",
        None,
        [line(item="Old Sign", save_item=True)],
        user(Role.BILLING),
        today=TODAY,
    )
    assert not refused.ok and refused.field == "lines-0-save_item"
    assert FeeType.objects.filter(name__iexact="old sign").count() == 1
    assert InvoiceDraft.objects.count() == 1


# --- the catalogue grows only through billing, and once --------------------------------------


def test_a_viewer_cannot_create_a_catalogue_item_through_the_builder(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    before = FeeType.objects.count()
    viewer, viewer_user = client_for(Role.VIEWER)
    data = {
        "memo": "m",
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-0-item": "Viewer item",
        "lines-0-unit_amount": "10.00",
        "lines-0-save_item": "on",
    }
    assert viewer.post(f"/invoices/new/store/{lease.store_id}/", data).status_code == 403
    outcome = invoice_builder.create_draft(
        operator,
        lease,
        "m",
        None,
        [line(item="Viewer item", save_item=True)],
        viewer_user,
        today=TODAY,
    )
    assert not outcome.ok
    assert FeeType.objects.count() == before
    assert not FeeType.objects.filter(name="Viewer item").exists()
    assert not AuditLog.objects.filter(action="fee_type.created").exists()


def test_a_card_number_or_a_blank_cannot_become_an_item_or_a_catalogue_entry(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    for item in (CARD, f"Repair {CARD.replace(' ', '')}", "", "   ", "​"):
        outcome = invoice_builder.create_draft(
            operator,
            lease,
            "memo",
            None,
            [line(item=item, save_item=True)],
            user(Role.BILLING),
            today=TODAY,
        )
        assert not outcome.ok, repr(item)
    assert InvoiceDraft.objects.count() == 0 and InvoiceDraftLine.objects.count() == 0
    assert not FeeType.objects.filter(name__contains="4111").exists()
    dump = "".join(str(r.after) + str(r.before) for r in AuditLog.objects.all())
    assert "4111111111111111" not in dump.replace(" ", "")


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_saves_of_one_new_name_leave_one_catalogue_item(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    operator_pk, lease_pk = operator.pk, lease.pk
    billing = user(Role.BILLING)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def save() -> None:
        from ledger.models import Lease, Operator

        try:
            barrier.wait(timeout=10)
            outcome = invoice_builder.create_draft(
                Operator.objects.get(pk=operator_pk),
                Lease.objects.get(pk=lease_pk),
                "memo",
                None,
                [line(item="Awning wash", save_item=True)],
                billing,
                today=TODAY,
            )
            outcomes.append("ok" if outcome.ok else f"refused: {outcome.message}")
        except Exception as error:  # the fence is about the catalogue, not the thread
            outcomes.append(f"error: {type(error).__name__}")
        finally:
            connections.close_all()

    threads = [threading.Thread(target=save) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(outcomes) == 2 and not any(o.startswith("error") for o in outcomes), outcomes
    assert FeeType.objects.filter(name="Awning wash").count() == 1
    assert FeeType.objects.filter(code__startswith="awning-wash").count() == 1
    linked = InvoiceDraftLine.objects.filter(item="Awning wash", fee_type__isnull=False).count()
    assert linked == outcomes.count("ok")


# --- what the provider receives: the item's text and the detail, as text ------------------------


def test_the_provider_receives_the_item_text_and_the_detail_and_no_fee_type_id(
    fake: FakeProvider, worker: None
) -> None:
    operator, lease = a_world(fake)
    catalogue = FeeType.objects.create(code="sign-repair", name="Sign repair", active=True)
    draft = a_draft(
        operator,
        lease,
        [
            line(item="sign repair", description="North face"),
            line(item=SCRIPT, unit="40.00", description="Typed & detailed"),
        ],
    )
    sent = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert sent.ok and sent.invoice is not None, sent.message
    run_queued_jobs()
    [(request,)] = [a for m, a in fake.calls if m == "create_invoice"]
    assert isinstance(request, InvoiceInput)
    first, second = request.amounts.line_items
    assert (first.description, first.details) == ("Sign repair", "North face")
    assert (second.description, second.details) == (SCRIPT, "Typed & detailed")
    assert str(catalogue.pk) not in repr(request)
    items = list(
        InvoiceLineItem.objects.filter(invoice=sent.invoice)
        .order_by("position")
        .values_list("description", "details", "kind")
    )
    assert items == [
        ("Sign repair", "North face", "charge"),
        (SCRIPT, "Typed & detailed", "charge"),
    ]
    charges = {c.description: c for c in OneOffCharge.objects.all()}
    assert charges["Sign repair"].category == OneOffCategory.SETUP
    assert charges[SCRIPT].category == OneOffCategory.OTHER
    assert "Detail: Typed & detailed" in charges[SCRIPT].note
    # The pages escape the item and the detail; nothing executes.
    viewer, _ = client_for(Role.VIEWER)
    for route in ("preview", "print"):
        page = viewer.get(f"/invoices/drafts/{draft.pk}/{route}/").content.decode()
        assert "<script>alert" not in page and "&lt;script&gt;" in page
        assert "Typed &amp; detailed" in page


def test_the_stax_body_carries_details_as_a_line_field_and_no_fee_type_key(worker: None) -> None:
    rig = make_rig(always(200, {"id": "inv-1", "status": "DRAFT"}))
    amounts = Amounts(
        total=D("40.00"),
        subtotal=D("40.00"),
        tax=D("0.00"),
        line_items=(LineItem(SCRIPT, D("1"), D("40.00"), details="Typed & detailed"),),
    )
    StaxProvider(rig.transport).create_invoice(InvoiceInput("cus-1", amounts, due_on=TODAY))
    [request] = rig.recorder.requests
    body = request.content.decode()
    assert "Typed & detailed" in body or "Typed \\u0026 detailed" in body
    assert SCRIPT in body or SCRIPT.replace("<", "\\u003c") in body
    import json

    parsed = json.loads(request.content)
    lines = parsed.get("meta", {}).get("lineItems", []) + parsed.get("lineItems", [])
    assert lines, "no line items in the body"
    keys = set(parsed) | {k for item in lines for k in item}
    # ``item`` is the provider's own name for a line's text; our form field names never go.
    assert "fee_type" not in keys and "save_item" not in keys and "fee_type_id" not in keys
    assert "details" in keys


def test_no_template_or_form_offers_free_text_any_more() -> None:
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    sources = list((backend / "ledger" / "templates" / "ledger" / "invoices").glob("*.html"))
    sources += [
        backend / "ledger" / "forms.py",
        backend / "ledger" / "static" / "ledger" / "invoice_lines.js",
    ]
    for path in sources:
        assert "Free text" not in path.read_text(encoding="utf-8"), path
