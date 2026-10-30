"""PLAN-012f-b Batch B, slice B-2 (D9): the operator's Type (Company / Person), as PLAN-014
C1 changed it, and Fable's finding B3-1 (no select of ``ledger/forms.py`` repeats a refused
value).

PLAN-014 C1: an operator is a person, named by its Full name (the display name, typed on the
form). The type, company, contact person, first and last name and legal name left the form:
a post that still sends them is not refused, and they are not written (the columns stay as
history). ``Operator.save`` derives the display name from the kind's name fields only while
it is empty. Existing operators became people with empty names and their display name kept
(ledger migration 0052, which reverses). The search finds a company by its name and a
person by either name. Every select of the module refuses with its own words, never with
the posted value: a card number posted to any of them is not in the response nor in the
page that re-opens the form.
"""

from __future__ import annotations

import importlib
import inspect
import re
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django import forms
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client

from accounts.roles import Role
from ledger import forms as ledger_forms
from ledger.forms import (
    BRAND_REFUSED,
    CHOICE_REFUSED,
    STATE_REFUSED,
    OperatorCreateForm,
    OperatorForm,
)
from ledger.models import Operator, OperatorKind, SyncStatus
from ledger.services import operators, search
from payments import lease_sync
from payments.fake import FakeProvider
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_form_modals import _field_error, dialog_of, page
from tests.test_lease_sync import billed_lease
from tests.test_lease_views import edit_data
from tests.test_lease_views import form_data as lease_data
from tests.test_operator_views import client_for
from tests.test_operator_views import form_data as operator_data
from tests.test_store_views import form_data as store_data

pytestmark = pytest.mark.django_db

CARD = "4111111111111111"
NEW = "/operators/new/"
BEFORE = ("ledger", "0051_store_type_and_state_codes")
AFTER = ("ledger", "0052_operator_kind")


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


#: The fields of PLAN-012f-b D9 that left the form with PLAN-014 C1.
KIND_FIELDS = ("kind", "company", "contact_person", "first_name", "last_name", "legal_name")


def person_data(**overrides: Any) -> dict[str, Any]:
    """The operator form's post for Ada Lovelace (PLAN-014 C1: the Full name)."""
    data = operator_data(display_name="Ada Lovelace")
    return {**data, **overrides}


# --- The model: the display name derived from the kind's name fields --------------------------


def test_a_companys_display_name_is_its_company_name() -> None:
    operator = Operator.objects.create(
        kind=OperatorKind.COMPANY, company="  Northwind   Stores ", contact_person="Ann Lee"
    )
    assert operator.display_name == "Northwind Stores"
    assert operator.contact_person == "Ann Lee"


def test_a_persons_display_name_is_first_and_last() -> None:
    operator = Operator.objects.create(first_name=" Ada ", last_name="Lovelace")
    assert operator.kind == OperatorKind.PERSON
    assert operator.display_name == "Ada Lovelace"


def test_a_change_of_kind_or_name_keeps_a_display_name_once_set() -> None:
    """PLAN-014 C1: the name fields fill only an empty display name; a Full name is kept."""
    operator = Operator.objects.create(first_name="Ada", last_name="Lovelace", company="Acme")
    assert operator.display_name == "Ada Lovelace"
    operator.kind = OperatorKind.COMPANY
    operator.save()
    assert Operator.objects.get(pk=operator.pk).display_name == "Ada Lovelace"
    operator.company = "Acme Two"
    operator.save()
    assert Operator.objects.get(pk=operator.pk).display_name == "Ada Lovelace"
    operator.display_name = "   "
    operator.save()
    assert Operator.objects.get(pk=operator.pk).display_name == "Acme Two"


