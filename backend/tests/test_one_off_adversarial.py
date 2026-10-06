"""Adversarial cases for PLAN-010d (one-off charges, Charge now, the audit page), by the QA
owner (plan §6). Opus's files cover the reference collisions, the key's immutability and the
audit filters; these cases go after the inputs and states around them."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client, override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    Invoice,
    OneOffCharge,
    Payment,
    PaymentStatus,
)
from ledger.services import one_off_charges as service
from payments.fake import FakeProvider
from payments.models import ExternalRef, Job
from payments.provider import InvoiceRecord
from tests.job_helpers import run_queued_jobs
from tests.test_charge_now import confirm, pays, ready, store_methods
from tests.test_one_off_charges import (
    CUSTOMER,
    an_operator,
    calls,
    created,
    fresh,
    invoice_of,
    item,
    provider_ref,
)
from tests.test_operator_views import client_for
from tests.test_webhook_handlers import deliver

pytestmark = pytest.mark.django_db

D = Decimal
CARD = "4111 1111 1111 1111"
CARD_DIGITS = "4111111111111111"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    # Paying from a stored method is enabled on the fake; the setting still gates the app.
    provider = FakeProvider(pay_invoice_enabled=True)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


@pytest.fixture
def enabled() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def _nothing_created(operator_id: uuid.UUID) -> None:
    assert not OneOffCharge.objects.filter(operator_id=operator_id).exists()
    assert not Invoice.objects.filter(operator_id=operator_id).exists()
    assert Job.objects.count() == 0
    dump = "".join(str(r.after) + str(r.before) for r in AuditLog.objects.all())
    assert CARD_DIGITS not in dump.replace(" ", "").replace("-", "")


# --- hostile descriptions and notes ------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        f"Card on file {CARD}",
        "4111-1111-1111-1111",
        "<script>alert(1)</script>",
        "x" * 10_000,
        "‮‏‎",  # bidi and format marks only
        "",
    ],
)
def test_a_hostile_description_is_refused_and_nothing_is_written(
    fake: FakeProvider, description: str
) -> None:
    operator = an_operator(fake)
    outcome = service.create_charge(
        operator, None, item(description=description), actor=client_for(Role.BILLING)[1]
    )
    if description == "<script>alert(1)</script>":
        # Markup is not forbidden; it must be inert on every page.
        if outcome.ok and outcome.charge is not None:
            page = client_for(Role.VIEWER)[0].get(f"/one-off-charges/{outcome.charge.pk}/")
            assert "<script>alert(1)</script>" not in page.content.decode()
        return
    assert not outcome.ok, repr(description[:40])
    _nothing_created(operator.pk)
    assert calls(fake, "create_invoice") == []


def test_a_note_with_a_card_number_is_refused_before_any_row(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    outcome = service.create_charge(operator, None, item(note=f"paid by {CARD}"), actor=None)
    assert not outcome.ok
    _nothing_created(operator.pk)


# --- who may do what ----------------------------------------------------------------------------


def test_a_viewer_cannot_create_cancel_or_charge_and_an_outsider_gets_nothing(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    charge = created(operator, item(CollectionMethod.MANUAL))
    viewer, _ = client_for(Role.VIEWER)
    data = {
        "category": "arrears",
        "collection": "manual",
        "price": "10.00",
        "due_date": (timezone.localdate() + timedelta(days=5)).isoformat(),
        "description": "x",
        "note": "y",
        "creation_key": str(uuid.uuid4()),
    }
    assert viewer.post(f"/operators/{operator.pk}/charges/new/", data).status_code == 403
    assert viewer.post(f"/one-off-charges/{charge.pk}/cancel/", {"note": "n"}).status_code == 403
    assert viewer.post(f"/one-off-charges/{charge.pk}/charge-now/", {}).status_code == 403
    billing, _ = client_for(Role.BILLING)
    assert billing.post(f"/one-off-charges/{charge.pk}/charge-now/", {}).status_code == 403
    nobody, _ = client_for(None)
    assert nobody.get(f"/one-off-charges/{charge.pk}/").status_code == 403
    assert nobody.get("/audit/").status_code == 403
    anonymous = Client()
    assert anonymous.get("/audit/").status_code in (302, 401, 403)
    assert anonymous.get("/audit/export.csv").status_code in (302, 401, 403)
    assert OneOffCharge.objects.count() == 1
    assert Job.objects.filter(kind="invoice_pay").count() == 0


# --- provider states around cancel and charge now -------------------------------------------------


def test_a_webhook_with_our_reference_but_another_total_never_links_silently(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL, "3000.00"))
    invoice = invoice_of(charge)
    fake.invoices["fake-wrong-total"] = InvoiceRecord(
        external_id="fake-wrong-total",
        raw_status="DRAFT",
        fetched_at=timezone.now(),
        customer_external_id=CUSTOMER,
        total=D("30.00"),
        raw_summary={"invoice_date_at": invoice.issue_date.isoformat()},
        reference=invoice.invoice_number,
    )
    deliver("create_invoice", id="fake-wrong-total")
    linked = ExternalRef.objects.filter(
        entity_type="invoice", entity_id=invoice.pk, external_id="fake-wrong-total"
    ).exists()
    if linked:
        # Linked by our reference: the total disagreement must be visible, never silent.
        assert fresh(charge).needs_review
    else:
        assert not Invoice.objects.filter(operator=charge.operator).exclude(pk=invoice.pk).exists()


def test_cancel_is_refused_once_a_payment_is_pending_even_from_a_webhook(
    fake: FakeProvider, worker: None
) -> None:
    charge = created(an_operator(fake), item(CollectionMethod.MANUAL, "250.00"))
    run_queued_jobs()
    invoice = invoice_of(charge)
    Payment.objects.create(
        invoice=invoice,
        one_off_charge=charge,
        kind="one_off",
        method="ach",
        amount=D("250.00"),
        status=PaymentStatus.PENDING,
        initiated_on=timezone.localdate(),
    )
    outcome = service.cancel_charge(charge.pk, note="never mind", actor=client_for(Role.BILLING)[1])
    assert not outcome.ok
    assert fresh(charge).status != "voided"
    assert calls(fake, "delete_invoice") == []
    assert fake.invoices[provider_ref(invoice)].raw_status != "DELETED"


def test_charge_now_is_refused_when_the_stored_method_is_a_card(
    fake: FakeProvider, enabled: None
) -> None:
    operator = an_operator(fake)
    store_methods(operator, method="card")
    charge = ready(fake, "100.00", operator=operator, methods=False)
    outcome = confirm(charge, confirmed=True)
    assert not outcome.ok
    run_queued_jobs()
    assert pays(fake) == []
    assert fresh(charge).charge_now_key is None


def test_two_charge_now_confirms_in_a_row_make_one_call_and_one_key(
    fake: FakeProvider, enabled: None
) -> None:
    charge = ready(fake, "100.00")
    first = confirm(charge, confirmed=True)
    second = confirm(charge, confirmed=True)
    assert first.ok and not second.ok
    run_queued_jobs()
    assert len(pays(fake)) == 1
    key = fresh(charge).charge_now_key
    assert key is not None
    # A third attempt after a failure would reuse the key; the model refuses any other value.
    row = fresh(charge)
    row.charge_now_key = uuid.uuid4()
    with pytest.raises((ValueError, ValidationError, IntegrityError)):
        row.save()
    assert fresh(charge).charge_now_key == key


# --- the audit page under hostile parameters ----------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"entity_id": "' OR 1=1 --", "from": "2026-01-01", "to": "2026-12-31"},
        {"action": "*", "from": "0001-01-01", "to": "9999-12-31"},
        {"page": "-1"},
        {"page": "abc"},
        {"page": "99999999999999999999"},
        {"user": "a" * 5000},
        {"entity_type": "../../etc/passwd"},
        {"from": "2026-10-01T00:00:00Z", "to": "now"},
    ],
)
def test_hostile_audit_parameters_answer_200_and_leak_nothing(params: dict[str, str]) -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.get("/audit/", params)
    assert response.status_code == 200
    body = response.content.decode()
    assert "Traceback" not in body and "OperationalError" not in body
    export = admin.get("/audit/export.csv", params)
    assert export.status_code == 200


def test_an_action_prefix_filter_returns_only_that_prefix_and_the_csv_matches(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    created(operator, item(CollectionMethod.MANUAL))
    AuditLog.objects.create(
        action="document.uploaded", entity_type="document", entity_id=str(uuid.uuid4())
    )
    admin, _ = client_for(Role.ADMIN)
    # The page counts New York days; a three-day window holds "now" whatever the hour.
    today = (timezone.localdate() - timedelta(days=1)).isoformat()
    until = (timezone.localdate() + timedelta(days=1)).isoformat()
    page = admin.get(
        "/audit/", {"action": "document.*", "from": today, "to": until}
    ).content.decode()
    assert "document.uploaded" in page
    assert "one_off_charge.created" not in page
    export = b"".join(
        admin.get(
            "/audit/export.csv", {"action": "document.*", "from": today, "to": until}
        ).streaming_content  # type: ignore[attr-defined]
    ).decode()
    assert "one_off_charge" not in export
    assert CARD_DIGITS not in export
