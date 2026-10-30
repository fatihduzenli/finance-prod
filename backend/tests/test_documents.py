"""Documents on stores and leases (docs/qa/PLAN-010c-documents-and-cost-corrections.md §6,
Batch A): type decision, filenames, text extraction, the payment-data guard on files,
upload, download, preview, delete, the pages, the create forms, the upload limits and the
``documents`` reshape.

Every file is built in code (``tests/document_files.py``); the storage is the in-memory
one. The QA owner's adversarial cases live in ``test_documents_adversarial.py``.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import (
    MemoryFileUploadHandler,
    StopFutureHandlers,
    StopUpload,
    TemporaryFileUploadHandler,
)
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    TextStringObject,
)

import documents_storage
from accounts.roles import Role
from documents_storage import StorageUnavailable
from documents_storage.memory import MemoryDocumentStorage
from finance.uploads import CappedMemoryFileUploadHandler
from ledger.models import AuditLog, Document, Lease, Store
from ledger.services import documents as service
from ledger.validators import PAYMENT_DATA_MESSAGE
from tests.auth_helpers import make_user, verified_client
from tests.document_files import (
    AVIF,
    DOCM_CONTENT_TYPES,
    EXE,
    HEIC,
    HEIF_GENERIC_WITH_HEIC,
    HTML,
    JPEG,
    PNG,
    make_blank_pdf,
    make_docx,
    make_encrypted_pdf,
    make_pdf,
    paragraph,
    part,
)
from tests.test_lease_services import _race
from tests.test_lease_sync import requires_postgresql
from tests.test_ledger_models import new_lease, new_store, saved
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

CARD = "4111 1111 1111 1111"
CARD_DIGITS = "4111111111111111"
#: ABA-valid and synthetic, as in test_card_number_guard.
ROUTING = "123456780"
ACCOUNT = "987654321012"
MB = 1024 * 1024
MAX = 25 * MB
#: Django's default request body limit, kept for every route that takes no document.
SMALL = 2_621_440


@pytest.fixture(autouse=True)
def storage(monkeypatch: pytest.MonkeyPatch) -> MemoryDocumentStorage:
    memory = MemoryDocumentStorage()
    monkeypatch.setattr(documents_storage, "get_storage", lambda: memory)
    return memory


@pytest.fixture
def billing_user() -> User:
    return make_user(f"billing-{uuid.uuid4().hex[:8]}", Role.BILLING)[0]


@pytest.fixture
def admin_user() -> User:
    return make_user(f"admin-{uuid.uuid4().hex[:8]}", Role.ADMIN)[0]


@pytest.fixture
def viewer_user() -> User:
    return make_user(f"viewer-{uuid.uuid4().hex[:8]}", Role.VIEWER)[0]


def client_of(role: Role | None) -> tuple[Client, User]:
    user, device = make_user(f"user-{uuid.uuid4().hex[:8]}", role)
    assert device is not None
    return verified_client(user, device), user


@pytest.fixture
def store() -> Store:
    return saved(new_store())


@pytest.fixture
def lease() -> Lease:
    return saved(new_lease())


def upload_file(name: str, data: bytes, content_type: str = "application/octet-stream") -> Any:
    return SimpleUploadedFile(name, data, content_type=content_type)


def do_upload(
    owner: Store | Lease,
    actor: User,
    name: str,
    data: bytes,
    *,
    kind: str = "other",
    note: str = "",
) -> service.Outcome:
    return service.upload(owner, kind, upload_file(name, data), note, actor)


def refused_rows() -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=service.ACTION_UPLOAD_REFUSED).order_by("at"))


def assert_nothing_stored(storage: MemoryDocumentStorage, code: str) -> None:
    assert storage.blobs == {}
    assert Document.objects.count() == 0
    rows = refused_rows()
    assert rows and rows[-1].after is not None
    assert rows[-1].after["reason"] == code
    assert not AuditLog.objects.filter(action=service.ACTION_UPLOADED).exists()


def no_payment_data_in_the_audit_log() -> None:
    """No audit row holds the test card, routing or account number, in any spacing."""
    for row in AuditLog.objects.all():
        dump = (str(row.before) + str(row.after) + row.entity_id).replace(" ", "")
        dump = dump.replace("-", "")
        for number in (CARD_DIGITS, ROUTING, ACCOUNT):
            assert number not in dump


ALLOWED_FILES = [
    ("lease.pdf", make_pdf([["Lease agreement for Store 900"]]), "application/pdf"),
    (
        "lease.docx",
        make_docx(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ("photo.jpg", JPEG, "image/jpeg"),
    ("photo.jpeg", JPEG, "image/jpeg"),
    ("photo.JPG", JPEG, "image/jpeg"),
    ("plan.png", PNG, "image/png"),
    ("front.heic", HEIC, "image/heic"),
    ("front.heic", HEIF_GENERIC_WITH_HEIC, "image/heic"),
]


# --- Filenames (D5) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("lease.pdf", "lease.pdf"),
        ("C:\\Users\\me\\Desktop\\lease.pdf", "lease.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("a/b\\c.pdf", "c.pdf"),
        ("lease\u202efdp.exe.pdf", "leasefdp.exe.pdf"),
        ("le\x00as\x1fe\x7f.pdf", "lease.pdf"),
        ("lease\u200b.pdf", "lease.pdf"),
        ('a:b*c?d"e<f>g|h.pdf', "abcdefgh.pdf"),
        ("  many   spaces  .pdf", "many spaces.pdf"),
        (".pdf", "document.pdf"),
        ("...pdf", "document.pdf"),
        ("", "document"),
        ("Cafe\u0301.pdf", "Café.pdf"),
        ("photo.JPG", "photo.JPG"),
    ],
)
def test_sanitise_filename(raw: str, expected: str) -> None:
    assert service.sanitise_filename(raw) == expected


def test_a_300_character_name_is_cut_to_150_keeping_the_extension() -> None:
    name = service.sanitise_filename("x" * 300 + ".pdf")
    assert len(name) == 150
    assert name.endswith(".pdf")
    assert service.sanitise_filename("y" * 300) == "y" * 150


# --- Type decision (D5) -------------------------------------------------------------------


@pytest.mark.parametrize(("name", "data", "content_type"), ALLOWED_FILES)
def test_each_allowed_type_is_sniffed_from_its_content(
    name: str, data: bytes, content_type: str
) -> None:
    sniffed = service.sniff(data, name)
    assert isinstance(sniffed, service.Sniffed)
    assert sniffed.content_type == content_type


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("plan.pdf", PNG, service.EXTENSION_MISMATCH),
        ("lease.png", make_pdf([["x"]]), service.EXTENSION_MISMATCH),
        ("lease", make_pdf([["x"]]), service.EXTENSION_MISMATCH),
        ("photo.png", JPEG, service.EXTENSION_MISMATCH),
        ("lease.docm", make_docx(), service.EXTENSION_MISMATCH),
        ("page.pdf", HTML, service.TYPE_NOT_ACCEPTED),
        ("tool.png", EXE, service.TYPE_NOT_ACCEPTED),
        ("image.heic", AVIF, service.TYPE_NOT_ACCEPTED),
        ("notes.txt", b"plain text", service.TYPE_NOT_ACCEPTED),
        ("archive.docx", make_docx(include_main=False), service.TYPE_NOT_ACCEPTED),
        (
            "macro.docx",
            make_docx(extra={"word/vbaProject.bin": b"\x00" * 16}),
            service.MACRO,
        ),
        ("macro.docx", make_docx(content_types=DOCM_CONTENT_TYPES), service.MACRO),
        ("broken.docx", b"PK\x03\x04" + b"\x00" * 64, service.DAMAGED),
    ],
)
def test_wrong_or_unaccepted_types_are_refused(name: str, data: bytes, code: str) -> None:
    refusal = service.sniff(data, name)
    assert refusal == service.Refusal(code)
    assert refusal.message


def test_a_docx_with_too_many_entries_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "DOCX_MAX_ENTRIES", 3)
    data = make_docx(extra={"word/a.xml": "<a/>", "word/b.xml": "<b/>"})
    assert service.sniff(data, "x.docx") == service.Refusal(service.TOO_MUCH_TEXT)


# --- Text extraction (D6) -----------------------------------------------------------------


def _text(name: str, data: bytes) -> str | service.Refusal:
    sniffed = service.sniff(data, name)
    assert isinstance(sniffed, service.Sniffed)
    return service.extract_text(sniffed, data)


def test_pdf_text_of_every_page_is_extracted() -> None:
    text = _text("a.pdf", make_pdf([["first page"], ["second page"]]))
    assert isinstance(text, str)
    assert "first page" in text and "second page" in text


def test_a_scanned_pdf_without_text_is_accepted_with_no_text() -> None:
    # No page text; the only text is the producer pypdf writes in the document's
    # information dictionary, read since finding 1 on Batch D.
    assert _text("scan.pdf", make_blank_pdf()) == service.PIECE_SEPARATOR + "pypdf"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (make_blank_pdf(0), service.PDF_NO_PAGES),
        (make_encrypted_pdf(), service.PDF_UNREADABLE),
        (b"%PDF-1.7\nnot really a pdf", service.PDF_UNREADABLE),
    ],
    ids=["no-pages", "encrypted", "corrupt"],
)
def test_unreadable_pdfs_are_refused(data: bytes, code: str) -> None:
    assert _text("a.pdf", data) == service.Refusal(code)


def test_500_pages_are_read_and_501_refused() -> None:
    text = _text("a.pdf", make_blank_pdf(500))
    assert isinstance(text, str) and text.strip() == "pypdf"
    assert _text("a.pdf", make_blank_pdf(501)) == service.Refusal(service.PDF_TOO_MANY_PAGES)


def test_the_unreadable_pdf_message_is_the_plans() -> None:
    assert service.MESSAGES[service.PDF_UNREADABLE] == (
        "This PDF cannot be read, so it cannot be checked."
    )


def test_docx_runs_of_a_paragraph_are_joined() -> None:
    data = make_docx(part(paragraph("4111 11", "11 1111 ", "1111"), paragraph("next")))
    text = _text("a.docx", data)
    assert isinstance(text, str)
    assert "4111 1111 1111 1111" in text
    assert text.index("next") > text.index("1111")


def test_docx_headers_footers_footnotes_and_comments_are_read() -> None:
    extra = {
        "word/header1.xml": part(paragraph("in the header"), root="hdr"),
        "word/footer1.xml": part(paragraph("in the footer"), root="ftr"),
        "word/footnotes.xml": part(paragraph("in a footnote"), root="footnotes"),
        "word/comments.xml": part(paragraph("in a comment"), root="comments"),
    }
    text = _text("a.docx", make_docx(extra=extra))
    assert isinstance(text, str)
    for words in ("in the header", "in the footer", "in a footnote", "in a comment"):
        assert words in text


def test_docx_text_decodes_references_cdata_tabs_and_breaks_without_a_parser() -> None:
    body = (
        "<w:p><w:r><w:t>&#52;&#x31;11 &amp; &lt;b&gt;</w:t><w:tab/>"
        "<w:t><![CDATA[cdata text]]></w:t><w:br/><w:t>next line</w:t>"
        "<w:delText>deleted words</w:delText></w:r></w:p>"
        '<!DOCTYPE x [<!ENTITY big "expanded">]><w:p><w:r><w:t>&big;</w:t></w:r></w:p>'
    )
    text = _text("a.docx", make_docx(part(body)))
    assert isinstance(text, str)
    assert "4111 & <b>\tcdata text\nnext line" in text
    assert "deleted words" in text
    assert "&big;" in text and "expanded" not in text


def test_docx_text_with_other_prefixes_and_quoted_angle_brackets_is_read() -> None:
    body = '<x:p><x:r><x:t a="1 > 2">hidden prefix text</x:t></x:r></x:p>'
    text = _text("a.docx", make_docx(part(body)))
    assert isinstance(text, str)
    assert "hidden prefix text" in text


def test_a_docx_part_past_the_unpacked_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "DOCX_PART_MAX_BYTES", 10_000)
    data = make_docx(part(paragraph("a" * 20_000)))
    assert _text("a.docx", data) == service.Refusal(service.TOO_MUCH_TEXT)


def test_docx_parts_together_past_the_total_cap_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "DOCX_TOTAL_MAX_BYTES", 30_000)
    extra = {f"word/header{index}.xml": part(paragraph("b" * 9_000)) for index in range(4)}
    assert _text("a.docx", make_docx(extra=extra)) == service.Refusal(service.TOO_MUCH_TEXT)


def test_extracted_text_past_the_character_cap_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "TEXT_MAX_CHARACTERS", 100)
    assert _text("a.docx", make_docx(part(paragraph("c" * 200)))) == service.Refusal(
        service.TOO_MUCH_TEXT
    )


def test_images_have_no_text() -> None:
    assert _text("a.png", PNG) == ""
    assert _text("a.heic", HEIC) == ""


# --- Upload (D3 to D6, D8) ----------------------------------------------------------------


@pytest.mark.parametrize(("name", "data", "content_type"), ALLOWED_FILES)
@pytest.mark.parametrize("owner_kind", ["store", "lease"])
def test_each_allowed_type_uploads_to_a_store_and_a_lease(
    name: str,
    data: bytes,
    content_type: str,
    owner_kind: str,
    billing_user: User,
    storage: MemoryDocumentStorage,
) -> None:
    owner = saved(new_store()) if owner_kind == "store" else saved(new_lease())
    outcome = service.upload(
        owner, "photo", upload_file(name, data, "text/html"), "Front view", billing_user
    )
    assert outcome.ok, outcome.message
    document = Document.objects.get()
    assert document.content_type == content_type
    assert document.original_filename == name
    assert document.size_bytes == len(data)
    assert document.sha256 == hashlib.sha256(data).hexdigest()
    assert document.note == "Front view"
    assert document.uploaded_by == billing_user
    assert getattr(document, owner_kind) == owner
    assert document.blob_path == f"documents/{owner_kind}/{owner.pk}/{document.pk}"
    assert name not in document.blob_path
    assert storage.blobs[document.blob_path] == (data, content_type)
    entry = AuditLog.objects.get(action=service.ACTION_UPLOADED)
    assert entry.actor == billing_user
    assert entry.entity_type == "document" and entry.entity_id == str(document.pk)
    assert entry.after == {
        "document_id": str(document.pk),
        "owner_type": owner_kind,
        "owner_id": str(owner.pk),
        "kind": "photo",
        "size_bytes": len(data),
        "sha256_prefix": document.sha256[:12],
    }


def test_exactly_25_mb_is_accepted(store: Store, billing_user: User) -> None:
    data = PNG + b"\x00" * (MAX - len(PNG))
    assert len(data) == MAX
    assert do_upload(store, billing_user, "big.png", data).ok


def test_one_byte_more_than_25_mb_is_refused(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    data = PNG + b"\x00" * (MAX + 1 - len(PNG))
    outcome = do_upload(store, billing_user, "big.png", data)
    assert outcome.code == service.TOO_LARGE
    assert outcome.message == "The file is larger than 25 MB."
    assert_nothing_stored(storage, service.TOO_LARGE)


def test_an_empty_file_is_refused(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    outcome = do_upload(store, billing_user, "empty.pdf", b"")
    assert outcome.code == service.EMPTY
    assert_nothing_stored(storage, service.EMPTY)


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("plan.pdf", PNG, service.EXTENSION_MISMATCH),
        ("page.pdf", HTML, service.TYPE_NOT_ACCEPTED),
        ("lease.pdf", make_encrypted_pdf(), service.PDF_UNREADABLE),
        ("broken.docx", b"PK\x03\x04garbage", service.DAMAGED),
        ("macro.docx", make_docx(content_types=DOCM_CONTENT_TYPES), service.MACRO),
    ],
)
def test_a_refused_type_stores_nothing_and_audits_the_reason_only(
    name: str,
    data: bytes,
    code: str,
    store: Store,
    billing_user: User,
    storage: MemoryDocumentStorage,
) -> None:
    outcome = do_upload(store, billing_user, name, data, kind="lease_agreement")
    assert not outcome.ok and outcome.code == code
    assert_nothing_stored(storage, code)
    entry = refused_rows()[-1]
    assert entry.entity_type == "store" and entry.entity_id == str(store.pk)
    assert entry.after == {
        "reason": code,
        "owner_type": "store",
        "owner_id": str(store.pk),
        "kind": "lease_agreement",
        "size_bytes": len(data),
        "filename_length": len(name),
    }
    assert name not in str(entry.after)


def test_an_unknown_kind_is_refused_and_not_written_to_the_audit(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    outcome = do_upload(store, billing_user, "a.png", PNG, kind=f"card {CARD}")
    assert outcome.code == service.KIND_UNKNOWN
    assert_nothing_stored(storage, service.KIND_UNKNOWN)
    assert refused_rows()[-1].after["kind"] == ""


def test_a_note_longer_than_2000_characters_is_refused(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    outcome = do_upload(store, billing_user, "a.png", PNG, note="n" * 2001)
    assert outcome.code == service.NOTE_TOO_LONG
    assert_nothing_stored(storage, service.NOTE_TOO_LONG)


def test_the_note_is_stored_as_visible_text(store: Store, billing_user: User) -> None:
    assert do_upload(store, billing_user, "a.png", PNG, note="\u200b  Front \u00a0").ok
    assert Document.objects.get().note == "Front"


@pytest.mark.parametrize(
    ("name", "data", "note"),
    [
        ("card.pdf", make_pdf([["Card on file", CARD]]), ""),
        ("split.pdf", make_pdf([["Card 4111 1111", "1111 1111 on two lines"]]), ""),
        ("pages.pdf", make_pdf([["Card 4111 1111"], ["1111 1111"]]), ""),
        ("dashes.pdf", make_pdf([["4111-1111-1111-1111"]]), ""),
        ("bank.pdf", make_pdf([[f"Routing {ROUTING} account {ACCOUNT}"]]), ""),
        ("bank.pdf", make_pdf([[f"Routing {ROUTING}", f"Account {ACCOUNT}"]]), ""),
        ("runs.docx", make_docx(part(paragraph("41111111", "11111111"))), ""),
        (
            "bank.docx",
            make_docx(part(paragraph(f"Routing {ROUTING}"), paragraph(f"Account {ACCOUNT}"))),
            "",
        ),
        (
            "header.docx",
            make_docx(extra={"word/header1.xml": part(paragraph(CARD), root="hdr")}),
            "",
        ),
        (f"scan {CARD}.png", PNG, ""),
        (f"{CARD_DIGITS}.pdf", make_blank_pdf(), ""),
        ("photo.png", PNG, f"Card {CARD}"),
        ("photo.png", PNG, f"Routing {ROUTING} account {ACCOUNT}"),
    ],
)
def test_payment_data_in_text_filename_or_note_refuses_and_stores_nothing(
    name: str,
    data: bytes,
    note: str,
    store: Store,
    billing_user: User,
    storage: MemoryDocumentStorage,
) -> None:
    outcome = do_upload(store, billing_user, name, data, note=note)
    assert outcome.code == service.PAYMENT_DATA
    assert outcome.message == PAYMENT_DATA_MESSAGE
    assert_nothing_stored(storage, service.PAYMENT_DATA)
    no_payment_data_in_the_audit_log()


def test_a_routing_number_far_from_an_account_number_is_accepted_as_the_text_guard_decides(
    store: Store, billing_user: User
) -> None:
    far = make_pdf([[f"Routing {ROUTING}" + " filler" * 5 + f" account {ACCOUNT}"]])
    assert do_upload(store, billing_user, "far.pdf", far).ok
    near = make_docx(part(paragraph(f"Routing {ROUTING}"), paragraph(f"Account {ACCOUNT}")))
    assert not do_upload(store, billing_user, "near.docx", near).ok


def test_the_same_file_twice_on_one_record_is_refused(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    assert do_upload(store, billing_user, "a.png", PNG).ok
    outcome = do_upload(store, billing_user, "renamed.png", PNG)
    assert outcome.code == service.DUPLICATE
    assert outcome.message == "This file was already uploaded to this record."
    assert Document.objects.count() == 1
    assert len(storage.blobs) == 1


def test_the_same_file_on_two_records_is_allowed(billing_user: User) -> None:
    store = saved(new_store())
    lease = saved(new_lease())
    assert do_upload(store, billing_user, "a.png", PNG).ok
    assert do_upload(lease, billing_user, "a.png", PNG).ok
    assert do_upload(saved(new_store()), billing_user, "a.png", PNG).ok
    assert Document.objects.count() == 3


def test_a_deleted_file_can_be_uploaded_again(
    store: Store, billing_user: User, admin_user: User
) -> None:
    first = do_upload(store, billing_user, "a.png", PNG).document
    assert first is not None
    assert service.delete(first, "Wrong store.", admin_user).ok
    assert do_upload(store, billing_user, "a.png", PNG).ok


def test_a_failing_storage_writes_no_row(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    storage.fail_put = True
    outcome = do_upload(store, billing_user, "a.png", PNG)
    assert outcome.code == service.STORAGE_UNAVAILABLE
    assert outcome.message.startswith("The file could not be stored.")
    assert_nothing_stored(storage, service.STORAGE_UNAVAILABLE)


def test_feature_off_refuses_every_upload(
    store: Store, billing_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(documents_storage, "get_storage", lambda: None)
    outcome = do_upload(store, billing_user, "a.png", PNG)
    assert outcome.code == service.FEATURE_OFF
    assert "Document storage is not configured." in outcome.message
    assert Document.objects.count() == 0
    assert refused_rows()[-1].after["reason"] == service.FEATURE_OFF


def test_a_viewer_cannot_upload_through_the_service(store: Store, viewer_user: User) -> None:
    with pytest.raises(PermissionDenied):
        do_upload(store, viewer_user, "a.png", PNG)
    assert Document.objects.count() == 0


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_uploads_of_one_file_to_one_record_make_one_row(
    storage: MemoryDocumentStorage,
) -> None:
    store = saved(new_store())
    user = make_user(f"billing-{uuid.uuid4().hex[:8]}", Role.BILLING)[0]
    outcomes = _race(
        lambda: do_upload(store, user, "a.png", PNG), lambda: do_upload(store, user, "b.png", PNG)
    )
    assert sorted(outcome.ok for outcome in outcomes) == [False, True]
    assert [outcome.code for outcome in outcomes if not outcome.ok] == [service.DUPLICATE]
    assert Document.objects.count() == 1
    assert len(storage.blobs) == 1


# --- Open, delete, list (D3, D7, D8) ------------------------------------------------------


def _uploaded(owner: Store | Lease, actor: User, name: str, data: bytes) -> Document:
    outcome = do_upload(owner, actor, name, data)
    assert outcome.ok and outcome.document is not None
    return outcome.document


def test_download_streams_the_same_bytes_as_an_attachment(
    store: Store, billing_user: User, viewer_user: User
) -> None:
    data = make_pdf([["Lease agreement"]])
    document = _uploaded(store, billing_user, "Lease 2026.pdf", data)
    opened = service.open_for(document, viewer_user, inline=False)
    assert hashlib.sha256(b"".join(opened.chunks)).hexdigest() == document.sha256
    assert opened.headers["Content-Disposition"] == 'attachment; filename="Lease 2026.pdf"'
    assert opened.headers["Content-Type"] == "application/pdf"
    assert opened.headers["X-Content-Type-Options"] == "nosniff"
    assert opened.headers["Content-Length"] == str(len(data))
    entry = AuditLog.objects.get(action=service.ACTION_DOWNLOADED)
    assert entry.actor == viewer_user
    assert entry.after["result"] == "ok"
    assert entry.after["sha256_prefix"] == document.sha256[:12]


def test_preview_is_inline_sandboxed_and_audited(
    store: Store, billing_user: User, viewer_user: User
) -> None:
    document = _uploaded(store, billing_user, "plan.png", PNG)
    opened = service.open_for(document, viewer_user, inline=True)
    assert opened.headers["Content-Disposition"] == 'inline; filename="plan.png"'
    assert opened.headers["Content-Security-Policy"] == "sandbox"
    assert opened.headers["Content-Type"] == "image/png"
    assert AuditLog.objects.filter(action=service.ACTION_PREVIEWED).count() == 1


@pytest.mark.parametrize(("name", "data"), [("a.docx", make_docx()), ("a.heic", HEIC)])
def test_docx_and_heic_are_never_previewed(
    name: str, data: bytes, store: Store, billing_user: User
) -> None:
    document = _uploaded(store, billing_user, name, data)
    with pytest.raises(service.DocumentHidden):
        service.open_for(document, billing_user, inline=True)
    assert b"".join(service.open_for(document, billing_user, inline=False).chunks) == data


def test_a_non_ascii_name_is_sent_rfc_5987_encoded(store: Store, billing_user: User) -> None:
    document = _uploaded(store, billing_user, "Bail été.pdf", make_pdf([["x"]]))
    header = service.open_for(document, billing_user, inline=False).headers["Content-Disposition"]
    assert header == "attachment; filename*=utf-8''Bail%20%C3%A9t%C3%A9.pdf"


def test_a_storage_read_failure_raises_and_is_audited(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    document = _uploaded(store, billing_user, "a.png", PNG)
    storage.fail_open = True
    with pytest.raises(StorageUnavailable):
        service.open_for(document, billing_user, inline=False)
    entry = AuditLog.objects.get(action=service.ACTION_DOWNLOADED)
    assert entry.after["result"] == "failed"
    assert entry.after["reason"] == service.STORAGE_UNAVAILABLE


def test_delete_is_soft_hides_from_non_admins_and_keeps_the_blob(
    store: Store,
    billing_user: User,
    admin_user: User,
    storage: MemoryDocumentStorage,
) -> None:
    document = _uploaded(store, billing_user, "a.png", PNG)
    outcome = service.delete(document, "  Uploaded to the wrong store. ", admin_user)
    assert outcome.ok
    document.refresh_from_db()
    assert document.deleted_at is not None
    assert document.deleted_by == admin_user
    assert document.delete_note == "Uploaded to the wrong store."
    assert document.blob_path in storage.blobs
    assert list(service.documents_of(store)) == []
    assert list(service.documents_of(store, include_deleted=True)) == [document]
    with pytest.raises(service.DocumentHidden):
        service.open_for(document, billing_user, inline=False)
    assert b"".join(service.open_for(document, admin_user, inline=False).chunks) == PNG
    assert AuditLog.objects.filter(action=service.ACTION_DELETED).count() == 1


@pytest.mark.parametrize("note", ["", "   ", "\u200b\u2060", "\u00a0\ufeff"])
def test_delete_needs_a_visible_note(
    note: str, store: Store, billing_user: User, admin_user: User
) -> None:
    document = _uploaded(store, billing_user, "a.png", PNG)
    outcome = service.delete(document, note, admin_user)
    assert outcome.code == service.NOTE_REQUIRED
    document.refresh_from_db()
    assert document.deleted_at is None


def test_delete_refuses_payment_data_and_a_second_delete(
    store: Store, billing_user: User, admin_user: User
) -> None:
    document = _uploaded(store, billing_user, "a.png", PNG)
    assert service.delete(document, f"card {CARD}", admin_user).code == service.PAYMENT_DATA
    assert service.delete(document, "Duplicate.", admin_user).ok
    assert service.delete(document, "Again.", admin_user).code == service.ALREADY_DELETED


def test_only_an_admin_deletes(store: Store, billing_user: User) -> None:
    document = _uploaded(store, billing_user, "a.png", PNG)
    with pytest.raises(PermissionDenied):
        service.delete(document, "No.", billing_user)


def test_documents_of_lists_newest_first_and_only_the_owners(
    store: Store, lease: Lease, billing_user: User
) -> None:
    first = _uploaded(store, billing_user, "a.png", PNG)
    second = _uploaded(store, billing_user, "b.jpg", JPEG)
    _uploaded(lease, billing_user, "c.png", PNG)
    Document.objects.filter(pk=first.pk).update(uploaded_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert list(service.documents_of(store)) == [second, first]


# --- Migration (D4) -----------------------------------------------------------------------

reshape = importlib.import_module("ledger.migrations.0015_documents_reshape")


def test_the_reshape_refuses_to_run_over_a_row(store: Store, billing_user: User) -> None:
    from django.apps import apps

    # The check only reads through the editor's connection.
    editor: Any = SimpleNamespace(connection=connection)
    reshape.refuse_when_rows_exist(apps, editor)
    _uploaded(store, billing_user, "a.png", PNG)
    with pytest.raises(RuntimeError, match="refuses to run over existing data"):
        reshape.refuse_when_rows_exist(apps, editor)


def test_the_reshape_guards_the_new_text_columns() -> None:
    assert reshape.GUARDED_COLUMNS == {
        "documents": ("original_filename", "blob_path", "note", "delete_note")
    }


# --- Views (D7) ---------------------------------------------------------------------------


def store_url(store: Store) -> str:
    return reverse("ledger:stores:detail", kwargs={"pk": store.pk})


def store_documents(store: Store) -> str:
    """The store page's Documents tab (PLAN-012d D3), where the section is."""
    return f"{store_url(store)}?tab=documents"