def test_empty_name_fields_keep_the_display_name() -> None:
    operator = an_operator("Legacy Holdings")
    assert (operator.kind, operator.first_name, operator.last_name) == ("person", "", "")
    operator.city = "Elsewhere"
    operator.save()
    assert Operator.objects.get(pk=operator.pk).display_name == "Legacy Holdings"
    company = Operator.objects.create(kind=OperatorKind.COMPANY, display_name="Kept Co")
    assert company.display_name == "Kept Co"


def test_the_kind_is_held_to_its_two_values_by_the_database() -> None:
    operator = an_operator("Checked")
    with pytest.raises(IntegrityError), transaction.atomic():
        Operator.objects.filter(pk=operator.pk).update(kind="trust")


# --- Migration 0052 ------------------------------------------------------------------------


def _migrate(target: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])
    return MigrationExecutor(connection).loader.project_state([target]).apps


def _columns() -> set[str]:
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(cursor, "operators")
        }


@pytest.mark.django_db(transaction=True)
def test_migration_0052_makes_existing_operators_people_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    new_columns = {"kind", "first_name", "last_name", "contact_person"}
    try:
        old = _migrate(BEFORE)
        assert not new_columns & _columns()
        row = old.get_model("ledger", "Operator").objects.create(
            display_name="Old Holdings LLC", company="Old Holdings"
        )
        new = _migrate(AFTER)
        migrated = new.get_model("ledger", "Operator").objects.get(pk=row.pk)
        assert (migrated.kind, migrated.first_name, migrated.last_name) == ("person", "", "")
        assert migrated.contact_person == ""
        assert (migrated.display_name, migrated.company) == ("Old Holdings LLC", "Old Holdings")

        _migrate(BEFORE)
        assert not new_columns & _columns()
        back = old.get_model("ledger", "Operator").objects.get(pk=row.pk)
        assert back.display_name == "Old Holdings LLC"
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
    assert new_columns <= _columns()


def test_the_migration_guards_the_three_new_text_columns() -> None:
    migration = importlib.import_module("ledger.migrations.0052_operator_kind")
    before = importlib.import_module("ledger.migrations.0031_line_kind_fees_consent")
    assert migration.GUARDED_COLUMNS["operators"] == (
        *before.GUARDED_COLUMNS["operators"],
        "contact_person",
        "first_name",
        "last_name",
    )


# --- The form ------------------------------------------------------------------------------


def test_the_form_has_the_full_name_first_and_no_type_or_name_pair() -> None:
    form = OperatorCreateForm()
    names = [field.name for field in form.visible_fields()]
    assert names[:3] == ["display_name", "email", "phone"]
    assert form["display_name"].label == "Full name"
    assert form.fields["display_name"].required
    for name in KIND_FIELDS:
        assert name not in form.fields, name


def test_no_field_is_shown_with_a_type_radio() -> None:
    body = str(OperatorForm())
    assert 'id="id_kind_0"' not in body and 'name="kind"' not in body
    assert 'data-shown-when="id_kind_' not in body
    assert "data-shown-when" not in str(OperatorForm()["display_name"])


