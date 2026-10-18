"""Adversarial cases for PLAN-012c Batch C ("forms show only what applies", D8), by the QA
owner (plan §6 C): hiding never relaxes validation. Each case posts a field that the page hides
in the current state, with a hostile value, and asserts that the server refuses or ignores it
exactly as before the batch: the row unchanged, nothing written, no markup echoed raw. The file
passes on the tree before Batch C (recorded on a clean worktree of the Batch B commit) and must
pass after it."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import (
    DepositResolution,
    Document,
    Lease,
    LeaseCredit,
    Operator,
    Payment,
    PaymentChannel,
)
from tests import test_lease_end as lease_end_tests
from tests import test_plan012d_b_adversarial as payment_tests
from tests.document_files import make_pdf
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_documents import post_file
from tests.test_lease_end import deductions, draft, figures, fresh, october
from tests.test_lease_views import edit_data
from tests.test_operator_views import client_for, form_data

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
# The lease-end file's autouse fixtures, re-exported so pytest applies them here too.
storage = lease_end_tests.storage
memory = lease_end_tests.memory


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: lease_end_tests.TODAY)
    return lease_end_tests.TODAY


def _refused_on_the_form(client: Client, path: str, data: dict[str, str]) -> str:
    """Post ``data``; the form is held with its error (rendered, or redirected back to itself
    with the errors kept, the PLAN-011 pattern). Returns the page shown afterwards."""
    response = client.post(path, data)
    assert response.status_code in (200, 302), path
    if response.status_code == 302:
        assert response["Location"].split("?")[0] == path, response["Location"]
        response = client.get(path)
        assert response.status_code == 200
    body = response.content.decode()
    assert MARKUP not in body
    assert "errorlist" in body or "field-error" in body or "error" in body.lower(), path
    return body


def _lease_fields(lease: Lease) -> tuple[object, ...]:
    row = Lease.objects.get(pk=lease.pk)
    return (
        row.debit_day,
        row.second_debit_day,
        row.payment_channel,
        row.collection_status,
        row.started_on,
    )


# --- the lease form: the second debit day of a monthly lease ----------------------------------


@pytest.mark.parametrize("hostile", ["abc", "-1", "0", "32", "1e1", MARKUP])
def test_a_hostile_second_debit_day_on_a_monthly_lease_is_refused(hostile: str) -> None:
    lease = a_lease(a_store("2491"), an_operator("Monthly Co"), channel=PaymentChannel.WIRE)
    before = _lease_fields(lease)
    admin, _ = client_for(Role.ADMIN)
    _refused_on_the_form(
        admin, f"/leases/{lease.pk}/edit/", edit_data(lease, second_debit_day=hostile)
    )
    assert _lease_fields(lease) == before


def test_a_second_debit_day_equal_to_the_first_is_refused_as_before() -> None:
    lease = a_lease(a_store("2492"), an_operator("Same Day Co"), channel=PaymentChannel.WIRE)
    before = _lease_fields(lease)
    admin, _ = client_for(Role.ADMIN)
    body = _refused_on_the_form(
        admin, f"/leases/{lease.pk}/edit/", edit_data(lease, second_debit_day=str(lease.debit_day))
    )
    assert "must differ" in body
    assert _lease_fields(lease) == before


# --- the operator form: consents and contact details on an operator without any ------------------


@pytest.mark.parametrize(
    "field", ["sms_consent_at", "email_consent_at", "sms_phone", "phone", "email"]
)
def test_a_hostile_contact_or_consent_value_is_refused_on_an_operator_without_contact_details(
    field: str,
) -> None:
    operator = Operator.objects.create(display_name="No Contact Co")
    admin, _ = client_for(Role.ADMIN)
    data = form_data(display_name="No Contact Co", email="", phone="", sms_phone="")
    data.pop("creation_key", None)
    data[field] = MARKUP if field in ("phone", "sms_phone", "email") else "2026-13-45T99:99"
    _refused_on_the_form(admin, f"/operators/{operator.pk}/edit/", data)
    row = Operator.objects.get(pk=operator.pk)
    assert not row.email and not row.phone and not row.sms_phone
    assert row.sms_consent_at is None and row.email_consent_at is None


# --- the lease-end settlement when nothing is refunded -------------------------------------------


def test_a_refund_method_posted_when_the_operator_owes_creates_no_refund() -> None:
    admin, user = client_for(Role.ADMIN)
    lease = a_lease(a_store("2493"), an_operator("Owing Co"), channel=PaymentChannel.WIRE)
    october(lease)  # the last month unpaid: the operator owes, nothing is refunded
    end = draft(lease, user)
    assert deductions(end).ok
    before = figures(end)
    assert before.refund_due == Decimal("0.00") and before.open_total > 0
    response = admin.post(
        f"/lease-ends/{end.pk}/settlement/",
        {
            "method": "zelle",
            "refund_on": lease_end_tests.TODAY.isoformat(),
            "reference": f"ZL-{MARKUP}",
            "note": MARKUP,
        },
    )
    assert response.status_code in (200, 302)
    for path in (f"/lease-ends/{end.pk}/settlement/", f"/lease-ends/{end.pk}/confirm/"):
        page = admin.get(path)
        assert page.status_code == 200, path
        assert MARKUP not in page.content.decode(), path
    after = figures(fresh(end))
    # Whatever the posted method, nothing becomes refundable and what is owed is unchanged.
    assert after.refund_due == Decimal("0.00")
    assert (after.open_total, after.still_open, after.debt) == (
        before.open_total,
        before.still_open,
        before.debt,
    )
    assert not Payment.objects.filter(amount__lt=0).exists()


# --- the deposit resolution: a transfer with no other lease ---------------------------------


def test_a_transfer_to_lease_posted_with_no_other_lease_is_refused() -> None:
    admin, user = client_for(Role.ADMIN)
    lease = a_lease(a_store("2494"), an_operator("Single Lease Co"), channel=PaymentChannel.WIRE)
    charge = lease_end_tests.deposit(lease, paid="2000.00")
    stranger = a_lease(a_store("2495"), an_operator("Other Co"), channel=PaymentChannel.WIRE)
    for target in (str(uuid.uuid4()), str(stranger.pk), MARKUP, ""):
        response = admin.post(
            f"/leases/{lease.pk}/deposits/{charge.pk}/resolve/",
            {
                "kind": "transferred",
                "amount": "2000.00",
                "resolved_on": lease_end_tests.TODAY.isoformat(),
                "method": "",
                "to_lease": target,
                "note": "",
            },
        )
        assert response.status_code in (200, 302, 404), target
        if response.status_code == 200:
            assert MARKUP not in response.content.decode()
        assert not DepositResolution.objects.exists(), target
    charge.refresh_from_db()
    assert charge.status != "voided"


# --- the document upload: a kind that is not the owner's -----------------------------------------


@pytest.mark.parametrize("kind", ["dispute_evidence", MARKUP, "nope", ""])
def test_a_kind_outside_the_owners_list_is_refused_on_a_store_upload(kind: str) -> None:
    billing, _ = client_for(Role.BILLING)
    store = a_store("2496")
    response = post_file(
        billing, store, "Lease 2026.pdf", make_pdf([["Lease agreement"]]), kind=kind
    )
    assert response.status_code in (200, 302, 400), kind
    assert MARKUP not in response.content.decode()
    assert not Document.objects.exists(), kind


# --- record a payment: keep the rest as a credit when nothing is left ------------------------


def test_keep_credit_posted_with_a_fully_allocated_payment_creates_no_credit() -> None:
    lease = payment_tests._lease()  # noqa: SLF001 - the PLAN-012d B file's helpers
    rent, _ = payment_tests._charges(lease)  # noqa: SLF001
    billing, _ = client_for(Role.BILLING)
    response = billing.post(
        payment_tests._url(lease),  # noqa: SLF001
        payment_tests._post("1000.00", {rent.pk: "1000.00"}, keep_credit="true"),  # noqa: SLF001
    )
    assert response.status_code == 302, response.content.decode()[:600]
    assert Payment.objects.count() == 1
    assert not LeaseCredit.objects.exists()


def test_anonymous_posts_to_the_forms_above_are_refused() -> None:
    lease = a_lease(a_store("2497"), an_operator("Anon Co"), channel=PaymentChannel.WIRE)
    anonymous = Client()
    for path in (
        f"/leases/{lease.pk}/edit/",
        f"/operators/{lease.operator_id}/edit/",
        f"/documents/stores/{lease.store_id}/upload/",
    ):
        assert anonymous.post(path, {"x": MARKUP}).status_code in (401, 403, 302), path
