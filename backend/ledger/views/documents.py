"""Documents on the store and lease pages (docs/qa/PLAN-010c-documents-and-cost-corrections.md
D3, D7), with their Activity list (docs/qa/PLAN-010d D8: the last ``document.*`` audit
rows of the record, and for admins a link to the audit log page filtered on them).

Access, as everywhere: a signed-in, second-factor-verified user with a role
(middleware plus :func:`~ledger.views.operators.role_required`). Upload needs
``billing``, delete needs ``admin`` (both POST only, checked again by
``accounts.registry``); download and preview are read routes open to every role. Every
change and every read goes through :mod:`ledger.services.documents`, which audits it.

Download and preview stream the file from the storage through the app; no storage URL
reaches the browser. Preview answers only for PDF, PNG and JPEG, ``inline``, with the
type decided at upload and ``X-Content-Type-Options: nosniff``; a PNG or JPEG preview
carries ``Content-Security-Policy: sandbox``, a PDF preview ``default-src 'none';
frame-ancestors 'self'`` (the browser's PDF viewer stays blank in a sandboxed document;
see :func:`ledger.services.documents.content_security_policy`). Preview is the one route
that may be framed, by this site only (``X-Frame-Options: SAMEORIGIN``), because the
owner's page shows it in a frame; images are shown in that frame too, as the document
itself, never in an ``<img>`` tag of the page. Downloads are ``attachment`` with
``sandbox``.

Every POST ends in a redirect to the owner's page, whatever the outcome; the outcome is
a message there.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST, require_safe

import documents_storage
from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import DocumentDeleteForm, DocumentUploadForm
from ledger.models import Document, DocumentKind, Lease, Store
from ledger.services import audit
from ledger.services import documents as service
from ledger.views.operators import _actor, _role, role_required

SERVICE_UNAVAILABLE = 503
ANCHOR = "#documents"


def _parse(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _owner_url(owner: service.Owner) -> str:
    if isinstance(owner, Store):
        return reverse("ledger:stores:detail", kwargs={"pk": owner.pk}) + ANCHOR
    return reverse("ledger:leases:detail", kwargs={"pk": owner.pk}) + ANCHOR


def _document_owner(document: Document) -> service.Owner:
    if document.store is not None:
        return document.store
    if document.lease is not None:
        return document.lease
    raise Http404


def feature_on() -> bool:
    return documents_storage.get_storage() is not None


# --- Context for the owner pages ----------------------------------------------------------


def section_context(request: HttpRequest, owner: service.Owner) -> dict[str, Any]:
    """What the Documents section of a store or lease page renders (D7)."""
    role = _role(request)
    is_admin = meets(role, Role.ADMIN)
    rows = list(service.documents_of(owner, include_deleted=is_admin))
    preview = None
    requested = request.GET.get("preview", "")
    if requested:
        try:
            key = uuid.UUID(requested)
        except ValueError:
            key = None
        preview = next(
            (
                row
                for row in rows
                if row.pk == key and row.deleted_at is None and service.can_preview(row)
            ),
            None,
        )
    upload_name = "upload_store" if isinstance(owner, Store) else "upload_lease"
    default_kind = DocumentKind.OTHER if isinstance(owner, Store) else DocumentKind.LEASE_AGREEMENT
    return {
        "documents": [{"row": row, "can_preview": service.can_preview(row)} for row in rows],
        "documents_feature_on": feature_on(),
        "documents_can_upload": meets(role, Role.BILLING),
        "documents_can_delete": is_admin,
        "documents_upload_url": reverse(f"ledger:documents:{upload_name}", kwargs={"pk": owner.pk}),
        "documents_form": DocumentUploadForm(initial={"kind": default_kind}),
        "documents_preview": preview,
        "documents_owner_url": _owner_url(owner).removesuffix(ANCHOR),
        "documents_activity": audit.activity_for(owner),
        "documents_activity_url": (
            f"{reverse('ledger:audit:page')}?{urlencode(audit.activity_query(owner))}"
            if is_admin
            else ""
        ),
    }


def create_form(kind: str) -> DocumentUploadForm | None:
    """The optional file of a create form, or None while document storage is off."""
    if not feature_on():
        return None
    return DocumentUploadForm(prefix="document", initial={"kind": kind})


def upload_after_create(request: HttpRequest, owner: service.Owner, noun: str) -> None:
    """The optional file of a create form, uploaded after the record was saved (D7).

    A refused file never undoes the record: the page says the record was created and
    why the file was not stored.
    """
    if not request.FILES:
        return
    form = DocumentUploadForm(request.POST, request.FILES, prefix="document")
    if not form.is_valid():
        reasons = " ".join(str(error) for errors in form.errors.values() for error in errors)
        messages.error(request, f"The {noun} was created, but the file was not uploaded: {reasons}")
        return
    outcome = service.upload(
        owner,
        form.cleaned_data["kind"],
        form.cleaned_data["file"],
        form.cleaned_data.get("note") or "",
        _actor(request),
    )
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(
            request, f"The {noun} was created, but the file was not uploaded: {outcome.message}"
        )


# --- Write --------------------------------------------------------------------------------


def _upload(request: HttpRequest, owner: service.Owner) -> HttpResponse:
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    form = DocumentUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, str(error))
        return redirect(_owner_url(owner))
    outcome = service.upload(
        owner,
        form.cleaned_data["kind"],
        form.cleaned_data["file"],
        form.cleaned_data.get("note") or "",
        _actor(request),
    )
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(_owner_url(owner))


@role_required
@require_POST
def upload_store(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:documents:upload_store`` (billing)."""
    return _upload(request, get_object_or_404(Store, pk=_parse(pk)))


@role_required
@require_POST
def upload_lease(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:documents:upload_lease`` (billing)."""
    return _upload(request, get_object_or_404(Lease, pk=_parse(pk)))


@role_required
@require_POST
def delete(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:documents:delete`` (admin, note required, soft)."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    document = get_object_or_404(Document.objects.select_related("store", "lease"), pk=_parse(pk))
    owner_url = _owner_url(_document_owner(document))
    form = DocumentDeleteForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, str(error))
        return redirect(owner_url)
    outcome = service.delete(document, form.cleaned_data["note"], _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(owner_url)


# --- Read ---------------------------------------------------------------------------------


def _stream(request: HttpRequest, pk: str, *, inline: bool) -> HttpResponseBase:
    document = get_object_or_404(Document.objects.select_related("store", "lease"), pk=_parse(pk))
    try:
        opened = service.open_for(document, _actor(request), inline)
    except service.DocumentHidden:
        raise Http404 from None
    except documents_storage.StorageUnavailable:
        return render(
            request,
            "ledger/documents/unavailable.html",
            {
                "message": service.READ_FAILED_MESSAGE,
                "owner_url": _owner_url(_document_owner(document)),
            },
            status=SERVICE_UNAVAILABLE,
        )
    response = StreamingHttpResponse(opened.chunks, content_type=opened.content_type)
    for name, value in opened.headers.items():
        response[name] = value
    return response


@role_required
@require_safe
def download(request: HttpRequest, pk: str) -> HttpResponseBase:
    """``ledger:documents:download``: the file as an attachment (every role)."""
    return _stream(request, pk, inline=False)


@xframe_options_sameorigin
@role_required
@require_safe
def preview(request: HttpRequest, pk: str) -> HttpResponseBase:
    """``ledger:documents:preview``: a PDF, PNG or JPEG inline (every role)."""
    return _stream(request, pk, inline=True)
