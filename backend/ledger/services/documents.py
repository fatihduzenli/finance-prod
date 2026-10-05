"""Documents kept with a store or a lease (docs/qa/PLAN-010c-documents-and-cost-corrections.md
D3 to D8).

Views call the functions below; this is the only writer of ``documents`` rows and the
only caller of :func:`documents_storage.get_storage`.

**Upload** checks, in this order, and stops at the first refusal: the feature is on, the
kind is known, the size (more than 0, at most 25 MB), the type decided from the content
(:func:`sniff`), the text (:func:`extract_text`, PDF and DOCX), the payment-data guard
over that text, the filename and the note, then the SHA-256 and the duplicate check
under the owner's row lock, then the blob is stored, then the row and its audit entry
are written in the same transaction. A refusal stores nothing (no blob, no row) and
writes one ``document.upload_refused`` audit row carrying a reason code, the owner,
the size and the filename's length, never the filename or any content.

**Payment-data guard** (D6, docs/CLAUDE.md Rule 1): the extracted text, the filename
(as sent and as stored) and the note go through
:func:`ledger.validators.validate_no_payment_data`, the text guard itself, unchanged.
Line breaks of every kind are first turned into ``\\n`` and the text of a DOCX paragraph
is the runs of that paragraph joined with nothing between them, so the guard's own
separator rule (up to three spaces, dashes or line breaks between digits) is what joins
a number written across lines, runs or pages. The text also holds every other place a
PDF or DOCX keeps text (form fields, annotations, properties, metadata, alt text, link
targets), each piece apart from the next by :data:`PIECE_SEPARATOR`. A file carrying an
embedded file is refused (``embedded_object``): what it holds cannot be checked. Images,
and PDFs without a text layer, cannot be checked; the upload box says so (D6).

**Download and preview** (D3, D7): streamed from the storage in chunks to a user with a
role, through the app only; every one is audited (``document.downloaded``,
``document.previewed``), including a read the storage could not serve. A deleted
document is visible to admins only. Only PDF, PNG and JPEG are ever served ``inline``,
always with ``X-Content-Type-Options: nosniff`` and the type decided at upload. A PNG or
JPEG preview and every download carry ``Content-Security-Policy: sandbox``; a PDF
preview carries ``default-src 'none'; frame-ancestors 'self'`` instead, because the
browser's PDF viewer does not render in a sandboxed document
(:func:`content_security_policy`).

**Delete** (D7): admin only, a note required (judged by :func:`visible_text`), soft: the
row keeps its blob path and the blob is never removed by the app.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import unicodedata
import uuid
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.http import content_disposition_header
from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NumberObject,
    PdfObject,
    StreamObject,
)

import documents_storage
from accounts.roles import Role, meets, role_of
from documents_storage import StorageUnavailable
from ledger.models import (
    DOCUMENT_FILENAME_MAX_LENGTH,
    AuditLog,
    Document,
    DocumentContentType,
    DocumentKind,
    DocumentSource,
    Lease,
    Store,
)
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text

logger = logging.getLogger(__name__)

Owner = Store | Lease

ENTITY_DOCUMENT = "document"
OWNER_STORE = "store"
OWNER_LEASE = "lease"

ACTION_UPLOADED = "document.uploaded"
ACTION_UPLOAD_REFUSED = "document.upload_refused"
ACTION_DOWNLOADED = "document.downloaded"
ACTION_PREVIEWED = "document.previewed"
ACTION_DELETED = "document.deleted"

#: Longest note on an upload or a delete.
NOTE_MAX_LENGTH = 2000
#: D6: a PDF with more pages is refused, as it cannot be checked in reasonable time.
PDF_MAX_PAGES = 500
#: A PDF's XMP metadata, or a text stream of a field or annotation, is read up to this
#: many decoded bytes; a larger one is skipped (that alone does not refuse the file).
PDF_STREAM_MAX_BYTES = 4 * 1024 * 1024
#: Form fields and annotations walked in one PDF; past this the file cannot be checked.
PDF_MAX_OBJECTS = 100_000
#: A DOCX is a zip; each part is read up to this many unpacked bytes (D6) ...
DOCX_PART_MAX_BYTES = 25 * 1024 * 1024
#: ... all parts together up to this many ...
DOCX_TOTAL_MAX_BYTES = 60 * 1024 * 1024
#: ... and the archive may list at most this many entries.
DOCX_MAX_ENTRIES = 2000
#: Extracted text longer than this cannot be checked in reasonable time: refused.
TEXT_MAX_CHARACTERS = 10_000_000
#: Characters of the SHA-256 written to the audit log.
SHA256_PREFIX_LENGTH = 12

# --- Reason codes (audit) and sentences (page) -------------------------------------------

FEATURE_OFF = "feature_off"
KIND_UNKNOWN = "kind_unknown"
EMPTY = "empty"
TOO_LARGE = "too_large"
TYPE_NOT_ACCEPTED = "type_not_accepted"
EXTENSION_MISMATCH = "extension_mismatch"
MACRO = "macro"
DAMAGED = "damaged"
PDF_UNREADABLE = "pdf_unreadable"
PDF_NO_PAGES = "pdf_no_pages"
PDF_TOO_MANY_PAGES = "pdf_too_many_pages"
TOO_MUCH_TEXT = "too_much_text"
NOTE_TOO_LONG = "note_too_long"
EMBEDDED_OBJECT = "embedded_object"
PAYMENT_DATA = "payment_data"
DUPLICATE = "duplicate"
STORAGE_UNAVAILABLE = "storage_unavailable"
NOTE_REQUIRED = "note_required"
ALREADY_DELETED = "already_deleted"

FEATURE_OFF_MESSAGE = "Document storage is not configured."
MESSAGES: dict[str, str] = {
    FEATURE_OFF: f"{FEATURE_OFF_MESSAGE} The file was not uploaded.",
    KIND_UNKNOWN: "Choose what kind of document this is.",
    EMPTY: "The file is empty.",
    TOO_LARGE: "The file is larger than 25 MB.",
    TYPE_NOT_ACCEPTED: (
        "This type of file is not accepted. Upload a PDF, a Word document (.docx), "
        "or a JPEG, PNG or HEIC image."
    ),
    EXTENSION_MISMATCH: (
        "The file's name does not match its content. Check that the file has the right "
        "extension (.pdf, .docx, .jpg, .jpeg, .png or .heic)."
    ),
    MACRO: "Word documents with macros are not accepted.",
    DAMAGED: "This file is damaged and cannot be read.",
    PDF_UNREADABLE: "This PDF cannot be read, so it cannot be checked.",
    PDF_NO_PAGES: "This PDF has no pages.",
    PDF_TOO_MANY_PAGES: f"This PDF has more than {PDF_MAX_PAGES} pages, so it cannot be checked.",
    TOO_MUCH_TEXT: "This file holds too much text to be checked.",
    EMBEDDED_OBJECT: (
        "This document contains an embedded file, which cannot be checked. "
        "Remove it and upload again."
    ),
    NOTE_TOO_LONG: f"The note may be at most {NOTE_MAX_LENGTH} characters.",
    PAYMENT_DATA: PAYMENT_DATA_MESSAGE,
    DUPLICATE: "This file was already uploaded to this record.",
    STORAGE_UNAVAILABLE: "The file could not be stored. Nothing was saved; try again in a minute.",
    NOTE_REQUIRED: "A note is required: say why the document is deleted.",
    ALREADY_DELETED: "This document was already deleted.",
}
READ_FAILED_MESSAGE = "The file could not be read from storage. Try again in a minute."

# --- Types (D5) -----------------------------------------------------------------------------

PDF = "pdf"
DOCX = "docx"
JPEG = "jpeg"
PNG = "png"
HEIC = "heic"

CONTENT_TYPES: dict[str, str] = {
    PDF: DocumentContentType.PDF,
    DOCX: DocumentContentType.DOCX,
    JPEG: DocumentContentType.JPEG,
    PNG: DocumentContentType.PNG,
    HEIC: DocumentContentType.HEIC,
}
#: The extensions each type may carry, lower-case.
EXTENSIONS: dict[str, frozenset[str]] = {
    PDF: frozenset({"pdf"}),
    DOCX: frozenset({"docx"}),
    JPEG: frozenset({"jpg", "jpeg"}),
    PNG: frozenset({"png"}),
    HEIC: frozenset({"heic"}),
}
#: The policy of every streamed response except a PDF preview (D7).
SANDBOX_POLICY = "sandbox"
#: The policy of a PDF preview: no ``sandbox`` (the browser's PDF viewer will not render
#: in a sandboxed document), nothing loaded from the file, framed by this site only.
PDF_PREVIEW_POLICY = "default-src 'none'; frame-ancestors 'self'"
#: Served inline by the preview route; everything else is download only (D7).
PREVIEWABLE: frozenset[str] = frozenset(
    {DocumentContentType.PDF, DocumentContentType.PNG, DocumentContentType.JPEG}
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PDF_SIGNATURE = b"%PDF-"
_ZIP_SIGNATURE = b"PK\x03\x04"
#: ISO base media brands of HEVC-coded HEIF images (HEIC) ...
_HEIC_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"})
#: ... and the generic HEIF brands, accepted when a HEIC brand is among the compatible ones.
_HEIF_GENERIC_BRANDS = frozenset({b"mif1", b"msf1"})
_FTYP_MAX_SIZE = 4096
_DOCX_CONTENT_TYPES = "[content_types].xml"
_DOCX_MAIN_PART = "word/document.xml"
_DOCX_MACRO_PART = "vbaproject.bin"
#: Where Word keeps embedded files (spreadsheets, PDFs, OLE objects) in a package.
_DOCX_EMBEDDINGS = "word/embeddings/"
_ZIP_ENCRYPTED_FLAG = 0x1
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass(frozen=True)
class Sniffed:
    """A file's type as its content shows it (D5)."""

    kind: str
    content_type: str
    extension: str


