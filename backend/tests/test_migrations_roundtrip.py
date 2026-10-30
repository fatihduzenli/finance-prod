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
import re
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
#: Every migration since the guards began that may add a guarded column, in both apps, from
#: ledger 0041 and payments 0018 on without exception (a migration that adds nothing is listed
#: with an empty ``GUARDED_COLUMNS`` or none at all): tests/test_card_number_guard.py keeps the
#: two lists complete (PLAN-012f-b Batch 0).
GUARD_MODULES = (
    "ledger.migrations.0002_postgresql_guards",
    "ledger.migrations.0014_store_costs_deposit_resolutions",
    "ledger.migrations.0019_one_off_charges_constraints",
    "ledger.migrations.0023_contacts_and_calendar",
    "ledger.migrations.0028_contact_log_maturity",
    "ledger.migrations.0029_invoice_builder",
    "ledger.migrations.0030_disputes",
    "ledger.migrations.0031_line_kind_fees_consent",
    "ledger.migrations.0032_builder_item",
    "ledger.migrations.0034_proration",
    "ledger.migrations.0035_lease_end_transfer",
    "ledger.migrations.0037_transfer_fields",
    "ledger.migrations.0038_installments",
    "ledger.migrations.0039_transfer_debt_override",
    "ledger.migrations.0040_lease_end_statement_opened",
    "ledger.migrations.0041_rent_lines",
    "ledger.migrations.0042_payment_allocation_and_credits",
    "ledger.migrations.0043_fee_type_show_in_setup",
    "ledger.migrations.0044_draft_existing_charge",
    "ledger.migrations.0045_company_settings",
    "ledger.migrations.0046_copy_labels",
    "ledger.migrations.0047_store_address",
    "ledger.migrations.0048_check_method",
    "ledger.migrations.0049_autopay_plans",
    "ledger.migrations.0050_rename_statement_saved_at",
    "ledger.migrations.0051_store_type_and_state_codes",
    "ledger.migrations.0052_operator_kind",
    "ledger.migrations.0053_cash_method",
    "ledger.migrations.0054_installment_settlement",
    "ledger.migrations.0055_installment_plan_paused",
    "ledger.migrations.0056_rent_charge_bank_debit",
    "ledger.migrations.0057_payment_void_fields",
    "ledger.migrations.0058_brands_and_store_notes",
    "payments.migrations.0002_postgresql_guards",
    "payments.migrations.0005_jobs",
    "payments.migrations.0018_copy_labels",
)


def guarded_columns(module: str) -> dict[str, tuple[str, ...]]:
    """The ``GUARDED_COLUMNS`` a migration module declares; none when it declares nothing."""
    loaded = importlib.import_module(module)
    found: dict[str, tuple[str, ...]] = getattr(loaded, "GUARDED_COLUMNS", {})
    return found


#: The triggers a later migration renamed with their table: ledger 0017 dropped the
#: ``setup_charges`` one and 0019 replaced it (``setup_charges`` became ``one_off_charges``,
#: PLAN-010d D1), and ledger 0058 renamed ``store_types`` to ``brands`` with its trigger
#: (PLAN-014 B1): gone after the round trip, back only below the rename.
RENAMED_TRIGGERS = {
    f"{table}_payment_data_guard"
    for table in importlib.import_module("ledger.migrations.0012_setup_charges").GUARDED_COLUMNS
    if table == "setup_charges"
} | {"store_types_payment_data_guard"}
GUARD_TRIGGERS = {
    "invoices_provider_status_guard",
    "audit_log_append_only",
    *(
        f"{table}_payment_data_guard"
        for module in GUARD_MODULES
        for table in guarded_columns(module)
    ),
} - RENAMED_TRIGGERS
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
                "[ ] 0016_lease_end_jobs",
                "[ ] 0017_autopay_stop_job",
                "[ ] 0018_copy_labels",
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


# --- PLAN-012c-b: one open promise per lease over existing rows --------------------------
#
# Seeds promises at ``ledger.0032``: three open on lease A (the middle one already closes an
# older rescheduled promise), a deleted open one on lease A, one open on lease B and two
# open without a lease. Migrates forward, back (a no-op) and forward again on both engines.

STATE_BEFORE_ONE_OPEN = (("ledger", "0032_builder_item"),)
ONE_OPEN_MIGRATION = ("ledger", "0033_one_open_promise_per_lease")
PROMISE_ROWS = (
    "SELECT e.body, e.status, COALESCE(c.body, '-'), "
    "CASE WHEN e.deleted_at IS NULL THEN 'live' ELSE 'deleted' END "
    "FROM contact_entries e LEFT JOIN contact_entries c ON c.id = e.closed_by_id "
    "ORDER BY e.body"
)


def _migration_audits(url: str) -> list[tuple[str, str]]:
    """The outcome audit rows as (the promise's body, the after as text), by body; ids
    compared as UUIDs (sqlite stores them without dashes)."""
    bodies = {
        uuid.UUID(key): body for key, body in _rows(url, "SELECT id, body FROM contact_entries")
    }
    audits = _rows(
        url,
        "SELECT entity_id, after FROM audit_log WHERE action = 'contact_entry.outcome_recorded'",
    )
    return sorted((bodies[uuid.UUID(key)], after) for key, after in audits)