def lease_url(lease: Lease) -> str:
    return reverse("ledger:leases:detail", kwargs={"pk": lease.pk})


def upload_url(owner: Store | Lease) -> str:
    name = "upload_store" if isinstance(owner, Store) else "upload_lease"
    return reverse(f"ledger:documents:{name}", kwargs={"pk": owner.pk})


def post_file(client: Client, owner: Store | Lease, name: str, data: bytes, **fields: str) -> Any:
    payload = {"kind": "other", "note": "", **fields, "file": upload_file(name, data)}
    return client.post(upload_url(owner), payload)


def streamed(response: Any) -> bytes:
    return b"".join(response.streaming_content)


def page_messages(client: Client, url: str) -> str:
    return client.get(url).content.decode()


@pytest.mark.parametrize("owner_kind", ["store", "lease"])
def test_billing_uploads_from_the_page_and_the_list_shows_it(owner_kind: str) -> None:
    owner = saved(new_store()) if owner_kind == "store" else saved(new_lease())
    client, _ = client_of(Role.BILLING)
    response = post_file(client, owner, "Floor plan.png", PNG, kind="floor_plan", note="Ground")
    detail = store_documents(owner) if isinstance(owner, Store) else lease_url(owner)
    assert response.status_code == 302
    # PLAN-012d D8: a store's upload returns to its Documents tab.
    anchor = "#tab-documents" if isinstance(owner, Store) else "#documents"
    assert response["Location"] == detail + anchor
    page = page_messages(client, detail)
    assert "Document uploaded." in page
    assert 'id="documents-list"' in page
    assert "Floor plan.png" in page and "Floor plan" in page and has_token(page, "Ground")
    document = Document.objects.get()
    assert reverse("ledger:documents:download", kwargs={"pk": document.pk}) in page
    assert f"?preview={document.pk}" in page
    assert "Do not upload bank forms, ACH authorisations, voided cheques or card details." in page
    assert "Those stay in Stax or DocuSign." in page
    assert 'enctype="multipart/form-data"' in page
    assert "ledger/document_drop.js" in page