@dataclass(frozen=True)
class Refusal:
    code: str

    @property
    def message(self) -> str:
        return MESSAGES[self.code]


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    code: str = ""
    document: Document | None = None


@dataclass
class Opened:
    """A document ready to stream: its chunks and the response headers."""

    chunks: Iterator[bytes]
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


class DocumentHidden(Exception):  # noqa: N818 - "not found" to the user
    """The document is not served to this user or in this way (the view answers 404)."""


class _Refused(Exception):  # noqa: N818
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- Filenames (D5) -------------------------------------------------------------------------

#: Removed from a filename: control, format (bidirectional overrides, zero-width
#: characters), surrogate, private-use and unassigned characters, and line and paragraph
#: separators.
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
#: Path separators and the characters no common file system accepts in a name.
_RESERVED_CHARACTERS = frozenset('/\\:*?"<>|')
_PATH_SEPARATORS = re.compile(r"[/\\]")
FALLBACK_STEM = "document"


def sanitise_filename(raw: str) -> str:
    """The name to store and to offer on download: the base name only, Unicode NFC,
    without control, format or path characters, whitespace collapsed, at most
    :data:`DOCUMENT_FILENAME_MAX_LENGTH` characters with the extension kept. A name with
    nothing before its extension becomes ``document.<extension>``."""
    name = _PATH_SEPARATORS.split(raw)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(
        character
        for character in name
        if unicodedata.category(character) not in _REMOVED_CATEGORIES
        and character not in _RESERVED_CHARACTERS
    )
    name = " ".join(name.split())
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    stem = stem.strip(" .")
    extension = extension.strip()
    suffix = f".{extension}" if extension else ""
    if len(suffix) >= DOCUMENT_FILENAME_MAX_LENGTH:
        return (stem or FALLBACK_STEM)[:DOCUMENT_FILENAME_MAX_LENGTH]
    stem = (stem or FALLBACK_STEM)[: DOCUMENT_FILENAME_MAX_LENGTH - len(suffix)].rstrip(" .")
    return f"{stem or FALLBACK_STEM}{suffix}"


