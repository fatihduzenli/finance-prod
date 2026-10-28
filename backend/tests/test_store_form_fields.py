"""PLAN-012f-b Batch B, slice B-3 (D10 to D12): the store type (the seeded list, the form's
select of active types, the grey pill after the name on the store page, the Stores list, the
Monthly rent overview and the operator's store cards, nothing when empty), the State select
(codes stored, names shown, anything else refused without echo; ledger migration 0051's
mapping of the free-text values), "State needs choosing", and the optional landlord rent
(empty is no cost row and unknown in the P&L and the Cashflow card; 0.00 is a row)."""

from __future__ import annotations

import importlib
import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django import forms
from django.apps import apps as django_apps

from accounts.roles import Role
from finance.us_states import US_STATES, state_name
from ledger.forms import STATE_REFUSED, StoreCreateForm, StoreForm
from ledger.models import AuditLog, Store, StoreCost, StoreStatus, StoreType
from ledger.services import attention, dashboard, pnl
from ledger.services import stores as service
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_pnl import JULY, rent
from tests.test_store_views import form_data

pytestmark = pytest.mark.django_db

NEW = "/stores/new/"
MIGRATION = importlib.import_module("ledger.migrations.0051_store_type_and_state_codes")
PILL = '<span class="pill pill-type">Kiosk</span>'


def kiosk() -> StoreType:
    return StoreType.objects.get(name="Kiosk")


def typed_store(**fields: Any) -> Store:
    store = a_store("Typed", 1991, **fields)
    store.store_type = kiosk()
    store.save()
    return store


def choices(form: StoreForm, name: str) -> list[Any]:
    field = form.fields[name]
    assert isinstance(field, forms.ChoiceField | forms.ModelChoiceField)
    offered: Any = field.choices
    return list(offered)


def html(path: str, role: Role = Role.ADMIN) -> str:
    return client_for(role)[0].get(path).content.decode()


# --- D10: the store type ------------------------------------------------------------------


def test_the_five_types_are_seeded_in_order_and_active() -> None:
    assert list(StoreType.objects.values_list("name", "active")) == [
        ("Inline store", True),
        ("Kiosk", True),
        ("Cart", True),
        ("Pop-up", True),
        ("Warehouse", True),
    ]


def test_the_type_select_lists_the_active_types_and_keeps_a_stores_own_inactive_one() -> None:
    StoreType.objects.filter(name="Cart").update(active=False)
    labels = [label for _, label in choices(StoreForm(), "store_type")]
    assert labels == ["—", "Inline store", "Kiosk", "Pop-up", "Warehouse"]
    store = a_store("Cart store")
    store.store_type = StoreType.objects.get(name="Cart")
    store.save()
    labels = [label for _, label in choices(StoreForm(instance=store), "store_type")]
    assert "Cart" in labels


def test_an_inactive_or_unknown_type_is_refused_without_echo() -> None:
    cart = StoreType.objects.get(name="Cart")
    cart.active = False
    cart.save()
    for value in (str(cart.pk), "4111111111111111"):
        form = StoreCreateForm(data=form_data(store_type=value))
        assert not form.is_valid() and "store_type" in form.errors
        assert value not in str(form.errors)


def test_the_new_store_form_saves_the_type_and_audits_it() -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(NEW, form_data(store_type=str(kiosk().pk)))
    assert response.status_code == 302
    store = Store.objects.get()
    assert store.store_type == kiosk()
    created = AuditLog.objects.get(action="store.created")
    assert created.after is not None and created.after["store_type"] == str(kiosk().pk)


def test_the_pill_shows_on_the_four_surfaces_when_set() -> None:
    store = typed_store()
    operator = an_operator("Acme")
    a_lease(store, operator)
    page = html(f"/stores/{store.pk}/")
    assert re.search(
        r'id="store-title">#1991 Typed <span class="pill pill-type">Kiosk</span>', page
    )
    listed = html("/stores/")
    assert f'#1991 Typed</a> {PILL}<span class="cell-subline">' in listed
    grid = html("/grid/")
    assert f'#1991 Typed</a> {PILL}<span class="cell-subline">Acme</span>' in grid
    cards = html(f"/operators/{operator.pk}/")
    assert f">#1991 Typed</a> {PILL}</span>" in cards


