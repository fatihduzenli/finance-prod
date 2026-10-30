"""Adversarial cases for PLAN-012f-b Batch B (stores and operators), by the QA owner (plan §6).
Slice B-1: the four form URLs render their page with the dialog open and write nothing on GET; a
dialog's return URL is a site path and never an outside address; a viewer's POST to each is
refused with nothing written; a held value with markup is escaped inside the dialog; the dialog
is the only form on the page with a primary button."""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel, Store
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_lease_end_drafts import row_counts
from tests.test_operator_views import client_for
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'


def a_world() -> Store:
    store = a_store(f"1991 Istanbul {MARKUP}", 1991)
    a_lease(store, an_operator(f"Fatih {MARKUP}"), channel=PaymentChannel.WIRE)
    return store


def form_urls(store: Store) -> list[tuple[str, Role]]:
    operator = store.leases.get().operator
    assert operator is not None
    return [
        (reverse("ledger:stores:create"), Role.ADMIN),
        (reverse("ledger:stores:edit", kwargs={"pk": store.pk}), Role.ADMIN),
        (reverse("ledger:operators:create"), Role.BILLING),
        (reverse("ledger:operators:edit", kwargs={"pk": operator.pk}), Role.BILLING),
    ]


def the_dialog(body: str) -> str:
    found = re.search(r"<dialog[^>]*class=\"dialog dialog-lg\"[^>]*>.*?</dialog>", body, re.S)
    assert found is not None
    return found.group(0)


def test_every_form_url_renders_its_page_with_the_dialog_open_and_writes_nothing() -> None:
    store = a_world()
    admin, _ = client_for(Role.ADMIN)
    before = row_counts()
    for url, _role in form_urls(store):
        response = admin.get(url)
        assert response.status_code == 200, url
        body = response.content.decode()
        dialog = the_dialog(body)
        head = dialog[: dialog.index(">") + 1]
        assert " open" in head and "data-open" in head, url
        assert 'id="page-header"' in body  # the page behind
        assert MARKUP not in body, url
        escaped = ("Istanbul &lt;script&gt;", "Fatih &lt;script&gt;")
        assert any(text in body for text in escaped) or "create" in url
        assert row_counts() == before, url


def test_the_return_url_is_a_site_path_and_an_outside_address_is_ignored() -> None:
    store = a_world()
    admin, _ = client_for(Role.ADMIN)
    for url, _role in form_urls(store):
        body = admin.get(
            url + "?next=//evil.example.com&return=https://evil.example.com"
        ).content.decode()
        dialog = the_dialog(body)
        head = dialog[: dialog.index(">") + 1]
        found = re.search(r'data-return-url="([^"]*)"', head)
        assert found is not None, url
        assert found.group(1).startswith("/") and not found.group(1).startswith("//"), url
        assert "evil" not in head


def test_a_viewers_post_to_every_form_is_refused_with_nothing_written() -> None:
    store = a_world()
    viewer, _ = client_for(Role.VIEWER)
    before = row_counts()
    for url, _role in form_urls(store):
        data = {"external_label": "X", "display_name": "Y", "email": "y@example.com"}
        response = viewer.post(url, data)
        assert response.status_code in (302, 403), url
    assert row_counts() == before


def test_billing_cannot_open_or_post_the_store_dialogs() -> None:
    store = a_world()
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    for url, role in form_urls(store):
        if role is Role.ADMIN:
            assert billing.get(url).status_code in (302, 403), url
            assert billing.post(url, {"external_label": "X"}).status_code in (302, 403), url
    assert row_counts() == before


def test_a_held_markup_value_is_escaped_inside_the_dialog() -> None:
    a_world()
    admin, _ = client_for(Role.ADMIN)
    url = reverse("ledger:stores:create")
    response = admin.post(url, {"external_label": MARKUP, "store_number": "x"})
    assert response.status_code == 302
    body = admin.get(url).content.decode()
    dialog = the_dialog(body)
    assert MARKUP not in body
    assert "&lt;script&gt;" in dialog


def test_the_dialog_holds_the_pages_only_primary_button() -> None:
    store = a_world()
    admin, _ = client_for(Role.ADMIN)
    for url, _role in form_urls(store):
        body = admin.get(url).content.decode()
        dialog = the_dialog(body)
        assert dialog.count("btn-primary") == 1, url
        outside = body.replace(dialog, "")
        header = outside[outside.index('id="page-header"') :]
        header = header[: header.index("</header>")] if "</header>" in header else header
        assert header.count("btn-primary") <= 1, url


# --- Slice B-3: the store type, the state select, landlord rent optional ----------------------


