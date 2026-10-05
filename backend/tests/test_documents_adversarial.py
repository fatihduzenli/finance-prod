"""Adversarial cases for PLAN-010c document uploads, written by the QA owner (plan §6).

The aim is Rule 1: bank data must not get in through a file. The cases go after the places
a number can sit in a PDF or DOCX that are not the page text (form field values, annotations,
document properties, alt text), the ways a number can be split so that only the joined text
shows it, and the ways a stored file could be served as something else or to someone else.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from ledger.models import AuditLog, Document, Lease, Store
from ledger.services import documents as service
from ledger.validators import validate_no_payment_data
from tests.document_files import HTML, PNG, make_docx, make_pdf, paragraph, part
from tests.test_documents import (
    ACCOUNT,
    CARD,
    CARD_DIGITS,
    ROUTING,
    client_of,
    do_upload,
    upload_file,
)
from tests.test_ledger_models import new_lease, new_store, saved

pytestmark = pytest.mark.django_db

PAIR = f"Routing {ROUTING} Account {ACCOUNT}"
NUMBERS = (CARD_DIGITS, ROUTING, ACCOUNT)


@pytest.fixture(autouse=True)
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    memory = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: memory)
    return memory


@pytest.fixture
def store() -> Store:
    return saved(new_store())


@pytest.fixture
def lease() -> Lease:
    return saved(new_lease())


def _billing() -> Any:
    return client_of(Role.BILLING)[1]


def _text_guard_refuses(text: str) -> bool:
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return True
    return False


def _nothing_kept(storage: MemoryDocumentStorage) -> None:
    assert storage.blobs == {}
    assert Document.objects.count() == 0
    dump = "".join(
        (str(row.before) + str(row.after) + row.entity_id).replace(" ", "").replace("-", "")
        for row in AuditLog.objects.all()
    )
    for number in NUMBERS:
        assert number not in dump


# --- numbers that are not in the page text ----------------------------------------------------


def _pdf_with(*, field_value: str = "", annotation: str = "", title: str = "") -> bytes:
    """A one-page PDF with no page text; the given strings sit in a form field's value, an
    annotation's contents and the document's title."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    annotations = ArrayObject()
    if field_value:
        field = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject("bank_details"),
                NameObject("/V"): TextStringObject(field_value),
                NameObject("/Rect"): ArrayObject([FloatObject(x) for x in (50, 700, 400, 720)]),
                NameObject("/F"): NumberObject(4),
            }
        )
        reference = writer._add_object(field)
        annotations.append(reference)
        writer._root_object[NameObject("/AcroForm")] = DictionaryObject(
            {NameObject("/Fields"): ArrayObject([reference])}
        )
    if annotation:
        note = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Text"),
                NameObject("/Contents"): TextStringObject(annotation),
                NameObject("/Rect"): ArrayObject([FloatObject(x) for x in (50, 600, 70, 620)]),
            }
        )
        annotations.append(writer._add_object(note))
    if len(annotations):
        page[NameObject("/Annots")] = annotations
    if title:
        writer.add_metadata({"/Title": title, "/Subject": title, "/Keywords": title})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize(
    "where",
    ["field_value", "annotation", "title"],
)
@pytest.mark.parametrize("secret", [CARD, PAIR])
def test_a_number_in_a_pdf_form_field_annotation_or_title_is_refused(
    store: Store, storage: MemoryDocumentStorage, where: str, secret: str
) -> None:
    """A filled-in bank form is a fillable PDF: its numbers are field values, not page text."""
    data = _pdf_with(**{where: secret})
    outcome = do_upload(store, _billing(), "ach-form.pdf", data)
    assert not outcome.ok, f"a number in the PDF's {where} was stored"
    _nothing_kept(storage)


@pytest.mark.parametrize("secret", [CARD, PAIR])
def test_a_number_in_docx_properties_or_alt_text_is_refused(
    lease: Lease, storage: MemoryDocumentStorage, secret: str
) -> None:
    core = (
        '<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{secret}</dc:title><dc:subject>lease</dc:subject></cp:coreProperties>"
    )
    for name, extra, document in (
        ("props.docx", {"docProps/core.xml": core}, None),
        (
            "alt.docx",
            {},
            part(
                paragraph("See the picture."),
                f'<w:p><w:r><w:drawing><wp:docPr id="1" name="Picture 1" descr="{secret}"/>'
                "</w:drawing></w:r></w:p>",
            ),
        ),
    ):
        outcome = do_upload(lease, _billing(), name, make_docx(document, extra=extra))
        assert not outcome.ok, f"a number in {name} was stored"
    _nothing_kept(storage)