def _extension(filename: str) -> str:
    stem, dot, extension = filename.rpartition(".")
    return extension.lower() if dot and stem else ""


# --- Type decision (D5) ---------------------------------------------------------------------


def sniff(data: bytes, filename: str) -> Sniffed | Refusal:
    """The type decided from the content, if it is an accepted one and the filename's
    extension agrees with it."""
    detected = _detect(data)
    if isinstance(detected, Refusal):
        return detected
    extension = _extension(filename)
    if detected is None:
        return Refusal(TYPE_NOT_ACCEPTED)
    if extension not in EXTENSIONS[detected]:
        return Refusal(EXTENSION_MISMATCH)
    return Sniffed(kind=detected, content_type=CONTENT_TYPES[detected], extension=extension)


def _detect(data: bytes) -> str | Refusal | None:
    if data.startswith(_PDF_SIGNATURE):
        return PDF
    if data.startswith(_PNG_SIGNATURE):
        return PNG
    if data.startswith(_JPEG_SIGNATURE):
        return JPEG
    if _is_heic(data):
        return HEIC
    if data.startswith(_ZIP_SIGNATURE):
        return _detect_docx(data)
    return None


def _is_heic(data: bytes) -> bool:
    """An ISO base media file whose first box is ``ftyp`` with a HEIC brand."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    size = int.from_bytes(data[0:4], "big")
    if size < 16 or size > _FTYP_MAX_SIZE or size > len(data):
        return False
    major = data[8:12]
    compatible = {data[offset : offset + 4] for offset in range(16, size - 3, 4)}
    return major in _HEIC_BRANDS or (
        major in _HEIF_GENERIC_BRANDS and bool(compatible & _HEIC_BRANDS)
    )


def _detect_docx(data: bytes) -> str | Refusal | None:
    """A zip holding ``[Content_Types].xml`` and ``word/document.xml`` and no macro
    project. Any other zip is not an accepted type."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > DOCX_MAX_ENTRIES:
                return Refusal(TOO_MUCH_TEXT)
            names = {entry.filename.lower(): entry for entry in entries}
            if any(name.rsplit("/", 1)[-1] == _DOCX_MACRO_PART for name in names):
                return Refusal(MACRO)
            if _DOCX_CONTENT_TYPES not in names or _DOCX_MAIN_PART not in names:
                return None
            if any(entry.flag_bits & _ZIP_ENCRYPTED_FLAG for entry in entries):
                return Refusal(DAMAGED)
            if _holds_embedded_file(archive, names):
                return Refusal(EMBEDDED_OBJECT)
            content_types = _read_part(archive, names[_DOCX_CONTENT_TYPES], DOCX_PART_MAX_BYTES)
    except _Refused as refused:
        return Refusal(refused.code)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, OSError,
            NotImplementedError, ValueError, RuntimeError) as error:  # fmt: skip
        logger.warning("Document zip could not be read: %s", type(error).__name__)
        return Refusal(DAMAGED)
    if b"macroenabled" in content_types.lower():
        return Refusal(MACRO)
    return DOCX


def _holds_embedded_file(archive: zipfile.ZipFile, names: dict[str, zipfile.ZipInfo]) -> bool:
    """An entry under ``word/embeddings/``, or a ``.bin`` entry that is an OLE compound
    file (an embedded object or an ActiveX control). Other ``.bin`` entries, such as
    the printer settings Word saves, are not files a reader can open."""
    for name, entry in names.items():
        if name.startswith(_DOCX_EMBEDDINGS) and len(name) > len(_DOCX_EMBEDDINGS):
            return True
        if name.endswith(".bin"):
            with archive.open(entry) as part:
                if part.read(len(_OLE_SIGNATURE)) == _OLE_SIGNATURE:
                    return True
    return False


