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
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any
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
            "payments.migrations.0005_jobs",
            "ledger.migrations.0014_store_costs_deposit_resolutions",
            "ledger.migrations.0019_one_off_charges_constraints",
            "ledger.migrations.0023_contacts_and_calendar",
            "ledger.migrations.0028_contact_log_maturity",
            "ledger.migrations.0029_invoice_builder",
            "ledger.migrations.0030_disputes",
            "ledger.migrations.0031_line_kind_fees_consent",
            "ledger.migrations.0032_builder_item",
        )
        for table in importlib.import_module(module).GUARDED_COLUMNS
    ),
}
#: The trigger ledger 0017 dropped and 0019 replaced (``setup_charges`` became
#: ``one_off_charges``, PLAN-010d D1): gone after the round trip, back only below 0017.
RENAMED_TRIGGERS = {
    f"{table}_payment_data_guard"
    for table in importlib.import_module("ledger.migrations.0012_setup_charges").GUARDED_COLUMNS
    if table == "setup_charges"
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
                "[ ] 0004_webhook_object_id_source",
                "[ ] 0005_jobs",
                "[ ] 0006_job_kinds_reads",
                "[ ] 0007_reconciliation",
                "[ ] 0008_setup_payment_mark",
                "[ ] 0009_setup_payment_retry_mark",
                "[ ] 0010_one_off_jobs",
                "[ ] 0011_one_off_jobs_rows",
                "[ ] 0012_one_off_jobs_constraints",
                "[ ] 0013_invoice_pay_job",
                "[ ] 0014_mail_jobs",
                "[ ] 0015_dispute_jobs",
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
    assert not RENAMED_TRIGGERS & triggers

    for label in ("payments", "ledger"):
        result = _manage(repo_root, scratch_postgresql_url, "migrate", label, "zero", "--noinput")
        assert result.returncode == 0, result.stderr
    triggers, functions = _guard_objects(scratch_postgresql_url)
    assert not GUARD_TRIGGERS & triggers
    assert not RENAMED_TRIGGERS & triggers
    assert not GUARD_FUNCTIONS & functions

    result = _manage(repo_root, scratch_postgresql_url, "migrate", "--noinput")
    assert result.returncode == 0, result.stderr
    _seed_twice(repo_root, scratch_postgresql_url)


# --- The rename of PLAN-010d D1 over existing rows ----------------------------------
#
# The staging deployment of 2026-10-06 failed: ``ledger.0017`` updated ``audit_log`` rows
# that have an actor, which queues the deferred foreign-key checks of that table, and a
# later ``ALTER TABLE audit_log`` in the same transaction was refused ("pending trigger
# events"). The round trip above runs on empty tables and cannot see that, so this one
# seeds rows at the state before the rename (with the historical models, in the real
# ``manage.py``), migrates forward and back, and reads the rows with plain SQL.

STATE_BEFORE_RENAME = (
    ("ledger", "0016_store_cost_corrections"),
    ("payments", "0009_setup_payment_retry_mark"),
)


APPEND_ONLY_STATE = "SELECT tgenabled FROM pg_trigger WHERE tgname = 'audit_log_append_only'"


def seed_before_the_rename() -> None:
    """Seed rows with the models of ``STATE_BEFORE_RENAME``; print their ids as JSON.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    import json
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_RENAME)).apps
    user = apps.get_model("auth", "User")
    first = user.objects.create(username="rename-staff-one", password="!")
    second = user.objects.create(username="rename-staff-two", password="!")
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Rename LLC")
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 0917", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store, operator=operator, collection_status="prospective", payment_channel="unknown"
    )
    fee_type = apps.get_model("ledger", "FeeType").objects.create(
        code="rename_deposit", name="Rename deposit", refundable=True
    )
    charge = apps.get_model("ledger", "SetupCharge").objects.create(
        lease=lease, fee_type=fee_type, price=Decimal("100.00"), due_date=date(2026, 10, 1)
    )
    payment = apps.get_model("ledger", "Payment").objects.create(
        setup_charge=charge,
        kind="setup",
        method="wire",
        amount=Decimal("100.00"),
        status="settled",
        note="Wire.",
        recorded_by=first,
    )
    invoice = apps.get_model("ledger", "Invoice").objects.create(
        invoice_number="INV-2026-0917",
        operator=operator,
        setup_charge=charge,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        subtotal=Decimal("100.00"),
        total=Decimal("100.00"),
    )
    resolution = apps.get_model("ledger", "DepositResolution").objects.create(
        setup_charge=charge,
        kind="refunded",
        amount=Decimal("100.00"),
        resolved_on=date(2026, 10, 2),
        method="wire",
        note="Refunded by wire.",
        created_by=first,
    )
    job = apps.get_model("payments", "Job")
    mark = job.objects.create(
        kind="setup_payment_mark",
        entity_type="setup_charge",
        entity_id=charge.pk,
        requested_by=second,
    )
    retry = job.objects.create(
        kind="setup_payment_retry_mark", entity_type="setup_charge", entity_id=charge.pk
    )
    audit = apps.get_model("ledger", "AuditLog")
    pk = str(charge.pk)
    for actor, action, entity_type, entity_id, before, after in (
        (first, "setup_charge.created", "setup_charge", pk, None, {"setup_charge": pk}),
        (second, "setup_charge.waived", "setup_charge", pk, {"setup_charge": pk}, {"x": 1}),
        (first, "payment.recorded", "payment", str(payment.pk), None, {"setup_charge": pk}),
        (None, "setup_charge.created", "setup_charge", pk, None, None),
        (second, "lease.updated", "lease", str(lease.pk), {"a": 1}, {"a": 2}),
    ):
        audit.objects.create(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
        )
    print(
        json.dumps(
            {
                "users": [first.pk, second.pk],
                "operator": str(operator.pk),
                "charge": pk,
                "payment": str(payment.pk),
                "invoice": str(invoice.pk),
                "resolution": str(resolution.pk),
                "jobs": [str(mark.pk), str(retry.pk)],
                "lease": str(lease.pk),
            }
        )
    )


def _audit_rows(database_url: str) -> list[tuple[object, ...]]:
    import psycopg

    with psycopg.connect(database_url) as scratch:
        return list(
            scratch.execute(
                "SELECT actor_id, action, entity_type, before, after FROM audit_log ORDER BY id"
            )
        )


def _one(database_url: str, query: str) -> tuple[object, ...]:
    import psycopg

    with psycopg.connect(database_url) as scratch:
        rows = list(scratch.execute(query))
    assert len(rows) == 1, (query, rows)
    return tuple(rows[0])


def _expected_audit(seeded: dict[str, Any], entity: str) -> list[tuple[object, ...]]:
    first, second = seeded["users"]
    pk = seeded["charge"]
    return [
        (first, f"{entity}.created", entity, None, {entity: pk}),
        (second, f"{entity}.waived", entity, {entity: pk}, {"x": 1}),
        (first, "payment.recorded", "payment", None, {entity: pk}),
        (None, f"{entity}.created", entity, None, None),
        (second, "lease.updated", "lease", {"a": 1}, {"a": 2}),
    ]


def _jobs(database_url: str) -> list[tuple[object, ...]]:
    import psycopg

    with psycopg.connect(database_url) as scratch:
        return list(
            scratch.execute("SELECT kind, entity_type FROM jobs ORDER BY created_at, id::text")
        )


def _migrate_to(repo_root: Path, database_url: str, *targets: tuple[str, str]) -> None:
    for label, name in targets:
        result = _manage(repo_root, database_url, "migrate", label, name, "--noinput")
        assert result.returncode == 0, (label, name, result.stdout, result.stderr)


@requires_postgresql
def test_the_rename_migrates_over_existing_rows_and_back(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import json

    url = scratch_postgresql_url
    _migrate_to(repo_root, url, *STATE_BEFORE_RENAME)
    seeded_run = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_the_rename as seed; seed()",
    )
    assert seeded_run.returncode == 0, seeded_run.stderr
    seeded = json.loads(seeded_run.stdout.strip().splitlines()[-1])
    charge, operator = seeded["charge"], seeded["operator"]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _audit_rows(url) == _expected_audit(seeded, "one_off_charge")
    assert _one(
        url,
        "SELECT operator_id::text, lease_id::text, category, collection, description, status "
        "FROM one_off_charges",
    ) == (operator, seeded["lease"], "setup", "none", "Rename deposit", "owed")
    assert _one(url, "SELECT kind, one_off_charge_id::text FROM payments") == ("one_off", charge)
    assert _one(url, "SELECT one_off_charge_id::text FROM invoices") == (charge,)
    assert _one(url, "SELECT setup_charge_id::text FROM deposit_resolutions") == (charge,)
    assert _jobs(url) == [
        ("one_off_payment_mark", "one_off_charge"),
        ("one_off_payment_retry_mark", "one_off_charge"),
    ]
    assert _one(url, APPEND_ONLY_STATE) == ("O",)  # enabled

    _migrate_to(repo_root, url, *STATE_BEFORE_RENAME)
    assert _audit_rows(url) == _expected_audit(seeded, "setup_charge")
    assert _one(url, "SELECT lease_id::text, status FROM setup_charges") == (
        seeded["lease"],
        "owed",
    )
    assert _one(url, "SELECT kind, setup_charge_id::text FROM payments") == ("setup", charge)
    assert _one(url, "SELECT setup_charge_id::text FROM invoices") == (charge,)
    assert _one(url, "SELECT setup_charge_id::text FROM deposit_resolutions") == (charge,)
    assert _jobs(url) == [
        ("setup_payment_mark", "setup_charge"),
        ("setup_payment_retry_mark", "setup_charge"),
    ]
    assert _one(url, APPEND_ONLY_STATE) == ("O",)  # enabled

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _audit_rows(url) == _expected_audit(seeded, "one_off_charge")


# --- PLAN-012a: line kind, fees and surcharge, consent over existing rows --------------
#
# Seeds invoices and lines at ``ledger.0030`` (with the historical models, in the real
# ``manage.py``), migrates forward, back and forward again on both engines, and reads the
# rows with plain SQL: every line becomes ``charge``, every invoice gets fees and surcharge
# 0 and keeps its total. The reverse refuses while an invoice has fees.

STATE_BEFORE_KIND = (("ledger", "0030_disputes"),)
KIND_MIGRATION = ("ledger", "0031_line_kind_fees_consent")
INVOICE_ROWS = "SELECT invoice_number, subtotal, tax, total FROM invoices ORDER BY invoice_number"
INVOICE_MONEY = (1, 2, 3)
NEW_MONEY = (1, 2)
NEW_COLUMNS = (
    "SELECT i.invoice_number, i.fees, i.surcharge, l.position, l.kind "
    "FROM invoices i JOIN invoice_line_items l ON l.invoice_id = i.id "
    "ORDER BY i.invoice_number, l.position"
)


def seed_before_the_kind() -> None:
    """Seed two invoices with lines with the models of ``STATE_BEFORE_KIND``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_KIND)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Kind LLC")
    invoice_model = apps.get_model("ledger", "Invoice")
    line_model = apps.get_model("ledger", "InvoiceLineItem")
    for number, subtotal, tax, parts in (
        ("INV-2026-3101", Decimal("150.00"), Decimal("0.00"), ("100.00", "50.00")),
        ("INV-2026-3102", Decimal("12.34"), Decimal("1.00"), ("12.34",)),
    ):
        invoice = invoice_model.objects.create(
            invoice_number=number,
            operator=operator,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 10, 1),
            subtotal=subtotal,
            tax=tax,
            total=subtotal + tax,
        )
        for position, amount in enumerate(parts, 1):
            line_model.objects.create(
                invoice=invoice,
                position=position,
                description=f"Line {position}",
                quantity=Decimal("1.0000"),
                unit_price=Decimal(amount),
                line_total=Decimal(amount),
            )


