"""Adversarial cases for PLAN-003c, written by the QA owner.

The implementation tests patch the prompt and the terminal check. These run the real
command in a child process attached to a real pseudo-terminal, with APP_ENV=staging,
against a throwaway PostgreSQL database: the hidden prompt, the echo behaviour and the
one-time rule under concurrency are exercised as the owner will meet them.

PostgreSQL only (a deployed APP_ENV refuses sqlite); skipped elsewhere, run in CI.
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from django.db import connection

PASSWORD = "Fake-Test-Passphrase-9143"
OTHER_PASSWORD = "Fake-Other-Passphrase-5528"

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="a deployed APP_ENV needs PostgreSQL; the test database is not PostgreSQL",
)


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@pytest.fixture
def staging_env(repo_root: Path) -> Iterator[dict[str, str]]:
    """Environment of a staging process with its own freshly migrated database."""
    import psycopg

    base_url = os.environ["DATABASE_URL"]
    name = f"plan003c_{uuid.uuid4().hex[:12]}"
    assert re.fullmatch(r"[a-z0-9_]+", name)
    admin_url = _with_database(base_url, "postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    env = {
        **os.environ,
        "APP_ENV": "staging",
        "STAX_ENV": "sandbox",
        "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
        "STAX_API_KEY": "FAKE-TEST-KEY-do-not-use-XYZ123",
        "STAX_WEBHOOK_URL_TOKEN": "t" * 64,
        "APP_BASE_URL": "https://staging.example.com",
        "DJANGO_SECRET_KEY": "test-only-not-secret",
        "DATABASE_URL": _with_database(base_url, name),
    }
    try:
        subprocess.run(
            [sys.executable, "backend/manage.py", "deploy_migrate"],
            cwd=repo_root,
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )
        yield env
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class _Terminal:
    """A child process on a real pseudo-terminal."""

    def __init__(self, repo_root: Path, env: dict[str, str], *args: str) -> None:
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(
            [sys.executable, "backend/manage.py", *args],
            cwd=repo_root,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.output = ""

    def _read(self, timeout: float) -> bool:
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return False
        try:
            data = os.read(self.master, 4096)
        except OSError:
            return False
        self.output += data.decode(errors="replace")
        return bool(data)

    def wait_for(self, text: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        seen_from = len(self.output)
        while text not in self.output[seen_from:]:
            assert time.monotonic() < deadline, f"never saw {text!r}; got {self.output!r}"
            if not self._read(0.2) and self.process.poll() is not None:
                self._read(0.2)
                assert text in self.output[seen_from:], self.output

    def send_line(self, text: str) -> None:
        os.write(self.master, text.encode() + b"\n")

    def finish(self, timeout: float = 60.0) -> int:
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            assert time.monotonic() < deadline, self.output
            self._read(0.2)
        while self._read(0.2):
            pass
        os.close(self.master)
        return self.process.returncode


def _run(repo_root: Path, env: dict[str, str], email: str, first: str, second: str) -> _Terminal:
    terminal = _Terminal(repo_root, env, "bootstrap_admin", "--email", email)
    terminal.wait_for("Password: ")
    terminal.send_line(first)
    terminal.wait_for("Password (again): ")
    terminal.send_line(second)
    return terminal


def _users(env: dict[str, str]) -> list[tuple[str, bool, bool, bool]]:
    import psycopg

    with psycopg.connect(env["DATABASE_URL"]) as check:
        return check.execute(
            "SELECT username, is_staff, is_superuser, is_active FROM auth_user ORDER BY username"
        ).fetchall()


def _count(env: dict[str, str], table: str) -> int:
    import psycopg

    assert re.fullmatch(r"[a-z_]+", table)
    with psycopg.connect(env["DATABASE_URL"]) as check:
        row = check.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


def test_real_terminal_creates_the_admin_and_never_echoes_the_password(
    repo_root: Path, staging_env: dict[str, str]
) -> None:
    terminal = _run(repo_root, staging_env, "  Owner@Example.COM ", PASSWORD, PASSWORD)
    assert terminal.finish() == 0, terminal.output

    assert PASSWORD not in terminal.output
    assert "Created admin owner@example.com." in terminal.output
    assert "Traceback" not in terminal.output
    assert _users(staging_env) == [("owner@example.com", True, False, True)]
    assert _count(staging_env, "otp_totp_totpdevice") == 0
    assert _count(staging_env, "otp_static_staticdevice") == 0


def test_real_terminal_mismatch_creates_nothing_and_echoes_nothing(
    repo_root: Path, staging_env: dict[str, str]
) -> None:
    terminal = _run(repo_root, staging_env, "owner@example.com", PASSWORD, OTHER_PASSWORD)
    assert terminal.finish() != 0

    assert PASSWORD not in terminal.output
    assert OTHER_PASSWORD not in terminal.output
    assert _users(staging_env) == []


def test_second_run_is_refused_with_exit_2_and_changes_nothing(
    repo_root: Path, staging_env: dict[str, str]
) -> None:
    assert _run(repo_root, staging_env, "owner@example.com", PASSWORD, PASSWORD).finish() == 0

    second = _run(repo_root, staging_env, "intruder@example.com", OTHER_PASSWORD, OTHER_PASSWORD)
    assert second.finish() == 2, second.output

    assert OTHER_PASSWORD not in second.output
    assert [row[0] for row in _users(staging_env)] == ["owner@example.com"]


def test_two_runs_at_the_same_moment_create_exactly_one_admin(
    repo_root: Path, staging_env: dict[str, str]
) -> None:
    first = _Terminal(repo_root, staging_env, "bootstrap_admin", "--email", "a@example.com")
    second = _Terminal(repo_root, staging_env, "bootstrap_admin", "--email", "b@example.com")
    for terminal in (first, second):
        terminal.wait_for("Password: ")
        terminal.send_line(PASSWORD)
        terminal.wait_for("Password (again): ")
    # Both are now one keystroke from the database; release them together.
    first.send_line(PASSWORD)
    second.send_line(PASSWORD)

    codes = sorted((first.finish(), second.finish()))

    assert codes == [0, 2], (first.output, second.output)
    users = _users(staging_env)
    assert len(users) == 1
    assert users[0][0] in {"a@example.com", "b@example.com"}


def test_password_is_not_in_the_process_arguments_or_log(
    repo_root: Path, staging_env: dict[str, str]
) -> None:
    terminal = _run(repo_root, staging_env, "owner@example.com", PASSWORD, PASSWORD)
    assert PASSWORD not in " ".join(str(part) for part in terminal.process.args)  # type: ignore[union-attr]
    assert terminal.finish() == 0
    # The WARNING line reaches the terminal through the stderr handler.
    assert "WARNING accounts.bootstrap_admin First admin created: owner@example.com" in (
        terminal.output
    )
    assert PASSWORD not in terminal.output
