"""Proration (docs/qa/PLAN-012e-lease-end-transfer-installments.md D1 to D3).

Schema: ``leases.prorate_first_month`` (default true) and ``rent_charges.proration_text``
(null). Adding the columns with their defaults is the whole change for existing rows:
every lease gets the switch on, every charge has no proration text, and no amount is
rewritten (D2: no backfill; leases already billed keep their charges).

Free text is guarded: on PostgreSQL the ``rent_charges`` trigger of ``0010`` is recreated
with ``proration_text`` among its arguments; the reverse restores the columns of
``0010``. Nothing is created on other vendors; the application validator applies there.

The reverse drops both columns (the proration texts are lost; the amounts stay).
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

TABLE = "rent_charges"
_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0010_rent_charge_review_message"
).GUARDED_COLUMNS[TABLE]

#: Table -> guarded columns after this migration. Replaces the ``rent_charges`` entry of
#: ``0010``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: (*_BEFORE, "proration_text"),
}


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {TABLE}_payment_data_guard ON {TABLE};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {TABLE}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS[TABLE])


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _BEFORE)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0033_one_open_promise_per_lease"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements, in
        # either direction (the lesson of 0018).
        migrations.RunPython(run_pending_checks, run_pending_checks, elidable=False),
        migrations.AddField(
            model_name="lease",
            name="prorate_first_month",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="rentcharge",
            name="proration_text",
            field=models.TextField(
                blank=True,
                default=None,
                null=True,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
    ]