def _rows(database_url: str, query: str, money: tuple[int, ...] = ()) -> list[tuple[str, ...]]:
    """Rows as text, so both engines compare alike; the ``money`` columns with two places
    (sqlite returns ``150`` for ``150.00``)."""
    if database_url.startswith("sqlite:///"):
        import sqlite3

        with closing(sqlite3.connect(database_url.removeprefix("sqlite:///"))) as scratch:
            found = scratch.execute(query).fetchall()
    else:
        import psycopg

        with psycopg.connect(database_url) as scratch:
            found = list(scratch.execute(query))
    return [
        tuple(
            f"{Decimal(str(value)):.2f}" if index in money else str(value)
            for index, value in enumerate(row)
        )
        for row in found
    ]


def _kind_round_trip(repo_root: Path, url: str) -> None:
    _migrate_to(repo_root, url, *STATE_BEFORE_KIND)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_the_kind as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    invoices = _rows(url, INVOICE_ROWS, INVOICE_MONEY)
    assert invoices == [
        ("INV-2026-3101", "150.00", "0.00", "150.00"),
        ("INV-2026-3102", "12.34", "1.00", "13.34"),
    ]
    expected = [
        ("INV-2026-3101", "0.00", "0.00", "1", "charge"),
        ("INV-2026-3101", "0.00", "0.00", "2", "charge"),
        ("INV-2026-3102", "0.00", "0.00", "1", "charge"),
    ]

    _migrate_to(repo_root, url, KIND_MIGRATION)
    assert _rows(url, INVOICE_ROWS, INVOICE_MONEY) == invoices
    assert _rows(url, NEW_COLUMNS, NEW_MONEY) == expected
    assert _rows(url, "SELECT sms_phone, sms_consent_at, email_consent_at FROM operators") == [
        ("", "None", "None")
    ]

    _migrate_to(repo_root, url, *STATE_BEFORE_KIND)
    assert _rows(url, INVOICE_ROWS, INVOICE_MONEY) == invoices

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, INVOICE_ROWS, INVOICE_MONEY) == invoices
    assert _rows(url, NEW_COLUMNS, NEW_MONEY) == expected

    # An invoice with fees cannot go back: the restored total CHECK could not hold it.
    with_fees = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from ledger.models import Invoice; from decimal import Decimal as D; "
        "Invoice.objects.filter(invoice_number='INV-2026-3102')"
        ".update(fees=D('1.00'), total=D('14.34'))",
    )
    assert with_fees.returncode == 0, with_fees.stderr
    refused = _manage(repo_root, url, "migrate", *STATE_BEFORE_KIND[0], "--noinput")
    assert refused.returncode != 0
    assert "refuses to reverse" in refused.stderr
    assert ("INV-2026-3102", "1.00", "0.00", "1", "charge") in _rows(url, NEW_COLUMNS, NEW_MONEY)


