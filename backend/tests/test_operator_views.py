"""The operator screens (docs/qa/PLAN-006-operators.md §6 rows 1, 2, 4, 5, 6 and 7).

``payments.get_provider`` is replaced by a :class:`~payments.fake.FakeProvider` for
every test here, so the views run exactly as deployed but no request leaves.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import Client
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.admin import OperatorAdmin
from ledger.models import EnrolmentStatus, Operator, SyncStatus
from ledger.views.operators import HELD_FORM_KEY
from payments import operator_sync
from payments.errors import CircuitOpen, ProviderRejected, ProviderUnavailable
from payments.fake import FakeProvider
from payments.provider import CustomerQuery, CustomerRecord
from tests.auth_helpers import iter_routes, make_user, verified_client
from tests.test_operator_sync import (
    add_provider_customer,
    call_names,
    customer_refs,
    fields,
    requires_postgresql,
)

pytestmark = pytest.mark.django_db

LIST = "/operators/"
NEW = "/operators/new/"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def client_for(role: Role | None, name: str = "") -> tuple[Client, User]:
    user, device = make_user(name or f"user-{uuid.uuid4().hex[:8]}", role)
    assert device is not None
    return verified_client(user, device), user


@pytest.fixture
def billing() -> Client:
    return client_for(Role.BILLING, "billing-views")[0]


def form_data(**overrides: Any) -> dict[str, Any]:
    data = {name: ("" if value is None else value) for name, value in fields().items()}
    data["creation_key"] = str(uuid.uuid4())
    return {**data, **overrides}


def detail(operator: Operator) -> str:
    return f"/operators/{operator.pk}/"


def backdate_attempt(operator: Operator, seconds: int = operator_sync.RETRY_LOCK_SECONDS) -> None:
    """Move the last sync attempt into the past, as if the retry lock had passed."""
    Operator.objects.filter(pk=operator.pk).update(
        sync_attempted_at=timezone.now() - timedelta(seconds=seconds)
    )


def new_operator(**overrides: Any) -> Operator:
    operator, _ = operator_sync.create_operator(
        fields(**overrides), creation_key=uuid.uuid4(), actor=None
    )
    return operator


# --- Row 1: double submission ---------------------------------------------------------------


def test_two_posts_with_one_creation_key_make_one_operator_and_one_customer(
    billing: Client, fake: FakeProvider
) -> None:
    data = form_data()
    first = billing.post(NEW, data)
    second = billing.post(NEW, {**data, "display_name": "Different on replay"})

    operator = Operator.objects.get()
    assert first.status_code == second.status_code == 302
    assert first["Location"] == second["Location"] == detail(operator)
    assert operator.display_name == "Northwind Stores"
    assert call_names(fake).count("create_customer") == 1
    assert len(customer_refs(operator)) == 1
    assert operator.sync_status == SyncStatus.LINKED


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_posts_with_one_creation_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowFake(FakeProvider):
        def find_customers(self, query: CustomerQuery) -> list[CustomerRecord]:
            threading.Event().wait(0.3)
            return super().find_customers(query)

    provider = SlowFake()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    clients = [client_for(Role.BILLING)[0] for _ in range(2)]
    data = form_data()
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def post(client: Client) -> None:
        try:
            barrier.wait()
            statuses.append(client.post(NEW, data).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=post, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [302, 302]
    assert Operator.objects.count() == 1
    assert call_names(provider).count("create_customer") == 1
    assert len(customer_refs(Operator.objects.get())) == 1


def test_the_create_form_carries_a_fresh_creation_key_and_csrf(billing: Client) -> None:
    keys = set()
    for _ in range(2):
        page = billing.get(NEW).content.decode()
        assert 'name="csrfmiddlewaretoken"' in page
        assert 'type="email"' in page
        assert 'id="id_display_name"' in page and "required" in page
        start = page.index('name="creation_key" value="') + len('name="creation_key" value="')
        keys.add(uuid.UUID(page[start : start + 36]))
    assert len(keys) == 2


def test_forms_enforce_csrf(fake: FakeProvider) -> None:
    user, device = make_user("csrf-billing", Role.BILLING)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    operator = new_operator(email=None)
    for path in (
        NEW,
        f"{detail(operator)}edit/",
        f"{detail(operator)}link/",
        f"{detail(operator)}retry-sync/",
    ):
        assert client.post(path, form_data()).status_code == 403, path
    assert Operator.objects.count() == 1
    assert fake.calls == []


# --- Row 2: failure shown, retry on click only ------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [ProviderUnavailable(503), ProviderRejected(422, "CANARY-provider-text"), CircuitOpen("x")],
    ids=["unavailable", "rejected", "circuit-open"],
)
def test_a_failure_is_shown_with_a_retry_button_and_retry_calls_once(
    billing: Client, fake: FakeProvider, error: Exception
) -> None:
    assert isinstance(error, ProviderUnavailable | ProviderRejected | CircuitOpen)
    fake.fail_next("find_customers", error)
    response = billing.post(NEW, form_data(), follow=True)

    operator = Operator.objects.get()
    name = type(error).__name__
    assert (operator.sync_status, operator.sync_failure) == (SyncStatus.FAILED, name)
    page = response.content.decode()
    assert operator_sync.MESSAGES[name].replace("'", "&#x27;") in page
    assert name in page
    # PLAN-006a §3.3: the retry button waits a minute after the attempt.
    assert "Retry sync" not in page
    assert "Retry available in " in page
    assert "CANARY" not in page
    assert len(fake.calls) == 1

    # Viewing and editing do not call again.
    billing.get(detail(operator))
    billing.post(f"{detail(operator)}edit/", form_data(phone="+1 555 010 0009"))
    assert len(fake.calls) == 1

    backdate_attempt(operator)
    assert "Retry sync" in billing.get(detail(operator)).content.decode()

    retried = billing.post(f"{detail(operator)}retry-sync/", follow=True)
    assert retried.status_code == 200
    assert call_names(fake) == ["find_customers", "find_customers", "create_customer"]
    operator.refresh_from_db()
    assert operator.sync_status == SyncStatus.LINKED
    assert "Retry sync" not in retried.content.decode()


# --- Row 4: malformed e-mail ------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "a@b", "a@@example.com", "x y@example.com", "ops@example..com", "@x.com"],
)
def test_a_malformed_email_is_refused_with_no_record_and_no_call(
    billing: Client, fake: FakeProvider, email: str
) -> None:
    response = billing.post(NEW, form_data(email=email), follow=True)
    assert response.redirect_chain == [(NEW, 302)]
    assert "Enter a valid email address." in response.content.decode()
    assert Operator.objects.count() == 0
    assert fake.calls == []


def test_the_email_is_stored_lower_case_and_stripped(billing: Client, fake: FakeProvider) -> None:
    billing.post(NEW, form_data(email="  Ops@Example.COM "))
    assert Operator.objects.get().email == "ops@example.com"


def test_a_duplicate_email_is_a_form_error(billing: Client, fake: FakeProvider) -> None:
    new_operator()
    response = billing.post(NEW, form_data(email="OPS@example.com"), follow=True)
    assert response.redirect_chain == [(NEW, 302)]
    assert 'class="errorlist' in response.content.decode()
    assert Operator.objects.count() == 1
    assert fake.calls == []


def test_a_card_number_in_a_text_field_is_refused(billing: Client, fake: FakeProvider) -> None:
    response = billing.post(NEW, form_data(notes="card 4111 1111 1111 1111"), follow=True)
    assert response.redirect_chain == [(NEW, 302)]
    assert "4111" not in response.content.decode()
    assert "card or account numbers" in response.content.decode()
    assert Operator.objects.count() == 0
    assert fake.calls == []


# --- Row 5: no e-mail, manual link ------------------------------------------------------------


def test_no_email_saves_awaiting_link_with_no_call(billing: Client, fake: FakeProvider) -> None:
    response = billing.post(NEW, form_data(email=""), follow=True)
    operator = Operator.objects.get()
    assert operator.email is None
    assert operator.sync_status == SyncStatus.AWAITING_LINK
    assert fake.calls == []
    assert "link the payment provider customer by hand" in response.content.decode()


def test_the_link_form_stores_both_ids_after_one_read(billing: Client, fake: FakeProvider) -> None:
    add_provider_customer(fake, "cus_manual_0001", "")
    operator = new_operator(email=None)

    response = billing.post(
        f"{detail(operator)}link/",
        {
            "customer_id": "cus_manual_0001",
            "payment_method_id": "pm_bank-0001",
            # Not a field of the form: ignored, never stored.
            "routing_number": "021000021",
            "account_number": "000123456789",
        },
        follow=True,
    )

    assert response.status_code == 200
    assert fake.calls == [("get_customer", ("cus_manual_0001",))]
    assert operator_sync.current_refs(operator) == {
        "customer": "cus_manual_0001",
        "payment_method": "pm_bank-0001",
    }
    operator.refresh_from_db()
    assert operator.enrolment_status == EnrolmentStatus.ENROLLED
    page = response.content.decode()
    assert "cus_…0001" in page and "pm_b…0001" in page
    assert "cus_manual_0001" not in page and "pm_bank-0001" not in page
    assert "021000021" not in page and "000123456789" not in page


def test_a_bad_customer_id_shows_a_clear_message_and_stores_nothing(
    billing: Client, fake: FakeProvider
) -> None:
    operator = new_operator(email=None)
    response = billing.post(
        f"{detail(operator)}link/",
        {"customer_id": "cus_missing", "payment_method_id": "pm_1"},
        follow=True,
    )
    assert response.redirect_chain == [(f"{detail(operator)}link/", 302)]
    assert operator_sync.LINK_REJECTED_MESSAGE in response.content.decode()
    assert call_names(fake) == ["get_customer"]
    assert operator_sync.current_refs(operator) == {}


@pytest.mark.parametrize(
    "value", ["cus 1", "cus;1", "cus_1'--", "{{ 7*7 }}", "x" * 256, "cüs_1", "cus/../1"]
)
def test_the_link_form_refuses_anything_but_an_identifier(
    billing: Client, fake: FakeProvider, value: str
) -> None:
    operator = new_operator(email=None)
    response = billing.post(
        f"{detail(operator)}link/", {"customer_id": value, "payment_method_id": "pm_1"}, follow=True
    )
    assert response.redirect_chain == [(f"{detail(operator)}link/", 302)]
    assert 'class="errorlist' in response.content.decode()
    assert fake.calls == []
    assert operator_sync.current_refs(operator) == {}


# --- Row 6: roles -------------------------------------------------------------------------------


def _paths(operator: Operator) -> dict[str, str]:
    base = detail(operator)
    return {
        "list": LIST,
        "create": NEW,
        "detail": base,
        "edit": f"{base}edit/",
        "archive": f"{base}archive/",
        "retry_sync": f"{base}retry-sync/",
        "link": f"{base}link/",
    }


GET_ROUTES = ("list", "create", "detail", "edit", "link")
POST_ROUTES = ("create", "edit", "archive", "retry_sync", "link")


def test_viewer_reads_everything_and_writes_nothing(fake: FakeProvider) -> None:
    client, _ = client_for(Role.VIEWER)
    operator = new_operator(email=None)
    paths = _paths(operator)
    for name in GET_ROUTES:
        assert client.get(paths[name]).status_code == 200, name
    for name in POST_ROUTES:
        response = client.post(paths[name], form_data())
        assert response.status_code == 403, name
        assert "Your role does not allow this action." in response.content.decode()
    assert Operator.objects.count() == 1
    operator.refresh_from_db()
    assert operator.archived_at is None
    assert fake.calls == []


def test_viewer_sees_no_write_controls(fake: FakeProvider) -> None:
    client, _ = client_for(Role.VIEWER)
    operator = new_operator(email=None)
    assert "New operator" not in client.get(LIST).content.decode()
    page = client.get(detail(operator)).content.decode()
    for control in ("Edit</a>", "Link provider IDs", ">Archive<", "Retry sync"):
        assert control not in page


def test_billing_cannot_archive_and_admin_can(fake: FakeProvider) -> None:
    operator = new_operator(email=None)
    billing, _ = client_for(Role.BILLING)
    assert billing.post(f"{detail(operator)}archive/").status_code == 403
    operator.refresh_from_db()
    assert operator.archived_at is None

    admin_client, _ = client_for(Role.ADMIN)
    response = admin_client.post(f"{detail(operator)}archive/")
    assert response.status_code == 302
    operator.refresh_from_db()
    assert operator.archived_at is not None


def test_billing_can_create_edit_retry_and_link(fake: FakeProvider) -> None:
    client, _ = client_for(Role.BILLING)
    assert client.post(NEW, form_data(email="")).status_code == 302
    operator = Operator.objects.get()
    paths = _paths(operator)
    assert client.post(paths["edit"], form_data(email="", city="Elsewhere")).status_code == 302
    assert client.post(paths["retry_sync"]).status_code == 302
    add_provider_customer(fake, "cus_1", "")
    response = client.post(paths["link"], {"customer_id": "cus_1", "payment_method_id": "pm_1"})
    assert response.status_code == 302


def test_anonymous_gets_401_everywhere() -> None:
    client = Client()
    operator = new_operator(email=None)
    paths = _paths(operator)
    for name in GET_ROUTES:
        assert client.get(paths[name]).status_code == 401, name
    for name in POST_ROUTES:
        assert client.post(paths[name]).status_code == 401, name


def test_a_user_without_a_role_is_refused_even_to_read(fake: FakeProvider) -> None:
    client, _ = client_for(None)
    operator = new_operator(email=None)
    for name in ("list", "detail", "create", "edit", "link"):
        assert client.get(_paths(operator)[name]).status_code == 403, name


def test_every_operator_route_is_classified_with_the_planned_roles() -> None:
    # PLAN-007 adds lease routes under ledger:leases:, classified in test_lease_views.py.
    names = {name for name, _, _ in iter_routes() if name and name.startswith("ledger:operators:")}
    assert names == {
        "ledger:operators:list",
        "ledger:operators:create",
        "ledger:operators:detail",
        "ledger:operators:edit",
        "ledger:operators:archive",
        "ledger:operators:retry_sync",
        "ledger:operators:link",
    }
    assert {"ledger:operators:list", "ledger:operators:detail"} <= registry.READ_ONLY_ROUTE_NAMES
    expected = {
        "create": Role.BILLING,
        "edit": Role.BILLING,
        "retry_sync": Role.BILLING,
        "link": Role.BILLING,
        "archive": Role.ADMIN,
    }
    for short, role in expected.items():
        rule = registry.rule_for(f"ledger:operators:{short}")
        assert rule is not None and rule.min_role is role and rule.methods == {"POST"}


def test_an_unknown_or_malformed_operator_id_is_404(billing: Client) -> None:
    for path in (f"/operators/{uuid.uuid4()}/", "/operators/not-a-uuid/", "/operators/1/edit/"):
        assert billing.get(path).status_code == 404, path


# --- Row 7: archive ---------------------------------------------------------------------------


def test_archive_hides_the_operator_unless_asked(fake: FakeProvider) -> None:
    kept = new_operator(display_name="Kept Operator", email="kept@example.com")
    gone = new_operator(display_name="Gone Operator", email="gone@example.com")
    admin_client, _ = client_for(Role.ADMIN)
    admin_client.post(f"{detail(gone)}archive/")

    gone.refresh_from_db()
    assert gone.archived_at is not None
    assert Operator.objects.filter(pk=gone.pk).exists()  # soft delete only
    listing = admin_client.get(LIST).content.decode()
    assert "Kept Operator" in listing and "Gone Operator" not in listing
    with_archived = admin_client.get(f"{LIST}?archived=1").content.decode()
    assert "Kept Operator" in with_archived and "Gone Operator" in with_archived
    assert kept.archived_at is None


def test_an_archived_operator_cannot_be_edited_retried_or_linked(fake: FakeProvider) -> None:
    operator = new_operator(email=None)
    operator_sync.archive_operator(operator.pk, actor=None)
    client, _ = client_for(Role.BILLING)
    paths = _paths(operator)
    for name in ("edit", "retry_sync", "link"):
        response = client.post(paths[name], form_data(city="Changed"))
        assert response.status_code == 302, name
        assert response["Location"] == detail(operator)
    operator.refresh_from_db()
    assert operator.city == "Testville"
    assert fake.calls == []


def test_no_delete_route_exists() -> None:
    for name in ("delete", "remove", "destroy"):
        with pytest.raises(NoReverseMatch):
            reverse(f"ledger:operators:{name}", kwargs={"pk": uuid.uuid4()})
    assert not any(
        "delete" in raw for name, raw, _ in iter_routes() if (name or "").startswith("ledger:")
    )


def test_the_admin_lists_operators_read_only(fake: FakeProvider) -> None:
    model_admin = admin.site._registry[Operator]
    assert isinstance(model_admin, OperatorAdmin)
    request = Client().request().wsgi_request
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False

    operator = new_operator(email=None)
    admin_client, _ = client_for(Role.ADMIN)
    response = admin_client.post(f"/admin/ledger/operator/{operator.pk}/delete/", {"post": "yes"})
    assert response.status_code == 403
    assert Operator.objects.filter(pk=operator.pk).exists()


# --- Screens --------------------------------------------------------------------------------


def test_search_paging_and_hostile_input(billing: Client) -> None:
    for index in range(30):
        new_operator(display_name=f"Operator {index:02d}", email=f"op{index}@example.com")
    first = billing.get(LIST).content.decode()
    assert "Page 1 of 2 (30 operators)" in first
    assert billing.get(f"{LIST}?page=99").status_code == 200
    assert billing.get(f"{LIST}?page=-1").status_code == 200
    assert billing.get(f"{LIST}?page=abc").status_code == 200
    found = billing.get(f"{LIST}?q=OP7%40EXAMPLE").content.decode()
    assert "Operator 07" in found and "Operator 08" not in found
    for hostile in ("' OR 1=1 --", "{{ settings.SECRET_KEY }}", "%", "_", "x" * 5000):
        response = billing.get(LIST, {"q": hostile})
        assert response.status_code == 200
        assert "No operators match." in response.content.decode()


def test_control_characters_in_the_search_are_removed_not_a_500(billing: Client) -> None:
    new_operator(display_name="Abc Holdings", email="abc@example.com")
    new_operator(display_name="Zeta Rentals", email="zeta@example.com")
    # A raw NUL in the query string reaches PostgreSQL as NUL unless it is removed.
    everyone = billing.get(f"{LIST}?q=%00")
    assert everyone.status_code == 200
    page = everyone.content.decode()
    assert "Abc Holdings" in page and "Zeta Rentals" in page
    for term, abc, zeta in (
        ("\x00abc", True, False),
        ("\x1b[31m", False, False),
        ("\u0085", True, True),
        ("zeta\x00 \x9f", False, True),
    ):
        response = billing.get(LIST, {"q": term})
        assert response.status_code == 200, term
        page = response.content.decode()
        assert ("Abc Holdings" in page) is abc, term
        assert ("Zeta Rentals" in page) is zeta, term
        assert "\x00" not in page and "\x1b" not in page, term


def test_the_detail_page_shows_balance_leases_and_masked_ids(
    billing: Client, fake: FakeProvider
) -> None:
    from tests.test_ledger_models import new_lease, new_rent_charge, saved

    operator = new_operator()
    lease = saved(new_lease(operator=operator))
    saved(new_rent_charge(lease=lease))
    operator_sync.sync_operator(operator.pk, provider=fake, actor=None)
    customer = operator_sync.current_refs(operator)["customer"]

    page = billing.get(detail(operator)).content.decode()
    assert "$2450.00" in page
    assert "Store 900 Testville" in page
    assert "No invoices yet." in page
    assert customer not in page
    assert f"{customer[:4]}…{customer[-4:]}" in page


# --- PLAN-006a §4 row 5: every POST redirects; errors shown once ------------------------------


def _held_count() -> int:
    """How many sessions hold a form for the next GET."""
    return sum(1 for session in Session.objects.all() if HELD_FORM_KEY in session.get_decoded())


def test_every_post_route_redirects_on_success(fake: FakeProvider) -> None:
    client, _ = client_for(Role.ADMIN)
    created = client.post(NEW, form_data())
    operator = Operator.objects.get()
    assert (created.status_code, created["Location"]) == (302, detail(operator))

    paths = _paths(operator)
    edited = client.post(paths["edit"], form_data(phone="+1 555 010 0003"))
    assert (edited.status_code, edited["Location"]) == (302, detail(operator))

    Operator.objects.filter(pk=operator.pk).update(sync_status=SyncStatus.FAILED)
    retried = client.post(paths["retry_sync"])
    assert (retried.status_code, retried["Location"]) == (302, detail(operator))

    manual = new_operator(email=None, display_name="Manual")
    add_provider_customer(fake, "cus_prg_0001", "")
    linked = client.post(
        _paths(manual)["link"], {"customer_id": "cus_prg_0001", "payment_method_id": "pm_prg"}
    )
    assert (linked.status_code, linked["Location"]) == (302, detail(manual))

    archived = client.post(paths["archive"])
    assert (archived.status_code, archived["Location"]) == (302, LIST)
    assert _held_count() == 0


def test_every_post_route_redirects_on_failure(fake: FakeProvider) -> None:
    client, _ = client_for(Role.ADMIN)
    operator = new_operator()
    paths = _paths(operator)

    invalid_create = client.post(NEW, form_data(display_name="  ", email="bad"))
    assert (invalid_create.status_code, invalid_create["Location"]) == (302, NEW)

    invalid_edit = client.post(paths["edit"], form_data(display_name=""))
    assert (invalid_edit.status_code, invalid_edit["Location"]) == (302, paths["edit"])

    invalid_link = client.post(paths["link"], {"customer_id": "bad id", "payment_method_id": ""})
    assert (invalid_link.status_code, invalid_link["Location"]) == (302, paths["link"])

    refused_link = client.post(paths["link"], {"customer_id": "cus_none", "payment_method_id": "p"})
    assert (refused_link.status_code, refused_link["Location"]) == (302, paths["link"])

    # A retry inside the lock is refused with a message, still a redirect.
    Operator.objects.filter(pk=operator.pk).update(
        sync_status=SyncStatus.FAILED, sync_attempted_at=timezone.now()
    )
    too_soon = client.post(paths["retry_sync"], follow=True)
    assert too_soon.redirect_chain == [(detail(operator), 302)]
    assert "Retry available in " in too_soon.content.decode()
    assert fake.calls == [("get_customer", ("cus_none",))]

    # Archiving twice, and anything on an archived operator, redirects too.
    assert client.post(paths["archive"]).status_code == 302
    for name in ("archive", "edit", "retry_sync", "link"):
        response = client.post(paths[name], form_data())
        assert response.status_code == 302, name


@pytest.mark.parametrize("route", ["create", "edit", "link"])
def test_errors_are_shown_on_the_next_get_only(
    billing: Client, fake: FakeProvider, route: str
) -> None:
    operator = new_operator(email=None)
    path = _paths(operator)[route]
    invalid: dict[str, Any] = (
        {"customer_id": "bad id", "payment_method_id": "pm_1"}
        if route == "link"
        else form_data(display_name="", legal_name="Kept Legal Name")
    )

    posted = billing.post(path, invalid)
    assert posted.status_code == 302 and posted["Location"] == path

    first = billing.get(path).content.decode()
    assert 'class="errorlist' in first
    if route != "link":
        assert "This field is required." in first
        assert 'value="Kept Legal Name"' in first
    else:
        assert 'value="bad id"' in first

    second = billing.get(path).content.decode()
    assert 'class="errorlist' not in second
    assert "Kept Legal Name" not in second
    assert _held_count() == 0


def test_the_create_form_keeps_its_creation_key_across_the_redirect(
    billing: Client, fake: FakeProvider
) -> None:
    data = form_data(display_name="")
    billing.post(NEW, data)
    page = billing.get(NEW).content.decode()
    assert f'value="{data["creation_key"]}"' in page


def test_a_held_form_is_not_shown_on_another_form(billing: Client, fake: FakeProvider) -> None:
    first = new_operator(email=None)
    second = new_operator(email=None, display_name="Second")
    billing.post(_paths(first)["edit"], form_data(display_name=""))
    other = billing.get(_paths(second)["edit"]).content.decode()
    assert 'class="errorlist' not in other
    # Consumed by that GET: the first form's page no longer shows it either.
    assert 'class="errorlist' not in billing.get(_paths(first)["edit"]).content.decode()


def test_a_card_number_is_never_held_in_the_session(billing: Client, fake: FakeProvider) -> None:
    billing.post(NEW, form_data(notes="card 4111 1111 1111 1111", city="Kept City"))
    stored = " ".join(str(session.get_decoded()) for session in Session.objects.all())
    assert "4111" not in stored
    assert "Kept City" in stored
    page = billing.get(NEW).content.decode()
    assert "4111" not in page
    assert "card or account numbers" in page
    assert 'value="Kept City"' in page


def test_refreshing_after_a_post_issues_only_a_get(billing: Client, fake: FakeProvider) -> None:
    response = billing.post(NEW, form_data(), follow=True)
    final_url = response.redirect_chain[-1][0]
    assert response.request["REQUEST_METHOD"] == "GET"
    # A refresh repeats the last request of the chain: a GET of the detail page.
    billing.get(final_url)
    assert Operator.objects.count() == 1
    assert call_names(fake).count("create_customer") == 1


# --- PLAN-006a §4 rows 4 and 6: the lock and the rejected fields on the detail page -----------


def test_the_detail_page_shows_the_rejected_field_names(
    billing: Client, fake: FakeProvider
) -> None:
    fake.fail_next("create_customer", ProviderRejected(422, error_fields=("email", "lastname")))
    response = billing.post(NEW, form_data(), follow=True)
    page = response.content.decode()
    assert "Fields the provider rejected: email, lastname" in page

    operator = Operator.objects.get()
    backdate_attempt(operator)
    billing.post(f"{detail(operator)}retry-sync/")
    page = billing.get(detail(operator)).content.decode()
    assert "Fields the provider rejected" not in page


def test_the_detail_page_hides_the_retry_button_while_locked(
    billing: Client, fake: FakeProvider
) -> None:
    operator = new_operator()
    Operator.objects.filter(pk=operator.pk).update(sync_status=SyncStatus.FAILED)
    for seconds, button in [(1, False), (59, False), (60, True), (600, True)]:
        backdate_attempt(operator, seconds)
        page = billing.get(detail(operator)).content.decode()
        assert ("Retry sync" in page) is button, seconds
        assert ("Retry available in " in page) is (not button), seconds


def test_a_viewer_sees_neither_the_button_nor_the_wait(fake: FakeProvider) -> None:
    client, _ = client_for(Role.VIEWER)
    operator = new_operator()
    Operator.objects.filter(pk=operator.pk).update(
        sync_status=SyncStatus.FAILED, sync_attempted_at=timezone.now()
    )
    page = client.get(detail(operator)).content.decode()
    assert "Retry sync" not in page and "Retry available" not in page


def test_two_quick_retry_posts_make_one_provider_call(billing: Client, fake: FakeProvider) -> None:
    operator = new_operator()
    Operator.objects.filter(pk=operator.pk).update(sync_status=SyncStatus.FAILED)
    fake.fail_next("find_customers", ProviderUnavailable(503))
    first = billing.post(f"{detail(operator)}retry-sync/")
    second = billing.post(f"{detail(operator)}retry-sync/", follow=True)
    assert first.status_code == 302
    assert second.redirect_chain == [(detail(operator), 302)]
    assert call_names(fake) == ["find_customers"]
    assert "Retry available in " in second.content.decode()


# --- PLAN-006b §3 rows 1 and 4: the phone rule in the form, the edit and the retry ------------

PHONE_MESSAGE = "Enter 10 to 15 digits, for example 4155550132."


@pytest.mark.parametrize(
    ("phone", "stored"),
    [
        ("4155550132", "4155550132"),
        ("+1 (415) 555-0132", "14155550132"),
        ("415.555.0132", "4155550132"),
        ("123456789012345", "123456789012345"),
        ("", ""),
    ],
    ids=["digits", "formatted", "dotted", "fifteen", "empty"],
)
def test_the_create_form_accepts_and_stores_the_normalised_phone(
    billing: Client, fake: FakeProvider, phone: str, stored: str
) -> None:
    billing.post(NEW, form_data(phone=phone))
    operator = Operator.objects.get()
    assert operator.phone == stored
    [(_, (customer,))] = [call for call in fake.calls if call[0] == "create_customer"]
    assert customer.phone == stored  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "phone",
    ["555-0132", "12345678901234567", "+44 20 7946 0958 ext 12", "call the office"],
    ids=["seven", "seventeen", "extension", "letters"],
)
def test_the_create_form_refuses_a_bad_phone_with_the_fixed_text_and_no_call(
    billing: Client, fake: FakeProvider, phone: str
) -> None:
    response = billing.post(NEW, form_data(phone=phone), follow=True)
    page = response.content.decode()
    assert PHONE_MESSAGE in page
    assert response.redirect_chain[-1][0] == NEW
    assert Operator.objects.count() == 0
    assert fake.calls == []


def test_a_bad_stored_phone_blocks_edit_and_retry_until_it_is_fixed(
    billing: Client, fake: FakeProvider
) -> None:
    operator = new_operator()
    Operator.objects.filter(pk=operator.pk).update(
        phone="555-0132", sync_status=SyncStatus.FAILED, sync_failure="ProviderRejected"
    )
    backdate_attempt(operator)
    edit = f"{detail(operator)}edit/"
    retry = f"{detail(operator)}retry-sync/"

    # The detail page offers the fix sentence, not the retry button.
    page = billing.get(detail(operator)).content.decode()
    assert "Fix the phone number first." in page
    assert "Retry sync" not in page
    assert "Retry available in" not in page

    # An edit that keeps the bad phone is refused; a direct retry POST makes no call.
    response = billing.post(edit, form_data(phone="555-0132", city="Elsewhere"), follow=True)
    assert PHONE_MESSAGE in response.content.decode()
    response = billing.post(retry, follow=True)
    assert "Fix the phone number first." in response.content.decode()
    operator.refresh_from_db()
    assert (operator.phone, operator.city) == ("555-0132", "Testville")
    assert operator.sync_status == SyncStatus.FAILED
    assert fake.calls == []

    # Fixed: saved as digits, the retry button returns, and the retry links the customer.
    billing.post(edit, form_data(phone="(415) 555-0132"))
    operator.refresh_from_db()
    assert operator.phone == "4155550132"
    page = billing.get(detail(operator)).content.decode()
    assert "Fix the phone number first." not in page
    assert "Retry sync" in page
    billing.post(retry)
    operator.refresh_from_db()
    assert operator.sync_status == SyncStatus.LINKED
    [(_, (customer,))] = [call for call in fake.calls if call[0] == "create_customer"]
    assert customer.phone == "4155550132"  # type: ignore[attr-defined]


def test_the_edit_of_a_linked_operator_with_a_bad_phone_is_refused(
    billing: Client, fake: FakeProvider
) -> None:
    billing.post(NEW, form_data())
    operator = Operator.objects.get()
    assert operator.sync_status == SyncStatus.LINKED
    Operator.objects.filter(pk=operator.pk).update(phone="555-0132")
    calls = len(fake.calls)

    response = billing.post(
        f"{detail(operator)}edit/", form_data(phone="555-0132", city="Elsewhere"), follow=True
    )

    assert PHONE_MESSAGE in response.content.decode()
    operator.refresh_from_db()
    assert operator.city == "Testville"
    assert len(fake.calls) == calls
