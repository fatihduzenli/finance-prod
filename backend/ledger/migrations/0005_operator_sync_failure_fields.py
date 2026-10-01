"""``operators.sync_failure_fields`` (docs/qa/PLAN-006a-sync-diagnostics.md §3.5).

The key names of the last ``ProviderRejected`` body, comma-separated; never a value.
It is free text, so the card-number guard covers it: on PostgreSQL the operators
trigger of ``0002_postgresql_guards`` is recreated with the new column among its
arguments. The reverse recreates the trigger with the original columns before the
column is dropped. Nothing is created on other vendors; the application validator
applies there.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

_PREVIOUS: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0002_postgresql_guards"
).GUARDED_COLUMNS["operators"]

#: Table -> guarded columns after this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "operators": (*_PREVIOUS, "sync_failure_fields"),
}


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS operators_payment_data_guard ON operators;", params=None
    )
    schema_editor.execute(
        "CREATE TRIGGER operators_payment_data_guard "
        "BEFORE INSERT OR UPDATE ON operators "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS["operators"])


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _PREVIOUS)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0004_reserved_tables"),
    ]

    operations = [
        migrations.AddField(
            model_name="operator",
            name="sync_failure_fields",
            field=models.TextField(
                blank=True,
                default="",
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
    ]