def test_a_refused_upload_says_why_on_the_page() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    post_file(client, store, "plan.pdf", PNG)
    assert "does not match its content" in page_messages(client, store_url(store))
    assert Document.objects.count() == 0


@pytest.mark.parametrize(
    ("fields", "code", "sentence"),
    [
        ({"note": f"Card {CARD}"}, service.PAYMENT_DATA, PAYMENT_DATA_MESSAGE),
        ({"note": "n" * 2001}, service.NOTE_TOO_LONG, "at most 2000 characters"),
        ({"kind": "bank_form"}, service.KIND_UNKNOWN, "Choose what kind of document"),
    ],
)
def test_a_refused_note_or_kind_from_the_page_is_audited_and_never_echoed(
    fields: dict[str, str], code: str, sentence: str, storage: MemoryDocumentStorage
) -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    post_file(client, store, "a.png", PNG, **fields)
    page = page_messages(client, store_url(store))
    assert sentence in page
    assert CARD not in page
    assert_nothing_stored(storage, code)
    no_payment_data_in_the_audit_log()


def test_an_upload_without_a_file_says_so() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    response = client.post(upload_url(store), {"kind": "other"})
    assert response.status_code == 302
    assert "Choose a file to upload." in page_messages(client, store_url(store))


def test_a_viewer_sees_the_list_but_no_upload_box_and_cannot_post() -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    post_file(billing, store, "a.png", PNG)
    viewer, _ = client_of(Role.VIEWER)
    page = page_messages(viewer, store_documents(store))
    assert "a.png" in page
    assert 'id="document-upload"' not in page
    assert not has_token(page.split('id="documents-list"')[1], "Delete")
    assert post_file(viewer, store, "b.jpg", JPEG).status_code == 403
    assert Document.objects.count() == 1


