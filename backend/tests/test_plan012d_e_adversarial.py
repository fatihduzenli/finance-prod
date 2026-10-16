"""Adversarial cases for PLAN-012d Batch E (the operator page as the workspace), by the QA
owner (plan §6 E). The store selector cannot pick another operator's store; the bar's actions
act on the selected store's lease only; a viewer sees no bar; the drafts' Delete on the
operator page cannot delete a sent invoice."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import Invoice, InvoiceDraft, InvoiceDraftStatus, Lease, PaymentChannel
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _operator_with_two_stores() -> tuple[Lease, Lease]:
    operator = an_operator("Two-store operator")
    first = a_lease(
        a_store("1001"), operator, started=date(2026, 9, 1), channel=PaymentChannel.WIRE
    )
    second = a_lease(
        a_store("1002"), operator, started=date(2026, 9, 1), channel=PaymentChannel.WIRE
    )
    return first, second


def _operator_page(lease: Lease, **query: str) -> str:
    url = reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id})
    if query:
        url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    return url


def test_the_store_selector_cannot_pick_another_operators_store() -> None:
    mine_first, _ = _operator_with_two_stores()
    theirs = a_lease(
        a_store("2001"), an_operator("Other"), started=date(2026, 9, 1), channel=PaymentChannel.WIRE
    )
    a_rent_charge(theirs, date(2026, 10, 1), due="4000.00")
    billing, _ = client_for(Role.BILLING)
    response = billing.get(_operator_page(mine_first, store=str(theirs.store_id)))
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        body = response.content.decode()
        assert str(theirs.store_id) not in body
        assert str(theirs.pk) not in body
        assert "4,000.00" not in body and "4000.00" not in body
        # The bar's links carry one of this operator's leases, never the other's.
        assert f"lease={theirs.pk}" not in body and f"store={theirs.store_id}" not in body


def test_the_bars_actions_act_on_the_selected_store_only() -> None:
    first, second = _operator_with_two_stores()
    billing, _ = client_for(Role.BILLING)
    page_second = billing.get(_operator_page(first, store=str(second.store_id))).content.decode()
    assert f"store={second.store_id}" in page_second or str(second.store_id) in page_second
    assert f"lease={first.pk}" not in page_second
    page_first = billing.get(_operator_page(first, store=str(first.store_id))).content.decode()
    assert f"lease={second.pk}" not in page_first
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(_operator_page(first)).content.decode()
    for action in ("/payments/new/", "/schedule-payment/", "/end-lease/", "/transfer/"):
        assert action not in body, action


def test_the_operator_pages_drafts_delete_cannot_delete_a_sent_invoice() -> None:
    first, _ = _operator_with_two_stores()
    assert first.operator is not None
    invoice = Invoice.objects.create(
        invoice_number="INV-2026-0902",
        operator=first.operator,
        issue_date=TODAY,
        due_date=TODAY,
        subtotal=D("100.00"),
        total=D("100.00"),
    )
    draft = InvoiceDraft.objects.create(
        operator=first.operator,
        lease=first,
        memo="Sent one",
        status=InvoiceDraftStatus.SENT,
        sent_invoice=invoice,
    )
    billing, _ = client_for(Role.BILLING)
    body = billing.get(_operator_page(first, tab="invoices")).content.decode()
    delete = reverse("ledger:invoices:delete_draft", kwargs={"pk": draft.pk})
    assert delete not in body
    assert billing.post(delete, {}).status_code in (302, 403, 405)
    assert InvoiceDraft.objects.filter(pk=draft.pk).exists()