# --- numbers split so that only the joined text shows them --------------------------------------


SPLIT_PDF_PAGES = [
    # one digit group per line
    [["Card on file:", "4111", "1111", "1111", "1111"]],
    # across a page break
    [["Card", "4111 1111"], ["1111 1111", "thank you"]],
    # dashes and dots
    [["4111-1111-1111-1111"]],
    [["4111.1111.1111.1111"]],
    # routing and account on consecutive lines
    [[f"Routing {ROUTING}", f"Account {ACCOUNT}"]],
    [[ROUTING, ACCOUNT]],
]


@pytest.mark.parametrize("pages", SPLIT_PDF_PAGES)
def test_a_pdf_is_refused_exactly_when_the_text_guard_refuses_its_joined_text(
    store: Store, storage: MemoryDocumentStorage, pages: list[list[str]]
) -> None:
    joined = "\n".join("\n".join(lines) for lines in pages)
    expected = _text_guard_refuses(joined)
    outcome = do_upload(store, _billing(), "notice.pdf", make_pdf(pages))
    assert outcome.ok is (not expected), joined
    if expected:
        _nothing_kept(storage)


def test_the_plain_card_and_the_plain_pair_are_refused_in_a_pdf(
    store: Store, storage: MemoryDocumentStorage
) -> None:
    # Not differential: these two must be refused whatever the text guard's fine print.
    for lines in ([CARD], [PAIR], [CARD_DIGITS]):
        assert not do_upload(store, _billing(), "x.pdf", make_pdf([lines])).ok, lines
    _nothing_kept(storage)


