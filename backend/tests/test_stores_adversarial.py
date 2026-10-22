"""Adversarial cases for PLAN-007b, written by the QA owner (plan row 6)."""

from __future__ import annotations

import re
import uuid
from datetime import date

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import AuditLog, Store
from tests.auth_helpers import make_user, verified_client

pytestmark = pytest.mark.django_db
CARD = "4111111111111111"


def _client(role: Role) -> Client:
    user, device = make_user(f"adv7b-{role.value}-{uuid.uuid4().hex[:6]}", role)
    assert device is not None
    return verified_client(user, device)


def _store_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "creation_key": str(uuid.uuid4()),
        "external_label": "2475",
        "store_number": "2475",
        "city": "Conyers",
        "state": "GA",
        "open_date": "2024-11-01",
        "closed_at": "",
        "status": "active",
        "landlord_rent": "0.00",  # required on a new store since PLAN-012e-2 D9
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "field",
    ["external_label", "city", "state"],
)
def test_a_card_number_in_any_store_text_field_saves_nothing(field: str) -> None:
    admin = _client(Role.ADMIN)
    response = admin.post("/stores/new/", _store_data(**{field: f"x {CARD}"}), follow=True)
    assert Store.objects.count() == 0
    assert CARD not in response.content.decode(errors="replace")


def test_the_second_debit_day_input_carries_no_value_on_the_lease_forms() -> None:
    admin = _client(Role.ADMIN)
    html = admin.get("/leases/new/").content.decode()
    inputs = re.findall(r"<input[^>]*name=\"second_debit_day\"[^>]*>", html)
    assert inputs, "the second debit day input must exist"
    assert all('value="' not in tag for tag in inputs), inputs


def test_store_dates_at_the_edges() -> None:
    admin = _client(Role.ADMIN)
    admin.post("/stores/new/", _store_data(open_date="2024-02-29", closed_at="2024-02-29"))
    assert Store.objects.filter(open_date=date(2024, 2, 29), closed_at=date(2024, 2, 29)).exists()
    before = Store.objects.count()
    admin.post("/stores/new/", _store_data(open_date="2023-02-29"))  # not a real date
    admin.post("/stores/new/", _store_data(open_date="2024-12-01", closed_at="2024-11-30"))
    admin.post("/stores/new/", _store_data(open_date=""))
    assert Store.objects.count() == before


def test_a_replayed_key_with_other_values_changes_nothing_and_writes_one_audit_row() -> None:
    admin = _client(Role.ADMIN)
    data = _store_data(external_label="First")
    admin.post("/stores/new/", data)
    admin.post("/stores/new/", {**data, "external_label": "Second", "city": "Elsewhere"})
    assert Store.objects.count() == 1
    assert Store.objects.get().external_label == "First"
    assert AuditLog.objects.filter(action="store.created").count() == 1


def test_viewer_and_billing_cannot_create_or_edit_a_store() -> None:
    store = Store.objects.create(external_label="9999", open_date=date(2024, 1, 1), status="active")
    for role in (Role.VIEWER, Role.BILLING):
        client = _client(role)
        assert client.post("/stores/new/", _store_data()).status_code == 403
        assert client.post(f"/stores/{store.pk}/edit/", _store_data()).status_code == 403
        assert client.get("/stores/").status_code == 200
    assert Store.objects.count() == 1
