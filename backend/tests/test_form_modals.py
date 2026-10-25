"""New / Edit store and New / Edit operator as modals (PLAN-012f-b Batch B, slice B-1;
DESIGN-SPEC §4a).

The create and edit URLs stay the only URLs (D8: POST-redirect-GET, no fetch): a GET renders
the page behind (the Stores or Operators list, the store or operator page) with the form
dialog open (``open`` and ``data-open``, the return URL); a saved form lands on that page
with the green flash; a refused one comes back to the GET with its errors under the fields,
inside the dialog. A new record lands on its own page (Fable's B-1 finding: the sync state
and the setup are there); Cancel and × return to the list. The roles are today's: admin for
stores, billing for operators.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from ledger.forms import OPEN_DATE_REQUIRED
from ledger.models import Operator, Store
from payments.fake import FakeProvider
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.test_operator_views import client_for, new_operator
from tests.test_operator_views import form_data as operator_data
from tests.test_store_views import edit_data, make_store
from tests.test_store_views import form_data as store_data

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("worker_between_requests")]

STORES = "/stores/"
NEW_STORE = "/stores/new/"
OPERATORS = "/operators/"
NEW_OPERATOR = "/operators/new/"
MARKUP = "<script>alert(1)</script>"


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def dialog_of(body: str, dialog_id: str) -> tuple[str, str]:
    """The dialog's opening tag and its whole markup."""
    match = re.search(rf'(<dialog [^>]*id="{dialog_id}"[^>]*>).*?</dialog>', body, re.S)
    assert match is not None, f"no dialog {dialog_id}"
    return match.group(1), match.group(0)


def assert_open(tag: str, return_url: str) -> None:
    assert 'class="dialog dialog-lg"' in tag
    assert " open" in tag and " data-open" in tag
    assert f'data-return-url="{return_url}"' in tag


def assert_foot(dialog: str, prefix: str, return_url: str) -> None:
    foot = re.search(r'<div class="dialog-foot">(.*?)</div>', dialog, re.S)
    assert foot is not None
    assert f'<a class="btn" id="cancel-{prefix}" href="{return_url}">Cancel</a>' in foot.group(1)
    assert f'<button class="btn btn-primary" type="submit" id="save-{prefix}">Save</button>' in (
        foot.group(1)
    )
    assert foot.group(1).count("btn-primary") == 1


def duplicate_ids(body: str) -> list[str]:
    counts = Counter(re.findall(r'\bid="([^"]+)"', body))
    return sorted(name for name, count in counts.items() if count > 1)


def a_store(**fields: Any) -> Store:
    return make_store(
        store_number=5151, external_label="Rome", open_date=date(2025, 3, 1), **fields
    )


# --- the GET of each URL: the page behind, the dialog open ----------------------------------


def test_new_store_renders_the_stores_list_with_the_dialog_open() -> None:
    admin, _ = client_for(Role.ADMIN)
    make_store(external_label="Behind the dialog")
    body = page(admin, NEW_STORE)
    assert 'id="stores"' in body and "Behind the dialog" in body  # the list's table
    tag, dialog = dialog_of(body, "store-form")
    assert_open(tag, STORES)
    assert '<h2 id="store-form-title">New store</h2>' in dialog
    assert 'class="dialog-close" aria-label="Close"' in dialog
    assert f'action="{NEW_STORE}"' in dialog and 'enctype="multipart/form-data"' in dialog
    assert 'class="dialog-form-grid" id="store-fields"' in dialog
    cost = dialog.split('<fieldset id="store-cost-fields" class="dialog-fieldset">', 1)[1]
    assert "<legend>Store cost</legend>" in cost
    assert 'name="landlord_rent"' in cost.split("</fieldset>", 1)[0]
    assert 'id="documents-fields"' in dialog
    assert 'name="creation_key"' in dialog and 'name="csrfmiddlewaretoken"' in dialog
    assert_foot(dialog, "store", STORES)
    assert re.search(r"<title>[^<]* · Stores</title>", body)
    assert duplicate_ids(body) == []