def test_the_kind_migration_over_existing_rows_on_sqlite(repo_root: Path, tmp_path: Path) -> None:
    _kind_round_trip(repo_root, f"sqlite:///{tmp_path / 'kind.sqlite3'}")


@requires_postgresql
def test_the_kind_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    _kind_round_trip(repo_root, scratch_postgresql_url)


# --- PLAN-012b: the Item column over existing rows ----------------------------------------
#
# Seeds draft lines (with a fee type and without one) and an invoice line at
# ``ledger.0031``, migrates forward, back and forward again on both engines, and reads
# the rows with plain SQL: a line's item is its fee type's name, else its old description,
# which becomes empty; the reverse restores a blank description from the item, cut to
# the description's 120 characters.

STATE_BEFORE_ITEM = (("ledger", "0031_line_kind_fees_consent"),)
ITEM_MIGRATION = ("ledger", "0032_builder_item")
DRAFT_LINES_AFTER = (
    "SELECT position, item, description, "
    "CASE WHEN fee_type_id IS NULL THEN 'typed' ELSE 'catalogue' END, line_total "
    "FROM invoice_draft_lines ORDER BY position"
)
DRAFT_LINES_BEFORE = (
    "SELECT position, description, "
    "CASE WHEN fee_type_id IS NULL THEN 'typed' ELSE 'catalogue' END, line_total "
    "FROM invoice_draft_lines ORDER BY position"
)
LINE_ITEM_ROWS = "SELECT description, details, line_total FROM invoice_line_items"


