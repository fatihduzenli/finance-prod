"""The void of a payment recorded outside the processor (docs/qa/PLAN-012f-c D2).

Schema, on ``payments``: ``void_reason`` (at most 200 characters, guarded short text, blank,
default ""), ``voided_at`` (timestamp, null) and ``voided_by`` (FK ``auth_user``, PROTECT,
null). A manual payment's void by billing sets the three; the processor void sets the last
two. Adding the columns fills every existing row with "" and nulls, which is the whole
backfill: a payment voided before keeps no stamp and no reason, so there is no CHECK tying
the three to the status.

Free text is guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in
either direction, and the payments trigger of ``0002_postgresql_guards`` is recreated with
``void_reason`` among its arguments; the reverse restores the columns of ``0009``. Nothing
is created on other vendors; the application validator applies there.

The reverse drops the three columns (the reasons and stamps of voids made since are lost;
the audit rows ``payment.voided_manual`` and ``payment.voided`` keep them).
"""

from __future__ import annotations

import importlib

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

TABLE = "payments"
_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0009_payment_returns_resolution"
).GUARDED_COLUMNS[TABLE]

#: Table -> guarded columns after this migration. Replaces the ``payments`` entry of
#: ``0009``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: (*_BEFORE, "void_reason"),
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
        ("ledger", "0056_rent_charge_bank_debit"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.AddField(
            model_name="payment",
            name="void_reason",
            field=models.CharField(
                blank=True,
                default="",
                max_length=200,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="voided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="voided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
        # Reversed first: any check queued runs before the ALTER TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
    ]
