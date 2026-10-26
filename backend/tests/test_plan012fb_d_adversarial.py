"""Adversarial cases for PLAN-012f-b Batch D (the invoice builder), by the QA owner (plan §6).
Slice D-1: the saved prompt is a read (GET writes nothing) that escapes the operator's name; a
viewer can neither open it nor send; a posted greyed choice, or the removed e-mail choice, is
refused with nothing written and the reason shown; an unknown draft answers 404."""

from __future__ import annotations

import uuid
from typing import Any

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


# --- Slice D-2: Schedule settlement -------------------------------------------------------------


def preview_url(**params: str) -> str:
    from urllib.parse import urlencode

    return reverse("ledger:invoices:settlement_preview") + "?" + urlencode(params)


@pytest.mark.parametrize(
    "params",
    [
        {"total": "1000.00", "count": "1", "first": "2026-12-01", "cadence": "monthly"},
        {"total": "1000.00", "count": "13", "first": "2026-12-01", "cadence": "monthly"},
        {"total": "1000.00", "count": "3", "first": "2020-01-01", "cadence": "monthly"},
        {"total": "1000.00", "count": "3", "first": "2026-12-01", "cadence": "daily"},
        {"total": "-5", "count": "3", "first": "2026-12-01", "cadence": "weekly"},
        {"total": "NaN", "count": "3", "first": "2026-12-01", "cadence": "weekly"},
        {"total": MARKUP, "count": MARKUP, "first": MARKUP, "cadence": MARKUP},
        {"total": "1e400", "count": "3", "first": "2026-12-01", "cadence": "yearly"},
        {},
    ],
)
def test_a_hostile_preview_request_answers_a_plain_refusal_and_writes_nothing(
    params: dict[str, str],
) -> None:
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    response = billing.get(preview_url(**params))
    assert response.status_code in (200, 400)
    body = response.content.decode()
    assert '"ok": false' in body or '"ok":false' in body
    assert "<script>" not in body and "NaN" not in body and "Infinity" not in body
    assert row_counts() == before


def test_the_preview_route_needs_billing_and_a_session() -> None:
    from django.test import Client

    good = {"total": "1000.00", "count": "10", "first": "2026-12-01", "cadence": "monthly"}
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(preview_url(**good)).status_code in (302, 403)
    assert Client().get(preview_url(**good)).status_code in (401, 403)
    billing, _ = client_for(Role.BILLING)
    response = billing.get(preview_url(**good))
    assert response.status_code == 200
    assert "10 debits of $100.00" in response.content.decode()


@pytest.mark.parametrize(
    ("count", "first", "cadence"),
    [
        ("1", "2026-12-01", "monthly"),
        ("13", "2026-12-01", "monthly"),
        ("3", "2020-01-01", "monthly"),
        ("3", "2026-12-01", "daily"),
    ],
)
def test_a_hostile_settlement_post_writes_nothing(count: str, first: str, cadence: str) -> None:
    from ledger.models import InstallmentPlan

    operator, lease = a_bankless_world()
    billing, _ = client_for(Role.BILLING)
    before = (
        InvoiceDraft.objects.count(),
        OneOffCharge.objects.count(),
        InstallmentPlan.objects.count(),
        Job.objects.count(),
    )
    url = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})  # type: ignore[attr-defined]
    data = builder_data(
        [{"item": "Sign repair", "description": "North wall", "unit_amount": "1000.00"}],
        lease=str(lease.pk),  # type: ignore[attr-defined]
        collect="settlement",
        settlement_count=count,
        settlement_first_on=first,
        settlement_cadence=cadence,
    )
    response = billing.post(url, data)
    assert response.status_code in (200, 302)
    assert (
        InvoiceDraft.objects.count(),
        OneOffCharge.objects.count(),
        InstallmentPlan.objects.count(),
        Job.objects.count(),
    ) == before, (count, first, cadence)


def test_the_settlement_shares_add_up_and_the_rounding_lands_on_the_last() -> None:
    from decimal import Decimal

    from ledger.services import installments

    for total, count in (("100.00", 3), ("10000.00", 10), ("0.07", 2), ("999999.99", 12)):
        shares = installments.split(Decimal(total), count)
        assert sum(shares) == Decimal(total), (total, count)
        assert len(shares) == count
        assert len(set(shares[:-1])) == 1  # every share but the last is equal
        assert all(share >= 0 for share in shares)


# --- Slice D-3: Pause / Resume / Stop for installment plans -------------------------------------

from tests.test_installment_plan_actions import (  # noqa: E402 — the slice's fixtures and builders
    a_store_lease,
    autopay_plan,
    fake,  # noqa: F401
    fixed_now,  # noqa: F401
    releases,
    worker,  # noqa: F401
    written,
)


def plan_url(name: str, plan: object) -> str:
    return reverse(f"ledger:plans:{name}", kwargs={"pk": plan.pk})  # type: ignore[attr-defined]


def test_a_viewer_can_neither_pause_resume_nor_stop_and_nothing_is_written(
    fake: Any,  # noqa: F811
    worker: None,  # noqa: F811
) -> None:
    plan = autopay_plan(fake, lease=a_store_lease(fake))
    viewer, _ = client_for(Role.VIEWER)
    before = (written(), row_counts())
    for name in ("pause", "resume", "cancel"):
        assert viewer.post(plan_url(name, plan), {"note": "x"}).status_code in (302, 403), name
    assert (written(), row_counts()) == before


def test_a_pause_never_debits_and_a_double_post_releases_once_per_schedule(
    fake: Any,  # noqa: F811
    worker: None,  # noqa: F811
) -> None:
    from payments.models import JobKind

    plan = autopay_plan(fake, lease=a_store_lease(fake))
    billing, _ = client_for(Role.BILLING)
    assert billing.post(plan_url("pause", plan)).status_code == 302
    first = len(releases())
    assert first >= 1
    assert not Job.objects.filter(kind=JobKind.INVOICE_PAY).exists()  # no money moves on a pause
    assert billing.post(plan_url("pause", plan)).status_code == 302  # refused as not active or busy
    assert len(releases()) == first
    assert not Job.objects.filter(kind=JobKind.INVOICE_PAY).exists()


def test_the_routes_refuse_get_and_an_unknown_plan(
    fake: Any,  # noqa: F811
    worker: None,  # noqa: F811
) -> None:
    plan = autopay_plan(fake, lease=a_store_lease(fake))
    billing, _ = client_for(Role.BILLING)
    for name in ("pause", "resume"):
        assert billing.get(plan_url(name, plan)).status_code == 405, name
        unknown = reverse(f"ledger:plans:{name}", kwargs={"pk": uuid.uuid4()})
        assert billing.post(unknown).status_code == 404, name
    hostile = reverse("ledger:plans:pause", kwargs={"pk": "x" * 36})
    assert billing.post(hostile).status_code == 404


def test_the_stop_page_is_a_read_that_escapes_markup(
    fake: Any,  # noqa: F811
    worker: None,  # noqa: F811
) -> None:
    from ledger.models import OneOffCharge as Charge

    lease = a_store_lease(fake)
    plan = autopay_plan(fake, lease=lease)
    parent = plan.one_off_charge
    assert parent is not None
    Charge.objects.filter(pk=parent.pk).update(description=f"Fee {MARKUP}")
    admin, _ = client_for(Role.ADMIN)
    before = (written(), row_counts())
    page = admin.get(plan_url("cancel", plan))
    assert page.status_code == 200
    body = page.content.decode()
    assert "Stop" in body and MARKUP not in body
    assert (written(), row_counts()) == before
