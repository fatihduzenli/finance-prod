"""Test files built in code for the document tests (PLAN-010c §6). Not a test module.

No real document is ever committed: every PDF, DOCX and image here is generated from a
few lines of text. Card and account numbers used with these are the obviously synthetic
ones of the guard tests.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping

from pypdf import PdfWriter

DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
DOCM_CONTENT_TYPES = DOCX_CONTENT_TYPES.replace(
    "vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "vnd.ms-word.document.macroEnabled.main+xml",
)
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _pdf_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content_stream(lines: list[str]) -> bytes:
    body = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
    for line in lines:
        body.append(f"({_pdf_string(line)}) Tj T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1")


def make_pdf(pages: list[list[str]]) -> bytes:
    """A PDF with one page per item, each page showing its lines of text (Helvetica).

    Written by hand so the text layer is exactly the given lines; offsets in the
    cross-reference table are computed, so the file is well formed.
    """
    objects: list[bytes] = []
    page_ids = [4 + index * 2 for index in range(len(pages))]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, lines in enumerate(pages):
        content_id = page_ids[index] + 1
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        stream = _content_stream(lines)
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def make_blank_pdf(pages: int = 1) -> bytes:
    """A PDF with blank pages and no text layer, as a scan without OCR reads."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    # The user password, positionally: an obvious placeholder.
    writer.encrypt("placeholder-not-secret", algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def paragraph(*runs: str) -> str:
    """A WordprocessingML paragraph with one run per text."""
    inner = "".join(f'<w:r><w:t xml:space="preserve">{run}</w:t></w:r>' for run in runs)
    return f"<w:p>{inner}</w:p>"


def part(*paragraphs: str, root: str = "document") -> str:
    body = "".join(paragraphs)
    if root == "document":
        return (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W}>'
            f"<w:body>{body}</w:body></w:document>"
        )
    return f'<?xml version="1.0" encoding="UTF-8"?><w:{root} {_W}>{body}</w:{root}>'


def make_docx(
    document: str | None = None,
    *,
    extra: Mapping[str, str | bytes] | None = None,
    content_types: str = DOCX_CONTENT_TYPES,
    include_main: bool = True,
) -> bytes:
    """A DOCX package (a zip) with the given main document and extra parts."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        if include_main:
            archive.writestr(
                "word/document.xml", document if document is not None else part(paragraph("Hello"))
            )
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return out.getvalue()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
HEIC = (24).to_bytes(4, "big") + b"ftypheic" + b"\x00\x00\x00\x00" + b"mif1heic" + b"\x00" * 64
HEIF_GENERIC_WITH_HEIC = (
    (24).to_bytes(4, "big") + b"ftypmif1" + b"\x00\x00\x00\x00" + b"mif1heic" + b"\x00" * 64
)
AVIF = (24).to_bytes(4, "big") + b"ftypavif" + b"\x00\x00\x00\x00" + b"mif1avif" + b"\x00" * 64
HTML = b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>"
EXE = b"MZ\x90\x00" + b"\x00" * 64
