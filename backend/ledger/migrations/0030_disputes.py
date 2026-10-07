"""The dispute list (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D8).

``disputes`` holds the payment provider's disputes as last read (the provider's
identifier in ``external_refs``); ``dispute_evidence`` an evidence file and its upload.
``documents`` gains a third owner, ``operator``, for dispute evidence only (kind
``dispute_evidence``, the only kind an operator keeps), and the TIFF type. No existing
row changes.

Free text is guarded: on PostgreSQL ``disputes`` (``reason``, ``status``,
``raw_summary``) and ``dispute_evidence`` (``name``, ``error``) get the payment-data
trigger of ``0002_postgresql_guards``. The reverse drops the triggers, and refuses while
a dispute evidence document exists (the restored owner rule could not hold it).
"""

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.models
import ledger.validators

#: Table -> guarded columns.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "disputes": ("reason", "status", "raw_summary"),
    "dispute_evidence": ("name", "error"),
}
CANNOT_REVERSE = "Dispute evidence documents exist; this migration refuses to reverse."


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


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    document = apps.get_model("ledger", "Document")
    if document.objects.filter(operator__isnull=False).exists():
        raise RuntimeError(CANNOT_REVERSE)


def nothing(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return None


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0029_invoice_builder"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Dispute",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        max_length=64,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("amount", ledger.models.MoneyField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        blank=True,
                        max_length=64,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("respond_by", models.DateTimeField(blank=True, null=True)),
                ("provider_created_at", models.DateTimeField(blank=True, null=True)),
                ("changed_at", models.DateTimeField(blank=True, null=True)),
                ("fetched_at", models.DateTimeField()),
                (
                    "raw_summary",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        validators=[ledger.validators.validate_json_has_no_payment_data],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "disputes",
            },
        ),
        migrations.CreateModel(
            name="DisputeEvidence",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=150, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("queued", "Queued"), ("sent", "Sent"), ("failed", "Failed")],
                        default="queued",
                        max_length=32,
                    ),
                ),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                (
                    "error",
                    models.CharField(
                        blank=True,
                        max_length=100,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "dispute_evidence",
            },
        ),
        migrations.RemoveConstraint(
            model_name="document",
            name="documents_exactly_one_owner",
        ),
        migrations.RemoveConstraint(
            model_name="document",
            name="documents_kind_valid",
        ),
        migrations.RemoveConstraint(
            model_name="document",
            name="documents_content_type_valid",
        ),
        migrations.AddField(
            model_name="document",
            name="operator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="ledger.operator",
            ),
        ),
        migrations.AlterField(
            model_name="document",
            name="content_type",
            field=models.CharField(
                choices=[
                    ("application/pdf", "PDF"),
                    (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "Word",
                    ),
                    ("image/jpeg", "JPEG image"),
                    ("image/png", "PNG image"),
                    ("image/heic", "HEIC image"),
                    ("image/tiff", "TIFF image"),
                ],
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="document",
            name="kind",
            field=models.CharField(
                choices=[
                    ("lease_agreement", "Lease agreement"),
                    ("amendment", "Amendment"),
                    ("floor_plan", "Floor plan"),
                    ("photo", "Photo"),
                    ("notice", "Notice"),
                    ("other", "Other"),
                    ("dispute_evidence", "Dispute evidence"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("lease__isnull", True),
                        ("operator__isnull", True),
                        ("store__isnull", False),
                    ),
                    models.Q(
                        ("lease__isnull", False),
                        ("operator__isnull", True),
                        ("store__isnull", True),
                    ),
                    models.Q(
                        ("lease__isnull", True),
                        ("operator__isnull", False),
                        ("store__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="documents_exactly_one_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("operator__isnull", True), ("kind", "dispute_evidence"), _connector="XOR"
                ),
                name="documents_operator_keeps_dispute_evidence",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        [
                            "lease_agreement",
                            "amendment",
                            "floor_plan",
                            "photo",
                            "notice",
                            "other",
                            "dispute_evidence",
                        ],
                    )
                ),
                name="documents_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "content_type__in",
                        [
                            "application/pdf",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "image/jpeg",
                            "image/png",
                            "image/heic",
                            "image/tiff",
                        ],
                    )
                ),
                name="documents_content_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True), ("operator__isnull", False)),
                fields=("operator", "sha256"),
                name="documents_operator_sha256_live_unique",
            ),
        ),
        migrations.AddField(
            model_name="dispute",
            name="operator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="disputes",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="dispute",
            name="payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="disputes",
                to="ledger.payment",
            ),
        ),
        migrations.AddField(
            model_name="dispute",
            name="store",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="disputes",
                to="ledger.store",
            ),
        ),
        migrations.AddField(
            model_name="disputeevidence",
            name="dispute",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="evidence",
                to="ledger.dispute",
            ),
        ),
        migrations.AddField(
            model_name="disputeevidence",
            name="document",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="dispute_evidence",
                to="ledger.document",
            ),
        ),
        migrations.AddField(
            model_name="disputeevidence",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="dispute",
            index=models.Index(fields=["respond_by"], name="disputes_respond_by_idx"),
        ),
        migrations.AddConstraint(
            model_name="disputeevidence",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["queued", "sent", "failed"])),
                name="dispute_evidence_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="disputeevidence",
            constraint=models.CheckConstraint(
                condition=models.Q(("name__regex", "\\S")), name="dispute_evidence_name_not_blank"
            ),
        ),
        migrations.AddConstraint(
            model_name="disputeevidence",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("sent_at__isnull", False), ("status", "sent")),
                    models.Q(("status", "sent"), _negated=True),
                    _connector="OR",
                ),
                name="dispute_evidence_sent_has_time",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        migrations.RunPython(nothing, refuse_reverse, elidable=False),
    ]
