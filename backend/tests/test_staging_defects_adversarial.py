"""Adversarial cases for PLAN-003a, written by the QA owner (plan §5, rows 8 to 12).

These try to get a secret into the container log by routes the implementation tests
do not take, to start gunicorn after a failed migration, and to make two replicas
migrate at once.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from django.db import connection

from finance.log_redaction import REDACTED, RedactingFormatter, secret_values

FAKE_TOKEN = "fake-webhook-token-" + "w" * 45  # 64 characters, obviously not real
FAKE_PASSWORD = "p@ss/word fake+1"  # needs quoting in a URL


def _formatter(*secrets: str) -> RedactingFormatter:
    return RedactingFormatter("%(levelname)s %(name)s %(message)s", secrets=secrets)


def _record(msg: str, *args: object, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord("adversarial", logging.ERROR, __file__, 1, msg, args, exc_info)


# --- 8: the webhook token inside a request path ----------------------------------------


def test_request_path_with_the_webhook_token_is_redacted_in_the_real_log(
    repo_root: Path,
) -> None:
    """A refused request to a webhook-shaped URL must not write the token to stderr."""
    code = (
        "import os\n"
        "from django.test import Client\n"
        "token = os.environ['STAX_WEBHOOK_URL_TOKEN']\n"
        "response = Client(raise_request_exception=False).get(\n"
        "    f'/webhooks/stax/{token}/', HTTP_HOST='localhost')\n"
        "print('status', response.status_code)\n"
    )
    result = subprocess.run(
        [sys.executable, "backend/manage.py", "shell", "-c", code],
        cwd=repo_root,
        env={
            **os.environ,
            "STAX_WEBHOOK_URL_TOKEN": FAKE_TOKEN,
            "DATABASE_URL": "sqlite:///:memory:",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "status 401" in result.stdout
    assert FAKE_TOKEN not in result.stderr
    assert FAKE_TOKEN not in result.stdout
    assert f"/webhooks/stax/{REDACTED}/" in result.stderr


# --- 9: quoted forms and a secret assembled from arguments -----------------------------


def test_url_quoted_and_plain_database_password_are_both_redacted() -> None:
    encoded = quote(FAKE_PASSWORD, safe="")
    values = secret_values(
        stax_api_key="",
        stax_webhook_url_token="",
        django_secret_key="",
        database_url=f"postgres://finance:{encoded}@db.example.com:5432/finance",
        redis_url="",
    )
    formatter = RedactingFormatter("%(message)s", secrets=values)
    line = formatter.format(
        _record(
            "could not connect: postgres://finance:%s@db.example.com (password=%s)",
            encoded,
            FAKE_PASSWORD,
        )
    )
    assert FAKE_PASSWORD not in line
    assert encoded not in line
    assert line.count(REDACTED) == 2
    assert "db.example.com" in line  # only the password is removed


def test_secret_assembled_from_two_arguments_is_redacted() -> None:
    half = len(FAKE_TOKEN) // 2
    line = _formatter(FAKE_TOKEN).format(_record("t=%s%s", FAKE_TOKEN[:half], FAKE_TOKEN[half:]))
    assert line == f"ERROR adversarial t={REDACTED}"


def test_secret_repeated_and_adjacent_is_redacted_every_time() -> None:
    line = _formatter(FAKE_TOKEN).format(_record(FAKE_TOKEN * 3))
    assert FAKE_TOKEN not in line
    assert line == f"ERROR adversarial {REDACTED * 3}"


# --- 10: chained exceptions and multi-line tracebacks ----------------------------------


def test_secret_in_the_cause_of_a_chained_exception_is_redacted() -> None:
    try:
        try:
            raise ConnectionError(f"auth failed for token {FAKE_TOKEN}")
        except ConnectionError as inner:
            raise RuntimeError("request failed") from inner
    except RuntimeError as outer:
        record = _record("unhandled", exc=outer)

    text = _formatter(FAKE_TOKEN).format(record)

    assert FAKE_TOKEN not in text
    assert "direct cause of the following exception" in text
    assert f"ConnectionError: auth failed for token {REDACTED}" in text
    assert "RuntimeError: request failed" in text


def test_cached_traceback_text_cannot_bypass_redaction() -> None:
    """Formatter caches ``exc_text`` on the record; a second format must stay redacted."""
    try:
        raise ValueError(FAKE_TOKEN)
    except ValueError as error:
        record = _record("first", exc=error)
    formatter = _formatter(FAKE_TOKEN)
    first = formatter.format(record)
    second = formatter.format(record)
    assert FAKE_TOKEN not in first
    assert FAKE_TOKEN not in second


# --- 11: two replicas start together (PostgreSQL only) ---------------------------------

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="concurrent migration needs PostgreSQL; the test database is not PostgreSQL",
)


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@requires_postgresql
def test_two_concurrent_runs_both_succeed_and_migrate_once(repo_root: Path) -> None:
    import psycopg

    base_url = os.environ["DATABASE_URL"]
    name = f"plan003a_{uuid.uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", name)
    admin_url = _with_database(base_url, "postgres")
    target_url = _with_database(base_url, name)

    baseline = f"{name}_baseline"
    baseline_url = _with_database(base_url, baseline)

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
        admin.execute(f'CREATE DATABASE "{baseline}"')
    try:
        env = {**os.environ, "DATABASE_URL": target_url}
        command = [sys.executable, "backend/manage.py", "deploy_migrate"]
        runs = [
            subprocess.Popen(
                command,
                cwd=repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [(run.communicate(timeout=300), run.returncode) for run in runs]
        for (stdout, stderr), returncode in results:
            assert returncode == 0, stderr
            assert stdout.splitlines()[-1] == "deploy_migrate: migrations are up to date"

        plan = subprocess.run(
            [sys.executable, "backend/manage.py", "showmigrations", "--plan"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout.splitlines()
        assert plan
        assert all(line.startswith("[X]") for line in plan), plan

        # Baseline: the same migrations applied by a single run. django_migrations also
        # records squashed-away migrations, so its row count is not the plan length.
        subprocess.run(
            command,
            cwd=repo_root,
            env={**os.environ, "DATABASE_URL": baseline_url},
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        recorded = "SELECT app, name FROM django_migrations ORDER BY app, name"
        with psycopg.connect(target_url) as check:
            concurrent_rows = check.execute(recorded).fetchall()
        with psycopg.connect(baseline_url) as check:
            baseline_rows = check.execute(recorded).fetchall()
        assert len(concurrent_rows) == len(set(concurrent_rows))  # nothing applied twice
        assert concurrent_rows == baseline_rows
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            admin.execute(f'DROP DATABASE IF EXISTS "{baseline}" WITH (FORCE)')


# --- 12: the image's start command ------------------------------------------------------


def _image_cmd(repo_root: Path) -> str:
    lines = (repo_root / "Dockerfile").read_text().splitlines()
    commands = [line for line in lines if line.startswith("CMD ")]
    assert len(commands) == 1, commands
    return commands[0]


def test_image_starts_gunicorn_only_after_a_successful_migration(repo_root: Path) -> None:
    cmd = _image_cmd(repo_root)
    assert "python backend/manage.py deploy_migrate && exec gunicorn " in cmd
    # No operator that would continue after a failure.
    assert ";" not in cmd
    assert "||" not in cmd
    assert re.search(r"(?<!&)&(?!&)", cmd) is None
    assert cmd.count("&&") == 1


def test_image_keeps_access_logging_off_and_gunicorn_arguments(repo_root: Path) -> None:
    cmd = _image_cmd(repo_root)
    assert "--access-logfile" not in cmd
    assert "--access-logformat" not in cmd
    for argument in (
        "finance.wsgi:application",
        "--chdir backend",
        "--bind 0.0.0.0:8000",
        "--workers 3",
        "--timeout 60",
        "--graceful-timeout 30",
        "--error-logfile -",
    ):
        assert argument in cmd, argument


def test_image_command_runs_no_destructive_management_command(repo_root: Path) -> None:
    dockerfile = (repo_root / "Dockerfile").read_text()
    for forbidden in ("flush", "makemigrations", "sqlflush", "createsuperuser", "loaddata"):
        assert forbidden not in dockerfile, forbidden


# --- PLAN-003b rows 5 to 7: the refused-host filter under hostile input ----------------


def _refused(msg: str, *args: object, secrets: tuple[str, ...] = ()) -> str:
    """One record through the filter and then the formatter, as the root handler does."""
    from finance.log_redaction import RefusedHostFilter

    try:
        raise ValueError("traceback that must not be written")
    except ValueError as error:
        record = logging.LogRecord(
            "django.security.DisallowedHost",
            logging.ERROR,
            __file__,
            1,
            msg,
            args,
            (type(error), error, error.__traceback__),
        )
    record.stack_info = "Stack (most recent call last): fake"
    assert RefusedHostFilter().filter(record) is True
    return RedactingFormatter("%(levelname)s %(name)s %(message)s", secrets=secrets).format(record)


def _django_message(host: str) -> str:
    return f"Invalid HTTP_HOST header: {host!r}. You may need to add {host!r} to ALLOWED_HOSTS."


PREFIX = "WARNING django.security.DisallowedHost Refused request: host not allowed"


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com\r\nERROR forged line",
        "a'b\"c",
        "host with spaces",
        "\x1b[31mred\x1b[0m.example.com",
        "exämple.com",
        "%s%s%s%n",
        "{0}{host}",
        "\\" * 5000,
        "'" * 5000,
        "a" * 5000,
        "\x00\x07\x08",
    ],
)
def test_hostile_host_values_give_one_safe_line(host: str) -> None:
    line = _refused(_django_message(host))

    assert line.startswith(PREFIX)
    assert "\n" not in line
    assert "\r" not in line
    assert "Traceback" not in line
    assert "Stack (most recent call last)" not in line
    assert "ALLOWED_HOSTS" not in line
    written_host = line[len(PREFIX) :].removeprefix(": ")
    assert len(written_host) <= 100
    assert re.fullmatch(r"[A-Za-z0-9.:\-\[\]_?]*", written_host), written_host


def test_plain_host_is_written_as_it_is() -> None:
    assert _refused(_django_message("old.example.com:8443")) == f"{PREFIX}: old.example.com:8443"


def test_host_equal_to_a_secret_is_redacted() -> None:
    secret = "FakeSecretHostValue0123456789"
    line = _refused(_django_message(secret), secrets=(secret,))
    assert secret not in line
    assert line == f"{PREFIX}: {REDACTED}"


@pytest.mark.parametrize(
    ("msg", "args"),
    [
        ("something else entirely", ()),
        ("", ()),
        ("Invalid HTTP_HOST header: 'unterminated", ()),
        ("Invalid HTTP_HOST header: %r.", ("via-args.example.com",)),
        ("mismatched %s %s", ("only-one",)),
        ("mismatched %d", ("not-a-number",)),
    ],
)
def test_unexpected_records_never_raise_and_never_echo_the_message(
    msg: str, args: tuple[object, ...]
) -> None:
    line = _refused(msg, *args)
    if args == ("via-args.example.com",):
        assert line == f"{PREFIX}: via-args.example.com"
    else:
        assert line == PREFIX


def test_malformed_message_with_many_backslashes_is_handled_quickly() -> None:
    """No closing quote and a long run of backslashes must not stall the worker."""
    import time

    started = time.monotonic()
    line = _refused("Invalid HTTP_HOST header: '" + "\\" * 60)
    assert time.monotonic() - started < 1.0
    assert line == PREFIX


def test_very_long_well_formed_host_keeps_its_first_hundred_characters() -> None:
    assert _refused(_django_message("a" * 5000)) == f"{PREFIX}: {'a' * 100}"


@pytest.mark.parametrize("length", [480, 481, 482, 483, 484, 485, 486])
def test_message_cut_in_the_middle_of_an_escape_is_still_one_safe_line(length: int) -> None:
    """The bounded input may end inside a ``\\\\`` pair; the line must stay safe."""
    line = _refused(_django_message("b" * length + "\\" * 50))
    assert line == f"{PREFIX}: {'b' * 100}"


@pytest.mark.parametrize("size", [1_000, 100_000])
def test_matching_time_does_not_depend_on_the_input_shape(size: int) -> None:
    import time

    for body in ("\\" * size, "'" * size, '"' * size, "\\'" * size, "a\\" * size):
        started = time.monotonic()
        _refused("Invalid HTTP_HOST header: '" + body)
        _refused('Invalid HTTP_HOST header: "' + body)
        assert time.monotonic() - started < 0.5
