"""Reshape the reserved ``documents`` table for uploads on stores and leases
(docs/qa/PLAN-010c-documents-and-cost-corrections.md D4).

The table was reserved in PLAN-006 and has never had a writer. The reserved
``operator`` and ``file_reference`` columns are dropped, which is irreversible for any
data they held, so the migration **refuses to run, in either direction, when the table
has a row**. ``created_at`` becomes ``uploaded_at``; ``kind`` becomes a closed list.

On PostgreSQL the table's payment-data trigger (from ``0004_reserved_tables``) is
replaced by one over the new text columns: ``original_filename``, ``blob_path``,
``note`` and ``delete_note`` (``sha256`` is an identifier, outside the guard, held to a
digest's shape by the ``documents_sha256_hex`` check instead). The
reverse restores the old trigger. Nothing is created on other vendors; the application
validator applies there.
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

TABLE = "documents"
OLD_GUARDED_COLUMNS: tuple[str, ...] = ("kind", "file_reference")
#: Table -> guarded columns. Replaces the ``documents`` entry of ``0004_reserved_tables``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: ("original_filename", "blob_path", "note", "delete_note"),
}
NOT_EMPTY = (
    "The documents table has rows; this migration drops reserved columns and refuses to "
    "run over existing data."
)


def refuse_when_rows_exist(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT 1 FROM {TABLE} LIMIT 1")  # noqa: S608 - constant name
        if cursor.fetchone() is not None:
            raise RuntimeError(NOT_EMPTY)


def _trigger_sql(columns: tuple[str, ...]) -> str:
    arguments = ", ".join(f"'{column}'" for column in columns)
    return (
        f"CREATE TRIGGER {TABLE}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});"
    )


def _drop_trigger(schema_editor: BaseDatabaseSchemaEditor) -> None:
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {TABLE}_payment_data_guard ON {TABLE};", params=None
    )


def drop_old_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _drop_trigger(schema_editor)


def restore_old_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_trigger_sql(OLD_GUARDED_COLUMNS), params=None)


def create_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_trigger_sql(GUARDED_COLUMNS[TABLE]), params=None)


def drop_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _drop_trigger(schema_editor)


KIND_CHOICES = [
    ("lease_agreement", "Lease agreement"),
    ("amendment", "Amendment"),
    ("floor_plan", "Floor plan"),
    ("photo", "Photo"),
    ("notice", "Notice"),
    ("other", "Other"),
]
CONTENT_TYPE_CHOICES = [
    ("application/pdf", "PDF"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word"),
    ("image/jpeg", "JPEG image"),
    ("image/png", "PNG image"),
    ("image/heic", "HEIC image"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0014_store_costs_deposit_resolutions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Forward: refuse before anything changes.
        migrations.RunPython(refuse_when_rows_exist, migrations.RunPython.noop, elidable=False),
        migrations.RunPython(drop_old_guard, restore_old_guard, elidable=False),
        migrations.RemoveField(model_name="document", name="operator"),
        migrations.RemoveField(model_name="document", name="file_reference"),
        migrations.RenameField(
            model_name="document", old_name="created_at", new_name="uploaded_at"
        ),
        migrations.AlterField(
            model_name="document",
            name="kind",
            field=models.CharField(choices=KIND_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="document",
            name="source",
            field=models.CharField(
                choices=[("upload", "Upload"), ("docusign", "DocuSign")],
                default="upload",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="store",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="ledger.store",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="documents",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="original_filename",
            field=models.CharField(
                default="",
                max_length=150,
                validators=[ledger.validators.validate_no_payment_data],
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="document",
            name="content_type",
            field=models.CharField(choices=CONTENT_TYPE_CHOICES, default="", max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="document",
            name="size_bytes",
            field=models.BigIntegerField(default=0),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="document",
            name="blob_path",
            field=models.CharField(
                default="",
                max_length=200,
                unique=True,
                validators=[ledger.validators.validate_no_payment_data],
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="document",
            name="sha256",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="document",
            name="note",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="delete_note",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("lease__isnull", True), ("store__isnull", False)),
                    models.Q(("lease__isnull", False), ("store__isnull", True)),
                    _connector="OR",
                ),
                name="documents_exactly_one_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        ["lease_agreement", "amendment", "floor_plan", "photo", "notice", "other"],
                    )
                ),
                name="documents_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("content_type__in", [value for value, _ in CONTENT_TYPE_CHOICES])
                ),
                name="documents_content_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(("size_bytes__gt", 0)), name="documents_size_bytes_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(("original_filename__regex", "\\S")),
                name="documents_original_filename_not_blank",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(("sha256__regex", "\\A[0-9a-f]{64}\\Z")),
                name="documents_sha256_hex",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("deleted_at__isnull", True), ("delete_note__regex", "\\S"), _connector="OR"
                ),
                name="documents_deleted_has_note",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True), ("store__isnull", False)),
                fields=("store", "sha256"),
                name="documents_store_sha256_live_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.UniqueConstraint(
                condition=models.Q(("deleted_at__isnull", True), ("lease__isnull", False)),
                fields=("lease", "sha256"),
                name="documents_lease_sha256_live_unique",
            ),
        ),
        migrations.RunPython(create_guard, drop_guard, elidable=False),
        # Reverse: refuse before anything changes (the reverse runs this first).
        migrations.RunPython(migrations.RunPython.noop, refuse_when_rows_exist, elidable=False),
    ]