@pytest.mark.parametrize("name", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"])
def test_the_form_refuses_a_missing_full_name_under_its_field(name: str) -> None:
    form = OperatorForm(data={"display_name": name, "email": "", "phone": "", "kind": CARD})
    assert not form.is_valid()
    assert form.errors["display_name"] == ["Enter the full name."]
    assert CARD not in str(form.errors)


@pytest.mark.parametrize(
    "posted",
    [
        {"kind": "company", "company": "Acme"},
        {"kind": "person", "first_name": "A", "last_name": "B"},
        {"kind": ""},
        {"kind": CARD},
    ],
    ids=["company", "person", "no-kind", "card-kind"],
)
def test_a_posted_type_or_name_pair_is_ignored_not_an_error(posted: dict[str, str]) -> None:
    form = OperatorForm(data={"display_name": "Ada  Lovelace ", **posted})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["display_name"] == "Ada Lovelace"
    for name in KIND_FIELDS:
        assert name not in form.cleaned_data, name
    # Without the Full name the pair alone does not make a valid form.
    assert not OperatorForm(data=posted).is_valid()


# --- The screens ---------------------------------------------------------------------------


@pytest.mark.usefixtures("worker_between_requests")
def test_a_new_operator_is_named_by_its_full_name_and_the_old_fields_are_ignored() -> None:
    billing, _ = client_for(Role.BILLING)
    billing.post(
        NEW,
        operator_data(
            display_name="Acme Kiosks",
            kind="company",
            company="Posted Co",
            contact_person="Ann Lee",
            legal_name="Posted Legal",
            email="",
        ),
    )
    billing.post(NEW, person_data(email="", first_name="Forged", last_name="Names"))
    company = Operator.objects.get(display_name="Acme Kiosks")
    person = Operator.objects.get(display_name="Ada Lovelace")
    # PLAN-014 C1: new operators are persons; nothing of the old fields is written.
    for operator in (company, person):
        assert operator.kind == OperatorKind.PERSON
        assert (operator.company, operator.contact_person, operator.legal_name) == ("", "", "")
        assert (operator.first_name, operator.last_name) == ("", "")
    page_body = page(billing, f"/operators/{person.pk}/")
    assert 'id="operator-title"' in page_body and "Ada Lovelace" in page_body
    assert '<dd id="full-name">Ada Lovelace</dd>' in page_body


@pytest.mark.usefixtures("worker_between_requests")
def test_the_posted_full_name_is_the_display_name_and_the_name_pair_is_ignored() -> None:
    billing, _ = client_for(Role.BILLING)
    billing.post(NEW, person_data(email="", first_name="Forged", last_name="Name"))
    operator = Operator.objects.get()
    assert operator.display_name == "Ada Lovelace"
    edit = f"/operators/{operator.pk}/edit/"
    billing.post(edit, person_data(email="", display_name="Ada Byron", last_name="Forged Again"))
    operator.refresh_from_db()
    assert operator.display_name == "Ada Byron"
    assert operator.last_name == ""


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "spaces"])
@pytest.mark.parametrize("route", ["create", "edit"])
@pytest.mark.usefixtures("worker_between_requests")
def test_a_missing_name_reopens_the_dialog_with_the_error_under_the_field(
    blank: str, route: str
) -> None:
    billing, _ = client_for(Role.BILLING)
    operator = an_operator("Walk Co")
    path = NEW if route == "create" else f"/operators/{operator.pk}/edit/"
    response = billing.post(path, operator_data(display_name=blank, city="Kept City", email=""))
    assert (response.status_code, response["Location"]) == (302, path)
    _, dialog = dialog_of(page(billing, path), "operator-form")
    assert "Enter the full name." in _field_error(dialog, "id_display_name")
    assert 'value="Kept City"' in dialog
    assert Operator.objects.count() == 1
    assert Operator.objects.get().display_name == "Walk Co"


def test_an_existing_operator_opens_with_its_full_name_and_no_type() -> None:
    billing, _ = client_for(Role.BILLING)
    operator = an_operator("Legacy Holdings")
    _, dialog = dialog_of(page(billing, f"/operators/{operator.pk}/edit/"), "operator-form")
    assert "Edit operator Legacy Holdings" in dialog
    assert re.search(r'<input type="text" name="display_name" value="Legacy Holdings"', dialog)
    assert 'name="kind"' not in dialog