def test_no_pill_anywhere_when_empty() -> None:
    store = a_store("Plain", 1992)
    operator = an_operator("Acme")
    a_lease(store, operator)
    for path in (f"/stores/{store.pk}/", "/stores/", "/grid/", f"/operators/{operator.pk}/"):
        assert "pill-type" not in html(path), path


def test_the_list_and_the_grid_read_the_type_without_a_query_per_row() -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def count(path: str) -> int:
        client = client_for(Role.VIEWER)[0]
        client.get(path)
        with CaptureQueriesContext(connection) as captured:
            client.get(path)
        return len(captured)

    typed_store()
    small = (count("/stores/"), count("/grid/"))
    for number in range(5):
        extra = a_store(f"More {number}", 3000 + number)
        extra.store_type = kiosk()
        extra.save()
    assert (count("/stores/"), count("/grid/")) == small


# --- D11: the state ------------------------------------------------------------------------


def test_the_state_select_offers_the_codes_by_name() -> None:
    offered = choices(StoreForm(), "state")
    assert offered[0] == ("", "—")
    assert offered[1:] == list(US_STATES) and len(US_STATES) == 51
    page = html(NEW)
    assert '<option value="TX">Texas</option>' in page
    assert '<option value="DC">District of Columbia</option>' in page


@pytest.mark.parametrize("value", ["ZZ", "Texas", "tx", "4111111111111111"])
def test_anything_but_a_code_is_refused_without_echo(value: str) -> None:
    form = StoreCreateForm(data=form_data(state=value))
    assert not form.is_valid()
    assert form.errors["state"] == [STATE_REFUSED]
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(NEW, form_data(state=value), follow=True)
    assert not Store.objects.exists()
    field = response.content.decode().split('id="id_state"', 1)[1].split("</select>", 1)[1]
    assert STATE_REFUSED in field.split("</div>", 1)[0]
    if value != "Texas":  # "Texas" is an option's label; the rest are never on the page
        # As a token of its own: a CSRF token or a hex id may hold "tx" inside a longer run.
        as_a_token = rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])"
        assert not re.search(as_a_token, response.content.decode())


def test_an_empty_state_is_allowed() -> None:
    assert StoreCreateForm(data=form_data(state="")).is_valid()


def test_screens_show_the_name_and_the_csv_keeps_the_code() -> None:
    store = a_store("Lone Star", 1993, state="TX")
    store.city = "Austin"
    store.save()
    assert "Austin, Texas" in html(f"/stores/{store.pk}/")
    listed = html("/stores/")
    assert '<span class="cell-subline">Austin, Texas</span>' in listed
    assert '<option value="TX">Texas</option>' in listed
    assert '<option value="TX">Texas</option>' in html("/grid/")
    export = client_for(Role.VIEWER)[0].get("/stores/export.csv")
    body = b"".join(export.streaming_content).decode()  # type: ignore[attr-defined]
    assert ",TX," in body and "Texas" not in body


def test_the_filters_compare_codes() -> None:
    a_store("In Texas", 1994, state="TX")
    a_store("In Georgia", 1995, state="GA")
    listed = html("/stores/?state=TX")
    assert "In Texas" in listed and "In Georgia" not in listed
    grid = html("/grid/?state=tx")
    assert "In Texas" in grid and "In Georgia" not in grid
    assert '<option value="TX" selected>Texas</option>' in grid


def test_state_name_and_state_text() -> None:
    assert (state_name("TX"), state_name("tx"), state_name("ZZ"), state_name(None)) == (
        "Texas",
        "",
        "",
        "",
    )
    assert (service.state_text("NY"), service.state_text(" @TX "), service.state_text("")) == (
        "New York",
        "@TX",
        "",
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Texas", "TX"),
        ("tx", "TX"),
        (" TX ", "TX"),
        ("TEXAS", "TX"),
        ("new  york", "NY"),
        ("District of Columbia", "DC"),
        ("Tejas", ""),
        ("T X", ""),
        ("", ""),
    ],
)
def test_the_migration_maps_a_code_or_a_full_name(raw: str, code: str) -> None:
    assert MIGRATION.state_code(raw) == code