def test_viewer_downloads_and_previews_through_the_app() -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    data = make_pdf([["Lease agreement"]])
    post_file(billing, store, "lease.pdf", data)
    document = Document.objects.get()
    viewer, _ = client_of(Role.VIEWER)
    download = viewer.get(reverse("ledger:documents:download", kwargs={"pk": document.pk}))
    assert download.status_code == 200
    assert streamed(download) == data
    assert download["Content-Disposition"] == 'attachment; filename="lease.pdf"'
    assert download["X-Content-Type-Options"] == "nosniff"
    assert download["X-Frame-Options"] == "DENY"
    preview = viewer.get(reverse("ledger:documents:preview", kwargs={"pk": document.pk}))
    assert preview.status_code == 200
    assert streamed(preview) == data
    assert preview["Content-Type"] == "application/pdf"
    assert preview["Content-Disposition"] == 'inline; filename="lease.pdf"'
    assert preview["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'self'"
    assert preview["X-Content-Type-Options"] == "nosniff"
    assert preview["X-Frame-Options"] == "SAMEORIGIN"
    assert "blob.core" not in str(download.headers) + str(preview.headers)


#: Finding 1 on Batch A: the header set of every streamed response, by type and route.
#: A PDF preview has no ``sandbox`` (the browser's PDF viewer stays blank in a sandboxed
#: document); everything else keeps it.
PDF_PREVIEW_POLICY = "default-src 'none'; frame-ancestors 'self'"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HEADER_SETS = [
    ("preview", "lease.pdf", make_pdf([["Lease"]]), "application/pdf", PDF_PREVIEW_POLICY),
    ("preview", "plan.png", PNG, "image/png", "sandbox"),
    ("preview", "front.jpg", JPEG, "image/jpeg", "sandbox"),
    ("download", "lease.pdf", make_pdf([["Lease"]]), "application/pdf", "sandbox"),
    ("download", "plan.png", PNG, "image/png", "sandbox"),
    ("download", "front.jpg", JPEG, "image/jpeg", "sandbox"),
    ("download", "contract.docx", make_docx(), DOCX_TYPE, "sandbox"),
    ("download", "front.heic", HEIC, "image/heic", "sandbox"),
]


@pytest.mark.parametrize(("route", "name", "data", "content_type", "policy"), HEADER_SETS)
def test_each_type_and_route_has_its_header_set(
    route: str, name: str, data: bytes, content_type: str, policy: str
) -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    post_file(billing, store, name, data)
    document = Document.objects.get()
    response = billing.get(reverse(f"ledger:documents:{route}", kwargs={"pk": document.pk}))
    assert response.status_code == 200
    assert streamed(response) == data
    disposition = "inline" if route == "preview" else "attachment"
    assert {
        header: response[header]
        for header in (
            "Content-Type",
            "Content-Disposition",
            "Content-Security-Policy",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Cache-Control",
        )
    } == {
        "Content-Type": content_type,
        "Content-Disposition": f'{disposition}; filename="{name}"',
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN" if route == "preview" else "DENY",
        "Cache-Control": "private, no-store",
    }
    if policy != "sandbox":
        # The PDF preview's policy loads and runs nothing from the file.
        assert "sandbox" not in policy
        assert policy.startswith("default-src 'none';")


def test_the_policy_is_sandbox_for_everything_but_a_pdf_preview() -> None:
    for content_type in ("application/pdf", "image/png", "image/jpeg", "image/heic", DOCX_TYPE):
        assert service.content_security_policy(content_type, inline=False) == "sandbox"
    for content_type in ("image/png", "image/jpeg"):
        assert service.content_security_policy(content_type, inline=True) == "sandbox"
    assert service.content_security_policy("application/pdf", inline=True) == PDF_PREVIEW_POLICY


def test_the_page_frames_the_preview_of_a_previewable_document_only() -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    post_file(billing, store, "plan.png", PNG)
    post_file(billing, store, "contract.docx", make_docx())
    png = Document.objects.get(content_type="image/png")
    docx = Document.objects.get(content_type__contains="word")
    page = page_messages(billing, f"{store_url(store)}?preview={png.pk}")
    assert reverse("ledger:documents:preview", kwargs={"pk": png.pk}) in page
    assert "<iframe" in page
    assert f"?preview={docx.pk}" not in page
    for value in (str(docx.pk), "not-a-uuid"):
        assert "<iframe" not in page_messages(billing, f"{store_url(store)}?preview={value}")


def test_preview_of_a_docx_is_404_and_download_works() -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    post_file(billing, store, "contract.docx", make_docx())
    document = Document.objects.get()
    assert (
        billing.get(reverse("ledger:documents:preview", kwargs={"pk": document.pk})).status_code
        == 404
    )
    assert (
        billing.get(reverse("ledger:documents:download", kwargs={"pk": document.pk})).status_code
        == 200
    )


def test_a_user_without_a_role_gets_403_and_an_anonymous_one_401() -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    post_file(billing, store, "a.png", PNG)
    url = reverse("ledger:documents:download", kwargs={"pk": Document.objects.get().pk})
    no_role, _ = client_of(None)
    assert no_role.get(url).status_code == 403
    assert Client().get(url).status_code == 401
    assert AuditLog.objects.filter(action=service.ACTION_DOWNLOADED).count() == 0


@pytest.mark.parametrize("pk", ["not-a-uuid", "..%2F..%2Fetc%2Fpasswd", str(uuid.uuid4())])
def test_a_bad_or_unknown_document_id_is_404(pk: str) -> None:
    viewer, _ = client_of(Role.VIEWER)
    assert viewer.get(f"/documents/{pk}/download/").status_code == 404
    assert viewer.get(f"/documents/{pk}/preview/").status_code == 404


def test_delete_from_the_page_needs_admin_and_a_note() -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    post_file(billing, store, "a.png", PNG)
    document = Document.objects.get()
    url = reverse("ledger:documents:delete", kwargs={"pk": document.pk})
    assert billing.post(url, {"note": "Mine."}).status_code == 403
    admin, _ = client_of(Role.ADMIN)
    assert admin.post(url, {"note": "\u200b"}).status_code == 302
    assert "A note is required" in page_messages(admin, store_url(store))
    document.refresh_from_db()
    assert document.deleted_at is None
    assert admin.post(url, {"note": "Wrong store."}).status_code == 302
    assert Document.objects.get(pk=document.pk).deleted_at is not None
    admin_page = page_messages(admin, store_documents(store))
    assert "Document deleted." in admin_page and "Wrong store." in admin_page
    assert "a.png" not in page_messages(billing, store_documents(store))
    download = reverse("ledger:documents:download", kwargs={"pk": document.pk})
    assert billing.get(download).status_code == 404
    assert admin.get(download).status_code == 200


def test_the_routes_refuse_the_wrong_method() -> None:
    store = saved(new_store())
    admin, _ = client_of(Role.ADMIN)
    assert admin.get(upload_url(store)).status_code == 405
    pk = uuid.uuid4()
    assert admin.get(reverse("ledger:documents:delete", kwargs={"pk": pk})).status_code == 405
    assert admin.post(reverse("ledger:documents:download", kwargs={"pk": pk})).status_code == 403


def test_a_storage_read_failure_shows_a_sentence(storage: MemoryDocumentStorage) -> None:
    store = saved(new_store())
    billing, _ = client_of(Role.BILLING)
    post_file(billing, store, "a.png", PNG)
    storage.fail_open = True
    document = Document.objects.get()
    for name in ("download", "preview"):
        response = billing.get(reverse(f"ledger:documents:{name}", kwargs={"pk": document.pk}))
        assert response.status_code == 503
        assert "could not be read from storage" in response.content.decode()
    assert AuditLog.objects.filter(after__result="failed").count() == 2


def test_feature_off_is_rendered_and_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents_storage, "get_storage", lambda: None)
    store = saved(new_store())
    lease = saved(new_lease())
    admin, _ = client_of(Role.ADMIN)
    for url in (store_documents(store), lease_url(lease), "/stores/new/", "/leases/new/"):
        page = page_messages(admin, url)
        assert "Document storage is not configured." in page, url
        assert 'type="file"' not in page, url
    post_file(admin, store, "a.png", PNG)
    assert "Document storage is not configured." in page_messages(admin, store_documents(store))
    assert Document.objects.count() == 0


