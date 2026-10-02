"""``jobs``: the database queue the worker drains (docs/qa/PLAN-010 D1).

On PostgreSQL ``params`` and ``result`` get the card-number trigger of
``ledger.0002_postgresql_guards`` (the function it created is reused). The reverse drops
the trigger before the table goes. Nothing is created on other vendors; the
application validator applies there.
"""

from __future__ import annotations

import uuid

import django.core.serializers.json
import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: Table -> guarded columns: every text column without choices and every JSON column.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "jobs": ("params", "result"),
}


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        arguments = ", ".join(f"'{column}'" for column in columns)
        schema_editor.execute(
            f"CREATE TRIGGER {table}_payment_data_guard "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
            params=None,
        )


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_COLUMNS:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
        )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0004_webhook_object_id_source"),
        ("ledger", "0002_postgresql_guards"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Job",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("operator_sync", "Operator sync"),
                            ("operator_retry", "Operator sync retry"),
                            ("operator_link", "Operator link"),
                            ("autopay_enable", "Enable autopay"),
                            ("autopay_pause", "Pause autopay"),
                            ("autopay_resume", "Resume autopay"),
                            ("payment_mark", "Mark the provider invoice paid"),
                            ("payment_retry_mark", "Retry marking the provider invoice"),
                            ("payment_refund", "Refund"),
                            ("payment_void", "Void"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "entity_type",
                    models.CharField(
                        choices=[
                            ("operator", "Operator"),
                            ("lease", "Lease"),
                            ("rent_charge", "Rent charge"),
                            ("payment", "Payment"),
                        ],
                        max_length=32,
                    ),
                ),
                ("entity_id", models.UUIDField()),
                (
                    "params",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                        validators=[ledger.validators.validate_json_has_no_payment_data],
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                            ("dead", "Dead"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=1)),
                (
                    "result",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
                ("replay_key", models.UUIDField(blank=True, null=True, unique=True)),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "jobs",
                "indexes": [
                    models.Index(fields=["status", "created_at"], name="jobs_status_created_idx"),
                    models.Index(
                        fields=["entity_type", "entity_id", "created_at"], name="jobs_entity_idx"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "kind__in",
                                [
                                    "operator_sync",
                                    "operator_retry",
                                    "operator_link",
                                    "autopay_enable",
                                    "autopay_pause",
                                    "autopay_resume",
                                    "payment_mark",
                                    "payment_retry_mark",
                                    "payment_refund",
                                    "payment_void",
                                ],
                            )
                        ),
                        name="jobs_kind_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("entity_type__in", ["operator", "lease", "rent_charge", "payment"])
                        ),
                        name="jobs_entity_type_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("status__in", ["queued", "running", "done", "failed", "dead"])
                        ),
                        name="jobs_status_valid",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