def test_edit_store_renders_the_store_page_with_the_dialog_open() -> None:
    admin, _ = client_for(Role.ADMIN)
    store = a_store(city="Springfield")
    detail = f"/stores/{store.pk}/"
    body = page(admin, f"{detail}edit/")
    assert 'id="store-title"' in body and 'id="store-tabs"' in body  # the store page
    tag, dialog = dialog_of(body, "store-form")
    assert_open(tag, detail)
    assert '<h2 id="store-form-title">Edit store #5151 Rome</h2>' in dialog
    assert f'action="{detail}edit/"' in dialog and "multipart" not in dialog
    assert 'value="Springfield"' in dialog and 'value="Rome"' in dialog
    assert 'id="store-cost-fields"' not in dialog and 'name="landlord_rent"' not in dialog
    assert 'id="documents-fields"' not in dialog and 'name="creation_key"' not in dialog
    assert_foot(dialog, "store", detail)
    assert duplicate_ids(body) == []


def test_new_operator_renders_the_operators_list_with_the_dialog_open() -> None:
    billing, _ = client_for(Role.BILLING)
    new_operator(email=None, display_name="Behind Co")
    body = page(billing, NEW_OPERATOR)
    assert 'id="operators"' in body and "Behind Co" in body  # the list's table
    tag, dialog = dialog_of(body, "operator-form")
    assert_open(tag, OPERATORS)
    assert '<h2 id="operator-form-title">New operator</h2>' in dialog
    assert f'action="{NEW_OPERATOR}"' in dialog
    assert 'class="dialog-form-grid" id="operator-fields"' in dialog
    # PLAN-012f-b D9: Type and the kind's name fields; the display name is derived.
    assert 'name="creation_key"' in dialog and 'name="kind"' in dialog
    assert 'name="display_name"' not in dialog
    # The consents follow the phone and the e-mail (ledger/shown_when.js, loaded once).
    assert 'data-shown-when="id_sms_phone|id_phone"' in dialog
    assert body.count("ledger/shown_when.js") == 1
    assert_foot(dialog, "operator", OPERATORS)
    assert duplicate_ids(body) == []


def test_edit_operator_renders_the_operator_page_with_the_dialog_open() -> None:
    billing, _ = client_for(Role.BILLING)
    operator = new_operator(email=None, display_name="Walk Co")
    detail = f"/operators/{operator.pk}/"
    body = page(billing, f"{detail}edit/")
    assert 'id="operator-title"' in body and 'id="operator-actions"' in body  # the page
    tag, dialog = dialog_of(body, "operator-form")
    assert_open(tag, detail)
    assert '<h2 id="operator-form-title">Edit operator Walk Co</h2>' in dialog
    assert f'action="{detail}edit/"' in dialog and 'name="display_name"' not in dialog
    assert 'name="creation_key"' not in dialog
    assert_foot(dialog, "operator", detail)
    assert duplicate_ids(body) == []


def test_the_pages_alone_have_no_form_dialog() -> None:
    admin, _ = client_for(Role.ADMIN)
    store = a_store()
    operator = new_operator(email=None)
    for path, dialog_id in (
        (STORES, "store-form"),
        (f"/stores/{store.pk}/", "store-form"),
        (OPERATORS, "operator-form"),
        (f"/operators/{operator.pk}/", "operator-form"),
    ):
        body = page(admin, path)
        assert f'id="{dialog_id}"' not in body, path
        assert "data-open" not in body, path


def test_the_new_and_edit_links_keep_their_urls() -> None:
    admin, _ = client_for(Role.ADMIN)
    store = a_store()
    operator = new_operator(email=None)
    assert f'href="{NEW_STORE}"' in page(admin, STORES)
    assert f'href="/stores/{store.pk}/edit/"' in page(admin, f"/stores/{store.pk}/")
    assert f'href="{NEW_OPERATOR}"' in page(admin, OPERATORS)
    assert f'href="/operators/{operator.pk}/edit/"' in page(admin, f"/operators/{operator.pk}/")


def test_each_page_loads_the_single_submit_script_once() -> None:
    admin, _ = client_for(Role.ADMIN)
    store = a_store()
    operator = new_operator(email=None)
    for path in (
        NEW_STORE,
        f"/stores/{store.pk}/edit/",
        NEW_OPERATOR,
        f"/operators/{operator.pk}/edit/",
    ):
        body = page(admin, path)
        assert body.count('document.querySelectorAll("form.js-single-submit")') == 1, path


def test_the_list_behind_keeps_its_search() -> None:
    admin, _ = client_for(Role.ADMIN)
    make_store(external_label="Kept Plaza")
    make_store(external_label="Other Plaza")
    body = page(admin, f"{NEW_STORE}?q=Kept")
    table = body.split('id="stores"', 1)[1].split("</table>", 1)[0]
    assert "Kept Plaza" in table and "Other Plaza" not in table
    _, dialog = dialog_of(body, "store-form")
    assert f'action="{NEW_STORE}"' in dialog


