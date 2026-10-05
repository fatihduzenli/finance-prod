"""Store-cost corrections (docs/qa/PLAN-010c-documents-and-cost-corrections.md D10).

``store_costs`` gains ``superseded_at``, ``superseded_by`` (the row that replaced this
one) and ``change_note``. The one-row-per-start-date and one-open-row rules now hold
among live rows (``superseded_at`` null) only; a superseded row names its replacement
and says why.

On PostgreSQL the table's payment-data trigger (from
``0014_store_costs_deposit_resolutions``) is replaced by one that also covers
``change_note``; the reverse restores the old one. Nothing is created on other vendors;
the application validator applies there.
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

TABLE = "store_costs"
OLD_GUARDED_COLUMNS: tuple[str, ...] = ("other_cost_description", "notes")
#: Table -> guarded columns. Replaces the ``store_costs`` entry of ``0014``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: ("other_cost_description", "notes", "change_note"),
}


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
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
    _replace_trigger(schema_editor, GUARDED_COLUMNS[TABLE])


def guard_old_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _replace_trigger(schema_editor, OLD_GUARDED_COLUMNS)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0015_documents_reshape"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="storecost",
            name="store_costs_store_from_unique",
        ),
        migrations.RemoveConstraint(
            model_name="storecost",
            name="store_costs_one_open_row",
        ),
        migrations.AddField(
            model_name="storecost",
            name="change_note",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddField(
            model_name="storecost",
            name="superseded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="storecost",
            name="superseded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.storecost",
            ),
        ),
        migrations.AddConstraint(
            model_name="storecost",
            constraint=models.UniqueConstraint(
                condition=models.Q(("superseded_at__isnull", True)),
                fields=("store", "effective_from"),
                name="store_costs_store_from_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="storecost",
            constraint=models.UniqueConstraint(
                condition=models.Q(("effective_to__isnull", True), ("superseded_at__isnull", True)),
                fields=("store",),
                name="store_costs_one_open_row",
            ),
        ),
        migrations.AddConstraint(
            model_name="storecost",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("superseded_at__isnull", True), ("superseded_by__isnull", True)),
                    models.Q(
                        ("change_note__regex", "\\S"),
                        ("superseded_at__isnull", False),
                        ("superseded_by__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="store_costs_superseded_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="storecost",
            constraint=models.CheckConstraint(
                condition=models.Q(("superseded_by", models.F("id")), _negated=True),
                name="store_costs_not_superseded_by_itself",
            ),
        ),
        migrations.RunPython(guard_new_columns, guard_old_columns, elidable=False),
    ]