def _read_part(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int) -> bytes:
    """One part's unpacked bytes, refused once they pass ``limit`` (the declared size in
    the archive is not trusted)."""
    chunks: list[bytes] = []
    total = 0
    with archive.open(entry) as part:
        while chunk := part.read(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise _Refused(TOO_MUCH_TEXT)
            chunks.append(chunk)
    return b"".join(chunks)


# --- Text extraction (D6) -------------------------------------------------------------------


def extract_text(sniffed: Sniffed, data: bytes) -> str | Refusal:
    """The text a PDF or DOCX holds, as the guard reads it; ``""`` for an image."""
    if sniffed.kind == PDF:
        text = _pdf_text(data)
    elif sniffed.kind == DOCX:
        text = _docx_text(data)
    else:
        return ""
    if isinstance(text, Refusal):
        return text
    if len(text) > TEXT_MAX_CHARACTERS:
        return Refusal(TOO_MUCH_TEXT)
    return text


def _pdf_text(data: bytes) -> str | Refusal:
    """The page text of every page, then every other place a PDF keeps text a reader can
    see or a program fills in: form fields, annotations, the document information
    dictionary and the XMP metadata (:func:`_pdf_pieces`).

    The page text decides whether the PDF can be read at all: any failure there refuses
    the file as unreadable. An object that cannot be decoded while the other places are
    collected is skipped (logged by its class name) and collection goes on.
    """
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return Refusal(PDF_UNREADABLE)
        count = len(reader.pages)
        if count == 0:
            return Refusal(PDF_NO_PAGES)
        if count > PDF_MAX_PAGES:
            return Refusal(PDF_TOO_MANY_PAGES)
        texts: list[str] = []
        length = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            length += len(text) + 1
            if length > TEXT_MAX_CHARACTERS:
                return Refusal(TOO_MUCH_TEXT)
            texts.append(text)
    except Exception as error:  # noqa: BLE001 - any failure of the reader means "unreadable"
        # pypdf raises many types on a malformed or hostile file (its own errors, and
        # ValueError, KeyError, RecursionError, ...). The file cannot be checked, so it
        # is refused; the class name is logged, never the content.
        logger.warning("PDF text extraction failed: %s", type(error).__name__)
        return Refusal(PDF_UNREADABLE)
    pieces = _Pieces(length)
    try:
        _pdf_pieces(reader, pieces)
    except _Refused as refused:
        return Refusal(refused.code)
    # Pages are joined by a line break, so a number split over a page break is joined
    # by the guard's own separator rule; the other pieces follow, each on its own.
    return _normalise_line_breaks(PIECE_SEPARATOR.join(["\n".join(texts), *pieces.items]))


#: Keys of a form field (and of a widget annotation, which may be its own field) whose
#: values are text: the name, the alternate name, the value, the default value, the
#: options of a choice field.
_PDF_FIELD_KEYS = ("/T", "/TU", "/V", "/DV", "/Opt")
_PDF_ANNOTATION_KEYS = ("/T", "/Contents", *_PDF_FIELD_KEYS[1:])
#: Keys whose value is rich text (XHTML): read with :func:`_add_markup`.
_PDF_RICH_TEXT_KEYS = ("/RC", "/RV")
_PDF_DATE_KEYS = frozenset({"/CreationDate", "/ModDate"})
#: ``D:YYYYMMDDHHmmSS``: fourteen digits in a row, which the card rule could read as a
#: card number. A date in range is written with separators the guard does not join.
_PDF_DATE = re.compile(r"(?:D:)?(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)(.*)", re.DOTALL)
_PDF_VALUE_MAX_DEPTH = 8


class _Pieces:
    """Text collected outside the page text, counted against :data:`TEXT_MAX_CHARACTERS`
    as it is collected (one object referenced many times is collected once)."""

    def __init__(self, used: int) -> None:
        self.items: list[str] = []
        self.length = used
        self.seen: set[tuple[int, int]] = set()

    def add(self, text: str) -> None:
        if not text or text.isspace():
            return
        self.length += len(text) + 1
        if self.length > TEXT_MAX_CHARACTERS:
            raise _Refused(TOO_MUCH_TEXT)
        self.items.append(text)

    def first_visit(self, item: Any) -> bool:
        """False for an indirect object already collected; a walk never loops."""
        if not isinstance(item, IndirectObject):
            return True
        key = (item.idnum, item.generation)
        if key in self.seen:
            return False
        if len(self.seen) >= PDF_MAX_OBJECTS:
            raise _Refused(TOO_MUCH_TEXT)
        self.seen.add(key)
        return True


@contextmanager
def _skipped_if_unreadable(what: str) -> Iterator[None]:
    """An object outside the page text that cannot be decoded is skipped, not refused."""
    try:
        yield
    except _Refused:
        raise
    except Exception as error:  # noqa: BLE001 - pypdf raises many types on a hostile file
        logger.warning("PDF %s skipped, unreadable: %s", what, type(error).__name__)


def _resolved(value: Any) -> Any:
    return value.get_object() if isinstance(value, PdfObject) else value


def _pdf_pieces(reader: PdfReader, pieces: _Pieces) -> None:
    """Form fields (with their kids), every page's annotations, the document information
    dictionary and the XMP metadata. A PDF carrying a file (the ``EmbeddedFiles`` name
    tree or a file-attachment annotation) is refused (D6)."""
    root: Any = None
    with _skipped_if_unreadable("catalog"):
        root = _resolved(reader.trailer.get("/Root"))
    if not isinstance(root, DictionaryObject):
        root = None
    if root is not None:
        with _skipped_if_unreadable("name tree"):
            names = _resolved(root.get("/Names"))
            if isinstance(names, DictionaryObject) and "/EmbeddedFiles" in names:
                raise _Refused(EMBEDDED_OBJECT)
        with _skipped_if_unreadable("form"):
            _pdf_fields(root, pieces)
    for page in reader.pages:
        with _skipped_if_unreadable("annotation list"):
            for item in _array_items(_resolved(page.get("/Annots"))):
                if pieces.first_visit(item):
                    with _skipped_if_unreadable("annotation"):
                        _pdf_annotation(_resolved(item), pieces)
    with _skipped_if_unreadable("document information"):
        info = _resolved(reader.trailer.get("/Info"))
        if isinstance(info, DictionaryObject):
            for key, value in list(info.items()):
                with _skipped_if_unreadable("document information entry"):
                    resolved = _resolved(value)
                    if key in _PDF_DATE_KEYS and isinstance(resolved, str):
                        resolved = _readable_pdf_date(resolved)
                    _add_pdf_value(resolved, pieces)
    if root is not None:
        with _skipped_if_unreadable("XMP metadata"):
            metadata = _resolved(root.get("/Metadata"))
            if isinstance(metadata, StreamObject):
                raw = _stream_bytes(metadata)
                if raw is not None:
                    _add_markup(_decode_xml(raw), pieces)


def _array_items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, ArrayObject) else []