# --- POST: success lands on the page behind with the flash ----------------------------------


def test_a_new_store_lands_on_its_page_with_the_flash() -> None:
    # Fable's B-1 finding: a new record's page, not the list (its setup is there).
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(NEW_STORE, store_data(store_number="5151", external_label="Rome"))
    store = Store.objects.get()
    assert (response.status_code, response["Location"]) == (302, f"/stores/{store.pk}/")
    body = page(admin, response["Location"])
    assert (
        '<li class="toast success" role="status"><span class="toast-text">'
        "Store #5151 Rome added.</span>"
    ) in body
    assert 'id="store-form"' not in body and 'id="store-title"' in body
    assert store.external_label == "Rome"


def test_an_edited_store_lands_on_its_page_with_the_flash() -> None:
    admin, _ = client_for(Role.ADMIN)
    store = a_store()
    detail = f"/stores/{store.pk}/"
    response = admin.post(f"{detail}edit/", edit_data(store, city="Elsewhere"))
    assert (response.status_code, response["Location"]) == (302, detail)
    body = page(admin, detail)
    assert "Store saved." in body and 'id="store-form"' not in body
    store.refresh_from_db()
    assert store.city == "Elsewhere"


def test_a_new_operator_lands_on_its_page_with_the_flash() -> None:
    # Fable's B-1 finding: the operator's page, where the sync's state shows.
    billing, _ = client_for(Role.BILLING)
    response = billing.post(NEW_OPERATOR, operator_data(company="Rome Stores", email=""))
    operator = Operator.objects.get()
    assert (response.status_code, response["Location"]) == (302, f"/operators/{operator.pk}/")
    body = page(billing, response["Location"])
    assert "Operator Rome Stores added." in body and 'id="operator-form"' not in body
    assert 'id="operator-title"' in body and operator.display_name == "Rome Stores"


def test_an_edited_operator_lands_on_its_page_with_the_flash() -> None:
    billing, _ = client_for(Role.BILLING)
    operator = new_operator(email=None)
    detail = f"/operators/{operator.pk}/"
    response = billing.post(f"{detail}edit/", operator_data(email="", city="Elsewhere"))
    assert (response.status_code, response["Location"]) == (302, detail)
    body = page(billing, detail)
    assert "Operator saved." in body and 'id="operator-form"' not in body
    operator.refresh_from_db()
    assert operator.city == "Elsewhere"


def test_the_flash_escapes_the_name() -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(
        NEW_STORE, store_data(store_number="77", external_label=f"Zürich {MARKUP}")
    )
    body = page(admin, response["Location"])
    assert MARKUP not in body
    assert "Store #77 Zürich &lt;script&gt;alert(1)&lt;/script&gt; added." in body


# --- POST refused: back to the GET, the error under the field, inside the dialog ------------


def _field_error(dialog: str, field_id: str) -> str:
    match = re.search(
        rf'<div class="field has-error">\s*<label for="{field_id}">.*?</div>', dialog, re.S
    )
    assert match is not None, f"no error on {field_id}"
    return match.group(0)


def test_a_refused_new_store_reopens_the_dialog_with_the_error_under_the_field() -> None:
    admin, _ = client_for(Role.ADMIN)
    response = admin.post(NEW_STORE, store_data(open_date="", external_label="Kept name"))
    assert (response.status_code, response["Location"]) == (302, NEW_STORE)
    assert not Store.objects.exists()
    body = page(admin, NEW_STORE)
    tag, dialog = dialog_of(body, "store-form")
    assert_open(tag, STORES)
    assert OPEN_DATE_REQUIRED in _field_error(dialog, "id_open_date")
    assert 'value="Kept name"' in dialog
    assert body.count(OPEN_DATE_REQUIRED) == 1  # inside the dialog only
    # Shown once: the next GET is a fresh form.
    assert OPEN_DATE_REQUIRED not in page(admin, NEW_STORE)


def test_a_refused_store_edit_reopens_the_dialog_over_the_store_page() -> None:
    admin, _ = client_for(Role.ADMIN)
    store = a_store()
    edit = f"/stores/{store.pk}/edit/"
    response = admin.post(edit, edit_data(store, open_date=""))
    assert (response.status_code, response["Location"]) == (302, edit)
    body = page(admin, edit)
    assert 'id="store-title"' in body
    _, dialog = dialog_of(body, "store-form")
    assert OPEN_DATE_REQUIRED in _field_error(dialog, "id_open_date")
    store.refresh_from_db()
    assert store.open_date == date(2025, 3, 1)


