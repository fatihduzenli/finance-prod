"""Brands (docs/qa/PLAN-014 Batch B, B1).

Schema: the table ``store_types`` becomes ``brands`` (model ``StoreType`` becomes ``Brand``,
its rows kept) and ``stores.store_type_id`` becomes ``stores.brand_id`` (null; the foreign
key still refuses to delete a brand in use). The not-blank CHECK on the name is renamed
with its table (``brands_name_not_blank``) and, on PostgreSQL, so is the payment-data
trigger of ``0051`` (``brands_payment_data_guard``, the same column). On PostgreSQL ``SET
CONSTRAINTS ALL IMMEDIATE`` runs any check this transaction queued before the ``ALTER TABLE``
statements (the lesson of ``0018``), in either direction.

The reverse renames everything back.
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

OLD_TABLE = "store_types"
NEW_TABLE = "brands"
#: Table -> guarded columns after this migration: the brands' name keeps the trigger 0051
#: created on ``store_types``, renamed with its table.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {NEW_TABLE: ("name",)}


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def _rename_trigger(
    schema_editor: BaseDatabaseSchemaEditor, table: str, old: str, new: str
) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        f"ALTER TRIGGER {old}_payment_data_guard ON {table} RENAME TO {new}_payment_data_guard;",
        params=None,
    )


def rename_trigger(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _rename_trigger(schema_editor, NEW_TABLE, OLD_TABLE, NEW_TABLE)


def unrename_trigger(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    # Runs before the table is renamed back: the trigger is still on ``brands``.
    _rename_trigger(schema_editor, NEW_TABLE, NEW_TABLE, OLD_TABLE)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0057_payment_void_fields"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(model_name="storetype", name="store_types_name_not_blank"),
        migrations.RenameModel(old_name="StoreType", new_name="Brand"),
        migrations.AlterModelTable(name="brand", table=NEW_TABLE),
        migrations.AlterModelOptions(
            name="brand",
            options={
                "ordering": ("sort_order", "name"),
                "verbose_name": "Brand",
                "verbose_name_plural": "Brands",
            },
        ),
        migrations.AddConstraint(
            model_name="brand",
            constraint=models.CheckConstraint(
                condition=models.Q(("name__regex", "\\S")), name="brands_name_not_blank"
            ),
        ),
        migrations.RenameField(model_name="store", old_name="store_type", new_name="brand"),
        migrations.AlterField(
            model_name="store",
            name="brand",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stores",
                to="ledger.brand",
                verbose_name="Brand",
            ),
        ),
        migrations.RunPython(rename_trigger, unrename_trigger, elidable=False),
        # Reversed first: any check queued runs before the ALTER TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
    ]
