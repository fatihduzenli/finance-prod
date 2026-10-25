"""Adversarial cases for PLAN-012f-b Batch D (the invoice builder), by the QA owner (plan §6).
Slice D-1: the saved prompt is a read (GET writes nothing) that escapes the operator's name; a
viewer can neither open it nor send; a posted greyed choice, or the removed e-mail choice, is
refused with nothing written and the reason shown; an unknown draft answers 404."""

from __future__ import annotations

import uuid

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import InvoiceDraft, OneOffCharge, PaymentChannel
from payments.models import Job
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_collect_parity import builder_data
from tests.test_how_to_collect import a_draft, one_line
from tests.test_lease_end_drafts import row_counts
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'


def a_bankless_world() -> tuple[object, object]:
    """An operator with no provider customer and no bank method, and an active lease."""
    store = a_store(f"1991 Istanbul {MARKUP}", 1991)
    operator = an_operator(f"Fatih {MARKUP}")
    lease = a_lease(store, operator, channel=PaymentChannel.WIRE)
    return operator, lease


def saved_url(draft: InvoiceDraft) -> str:
    return reverse("ledger:invoices:saved", kwargs={"pk": draft.pk})


def test_the_saved_prompt_writes_nothing_and_escapes_the_operators_name() -> None:
    operator, lease = a_bankless_world()
    draft = a_draft(operator, lease, one_line())  # type: ignore[arg-type]
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    response = billing.get(saved_url(draft))
    assert response.status_code == 200
    body = response.content.decode()
    assert "<dialog" in body and 'id="invoice-saved"' in body
    assert "Invoice saved" in body
    assert MARKUP not in body and "Fatih &lt;script&gt;" in body
    assert row_counts() == before
    assert InvoiceDraft.objects.get(pk=draft.pk).status == draft.status


def test_a_viewer_cannot_open_the_prompt_or_send_from_it() -> None:
    operator, lease = a_bankless_world()
    draft = a_draft(operator, lease, one_line())  # type: ignore[arg-type]
    viewer, _ = client_for(Role.VIEWER)
    before = (row_counts(), Job.objects.count())
    assert viewer.get(saved_url(draft)).status_code in (302, 403)
    send = reverse("ledger:invoices:send", kwargs={"pk": draft.pk})
    assert viewer.post(send).status_code in (302, 403)
    assert (row_counts(), Job.objects.count()) == before
    assert InvoiceDraft.objects.get(pk=draft.pk).status == draft.status


@pytest.mark.parametrize("choice", ["email", "charge_now", "schedule", MARKUP, "settlement "])
def test_a_posted_greyed_or_removed_choice_is_refused_with_nothing_written(choice: str) -> None:
    operator, lease = a_bankless_world()
    billing, _ = client_for(Role.BILLING)
    before = (InvoiceDraft.objects.count(), OneOffCharge.objects.count(), Job.objects.count())
    url = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})  # type: ignore[attr-defined]
    data = builder_data(
        [{"item": "Sign repair", "description": "North wall", "unit_amount": "150.00"}],
        lease=str(lease.pk),  # type: ignore[attr-defined]
        collect=choice,
        schedule_on="2026-12-01",
    )
    response = billing.post(url, data)
    assert response.status_code in (200, 302), choice
    shown = (
        response.content.decode()
        if response.status_code == 200
        else billing.get(url).content.decode()
    )
    assert MARKUP not in shown
    assert (
        InvoiceDraft.objects.count(),
        OneOffCharge.objects.count(),
        Job.objects.count(),
    ) == before, choice


def test_an_unknown_draft_answers_404_on_the_prompt() -> None:
    billing, _ = client_for(Role.BILLING)
    assert (
        billing.get(reverse("ledger:invoices:saved", kwargs={"pk": uuid.uuid4()})).status_code
        == 404
    )