def store_create_data(**overrides: Any) -> dict[str, Any]:
    return {
        "creation_key": str(uuid.uuid4()),
        "store_number": "321",
        "external_label": "Store 0321",
        "city": "Springfield",
        "state": "IL",
        "open_date": "2025-03-01",
        "closed_at": "",
        "status": "active",
        # PLAN-012e-2 D9: the New store form's first cost row (the rent is required).
        "landlord_rent": "0.00",
        **overrides,
    }


def test_store_create_uploads_its_file_after_saving() -> None:
    admin, _ = client_of(Role.ADMIN)
    data = store_create_data(
        **{
            "document-file": upload_file("plan.png", PNG),
            "document-kind": "floor_plan",
            "document-note": "Ground floor",
        }
    )
    response = admin.post("/stores/new/", data)
    store = Store.objects.get(external_label="Store 0321")
    assert response["Location"] == store_url(store)
    document = Document.objects.get()
    assert document.store == store and document.kind == "floor_plan"
    page = page_messages(admin, store_url(store))
    # PLAN-012f-b Batch B: the owner's wording for a new store.
    assert "Store #321 Store 0321 added." in page and "Document uploaded." in page


def test_a_refused_file_never_undoes_the_store_and_the_page_says_why() -> None:
    admin, _ = client_of(Role.ADMIN)
    data = store_create_data(
        **{"document-file": upload_file("plan.pdf", PNG), "document-kind": "floor_plan"}
    )
    admin.post("/stores/new/", data)
    store = Store.objects.get(external_label="Store 0321")
    assert Document.objects.count() == 0
    page = page_messages(admin, store_url(store))
    assert "The store was created, but the file was not uploaded:" in page
    assert "does not match its content" in page


def test_store_create_without_a_file_uploads_nothing() -> None:
    admin, _ = client_of(Role.ADMIN)
    admin.post("/stores/new/", store_create_data())
    assert Store.objects.filter(external_label="Store 0321").exists()
    assert not AuditLog.objects.filter(action__startswith="document.").exists()


def test_the_create_forms_offer_the_optional_file() -> None:
    admin, _ = client_of(Role.ADMIN)
    for url in ("/stores/new/", "/leases/new/"):
        page = page_messages(admin, url)
        assert 'enctype="multipart/form-data"' in page, url
        assert 'name="document-file"' in page, url
        assert "Those stay in Stax or DocuSign." in page, url


def test_lease_create_uploads_its_file_and_a_refused_one_keeps_the_lease() -> None:
    from tests.test_lease_views import form_data as lease_form_data

    admin, _ = client_of(Role.ADMIN)
    good = lease_form_data(
        **{
            "document-file": upload_file("lease.pdf", make_pdf([["Lease"]])),
            "document-kind": "lease_agreement",
        }
    )
    admin.post("/leases/new/", good)
    assert Document.objects.get().lease is not None
    bad = {
        **good,
        "creation_key": str(uuid.uuid4()),
        "store": str(saved(new_store()).pk),
        "document-file": upload_file("card.pdf", make_pdf([[CARD]])),
    }
    response = admin.post("/leases/new/", bad)
    assert Lease.objects.count() == 2
    assert Document.objects.count() == 1
    page = page_messages(admin, response["Location"])
    assert "The lease was created, but the file was not uploaded:" in page
    assert PAYMENT_DATA_MESSAGE in page
    no_payment_data_in_the_audit_log()


# --- Upload limits (D3) ---------------------------------------------------------------------


class _UnreadableInput(io.RawIOBase):
    """A request body that fails the test if anything reads it."""

    def read(self, *args: Any) -> bytes:
        raise AssertionError("the body was read")

    readline = read


def unreadable_body() -> Any:
    return {"wsgi.input": _UnreadableInput()}