@pytest.mark.parametrize("route", ["create", "edit"])
def test_a_refused_operator_reopens_the_dialog_with_the_error_under_the_field(route: str) -> None:
    billing, _ = client_for(Role.BILLING)
    operator = new_operator(email=None, display_name="Walk Co")
    path = NEW_OPERATOR if route == "create" else f"/operators/{operator.pk}/edit/"
    response = billing.post(path, operator_data(company="", legal_name="Kept Legal"))
    assert (response.status_code, response["Location"]) == (302, path)
    body = page(billing, path)
    _, dialog = dialog_of(body, "operator-form")
    assert "Enter the company name." in _field_error(dialog, "id_company")
    assert 'value="Kept Legal"' in dialog
    assert Operator.objects.count() == 1


def test_a_hostile_value_held_by_a_refused_form_is_escaped_in_the_dialog() -> None:
    admin, _ = client_for(Role.ADMIN)
    admin.post(NEW_STORE, store_data(open_date="", external_label=MARKUP))
    body = page(admin, NEW_STORE)
    assert MARKUP not in body and "&lt;script&gt;" in dialog_of(body, "store-form")[1]


def test_a_hostile_store_name_is_escaped_in_the_dialog_title() -> None:
    admin, _ = client_for(Role.ADMIN)
    store = make_store(store_number=12, external_label=MARKUP, open_date=date(2025, 3, 1))
    body = page(admin, f"/stores/{store.pk}/edit/")
    assert MARKUP not in body
    assert (
        '<h2 id="store-form-title">Edit store #12 &lt;script&gt;alert(1)&lt;/script&gt;</h2>'
        in body
    )


# --- the roles are today's -------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_below_admin_the_store_dialogs_are_refused(role: Role) -> None:
    client, _ = client_for(role)
    store = a_store()
    for path in (NEW_STORE, f"/stores/{store.pk}/edit/"):
        assert client.get(path).status_code == 403, path
    assert client.post(NEW_STORE, store_data()).status_code == 403
    assert client.post(f"/stores/{store.pk}/edit/", edit_data(store, city="X")).status_code == 403
    assert Store.objects.count() == 1
    store.refresh_from_db()
    assert store.city == ""


def test_a_viewer_cannot_save_an_operator() -> None:
    viewer, _ = client_for(Role.VIEWER)
    operator = new_operator(email=None, display_name="Walk Co")
    assert viewer.post(NEW_OPERATOR, operator_data()).status_code == 403
    edit = f"/operators/{operator.pk}/edit/"
    assert viewer.post(edit, operator_data(display_name="Changed")).status_code == 403
    assert Operator.objects.count() == 1
    operator.refresh_from_db()
    assert operator.display_name == "Walk Co"


def test_an_archived_operators_edit_still_redirects_to_its_page() -> None:
    admin, _ = client_for(Role.ADMIN)
    operator = new_operator(email=None)
    Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
    response = admin.get(f"/operators/{operator.pk}/edit/")
    assert (response.status_code, response["Location"]) == (302, f"/operators/{operator.pk}/")


def test_anonymous_gets_401_on_the_four_urls() -> None:
    store = a_store()
    operator = new_operator(email=None)
    for path in (
        NEW_STORE,
        f"/stores/{store.pk}/edit/",
        NEW_OPERATOR,
        f"/operators/{operator.pk}/edit/",
    ):
        assert Client().get(path).status_code == 401, path


# --- the CSS ---------------------------------------------------------------------------------


def test_the_form_dialog_css() -> None:
    from tests.test_plan012f_d_pages import APP_CSS

    css = APP_CSS.read_text(encoding="utf-8")
    assert ".dialog-lg { max-height: 90vh; overflow: auto; }" in css
    assert ".dialog-form-grid { display: grid; grid-template-columns: 1fr 1fr;" in css
    assert ".dialog-form-grid .span-2 { grid-column: 1 / -1; }" in css
    phone = css.split("@media (max-width: 719.98px) {", 1)[1].split("\n}\n", 1)[0]
    assert ".dialog-form-grid { grid-template-columns: minmax(0, 1fr); }" in phone
    full = ".dialog-lg { height: 100dvh; max-height: none; margin: 0; border-width: 0;"
    assert full in phone and phone.index(full) > phone.index(".dialog, .dialog-sm, .dialog-lg {")
    assert "border-radius: 0; }" in phone.split(full, 1)[1].split("\n", 1)[0]