def test_the_migration_mapping_writes_codes_and_audits_what_it_cannot_match() -> None:
    rows = {raw: a_store(f"Store {raw!r}", state=raw) for raw in ("Texas", "tx", " TX ", "Tejas")}
    kept = a_store("Already", state="GA")
    MIGRATION.map_states(django_apps, None)
    states = {raw: Store.objects.get(pk=store.pk).state for raw, store in rows.items()}
    assert states == {"Texas": "TX", "tx": "TX", " TX ": "TX", "Tejas": ""}
    assert Store.objects.get(pk=kept.pk).state == "GA"
    [entry] = AuditLog.objects.filter(action=MIGRATION.UNMAPPED_ACTION)
    assert (entry.entity_id, entry.before, entry.after) == (
        str(rows["Tejas"].pk),
        {"state": "Tejas"},
        {"state": ""},
    )
    MIGRATION.unmap_states(django_apps, None)
    assert Store.objects.get(pk=rows["tx"].pk).state == "Texas"
    assert Store.objects.get(pk=rows["Tejas"].pk).state == ""


def test_a_blank_state_needs_choosing_and_a_closed_store_does_not() -> None:
    store = a_store("No state", state="")
    Store.objects.create(external_label="Closed", status=StoreStatus.CLOSED)
    found = attention.attention(date(2026, 10, 25), limit=None)
    items = [item for item in found.items if item.kind == attention.STORE_STATE_MISSING]
    assert [(item.text, item.url) for item in items] == [
        ("State needs choosing: No state", f"/stores/{store.pk}/edit/")
    ]


# --- D12: landlord rent optional -----------------------------------------------------------


def test_an_empty_landlord_rent_saves_the_store_without_a_cost_row() -> None:
    admin, _ = client_for(Role.ADMIN)
    assert (
        admin.post(NEW, form_data(landlord_rent="", other_monthly_cost="50.00")).status_code == 302
    )
    store = Store.objects.get()
    assert not StoreCost.objects.exists()
    assert not AuditLog.objects.filter(entity_type="store_cost").exists()
    cost = html(f"/stores/{store.pk}/").split('id="store-cost"', 1)[1].split("</section>", 1)[0]
    assert '<dd id="landlord-rent">Not entered</dd>' in cost
    assert 'id="change-cost"' in cost


def test_a_zero_landlord_rent_saves_a_row_at_zero() -> None:
    admin, _ = client_for(Role.ADMIN)
    assert admin.post(NEW, form_data(landlord_rent="0.00")).status_code == 302
    [row] = StoreCost.objects.all()
    assert row.landlord_rent == Decimal("0.00")


def test_the_help_text_says_empty_and_zero_differ() -> None:
    assert StoreCreateForm().fields["landlord_rent"].help_text == (
        "Leave empty when the cost is not known yet; 0.00 means the store costs nothing."
    )


def test_the_pnl_and_the_cashflow_read_an_empty_landlord_rent_as_unknown() -> None:
    admin, _ = client_for(Role.ADMIN)
    admin.post(NEW, form_data(landlord_rent="", open_date="2026-01-01"))
    store = Store.objects.get()
    rent(a_lease(store, an_operator("Acme"), started=date(2026, 1, 1)), JULY)
    [row] = pnl.rows(pnl.PnlQuery(from_month=JULY, to_month=JULY))
    assert row.landlord_rent is None and pnl.LANDLORD_RENT in row.unknown
    page = admin.get("/reports/pnl/", {"from": "2026-07", "to": "2026-07"}).content.decode()
    assert "Unknown: landlord rent" in page
    chosen = dashboard.Range("custom", "Jul 2026", JULY, JULY)
    flow = dashboard.cashflow(chosen)
    assert flow.expenses is None and flow.stores_without_cost == 1
