"""Store screens, read-only admin, lease form debit days (docs/qa/PLAN-007b-stores.md §2
rows 1 to 5).

No store view reaches the payment provider; ``payments.get_provider`` is still replaced
by a :class:`~payments.fake.FakeProvider` where a page could, so a call would show up.
"""

from __future__ import annotations

import importlib
import re
import uuid
from typing import Any

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth.models import Group
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import NoReverseMatch, reverse

from accounts import registry
from accounts.roles import Role
from ledger.admin import LeaseAdmin, OperatorAdmin, ReadOnlyAdmin, StoreAdmin
from ledger.forms import LeaseCreateForm, StoreCreateForm
from ledger.models import AuditLog, Lease, Operator, Store, StoreStatus
from ledger.services import stores as service
from payments.fake import FakeProvider
from tests.auth_helpers import iter_routes, make_user, verified_client
from tests.test_lease_services import _race, make_lease, make_operator
from tests.test_lease_views import NEW as LEASE_NEW
from tests.test_lease_views import form_data as lease_form_data
from tests.test_operator_sync import requires_postgresql
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

LIST = "/stores/"
NEW = "/stores/new/"
POST_ROUTES = ("create", "edit")

migration = importlib.import_module("ledger.migrations.0008_admin_group_view_permissions")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def admin() -> Client:
    return client_for(Role.ADMIN, "admin-store-views")[0]


def form_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "creation_key": str(uuid.uuid4()),
        "store_number": "202",
        "external_label": "Store 0202",
        "address_line1": "",
        "city": "Springfield",
        "state": "IL",
        "postal_code": "",
        "open_date": "2025-03-01",
        "closed_at": "",
        "status": StoreStatus.ACTIVE,
        # PLAN-012e-2 D9: the New store form's first cost row (optional since PLAN-012f-b D12).
        "landlord_rent": "0.00",
        "landlord_lease_end": "",
        "other_monthly_cost": "",
        "other_cost_description": "",
    }
    return {**data, **overrides}


def edit_data(store: Store, **overrides: Any) -> dict[str, Any]:
    data = {
        "store_number": "" if store.store_number is None else str(store.store_number),
        "external_label": store.external_label,
        "address_line1": store.address_line1,
        "city": store.city,
        "state": store.state,
        "postal_code": store.postal_code,
        "open_date": store.open_date.isoformat() if store.open_date else "",
        "closed_at": store.closed_at.isoformat() if store.closed_at else "",
        "status": store.status,
    }
    return {**data, **overrides}


def make_store(**fields: Any) -> Store:
    values: dict[str, Any] = {"external_label": "Store 0303", "status": StoreStatus.ACTIVE}
    return Store.objects.create(**{**values, **fields})


def paths(store: Store) -> dict[str, str]:
    base = f"/stores/{store.pk}/"
    return {"list": LIST, "create": NEW, "detail": base, "edit": f"{base}edit/"}


def audit_rows(store: Store) -> list[AuditLog]:
    return list(
        AuditLog.objects.filter(entity_type="store", entity_id=str(store.pk)).order_by("id")
    )


# --- Row 1: create, edit, audit, open and closed dates, replay, PRG -------------------------------


def test_admin_creates_a_store_with_an_audit_row(admin: Client, fake: FakeProvider) -> None:
    data = form_data()
    response = admin.post(NEW, data)
    store = Store.objects.get()
    assert (response.status_code, response["Location"]) == (302, f"/stores/{store.pk}/")
    assert store.external_label == "Store 0202"
    assert store.store_number == 202
    assert store.open_date is not None and store.open_date.isoformat() == "2025-03-01"
    assert store.needs_open_date is False
    [row] = audit_rows(store)
    assert row.action == "store.created" and row.before is None
    assert row.after is not None
    assert row.after["creation_key"] == data["creation_key"]
    assert row.after["external_label"] == "Store 0202"
    assert row.actor is not None and row.actor.username == "admin-store-views"
    page = admin.get(response["Location"]).content.decode()
    # PLAN-012f-b Batch B: the owner's wording for a new store.
    assert (
        "Store #202 Store 0202 added." in page and "Springfield" in page and "Mar 1, 2025" in page
    )
    assert fake.calls == []