def test_the_upload_handler_list_is_the_capped_in_memory_handler_only() -> None:
    from django.conf import settings

    assert settings.FILE_UPLOAD_HANDLERS == ["finance.uploads.CappedMemoryFileUploadHandler"]
    assert issubclass(CappedMemoryFileUploadHandler, MemoryFileUploadHandler)
    assert not issubclass(CappedMemoryFileUploadHandler, TemporaryFileUploadHandler)
    assert settings.FILE_UPLOAD_MAX_MEMORY_SIZE == settings.UPLOAD_MAX_REQUEST_BYTES
    assert settings.UPLOAD_MAX_REQUEST_BYTES == MAX + MB
    assert settings.DOCUMENT_MAX_BYTES == MAX
    # Finding 2 on Batch A: everything else keeps Django's default of 2.5 MiB.
    assert settings.REQUEST_MAX_BYTES == SMALL == 2_621_440
    assert settings.DATA_UPLOAD_MAX_MEMORY_SIZE == SMALL


@pytest.fixture
def no_temporary_files(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a temporary file was created")

    for name in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, name, refuse)
    monkeypatch.setattr("django.core.files.temp.NamedTemporaryFile", refuse)
    monkeypatch.setattr("django.core.files.uploadedfile.tempfile.NamedTemporaryFile", refuse)
    yield


def test_a_large_upload_creates_no_temporary_file(no_temporary_files: None) -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    data = PNG + b"\x00" * (3 * MB)
    response = post_file(client, store, "large.png", data)
    assert response.status_code == 302
    assert Document.objects.get().size_bytes == len(data)


def test_a_file_of_25_mb_and_one_byte_is_refused_through_the_page() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    data = PNG + b"\x00" * (MAX + 1 - len(PNG))
    response = post_file(client, store, "big.png", data)
    assert response.status_code == 302
    assert "The file is larger than 25 MB." in page_messages(client, store_url(store))
    assert Document.objects.count() == 0


def test_a_declared_length_over_the_limit_is_refused_before_the_body_is_read() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    response = client.generic(
        "POST",
        upload_url(store),
        b"",
        content_type="multipart/form-data; boundary=x",
        CONTENT_LENGTH=str(30 * MB),
        **unreadable_body(),
    )
    assert response.status_code == 413
    assert "at most 25 MB" in response.content.decode()


def test_a_body_without_a_length_is_read_as_empty() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    response = client.generic(
        "POST",
        upload_url(store),
        b"",
        content_type="multipart/form-data; boundary=x",
        CONTENT_LENGTH="",
        **unreadable_body(),
    )
    # Without a length the body is never read (the input would fail the test): no file
    # arrives, and the page says so.
    assert response.status_code == 302
    assert "Choose a file to upload." in page_messages(client, store_url(store))
    assert Document.objects.count() == 0


def test_the_handler_stops_an_upload_past_the_cap_while_reading() -> None:
    store = saved(new_store())
    request = RequestFactory().post(upload_url(store))
    with override_settings(UPLOAD_MAX_REQUEST_BYTES=10):
        handler = CappedMemoryFileUploadHandler(request)
        assert handler.limit == 10
        with pytest.raises(StopUpload) as raised:
            handler.handle_raw_input(None, {}, 11, b"x")
        assert raised.value.connection_reset
        handler = CappedMemoryFileUploadHandler(request)
        handler.handle_raw_input(None, {}, 5, b"x")
        with pytest.raises(StopFutureHandlers):
            handler.new_file("file", "a.png", "image/png", 5)
        assert handler.receive_data_chunk(b"123456", 0) is None
        with pytest.raises(StopUpload) as raised:
            handler.receive_data_chunk(b"12345", 6)
        assert raised.value.connection_reset


def test_two_files_in_one_request_are_refused() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.BILLING)
    response = client.post(
        upload_url(store),
        {"kind": "other", "file": upload_file("a.png", PNG), "other": upload_file("b.jpg", JPEG)},
    )
    assert response.status_code == 400
    assert Document.objects.count() == 0


# --- The large limit is for the document routes only (finding 2 on Batch A) ----------------


_PK = "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    ("path", "name", "large"),
    [
        (f"/documents/stores/{_PK}/upload/", "ledger:documents:upload_store", True),
        (f"/documents/leases/{_PK}/upload/", "ledger:documents:upload_lease", True),
        ("/stores/new/", "ledger:stores:create", True),
        ("/leases/new/", "ledger:leases:create", True),
        (f"/stores/{_PK}/edit/", "ledger:stores:edit", False),
        (f"/leases/{_PK}/edit/", "ledger:leases:edit", False),
        (f"/documents/{_PK}/delete/", "ledger:documents:delete", False),
        ("/account/login/", "two_factor:login", False),
        ("/api/webhooks/stax/x", "webhooks:receive", False),
    ],
)
def test_request_limit_by_route(path: str, name: str, large: bool) -> None:
    from django.urls import resolve

    from finance.uploads import LARGE_UPLOAD_ROUTE_NAMES, request_limit

    assert resolve(path).view_name == name
    assert request_limit(RequestFactory().post(path)) == (MAX + MB if large else SMALL)
    assert (name in LARGE_UPLOAD_ROUTE_NAMES) is large


def test_the_large_routes_are_a_closed_list_of_four() -> None:
    from finance.uploads import LARGE_UPLOAD_ROUTE_NAMES

    assert LARGE_UPLOAD_ROUTE_NAMES == {
        "ledger:documents:upload_store",
        "ledger:documents:upload_lease",
        "ledger:stores:create",
        "ledger:leases:create",
    }


def test_an_unknown_path_or_no_request_gets_the_small_limit() -> None:
    from finance.uploads import request_limit

    assert request_limit(RequestFactory().post("/no-such-route/")) == SMALL
    assert request_limit(None) == SMALL


def test_an_unauthenticated_post_to_the_login_form_over_2_5_mib_is_413_unread() -> None:
    response = Client().generic(
        "POST",
        "/account/login/",
        b"",
        content_type="application/x-www-form-urlencoded",
        CONTENT_LENGTH=str(SMALL + 1),
        **unreadable_body(),
    )
    assert response.status_code == 413


def test_an_unauthenticated_post_to_the_login_form_at_the_limit_is_read() -> None:
    response = Client().generic(
        "POST",
        "/account/login/",
        b"auth-username=" + b"a" * (SMALL - len(b"auth-username=")),
        content_type="application/x-www-form-urlencoded",
    )
    assert response.status_code != 413


def test_a_signed_in_post_to_another_route_over_2_5_mib_is_413_unread() -> None:
    store = saved(new_store())
    client, _ = client_of(Role.ADMIN)
    response = client.generic(
        "POST",
        reverse("ledger:stores:edit", kwargs={"pk": store.pk}),
        b"",
        content_type="multipart/form-data; boundary=x",
        CONTENT_LENGTH=str(SMALL + 1),
        **unreadable_body(),
    )
    assert response.status_code == 413


@pytest.mark.parametrize("route", ["upload", "store_create", "lease_create"])
def test_a_document_route_accepts_a_body_over_2_5_mib(route: str) -> None:
    store = saved(new_store())
    client, _ = client_of(Role.ADMIN)
    data = PNG + b"\x00" * (3 * MB)
    if route == "upload":
        response = post_file(client, store, "large.png", data)
    else:
        url = reverse("ledger:stores:create" if route == "store_create" else "ledger:leases:create")
        response = client.post(
            url, {"document-kind": "other", "document-file": upload_file("large.png", data)}
        )
    assert response.status_code == 302
    if route == "upload":
        assert Document.objects.get().size_bytes == len(data)


def test_a_file_over_2_5_mib_to_another_route_is_stopped_while_reading() -> None:
    """A multipart body whose length is under-declared on a small route: the handler
    stops at 2.5 MiB, whatever the stream holds."""
    request = RequestFactory().post(reverse("ledger:stores:edit", kwargs={"pk": uuid.uuid4()}))
    handler = CappedMemoryFileUploadHandler(request)
    assert handler.limit == SMALL
    handler.handle_raw_input(None, {}, SMALL, b"x")
    with pytest.raises(StopFutureHandlers):
        handler.new_file("file", "a.png", "image/png", SMALL)
    handler.receive_data_chunk(b"\x00" * SMALL, 0)
    with pytest.raises(StopUpload):
        handler.receive_data_chunk(b"\x00", SMALL)


def test_the_handler_on_a_document_route_allows_up_to_the_large_limit() -> None:
    store = saved(new_store())
    handler = CappedMemoryFileUploadHandler(RequestFactory().post(upload_url(store)))
    assert handler.limit == MAX + MB
    with pytest.raises(StopUpload):
        handler.handle_raw_input(None, {}, MAX + MB + 1, b"x")


# --- The SHA-256 column holds a digest's shape (finding 3 on Batch A) -----------------------


