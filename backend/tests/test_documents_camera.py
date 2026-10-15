"""The "Take photo" input on the document upload forms
(docs/qa/PLAN-011-dashboard-reports-mobile.md D9): its attributes, an upload through it on
the store and lease pages and the create forms, and one file per submit still.

A browser sends a part for every file input of the form, an empty one with
``filename=""`` for the input left empty, which Django does not count as a file;
:func:`browser_body` builds such a body by hand, since Django's test client leaves an
empty input out.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import Document, Store
from tests.document_files import JPEG, PNG
from tests.test_documents import (  # noqa: F401  (storage: autouse fixture)
    client_of,
    lease_url,
    page_messages,
    storage,
    store_create_data,
    store_url,
    upload_file,
    upload_url,
)
from tests.test_ledger_models import new_lease, new_store, saved

pytestmark = pytest.mark.django_db

BOUNDARY = "----finance-test-boundary"
FILE_ACCEPT = 'accept="image/*,.pdf,.docx,.heic"'
PHOTO_ACCEPT = 'accept="image/*" capture="environment"'


def browser_body(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> bytes:
    """A multipart body as a browser sends it: every file input has a part."""
    lines: list[bytes] = []
    for name, value in fields.items():
        lines += [
            f"--{BOUNDARY}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value.encode(),
        ]
    for name, (filename, data) in files.items():
        lines += [
            f"--{BOUNDARY}".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(),
            b"Content-Type: application/octet-stream",
            b"",
            data,
        ]
    lines += [f"--{BOUNDARY}--".encode(), b""]
    return b"\r\n".join(lines)


def browser_post(client: Client, url: str, fields: dict[str, str], files: dict[str, Any]) -> Any:
    return client.post(
        url,
        data=browser_body(fields, files),
        content_type=f"multipart/form-data; boundary={BOUNDARY}",
    )


# --- Attributes -----------------------------------------------------------------------------


def test_the_store_and_lease_pages_offer_a_file_and_a_photo_input() -> None:
    lease = saved(new_lease())
    client, _ = client_of(Role.BILLING)
    # PLAN-012d D3: the store's Documents section is its page's Documents tab.
    for url in (f"{store_url(lease.store)}?tab=documents", lease_url(lease)):
        page = page_messages(client, url)
        assert (
            f'<input type="file" name="file" {FILE_ACCEPT} class="js-document-file" id="id_file">'
            in page
        ), url
        assert (
            f'<input type="file" name="photo" {PHOTO_ACCEPT} class="js-document-photo"'
            ' id="id_photo">' in page
        ), url
        assert '<label for="id_photo">Take photo</label>' in page, url


def test_the_create_forms_offer_the_photo_input_too() -> None:
    client, _ = client_of(Role.ADMIN)
    for url in ("/stores/new/", "/leases/new/"):
        page = page_messages(client, url)
        assert f'name="document-file" {FILE_ACCEPT}' in page, url
        assert f'name="document-photo" {PHOTO_ACCEPT}' in page, url
        assert '<label for="id_document-photo">Take photo</label>' in page, url


# --- Upload through the photo input -----------------------------------------------------------


def test_a_photo_uploads_through_the_second_input() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    response = client.post(
        upload_url(store), {"kind": "other", "note": "", "photo": upload_file("IMG_0001.jpg", JPEG)}
    )
    assert response.status_code == 302
    document = Document.objects.get()
    assert document.store == store and document.original_filename == "IMG_0001.jpg"
    assert "Document uploaded." in page_messages(client, store_url(store))


def test_a_browser_post_with_one_input_left_empty_uploads() -> None:
    """The regression a second input brings: the empty part of the other input."""
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    fields = {"kind": "other", "note": ""}
    response = browser_post(
        client, upload_url(store), fields, {"file": ("", b""), "photo": ("IMG_0002.jpg", JPEG)}
    )
    assert response.status_code == 302
    response = browser_post(
        client, upload_url(store), fields, {"file": ("plan.png", PNG), "photo": ("", b"")}
    )
    assert response.status_code == 302
    assert sorted(Document.objects.values_list("original_filename", flat=True)) == [
        "IMG_0002.jpg",
        "plan.png",
    ]


def test_a_file_in_both_inputs_is_refused_and_nothing_is_stored() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    response = client.post(
        upload_url(store),
        {
            "kind": "other",
            "file": upload_file("a.png", PNG),
            "photo": upload_file("b.jpg", JPEG),
        },
    )
    assert response.status_code == 400
    assert Document.objects.count() == 0


def test_a_file_under_another_name_is_never_stored() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    response = browser_post(
        client,
        upload_url(store),
        {"kind": "other"},
        {"file": ("", b""), "photo": ("", b""), "extra": ("c.png", PNG)},
    )
    assert response.status_code == 302
    assert Document.objects.count() == 0
    assert "Choose a file to upload." in page_messages(client, store_url(store))


def test_neither_input_says_choose_a_file() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    browser_post(
        client, upload_url(store), {"kind": "other"}, {"file": ("", b""), "photo": ("", b"")}
    )
    assert Document.objects.count() == 0
    assert "Choose a file to upload." in page_messages(client, store_url(store))


def test_the_photo_input_is_still_checked_by_content() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    client.post(upload_url(store), {"kind": "other", "photo": upload_file("x.jpg", PNG)})
    assert Document.objects.count() == 0
    assert "does not match its content" in page_messages(client, store_url(store))


def test_the_store_create_form_uploads_a_photo() -> None:
    client, _ = client_of(Role.ADMIN)
    data = store_create_data(
        **{"document-photo": upload_file("front.jpg", JPEG), "document-kind": "other"}
    )
    client.post("/stores/new/", data)
    store = Store.objects.get(external_label="Store 0321")
    assert Document.objects.get().store == store


def test_the_store_create_form_with_both_inputs_is_refused_whole() -> None:
    """Two files are refused by the request parser (one file per submit), before the
    store is saved."""
    client, _ = client_of(Role.ADMIN)
    data = store_create_data(
        **{
            "document-file": upload_file("plan.png", PNG),
            "document-photo": upload_file("front.jpg", JPEG),
            "document-kind": "other",
        }
    )
    assert client.post("/stores/new/", data).status_code == 400
    assert not Store.objects.filter(external_label="Store 0321").exists()
    assert Document.objects.count() == 0