def seed_before_one_open() -> None:
    """Seed the promises above with the models of ``STATE_BEFORE_ONE_OPEN``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import UTC, date, datetime, timedelta
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_ONE_OPEN)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Sena LLC")
    store_model = apps.get_model("ledger", "Store")
    lease_model = apps.get_model("ledger", "Lease")
    leases = {
        label: lease_model.objects.create(
            store=store_model.objects.create(external_label=label, state="GA", status="active"),
            operator=operator,
            debit_day=5,
            collection_status="active",
            payment_channel="wire",
        )
        for label in ("A", "B")
    }
    admin = apps.get_model("auth", "User").objects.create(username="seed-admin")
    entries = apps.get_model("ledger", "ContactEntry")
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    made = {}
    for minute, (body, lease) in enumerate(
        (
            ("X", "A"),
            ("A1", "A"),
            ("A2", "A"),
            ("A3", "A"),
            ("Adel", "A"),
            ("B1", "B"),
            ("N1", None),
            ("N2", None),
        )
    ):
        row = entries.objects.create(
            operator=operator,
            lease=leases[lease] if lease else None,
            kind="promise",
            happened_on=date(2026, 10, 1),
            promised_date=date(2026, 10, 5),
            promised_amount=Decimal("250.00"),
            body=body,
        )
        entries.objects.filter(pk=row.pk).update(created_at=start + timedelta(minutes=minute))
        made[body] = row
    entries.objects.filter(pk=made["X"].pk).update(status="rescheduled", closed_by=made["A2"])
    entries.objects.filter(pk=made["Adel"].pk).update(
        deleted_at=start, deleted_by=admin, delete_reason="Wrong operator."
    )


def _one_open_round_trip(repo_root: Path, url: str) -> None:
    _migrate_to(repo_root, url, *STATE_BEFORE_ONE_OPEN)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_one_open as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    before = _rows(url, PROMISE_ROWS)
    assert [row[1] for row in before].count("open") == 7
    after = [
        ("A1", "rescheduled", "-", "live"),  # A2 already closes X: closed_by stays null
        ("A2", "rescheduled", "A3", "live"),
        ("A3", "open", "-", "live"),
        ("Adel", "open", "-", "deleted"),
        ("B1", "open", "-", "live"),
        ("N1", "rescheduled", "N2", "live"),
        ("N2", "open", "-", "live"),
        ("X", "rescheduled", "A2", "live"),
    ]

    _migrate_to(repo_root, url, ONE_OPEN_MIGRATION)
    assert _rows(url, PROMISE_ROWS) == after
    audits = _migration_audits(url)
    assert [row[0] for row in audits] == ["A1", "A2", "N1"]
    assert all("0033" in row[1] and "rescheduled" in row[1] for row in audits)

    # The reverse is a no-op; forward again changes nothing and writes no audit row.
    _migrate_to(repo_root, url, *STATE_BEFORE_ONE_OPEN)
    assert _rows(url, PROMISE_ROWS) == after
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, PROMISE_ROWS) == after
    assert _migration_audits(url) == audits


def test_the_one_open_promise_migration_over_existing_rows_on_sqlite(
    repo_root: Path, tmp_path: Path
) -> None:
    _one_open_round_trip(repo_root, f"sqlite:///{tmp_path / 'one-open.sqlite3'}")


@requires_postgresql
def test_the_one_open_promise_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    _one_open_round_trip(repo_root, scratch_postgresql_url)


# --- PLAN-012e: proration, lease ends, message kinds, the documents job ------------------
#
# Seeds a lease, a rent charge, a collection e-mail and a deposit resolution at ``ledger.0033``
# and ``payments.0015``, migrates forward, back and forward again on both engines, and reads
# the rows with plain SQL: the lease gets the switch on, the charge no proration text, the
# e-mail kind ``ladder``, the resolution an offset of 0; the final statement template is
# seeded and taken back. The reverse refuses while a lease end exists.

STATE_BEFORE_LEASE_END = (
    ("ledger", "0033_one_open_promise_per_lease"),
    ("payments", "0015_dispute_jobs"),
)
LEASE_END_ROWS = (
    "SELECT l.prorate_first_month, r.amount_due, r.proration_text, m.kind, m.document_id, "
    "d.offset_amount FROM leases l JOIN rent_charges r ON r.lease_id = l.id "
    "JOIN outbound_messages m ON m.operator_id = l.operator_id "
    "JOIN deposit_resolutions d ON d.created_by_id IS NULL"
)
FINAL_TEMPLATE = "SELECT count(*) FROM message_templates WHERE code = 'final_statement'"


def seed_before_lease_end() -> None:
    """Seed the rows above with the models of ``STATE_BEFORE_LEASE_END``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    import uuid as uuid_module
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_LEASE_END)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(
        display_name="End LLC", email="end@example.com"
    )
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1201", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        collection_status="active",
        payment_channel="wire",
    )
    apps.get_model("ledger", "RentCharge").objects.create(
        lease=lease,
        period_month=date(2026, 10, 1),
        due_on=date(2026, 10, 1),
        amount_due=Decimal("2000.00"),
        status="due",
    )
    step = apps.get_model("ledger", "CollectionStep").objects.get(day_offset=3)
    apps.get_model("ledger", "OutboundMessage").objects.create(
        operator=operator,
        template=step.template,
        step=step,
        charge_entity_type="rent_charge",
        charge_entity_id=uuid_module.uuid4(),
        to_address="end@example.com",
    )
    fee_type = apps.get_model("ledger", "FeeType").objects.create(
        code="end_deposit", name="End deposit", refundable=True
    )
    charge = apps.get_model("ledger", "OneOffCharge").objects.create(
        lease=lease,
        operator=operator,
        fee_type=fee_type,
        price=Decimal("500.00"),
        due_date=date(2026, 1, 1),
    )
    apps.get_model("ledger", "DepositResolution").objects.create(
        setup_charge=charge,
        kind="withheld",
        amount=Decimal("500.00"),
        withheld_amount=Decimal("500.00"),
        resolved_on=date(2026, 10, 2),
        note="Kept for repairs.",
    )


def _lease_end_round_trip(repo_root: Path, url: str) -> None:
    _migrate_to(repo_root, url, *STATE_BEFORE_LEASE_END)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_lease_end as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    after = [("True", "2000.00", "None", "ladder", "None", "0.00")]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    rows = _rows(url, LEASE_END_ROWS, (1, 5))
    # sqlite reads a boolean as 1.
    assert [("True" if row[0] in ("True", "1") else row[0], *row[1:]) for row in rows] == after
    assert _rows(url, FINAL_TEMPLATE) == [("1",)]

    _migrate_to(repo_root, url, *STATE_BEFORE_LEASE_END)
    assert _rows(url, FINAL_TEMPLATE) == [("0",)]
    assert _rows(url, "SELECT amount_due FROM rent_charges", (0,)) == [("2000.00",)]
    assert _rows(url, "SELECT count(*) FROM outbound_messages") == [("1",)]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, FINAL_TEMPLATE) == [("1",)]

    # A lease end cannot be lost by going back.
    made = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from ledger.models import Lease, LeaseEnd; "
        "LeaseEnd.objects.create(lease=Lease.objects.get())",
    )
    assert made.returncode == 0, made.stderr
    refused = _manage(repo_root, url, "migrate", "ledger", "0034_proration", "--noinput")
    assert refused.returncode != 0 and "refuses to reverse" in refused.stderr
    assert _rows(url, "SELECT count(*) FROM lease_ends") == [("1",)]


def test_the_lease_end_migrations_over_existing_rows_on_sqlite(
    repo_root: Path, tmp_path: Path
) -> None:
    _lease_end_round_trip(repo_root, f"sqlite:///{tmp_path / 'lease-end.sqlite3'}")


@requires_postgresql
def test_the_lease_end_migrations_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    _lease_end_round_trip(repo_root, scratch_postgresql_url)


# --- PLAN-012e D6: the transfer wizard's state ------------------------------------------
#
# Seeds a bare transfer (store and outgoing lease only) at ``ledger.0036``, migrates
# forward, back and forward again on PostgreSQL (the engine of the trigger; sqlite runs
# 0037 in the plain round trip), and reads the row with plain SQL: a draft, no incoming
# terms, the first month prorated, no setup line. The new JSON column is guarded, and
# the reverse refuses once a transfer holds an incoming term.

STATE_BEFORE_TRANSFER_FIELDS = (("ledger", "0036_messages_kind"),)
TRANSFER_ROWS = (
    "SELECT status, incoming_operator_id, incoming_rent, incoming_prorate_first_month, "
    "deposit_amount, setup_lines::text FROM lease_transfers"
)