SPLIT_DOCX = [
    paragraph("4111", "1111", "1111", "1111"),  # split across runs
    paragraph("4", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"),
    paragraph("4111​1111​1111​1111"),  # zero-width spaces
    paragraph("４１１１ 1111 1111 1111"),  # full-width digits
    paragraph(f"Routing {ROUTING}") + paragraph(f"Account {ACCOUNT}"),
    paragraph("4111 1111") + paragraph("1111 1111"),
]


@pytest.mark.parametrize("body", SPLIT_DOCX)
def test_a_docx_number_split_across_runs_is_refused_and_paragraph_splits_follow_the_text_guard(
    lease: Lease, storage: MemoryDocumentStorage, body: str
) -> None:
    extracted = service.extract_text(
        service.Sniffed(service.DOCX, "amendment.docx", ""), make_docx(part(body))
    )
    assert isinstance(extracted, str)
    expected = _text_guard_refuses(extracted)
    outcome = do_upload(lease, _billing(), "amendment.docx", make_docx(part(body)))
    assert outcome.ok is (not expected)
    if body.count("<w:p>") == 1 and "４" not in body:
        assert expected, "a number inside one paragraph must be seen whatever its runs"
    if expected:
        _nothing_kept(storage)


def test_an_encrypted_zip_named_docx_and_a_polyglot_are_refused_or_served_inert(
    store: Store, storage: MemoryDocumentStorage
) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    raw = bytearray(out.getvalue())
    # Mark every entry encrypted, in its local header and in the central directory.
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = raw.find(signature)
        while at != -1:
            raw[at + flag_offset] |= 0x1
            at = raw.find(signature, at + 4)
    assert not do_upload(store, _billing(), "locked.docx", bytes(raw)).ok

    polyglot = make_pdf([["A lease."]]) + b"\n" + HTML
    outcome = do_upload(store, _billing(), "lease.pdf", polyglot)
    if outcome.ok:
        assert outcome.document is not None
        client, _ = client_of(Role.VIEWER)
        for route in ("preview", "download"):
            response = client.get(f"/documents/{outcome.document.pk}/{route}/")
            assert response["Content-Type"] == "application/pdf"
            assert response["X-Content-Type-Options"] == "nosniff"
            assert "text/html" not in response["Content-Type"]


# --- names -----------------------------------------------------------------------------------


def test_a_filename_with_a_card_number_is_refused_and_markup_in_a_name_is_inert(
    store: Store, storage: MemoryDocumentStorage
) -> None:
    assert not do_upload(store, _billing(), f"card {CARD}.png", PNG).ok
    _nothing_kept(storage)
    outcome = do_upload(store, _billing(), '"><script>alert(1)</script>.png', PNG)
    assert outcome.ok and outcome.document is not None
    assert "<" not in outcome.document.original_filename
    assert "/" not in outcome.document.blob_path.rsplit("/", 1)[-1]
    assert outcome.document.original_filename not in outcome.document.blob_path
    client, _ = client_of(Role.VIEWER)
    page = client.get(f"/stores/{store.pk}/").content.decode()
    assert "<script>alert(1)</script>" not in page
    response = client.get(f"/documents/{outcome.document.pk}/download/")
    assert "\n" not in response["Content-Disposition"]
    assert "<script>" not in response["Content-Disposition"]


# --- who may do what -----------------------------------------------------------------------------


def test_viewers_read_but_cannot_upload_or_delete_and_others_get_nothing(
    store: Store, storage: MemoryDocumentStorage
) -> None:
    document = do_upload(store, _billing(), "plan.png", PNG, kind="floor_plan").document
    assert document is not None
    viewer, _ = client_of(Role.VIEWER)
    assert viewer.get(f"/documents/{document.pk}/download/").status_code == 200
    assert viewer.get(f"/documents/{document.pk}/preview/").status_code == 200
    assert (
        viewer.post(
            f"/documents/stores/{store.pk}/upload/",
            {"kind": "photo", "file": upload_file("p.png", PNG + b"1")},
        ).status_code
        == 403
    )
    assert viewer.post(f"/documents/{document.pk}/delete/", {"note": "x"}).status_code == 403
    billing, _ = client_of(Role.BILLING)
    assert billing.post(f"/documents/{document.pk}/delete/", {"note": "x"}).status_code == 403
    nobody, _ = client_of(None)
    for route in ("download", "preview"):
        assert nobody.get(f"/documents/{document.pk}/{route}/").status_code == 403
    from django.test import Client

    anonymous = Client()
    response = anonymous.get(f"/documents/{document.pk}/download/")
    assert response.status_code in (302, 401, 403)
    assert b"\x89PNG" not in b"".join(getattr(response, "streaming_content", [response.content]))
    assert Document.objects.filter(deleted_at__isnull=True).count() == 1


def test_a_deleted_document_is_gone_for_everyone_but_admin_and_ids_cannot_be_walked(
    store: Store, storage: MemoryDocumentStorage
) -> None:
    document = do_upload(store, _billing(), "plan.png", PNG).document
    assert document is not None
    admin, _ = client_of(Role.ADMIN)
    assert (
        admin.post(f"/documents/{document.pk}/delete/", {"note": "wrong store"}).status_code == 302
    )
    assert storage.blobs, "a soft delete keeps the blob"
    billing, _ = client_of(Role.BILLING)
    for route in ("download", "preview"):
        assert billing.get(f"/documents/{document.pk}/{route}/").status_code == 404
    assert "plan.png" not in billing.get(f"/stores/{store.pk}/").content.decode()
    for key in (str(uuid.uuid4()), "not-a-uuid", "..%2F..%2Fetc", "%00"):
        for route in ("download", "preview"):
            assert billing.get(f"/documents/{key}/{route}/").status_code == 404, key


def test_every_read_and_delete_is_audited_with_no_filename_text_on_a_refusal(
    store: Store, storage: MemoryDocumentStorage
) -> None:
    billing_client, billing_user = client_of(Role.BILLING)
    document = do_upload(store, billing_user, "plan.png", PNG).document
    assert document is not None
    billing_client.get(f"/documents/{document.pk}/download/")
    billing_client.get(f"/documents/{document.pk}/preview/")
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {
        service.ACTION_UPLOADED,
        service.ACTION_DOWNLOADED,
        service.ACTION_PREVIEWED,
    } <= actions
    do_upload(store, billing_user, "secret-project-name.exe", b"MZ" + b"\x00" * 80)
    refusal = AuditLog.objects.filter(action=service.ACTION_UPLOAD_REFUSED).latest("at")
    assert "secret-project-name" not in str(refusal.after)


def test_a_blob_lost_from_storage_gives_a_sentence_not_a_traceback(
    store: Store, storage: MemoryDocumentStorage
) -> None:
    document = do_upload(store, _billing(), "plan.png", PNG).document
    assert document is not None
    storage.blobs.clear()
    viewer, _ = client_of(Role.VIEWER)
    response = viewer.get(f"/documents/{document.pk}/download/")
    assert response.status_code in (200, 302, 404, 502, 503)
    body = b"".join(getattr(response, "streaming_content", [response.content]))
    assert b"Traceback" not in body
