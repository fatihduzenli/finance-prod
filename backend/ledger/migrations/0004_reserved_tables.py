"""Reserved tables ``documents`` and ``outbound_messages`` (docs/qa/PLAN-006-operators.md
D8, §4.4). No UI and no admin yet.

On PostgreSQL their text and JSON columns get the card-number trigger of
``0002_postgresql_guards`` (the functions it created are reused). The reverse drops
exactly the two triggers before the tables go. Nothing is created on other vendors;
the application validator applies there.
"""

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: Table -> guarded columns: every text column without choices and every JSON column.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "documents": ("kind", "file_reference"),
    "outbound_messages": ("template", "subject", "document_refs", "delivery_status"),
}


def _trigger_sql(table: str, columns: tuple[str, ...]) -> str:
    arguments = ", ".join(f"'{column}'" for column in columns)
    return (
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});"
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        schema_editor.execute(_trigger_sql(table, columns), params=None)


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_COLUMNS:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0003_operator_sync"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Document",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("kind", models.TextField(validators=[ledger.validators.validate_no_payment_data])),
                (
                    "source",
                    models.CharField(
                        choices=[("upload", "Upload"), ("docusign", "DocuSign")], max_length=32
                    ),
                ),
                (
                    "file_reference",
                    models.TextField(validators=[ledger.validators.validate_no_payment_data]),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "documents",
            },
        ),
        migrations.CreateModel(
            name="OutboundMessage",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "template",
                    models.TextField(validators=[ledger.validators.validate_no_payment_data]),
                ),
                (
                    "subject",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "document_refs",
                    models.JSONField(
                        blank=True,
                        default=list,
                        validators=[ledger.validators.validate_json_has_no_payment_data],
                    ),
                ),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                (
                    "delivery_status",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
            ],
            options={
                "db_table": "outbound_messages",
            },
        ),
        migrations.AddField(
            model_name="document",
            name="operator",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="uploaded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="operator",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="outbound_messages",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="sent_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(("source__in", ["upload", "docusign"])),
                name="documents_source_valid",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