def test_a_rename_is_local_and_the_leases_customer_input_follows_the_full_name(
    fake: FakeProvider,
) -> None:
    operator, _ = operators.create_operator(
        {"kind": "company", "company": "Ignored Co", "display_name": "Before Person"},
        creation_key=uuid.uuid4(),
        actor=None,
    )
    assert (operator.display_name, operator.kind, operator.company) == (
        "Before Person",
        OperatorKind.PERSON,
        "",
    )
    lease = billed_lease(operator=operator)
    before = lease_sync.customer_input(lease)
    updated = operators.update_operator(
        operator.pk, {"company": "Ignored Two", "display_name": "After Person"}, actor=None
    )
    assert (updated.display_name, updated.company) == ("After Person", "")
    lease.refresh_from_db()
    after = lease_sync.customer_input(lease)
    assert (before.first_name, before.last_name) == ("Before", "Person")
    assert (after.first_name, after.last_name) == ("After", "Person")
    assert fake.calls == []


def test_a_rename_of_an_operator_with_a_linked_lease_queues_no_sync(fake: FakeProvider) -> None:
    """PLAN-014 C3: operator writes are local; the lease's customer is not marked pending."""
    operator, _ = operators.create_operator(
        {"display_name": "Ada Lovelace", "email": "a@example.com"},
        creation_key=uuid.uuid4(),
        actor=None,
    )
    lease = billed_lease(operator=operator, email="a@example.com")
    lease_sync.sync_lease(lease.pk, provider=fake, actor=None)
    lease.refresh_from_db()
    assert lease.sync_status == SyncStatus.LINKED
    calls = len(fake.calls)
    updated = operators.update_operator(operator.pk, {"display_name": "Ada Byron"}, actor=None)
    lease.refresh_from_db()
    assert (updated.display_name, updated.sync_status) == ("Ada Byron", SyncStatus.NOT_APPLICABLE)
    assert lease.sync_status == SyncStatus.LINKED
    assert len(fake.calls) == calls


# --- The search ----------------------------------------------------------------------------


def test_the_search_finds_a_company_by_its_name_and_a_person_by_either_name() -> None:
    company = Operator.objects.create(kind=OperatorKind.COMPANY, company="Northwind Kiosks")
    person = Operator.objects.create(first_name="Grace", last_name="Hopper")
    Operator.objects.create(first_name="Other", last_name="Person")

    def found(query: str) -> list[Operator]:
        return list(search.filter_operators(Operator.objects.order_by("display_name"), query))

    assert found("northwind") == [company]
    assert found("Grace") == [person]
    assert found("hopper") == [person]
    assert found("grace hopper") == [person]
    assert found("kiosks grace") == []
    # Only bound parameters: a quote or a percent sign is text, not SQL.
    assert found("' OR 1=1 --") == [] and found("%") == []


# --- Fable's finding B3-1: no select repeats a refused value ---------------------------------

#: The keyword arguments the forms that need them are built with.
FORM_KWARGS: dict[str, dict[str, Any]] = {
    "AddPaymentForm": {"debit_reason": "", "balance": Decimal("100.00")},
    "CollectForm": {"can_charge_now": True},
    "ContactEntryForm": {
        "leases": [(str(uuid.uuid4()), "A lease")],
        "lease_required": False,
        "promises": [(str(uuid.uuid4()), "A promise")],
    },
    "ContactOutcomeForm": {"outcome": "kept", "leases": [(str(uuid.uuid4()), "A lease")]},
    "InvoiceDraftForm": {"leases": [], "allow_none": True},
}
FORM_CLASSES = sorted(
    (
        cls
        for _, cls in inspect.getmembers(ledger_forms, inspect.isclass)
        if issubclass(cls, forms.BaseForm) and cls.__module__ == ledger_forms.__name__
    ),
    key=lambda cls: cls.__name__,
)


def _built(cls: type[forms.BaseForm], data: Any = None) -> forms.BaseForm:
    return cls(data, **FORM_KWARGS.get(cls.__name__, {}))


def test_every_form_of_the_module_is_covered() -> None:
    assert len(FORM_CLASSES) > 40
    assert {"LeaseForm", "StoreForm", "OperatorForm", "CalendarEntryForm"} <= {
        cls.__name__ for cls in FORM_CLASSES
    }


