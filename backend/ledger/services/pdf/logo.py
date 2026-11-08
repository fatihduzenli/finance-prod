"""The logo's PNG copy (docs/qa/15/PLAN-015c-invoice-pdfs.md §3, the owner's decision 4).

The logo override is a stored document (``CompanySettings.logo``, a PNG or JPEG), readable only
through :func:`ledger.services.documents.read_all` with a staff actor. The PDF renderer and the
portal have none, so when an admin saves the logo :func:`refresh` reads the document once (an
audited download by that admin) and keeps a PNG copy in ``CompanySettings.logo_png``:
:func:`png_copy` keeps a PNG as it is and re-encodes a JPEG, scales an image down until the
copy holds at most :data:`~ledger.models.LOGO_PNG_MAX_BYTES`, and refuses anything Pillow
cannot read as a PNG or a JPEG. A blank logo, a missing or deleted document, a storage failure
or a refused image clears the copy: the PDFs then show the brand logo.
"""

from __future__ import annotations

import io
import logging

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied

import documents_storage
from ledger.models import LOGO_CONTENT_TYPES, LOGO_PNG_MAX_BYTES, CompanySettings, Document

logger = logging.getLogger(__name__)

#: The widths, in pixels, a copy too large is scaled down to in turn (the band shows the logo
#: about 160 points wide).
SCALED_WIDTHS = (1200, 600, 300)
#: Pillow's names of the formats a logo may be.
FORMATS = ("PNG", "JPEG")


def png_copy(data: bytes) -> bytes:
    """``data`` (a PNG or a JPEG) as PNG bytes of at most
    :data:`~ledger.models.LOGO_PNG_MAX_BYTES`; :class:`ValueError` when it is neither or no
    scaled copy fits."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as probe:
            kind = probe.format
            probe.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as error:
        raise ValueError("The logo is not an image.") from error
    if kind not in FORMATS:
        raise ValueError("The logo is not a PNG or a JPEG.")
    if kind == "PNG" and len(data) <= LOGO_PNG_MAX_BYTES:
        return data
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        picture = image.convert("RGBA" if kind == "PNG" else "RGB")
    for width in (picture.width, *SCALED_WIDTHS):
        if width > picture.width:
            continue
        height = max(1, round(picture.height * width / picture.width))
        out = io.BytesIO()
        picture.resize((width, height)).save(out, format="PNG", optimize=True)
        if out.tell() <= LOGO_PNG_MAX_BYTES:
            return out.getvalue()
    raise ValueError("The logo is too large.")


def refresh(row: CompanySettings, actor: User) -> bool:
    """Write the row's ``logo_png`` from its logo override, read as ``actor``; ``True`` when a
    copy is kept, ``False`` when it is cleared (see the module docstring)."""
    from ledger.services import documents

    copy = b""
    document = (
        Document.objects.filter(
            blob_path=row.logo, deleted_at__isnull=True, content_type__in=LOGO_CONTENT_TYPES
        ).first()
        if row.logo
        else None
    )
    if document is not None:
        try:
            copy = png_copy(documents.read_all(document, actor))
        except (
            ValueError,
            PermissionDenied,
            documents.DocumentHidden,
            documents_storage.StorageUnavailable,
        ) as error:
            logger.warning("The logo's PNG copy was not made (%s).", type(error).__name__)
    CompanySettings.objects.filter(pk=row.pk).update(logo_png=copy)
    row.logo_png = copy
    return bool(copy)
