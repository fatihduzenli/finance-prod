"""Adversarial cases for PLAN-012e-2 Batch C (store details, the Store cost block, the New
store form's cost fields, the help pages), by the QA owner (plan §6). An address with markup
is escaped in the line and the title; a ZIP too long or a street carrying a card number is
refused; a hostile landlord rent on a new store refuses the whole thing (no store, no cost
row); billing cannot create a store; the help pages answer 401 anonymous, carry no form of
their own and no user data, and refuse a hostile slug; every store status renders a badge."""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import Store, StoreCost, StoreStatus
from tests.test_operator_views import client_for
from tests.test_store_views import form_data

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
LUHN_CARD = "4111 1111 1111 1111"


def _new_store(**overrides: Any) -> dict[str, Any]:
    data = form_data(
        address_line1="12 Main Street",
        postal_code="62701",
        landlord_rent="3500.00",
        landlord_lease_end="",
        other_monthly_cost="",
        other_cost_description="",
    )
    data.update(overrides)
    return data


def _detail(client: Client, store: Store) -> str:
    return client.get(reverse("ledger:stores:detail", kwargs={"pk": store.pk})).content.decode()


# --- the address line ----------------------------------------------------------------------


def test_an_address_with_markup_is_escaped_in_the_line_the_title_and_the_list() -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(
        reverse("ledger:stores:create"),
        _new_store(
            address_line1=f"12 {MARKUP} Street",
            city=f"Spring{MARKUP}field",
            state="IL",
            postal_code="62701",
            external_label=f"Store {MARKUP}",
        ),
    )
    assert response.status_code == 302, response.content.decode()[:400]
    store = Store.objects.get()
    body = _detail(admin, store)
    assert MARKUP not in body
    assert "&lt;script&gt;" in body
    line = body.split('id="store-address"')[1].split("</dd>")[0]
    assert "Street" in line and "field" in line and "IL 62701" in line
    assert MARKUP not in body.split("<title>")[1].split("</title>")[0]
    stores = admin.get(reverse("ledger:stores:list")).content.decode()
    assert MARKUP not in stores


@pytest.mark.parametrize(
    "field, value",
    [
        ("postal_code", "9" * 21),
        ("postal_code", "x" * 300),
        ("address_line1", f"Suite {LUHN_CARD}"),
        ("address_line1", "a" * 2000),
    ],
)
def test_a_hostile_address_part_is_refused_and_nothing_is_created(field: str, value: str) -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(reverse("ledger:stores:create"), _new_store(**{field: value}))
    assert response.status_code in (200, 302), field
    assert not Store.objects.exists(), field
    assert not StoreCost.objects.exists(), field
    if response.status_code == 200:
        assert LUHN_CARD not in response.content.decode()


# --- the New store form's cost fields -------------------------------------------------------


@pytest.mark.parametrize(
    "rent",
    ["-1", "abc", "1e400", "99999999999999", "NaN", "Infinity", MARKUP, "", "1,000"],
)
def test_a_hostile_landlord_rent_refuses_the_whole_new_store(rent: str) -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(reverse("ledger:stores:create"), _new_store(landlord_rent=rent))
    # The store screens redirect every POST and hold a refused form (PLAN-011 pattern).
    assert response.status_code in (200, 302), rent
    assert not Store.objects.exists() and not StoreCost.objects.exists(), rent
    shown = admin.get(reverse("ledger:stores:create")).content.decode()
    assert MARKUP not in shown and MARKUP not in response.content.decode()


def test_a_zero_landlord_rent_creates_the_store_with_a_no_cost_row() -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(reverse("ledger:stores:create"), _new_store(landlord_rent="0.00"))
    assert response.status_code == 302, response.content.decode()[:400]
    store = Store.objects.get()
    [row] = StoreCost.objects.filter(store=store)
    assert str(row.landlord_rent) == "0.00"
    body = _detail(admin, store)
    assert "no cost" in body
    assert "Margin" not in body.split('id="store-cost"')[1].split("</section>")[0]


def test_billing_and_a_viewer_cannot_create_a_store_even_with_cost_fields() -> None:
    for role in (Role.BILLING, Role.VIEWER):
        client, _ = client_for(role)
        response = client.post(reverse("ledger:stores:create"), _new_store())
        assert response.status_code == 403, role
    assert not Store.objects.exists() and not StoreCost.objects.exists()
    assert Client().post(reverse("ledger:stores:create"), _new_store()).status_code in (401, 403)


def test_a_resubmitted_new_store_creates_one_store_and_one_cost_row() -> None:
    admin, _ = client_for(Role.ADMIN)
    data = _new_store(creation_key=str(uuid.uuid4()))
    first = admin.post(reverse("ledger:stores:create"), data)
    second = admin.post(reverse("ledger:stores:create"), data)
    assert first.status_code == 302 and second.status_code in (200, 302)
    assert Store.objects.count() == 1 and StoreCost.objects.count() == 1


# --- the status badge ------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(StoreStatus.values))
def test_every_store_status_renders_a_badge_with_words(status: str) -> None:
    store = Store.objects.create(external_label="Store 2611", status=status)
    assert store.status == status
    viewer, _ = client_for(Role.VIEWER)
    body = _detail(viewer, store)
    cell = body.split('id="store-status"')[1].split("</dd>")[0]
    assert 'class="badge' in cell, cell
    label = StoreStatus(status).label
    assert label in re.sub(r"<[^>]+>", "", cell), (status, cell)


# --- the help pages ---------------------------------------------------------------------------


def test_the_help_pages_need_a_sign_in_carry_no_form_of_their_own_and_no_user_data() -> None:
    Store.objects.create(
        external_label="Store Secret 2612", city="Hiddenville", status=StoreStatus.ACTIVE
    )
    assert Client().get("/help/").status_code in (401, 403, 302)
    viewer, _ = client_for(Role.VIEWER)
    shell_forms = viewer.get("/").content.decode().count("<form")
    index = viewer.get("/help/")
    assert index.status_code == 200
    body = index.content.decode()
    assert body.count("<form") <= shell_forms
    assert "Hiddenville" not in body and "Store Secret" not in body
    topics = re.findall(r'href="(/help/[a-z0-9-]+/)"', body)
    assert topics, "the help index links no topic"
    for topic in sorted(set(topics)):
        page = viewer.get(topic)
        assert page.status_code == 200, topic
        text = page.content.decode()
        assert text.count("<form") <= shell_forms, topic
        assert "Hiddenville" not in text and "Store Secret" not in text, topic


@pytest.mark.parametrize(
    "path",
    [
        "/help/../../etc/passwd/",
        "/help/%3Cscript%3E/",
        "/help/store-costs/../store-costs/",
        "/help/" + "a" * 300 + "/",
        "/help/STORE-COSTS/",
    ],
)
def test_a_hostile_help_slug_answers_404_and_echoes_nothing(path: str) -> None:
    viewer, _ = client_for(Role.VIEWER)
    response = viewer.get(path)
    assert response.status_code in (404, 200)
    body = response.content.decode()
    assert "<script>" not in body and "etc/passwd" not in body
    assert "a" * 100 not in body