def seed_before_transfer_fields() -> None:
    """Seed one bare transfer with the models of ``STATE_BEFORE_TRANSFER_FIELDS``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_TRANSFER_FIELDS)).apps
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1301", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store, collection_status="prospective", payment_channel="unknown"
    )
    apps.get_model("ledger", "LeaseTransfer").objects.create(store=store, outgoing_lease=lease)


@requires_postgresql
def test_the_transfer_fields_migration_over_an_existing_row_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_TRANSFER_FIELDS)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_transfer_fields as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    after = [("draft", "None", "None", "True", "None", "[]")]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, TRANSFER_ROWS) == after
    _migrate_to(repo_root, url, *STATE_BEFORE_TRANSFER_FIELDS)
    assert _rows(url, "SELECT count(*) FROM lease_transfers") == [("1",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, TRANSFER_ROWS) == after

    # The setup lines are guarded by the trigger.
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(
            "UPDATE lease_transfers SET setup_lines = "
            """'[{"price": "1.00", "fee_type": "4111 1111 1111 1111"}]'"""
        )
    # A transfer holding an incoming term cannot be lost by going back.
    with psycopg.connect(url) as scratch:
        scratch.execute("UPDATE lease_transfers SET incoming_rent = 2500")
    refused = _manage(repo_root, url, "migrate", "ledger", "0036_messages_kind", "--noinput")
    assert refused.returncode != 0 and "refuses to reverse" in refused.stderr
    assert _rows(url, "SELECT incoming_rent FROM lease_transfers", (0,)) == [("2500.00",)]


# --- PLAN-012e D9: installment plans ----------------------------------------------------
#
# Seeds one one-off charge at ``ledger.0037``, migrates forward, back and forward again on
# PostgreSQL (the engine of the trigger; sqlite runs 0038 in the plain round trip), and
# reads the row with plain SQL: unchanged, no plan, no installment number. The plan's
# note is guarded by the trigger, the new category is accepted only with a plan, and the
# reverse refuses once a plan exists.

STATE_BEFORE_INSTALLMENTS = (("ledger", "0037_transfer_fields"),)
CHARGE_ROWS = "SELECT category, price, status, plan_id, installment_no FROM one_off_charges"


def seed_before_installments() -> None:
    """Seed one arrears charge with the models of ``STATE_BEFORE_INSTALLMENTS``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_INSTALLMENTS)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Plan Co")
    apps.get_model("ledger", "OneOffCharge").objects.create(
        operator=operator,
        category="arrears",
        collection="manual",
        description="Arrears",
        price=Decimal("1000.00"),
        due_date=date(2026, 10, 1),
        status="owed",
    )


@requires_postgresql
def test_the_installments_migration_over_an_existing_row_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_INSTALLMENTS)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_installments as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    after = [("arrears", "1000.00", "owed", "None", "None")]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, CHARGE_ROWS, (1,)) == after
    _migrate_to(repo_root, url, *STATE_BEFORE_INSTALLMENTS)
    assert _rows(url, "SELECT count(*) FROM one_off_charges") == [("1",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, CHARGE_ROWS, (1,)) == after

    with psycopg.connect(url) as scratch:
        [(charge, operator)] = list(scratch.execute("SELECT id, operator_id FROM one_off_charges"))
        # An installment without a plan breaks its CHECK.
        with pytest.raises(psycopg.errors.CheckViolation):
            scratch.execute("UPDATE one_off_charges SET category = 'installment'")
    plan = (
        "INSERT INTO installment_plans (id, one_off_charge_id, operator_id, count, "
        "first_due, cadence, collection, status, confirmed_at, note, created_at, updated_at, "
        "origin) "  # origin since PLAN-012f-b Batch D (0054)
        "VALUES (%s, %s, %s, 3, '2026-11-01', 'monthly', 'manual', 'active', now(), %s, "
        "now(), now(), 'split')"
    )
    # The plan's note is guarded by the trigger.
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(plan, (uuid.uuid4(), charge, operator, "4111 1111 1111 1111"))
    with psycopg.connect(url) as scratch:
        scratch.execute(plan, (uuid.uuid4(), charge, operator, ""))
    # A plan cannot be lost by going back.
    refused = _manage(repo_root, url, "migrate", "ledger", "0037_transfer_fields", "--noinput")
    assert refused.returncode != 0 and "refuses to reverse" in refused.stderr
    assert _rows(url, "SELECT count(*) FROM installment_plans") == [("1",)]


# --- PLAN-012e D14: a lease-end balance allowed to remain after a transfer --------------
#
# Seeds a bare transfer at ``ledger.0038``, migrates forward, back and forward again on
# PostgreSQL (the engine of the trigger; sqlite runs 0039 in the plain round trip), and
# reads the row with plain SQL: nothing allowed. The reason is guarded by the trigger, the
# CHECK holds the four columns together, and the reverse drops them plainly.

STATE_BEFORE_DEBT_OVERRIDE = (("ledger", "0038_installments"),)
OVERRIDE_ROWS = (
    "SELECT status, debt_override_amount, debt_override_reason, debt_override_by_id, "
    "debt_override_at FROM lease_transfers"
)


def seed_before_debt_override() -> None:
    """Seed one bare transfer with the models of ``STATE_BEFORE_DEBT_OVERRIDE``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_DEBT_OVERRIDE)).apps
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1401", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store, collection_status="prospective", payment_channel="unknown"
    )
    apps.get_model("ledger", "LeaseTransfer").objects.create(store=store, outgoing_lease=lease)


@requires_postgresql
def test_the_debt_override_migration_over_an_existing_row_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_DEBT_OVERRIDE)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_debt_override as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    after = [("draft", "None", "", "None", "None")]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, OVERRIDE_ROWS) == after
    _migrate_to(repo_root, url, *STATE_BEFORE_DEBT_OVERRIDE)
    assert _rows(url, "SELECT count(*) FROM lease_transfers") == [("1",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, OVERRIDE_ROWS) == after

    allow = (
        "UPDATE lease_transfers SET debt_override_amount = 500, debt_override_at = now(), "
        "debt_override_reason = %s"
    )
    # The reason is guarded by the trigger.
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(allow, ("4111 1111 1111 1111",))
    # An amount without a reason, or a reason without its moment, breaks the CHECK.
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(allow, (" ",))
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute("UPDATE lease_transfers SET debt_override_reason = 'Agreed'")
    with psycopg.connect(url) as scratch:
        scratch.execute(allow, ("Agreed with the owner",))
    # The setup lines stay guarded by the recreated trigger.
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(
            "UPDATE lease_transfers SET setup_lines = "
            """'[{"price": "1.00", "fee_type": "4111 1111 1111 1111"}]'"""
        )
    # The reverse drops the columns plainly; the transfer stays, still guarded by 0037's
    # trigger.
    _migrate_to(repo_root, url, *STATE_BEFORE_DEBT_OVERRIDE)
    assert _rows(url, "SELECT count(*) FROM lease_transfers") == [("1",)]
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(
            "UPDATE lease_transfers SET setup_lines = "
            """'[{"price": "1.00", "fee_type": "4111 1111 1111 1111"}]'"""
        )
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, OVERRIDE_ROWS) == after


# --- PLAN-012b-b D9: when a lease end's statement step opened ---------------------------
#
# Seeds a draft lease end at ``ledger.0039``, migrates forward, back and forward again on
# PostgreSQL (sqlite runs 0040 in the plain round trip) and reads the row with plain SQL:
# the new column is null (the draft is not in progress) and the reverse drops it plainly.

STATE_BEFORE_STATEMENT_OPENED = (("ledger", "0039_transfer_debt_override"),)
OPENED_ROWS = "SELECT status, ended_on, statement_saved_at FROM lease_ends"


def seed_before_statement_opened() -> None:
    """Seed one draft lease end with the models of ``STATE_BEFORE_STATEMENT_OPENED``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_STATEMENT_OPENED)).apps
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1402", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store, collection_status="prospective", payment_channel="unknown"
    )
    apps.get_model("ledger", "LeaseEnd").objects.create(lease=lease)


