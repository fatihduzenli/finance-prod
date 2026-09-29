"""Error logging to stderr, with secrets removed (PLAN-003a §5, 5-7).

The in-process tests point the configured handler at a buffer for the duration of a
test, so they exercise the live configuration Django applied at start-up. One
subprocess test checks that the same configuration really writes to the process's
stderr.
"""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.test import Client
from django.urls import path

from accounts.roles import Role
from finance.log_redaction import REDACTED, RedactingFormatter, secret_values
from tests.auth_helpers import make_user, verified_client

#: Start of one log event: the configured line format begins with asctime and level.
EVENT_START_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) ",
    re.MULTILINE,
)

#: Obviously fake values for formatter-level tests.
FAKE_SECRET = "FAKE-SECRET-value-do-not-use-123"
OTHER_FAKE_SECRET = "FAKE-OTHER-value-do-not-use-456"


def _boom(request: HttpRequest) -> HttpResponse:
    raise RuntimeError("boom-marker for the logging test")


urlpatterns = [path("boom/", _boom)]


def _configured_handlers() -> list[logging.StreamHandler[Any]]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
        and isinstance(handler.formatter, RedactingFormatter)
    ]


@pytest.fixture
def stderr_log() -> Iterator[io.StringIO]:
    """The configured stderr handler, writing to a buffer for this test only."""
    handlers = _configured_handlers()
    assert len(handlers) == 1
    handler = handlers[0]
    original = handler.stream
    buffer = io.StringIO()
    handler.setStream(buffer)
    try:
        yield buffer
    finally:
        handler.setStream(original)


def _formatter(*secrets: str) -> RedactingFormatter:
    return RedactingFormatter("%(levelname)s %(name)s %(message)s", secrets=secrets)


def _record(msg: str, *args: object, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord("test", logging.ERROR, __file__, 1, msg, args, exc_info)


# --- configuration --------------------------------------------------------------------


def test_one_stderr_handler_on_root_at_warning() -> None:
    assert settings.LOGGING["handlers"] == {
        "stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "redacting",
        }
    }
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert len(_configured_handlers()) == 1
    # Django's default handlers (console, mail_admins, django.server) are gone, so an
    # event is written once, by the root handler.
    for name in ("django", "django.request", "django.server", "django.security"):
        assert logging.getLogger(name).handlers == [], name
        assert logging.getLogger(name).propagate, name


# --- 5: a view that raises --------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.urls(__name__)
def test_unhandled_view_error_is_one_event_with_traceback(stderr_log: io.StringIO) -> None:
    user, device = make_user("log-viewer", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device, Client(raise_request_exception=False))

    response = client.get("/boom/")

    assert response.status_code == 500
    output = stderr_log.getvalue()
    events = EVENT_START_RE.findall(output)
    assert events == ["ERROR"], output
    assert " ERROR django.request Internal Server Error: /boom/" in output
    assert output.count("Traceback (most recent call last):") == 1
    assert "RuntimeError: boom-marker for the logging test" in output


def test_logged_error_reaches_process_stderr_redacted(
    repo_root: Path, fake_stax_api_key: str
) -> None:
    code = (
        "import logging, os; "
        "logging.getLogger('probe').error('key is %s', os.environ['STAX_API_KEY'])"
    )
    result = subprocess.run(
        [sys.executable, "backend/manage.py", "shell", "-c", code],
        cwd=repo_root,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert f"ERROR probe key is {REDACTED}" in result.stderr
    assert fake_stax_api_key not in result.stderr
    assert fake_stax_api_key not in result.stdout


# --- 6: redaction in message, args and traceback --------------------------------------


def test_live_configuration_redacts_the_configured_key(
    stderr_log: io.StringIO, fake_stax_api_key: str
) -> None:
    logger = logging.getLogger("tests.logging.live")
    logger.error(f"message has {fake_stax_api_key}")
    logger.error("args have %s", fake_stax_api_key)
    try:
        raise ValueError(f"traceback has {fake_stax_api_key}")
    except ValueError:
        logger.exception("failed")

    output = stderr_log.getvalue()
    assert fake_stax_api_key not in output
    assert f"message has {REDACTED}" in output
    assert f"args have {REDACTED}" in output
    assert f"ValueError: traceback has {REDACTED}" in output
    assert len(EVENT_START_RE.findall(output)) == 3


def test_secret_in_message_is_redacted() -> None:
    text = _formatter(FAKE_SECRET).format(_record(f"before {FAKE_SECRET} after"))
    assert text == f"ERROR test before {REDACTED} after"


def test_secret_in_args_is_redacted() -> None:
    text = _formatter(FAKE_SECRET).format(_record("value=%s other=%d", FAKE_SECRET, 7))
    assert text == f"ERROR test value={REDACTED} other=7"


def test_secret_in_traceback_is_redacted() -> None:
    try:
        raise KeyError(FAKE_SECRET)
    except KeyError as error:
        record = _record("lookup failed", exc=error)
    text = _formatter(FAKE_SECRET).format(record)
    assert FAKE_SECRET not in text
    assert "Traceback (most recent call last):" in text
    assert f"KeyError: '{REDACTED}'" in text


def test_every_configured_secret_is_redacted() -> None:
    text = _formatter(FAKE_SECRET, OTHER_FAKE_SECRET).format(
        _record("%s %s", OTHER_FAKE_SECRET, FAKE_SECRET)
    )
    assert text == f"ERROR test {REDACTED} {REDACTED}"


def test_longer_secret_containing_a_shorter_one_is_redacted_whole() -> None:
    longer = FAKE_SECRET + "-suffix"
    text = _formatter(FAKE_SECRET, longer).format(_record(longer))
    assert text == f"ERROR test {REDACTED}"


def test_repr_does_not_contain_secrets() -> None:
    formatter = _formatter(FAKE_SECRET)
    assert FAKE_SECRET not in repr(formatter)
    assert FAKE_SECRET not in str(formatter)


def test_secret_values_collects_each_secret_and_url_password() -> None:
    values = secret_values(
        stax_api_key=FAKE_SECRET,
        stax_webhook_url_token="w" * 64,
        django_secret_key=OTHER_FAKE_SECRET,
        database_url="postgres://user:fake-db-password-1@db.example.com:5432/finance",
        redis_url="rediss://:fake-redis-password-2@cache.example.com:6380/0",
    )
    assert {
        FAKE_SECRET,
        "w" * 64,
        OTHER_FAKE_SECRET,
        "fake-db-password-1",
        "fake-redis-password-2",
    } <= values
    # Only the password component of a URL is secret.
    assert not any("example.com" in value for value in values)
    assert "user" not in values


# --- 7: empty and short values ---------------------------------------------------------


def test_empty_and_short_values_redact_nothing() -> None:
    message = "a plain line with short1 and nothing else"
    text = _formatter("", "short1", "1234567").format(_record(message))
    assert text == f"ERROR test {message}"
    assert REDACTED not in text


def test_eight_characters_is_the_first_length_redacted() -> None:
    text = _formatter("12345678").format(_record("id 12345678 end"))
    assert text == f"ERROR test id {REDACTED} end"


def test_secret_values_skips_unset_and_short_values() -> None:
    assert (
        secret_values(
            stax_api_key="",
            stax_webhook_url_token="",
            django_secret_key="short",
            database_url="sqlite:///:memory:",
            redis_url="",
        )
        == frozenset()
    )
    assert (
        secret_values(
            stax_api_key="",
            stax_webhook_url_token="",
            django_secret_key="",
            database_url="postgres://user:pw@db.example.com/finance",
            redis_url="redis://cache.example.com:6379/0",
        )
        == frozenset()
    )
