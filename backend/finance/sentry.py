"""Sentry wiring: off unless a DSN is configured, and scrubbed when it is on.

Scrubbing is a hard requirement, not a nicety: the app handles operator data and
Stax credentials, and neither may reach an error tracker (root ``CLAUDE.md`` §5.3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, cast
from urllib.parse import quote, quote_plus

import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint

FILTERED = "[Filtered]"

#: Any key matching this, at any depth in the event, loses its value.
SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|token|secret|password|card|account|routing)",
    re.IGNORECASE,
)

#: ``url`` is dropped too: the webhook route carries its secret token in the path
#: (docs/qa/PLAN-008-webhooks.md D11).
DROPPED_REQUEST_KEYS: tuple[str, ...] = ("headers", "cookies", "data", "query_string", "url")
DROPPED_USER_KEYS: tuple[str, ...] = ("email", "ip_address")

#: Breadcrumbs of these categories describe an outbound HTTP request. Their URL and
#: query can carry the webhook URL token (register_webhooks sends it as target_url).
HTTP_BREADCRUMB_CATEGORIES: frozenset[str] = frozenset({"httplib", "http", "httpx"})
HTTP_BREADCRUMB_URL_KEYS: tuple[str, ...] = ("url", "http.query", "http.fragment")
HTTP_BREADCRUMB_KEPT_KEYS: tuple[str, ...] = ("method", "status_code")

#: Matches any configured secret value; set by init(). None: nothing to replace.
_secret_pattern: re.Pattern[str] | None = None


def init(
    dsn: str,
    app_env: str,
    stax_env: str,
    *,
    stax_api_key: str = "",
    stax_webhook_url_token: str = "",
    other_secrets: Iterable[str] = (),
) -> None:
    """Initialise Sentry. An empty DSN turns error reporting off entirely.

    The secret values (the two above and ``other_secrets``, PLAN-011b D3) are kept (as a
    compiled pattern, never logged) so the scrubber can replace them in any string of an
    event as a last line of defence.
    """
    global _secret_pattern
    _secret_pattern = _compile_secret_pattern(stax_api_key, stax_webhook_url_token, *other_secrets)
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=app_env,
        integrations=[DjangoIntegration()],
        send_default_pii=False,
        traces_sample_rate=0.0,
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )
    sentry_sdk.set_tag("app_env", app_env)
    sentry_sdk.set_tag("stax_env", stax_env)


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """``before_send`` hook: drop request/user detail and filter sensitive keys.

    Breadcrumbs lose their HTTP URL detail, and any configured secret value left in
    any string of the event is replaced with ``FILTERED``.
    """
    scrubbed = cast("dict[str, Any]", _scrub(dict(event)))

    breadcrumbs = scrubbed.get("breadcrumbs")
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
        for crumb in breadcrumbs["values"]:
            if isinstance(crumb, dict):
                _strip_http_detail(crumb)

    request = scrubbed.get("request")
    if isinstance(request, dict):
        for key in DROPPED_REQUEST_KEYS:
            request.pop(key, None)

    user = scrubbed.get("user")
    if isinstance(user, dict):
        for key in DROPPED_USER_KEYS:
            user.pop(key, None)

    return cast("Event", scrubbed)


def scrub_breadcrumb(crumb: Breadcrumb, hint: BreadcrumbHint) -> Breadcrumb | None:
    """``before_breadcrumb`` hook: an HTTP breadcrumb keeps only method and status."""
    _strip_http_detail(crumb)
    return crumb


def _strip_http_detail(crumb: dict[str, Any]) -> None:
    """Reduce an HTTP breadcrumb's data to ``HTTP_BREADCRUMB_KEPT_KEYS``, in place."""
    data = crumb.get("data")
    if not isinstance(data, dict):
        return
    is_http = crumb.get("category") in HTTP_BREADCRUMB_CATEGORIES or any(
        key in data for key in HTTP_BREADCRUMB_URL_KEYS
    )
    if is_http:
        crumb["data"] = {key: data[key] for key in HTTP_BREADCRUMB_KEPT_KEYS if key in data}


def _compile_secret_pattern(*secrets: str) -> re.Pattern[str] | None:
    """One alternation of every non-empty secret, raw and URL-quoted, longest first."""
    values = {form for secret in secrets if secret for form in _quoted_forms(secret)}
    if not values:
        return None
    ordered = sorted(values, key=len, reverse=True)
    return re.compile("|".join(re.escape(value) for value in ordered))


def _quoted_forms(secret: str) -> tuple[str, str, str]:
    return (secret, quote(secret, safe=""), quote_plus(secret, safe=""))


def _scrub(value: object) -> object:
    """Copy ``value``, replacing values of sensitive keys and secret strings at any depth."""
    if isinstance(value, str):
        return _secret_pattern.sub(FILTERED, value) if _secret_pattern else value
    if isinstance(value, Mapping):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and SENSITIVE_KEY_RE.search(key):
                scrubbed[_scrub(key)] = FILTERED
            else:
                scrubbed[_scrub(key)] = _scrub(item)
        return scrubbed
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value
