"""The bank's institution name beside the last four (ADR-0019, the owner, 2026-11-01).

Schema, on ``lease_billing``: ``bank_name`` (100 characters, default ``""``): the cached
institution name of the linked bank method, as the processor returns it, set with
``bank_last4`` from the method snapshot (``""`` when unknown; the cards then say "Bank").
Existing rows get ``""`` and take the name at the next method read.

The column is guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statement (the lesson of ``0018``), and the
``lease_billing`` trigger of ``0059_lease_billing`` is recreated with ``bank_name`` among its
arguments; the reverse restores ``0059``'s columns, then drops the column. The name is a cache
of the processor's method, so the reverse loses nothing that the next method read does not
bring back. Nothing is created on other vendors; the application validators apply there.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

BILLING = "lease_billing"
_BILLING_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0059_lease_billing"
).GUARDED_COLUMNS[BILLING]
#: Table -> guarded columns after this migration: ``lease_billing`` replaces ``0059``'s entry.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    BILLING: (*_BILLING_BEFORE, "bank_name"),
}


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def _replace_trigger(
    schema_editor: BaseDatabaseSchemaEditor, table: str, columns: tuple[str, ...]
) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        _replace_trigger(schema_editor, table, columns)


def restore_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, BILLING, _BILLING_BEFORE)
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0066_operator_state_codes"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statement.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.AddField(
            model_name="leasebilling",
            name="bank_name",
            field=models.CharField(
                blank=True,
                default="",
                max_length=100,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.RunPython(create_guards, restore_guards, elidable=False),
    ]