def _document_row(store: Store, sha256: str) -> Document:
    document_id = uuid.uuid4()
    return Document(
        id=document_id,
        store=store,
        kind="other",
        original_filename="a.png",
        content_type="image/png",
        size_bytes=1,
        blob_path=f"documents/store/{store.pk}/{document_id}",
        sha256=sha256,
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "a" * 63 + "\n",
        "a" * 64 + "\n",
        "\n" + "a" * 64,
        " " + "a" * 63,
        "a" * 32 + "-" + "a" * 31,
        "\u0661" * 64,
    ],
)
def test_the_sha256_check_refuses_anything_but_64_lower_case_hex_characters(
    value: str, store: Store
) -> None:
    from django.db import DataError, IntegrityError, transaction

    # PostgreSQL refuses a value over 64 characters by the column's length (DataError)
    # before the check runs; sqlite has no length limit, so the check refuses it there.
    expected = (
        DataError if connection.vendor == "postgresql" and len(value) > 64 else IntegrityError
    )
    row = _document_row(store, value)
    with pytest.raises(expected), transaction.atomic():
        row.save(force_insert=True)
    assert Document.objects.count() == 0


def test_the_sha256_check_accepts_a_digest(store: Store) -> None:
    _document_row(store, hashlib.sha256(b"x").hexdigest()).save(force_insert=True)
    assert Document.objects.count() == 1


# --- Text outside the page text, embedded files (QA adversarial findings 1 to 3) ------------

PAIR_TEXT = f"Routing {ROUTING} Account {ACCOUNT}"


def _pdf(build: Any = None, *, lines: list[str] | None = None) -> bytes:
    """A one-page PDF, blank or with ``lines`` of page text, changed by ``build(writer,
    page)`` before it is written."""
    if lines is None:
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
    else:
        writer = PdfWriter(clone_from=io.BytesIO(make_pdf([lines])))
        page = writer.pages[0]
    if build is not None:
        build(writer, page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _name(value: str) -> NameObject:
    return NameObject(value)


def _form(writer: PdfWriter, page: Any, *fields: DictionaryObject) -> list[Any]:
    """Adds ``fields`` as the form's top-level fields and as the page's widgets."""
    references = [writer._add_object(field) for field in fields]
    writer._root_object[_name("/AcroForm")] = DictionaryObject(
        {_name("/Fields"): ArrayObject(references)}
    )
    page[_name("/Annots")] = ArrayObject(references)
    return references


def _field(**entries: Any) -> DictionaryObject:
    field = DictionaryObject({_name("/FT"): _name("/Tx"), _name("/T"): TextStringObject("f")})
    for key, value in entries.items():
        field[_name(f"/{key}")] = value
    return field


def _pdf_refused(data: bytes, code: str = service.PAYMENT_DATA) -> bool:
    text = _text("a.pdf", data)
    if isinstance(text, service.Refusal):
        return text.code == code
    return code == service.PAYMENT_DATA and service._holds_payment_data(text)


def test_a_number_in_a_nested_kid_field_is_refused() -> None:
    def build(writer: PdfWriter, page: Any) -> None:
        child = writer._add_object(_field(T=TextStringObject("card"), V=TextStringObject(CARD)))
        middle = writer._add_object(DictionaryObject({_name("/Kids"): ArrayObject([child])}))
        parent = DictionaryObject(
            {_name("/T"): TextStringObject("bank"), _name("/Kids"): ArrayObject([middle])}
        )
        writer._root_object[_name("/AcroForm")] = DictionaryObject(
            {_name("/Fields"): ArrayObject([writer._add_object(parent)])}
        )

    text = _text("a.pdf", _pdf(build))
    assert isinstance(text, str) and CARD in text
    assert _pdf_refused(_pdf(build))


@pytest.mark.parametrize(
    "options",
    [
        ArrayObject([TextStringObject("none"), TextStringObject(CARD)]),
        ArrayObject(
            [ArrayObject([TextStringObject(CARD_DIGITS), TextStringObject("Visa ending 1111")])]
        ),
    ],
    ids=["plain", "export-and-display"],
)
def test_a_number_in_a_choice_fields_options_is_refused(options: ArrayObject) -> None:
    def build(writer: PdfWriter, page: Any) -> None:
        _form(writer, page, _field(FT=_name("/Ch"), Opt=options))

    assert _pdf_refused(_pdf(build))


def test_a_number_in_a_fields_default_value_only_is_refused() -> None:
    def build(writer: PdfWriter, page: Any) -> None:
        _form(writer, page, _field(DV=TextStringObject(CARD)))

    assert _pdf_refused(_pdf(build))


def test_a_number_in_an_annotations_rich_text_only_is_refused() -> None:
    rich = f'<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml"><p>{CARD}</p></body>'

    def build(writer: PdfWriter, page: Any) -> None:
        note = DictionaryObject(
            {
                _name("/Type"): _name("/Annot"),
                _name("/Subtype"): _name("/FreeText"),
                _name("/RC"): TextStringObject(rich),
                _name("/T"): TextStringObject("Reviewer"),
            }
        )
        page[_name("/Annots")] = ArrayObject([writer._add_object(note)])

    assert _pdf_refused(_pdf(build))


@pytest.mark.parametrize(
    "value", ["4111-1111-1111-1111", "4111\n1111\n1111\n1111", "4111 1111\r\n1111 1111"]
)
def test_a_number_split_inside_a_fields_value_and_nowhere_else_is_refused(value: str) -> None:
    def build(writer: PdfWriter, page: Any) -> None:
        _form(writer, page, _field(V=TextStringObject(value)))

    data = _pdf(build, lines=["Card on file: see the form field."])
    assert _pdf_refused(data)


def test_a_routing_and_an_account_number_in_neighbouring_fields_are_refused() -> None:
    def build(writer: PdfWriter, page: Any) -> None:
        _form(
            writer,
            page,
            _field(T=TextStringObject("rtn"), V=TextStringObject(ROUTING)),
            _field(T=TextStringObject("acct"), V=TextStringObject(ACCOUNT)),
        )

    assert _pdf_refused(_pdf(build, lines=["Routing number", "Account number"]))


def test_a_number_in_a_custom_document_property_or_the_xmp_metadata_is_refused() -> None:
    def info(writer: PdfWriter, page: Any) -> None:
        writer.add_metadata({"/BankAccount": PAIR_TEXT})

    def xmp(writer: PdfWriter, page: Any) -> None:
        stream = DecodedStreamObject()
        stream.set_data(_xmp(f'<rdf:Description pdf:Keywords="{CARD}"/>'))
        stream[_name("/Type")] = _name("/Metadata")
        stream[_name("/Subtype")] = _name("/XML")
        writer._root_object[_name("/Metadata")] = writer._add_object(stream.flate_encode())

    def xmp_element(writer: PdfWriter, page: Any) -> None:
        stream = DecodedStreamObject()
        stream.set_data(_xmp(f"<rdf:Description><dc:title>{CARD}</dc:title></rdf:Description>"))
        writer._root_object[_name("/Metadata")] = writer._add_object(stream)

    for build in (info, xmp, xmp_element):
        assert _pdf_refused(_pdf(build)), build.__name__


def _xmp(description: str) -> bytes:
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f"{description}</rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    ).encode()


def test_xmp_metadata_past_the_cap_is_skipped_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def build(writer: PdfWriter, page: Any) -> None:
        stream = DecodedStreamObject()
        stream.set_data(_xmp(f"<dc:title>{CARD}</dc:title>" + " " * 2_000))
        writer._root_object[_name("/Metadata")] = writer._add_object(stream.flate_encode())

    data = _pdf(build)
    assert _pdf_refused(data)
    monkeypatch.setattr(service, "PDF_STREAM_MAX_BYTES", 1_000)
    text = _text("a.pdf", data)
    assert isinstance(text, str) and CARD not in text


def test_a_pdf_date_is_read_as_a_date_and_never_as_a_card_number() -> None:
    # 20240315123008 is a valid date and passes the Luhn check as fourteen digits.
    def build(writer: PdfWriter, page: Any) -> None:
        writer.add_metadata(
            {"/CreationDate": "D:20240315123008Z", "/ModDate": "20240315123008+05'00'"}
        )

    text = _text("a.pdf", _pdf(build))
    assert isinstance(text, str)
    assert "2024-03-15 12:30:08Z" in text and "2024-03-15 12:30:08+05'00'" in text
    assert not service._holds_payment_data(text)
    assert service._readable_pdf_date("D:20241399123008") == "D:20241399123008"
    assert service._readable_pdf_date(f"D:{CARD_DIGITS}") == f"D:{CARD_DIGITS}"


def test_a_number_in_a_title_repeated_in_subject_and_keywords_is_still_refused() -> None:
    def build(writer: PdfWriter, page: Any) -> None:
        writer.add_metadata({"/Title": CARD_DIGITS, "/Subject": CARD_DIGITS, "/Author": "1234"})

    text = _text("a.pdf", _pdf(build))
    assert isinstance(text, str)
    assert f"{CARD_DIGITS}{service.PIECE_SEPARATOR}{CARD_DIGITS}" in text
    assert _pdf_refused(_pdf(build))


def test_an_undecodable_field_is_skipped_and_the_next_one_still_read() -> None:
    def broken(writer: PdfWriter) -> Any:
        stream = DecodedStreamObject()
        stream.set_data(b"not deflate data at all")
        stream[_name("/Filter")] = _name("/FlateDecode")
        return writer._add_object(stream)

    def only_broken(writer: PdfWriter, page: Any) -> None:
        _form(writer, page, _field(V=broken(writer)))

    def broken_then_card(writer: PdfWriter, page: Any) -> None:
        _form(writer, page, _field(V=broken(writer)), _field(V=TextStringObject(CARD)))

    text = _text("a.pdf", _pdf(only_broken, lines=["Lease text."]))
    assert isinstance(text, str) and "Lease text." in text
    assert _pdf_refused(_pdf(broken_then_card, lines=["Lease text."]))


def test_a_field_tree_that_loops_ends_and_a_huge_one_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def looping(writer: PdfWriter, page: Any) -> None:
        field = _field(V=TextStringObject("plain"))
        reference = writer._add_object(field)
        field[_name("/Kids")] = ArrayObject([reference])
        writer._root_object[_name("/AcroForm")] = DictionaryObject(
            {_name("/Fields"): ArrayObject([reference, reference])}
        )
        page[_name("/Annots")] = ArrayObject([reference])

    text = _text("a.pdf", _pdf(looping))
    assert isinstance(text, str) and text.count("plain") == 1

    def many(writer: PdfWriter, page: Any) -> None:
        _form(writer, page, *(_field(V=TextStringObject(str(index))) for index in range(20)))

    monkeypatch.setattr(service, "PDF_MAX_OBJECTS", 10)
    assert _text("a.pdf", _pdf(many)) == service.Refusal(service.TOO_MUCH_TEXT)


def test_a_pdf_with_an_embedded_file_or_a_file_attachment_is_refused(
    store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    def name_tree(writer: PdfWriter, page: Any) -> None:
        writer.add_attachment("statement.txt", b"nothing to see")

    def attachment(writer: PdfWriter, page: Any) -> None:
        annotation = DictionaryObject(
            {
                _name("/Type"): _name("/Annot"),
                _name("/Subtype"): _name("/FileAttachment"),
                _name("/Rect"): ArrayObject([FloatObject(x) for x in (0, 0, 10, 10)]),
            }
        )
        page[_name("/Annots")] = ArrayObject([writer._add_object(annotation)])

    for build in (name_tree, attachment):
        data = _pdf(build, lines=["A lease."])
        assert _text("a.pdf", data) == service.Refusal(service.EMBEDDED_OBJECT)
        outcome = do_upload(store, billing_user, "lease.pdf", data)
        assert not outcome.ok and outcome.code == service.EMBEDDED_OBJECT
    assert_nothing_stored(storage, service.EMBEDDED_OBJECT)


def test_the_embedded_file_message_is_the_plans() -> None:
    assert service.MESSAGES[service.EMBEDDED_OBJECT] == (
        "This document contains an embedded file, which cannot be checked. "
        "Remove it and upload again."
    )


_CORE = (
    '<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties '
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">{}</cp:coreProperties>'
)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("docProps/core.xml", _CORE.format(f"<dc:title>{CARD}</dc:title>")),
        ("docProps/core.xml", _CORE.format(f"<dc:subject>{CARD}</dc:subject>")),
        ("docProps/core.xml", _CORE.format(f"<cp:keywords>{PAIR_TEXT}</cp:keywords>")),
        ("docProps/core.xml", _CORE.format(f"<dc:description>{CARD}</dc:description>")),
        (
            "docProps/custom.xml",
            '<?xml version="1.0"?><Properties><property fmtid="{D5CDD505}" pid="2" '
            f'name="Bank"><vt:lpwstr>{PAIR_TEXT}</vt:lpwstr></property></Properties>',
        ),
        ("customXml/item1.xml", f"<root><anything>{CARD}</anything></root>"),
    ],
    ids=["title", "subject", "keywords", "description", "custom", "any-element"],
)
def test_a_number_in_docx_properties_or_any_element_is_refused(name: str, content: str) -> None:
    text = _text("a.docx", make_docx(extra={name: content}))
    assert isinstance(text, str) and service._holds_payment_data(text)


