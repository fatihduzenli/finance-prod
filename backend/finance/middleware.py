"""Response middleware: the sandbox banner, and no shared or stored copy of a page; the
processor's name for the request's sentences.

docs/CLAUDE.md Rule 6: when ``STAX_ENV=sandbox`` every rendered page, Django admin
included, carries a fixed yellow bar that cannot be dismissed, and the page title is
prefixed. The banner is injected into the finished HTML so no template needs to know
about it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import cache

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.template.loader import render_to_string
from django.utils.cache import patch_cache_control

from finance.context_processors import company_settings
from ledger import wording

BANNER_TEMPLATE = "finance/banner.html"
SANDBOX_STAX_ENV = "sandbox"
TITLE_PREFIX = b"[SANDBOX] "

_BODY_TAG_RE = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)
_TITLE_TAG_RE = re.compile(rb"<title\b[^>]*>", re.IGNORECASE)


@cache
def _banner_bytes() -> bytes:
    """The banner markup, rendered once.

    Rendered without a request and without context, so no context processor can pull
    a setting (such as the Stax key) into the page.
    """
    return render_to_string(BANNER_TEMPLATE).strip().encode("utf-8")


class SandboxBannerMiddleware:
    """Inject the sandbox banner into every HTML response."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)

        if settings.STAX_ENV != SANDBOX_STAX_ENV:
            return response
        if isinstance(response, StreamingHttpResponse) or not isinstance(response, HttpResponse):
            # Streaming and file responses have no in-memory body to rewrite.
            return response
        if response.has_header("Content-Encoding"):
            # Already encoded; rewriting the bytes would corrupt them.
            return response
        if not response.headers.get("Content-Type", "").lower().startswith("text/html"):
            return response

        content = response.content
        if not _BODY_TAG_RE.search(content):
            return response

        content = _BODY_TAG_RE.sub(lambda match: match.group(0) + _banner_bytes(), content, count=1)
        content = _TITLE_TAG_RE.sub(lambda match: match.group(0) + TITLE_PREFIX, content, count=1)
        response.content = content
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response


class PrivateNoStoreMiddleware:
    """``Cache-Control: private, no-store`` on every response that is not a static file
    (docs/qa/PLAN-011-dashboard-reports-mobile.md D9, §7).

    Pages, exports, documents and errors carry financial data: no browser, proxy or
    service worker may keep a copy. Directives a view already set are kept (for example
    ``no-cache``, ``must-revalidate``, ``max-age=0``); ``private`` and ``no-store`` are
    added and a ``public`` is removed. Static files under ``STATIC_URL`` keep the caching
    whitenoise gives them.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response
        prefix = settings.STATIC_URL or "static/"
        self.static_prefix = prefix if prefix.startswith("/") else f"/{prefix}"

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        if not request.path_info.startswith(self.static_prefix):
            patch_cache_control(response, private=True, no_store=True)
        return response


class ProviderNameMiddleware:
    """The processor's name in the app's sentences comes from this request's company
    settings row (docs/qa/PLAN-012c-design-pass.md D9): the row the shell reads
    (:func:`finance.context_processors.company_settings`), so a page that names the
    processor reads the row once, and not at all when nothing on it does."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        with wording.reading_from(lambda: str(company_settings(request).provider_name)):
            return self.get_response(request)