def test_the_state_mapping_never_invents_a_code() -> None:
    from importlib import import_module

    from finance.us_states import state_name

    migration = import_module("ledger.migrations.0051_store_type_and_state_codes")
    assert migration.state_code(" tx ") == "TX"
    assert migration.state_code("District of Columbia") == "DC"
    assert migration.state_code("new  york") == "NY"
    for raw in (MARKUP, "4111111111111111", "", "Tejas", "T", "TXX", "Texas;drop", "ZZ"):
        assert migration.state_code(raw) == "", raw
    assert state_name(MARKUP) == "" and state_name(None) == "" and state_name("TX") == "Texas"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "ZZ"),
        ("state", MARKUP),
        ("state", "4111111111111111"),
        ("brand", "4111111111111111"),
        ("brand", MARKUP),
        ("brand", "00000000-0000-0000-0000-000000000000"),
        ("landlord_rent", "-1"),
        ("landlord_rent", "1e9"),
        ("landlord_rent", MARKUP),
    ],
)
def test_a_hostile_store_form_value_is_refused_without_echo_and_writes_nothing(
    field: str, value: str
) -> None:
    from ledger.models import StoreCost

    a_world()
    admin, _ = client_for(Role.ADMIN)
    before = (row_counts(), StoreCost.objects.count())
    data = {
        "external_label": "Probe",
        "store_number": "4242",
        "open_date": "2026-10-01",
        "status": "active",
        field: value,
    }
    url = reverse("ledger:stores:create")
    response = admin.post(url, data)
    assert response.status_code == 302
    body = admin.get(url).content.decode()
    if field != "landlord_rent":  # a number field re-fills its held value; the selects never do
        assert_absent(body, value, (field, value))
    assert "4111111111111111" not in body
    assert not Store.objects.filter(external_label="Probe").exists()
    assert (row_counts(), StoreCost.objects.count()) == before


def test_an_empty_landlord_rent_writes_no_cost_row_and_zero_writes_one() -> None:
    from decimal import Decimal

    from ledger.models import StoreCost
    from tests.test_store_views import form_data

    a_world()
    admin, _ = client_for(Role.ADMIN)
    url = reverse("ledger:stores:create")

    def created(**fields: str) -> Store:
        response = admin.post(url, form_data(**fields))
        assert response.status_code == 302, response.status_code
        return Store.objects.get(pk=response["Location"].rstrip("/").rsplit("/", 1)[-1])

    empty = created(external_label="Empty", store_number="4301", landlord_rent="")
    assert not StoreCost.objects.filter(store=empty).exists()
    zero = created(external_label="Zero", store_number="4302", landlord_rent="0")
    assert StoreCost.objects.get(store=zero).landlord_rent == Decimal("0.00")
    page = admin.get(reverse("ledger:stores:detail", kwargs={"pk": empty.pk})).content.decode()
    assert "Not entered" in page


def test_markup_in_a_store_type_name_is_escaped_on_every_pill() -> None:
    from ledger.models import Brand

    store = a_world()
    kind = Brand.objects.create(name=f"Kiosk {MARKUP}", sort_order=99)
    store.brand = kind
    store.save(update_fields=["brand"])
    operator = store.leases.get().operator
    assert operator is not None
    billing, _ = client_for(Role.BILLING)
    for url in (
        reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
        reverse("ledger:stores:list"),
        "/grid/",
        reverse("ledger:operators:detail", kwargs={"pk": operator.pk}),
    ):
        body = billing.get(url).content.decode()
        assert MARKUP not in body, url
        assert "Kiosk &lt;script&gt;" in body, url


def test_a_blank_state_lands_in_needs_review_and_a_closed_store_does_not() -> None:
    from ledger.services import attention

    store = a_world()
    store.state = ""
    store.save(update_fields=["state"])
    closed = a_store("5555 Closed", 5555)
    closed.state = ""
    closed.status = "closed"
    closed.save(update_fields=["state", "status"])
    from django.utils import timezone

    found = attention.attention(timezone.localdate()).items
    items = [item for item in found if item.kind == attention.STORE_STATE_MISSING]
    assert [item.store_id for item in items] == [store.pk]
    assert all(item.text.startswith("State needs choosing") for item in items)
    # The text is data; the page escapes it.
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/").content.decode()
    assert MARKUP not in body and "State needs choosing" in body


# --- Slice B-2: the operator type ------------------------------------------------------------


def test_a_forged_display_name_is_ignored_and_the_name_is_derived() -> None:
    from ledger.models import Operator
    from tests.test_operator_views import form_data as operator_data

    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:operators:create")
    data = operator_data(
        kind="person",
        company="",
        first_name="Ada",
        last_name="Lovelace",
        display_name=MARKUP,
        email="ada@example.com",
    )
    response = billing.post(url, data)
    assert response.status_code == 302, response.status_code
    operator = Operator.objects.get(email="ada@example.com")
    assert operator.display_name == "Ada Lovelace"
    assert operator.kind == "person"


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", MARKUP), ("kind", "robot"), ("kind", "4111111111111111"), ("kind", "")],
)
def test_a_hostile_kind_is_refused_without_echo_and_writes_nothing(field: str, value: str) -> None:
    from ledger.models import Operator
    from tests.test_operator_views import form_data as operator_data

    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:operators:create")
    before = Operator.objects.count()
    response = billing.post(url, operator_data(email="kind@example.com", **{field: value}))
    assert response.status_code == 302
    body = billing.get(url).content.decode()
    assert "4111111111111111" not in body and MARKUP not in body
    assert Operator.objects.count() == before


