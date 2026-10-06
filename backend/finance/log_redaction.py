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

import json
import logging
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus, unquote, urlsplit

REDACTED = "[REDACTED]"

#: Longest refused Host value written to the log (docs/qa/PLAN-003b §3).
MAX_REFUSED_HOST_LENGTH = 100

REFUSED_HOST_MESSAGE = "Refused request: host not allowed"

#: Only this much of a ``DisallowedHost`` message is searched for the Host value. It
#: holds the prefix and far more than ``MAX_REFUSED_HOST_LENGTH`` host characters, and
#: it bounds the work done on an attacker-controlled message.
MAX_MATCHED_MESSAGE_LENGTH = 512

#: Django's ``DisallowedHost`` message starts with the Host value as a Python ``repr``;
#: the quoted part, escapes included, is taken up to its closing quote. An escape and a
#: plain character never start with the same character, so matching is linear. The
#: group ``cut`` is the end of a message cut at ``MAX_MATCHED_MESSAGE_LENGTH``, which
#: may fall inside an escape; it is accepted only when the message was cut.
_DISALLOWED_HOST_RE = re.compile(
    r"^Invalid HTTP_HOST header: "
    r"(?P<quote>['\"])(?P<host>(?:\\.|(?!(?P=quote))[^\\])*)"
    r"(?:(?P=quote)|(?P<cut>\\?\Z))",
    re.DOTALL,
)

#: Every character outside this set is replaced by ``?`` in a refused Host value.
_UNSAFE_HOST_CHAR_RE = re.compile(r"[^A-Za-z0-9.:\-\[\]_]")

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
    others: Iterable[str] = (),
) -> frozenset[str]:
    """Every string the formatter must redact, including URL-quoted forms.

    For the two URLs only the password component is a secret. It is taken both as it
    is written in the URL (percent-encoded) and decoded, because a driver error may
    show either.
    """
    bases = [stax_api_key, stax_webhook_url_token, django_secret_key, *others]
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


def service_account_secrets(raw: str) -> tuple[str, ...]:
    """The parts of a service-account key (PLAN-011b D3) a log line must never show: the
    whole JSON text, the private key as stored and with its line breaks escaped as JSON
    writes them, each line of the key's body, and the key's ID. Empty when ``raw`` is
    empty or not a JSON object."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return (raw,)
    if not isinstance(parsed, dict):
        return (raw,)
    values = [raw]
    private_key = parsed.get("private_key")
    if isinstance(private_key, str) and private_key:
        values += [private_key, private_key.replace("\n", "\\n")]
        values += [line.strip() for line in private_key.splitlines() if "-----" not in line]
    key_id = parsed.get("private_key_id")
    if isinstance(key_id, str):
        values.append(key_id)
    return tuple(value for value in values if len(value) >= MIN_SECRET_LENGTH)


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


class RefusedHostFilter(logging.Filter):
    """Turn Django's refused-Host report into one WARNING line without a traceback.

    Attached to the logger ``django.security.DisallowedHost`` only (PLAN-003b §3). The
    Host header is attacker-controlled, so the rewritten message keeps nothing of the
    original but the host itself, reduced to a safe character set and cut to
    ``MAX_REFUSED_HOST_LENGTH`` characters. The record is always kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # ``msg`` and ``args`` do not match. The record is still reported, without
            # a host, rather than raising into Django's request handling.
            message = ""
        host = _refused_host(message)
        record.msg = f"{REFUSED_HOST_MESSAGE}: {host}" if host else REFUSED_HOST_MESSAGE
        record.args = ()
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _refused_host(message: str) -> str:
    """The sanitised Host value from a ``DisallowedHost`` message, or ``""``."""
    text = message[:MAX_MATCHED_MESSAGE_LENGTH]
    match = _DISALLOWED_HOST_RE.match(text)
    if match is None or (match["cut"] is not None and text == message):
        return ""
    return _UNSAFE_HOST_CHAR_RE.sub("?", match["host"])[:MAX_REFUSED_HOST_LENGTH]