def test_a_replayed_creation_key_saves_once(admin: Client) -> None:
    data = form_data()
    first = admin.post(NEW, data)
    second = admin.post(NEW, {**data, "external_label": "Other name"})
    store = Store.objects.get()
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"] == f"/stores/{store.pk}/"
    assert store.external_label == "Store 0202"
    assert len(audit_rows(store)) == 1
    assert "already saved" in admin.get(second["Location"]).content.decode()


def test_the_service_returns_the_store_a_key_already_made() -> None:
    key = uuid.uuid4()
    fields = {"external_label": "Store 0404", "open_date": None, "status": StoreStatus.ACTIVE}
    store, created = service.create_store(fields, creation_key=key, actor=None)
    again, created_again = service.create_store(
        {**fields, "external_label": "Changed"}, creation_key=key, actor=None
    )
    assert (created, created_again) == (True, False)
    assert again.pk == store.pk and Store.objects.count() == 1
    assert service.store_for_creation_key(str(key)) == store
    assert service.store_for_creation_key("not-a-key") is None
    assert service.store_for_creation_key(None) is None


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_submissions_of_one_form_make_one_store() -> None:
    key = uuid.uuid4()
    fields = {"external_label": "Race", "open_date": None, "status": StoreStatus.ACTIVE}
    results = _race(
        lambda: service.create_store(fields, creation_key=key, actor=None),
        lambda: service.create_store(fields, creation_key=key, actor=None),
    )
    assert sorted(created for _, created in results) == [False, True]
    assert Store.objects.count() == 1
    assert AuditLog.objects.filter(action="store.created").count() == 1


def test_admin_edits_a_store_with_a_before_and_after_audit_row(admin: Client) -> None:
    store = make_store(open_date=None)
    assert store.needs_open_date is True
    route = paths(store)
    response = admin.post(
        route["edit"], edit_data(store, city="Shelbyville", open_date="2024-05-01")
    )
    assert (response.status_code, response["Location"]) == (302, route["detail"])
    store = Store.objects.get(pk=store.pk)
    assert store.city == "Shelbyville"
    assert store.needs_open_date is False
    [row] = audit_rows(store)
    assert row.action == "store.updated"
    assert row.before is not None and row.after is not None
    assert (row.before["city"], row.after["city"]) == ("", "Shelbyville")
    assert (row.before["open_date"], row.after["open_date"]) == (None, "2024-05-01")
    assert (row.before["needs_open_date"], row.after["needs_open_date"]) == (True, False)


def test_an_edit_that_changes_nothing_writes_no_audit_row(admin: Client) -> None:
    store = make_store(open_date="2024-05-01")
    store.refresh_from_db()
    response = admin.post(paths(store)["edit"], edit_data(store))
    assert response.status_code == 302
    assert audit_rows(store) == []


@pytest.mark.parametrize(
    ("overrides", "text"),
    [
        ({"open_date": ""}, "Enter the open date."),
        ({"closed_at": "2025-02-28"}, "The closed date cannot be before the open date."),
        ({"external_label": "   "}, "This field is required."),
        ({"external_label": ""}, "This field is required."),
        ({"status": "demolished"}, "Select a valid choice."),
        ({"store_number": "abc"}, "Enter a whole number."),
        ({"store_number": "0"}, "greater than or equal to 1"),
        ({"open_date": "2025-02-30"}, "Enter a valid date."),
    ],
)
def test_an_invalid_store_is_held_and_shown_once_after_a_redirect(
    admin: Client, overrides: dict[str, str], text: str
) -> None:
    response = admin.post(NEW, form_data(**overrides))
    assert (response.status_code, response["Location"]) == (302, NEW)
    assert Store.objects.count() == 0
    assert not AuditLog.objects.filter(entity_type="store").exists()
    assert text in admin.get(NEW).content.decode()
    assert text not in admin.get(NEW).content.decode()


def test_an_invalid_edit_is_held_and_changes_nothing(admin: Client) -> None:
    store = make_store(open_date="2025-03-01")
    store.refresh_from_db()
    route = paths(store)
    response = admin.post(route["edit"], edit_data(store, open_date="", city="Nowhere"))
    assert (response.status_code, response["Location"]) == (302, route["edit"])
    page = admin.get(route["edit"]).content.decode()
    assert "Enter the open date." in page and 'value="Nowhere"' in page
    store.refresh_from_db()
    assert store.city == "" and store.open_date is not None
    assert audit_rows(store) == []