def _pdf_fields(root: DictionaryObject, pieces: _Pieces) -> None:
    """Every field of the interactive form, its kids too, in document order."""
    form = _resolved(root.get("/AcroForm"))
    if not isinstance(form, DictionaryObject):
        return
    stack = list(reversed(_array_items(_resolved(form.get("/Fields")))))
    walked = 0
    while stack:
        item = stack.pop()
        walked += 1
        if walked > PDF_MAX_OBJECTS:
            raise _Refused(TOO_MUCH_TEXT)
        if not pieces.first_visit(item):
            continue
        with _skipped_if_unreadable("form field"):
            field_object = _resolved(item)
            if isinstance(field_object, DictionaryObject):
                _add_pdf_keys(field_object, _PDF_FIELD_KEYS, pieces)
                stack.extend(reversed(_array_items(_resolved(field_object.get("/Kids")))))


def _pdf_annotation(annotation: Any, pieces: _Pieces) -> None:
    if not isinstance(annotation, DictionaryObject):
        return
    if _resolved(annotation.get("/Subtype")) == "/FileAttachment":
        raise _Refused(EMBEDDED_OBJECT)
    _add_pdf_keys(annotation, _PDF_ANNOTATION_KEYS, pieces)


def _add_pdf_keys(dictionary: DictionaryObject, keys: tuple[str, ...], pieces: _Pieces) -> None:
    for key in keys:
        with _skipped_if_unreadable("value"):
            _add_pdf_value(_resolved(dictionary.get(key)), pieces)
    for key in _PDF_RICH_TEXT_KEYS:
        with _skipped_if_unreadable("rich text"):
            value = _resolved(dictionary.get(key))
            if isinstance(value, StreamObject):
                raw = _stream_bytes(value)
                value = None if raw is None else _decode_xml(raw)
            elif isinstance(value, bytes):
                value = value.decode("latin-1")
            if isinstance(value, str):
                _add_markup(value, pieces)


def _add_pdf_value(value: Any, pieces: _Pieces, depth: int = 0) -> None:
    """A text string, a byte string, a name, a number, a text stream, or an array of
    those (a choice field's options, a multiple selection)."""
    if isinstance(value, str):  # TextStringObject and NameObject
        pieces.add(str(value))
    elif isinstance(value, bytes):  # ByteStringObject
        pieces.add(value.decode("latin-1"))
    elif isinstance(value, NumberObject | FloatObject):
        pieces.add(str(value))
    elif isinstance(value, StreamObject):
        raw = _stream_bytes(value)
        if raw is not None:
            pieces.add(_decode_xml(raw))
    elif isinstance(value, ArrayObject) and depth < _PDF_VALUE_MAX_DEPTH:
        for item in value:
            with _skipped_if_unreadable("array item"):
                _add_pdf_value(_resolved(item), pieces, depth + 1)


def _readable_pdf_date(value: str) -> str:
    """A PDF date as ``YYYY-MM-DD HH:MM:SS`` when it is one, any other string unchanged."""
    match = _PDF_DATE.fullmatch(value)
    if match is None:
        return value
    year, month, day, hour, minute, second = (match.group(index) for index in range(1, 7))
    if not (
        1 <= int(month) <= 12
        and 1 <= int(day) <= 31
        and int(hour) <= 23
        and int(minute) <= 59
        and int(second) <= 59
    ):
        return value
    return f"{year}-{month}-{day} {hour}:{minute}:{second}{match.group(7)}"


def _stream_bytes(stream: StreamObject) -> bytes | None:
    """A stream's decoded bytes up to :data:`PDF_STREAM_MAX_BYTES`, or ``None`` when it is
    larger or uses a filter other than none or ``FlateDecode`` (it cannot be read with a
    cap). pypdf's own decoding is not used: it has no cap this module controls."""
    raw = stream._data  # noqa: SLF001 - the stream as stored, before any filter
    filters = _resolved(stream.get("/Filter"))
    if isinstance(filters, ArrayObject) and len(filters) == 1:
        filters = _resolved(filters[0])
    if filters is None:
        return raw if len(raw) <= PDF_STREAM_MAX_BYTES else None
    if filters != "/FlateDecode" or stream.get("/DecodeParms") is not None:
        return None
    decompressor = zlib.decompressobj()
    decoded = decompressor.decompress(raw, PDF_STREAM_MAX_BYTES + 1)
    return decoded if len(decoded) <= PDF_STREAM_MAX_BYTES else None