@requires_postgresql
def test_the_statement_opened_migration_over_an_existing_row_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_STATEMENT_OPENED)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_statement_opened as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    after = [("draft", "None", "None")]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, OPENED_ROWS) == after
    with psycopg.connect(url) as scratch:
        scratch.execute("UPDATE lease_ends SET statement_saved_at = now()")
    _migrate_to(repo_root, url, *STATE_BEFORE_STATEMENT_OPENED)
    assert _rows(url, "SELECT count(*) FROM lease_ends") == [("1",)]
    assert _rows(
        url,
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'lease_ends' "
        "AND column_name = 'statement_opened_at'",
    ) == [("0",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, OPENED_ROWS) == after


# --- PLAN-012b-b D4, D5: rent billed by invoice ----------------------------------------
#
# Seeds a rent charge, a draft line and an invoice line at ``ledger.0040``, migrates
# forward, back and forward again on PostgreSQL (sqlite runs 0041 in the plain round
# trip) and reads the rows with plain SQL: the new columns are null, the CHECKs hold, and
# the reverse drops them plainly.

STATE_BEFORE_RENT_LINES = (("ledger", "0040_lease_end_statement_opened"),)
RENT_LINE_ROWS = (
    "SELECT (SELECT count(*) FROM invoice_draft_lines WHERE rent_lease_id IS NULL "
    "AND rent_month IS NULL), "
    "(SELECT count(*) FROM invoice_line_items WHERE rent_charge_id IS NULL), "
    "(SELECT count(*) FROM rent_charges WHERE billed_by_invoice_id IS NULL)"
)


def seed_before_rent_lines() -> None:
    """Seed a rent charge, a draft with one line and an invoice with one line with the
    models of ``STATE_BEFORE_RENT_LINES``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_RENT_LINES)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Northwind")
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1403", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        collection_status="active",
        payment_channel="wire",
    )
    apps.get_model("ledger", "RentCharge").objects.create(
        lease=lease,
        period_month=date(2026, 10, 1),
        due_on=date(2026, 10, 1),
        amount_due=Decimal("1000.00"),
        status="due",
    )
    draft = apps.get_model("ledger", "InvoiceDraft").objects.create(operator=operator)
    apps.get_model("ledger", "InvoiceDraftLine").objects.create(
        draft=draft,
        position=1,
        item="Cleaning",
        quantity=Decimal("1"),
        unit_amount=Decimal("50.00"),
        line_total=Decimal("50.00"),
    )
    invoice = apps.get_model("ledger", "Invoice").objects.create(
        invoice_number="INV-2026-0901",
        operator=operator,
        issue_date=date(2026, 10, 1),
        due_date=date(2026, 10, 15),
        subtotal=Decimal("50.00"),
        total=Decimal("50.00"),
    )
    apps.get_model("ledger", "InvoiceLineItem").objects.create(
        invoice=invoice,
        position=1,
        description="Cleaning",
        quantity=Decimal("1"),
        unit_price=Decimal("50.00"),
        line_total=Decimal("50.00"),
    )


@requires_postgresql
def test_the_rent_lines_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_RENT_LINES)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_rent_lines as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    after = [("1", "1", "1")]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, RENT_LINE_ROWS) == after
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        # A month without its lease breaks "both or neither".
        scratch.execute("UPDATE invoice_draft_lines SET rent_month = '2026-10-01'")
    with psycopg.connect(url) as scratch:
        scratch.execute(
            "UPDATE invoice_draft_lines SET rent_lease_id = (SELECT id FROM leases), "
            "rent_month = '2026-10-01'"
        )
        scratch.execute("UPDATE rent_charges SET billed_by_invoice_id = (SELECT id FROM invoices)")
        scratch.execute(
            "UPDATE invoice_line_items SET rent_charge_id = (SELECT id FROM rent_charges)"
        )
    _migrate_to(repo_root, url, *STATE_BEFORE_RENT_LINES)
    assert _rows(url, "SELECT count(*) FROM invoice_draft_lines") == [("1",)]
    assert _rows(
        url,
        "SELECT count(*) FROM information_schema.columns WHERE column_name IN "
        "('rent_lease_id', 'rent_month', 'billed_by_invoice_id') "
        "OR (table_name = 'invoice_line_items' AND column_name = 'rent_charge_id')",
    ) == [("0",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, RENT_LINE_ROWS) == after


# --- PLAN-012d D6a, D6b: manual payment allocation and lease credits -------------------
#
# Seeds a lease, a rent charge and a manual payment at ``ledger.0041``, migrates forward,
# back and forward again on PostgreSQL (sqlite runs 0042 in the plain round trip) and reads
# the rows with plain SQL: the new columns are null, the new CHECKs and the payment-data
# trigger on ``lease_credits`` hold, and the reverse refuses while a credit exists.

STATE_BEFORE_CREDITS = (("ledger", "0041_rent_lines"),)
CREDIT_ROWS = (
    "SELECT (SELECT count(*) FROM payments WHERE allocation_group IS NULL "
    "AND credit_id IS NULL), (SELECT count(*) FROM payments WHERE method = 'zelle')"
)


def seed_before_credits() -> None:
    """Seed a lease, a rent charge and a Zelle payment on it with the models of
    ``STATE_BEFORE_CREDITS``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_CREDITS)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Eastwind")
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1404", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        collection_status="active",
        payment_channel="wire",
    )
    charge = apps.get_model("ledger", "RentCharge").objects.create(
        lease=lease,
        period_month=date(2026, 10, 1),
        due_on=date(2026, 10, 1),
        amount_due=Decimal("1000.00"),
        status="due",
    )
    apps.get_model("ledger", "Payment").objects.create(
        rent_charge=charge,
        kind="rent",
        method="zelle",
        amount=Decimal("400.00"),
        status="settled",
        settled_on=date(2026, 10, 2),
        note="Zelle received.",
    )


