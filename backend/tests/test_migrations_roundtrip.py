"""Migrations apply, roll back to zero and apply again (PLAN-004 §6 rows 1 and 2).

Row 1 runs the real ``manage.py`` in a subprocess against a database created for the
test, so it really starts empty and the shared test database is left alone: a fresh
sqlite file always, and on PostgreSQL (when the test database is PostgreSQL) a scratch
database created and dropped here. After the round trip, ``seed_demo`` runs twice on the
fresh database (row 10: "runs after migrate").
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from django.core.management import call_command
from django.db import connection

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="needs PostgreSQL; the test database is not PostgreSQL",
)

ROUND_TRIP = (
    ("migrate",),
    ("migrate", "ledger", "zero"),
    ("migrate", "payments", "zero"),
    ("migrate",),
)
GUARD_TRIGGERS = {
    "invoices_provider_status_guard",
    "audit_log_append_only",
    *(
        f"{table}_payment_data_guard"
        for module in (
            "ledger.migrations.0002_postgresql_guards",
            "payments.migrations.0002_postgresql_guards",
        )
        for table in importlib.import_module(module).GUARDED_COLUMNS
    ),
}
GUARD_FUNCTIONS = {
    "finance_contains_card_number",
    "finance_reject_payment_data",
    "finance_guard_provider_status",
    "finance_audit_log_append_only",
}


def _manage(repo_root: Path, database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "backend/manage.py", *args],
        cwd=repo_root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def _applied(repo_root: Path, database_url: str, app_label: str) -> list[str]:
    result = _manage(repo_root, database_url, "showmigrations", app_label)
    assert result.returncode == 0, result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("[")]


def _round_trip(repo_root: Path, database_url: str) -> None:
    for step, args in enumerate(ROUND_TRIP):
        result = _manage(repo_root, database_url, *args, "--noinput")
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        assert result.stderr == "", (args, result.stderr)
        if args[1:] == ("ledger", "zero"):
            assert all(
                line.startswith("[ ]") for line in _applied(repo_root, database_url, "ledger")
            )
            # payments.0001 depends on ledger.0001 (it uses ledger's field and
            # validators), so both payments migrations were unapplied first.
            assert _applied(repo_root, database_url, "payments") == [
                "[ ] 0001_initial",
                "[ ] 0002_postgresql_guards",
                "[ ] 0003_webhook_event_processing",
            ]
        if step == len(ROUND_TRIP) - 1:
            for label in ("ledger", "payments"):
                applied = _applied(repo_root, database_url, label)
                assert applied and all(line.startswith("[X]") for line in applied), applied


def _seed_twice(repo_root: Path, database_url: str) -> None:
    for _ in range(2):
        result = _manage(repo_root, database_url, "seed_demo")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "25 invoices" in result.stdout


# --- Row 1: sqlite ------------------------------------------------------------------


def test_round_trip_on_a_fresh_sqlite_database(repo_root: Path, tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'round_trip.sqlite3'}"
    _round_trip(repo_root, database_url)
    _seed_twice(repo_root, database_url)


# --- Row 1: PostgreSQL --------------------------------------------------------------


@pytest.fixture
def scratch_postgresql_url() -> Iterator[str]:
    """A new, empty database on the test server; dropped afterwards."""
    import psycopg
    from psycopg import sql

    settings = connection.settings_dict
    name = f"plan004_round_trip_{uuid.uuid4().hex[:12]}"
    credentials = quote(str(settings["USER"] or ""), safe="")
    if settings["PASSWORD"]:
        credentials += ":" + quote(str(settings["PASSWORD"]), safe="")
    host = settings["HOST"] or "localhost"
    port = f":{settings['PORT']}" if settings["PORT"] else ""
    server = f"postgresql://{credentials}@{host}{port}"
    with psycopg.connect(f"{server}/postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield f"{server}/{name}"
    finally:
        with psycopg.connect(f"{server}/postgres", autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _guard_objects(database_url: str) -> tuple[set[str], set[str]]:
    import psycopg

    with psycopg.connect(database_url) as scratch:
        triggers = {
            row[0]
            for row in scratch.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        }
        functions = {
            row[0]
            for row in scratch.execute("SELECT proname FROM pg_proc WHERE proname LIKE 'finance_%'")
        }
    return triggers, functions


@requires_postgresql
def test_round_trip_on_a_fresh_postgresql_database(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    _round_trip(repo_root, scratch_postgresql_url)
    triggers, functions = _guard_objects(scratch_postgresql_url)
    assert GUARD_TRIGGERS <= triggers
    assert GUARD_FUNCTIONS <= functions

    for label in ("payments", "ledger"):
        result = _manage(repo_root, scratch_postgresql_url, "migrate", label, "zero", "--noinput")
        assert result.returncode == 0, result.stderr
    triggers, functions = _guard_objects(scratch_postgresql_url)
    assert not GUARD_TRIGGERS & triggers
    assert not GUARD_FUNCTIONS & functions

    result = _manage(repo_root, scratch_postgresql_url, "migrate", "--noinput")
    assert result.returncode == 0, result.stderr
    _seed_twice(repo_root, scratch_postgresql_url)


# --- Row 2 --------------------------------------------------------------------------


@pytest.mark.django_db
def test_models_and_migrations_agree() -> None:
    # makemigrations --check exits non-zero (SystemExit) when a migration is missing.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def test_payments_initial_migration_depends_on_ledger() -> None:
    # It imports ledger.models.MoneyField and ledger.validators.
    initial = importlib.import_module("payments.migrations.0001_initial")
    assert ("ledger", "0001_initial") in initial.Migration.dependencies