def _docx_text(data: bytes) -> str | Refusal:
    """The text of every XML part of the package, main document first, then the text of
    every other place in those parts (:func:`_xml_text`).

    Every part is read, not only the main document, the headers, footers, footnotes and
    comments: Word follows the package's relationships, so text can live in a part with
    any name. The relationship parts (``.rels``) are read for their targets. Each part is
    read up to :data:`DOCX_PART_MAX_BYTES`, all together up to
    :data:`DOCX_TOTAL_MAX_BYTES`, and scanned without an XML parser (no DTD, no entity
    expansion).

    The paragraph text of every part comes first, exactly as Word shows it, the parts
    joined by a line break. The other pieces (properties, alt text, field values held in
    attributes, link targets, any other element text) follow, each separated from the
    next by :data:`PIECE_SEPARATOR`.
    """
    texts: list[str] = []
    pieces: list[str] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = sorted(
                (
                    entry
                    for entry in archive.infolist()
                    if entry.filename.lower().endswith((".xml", ".rels"))
                ),
                key=lambda entry: (entry.filename.lower() != _DOCX_MAIN_PART, entry.filename),
            )
            for entry in entries:
                raw = _read_part(archive, entry, DOCX_PART_MAX_BYTES)
                total += len(raw)
                if total > DOCX_TOTAL_MAX_BYTES:
                    return Refusal(TOO_MUCH_TEXT)
                relationships = entry.filename.lower().endswith(".rels")
                text, other = _xml_text(
                    _decode_xml(raw),
                    _relationship_attribute if relationships else _docx_attribute,
                )
                texts.append(text)
                pieces.extend(other)
    except _Refused as refused:
        return Refusal(refused.code)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, OSError,
            NotImplementedError, ValueError, RuntimeError) as error:  # fmt: skip
        logger.warning("DOCX text extraction failed: %s", type(error).__name__)
        return Refusal(DAMAGED)
    return _normalise_line_breaks(PIECE_SEPARATOR.join(["\n".join(texts), *pieces]))


#: Between the page or paragraph text and each other piece of a PDF or DOCX: four line
#: breaks, one more than the card rule's separator joins. Two pieces are never read as
#: one digit run: a run of 20 or more digits is not tested at all, so a card number in a
#: title would be hidden by the same number in the subject next to it, and numbers in a
#: row (a document's statistics in ``docProps/app.xml``) would be read as a card. A
#: routing number and an account number in neighbouring pieces stay within the pair
#: rule's 20-character gap.
PIECE_SEPARATOR = "\n\n\n\n"


