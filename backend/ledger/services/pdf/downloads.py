"""Downloading an invoice or a receipt PDF (docs/qa/15/PLAN-015c-invoice-pdfs.md §3, the
"Downloads"): the response headers and the audit row, shared by the staff pages and the
operator portal. The PDF is rendered on request and never stored; the headers are a stored
document's download headers (``ledger.services.documents.open_for``): an attachment, no
sniffing, the sandbox policy and ``Cache-Control: private, no-store``.

Each download writes one audit row: ``invoice.pdf_downloaded`` (entity ``invoice``) or
``payment.receipt_downloaded`` (entity ``payment``), with the staff actor, or, from the portal,
no staff actor and the portal user's id in ``after``.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.utils.http import content_disposition_header

from ledger.models import AuditLog, DocumentContentType
from ledger.services.documents import SANDBOX_POLICY

INVOICE_DOWNLOADED = "invoice.pdf_downloaded"
RECEIPT_DOWNLOADED = "payment.receipt_downloaded"
CONTENT_TYPE = str(DocumentContentType.PDF)


def headers(filename: str, size: int) -> dict[str, str]:
    """The download headers of a rendered PDF of ``size`` bytes."""
    return {
        "Content-Type": CONTENT_TYPE,
        "Content-Disposition": content_disposition_header(True, filename) or "attachment",
        "Content-Length": str(size),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": SANDBOX_POLICY,
        "Cache-Control": "private, no-store",
    }


def audit(
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    actor: User | None = None,
    portal_user: uuid.UUID | None = None,
    filename: str,
) -> None:
    """The download's audit row (see the module docstring)."""
    after: dict[str, str] = {"filename": filename}
    if portal_user is not None:
        after["portal_user"] = str(portal_user)
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=None,
        after=after,
    )