def test_the_open_date_is_required_on_the_form_only() -> None:
    assert StoreCreateForm().fields["open_date"].required is True
    assert StoreCreateForm(data=form_data(open_date="")).is_valid() is False
    # The model keeps the column nullable for the import, and flags the row.
    store = Store.objects.create(external_label="Imported", status=StoreStatus.ACTIVE)
    assert store.open_date is None and store.needs_open_date is True


def test_a_closed_date_equal_to_the_open_date_is_accepted(admin: Client) -> None:
    response = admin.post(NEW, form_data(closed_at="2025-03-01", status=StoreStatus.CLOSED))
    assert response.status_code == 302
    store = Store.objects.get()
    assert store.closed_at == store.open_date


def test_a_closed_store_is_saved_and_listed_with_its_status(admin: Client) -> None:
    admin.post(
        NEW,
        form_data(external_label="Closed One", closed_at="2026-06-30", status=StoreStatus.CLOSED),
    )
    store = Store.objects.get()
    assert store.status == StoreStatus.CLOSED
    assert store.closed_at is not None and store.closed_at.isoformat() == "2026-06-30"
    page = admin.get(LIST).content.decode()
    # PLAN-012f-a D3: the Status column is a badge (a closed store in the unpaid family's red).
    status = re.search(r'<td class="store-status"[^>]*>(.*?)</td>', page, re.S)
    assert "Closed One" in page and status is not None
    assert 'class="badge badge-unpaid"' in status.group(1) and "Closed" in status.group(1)
    detail = admin.get(paths(store)["detail"]).content.decode()
    assert "Jun 30, 2026" in detail and "Closed" in detail


def test_every_post_ends_in_a_redirect(admin: Client) -> None:
    store = make_store(open_date="2025-03-01")
    store.refresh_from_db()
    route = paths(store)
    posts = [
        (NEW, form_data()),
        (NEW, form_data(open_date="")),
        (route["edit"], edit_data(store, city="X")),
        (route["edit"], edit_data(store, closed_at="2000-01-01")),
    ]
    for path, data in posts:
        response = admin.post(path, data)
        assert response.status_code == 302, (path, data)


def test_no_delete_route_exists() -> None:
    for name in ("delete", "remove", "archive"):
        with pytest.raises(NoReverseMatch):
            reverse(f"ledger:stores:{name}", kwargs={"pk": uuid.uuid4()})


def test_a_malformed_or_unknown_store_id_is_404(admin: Client) -> None:
    for path in (f"/stores/{uuid.uuid4()}/", "/stores/not-a-uuid/", "/stores/1/edit/"):
        assert admin.get(path).status_code == 404, path


# --- Row 2: list search and paging; detail shows the store's leases -------------------------------


def test_list_search_by_label_and_city_and_paging(admin: Client) -> None:
    for index in range(30):
        make_store(external_label=f"Store {index:02d}", city="Springfield" if index else "Capital")
    first = admin.get(LIST).content.decode()
    assert '<span class="pager-count">1–25 of 30</span>' in first
    assert "Store 00" in first and "Store 29" not in first
    second = admin.get(f"{LIST}?page=2").content.decode()
    assert "Store 29" in second
    for nonsense in ("99", "-1", "abc", "1.5", ""):
        assert admin.get(LIST, {"page": nonsense}).status_code == 200, nonsense
    by_label = admin.get(LIST, {"q": "store 07"}).content.decode()
    assert "Store 07" in by_label and "Store 08" not in by_label
    by_city = admin.get(LIST, {"q": "capital"}).content.decode()
    assert "Store 00" in by_city and "Store 01" not in by_city
    for hostile in ("' OR 1=1 --", "{{ settings.SECRET_KEY }}", "%", "_", "x" * 5000, "a\x00b"):
        response = admin.get(LIST, {"q": hostile})
        assert response.status_code == 200
        assert "No stores match." in response.content.decode()


def test_paging_keeps_the_search(admin: Client) -> None:
    for index in range(26):
        make_store(external_label=f"Match {index:02d}", city="Alphaville")
    make_store(external_label="Other", city="Betatown")
    page = admin.get(LIST, {"q": "Alphaville"}).content.decode()
    assert '<span class="pager-count">1–25 of 26 · filtered from 27</span>' in page
    assert "?q=Alphaville&amp;page=2" in page