def _decode_xml(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


#: Elements whose character data is text a reader sees (``t``) or that the file keeps
#: (deleted text of a tracked change, field instructions). Matched by local name, so a
#: prefix other than ``w:`` (or DrawingML's ``a:t`` in a text box) is read too.
_TEXT_ELEMENTS = frozenset({"t", "delText", "instrText"})
_PARAGRAPH = "p"
_LINE_BREAK_ELEMENTS = frozenset({"br", "cr"})
_TAB = "tab"
#: Attributes read in any DOCX element, by local name: alt text and names of pictures and
#: shapes (``descr``, ``title``, ``name``), alternative text of VML (``alt``).
_DOCX_ATTRIBUTES = frozenset({"descr", "title", "alt", "name"})
#: Attributes read only in the named element: the value of a content control's tag and
#: alias, a hyperlink's targets, a simple field's instructions.
_DOCX_ELEMENT_ATTRIBUTES = {
    "tag": frozenset({"val"}),
    "alias": frozenset({"val"}),
    "hyperlink": frozenset({"anchor", "docLocation"}),
    "fldSimple": frozenset({"instr"}),
}
#: The end of a tag that starts at the position after ``<``; quoted attribute values may
#: hold ``>``. Possessive quantifiers keep the match linear.
_TAG_BODY = re.compile(r"""[^>"']*+(?:(?:"[^"]*+"|'[^']*+')[^>"']*+)*+>""")
_TAG_NAME = re.compile(r"/?\s*(?:[^\s/>:]+:)?([^\s/>:]+)")
_ATTRIBUTE = re.compile(r"""(?:[^\s=/>"':]+:)?([^\s=/>"':]+)\s*=\s*(?:"([^"]*+)"|'([^']*+)')""")
_ENTITY = re.compile(r"&(#[0-9]{1,7}|#x[0-9a-fA-F]{1,6}|lt|gt|amp|quot|apos);")
_NAMED_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


def _docx_attribute(element: str, attribute: str) -> bool:
    return attribute in _DOCX_ATTRIBUTES or attribute in _DOCX_ELEMENT_ATTRIBUTES.get(
        element, frozenset()
    )


def _relationship_attribute(element: str, attribute: str) -> bool:
    return attribute == "Target"


def _any_attribute(element: str, attribute: str) -> bool:
    return True


def _entity(match: re.Match[str]) -> str:
    name = match.group(1)
    if name in _NAMED_ENTITIES:
        return _NAMED_ENTITIES[name]
    try:
        code = int(name[2:], 16) if name.startswith("#x") else int(name[1:])
        return chr(code)
    except (ValueError, OverflowError):
        return "�"


def _xml_text(xml: str, wanted: Callable[[str, str], bool]) -> tuple[str, list[str]]:
    """The paragraph text and the other pieces of one XML part.

    The paragraph text is the character data of the text elements, the runs of a
    paragraph joined with nothing between them, one line per paragraph, a tab and a line
    break where the document has one. The other pieces are, in document order, the
    character data of every other element (whitespace only is left out) and the values
    of the attributes ``wanted(element, attribute)`` accepts, both by local name. The five
    predefined entities and character references are decoded; nothing else is expanded.
    """
    out: list[str] = []
    pieces: list[str] = []
    depth = 0
    position = 0
    length = len(xml)

    def character_data(text: str) -> None:
        if depth:
            out.append(text)
        elif text and not text.isspace():
            pieces.append(text)

    while position < length:
        start = xml.find("<", position)
        if start == -1:
            character_data(_ENTITY.sub(_entity, xml[position:]))
            break
        if start > position:
            character_data(_ENTITY.sub(_entity, xml[position:start]))
        if xml.startswith("<![CDATA[", start):
            end = xml.find("]]>", start + 9)
            end = length if end == -1 else end
            character_data(xml[start + 9 : end])
            position = end + 3
            continue
        if xml.startswith("<!--", start):
            end = xml.find("-->", start + 4)
            position = length if end == -1 else end + 3
            continue
        if xml.startswith("<?", start):
            end = xml.find("?>", start + 2)
            position = length if end == -1 else end + 2
            continue
        body = _TAG_BODY.match(xml, start + 1)
        if body is None:
            end = xml.find(">", start + 1)
            position = length if end == -1 else end + 1
            continue
        tag = xml[start + 1 : body.end() - 1]
        position = body.end()
        if tag.startswith("!"):
            continue
        name_match = _TAG_NAME.match(tag)
        if name_match is None:
            continue
        name = name_match.group(1)
        closing = tag.startswith("/")
        empty = tag.rstrip().endswith("/")
        if not closing:
            for attribute in _ATTRIBUTE.finditer(tag, name_match.end()):
                value = attribute.group(2)
                value = attribute.group(3) if value is None else value
                if value and not value.isspace() and wanted(name, attribute.group(1)):
                    pieces.append(_ENTITY.sub(_entity, value))
        if name in _TEXT_ELEMENTS:
            if closing:
                depth = max(0, depth - 1)
            elif not empty:
                depth += 1
        elif closing:
            if name == _PARAGRAPH:
                out.append("\n")
        elif name == _TAB:
            out.append("\t")
        elif name in _LINE_BREAK_ELEMENTS:
            out.append("\n")
    return "".join(out), pieces


def _add_markup(markup: str, pieces: _Pieces) -> None:
    """XML or XHTML held in a PDF (rich text, XMP metadata): the text of every element
    and every attribute value, each a piece of its own."""
    text, others = _xml_text(markup, _any_attribute)
    for piece in (text, *others):
        pieces.add(piece)


_LINE_BREAKS = str.maketrans(
    {"\r": "\n", "\x0b": "\n", "\x0c": "\n", "\x85": "\n", " ": "\n", " ": "\n"}
)


def _normalise_line_breaks(text: str) -> str:
    """Every kind of line break as ``\\n``, the one the text guard counts as a separator."""
    return text.replace("\r\n", "\n").translate(_LINE_BREAKS)


def _holds_payment_data(*values: str) -> bool:
    for value in values:
        try:
            validate_no_payment_data(value)
        except ValidationError:
            return True
    return False


# --- Upload (D3 to D6, D8) ------------------------------------------------------------------


def _owner_type(owner: Owner) -> str:
    return OWNER_STORE if isinstance(owner, Store) else OWNER_LEASE


def _owner_filter(owner: Owner) -> dict[str, Owner]:
    return {_owner_type(owner): owner}


def _require(actor: User, role: Role) -> None:
    if not meets(role_of(actor), role):
        raise PermissionDenied


def upload(
    owner: Owner, kind: str, uploaded_file: UploadedFile[bytes], note: str, actor: User
) -> Outcome:
    """Store one file for ``owner``, or refuse it and store nothing (billing and above)."""
    _require(actor, Role.BILLING)
    raw_name = str(uploaded_file.name or "")
    filename = sanitise_filename(raw_name)
    size = int(uploaded_file.size or 0)
    known_kind = kind if kind in DocumentKind.values else ""

    def refuse(code: str) -> Outcome:
        _audit(
            actor,
            ACTION_UPLOAD_REFUSED,
            _owner_type(owner),
            str(owner.pk),
            {
                "reason": code,
                "owner_type": _owner_type(owner),
                "owner_id": str(owner.pk),
                "kind": known_kind,
                "size_bytes": size,
                "filename_length": len(raw_name),
            },
        )
        return Outcome(ok=False, code=code, message=MESSAGES[code])

    storage = documents_storage.get_storage()
    if storage is None:
        return refuse(FEATURE_OFF)
    if not known_kind:
        return refuse(KIND_UNKNOWN)
    if size <= 0:
        return refuse(EMPTY)
    if size > settings.DOCUMENT_MAX_BYTES:
        return refuse(TOO_LARGE)
    uploaded_file.seek(0)
    data = uploaded_file.read(settings.DOCUMENT_MAX_BYTES + 1)
    size = len(data)
    if size == 0:
        return refuse(EMPTY)
    if size > settings.DOCUMENT_MAX_BYTES:
        return refuse(TOO_LARGE)
    sniffed = sniff(data, filename)
    if isinstance(sniffed, Refusal):
        return refuse(sniffed.code)
    text = extract_text(sniffed, data)
    if isinstance(text, Refusal):
        return refuse(text.code)
    note_text = visible_text(note or "")
    if len(note_text) > NOTE_MAX_LENGTH:
        return refuse(NOTE_TOO_LONG)
    if _holds_payment_data(text, raw_name, filename, note_text):
        return refuse(PAYMENT_DATA)
    del text

    digest = hashlib.sha256(data).hexdigest()
    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        kind=known_kind,
        source=DocumentSource.UPLOAD,
        original_filename=filename,
        content_type=sniffed.content_type,
        size_bytes=size,
        blob_path=f"documents/{_owner_type(owner)}/{owner.pk}/{document_id}",
        sha256=digest,
        note=note_text,
        uploaded_by=actor,
        **_owner_filter(owner),
    )
    document.check_payment_data()
    try:
        with transaction.atomic():
            # Uploads to one record are serialised, so the duplicate check holds. The
            # lock is held across ``put``, which the storage client bounds to about 30
            # seconds (documents_storage.azure_blob: short timeouts, no retries).
            type(owner).objects.select_for_update().get(pk=owner.pk)
            if _live(owner).filter(sha256=digest).exists():
                raise _Refused(DUPLICATE)
            storage.put(document.blob_path, data, sniffed.content_type)
            document.save(force_insert=True)
            _audit(actor, ACTION_UPLOADED, ENTITY_DOCUMENT, str(document.pk), _summary(document))
    except _Refused as refused:
        return refuse(refused.code)
    except StorageUnavailable:
        return refuse(STORAGE_UNAVAILABLE)
    except IntegrityError:
        if _live(owner).filter(sha256=digest).exists():
            return refuse(DUPLICATE)
        raise
    return Outcome(ok=True, message="Document uploaded.", document=document)


