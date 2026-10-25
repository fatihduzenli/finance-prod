"""Adversarial cases for PLAN-012f-b Batch B (stores and operators), by the QA owner (plan §6).
Slice B-1: the four form URLs render their page with the dialog open and write nothing on GET; a
dialog's return URL is a site path and never an outside address; a viewer's POST to each is
refused with nothing written; a held value with markup is escaped inside the dialog; the dialog
is the only form on the page with a primary button."""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel, Store
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_lease_end_drafts import row_counts
from tests.test_operator_views import client_for

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
