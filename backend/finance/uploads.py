"""Request-size limits and the one upload handler (PLAN-010c D3).

Uploaded files never touch the container's filesystem: :class:`CappedMemoryFileUploadHandler`
(a :class:`~django.core.files.uploadhandler.MemoryFileUploadHandler`) is the only upload
handler, so Django never spools a file to a temporary file.

**Which limit.** :func:`request_limit` decides, from the route the path resolves to:
the routes in :data:`LARGE_UPLOAD_ROUTE_NAMES` (the document uploads and the store and
lease create forms, which take an optional file) may carry
``UPLOAD_MAX_REQUEST_BYTES`` (one 25 MB document plus room for the form); every other
request, the unauthenticated login form included, ``REQUEST_MAX_BYTES`` (2.5 MiB,
Django's own default for a request body). The list is closed and kept here only.

Two checks apply the limit, so memory stays bounded:

1. :class:`RequestSizeLimitMiddleware` answers 413 to a request whose ``Content-Length``
   exceeds the limit before anything reads the body (it sits ahead of the CSRF
   middleware, which is the first to read a form).
2. The handler stops the upload when the bytes it has received pass the same limit.
   On WSGI the body stream already ends at ``Content-Length`` (a request without one has
   an empty body), so this second limit matters for a stream whose length was absent or
   understated.

``DATA_UPLOAD_MAX_MEMORY_SIZE`` stays at ``REQUEST_MAX_BYTES``: Django applies it to a
body read whole and to the non-file fields of a form, never to the file itself.

The 25 MB rule itself, and the refusal of an empty file, are applied by
``ledger.services.documents`` with a sentence for the user.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.files.uploadhandler import MemoryFileUploadHandler, StopUpload
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.urls import Resolver404, resolve

TOO_LARGE_MESSAGE = "The upload is too large. A document may be at most 25 MB."
REQUEST_ENTITY_TOO_LARGE = 413

#: The routes that may carry one document: the only ones with the large limit.
LARGE_UPLOAD_ROUTE_NAMES: frozenset[str] = frozenset(
    {
        "ledger:documents:upload_store",
        "ledger:documents:upload_lease",
        "ledger:stores:create",
        "ledger:leases:create",
    }
)

_LIMIT_ATTRIBUTE = "_finance_request_limit"


def request_limit(request: HttpRequest | None) -> int:
    """The largest body this request may carry, in bytes (see the module).

    Decided once per request from the path, before any view runs; a path that resolves
    to no route, or no request at all, gets the small limit.
    """
    if request is None:
        return int(settings.REQUEST_MAX_BYTES)
    cached = getattr(request, _LIMIT_ATTRIBUTE, None)
    if isinstance(cached, int):
        return cached
    try:
        view_name = resolve(request.path_info).view_name
    except Resolver404:
        view_name = ""
    limit = int(
        settings.UPLOAD_MAX_REQUEST_BYTES
        if view_name in LARGE_UPLOAD_ROUTE_NAMES
        else settings.REQUEST_MAX_BYTES
    )
    setattr(request, _LIMIT_ATTRIBUTE, limit)
    return limit


def _declared_length(request: HttpRequest) -> int | None:
    raw = request.META.get("CONTENT_LENGTH") or ""
    try:
        return int(raw)
    except ValueError:
        return None


class RequestSizeLimitMiddleware:
    """413 for a declared body larger than the request's limit; the body is never read."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        length = _declared_length(request)
        if length is not None and length > request_limit(request):
            return HttpResponse(
                TOO_LARGE_MESSAGE,
                status=REQUEST_ENTITY_TOO_LARGE,
                content_type="text/plain; charset=utf-8",
            )
        return self.get_response(request)


class CappedMemoryFileUploadHandler(MemoryFileUploadHandler):
    """The in-memory handler with a hard cap on the bytes received in one request: the
    request's :func:`request_limit`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.received = 0
        self.limit = request_limit(self.request)

    def handle_raw_input(
        self,
        input_data: Any,
        META: dict[str, Any],  # noqa: N803 - Django's parameter name
        content_length: int,
        boundary: Any,
        encoding: str | None = None,
    ) -> None:
        if content_length > self.limit:
            raise StopUpload(connection_reset=True)
        super().handle_raw_input(input_data, META, content_length, boundary, encoding)

    def receive_data_chunk(self, raw_data: bytes, start: int) -> bytes | None:
        self.received += len(raw_data)
        if self.received > self.limit:
            raise StopUpload(connection_reset=True)
        return super().receive_data_chunk(raw_data, start)