def _live(owner: Owner) -> QuerySet[Document]:
    return Document.objects.filter(deleted_at__isnull=True, **_owner_filter(owner))


def _summary(document: Document) -> dict[str, Any]:
    owner_type = OWNER_STORE if document.store_id is not None else OWNER_LEASE
    return {
        "document_id": str(document.pk),
        "owner_type": owner_type,
        "owner_id": str(document.store_id or document.lease_id),
        "kind": document.kind,
        "size_bytes": document.size_bytes,
        "sha256_prefix": document.sha256[:SHA256_PREFIX_LENGTH],
    }


def _audit(
    actor: User | None, action: str, entity_type: str, entity_id: str, after: dict[str, Any]
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=None,
        after=after,
    )


# --- Read (D3, D7, D8) ----------------------------------------------------------------------


def documents_of(owner: Owner, include_deleted: bool = False) -> QuerySet[Document]:
    """The owner's documents, newest first; deleted ones only when asked."""
    rows = Document.objects.filter(**_owner_filter(owner))
    if not include_deleted:
        rows = rows.filter(deleted_at__isnull=True)
    return rows.select_related("uploaded_by", "deleted_by").order_by("-uploaded_at", "id")


def can_preview(document: Document) -> bool:
    return document.content_type in PREVIEWABLE


def open_for(document: Document, actor: User, inline: bool) -> Opened:
    """The document's chunks and headers, for a user with a role.

    Raises :class:`DocumentHidden` for a deleted document (unless the user is an admin)
    and for an inline request of a type that is never previewed; raises
    :class:`~documents_storage.StorageUnavailable` when the storage cannot serve it,
    after auditing the failed read.
    """
    role = role_of(actor)
    if not meets(role, Role.VIEWER):
        raise PermissionDenied
    if document.deleted_at is not None and not meets(role, Role.ADMIN):
        raise DocumentHidden
    if inline and not can_preview(document):
        raise DocumentHidden
    action = ACTION_PREVIEWED if inline else ACTION_DOWNLOADED
    summary = _summary(document)
    storage = documents_storage.get_storage()
    try:
        if storage is None:
            raise StorageUnavailable("FeatureOff")
        chunks = storage.open(document.blob_path)
    except StorageUnavailable:
        _audit(
            actor,
            action,
            ENTITY_DOCUMENT,
            str(document.pk),
            {**summary, "result": "failed", "reason": STORAGE_UNAVAILABLE},
        )
        raise
    _audit(actor, action, ENTITY_DOCUMENT, str(document.pk), {**summary, "result": "ok"})
    return Opened(
        chunks=chunks,
        content_type=document.content_type,
        headers={
            "Content-Type": document.content_type,
            "Content-Disposition": content_disposition_header(
                not inline, document.original_filename
            )
            or "attachment",
            "Content-Length": str(document.size_bytes),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": content_security_policy(document.content_type, inline),
            "Cache-Control": "private, no-store",
        },
    )


def content_security_policy(content_type: str, inline: bool) -> str:
    """The ``Content-Security-Policy`` of a streamed document (D7).

    A PDF preview cannot carry ``sandbox``: the browser's built-in PDF viewer does not
    run in a sandboxed document and the frame stays blank. It gets
    :data:`PDF_PREVIEW_POLICY` instead: nothing may be loaded or run from the document
    (``default-src 'none'``) and only this site may frame it. Every other response, a
    PNG or JPEG preview and every download, keeps ``sandbox``.
    """
    if inline and content_type == DocumentContentType.PDF:
        return PDF_PREVIEW_POLICY
    return SANDBOX_POLICY


# --- Delete (D7) ----------------------------------------------------------------------------


def delete(document: Document, note: str, actor: User) -> Outcome:
    """Soft-delete (admin): the row is hidden from non-admins and the blob stays."""
    _require(actor, Role.ADMIN)
    text = visible_text(note or "")
    if not text:
        return Outcome(ok=False, code=NOTE_REQUIRED, message=MESSAGES[NOTE_REQUIRED])
    if len(text) > NOTE_MAX_LENGTH:
        return Outcome(ok=False, code=NOTE_TOO_LONG, message=MESSAGES[NOTE_TOO_LONG])
    if _holds_payment_data(text):
        return Outcome(ok=False, code=PAYMENT_DATA, message=MESSAGES[PAYMENT_DATA])
    with transaction.atomic():
        row = Document.objects.select_for_update().get(pk=document.pk)
        if row.deleted_at is not None:
            return Outcome(ok=False, code=ALREADY_DELETED, message=MESSAGES[ALREADY_DELETED])
        row.deleted_at = timezone.now()
        row.deleted_by = actor
        row.delete_note = text
        row.save(update_fields=["deleted_at", "deleted_by", "delete_note"])
        _audit(actor, ACTION_DELETED, ENTITY_DOCUMENT, str(row.pk), _summary(row))
    return Outcome(ok=True, message="Document deleted.", document=row)
