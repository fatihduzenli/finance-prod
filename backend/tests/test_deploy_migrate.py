"""deploy_migrate: migrations on image start, one replica at a time (PLAN-003a §5, 1-4).

Cases 1 and 2 run the real command in a subprocess against a fresh sqlite file, so the
database really starts empty and the shared test database is left alone. Cases 3 and 4
need PostgreSQL advisory locks and are skipped when the test database is anything
else; CI runs them on PostgreSQL 16.
"""

from __future__ import annotations

import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS, connection, connections

from finance.management.commands import deploy_migrate

START_LINE = "deploy_migrate: applying pending migrations"
SUCCESS_LINE = "deploy_migrate: migrations are up to date"

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="advisory locks need PostgreSQL; the test database is not PostgreSQL",
)

_LOCK_HELD_SQL = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = 0 "
    "AND objid::bigint = %s AND objsubid = 1 AND granted AND pid = pg_backend_pid()"
)


def _manage(repo_root: Path, database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "backend/manage.py", *args],
        cwd=repo_root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _unapplied(repo_root: Path, database_url: str) -> list[str]:
    result = _manage(repo_root, database_url, "showmigrations", "--plan")
    assert result.returncode == 0, result.stderr
    assert "[X]" in result.stdout
    return [line for line in result.stdout.splitlines() if line.startswith("[ ]")]


# --- 1, 2: empty database, then a second run -----------------------------------------


def test_first_run_applies_every_migration_and_second_run_is_a_no_op(
    repo_root: Path, tmp_path: Path
) -> None:
    database_url = f"sqlite:///{tmp_path / 'fresh.sqlite3'}"

    first = _manage(repo_root, database_url, "deploy_migrate")
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines() == [START_LINE, SUCCESS_LINE]
    assert _unapplied(repo_root, database_url) == []

    second = _manage(repo_root, database_url, "deploy_migrate")
    assert second.returncode == 0, second.stderr
    assert second.stdout.splitlines() == [START_LINE, SUCCESS_LINE]
    assert _unapplied(repo_root, database_url) == []


@pytest.mark.django_db
def test_in_process_run_on_a_migrated_database_prints_two_lines() -> None:
    out = StringIO()
    call_command("deploy_migrate", stdout=out)
    assert out.getvalue().splitlines() == [START_LINE, SUCCESS_LINE]


@pytest.mark.django_db
def test_migration_failure_propagates_without_the_success_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_migrate(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(deploy_migrate, "call_command", failing_migrate)
    out = StringIO()
    with pytest.raises(RuntimeError, match="migration failed"):
        call_command("deploy_migrate", stdout=out)
    assert out.getvalue().splitlines() == [START_LINE]


# --- 3, 4: PostgreSQL advisory lock --------------------------------------------------


def _lock_held_by_this_session() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(_LOCK_HELD_SQL, [deploy_migrate.DEPLOY_MIGRATE_LOCK_KEY])
        row = cursor.fetchone()
    assert row is not None
    return bool(row[0])


@requires_postgresql
@pytest.mark.django_db
def test_lock_is_held_during_migrate_and_released_after(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[tuple[Any, ...], dict[str, Any], bool]] = []

    def observing_migrate(*args: Any, **kwargs: Any) -> None:
        observed.append((args, kwargs, _lock_held_by_this_session()))

    monkeypatch.setattr(deploy_migrate, "call_command", observing_migrate)
    assert not _lock_held_by_this_session()

    call_command("deploy_migrate", stdout=StringIO())

    assert observed == [(("migrate",), {"interactive": False, "verbosity": 0}, True)]
    assert not _lock_held_by_this_session()


@requires_postgresql
@pytest.mark.django_db
def test_lock_is_released_when_migrate_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    held_during: list[bool] = []

    def failing_migrate(*args: Any, **kwargs: Any) -> None:
        held_during.append(_lock_held_by_this_session())
        raise RuntimeError("migration failed")

    monkeypatch.setattr(deploy_migrate, "call_command", failing_migrate)

    with pytest.raises(RuntimeError, match="migration failed"):
        call_command("deploy_migrate", stdout=StringIO())

    assert held_during == [True]
    assert not _lock_held_by_this_session()


@requires_postgresql
@pytest.mark.django_db
def test_times_out_without_migrating_while_another_session_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate_calls: list[object] = []
    monkeypatch.setattr(
        deploy_migrate, "call_command", lambda *a, **k: migrate_calls.append((a, k))
    )
    monkeypatch.setattr(deploy_migrate, "DEPLOY_MIGRATE_LOCK_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(deploy_migrate, "LOCK_POLL_INTERVAL_SECONDS", 0.1)

    other = connections.create_connection(DEFAULT_DB_ALIAS)
    try:
        with other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [deploy_migrate.DEPLOY_MIGRATE_LOCK_KEY])
        out = StringIO()
        with pytest.raises(CommandError) as raised:
            call_command("deploy_migrate", stdout=out)
    finally:
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(%s)", [deploy_migrate.DEPLOY_MIGRATE_LOCK_KEY]
            )
        other.close()

    message = str(raised.value)
    assert "migration lock" in message
    for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
        value = str(connection.settings_dict.get(key) or "")
        if value:
            assert value not in message, key
    assert migrate_calls == []
    assert out.getvalue().splitlines() == [START_LINE]
    assert not _lock_held_by_this_session()
