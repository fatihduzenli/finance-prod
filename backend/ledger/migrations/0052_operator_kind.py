"""The operator's kind and name fields (docs/qa/PLAN-012f-b Batch B, D9).

Schema, on ``operators``: ``kind`` (``company`` or ``person``, default ``person``, held to
those two by the CHECK ``operators_kind_valid``), and ``contact_person`` (at most 200
characters), ``first_name`` and ``last_name`` (at most 99 each), guarded short text,
blank, default "". Adding a column with its default fills every existing row with it,
which is the whole backfill: every existing operator becomes a person with empty name
fields, nothing is guessed, and its ``display_name`` is unchanged (``Operator.save``
keeps it while the kind's name fields are empty); staff change the kind on the form.

Free text is guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in
either direction, and the operators trigger of ``0002_postgresql_guards`` is recreated
with the three new text columns among its arguments; the reverse restores the columns of
``0031``. Nothing is created on other vendors; the application validator applies there.

The reverse drops the four columns (the kind and the names typed since are lost; the
display name derived from them stays in ``display_name`` and in the audit rows).
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

TABLE = "operators"
_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0031_line_kind_fees_consent"
).GUARDED_COLUMNS[TABLE]

#: Table -> guarded columns after this migration. Replaces the ``operators`` entry of
#: ``0031``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: (*_BEFORE, "contact_person", "first_name", "last_name"),
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


def guard_new_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS[TABLE])


def unguard_new_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _BEFORE)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0051_store_type_and_state_codes"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.AddField(
            model_name="operator",
            name="kind",
            field=models.CharField(
                choices=[("company", "Company"), ("person", "Person")],
                default="person",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="operator",
            name="contact_person",
            field=models.CharField(
                blank=True,
                default="",
                max_length=200,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="operator",
            name="first_name",
            field=models.CharField(
                blank=True,
                default="",
                max_length=99,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="operator",
            name="last_name",
            field=models.CharField(
                blank=True,
                default="",
                max_length=99,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddConstraint(
            model_name="operator",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["company", "person"])),
                name="operators_kind_valid",
            ),
        ),
        migrations.RunPython(guard_new_columns, unguard_new_columns, elidable=False),
        # Reversed first: any check queued runs before the ALTER TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
    ]