@pytest.mark.parametrize("cls", FORM_CLASSES, ids=lambda cls: cls.__name__)
def test_every_select_refuses_a_card_number_without_repeating_it(
    cls: type[forms.BaseForm],
) -> None:
    a_lease(a_store("Select store"), an_operator("Select Co"))
    selects = {
        name: field
        for name, field in _built(cls).fields.items()
        if isinstance(field, forms.ChoiceField | forms.ModelChoiceField)
    }
    for field in selects.values():
        assert "%(value)" not in str(field.error_messages["invalid_choice"])
    form = _built(cls, {name: CARD for name in _built(cls).fields})
    form.is_valid()
    assert CARD not in str(form.errors)


def test_a_model_select_refuses_a_value_that_is_not_a_uuid_with_its_own_words() -> None:
    form = ledger_forms.LeaseForm(data={"store": CARD, "operator": f"{uuid.uuid4()}"})
    assert not form.is_valid()
    assert form.errors["store"] == [CHOICE_REFUSED]
    assert form.errors["operator"] == [CHOICE_REFUSED]


def _assert_not_echoed(
    client: Client, path: str, data: dict[str, Any], refusal: str = CHOICE_REFUSED
) -> None:
    """The refused post's response and the page that re-opens the form hold the select's
    refusal and never the posted card number."""
    response = client.post(path, data)
    assert response.status_code in (200, 302), (path, response.status_code)
    posted = response.content.decode()
    assert CARD not in posted
    reopened = client.get(path)
    assert reopened.status_code == 200, path
    body = reopened.content.decode()
    assert CARD not in body
    assert refusal in (posted if response.status_code == 200 else body)


@pytest.mark.usefixtures("worker_between_requests")
def test_the_operator_form_ignores_a_posted_type_and_never_repeats_it() -> None:
    """PLAN-014 C1: Type is no field of the form any more; a card number posted as it is
    ignored, never stored and never shown."""
    billing, _ = client_for(Role.BILLING)
    response = billing.post(NEW, operator_data(kind=CARD), follow=True)
    assert response.status_code == 200
    assert CARD not in response.content.decode()
    operator = Operator.objects.get()
    assert operator.kind == OperatorKind.PERSON
    body = billing.get(f"/operators/{operator.pk}/edit/").content.decode()
    assert CARD not in body


@pytest.mark.parametrize("field", ["store", "operator", "payment_channel", "collection_status"])
def test_the_lease_forms_never_repeat_a_refused_select(field: str) -> None:
    admin, _ = client_for(Role.ADMIN)
    _assert_not_echoed(admin, "/leases/new/", lease_data(**{field: CARD}))
    lease = a_lease(a_store("Edited store"), an_operator("Edited Co"))
    _assert_not_echoed(admin, f"/leases/{lease.pk}/edit/", edit_data(lease, **{field: CARD}))


@pytest.mark.parametrize(
    ("field", "refusal"),
    [("brand", BRAND_REFUSED), ("state", STATE_REFUSED), ("status", CHOICE_REFUSED)],
)
def test_the_store_form_never_repeats_a_refused_select(field: str, refusal: str) -> None:
    admin, _ = client_for(Role.ADMIN)
    _assert_not_echoed(admin, "/stores/new/", store_data(**{field: CARD}), refusal)


@pytest.mark.parametrize("field", ["kind", "store", "operator", "lease"])
def test_the_calendar_form_never_repeats_a_refused_select(field: str) -> None:
    billing, _ = client_for(Role.BILLING)
    data = {"kind": "note", "on": "2026-10-20", "title": "Call", "body": "", field: CARD}
    _assert_not_echoed(billing, "/calendar/new/", data)


@pytest.mark.parametrize("field", ["kind", "lease"])
def test_the_contact_form_never_repeats_a_refused_select(field: str) -> None:
    billing, _ = client_for(Role.BILLING)
    operator = an_operator("Contact Co")
    a_lease(a_store("Contact store"), operator)
    data = {"kind": "call", "happened_on": "2026-10-06", "body": "Called.", field: CARD}
    _assert_not_echoed(billing, f"/operators/{operator.pk}/contacts/new/", data)