@pytest.mark.parametrize(
    "body",
    [
        f'<w:p><w:r><w:drawing><wp:docPr id="1" name="P" title="{CARD}"/></w:drawing></w:r></w:p>',
        f'<w:p><w:r><w:drawing><wp:docPr id="1" name="{CARD}"/></w:drawing></w:r></w:p>',
        f'<w:p><w:r><w:pict><v:shape alt="{CARD}"/></w:pict></w:r></w:p>',
        f'<w:sdt><w:sdtPr><w:alias w:val="{CARD}"/></w:sdtPr></w:sdt>',
        f'<w:sdt><w:sdtPr><w:tag w:val="{PAIR_TEXT}"/></w:sdtPr></w:sdt>',
        f'<w:p><w:hyperlink w:anchor="{CARD_DIGITS}"><w:r><w:t>link</w:t></w:r>'
        "</w:hyperlink></w:p>",
        f'<w:p><w:fldSimple w:instr=" HYPERLINK &quot;{CARD}&quot; "/></w:p>',
    ],
    ids=["docpr-title", "docpr-name", "vml-alt", "alias", "tag", "anchor", "simple-field"],
)
def test_a_number_in_a_docx_attribute_is_refused(body: str) -> None:
    text = _text("a.docx", make_docx(part(paragraph("Lease."), body)))
    assert isinstance(text, str) and service._holds_payment_data(text)


def test_a_number_in_a_relationship_target_is_refused_and_other_attributes_are_not_read() -> None:
    rels = (
        '<?xml version="1.0"?><Relationships><Relationship Id="rId9" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="https://example.com/pay?card={CARD_DIGITS}" TargetMode="External"/>'
        "</Relationships>"
    )
    text = _text("a.docx", make_docx(extra={"word/_rels/document.xml.rels": rels}))
    assert isinstance(text, str) and service._holds_payment_data(text)
    # Only the listed attributes are read: a colour or a font code is not text.
    body = f'<w:p><w:r><w:rPr><w:color w:val="{CARD_DIGITS}"/></w:rPr><w:t>x</w:t></w:r></w:p>'
    text = _text("a.docx", make_docx(part(body)))
    assert isinstance(text, str) and CARD_DIGITS not in text


def test_a_long_documents_statistics_are_not_read_as_a_card_number() -> None:
    # TotalTime, Pages, Words and Characters in a row: 4111 11 1111 11111 joined would be
    # sixteen digits passing the Luhn check. Each element is a piece of its own.
    app = (
        '<?xml version="1.0"?><Properties><Template>Normal.dotm</Template>'
        "<TotalTime>4111</TotalTime><Pages>11</Pages><Words>1111</Words>"
        "<Characters>111111</Characters><Application>Word</Application></Properties>"
    )
    assert service._holds_payment_data("4111\n11\n1111\n111111")
    text = _text("a.docx", make_docx(extra={"docProps/app.xml": app}))
    assert isinstance(text, str) and "4111" in text
    assert not service._holds_payment_data(text)


def test_a_picture_between_two_runs_keeps_the_runs_joined() -> None:
    body = (
        '<w:p><w:r><w:t xml:space="preserve">4111 1111 </w:t></w:r>'
        '<w:r><w:drawing><wp:anchor><wp:positionH relativeFrom="column">'
        '<wp:posOffset>914400</wp:posOffset></wp:positionH><wp:docPr id="1" name="Picture 1"/>'
        "</wp:anchor></w:drawing></w:r>"
        "<w:r><w:t>1111 1111</w:t></w:r></w:p>"
    )
    text = _text("a.docx", make_docx(part(body)))
    assert isinstance(text, str)
    assert text.startswith("4111 1111 1111 1111\n")
    assert "914400" in text and "Picture 1" in text


@pytest.mark.parametrize(
    "entries",
    [
        {"word/embeddings/Microsoft_Excel_Worksheet.xlsx": b"PK\x03\x04 a spreadsheet"},
        {"word/embeddings/oleObject1.bin": b"anything"},
        {"word/activeX/activeX1.bin": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64},
        {"word/other/thing.BIN": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64},
    ],
    ids=["embeddings-xlsx", "embeddings-ole", "activex-ole", "ole-elsewhere"],
)
def test_a_docx_with_an_embedded_file_is_refused(
    entries: dict[str, bytes], store: Store, billing_user: User, storage: MemoryDocumentStorage
) -> None:
    data = make_docx(extra=entries)
    assert service.sniff(data, "a.docx") == service.Refusal(service.EMBEDDED_OBJECT)
    outcome = do_upload(store, billing_user, "a.docx", data)
    assert not outcome.ok and outcome.code == service.EMBEDDED_OBJECT
    assert_nothing_stored(storage, service.EMBEDDED_OBJECT)


def test_printer_settings_are_not_an_embedded_file_and_a_macro_stays_a_macro() -> None:
    printer = {"word/printerSettings/printerSettings1.bin": b"\x00\x01DEVMODE" + b"\x00" * 64}
    assert isinstance(service.sniff(make_docx(extra=printer), "a.docx"), service.Sniffed)
    macro = {
        "word/vbaProject.bin": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "word/embeddings/oleObject1.bin": b"x",
    }
    assert service.sniff(make_docx(extra=macro), "a.docx") == service.Refusal(service.MACRO)
