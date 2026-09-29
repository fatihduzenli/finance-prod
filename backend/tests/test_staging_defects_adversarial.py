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
