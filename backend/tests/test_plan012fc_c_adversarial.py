"""Fable's adversarial cases for PLAN-012f-c Batch C, the Split into installments page (plan
§6 C, the abuse and boundary cases): hostile parameters on the preview route and the page, the
roles and methods the route refuses, what nothing writes, the count and balance bounds."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import AuditLog, InstallmentPlan, OneOffCharge, RentCharge, RentChargeStatus
from payments.fake import FakeProvider
from payments.models import Job
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store, an_operator
from tests.test_one_off_charges import an_operator as a_linked_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
PREVIEW = "ledger:plans:split_preview"


def written() -> tuple[int, int, int]:
    return InstallmentPlan.objects.count(), AuditLog.objects.count(), Job.objects.count()


def a_rent(due: str = "1200.00") -> RentCharge:
    lease = a_lease(a_store(), an_operator("Acme Stores"))
    return a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE, due)


def preview(client: Client, **params: str) -> tuple[int, dict[str, object]]:
    response = client.get(reverse(PREVIEW), params)
    try:
        payload = json.loads(response.content.decode())
    except ValueError:
        payload = {}
    return response.status_code, payload


def good(rent: RentCharge, **override: str) -> dict[str, str]:
    data = {
        "parent": f"rent:{rent.pk}",
        "count": "3",
        "first": (date.today() + timedelta(days=7)).isoformat(),
        "cadence": "monthly",
    }
    data.update(override)
    return data


# --- Hostile parameters on the preview route -------------------------------------------------


@pytest.mark.parametrize(
    "parent",
    [
        "",
        "rent:",
        "rent:not-a-uuid",
        "lease:00000000-0000-0000-0000-000000000000",
        "rent:../../etc/passwd",
        "<script>alert(1)</script>",
        "rent:" + "9" * 400,
    ],
)
def test_a_hostile_parent_is_refused_without_a_trace(parent: str) -> None:
    rent = a_rent()
    client, _ = client_for(Role.BILLING)
    before = written()
    status, payload = preview(client, **good(rent, parent=parent))
    assert status in (400, 404)
    if status == 400:
        assert payload.get("ok") is False and isinstance(payload.get("message"), str)
        assert "<script" not in json.dumps(payload) and "passwd" not in json.dumps(payload)
    assert written() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("count", "1"),
        ("count", "13"),
        ("count", "12.5"),
        ("count", "-3"),
        ("count", "3; DROP TABLE"),
        ("count", "9" * 40),
        ("first", "2026-13-40"),
        ("first", "yesterday"),
        ("first", (date.today() - timedelta(days=1)).isoformat()),
        ("first", "2026-10-26T00:00:00Z"),
        ("cadence", "<script>"),
        ("cadence", "biweekly"),
        ("cadence", "MONTHLY"),
        ("cadence", ""),
    ],
)
def test_a_bad_count_date_or_cadence_answers_a_refusal_and_writes_nothing(
    field: str, value: str
) -> None:
    rent = a_rent()
    client, _ = client_for(Role.BILLING)
    before = written()
    status, payload = preview(client, **good(rent, **{field: value}))
    assert status == 400, (field, value)
    assert payload.get("ok") is False and payload.get("message")
    assert value not in json.dumps(payload) or len(value) < 4
    assert written() == before


def test_an_unknown_charge_and_a_foreign_store_are_not_found(client_roles: None = None) -> None:
    rent = a_rent()
    other = a_rent()
    client, _ = client_for(Role.BILLING)
    status, _ = preview(client, **good(rent, parent=f"rent:{uuid.uuid4()}"))
    assert status == 404
    response = client.get(reverse(PREVIEW), {**good(rent), "store": str(other.lease.store_id)})
    assert response.status_code == 404


def test_the_route_is_read_only_for_every_role_and_closed_to_a_viewer() -> None:
    rent = a_rent()
    before = written()
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(reverse(PREVIEW), good(rent)).status_code == 403
    for role in (Role.BILLING, Role.ADMIN):
        client, _ = client_for(role)
        assert client.post(reverse(PREVIEW), good(rent)).status_code in (403, 405)
    anonymous = Client()
    assert anonymous.get(reverse(PREVIEW), good(rent)).status_code in (302, 401, 403)
    assert written() == before


def test_a_good_preview_sums_to_the_balance_marks_the_cent_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A one-off charge collected by e-mail of an operator connected to the processor (its
    # installments are invoices there), through the fake provider.
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    operator = a_linked_operator(provider)
    lease = a_lease(a_store(), operator)
    charge = a_one_off_charge(
        operator, "1000.00", date(2026, 9, 20), lease=lease, collection="email"
    )
    client, _ = client_for(Role.BILLING)
    before = written()
    status, payload = preview(client, **good(a_rent(), parent=f"one_off:{charge.pk}", count="3"))
    assert status == 200, payload
    assert payload["ok"] is True
    lines = payload["lines"]
    assert isinstance(lines, list) and len(lines) == 3
    amounts = [D(str(line["amount"])) for line in lines]
    assert sum(amounts) == D("1000.00") and amounts[-1] == D("333.34")
    assert payload["cent_on_last"] is True and payload["share"] in ("333.33", 333.33)
    assert written() == before


# --- The page -------------------------------------------------------------------------------------


def test_the_page_refuses_a_foreign_store_and_an_unknown_charge() -> None:
    rent = a_rent()
    other = a_rent()
    client, _ = client_for(Role.BILLING)
    url = reverse("ledger:charges:split", kwargs={"pk": rent.pk})
    assert client.get(f"{url}?store={other.lease.store_id}").status_code == 404
    assert (
        client.get(reverse("ledger:charges:split", kwargs={"pk": uuid.uuid4()})).status_code == 404
    )


def test_the_page_with_hostile_query_values_renders_without_echoing_them() -> None:
    rent = a_rent()
    client, _ = client_for(Role.BILLING)
    url = reverse("ledger:charges:split", kwargs={"pk": rent.pk})
    response = client.get(
        url, {"count": "<script>alert(1)</script>", "first": "javascript:x", "cadence": "weekly"}
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert "<script>alert(1)</script>" not in body and "javascript:x" not in body


def test_a_two_cent_balance_cannot_be_split_and_the_page_says_so_once() -> None:
    rent = a_rent("0.02")
    client, _ = client_for(Role.BILLING)
    body = client.get(reverse("ledger:charges:split", kwargs={"pk": rent.pk})).content.decode()
    assert 'id="split-refusal"' in body or 'id="split-preview-message"' in body
    assert 'id="split-confirm"' not in body or "disabled" in body


def test_a_one_off_charge_of_another_operator_is_not_previewed_under_this_store() -> None:
    lease = a_lease(a_store(), an_operator("Acme Stores"))
    stranger = a_one_off_charge(an_operator("Other Co"), "300.00", date(2026, 9, 20))
    client, _ = client_for(Role.BILLING)
    response = client.get(
        reverse(PREVIEW),
        {
            "parent": f"one_off:{stranger.pk}",
            "count": "2",
            "first": (date.today() + timedelta(days=3)).isoformat(),
            "cadence": "weekly",
            "store": str(lease.store_id),
        },
    )
    assert response.status_code == 404
    assert OneOffCharge.objects.get(pk=stranger.pk).status == "owed"