@requires_postgresql
def test_the_credits_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_CREDITS)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_credits as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    after = [("1", "1")]

    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, CREDIT_ROWS) == after
    credit = (
        "INSERT INTO lease_credits (id, lease_id, amount, remaining, method, received_on, "
        "reference, note, allocation_group, created_at, updated_at) "
        "VALUES (%s, (SELECT id FROM leases), %s, %s, %s, '2026-10-02', '', %s, %s, now(), now())"
    )
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        # The payment-data trigger on the new text column.
        scratch.execute(
            credit, (uuid.uuid4(), "50.00", "50.00", "zelle", "4111 1111 1111 1111", uuid.uuid4())
        )
    for amount, remaining, method, note in (
        ("50.00", "60.00", "zelle", "Kept"),  # remaining above the amount
        ("50.00", "50.00", "credit", "Kept"),  # not a manual method
        ("50.00", "50.00", "zelle", " "),  # a blank note
        ("0.00", "0.00", "zelle", "Kept"),  # not above zero
    ):
        with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
            scratch.execute(credit, (uuid.uuid4(), amount, remaining, method, note, uuid.uuid4()))
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        # A credit payment names its credit.
        scratch.execute("UPDATE payments SET method = 'credit'")
    with psycopg.connect(url) as scratch:
        scratch.execute(credit, (uuid.uuid4(), "50.00", "50.00", "zelle", "Kept", uuid.uuid4()))
        scratch.execute("UPDATE payments SET allocation_group = %s", (uuid.uuid4(),))
    # The reverse refuses while a credit exists.
    back = _manage(repo_root, url, "migrate", *STATE_BEFORE_CREDITS[0], "--noinput")
    assert back.returncode != 0 and "refuses to reverse" in back.stderr
    assert _rows(url, "SELECT count(*) FROM lease_credits") == [("1",)]
    with psycopg.connect(url) as scratch:
        scratch.execute("DELETE FROM lease_credits")
    _migrate_to(repo_root, url, *STATE_BEFORE_CREDITS)
    assert _rows(url, "SELECT count(*) FROM payments") == [("1",)]
    assert _rows(
        url,
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'payments' "
        "AND column_name IN ('allocation_group', 'credit_id')",
    ) == [("0",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, CREDIT_ROWS) == after


# --- PLAN-012d D9, D10: the checklist flag and existing-charge lines ---------------------
#
# Seeds fee types (active, inactive, an active NSF fee) at ``ledger.0042``, then a draft
# line and an open one-off charge, migrates forward, back and forward again on both
# engines and reads the rows with plain SQL: 0043 shows exactly the active fee types that
# are not NSF fees in the checklist; 0044's column starts null, and the reverse drops both
# columns plainly with every row kept.

STATE_BEFORE_SETUP_FLAG = (("ledger", "0042_payment_allocation_and_credits"),)
SETUP_FLAG_ROWS = (
    "SELECT code FROM fee_types WHERE show_in_setup ORDER BY code",
    "SELECT code FROM fee_types WHERE NOT show_in_setup ORDER BY code",
)
EXISTING_ROWS = "SELECT count(*) FROM invoice_draft_lines WHERE existing_charge_id IS NULL"


def seed_before_setup_flag() -> None:
    """Seed two fee types (one inactive), make an NSF fee active, and seed a draft with
    one line and an open one-off charge with the models of ``STATE_BEFORE_SETUP_FLAG``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_SETUP_FLAG)).apps
    fee_type = apps.get_model("ledger", "FeeType")
    fee_type.objects.create(code="window-film", name="Window film", active=True)
    fee_type.objects.create(code="old-fee", name="Old fee", active=False)
    fee_type.objects.filter(code="nsf_fee_first").update(active=True)
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Eastwind")
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1405", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        collection_status="active",
        payment_channel="wire",
    )
    apps.get_model("ledger", "OneOffCharge").objects.create(
        operator=operator,
        lease=lease,
        category="damage",
        collection="manual",
        description="Broken shelf",
        price=Decimal("150.00"),
        due_date=date(2026, 10, 1),
        status="owed",
    )
    draft = apps.get_model("ledger", "InvoiceDraft").objects.create(operator=operator, lease=lease)
    apps.get_model("ledger", "InvoiceDraftLine").objects.create(
        draft=draft,
        position=1,
        item="Broken shelf",
        quantity=Decimal("1"),
        unit_amount=Decimal("150.00"),
        line_total=Decimal("150.00"),
    )


def _setup_flag_round_trip(repo_root: Path, url: str) -> None:
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_SETUP_FLAG)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_setup_flag as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    for _ in range(2):
        result = _manage(repo_root, url, "migrate", "--noinput")
        assert result.returncode == 0, (result.stdout, result.stderr)
        shown = {row[0] for row in _rows(url, SETUP_FLAG_ROWS[0])}
        hidden = {row[0] for row in _rows(url, SETUP_FLAG_ROWS[1])}
        assert "window-film" in shown and "security_deposit" in shown
        assert {"old-fee", "nsf_fee_first", "nsf_fee_later"} <= hidden
        assert not shown & hidden
        assert _rows(url, EXISTING_ROWS) == [("1",)]
        _migrate_to(repo_root, url, *STATE_BEFORE_SETUP_FLAG)
        assert _rows(url, "SELECT count(*) FROM invoice_draft_lines") == [("1",)]
        assert _rows(url, "SELECT count(*) FROM fee_types WHERE code = 'window-film'") == [("1",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_the_setup_flag_and_existing_charge_migrations_over_existing_rows_on_sqlite(
    repo_root: Path, tmp_path: Path
) -> None:
    url = f"sqlite:///{tmp_path / 'setup-flag.sqlite3'}"
    _setup_flag_round_trip(repo_root, url)
    import sqlite3

    with closing(sqlite3.connect(url.removeprefix("sqlite:///"))) as scratch:
        columns = {row[1] for row in scratch.execute("PRAGMA table_info(invoice_draft_lines)")}
    assert "existing_charge_id" in columns


@requires_postgresql
def test_the_setup_flag_and_existing_charge_migrations_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    _setup_flag_round_trip(repo_root, url)
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        # An existing-charge line is never a catalogue item's or a Rent line.
        scratch.execute(
            "UPDATE invoice_draft_lines SET existing_charge_id = "
            "(SELECT id FROM one_off_charges WHERE description = 'Broken shelf'), "
            "fee_type_id = (SELECT id FROM fee_types WHERE code = 'window-film')"
        )
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(
            "UPDATE invoice_draft_lines SET existing_charge_id = "
            "(SELECT id FROM one_off_charges WHERE description = 'Broken shelf'), "
            "rent_lease_id = (SELECT id FROM leases), rent_month = '2026-10-01'"
        )
    with psycopg.connect(url) as scratch:
        scratch.execute(
            "UPDATE invoice_draft_lines SET existing_charge_id = "
            "(SELECT id FROM one_off_charges WHERE description = 'Broken shelf')"
        )
    assert _rows(url, EXISTING_ROWS) == [("0",)]
    # The reverse drops the columns with the rows kept, and forward again.
    _migrate_to(repo_root, url, *STATE_BEFORE_SETUP_FLAG)
    assert _rows(
        url,
        "SELECT count(*) FROM information_schema.columns WHERE "
        "(table_name = 'invoice_draft_lines' AND column_name = 'existing_charge_id') "
        "OR (table_name = 'fee_types' AND column_name = 'show_in_setup')",
    ) == [("0",)]
    assert _rows(url, "SELECT count(*) FROM invoice_draft_lines") == [("1",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, EXISTING_ROWS) == [("1",)]


# --- PLAN-012c D2: ledger 0045, company settings --------------------------------------------
# Seeds an operator at ``ledger.0044``, migrates forward, back and forward again on both
# engines and reads the rows with plain SQL: 0045 seeds the one company settings row and
# gives the admin group view and change on it; the reverse drops the table, removes the two
# permissions and keeps every other row; forward again seeds the row again.

STATE_BEFORE_COMPANY = (("ledger", "0044_draft_existing_charge"),)
COMPANY_ROW = (
    "SELECT id, name, short_name, support_email, time_zone, logo, processor_name "
    "FROM company_settings"
)
SEEDED_COMPANY = [("1", "Monvera", "Monvera", "", "America/New_York", "", "Stax")]
COMPANY_PERMISSIONS = (
    "SELECT p.codename FROM auth_group_permissions gp "
    "JOIN auth_group g ON g.id = gp.group_id JOIN auth_permission p ON p.id = gp.permission_id "
    "WHERE g.name = 'admin' AND p.codename LIKE '%companysettings' ORDER BY p.codename"
)


def seed_before_company() -> None:
    """Seed an operator with the models of ``STATE_BEFORE_COMPANY``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_COMPANY)).apps
    apps.get_model("ledger", "Operator").objects.create(display_name="Eastwind")