def seed_before_the_item() -> None:
    """Seed a draft with three lines and an invoice with one line with the models of
    ``STATE_BEFORE_ITEM``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_ITEM)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Item LLC")
    film = apps.get_model("ledger", "FeeType").objects.create(
        code="window_film_seed", name="Window film"
    )
    draft = apps.get_model("ledger", "InvoiceDraft").objects.create(operator=operator)
    line_model = apps.get_model("ledger", "InvoiceDraftLine")
    for position, fee_type, description in (
        (1, film, "Window film"),
        (2, film, "Film for the front door"),
        (3, None, "Poster repair"),
    ):
        line_model.objects.create(
            draft=draft,
            position=position,
            fee_type=fee_type,
            description=description,
            quantity=Decimal("1.0000"),
            unit_amount=Decimal("10.00"),
            line_total=Decimal("10.00"),
        )
    invoice = apps.get_model("ledger", "Invoice").objects.create(
        invoice_number="INV-2026-3201",
        operator=operator,
        issue_date=date(2026, 10, 1),
        due_date=date(2026, 10, 16),
        subtotal=Decimal("10.00"),
        tax=Decimal("0.00"),
        total=Decimal("10.00"),
    )
    apps.get_model("ledger", "InvoiceLineItem").objects.create(
        invoice=invoice,
        position=1,
        description="Poster repair",
        quantity=Decimal("1.0000"),
        unit_price=Decimal("10.00"),
        line_total=Decimal("10.00"),
    )


def _item_round_trip(repo_root: Path, url: str) -> None:
    _migrate_to(repo_root, url, *STATE_BEFORE_ITEM)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_the_item as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    before = [
        ("1", "Window film", "catalogue", "10.00"),
        ("2", "Film for the front door", "catalogue", "10.00"),
        ("3", "Poster repair", "typed", "10.00"),
    ]
    assert _rows(url, DRAFT_LINES_BEFORE, (3,)) == before
    after = [
        ("1", "Window film", "Window film", "catalogue", "10.00"),
        ("2", "Window film", "Film for the front door", "catalogue", "10.00"),
        ("3", "Poster repair", "", "typed", "10.00"),
    ]
    line_items = [("Poster repair", "", "10.00")]

    _migrate_to(repo_root, url, ITEM_MIGRATION)
    assert _rows(url, DRAFT_LINES_AFTER, (4,)) == after
    assert _rows(url, LINE_ITEM_ROWS, (2,)) == line_items

    _migrate_to(repo_root, url, *STATE_BEFORE_ITEM)
    assert _rows(url, DRAFT_LINES_BEFORE, (3,)) == before

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, DRAFT_LINES_AFTER, (4,)) == after
    assert _rows(url, LINE_ITEM_ROWS, (2,)) == line_items

    # A typed item longer than the old description goes back cut to 120 characters.
    changed = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from ledger.models import InvoiceDraftLine, InvoiceLineItem; "
        "InvoiceDraftLine.objects.filter(position=3).update(item='y' * 200); "
        "InvoiceLineItem.objects.update(details='Left panel')",
    )
    assert changed.returncode == 0, changed.stderr
    assert ("3", "y" * 200, "", "typed", "10.00") in _rows(url, DRAFT_LINES_AFTER, (4,))
    _migrate_to(repo_root, url, *STATE_BEFORE_ITEM)
    assert _rows(url, DRAFT_LINES_BEFORE, (3,)) == [*before[:2], ("3", "y" * 120, "typed", "10.00")]
    assert _rows(url, "SELECT description, line_total FROM invoice_line_items", (1,)) == [
        ("Poster repair", "10.00")
    ]


def test_the_item_migration_over_existing_rows_on_sqlite(repo_root: Path, tmp_path: Path) -> None:
    _item_round_trip(repo_root, f"sqlite:///{tmp_path / 'item.sqlite3'}")


@requires_postgresql
def test_the_item_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    _item_round_trip(repo_root, scratch_postgresql_url)


# --- Row 2 --------------------------------------------------------------------------


@pytest.mark.django_db
def test_models_and_migrations_agree() -> None:
    # makemigrations --check exits non-zero (SystemExit) when a migration is missing.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def test_payments_initial_migration_depends_on_ledger() -> None:
    # It imports ledger.models.MoneyField and ledger.validators.
    initial = importlib.import_module("payments.migrations.0001_initial")
    assert ("ledger", "0001_initial") in initial.Migration.dependencies
