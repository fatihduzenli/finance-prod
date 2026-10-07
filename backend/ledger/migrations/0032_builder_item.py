"""The invoice builder's Item column (docs/qa/PLAN-012b-builder-item-combo.md D1, D3, D5).

Schema: ``invoice_draft_lines.item`` (the line's text, at most 200 characters, not
blank) and ``invoice_line_items.details`` (the line's detail, blank allowed);
``invoice_draft_lines.description`` becomes an optional detail (blank allowed).

Data: every existing draft line's ``item`` is its fee type's name when it has one, else
its old description, whose text moved to ``item``: that description becomes ``''``. A
line with a fee type keeps its description. Every existing invoice line's ``details``
is ``''`` (the column's default fills them).

Constraints: ``invoice_draft_lines_description_not_blank`` is dropped before the
backfill (which blanks descriptions) and ``invoice_draft_lines_item_not_blank`` is added
after it. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``).

Free text is guarded: on PostgreSQL the payment-data triggers of ``invoice_draft_lines``
(``0029``) and ``invoice_line_items`` (``0002``) are recreated with the new columns
among their arguments; the reverse restores the earlier columns. Nothing is created on
other vendors; the application validator applies there.

The reverse restores a blank description from ``item`` (cut to the description's 120
characters), then drops the new columns: the details of sent invoices' lines are lost.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

import ledger.validators

_DRAFT_LINES_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0029_invoice_builder"
).GUARDED_COLUMNS["invoice_draft_lines"]
_LINE_ITEMS_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0002_postgresql_guards"
).GUARDED_COLUMNS["invoice_line_items"]

#: Table -> guarded columns after this migration. Replaces the ``invoice_draft_lines``
#: entry of ``0029`` and the ``invoice_line_items`` entry of ``0002``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "invoice_draft_lines": (*_DRAFT_LINES_BEFORE, "item"),
    "invoice_line_items": (*_LINE_ITEMS_BEFORE, "details"),
}
_BEFORE: dict[str, tuple[str, ...]] = {
    "invoice_draft_lines": _DRAFT_LINES_BEFORE,
    "invoice_line_items": _LINE_ITEMS_BEFORE,
}
#: ``invoice_draft_lines.description`` before and after this migration.
DESCRIPTION_MAX_LENGTH = 120


def _replace_triggers(
    schema_editor: BaseDatabaseSchemaEditor, guarded: dict[str, tuple[str, ...]]
) -> None:
    for table, columns in guarded.items():
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


def guard_new_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_triggers(schema_editor, GUARDED_COLUMNS)


def unguard_new_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_triggers(schema_editor, _BEFORE)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def move_text_to_item(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    lines = apps.get_model("ledger", "InvoiceDraftLine")
    for line in lines.objects.select_related("fee_type").order_by("pk"):
        if line.fee_type is not None:
            line.item = line.fee_type.name
        else:
            line.item = line.description
            line.description = ""
        line.save(update_fields=["item", "description"])
    run_pending_checks(apps, schema_editor)


def restore_description(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    lines = apps.get_model("ledger", "InvoiceDraftLine")
    for line in lines.objects.order_by("pk"):
        if line.description.strip():
            continue
        line.description = line.item[:DESCRIPTION_MAX_LENGTH].rstrip()
        line.save(update_fields=["description"])
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0031_line_kind_fees_consent"),
    ]

    operations = [
        # --- Schema: the columns; ``item`` is filled by the backfill below.
        migrations.AddField(
            model_name="invoicedraftline",
            name="item",
            field=models.CharField(
                default="",
                max_length=200,
                validators=[ledger.validators.validate_no_payment_data],
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="invoicelineitem",
            name="details",
            field=models.TextField(
                blank=True,
                default="",
                validators=[ledger.validators.validate_no_payment_data],
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="invoicedraftline",
            name="description",
            field=models.CharField(
                blank=True,
                max_length=120,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        # --- The old CHECK goes before the backfill blanks descriptions (and, on the
        # reverse, comes back after the descriptions are restored).
        migrations.RunPython(run_pending_checks, run_pending_checks, elidable=False),
        migrations.RemoveConstraint(
            model_name="invoicedraftline",
            name="invoice_draft_lines_description_not_blank",
        ),
        migrations.RunPython(move_text_to_item, restore_description, elidable=False),
        migrations.RunPython(guard_new_columns, unguard_new_columns, elidable=False),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=Q(item__regex="\\S"), name="invoice_draft_lines_item_not_blank"
            ),
        ),
    ]
