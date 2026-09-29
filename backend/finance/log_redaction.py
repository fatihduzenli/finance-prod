"""Log formatting that removes configured secret values (docs/qa/PLAN-003a §3).

Named ``log_redaction`` rather than ``logging`` so that no import anywhere can resolve
to this module instead of the standard library.

The formatter redacts after normal formatting, so a secret is removed wherever it
appears: in the message, in its ``%`` arguments, and in the traceback text. Values
are handed over by ``settings`` when logging is configured; nothing here reads the
environment, and nothing here logs, prints or ``repr``-s a value (root ``CLAUDE.md``
§5.3).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import quote, quote_plus, unquote, urlsplit

REDACTED = "[REDACTED]"

#: Shorter values are not redacted: an unset or trivial secret must not turn every
#: log line into ``[REDACTED]`` noise.
MIN_SECRET_LENGTH = 8


def secret_values(
    *,
    stax_api_key: str,
    stax_webhook_url_token: str,
    django_secret_key: str,
    database_url: str,
    redis_url: str,
) -> frozenset[str]:
    """Every string the formatter must redact, including URL-quoted forms.

    For the two URLs only the password component is a secret. It is taken both as it
    is written in the URL (percent-encoded) and decoded, because a driver error may
    show either.
    """
    bases = [stax_api_key, stax_webhook_url_token, django_secret_key]
    for url in (database_url, redis_url):
        password = _url_password(url)
        if password:
            bases.extend((password, unquote(password)))

    values: set[str] = set()
    for base in bases:
        if len(base) < MIN_SECRET_LENGTH:
            continue
        values.update((base, quote(base, safe=""), quote_plus(base, safe="")))
    return frozenset(values)


def _url_password(url: str) -> str:
    try:
        return urlsplit(url).password or ""
    except ValueError:
        # urlsplit rejects some malformed URLs (an unbalanced IPv6 bracket). Such a URL
        # yields no password to redact; the database or Redis client reports it.
        return ""


class RedactingFormatter(logging.Formatter):
    """A ``logging.Formatter`` that replaces each secret value with ``[REDACTED]``."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets: tuple[str, ...] = tuple(
            sorted(
                {value for value in secrets if len(value) >= MIN_SECRET_LENGTH},
                key=len,
                reverse=True,
            )
        )

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))

    def redact(self, text: str) -> str:
        """``text`` with every configured secret value replaced."""
        for value in self._secrets:
            text = text.replace(value, REDACTED)
        return text

    def __repr__(self) -> str:
        return f"<{type(self).__name__} redacting {len(self._secrets)} values>"