def _company_round_trip(repo_root: Path, url: str) -> None:
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_COMPANY)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_company as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    for _ in range(2):
        result = _manage(repo_root, url, "migrate", "--noinput")
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert _rows(url, COMPANY_ROW) == SEEDED_COMPANY
        assert _rows(url, COMPANY_PERMISSIONS) == [
            ("change_companysettings",),
            ("view_companysettings",),
        ]
        _migrate_to(repo_root, url, *STATE_BEFORE_COMPANY)
        assert _rows(url, COMPANY_PERMISSIONS) == []
        assert _rows(url, "SELECT display_name FROM operators") == [("Eastwind",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _rows(url, COMPANY_ROW) == SEEDED_COMPANY


def test_the_company_settings_migration_over_existing_rows_on_sqlite(
    repo_root: Path, tmp_path: Path
) -> None:
    import sqlite3

    url = f"sqlite:///{tmp_path / 'company.sqlite3'}"
    _company_round_trip(repo_root, url)
    with closing(sqlite3.connect(url.removeprefix("sqlite:///"))) as scratch:
        # The CHECKs on the names call REGEXP, which Django registers on its connections.
        scratch.create_function(
            "REGEXP", 2, lambda pattern, text: re.search(pattern, text or "") is not None
        )
        with pytest.raises(sqlite3.IntegrityError, match="company_settings_singleton"):
            scratch.execute(
                "INSERT INTO company_settings (id, name, short_name, support_email, time_zone, "
                "logo, processor_name, updated_at) VALUES "
                "(2, 'Other', 'Other', '', 'America/New_York', '', 'Other', '2026-10-16')"
            )


@requires_postgresql
def test_the_company_settings_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    _company_round_trip(repo_root, url)
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(
            "INSERT INTO company_settings (id, name, short_name, support_email, time_zone, "
            "logo, processor_name, updated_at) VALUES "
            "(2, 'Other', 'Other', '', 'America/New_York', '', 'Other', now())"
        )
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute("UPDATE company_settings SET logo = 'https://example.com/logo.svg'")
    # The free-text columns are guarded by the trigger.
    for column in ("name", "short_name", "support_email", "processor_name"):
        with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
            scratch.execute(
                f"UPDATE company_settings SET {column} = '4111 1111 1111 1111'"  # noqa: S608
            )
    assert _rows(url, COMPANY_ROW) == SEEDED_COMPANY


# --- PLAN-012e-2 D7: ledger 0047, the store's street and ZIP --------------------------------
# Seeds a store at ``ledger.0046``, migrates forward, back and forward again on both engines
# and reads the rows with plain SQL: 0047 adds the two columns blank on the existing row; the
# reverse drops them and keeps the row. On PostgreSQL the stores trigger guards both columns
# after the forward step, and the old columns are still guarded after the reverse.

STATE_BEFORE_ADDRESS = (("ledger", "0046_copy_labels"),)
ADDRESS_COLUMNS = (
    "SELECT count(*) FROM information_schema.columns WHERE table_name = 'stores' "
    "AND column_name IN ('address_line1', 'postal_code')"
)


def seed_before_address() -> None:
    """Seed a store with the models of ``STATE_BEFORE_ADDRESS``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_ADDRESS)).apps
    apps.get_model("ledger", "Store").objects.create(
        external_label="Store 2610", city="Springfield", state="IL", status="active"
    )


def _address_columns(url: str) -> list[tuple[str, ...]]:
    if url.startswith("sqlite:///"):
        return _rows(
            url,
            "SELECT count(*) FROM pragma_table_info('stores') "
            "WHERE name IN ('address_line1', 'postal_code')",
        )
    return _rows(url, ADDRESS_COLUMNS)


def _address_round_trip(repo_root: Path, url: str) -> None:
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_ADDRESS)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_address as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    for _ in range(2):
        result = _manage(repo_root, url, "migrate", "--noinput")
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert _address_columns(url) == [("2",)]
        assert _rows(
            url, "SELECT external_label, address_line1, city, state, postal_code FROM stores"
        ) == [("Store 2610", "", "Springfield", "IL", "")]
        _migrate_to(repo_root, url, *STATE_BEFORE_ADDRESS)
        assert _address_columns(url) == [("0",)]
        assert _rows(url, "SELECT external_label, city FROM stores") == [
            ("Store 2610", "Springfield")
        ]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_the_store_address_migration_over_existing_rows_on_sqlite(
    repo_root: Path, tmp_path: Path
) -> None:
    _address_round_trip(repo_root, f"sqlite:///{tmp_path / 'address.sqlite3'}")


@requires_postgresql
def test_the_store_address_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    _address_round_trip(repo_root, url)
    for column in ("address_line1", "postal_code", "city"):
        with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
            scratch.execute(f"UPDATE stores SET {column} = '4111 1111 1111 1111'")  # noqa: S608
    # Back at 0046, the trigger guards the columns of 0002 again.
    _migrate_to(repo_root, url, *STATE_BEFORE_ADDRESS)
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute("UPDATE stores SET city = '4111 1111 1111 1111'")
    with psycopg.connect(url) as scratch:
        scratch.execute("UPDATE stores SET city = 'Shelbyville'")
    assert _rows(url, "SELECT city FROM stores") == [("Shelbyville",)]


# --- PLAN-012e-3 D1: ledger 0048, Check as a manual payment method ------------------------
# Seeds a Zelle payment at ``ledger.0047``, migrates forward, back and forward again on both
# engines with plain SQL: after 0048 a payment and a lease credit may store ``check``; the
# reverse refuses while a row stores it and, once none does, restores the CHECKs without it.
# The ``method`` columns themselves change only their choices: no SQL.

STATE_BEFORE_CHECK = (("ledger", "0047_store_address"),)
CHECK_MIGRATION = ("ledger", "0048_check_method")
#: The CHECK constraints 0048 recreates with ``check`` among the methods.
CHECK_CONSTRAINTS = {
    "payments_method_valid",
    "payments_manual_method_has_note",
    "payments_refund_deposit_manual",
    "payments_credit_refund_shape",
    "lease_credits_method_valid",
    "deposit_resolutions_method_valid",
}
TO_CHECK = "UPDATE payments SET method = 'check'"


def seed_before_check() -> None:
    """Seed a lease, a rent charge and a Zelle payment on it with the models of
    ``STATE_BEFORE_CHECK``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_CHECK)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Eastwind")
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 1048", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        collection_status="active",
        payment_channel="wire",
    )
    charge = apps.get_model("ledger", "RentCharge").objects.create(
        lease=lease,
        period_month=date(2026, 10, 1),
        due_on=date(2026, 10, 1),
        amount_due=Decimal("1000.00"),
        status="due",
    )
    apps.get_model("ledger", "Payment").objects.create(
        rent_charge=charge,
        kind="rent",
        method="zelle",
        amount=Decimal("400.00"),
        status="settled",
        settled_on=date(2026, 10, 2),
        note="Check 1043 received.",
    )


