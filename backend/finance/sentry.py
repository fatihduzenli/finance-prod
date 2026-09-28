"""Sentry wiring: off unless a DSN is configured, and scrubbed when it is on.

Scrubbing is a hard requirement, not a nicety: the app handles operator data and
Stax credentials, and neither may reach an error tracker (root ``CLAUDE.md`` §5.3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.types import Event, Hint

FILTERED = "[Filtered]"

#: Any key matching this, at any depth in the event, loses its value.
SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|token|secret|password|card|account|routing)",
    re.IGNORECASE,
)

DROPPED_REQUEST_KEYS: tuple[str, ...] = ("headers", "cookies", "data", "query_string")
DROPPED_USER_KEYS: tuple[str, ...] = ("email", "ip_address")


def init(dsn: str, app_env: str, stax_env: str) -> None:
    """Initialise Sentry. An empty DSN turns error reporting off entirely."""
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=app_env,
        integrations=[DjangoIntegration()],
        send_default_pii=False,
        traces_sample_rate=0.0,
        before_send=scrub_event,
    )
    sentry_sdk.set_tag("app_env", app_env)
    sentry_sdk.set_tag("stax_env", stax_env)


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """``before_send`` hook: drop request/user detail and filter sensitive keys."""
    scrubbed = cast("dict[str, Any]", _scrub(dict(event)))

    request = scrubbed.get("request")
    if isinstance(request, dict):
        for key in DROPPED_REQUEST_KEYS:
            request.pop(key, None)

    user = scrubbed.get("user")
    if isinstance(user, dict):
        for key in DROPPED_USER_KEYS:
            user.pop(key, None)

    return cast("Event", scrubbed)


def _scrub(value: object) -> object:
    """Copy ``value``, replacing values of sensitive keys at any depth."""
    if isinstance(value, Mapping):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and SENSITIVE_KEY_RE.search(key):
                scrubbed[key] = FILTERED
            else:
                scrubbed[key] = _scrub(item)
        return scrubbed
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item) for item in value)
    return value
