"""Response middleware that makes a sandbox deployment impossible to miss.

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