def _accepted(url: str, statement: str) -> bool:
    """Run one write; ``False`` when a CHECK constraint refuses it (nothing written)."""
    if url.startswith("sqlite:///"):
        import sqlite3

        with closing(sqlite3.connect(url.removeprefix("sqlite:///"))) as scratch:
            # The CHECKs that read a note use REGEXP, which Django registers on its own
            # connections only.
            scratch.create_function(
                "REGEXP", 2, lambda pattern, value: bool(re.search(pattern, value or ""))
            )
            try:
                scratch.execute(statement)
            except sqlite3.IntegrityError as error:
                assert "CHECK constraint failed" in str(error), error
                return False
            scratch.commit()
        return True
    import psycopg

    try:
        with psycopg.connect(url) as scratch:
            scratch.execute(statement)
    except psycopg.errors.CheckViolation:
        return False
    return True


def _check_round_trip(repo_root: Path, url: str) -> None:
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_CHECK)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_check as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    # Before 0048 a check is not a method.
    assert not _accepted(url, TO_CHECK)
    for _ in range(2):
        result = _manage(repo_root, url, "migrate", "--noinput")
        assert result.returncode == 0, (result.stdout, result.stderr)
        # The existing row is untouched: a Zelle payment stays Zelle.
        assert _rows(url, "SELECT method, note FROM payments") == [
            ("zelle", "Check 1043 received.")
        ]
        assert _accepted(url, TO_CHECK)
        # Still refused: a method that is not one, and a check without a note.
        assert not _accepted(url, "UPDATE payments SET method = 'cheque'")
        assert not _accepted(url, "UPDATE payments SET note = ' '")
        # The reverse refuses while a row stores ``check`` and leaves the row as it was.
        back = _manage(repo_root, url, "migrate", *STATE_BEFORE_CHECK[0], "--noinput")
        assert back.returncode != 0
        assert _rows(url, "SELECT method FROM payments") == [("check",)]
        assert _accepted(url, "UPDATE payments SET method = 'zelle'")
        _migrate_to(repo_root, url, *STATE_BEFORE_CHECK)
        assert not _accepted(url, TO_CHECK)
        assert _rows(url, "SELECT method FROM payments") == [("zelle",)]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_the_check_method_migration_over_existing_rows_on_sqlite(
    repo_root: Path, tmp_path: Path
) -> None:
    _check_round_trip(repo_root, f"sqlite:///{tmp_path / 'check.sqlite3'}")


@requires_postgresql
def test_the_check_method_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    _check_round_trip(repo_root, url)
    # A lease credit may name ``check`` too, never ``credit``.
    credit = (
        "INSERT INTO lease_credits (id, lease_id, amount, remaining, method, received_on, "
        "reference, note, allocation_group, created_at, updated_at) VALUES "
        "(%s, (SELECT id FROM leases), 50.00, 50.00, %s, '2026-10-02', '1043', 'Kept', %s, "
        "now(), now())"
    )
    with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(credit, (uuid.uuid4(), "credit", uuid.uuid4()))
    with psycopg.connect(url) as scratch:
        scratch.execute(credit, (uuid.uuid4(), "check", uuid.uuid4()))
    assert _rows(url, "SELECT method, reference FROM lease_credits") == [("check", "1043")]


@requires_postgresql
def test_sqlmigrate_of_the_check_method_migration_touches_only_its_checks(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    result = _manage(repo_root, scratch_postgresql_url, "sqlmigrate", *CHECK_MIGRATION)
    assert result.returncode == 0, result.stderr
    statements = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("--") and line not in ("BEGIN;", "COMMIT;")
    ]
    # The three AlterField steps print a comment and no SQL: only the CHECKs change.
    assert result.stdout.count("-- Alter field method on") == 3
    assert len(statements) == 2 * len(CHECK_CONSTRAINTS)
    for statement in statements:
        assert re.match(r'ALTER TABLE "\w+" (DROP|ADD) CONSTRAINT "(\w+)"', statement), statement
        name = re.search(r'CONSTRAINT "(\w+)"', statement)
        assert name is not None and name.group(1) in CHECK_CONSTRAINTS, statement
    assert "ALTER COLUMN" not in result.stdout and "TYPE" not in result.stdout