# --- Fable's review of B-2 (B2-1, B2-2, B2-3) -----------------------------------------------


def test_a_person_post_with_a_stale_company_writes_neither() -> None:
    form = OperatorForm(
        data={
            "display_name": "Ada Lovelace",
            "kind": "person",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company": "Stale Co",
            "contact_person": "Stale Contact",
        }
    )
    assert form.is_valid(), form.errors
    stale = {"company", "contact_person", "first_name", "last_name"}
    assert not stale & form.cleaned_data.keys()
    assert form.cleaned_data["display_name"] == "Ada Lovelace"


def test_a_company_post_with_stale_person_names_writes_none_of_them() -> None:
    form = OperatorForm(
        data={
            "display_name": "Grace Hopper",
            "kind": "company",
            "company": "Acme",
            "contact_person": "Grace Hopper",
            "first_name": "Stale",
            "last_name": "Names",
        }
    )
    assert form.is_valid(), form.errors
    for name in KIND_FIELDS:
        assert name not in form.cleaned_data, name
    assert form.cleaned_data["display_name"] == "Grace Hopper"


@pytest.mark.usefixtures("worker_between_requests")
def test_a_scripts_off_edit_writes_the_full_name_and_keeps_the_old_columns() -> None:
    operator = Operator.objects.create(
        display_name="x", kind="company", company="Acme", contact_person="Grace"
    )
    client, _ = client_for(Role.BILLING)
    edit = f"/operators/{operator.pk}/edit/"
    data = operator_data(
        display_name="Ada L",
        kind="person",
        company="Posted",
        contact_person="Posted",
        first_name="Ada",
        last_name="L",
    )
    assert client.post(edit, data).status_code == 302
    operator.refresh_from_db()
    assert operator.display_name == "Ada L"
    # PLAN-014 C1: the old columns stay as history, unwritten by the form.
    assert (operator.kind, operator.company, operator.contact_person) == (
        "company",
        "Acme",
        "Grace",
    )
    assert (operator.first_name, operator.last_name) == ("", "")


def test_the_full_name_is_a_labelled_text_field_and_type_is_gone() -> None:
    form = OperatorCreateForm()
    assert not {"kind"} & form.fields.keys()
    from django.template.loader import render_to_string

    field = render_to_string("ledger/stores/_form_field.html", {"field": form["display_name"]})
    assert '<label for="id_display_name">' in field and "Full name" in field
    assert "radio-segmented" not in str(form)


def test_the_transfer_search_finds_the_full_name_only_and_loads_shown_when() -> None:
    from django.urls import reverse

    from ledger.models import LeaseTransfer

    store = a_store("Transfer review store", 5151)
    a_lease(store, an_operator("Outgoing Person"))
    Operator.objects.create(display_name="", kind="company", company="Acme Review")
    Operator.objects.create(display_name="Legacy Name", kind="person", company="Legacy Company")
    admin, _ = client_for(Role.ADMIN)
    admin.post(reverse("ledger:stores:transfer", kwargs={"pk": store.pk}))
    row = LeaseTransfer.objects.get(store=store)
    url = reverse("ledger:transfers:operator", kwargs={"pk": row.pk})
    body = admin.get(url + "?q=Acme").content.decode()
    assert "Acme Review" in body and "Acme Review (Acme Review)" not in body
    assert "ledger/shown_when.js" in body
    other = admin.get(url + "?q=Legacy").content.decode()
    assert "Legacy Name" in other and "(Legacy Company)" not in other
    # PLAN-014 C1: the search is the full name only; a company column is not searched.
    by_company = admin.get(url + "?q=Company").content.decode()
    assert "Legacy Name" not in by_company
