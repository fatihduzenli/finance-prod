"""``reconciliation_runs`` and ``reconciliation_mismatches`` (docs/qa/PLAN-010 D10), and
the ``reconcile_run`` job kind with its ``reconciliation`` entity type.

On PostgreSQL the guarded columns get the card-number trigger of
``ledger.0002_postgresql_guards`` (the function it created is reused). The reverse drops
the triggers before the tables go. Nothing is created on other vendors; the application
validator applies there.
"""

import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: Table -> guarded columns: every text column without choices and every JSON column.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "reconciliation_runs": ("counts", "error"),
    "reconciliation_mismatches": ("entity_type", "detail", "resolution_note"),
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
        ("payments", "0006_job_kinds_reads"),
        ("ledger", "0002_postgresql_guards"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReconciliationMismatch",
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
                            ("invoice_status_differs", "Invoice status differed"),
                            ("payment_restored", "Payment restored"),
                            ("deposit_unmatched", "Deposit row unmatched"),
                            ("settled_payment_undeposited", "Settled payment not deposited"),
                            ("return_missed_by_webhook", "Return missed by webhook"),
                            ("pending_too_old", "Pending too long"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "entity_type",
                    models.CharField(
                        max_length=32, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("entity_id", models.UUIDField()),
                (
                    "detail",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "resolution_note",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
            ],
            options={
                "db_table": "reconciliation_mismatches",
            },
        ),
        migrations.CreateModel(
            name="ReconciliationRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "trigger",
                    models.CharField(
                        choices=[("scheduled", "Scheduled"), ("manual", "Run now")], max_length=16
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        default="running",
                        max_length=16,
                    ),
                ),
                ("started_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "step_reached",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "None"),
                            ("a", "Invoices"),
                            ("b", "Transactions"),
                            ("c", "Deposits"),
                            ("d", "ACH rejections"),
                            ("e", "Pending payments"),
                            ("f", "Report"),
                        ],
                        default="",
                        max_length=1,
                    ),
                ),
                (
                    "counts",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        validators=[ledger.validators.validate_json_has_no_payment_data],
                    ),
                ),
                ("requests", models.PositiveIntegerField(default=0)),
                ("duration_ms", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "error",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
            ],
            options={
                "db_table": "reconciliation_runs",
            },
        ),
        migrations.RemoveConstraint(
            model_name="job",
            name="jobs_kind_valid",
        ),
        migrations.RemoveConstraint(
            model_name="job",
            name="jobs_entity_type_valid",
        ),
        migrations.AlterField(
            model_name="job",
            name="entity_type",
            field=models.CharField(
                choices=[
                    ("operator", "Operator"),
                    ("lease", "Lease"),
                    ("rent_charge", "Rent charge"),
                    ("payment", "Payment"),
                    ("rent_run", "Rent run"),
                    ("webhook_event", "Webhook event"),
                    ("reconciliation", "Reconciliation"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="job",
            name="kind",
            field=models.CharField(
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
                    ("operator_methods_refresh", "Refresh payment methods"),
                    ("run_match", "Match invoices"),
                    ("webhook_replay", "Replay webhook event"),
                    ("reconcile_run", "Run reconciliation"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
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
                            "operator_methods_refresh",
                            "run_match",
                            "webhook_replay",
                            "reconcile_run",
                        ],
                    )
                ),
                name="jobs_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "entity_type__in",
                        [
                            "operator",
                            "lease",
                            "rent_charge",
                            "payment",
                            "rent_run",
                            "webhook_event",
                            "reconciliation",
                        ],
                    )
                ),
                name="jobs_entity_type_valid",
            ),
        ),
        migrations.AddField(
            model_name="reconciliationmismatch",
            name="resolved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="reconciliationrun",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="reconciliationmismatch",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="mismatches",
                to="payments.reconciliationrun",
            ),
        ),
        migrations.AddIndex(
            model_name="reconciliationrun",
            index=models.Index(fields=["trigger", "started_at"], name="recon_runs_started_idx"),
        ),
        migrations.AddConstraint(
            model_name="reconciliationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("trigger__in", ["scheduled", "manual"])),
                name="reconciliation_runs_trigger_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="reconciliationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["running", "done", "failed", "skipped"])),
                name="reconciliation_runs_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="reconciliationrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("step_reached__in", ["", "a", "b", "c", "d", "e", "f"])),
                name="reconciliation_runs_step_reached_valid",
            ),
        ),
        migrations.AddIndex(
            model_name="reconciliationmismatch",
            index=models.Index(
                fields=["resolved_at", "created_at"], name="recon_mismatches_list_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="reconciliationmismatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        [
                            "invoice_status_differs",
                            "payment_restored",
                            "deposit_unmatched",
                            "settled_payment_undeposited",
                            "return_missed_by_webhook",
                            "pending_too_old",
                        ],
                    )
                ),
                name="reconciliation_mismatches_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="reconciliationmismatch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("resolved_at__isnull", True),
                    ("resolution_note__regex", "\\S"),
                    _connector="OR",
                ),
                name="reconciliation_mismatches_resolved_has_note",
            ),
        ),
        migrations.AddConstraint(
            model_name="reconciliationmismatch",
            constraint=models.UniqueConstraint(
                condition=models.Q(("resolved_at__isnull", True)),
                fields=("kind", "entity_type", "entity_id"),
                name="reconciliation_mismatches_one_open",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