def test_a_person_without_a_last_name_and_a_company_without_a_name_are_refused() -> None:
    from ledger.models import Operator
    from tests.test_operator_views import form_data as operator_data

    billing, _ = client_for(Role.BILLING)
    url = reverse("ledger:operators:create")
    before = Operator.objects.count()
    for data in (
        operator_data(kind="person", company="", first_name="Ada", last_name="   "),
        operator_data(kind="company", company="   "),
    ):
        assert billing.post(url, data).status_code == 302
        body = billing.get(url).content.decode()
        assert "Enter the" in body
    assert Operator.objects.count() == before


def test_an_existing_operator_opens_as_a_person_with_its_name_kept() -> None:
    store = a_world()
    operator = store.leases.get().operator
    assert operator is not None
    operator.refresh_from_db()
    assert operator.kind == "person" and operator.first_name == "" and operator.last_name == ""
    assert MARKUP in operator.display_name  # kept as it was, never guessed
    billing, _ = client_for(Role.BILLING)
    body = billing.get(
        reverse("ledger:operators:detail", kwargs={"pk": operator.pk})
    ).content.decode()
    assert "Fatih &lt;script&gt;" in body and MARKUP not in body


def test_the_search_finds_a_person_by_last_name_without_echoing_markup() -> None:
    from ledger.models import Operator

    Operator.objects.create(
        display_name="x",
        kind="person",
        first_name="Grace",
        last_name=f"Hopper {MARKUP}",
        email="g@example.com",
    )
    billing, _ = client_for(Role.BILLING)
    response = billing.get(reverse("ledger:search") + "?q=Hopper", follow=True)
    body = response.content.decode()
    assert MARKUP not in body
    assert "Hopper &lt;script&gt;" in body


# --- Slice B-4: the USPS address check ----------------------------------------------------------


def check_url() -> str:
    return reverse("ledger:stores:check_address")


@pytest.mark.parametrize(
    "data",
    [
        {"street": MARKUP, "city": MARKUP, "state": MARKUP, "zip": MARKUP},
        {"street": "4111111111111111", "city": "x", "state": "TX", "zip": "4111111111111111"},
        {"street": "a" * 5000, "city": "b" * 5000, "state": "c" * 5000, "zip": "9" * 5000},
        {"street": "1 Main St", "state": "TX; drop table stores", "zip": "%00"},
    ],
)
def test_a_hostile_address_check_never_echoes_and_writes_nothing(data: dict[str, str]) -> None:
    a_world()
    billing, _ = client_for(Role.BILLING)
    before = row_counts()
    response = billing.post(check_url(), data)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    body = response.content.decode()
    assert MARKUP not in body and "4111111111111111" not in body and "drop table" not in body
    assert '"verified": false' in body
    assert row_counts() == before


def test_the_address_check_route_refuses_the_wrong_method_role_and_session() -> None:
    from django.test import Client

    a_world()
    billing, _ = client_for(Role.BILLING)
    assert billing.get(check_url()).status_code == 405
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(check_url(), {"street": "1 Main St"}).status_code in (302, 403)
    assert Client().post(check_url(), {"street": "1 Main St"}).status_code in (401, 403)
    assert billing.post(check_url(), {"street": ""}).status_code == 400


def test_a_failed_check_answers_a_fixed_sentence_and_never_the_secret(
    monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from finance import usps

    secret = "placeholder-usps-secret-ZZ9"
    settings.USPS_CLIENT_ID = "placeholder-usps-id"
    settings.USPS_CLIENT_SECRET = secret
    monkeypatch.setattr(usps, "check", lambda address: None)
    a_world()
    billing, _ = client_for(Role.BILLING)
    body = billing.post(check_url(), {"street": "1 Main St", "state": "TX", "zip": "75001"})
    text = body.content.decode()
    assert secret not in text and "placeholder-usps-id" not in text
    assert '"verified": false' in text
    page = billing.get(reverse("ledger:stores:list")).content.decode()
    assert secret not in page


def test_the_usps_module_and_script_handle_the_secret_and_markup_safely() -> None:
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    module = (backend / "finance" / "usps.py").read_text(encoding="utf-8")
    for line in module.splitlines():
        if "logger." in line:
            assert "secret" not in line.lower() and "token" not in line.lower(), line
            assert "address" not in line.lower() or "USPS" in line, line
    assert "verify=False" not in module and "follow_redirects=True" not in module
    script = (backend / "ledger" / "static" / "ledger" / "address_check.js").read_text(
        encoding="utf-8"
    )
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "X-CSRFToken" in script or "csrfmiddlewaretoken" in script
    assert "credentials" in script
