"""``webhook_events``: event source, redacted payload, attempts, duplicate count
(docs/qa/PLAN-008-webhooks.md D3, D7, D8).

``payload`` is JSON that may hold provider text, so the card-number guard covers it: on
PostgreSQL the ``webhook_events`` trigger of ``0002_postgresql_guards`` is recreated
with the new column among its arguments. The reverse recreates the trigger with the
original columns before the column is dropped. Nothing is created on other vendors; the
application validator applies there. Existing rows get ``unknown``, no payload and zero
counts.
"""

from __future__ import annotations

import importlib

import django.core.serializers.json
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

_PREVIOUS: tuple[str, ...] = importlib.import_module(
    "payments.migrations.0002_postgresql_guards"
).GUARDED_COLUMNS["webhook_events"]

#: Table -> guarded columns after this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "webhook_events": (*_PREVIOUS, "payload"),
}


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS webhook_events_payment_data_guard ON webhook_events;",
        params=None,
    )
    schema_editor.execute(
        "CREATE TRIGGER webhook_events_payment_data_guard "
        "BEFORE INSERT OR UPDATE ON webhook_events "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS["webhook_events"])


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _PREVIOUS)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0002_postgresql_guards"),
    ]

    operations = [
        migrations.AddField(
            model_name="webhookevent",
            name="event_source",
            field=models.CharField(
                choices=[
                    ("header", "Header"),
                    ("body", "Body"),
                    ("shape", "Object shape"),
                    ("unknown", "Unknown"),
                ],
                default="unknown",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="webhookevent",
            name="payload",
            field=models.JSONField(
                blank=True,
                encoder=django.core.serializers.json.DjangoJSONEncoder,
                null=True,
                validators=[ledger.validators.validate_json_has_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="webhookevent",
            name="attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="webhookevent",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="webhookevent",
            name="duplicate_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="webhookevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("event_source__in", ["header", "body", "shape", "unknown"])),
                name="webhook_events_event_source_valid",
            ),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
    ]