@pytest.mark.django_db(transaction=True)
def test_the_check_method_migration_alters_choices_only() -> None:
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.operations import AddConstraint, AlterField, RemoveConstraint

    loader = MigrationLoader(connection)
    migration = loader.get_migration(*CHECK_MIGRATION)
    state = loader.project_state(STATE_BEFORE_CHECK[0])
    altered: list[str] = []
    for operation in migration.operations:
        after = state.clone()
        operation.state_forwards("ledger", after)
        if isinstance(operation, AlterField):
            with connection.schema_editor(collect_sql=True, atomic=False) as editor:
                operation.database_forwards("ledger", editor, state, after)
            assert editor.collected_sql == [], (operation.model_name, editor.collected_sql)
            altered.append(f"{operation.model_name}.{operation.name}")
        state = after
    assert sorted(altered) == ["depositresolution.method", "leasecredit.method", "payment.method"]
    removed = {op.name for op in migration.operations if isinstance(op, RemoveConstraint)}
    added = {op.constraint.name for op in migration.operations if isinstance(op, AddConstraint)}
    assert removed == added == CHECK_CONSTRAINTS


# --- PLAN-012f-a D11: ledger 0049, autopay plans ------------------------------------------
# Seeds a lease and an arrears charge at ``ledger.0048``, migrates forward, back and forward
# again on both engines with plain SQL: 0049 leaves the charge as it was (no plan, no
# period); an ``autopay_plan`` charge needs its plan; a plan's end rule is checked; the
# reverse refuses while a plan exists and, once none does, drops the table. On PostgreSQL
# the plan's item and detail are guarded by the trigger.

STATE_BEFORE_AUTOPAY = (("ledger", "0048_check_method"),)
AUTOPAY_CHARGE_ROWS = (
    "SELECT category, price, status, autopay_plan_id, period_no FROM one_off_charges"
)
AUTOPAY_TABLE = "SELECT count(*) FROM information_schema.tables WHERE table_name = 'autopay_plans'"


def seed_before_autopay() -> None:
    """Seed a lease and an arrears charge with the models of ``STATE_BEFORE_AUTOPAY``.

    Runs inside ``manage.py shell`` against the scratch database, not under pytest.
    """
    from datetime import date
    from decimal import Decimal

    from django.db import connection as db
    from django.db.migrations.loader import MigrationLoader

    apps = MigrationLoader(db).project_state(list(STATE_BEFORE_AUTOPAY)).apps
    operator = apps.get_model("ledger", "Operator").objects.create(display_name="Plan Co")
    store = apps.get_model("ledger", "Store").objects.create(
        external_label="Store 4901", status="active"
    )
    lease = apps.get_model("ledger", "Lease").objects.create(
        store=store,
        operator=operator,
        debit_day=1,
        collection_status="active",
        payment_channel="wire",
    )
    apps.get_model("ledger", "OneOffCharge").objects.create(
        operator=operator,
        lease=lease,
        category="arrears",
        collection="manual",
        description="Arrears",
        price=Decimal("1000.00"),
        due_date=date(2026, 10, 1),
        status="owed",
    )


def _a_plan(item: str = "Sign fee", end_count: str = "3") -> str:
    """One plan row, with plain SQL that both engines read (fixed test values only)."""
    return (
        "INSERT INTO autopay_plans (id, lease_id, operator_id, store_id, item_name, "  # noqa: S608
        "description, quantity, unit_amount, amount, cadence, first_on, end_rule, end_count, "
        "end_on, state, next_on, made_count, created_at, updated_at) VALUES "
        f"('{uuid.uuid4().hex}', (SELECT id FROM leases), (SELECT id FROM operators), "
        f"(SELECT id FROM stores), '{item}', '', 1, 100.00, 100.00, 'monthly', '2026-11-01', "
        f"'count', {end_count}, NULL, 'active', '2026-11-01', 0, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP)"
    )


def _autopay_table(url: str) -> list[tuple[str, ...]]:
    if url.startswith("sqlite:///"):
        return _rows(
            url,
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'autopay_plans'",
        )
    return _rows(url, AUTOPAY_TABLE)


def _autopay_round_trip(repo_root: Path, url: str) -> None:
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)
    _migrate_to(repo_root, url, *STATE_BEFORE_AUTOPAY)
    seeded = _manage(
        repo_root,
        url,
        "shell",
        "-c",
        "from tests.test_migrations_roundtrip import seed_before_autopay as seed; seed()",
    )
    assert seeded.returncode == 0, seeded.stderr
    assert _autopay_table(url) == [("0",)]
    after = [("arrears", "1000.00", "owed", "None", "None")]
    for _ in range(2):
        result = _manage(repo_root, url, "migrate", "--noinput")
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert _rows(url, AUTOPAY_CHARGE_ROWS, (1,)) == after
        # An occurrence needs its plan; a plan's count is 2 or more.
        assert not _accepted(url, "UPDATE one_off_charges SET category = 'autopay_plan'")
        assert not _accepted(url, _a_plan(end_count="1"))
        assert not _accepted(url, _a_plan(item=" "))
        assert _accepted(url, _a_plan())
        # A plan cannot be lost by going back.
        back = _manage(repo_root, url, "migrate", *STATE_BEFORE_AUTOPAY[0], "--noinput")
        assert back.returncode != 0 and "refuses to reverse" in back.stderr
        assert _rows(url, "SELECT count(*) FROM autopay_plans") == [("1",)]
        assert _accepted(url, "DELETE FROM autopay_plans")
        _migrate_to(repo_root, url, *STATE_BEFORE_AUTOPAY)
        assert _autopay_table(url) == [("0",)]
        assert _rows(url, "SELECT category, price, status FROM one_off_charges", (1,)) == [
            ("arrears", "1000.00", "owed")
        ]
    result = _manage(repo_root, url, "migrate", "--noinput")
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_the_autopay_plans_migration_over_existing_rows_on_sqlite(
    repo_root: Path, tmp_path: Path
) -> None:
    _autopay_round_trip(repo_root, f"sqlite:///{tmp_path / 'autopay.sqlite3'}")


@requires_postgresql
def test_the_autopay_plans_migration_over_existing_rows_on_postgresql(
    repo_root: Path, scratch_postgresql_url: str
) -> None:
    import psycopg

    url = scratch_postgresql_url
    _autopay_round_trip(repo_root, url)
    for column in ("item_name", "description"):
        with psycopg.connect(url) as scratch:
            scratch.execute(_a_plan())
        with psycopg.connect(url) as scratch, pytest.raises(psycopg.errors.CheckViolation):
            scratch.execute(
                f"UPDATE autopay_plans SET {column} = 'Card 4111 1111 1111 1111'"  # noqa: S608
            )
        with psycopg.connect(url) as scratch:
            scratch.execute("DELETE FROM autopay_plans")


# --- Row 2 --------------------------------------------------------------------------


@pytest.mark.django_db
def test_models_and_migrations_agree() -> None:
    # makemigrations --check exits non-zero (SystemExit) when a migration is missing.
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def test_payments_initial_migration_depends_on_ledger() -> None:
    # It imports ledger.models.MoneyField and ledger.validators.
    initial = importlib.import_module("payments.migrations.0001_initial")
    assert ("ledger", "0001_initial") in initial.Migration.dependencies