def test_the_detail_shows_the_leases_of_the_store(admin: Client) -> None:
    lease = make_lease(operator=make_operator(email="d@example.com", customer=None))
    other = make_lease()
    page = admin.get(f"/stores/{lease.store_id}/").content.decode()
    assert f"/leases/{lease.pk}/" in page
    assert f"/leases/{other.pk}/" not in page
    assert "Northwind Stores" in page and "Jan 1, 2026" in page
    empty = admin.get(f"/stores/{make_store().pk}/").content.decode()
    assert "No lease yet. New lease from the action bar." in empty


# --- Row 3: roles, CSRF, registry ----------------------------------------------------------------


def test_every_store_route_is_classified_with_the_planned_roles() -> None:
    names = {name for name, _, _ in iter_routes() if name and name.startswith("ledger:stores:")}
    # PLAN-010b D8, D12: setting the landlord rent needs billing. PLAN-010c D10:
    # correcting a cost row and closing the open one need admin.
    corrections = ("correct_cost", "close_cost")
    # PLAN-012 D3: the store's contact log (any role) and adding an entry (billing).
    contact_log = ("contacts", "contacts_add")
    # PLAN-012e D4, D6: End lease and Transfer start their wizards (admin).
    lease_end = ("end_lease", "transfer")
    # PLAN-012d D2: the action bar's Record a payment (billing) and Charge now (admin).
    workspace_bar = ("record_payment", "charge_now_pick")
    # PLAN-012d Batch B: the action bar's Schedule a payment (billing). Batch D (D13):
    # Create missing charges (billing, POST only).
    schedule = ("schedule_payment", "create_charges")
    # PLAN-012e-2 D4: Charge now and Schedule a payment moved into the invoice builder;
    # their routes are read-only redirects there.
    redirects = ("charge_now_pick", "schedule_payment")
    # PLAN-012f-a Batch E (ADR-0014): a repeating debit plan's Pause and Resume (billing),
    # Stop and its end rule (admin); all POST, Stop asks first on its GET (E-4).
    plans = ("plan_pause", "plan_resume", "plan_stop", "plan_end")
    assert names == {
        f"ledger:stores:{short}"
        for short in (
            "list",
            "detail",
            "export",
            "set_cost",
            *POST_ROUTES,
            *corrections,
            *contact_log,
            *lease_end,
            *workspace_bar,
            *schedule,
            *plans,
        )
    }
    for short in ("plan_pause", "plan_resume"):
        rule = registry.rule_for(f"ledger:stores:{short}")
        assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    for short in ("plan_stop", "plan_end"):
        rule = registry.rule_for(f"ledger:stores:{short}")
        assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    for short in redirects:
        assert f"ledger:stores:{short}" in registry.READ_ONLY_ROUTE_NAMES
        assert registry.rule_for(f"ledger:stores:{short}") is None
    for short in ("record_payment", "create_charges"):
        rule = registry.rule_for(f"ledger:stores:{short}")
        assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    assert {
        "ledger:stores:list",
        "ledger:stores:detail",
        "ledger:stores:export",
        "ledger:stores:contacts",
    } <= registry.READ_ONLY_ROUTE_NAMES
    rule = registry.rule_for("ledger:stores:contacts_add")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}
    for short in (*POST_ROUTES, *corrections, *lease_end):
        rule = registry.rule_for(f"ledger:stores:{short}")
        assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}
    rule = registry.rule_for("ledger:stores:set_cost")
    assert rule is not None and rule.min_role is Role.BILLING and rule.methods == {"POST"}


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_billing_and_viewer_read_and_change_nothing(role: Role) -> None:
    client, _ = client_for(role)
    store = make_store(open_date="2025-03-01")
    store.refresh_from_db()
    route = paths(store)
    for name in ("list", "detail"):
        assert client.get(route[name]).status_code == 200, name
    for name in POST_ROUTES:
        assert client.get(route[name]).status_code == 403, name
    assert client.post(route["create"], form_data()).status_code == 403
    assert client.post(route["edit"], edit_data(store, city="Hacked")).status_code == 403
    store.refresh_from_db()
    assert Store.objects.count() == 1 and store.city == ""
    assert not AuditLog.objects.filter(entity_type="store").exists()
    assert "New store" not in client.get(LIST).content.decode()
    assert f"/stores/{store.pk}/edit/" not in client.get(route["detail"]).content.decode()


