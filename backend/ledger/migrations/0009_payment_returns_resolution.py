"""``payments.resolved_at``, ``resolved_by`` and ``resolution_note``: the returns queue
(docs/qa/PLAN-008-webhooks.md D6).

Staff resolve a returned payment with a mandatory note; a CHECK keeps the three in
step (a resolved row is a returned payment with a note that is not blank).
``resolution_note`` is free text, so the card-number guard covers it: on PostgreSQL the
payments trigger of ``0002_postgresql_guards`` is recreated with the new column among
its arguments. The reverse recreates the trigger with the original columns before the
column is dropped. Nothing is created on other vendors; the application validator
applies there. Existing rows are unresolved.
"""

from __future__ import annotations

import importlib

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

_PREVIOUS: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0002_postgresql_guards"
).GUARDED_COLUMNS["payments"]

#: Table -> guarded columns after this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "payments": (*_PREVIOUS, "resolution_note"),
}


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS payments_payment_data_guard ON payments;", params=None
    )
    schema_editor.execute(
        "CREATE TRIGGER payments_payment_data_guard "
        "BEFORE INSERT OR UPDATE ON payments "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS["payments"])


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _PREVIOUS)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0008_admin_group_view_permissions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="resolution_note",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="resolved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(("resolved_at__isnull", True))
                | models.Q(("status", "returned"), ("resolution_note__regex", "\\S")),
                name="payments_resolved_return_has_note",
            ),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
    ]