def test_admin_sees_the_controls() -> None:
    client, _ = client_for(Role.ADMIN)
    store = make_store()
    assert "New store" in client.get(LIST).content.decode()
    assert f"/stores/{store.pk}/edit/" in client.get(paths(store)["detail"]).content.decode()
    assert client.get(NEW).status_code == 200
    assert client.get(paths(store)["edit"]).status_code == 200


def test_anonymous_gets_401_and_no_role_gets_403() -> None:
    store = make_store()
    route = paths(store)
    for name in ("list", "detail", "create", "edit"):
        assert Client().get(route[name]).status_code == 401, name
    for name in POST_ROUTES:
        assert Client().post(route[name], form_data()).status_code == 401, name
    nobody, _ = client_for(None)
    for name in ("list", "detail", "create", "edit"):
        assert nobody.get(route[name]).status_code == 403, name
    assert Store.objects.count() == 1


def test_every_post_route_enforces_csrf() -> None:
    user, device = make_user("csrf-admin-stores", Role.ADMIN)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    store = make_store(open_date="2025-03-01")
    store.refresh_from_db()
    assert client.post(NEW, form_data()).status_code == 403
    assert client.post(paths(store)["edit"], edit_data(store, city="X")).status_code == 403
    assert Store.objects.count() == 1
    assert not AuditLog.objects.filter(entity_type="store").exists()


def test_the_nav_links_to_the_store_list(admin: Client) -> None:
    page = admin.get(LIST).content.decode()
    # PLAN-012f D3: the sidebar item, with its icon.
    assert re.search(
        r'<a class="nav-item" href="/stores/" id="nav-stores" aria-current="page">'
        r"<svg [^>]*>.*?</svg><span>Stores</span></a>",
        page,
    )


# --- Row 4: the admin lists stores, operators and leases read-only --------------------------------


#: Models whose permissions ledger migration 0013 gives the admin group (PLAN-010b D12).
#: Models whose admin permissions later migrations grant (PLAN-010b ledger 0013; PLAN-011b
#: ledger 0024: the collections ladder's steps and templates).
#: PLAN-012c D2: ledger 0045, the company settings row.
PLAN_010B_ADMIN_MODELS = (
    "feetype",
    "insurancepremium",
    "collectionstep",
    "messagetemplate",
    "companysettings",
    # PLAN-012f-b D10: ledger 0051 gives the admin group view, add and change on store types.
    "storetype",
)


def _grant_view_permissions() -> None:
    """Run the data migration against its own historical state (transactional tests
    elsewhere flush the rows a migration made)."""
    state = MigrationExecutor(connection).loader.project_state(
        ("ledger", "0008_admin_group_view_permissions")
    )
    migration.grant_view_permissions(state.apps, None)


@pytest.mark.parametrize(
    ("model", "admin_class"),
    [(Store, StoreAdmin), (Operator, OperatorAdmin), (Lease, LeaseAdmin)],
)
def test_the_three_models_are_registered_read_only(model: type, admin_class: type) -> None:
    model_admin = django_admin.site._registry[model]
    assert isinstance(model_admin, admin_class)
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = Client().request().wsgi_request
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_the_migration_gives_only_the_admin_group_the_three_view_permissions() -> None:
    for name in ("admin", "billing", "viewer"):
        Group.objects.get_or_create(name=name)
    _grant_view_permissions()
    _grant_view_permissions()  # idempotent
    ledger = set(
        Group.objects.get(name="admin")
        .permissions.filter(content_type__app_label="ledger")
        .exclude(content_type__model__in=PLAN_010B_ADMIN_MODELS)
        .values_list("codename", flat=True)
    )
    assert ledger == {"view_store", "view_operator", "view_lease"}
    for name in ("billing", "viewer"):
        assert (
            not Group.objects.get(name=name)
            .permissions.filter(content_type__app_label="ledger")
            .exists()
        )


def test_the_migration_reverse_removes_exactly_the_three_permissions() -> None:
    Group.objects.get_or_create(name="admin")
    _grant_view_permissions()
    group = Group.objects.get(name="admin")
    before = group.permissions.count()
    state = MigrationExecutor(connection).loader.project_state(
        ("ledger", "0008_admin_group_view_permissions")
    )
    migration.revoke_view_permissions(state.apps, None)
    assert group.permissions.count() == before - 3
    assert (
        not group.permissions.filter(content_type__app_label="ledger")
        .exclude(content_type__model__in=PLAN_010B_ADMIN_MODELS)
        .exists()
    )


def test_an_admin_group_user_who_is_not_a_superuser_opens_the_three_lists() -> None:
    _grant_view_permissions()
    user, device = make_user("admin-not-super", Role.ADMIN, superuser=False)
    assert device is not None and not user.is_superuser
    client = verified_client(user, device)
    lease = make_lease(operator=make_operator(email="adm@example.com", customer=None))
    assert lease.operator is not None
    for model, row in (("store", lease.store), ("operator", lease.operator), ("lease", lease)):
        changelist = client.get(f"/admin/ledger/{model}/")
        assert changelist.status_code == 200, model
        assert f"/admin/ledger/{model}/add/" not in changelist.content.decode(), model
        assert client.get(f"/admin/ledger/{model}/{row.pk}/change/").status_code == 200, model
        assert client.get(f"/admin/ledger/{model}/add/").status_code == 403, model
        delete = client.post(f"/admin/ledger/{model}/{row.pk}/delete/", {"post": "yes"})
        assert delete.status_code == 403, model
    index = client.get("/admin/").content.decode()
    for title in ("Stores", "Operators", "Leases"):
        assert title in index
    assert Store.objects.filter(pk=lease.store_id).exists()
    assert Lease.objects.filter(pk=lease.pk).exists()


def test_the_admin_change_page_refuses_a_post() -> None:
    _grant_view_permissions()
    client, _ = client_for(Role.ADMIN)
    store = make_store()
    response = client.post(
        f"/admin/ledger/store/{store.pk}/change/", {"external_label": "Changed", "status": "active"}
    )
    assert response.status_code == 403
    store.refresh_from_db()
    assert store.external_label == "Store 0303"


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_billing_and_viewer_cannot_open_the_ledger_admin(role: Role) -> None:
    _grant_view_permissions()
    client, _ = client_for(role)
    for model in ("store", "operator", "lease"):
        assert client.get(f"/admin/ledger/{model}/").status_code == 403, model


@pytest.mark.parametrize("prefix", ["admin:ledger_store_", "admin:ledger_lease_"])
def test_the_new_admin_routes_need_admin_for_a_post(prefix: str) -> None:
    rule = registry.ADMIN_MODEL_PREFIX_RULES[prefix]
    assert rule.min_role is Role.ADMIN and rule.methods == {"POST"}


# --- Row 5: lease form debit days ----------------------------------------------------------------


def _input(page: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', page)
    assert match is not None, name
    return match.group(0)


def test_the_second_debit_day_renders_empty_by_default(admin: Client) -> None:
    field = _input(admin.get(LEASE_NEW).content.decode(), "second_debit_day")
    assert "value=" not in field
    assert "required" not in field
    lease = make_lease()
    edit_field = _input(admin.get(f"/leases/{lease.pk}/edit/").content.decode(), "second_debit_day")
    assert "value=" not in edit_field
    form = LeaseCreateForm()
    assert form.fields["second_debit_day"].required is False
    assert form["second_debit_day"].value() is None


@pytest.mark.parametrize(
    ("overrides", "text"),
    [
        ({"debit_day": "15", "second_debit_day": "15"}, "must differ from the first"),
        (
            {"debit_day": "", "second_debit_day": "20", "collection_status": "prospective"},
            "Set the first debit day first.",
        ),
    ],
)
def test_the_second_debit_day_refusals(admin: Client, overrides: dict[str, str], text: str) -> None:
    response = admin.post(LEASE_NEW, lease_form_data(**overrides))
    assert (response.status_code, response["Location"]) == (302, LEASE_NEW)
    assert Lease.objects.count() == 0
    assert text in admin.get(LEASE_NEW).content.decode()


def test_a_valid_pair_of_debit_days_is_accepted(admin: Client) -> None:
    response = admin.post(LEASE_NEW, lease_form_data(debit_day="1", second_debit_day="15"))
    lease = Lease.objects.get()
    assert (response.status_code, response["Location"]) == (302, f"/leases/{lease.pk}/")
    assert (lease.debit_day, lease.second_debit_day) == (1, 15)


def test_an_empty_second_debit_day_is_saved_as_none(admin: Client) -> None:
    admin.post(LEASE_NEW, lease_form_data(debit_day="1", second_debit_day=""))
    assert Lease.objects.get().second_debit_day is None
