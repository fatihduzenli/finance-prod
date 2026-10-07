"""The contact log grows up (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D2 to D5).

``contact_entries`` gains ``lease`` (the store an entry is about), ``status`` (``open``,
``kept``, ``rescheduled``, ``broken``; every kind but a promise stays ``open``, CHECK),
``kept_by_payment`` (the settled payment that kept a promise; only on a ``kept`` one,
CHECK) and the soft delete ``deleted_at``, ``deleted_by``, ``delete_reason`` (all three
or none, CHECK). ``contact_corrections`` holds the corrections appended to an entry.

A promise closed before this migration (by the removed "closes the promise" choice, its
``closed_by`` set) had no recorded outcome; it is given ``broken`` so that it stays off
the attention list, as it was, and never reads as paid. Every other row stays ``open``.

Free text is guarded: on PostgreSQL the ``contact_entries`` trigger of
``0023_contacts_and_calendar`` is replaced by one that also covers ``delete_reason``, and
``contact_corrections`` gets the payment-data trigger of ``0002_postgresql_guards``. The
reverse restores the old trigger and drops the new one. Nothing is created on other
vendors; the application validator applies there.
"""

from __future__ import annotations

import uuid
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

OLD_GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {"contact_entries": ("body",)}
#: Table -> guarded columns. Replaces the ``contact_entries`` entry of ``0023``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "contact_entries": ("body", "delete_reason"),
    "contact_corrections": ("body",),
}


def _create(schema_editor: BaseDatabaseSchemaEditor, table: str, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def _drop(schema_editor: BaseDatabaseSchemaEditor, table: str) -> None:
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        _drop(schema_editor, table)
        _create(schema_editor, table, columns)


def restore_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_COLUMNS:
        _drop(schema_editor, table)
    for table, columns in OLD_GUARDED_COLUMNS.items():
        _create(schema_editor, table, columns)


def closed_promises_broken(apps: Any, schema_editor: Any) -> None:
    apps.get_model("ledger", "ContactEntry").objects.filter(
        kind="promise", closed_by__isnull=False
    ).update(status="broken")


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0027_seed_nsf_fee_types"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ContactCorrection",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("body", models.TextField(validators=[ledger.validators.validate_no_payment_data])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "contact_corrections",
            },
        ),
        migrations.AddField(
            model_name="contactentry",
            name="delete_reason",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddField(
            model_name="contactentry",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contactentry",
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
            model_name="contactentry",
            name="kept_by_payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="kept_promises",
                to="ledger.payment",
            ),
        ),
        migrations.AddField(
            model_name="contactentry",
            name="lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="contact_entries",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="contactentry",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("kept", "Kept"),
                    ("rescheduled", "Rescheduled"),
                    ("broken", "Broken"),
                ],
                default="open",
                max_length=32,
            ),
        ),
        migrations.RunPython(closed_promises_broken, migrations.RunPython.noop, elidable=False),
        migrations.AddConstraint(
            model_name="contactentry",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["open", "kept", "rescheduled", "broken"])),
                name="contact_entries_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind__in", ["call", "email", "visit"]), _negated=True),
                    models.Q(("status__in", ["kept", "rescheduled", "broken"]), _negated=True),
                    _connector="OR",
                ),
                name="contact_entries_status_promise_only",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kept_by_payment__isnull", True), ("status", "kept"), _connector="OR"
                ),
                name="contact_entries_kept_by_payment_kept",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("delete_reason", ""),
                        ("deleted_at__isnull", True),
                        ("deleted_by__isnull", True),
                    ),
                    models.Q(
                        ("deleted_at__isnull", False),
                        ("deleted_by__isnull", False),
                        ("delete_reason__regex", "\\S"),
                    ),
                    _connector="OR",
                ),
                name="contact_entries_deleted_all_or_none",
            ),
        ),
        migrations.AddField(
            model_name="contactcorrection",
            name="author",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="contactcorrection",
            name="entry",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="corrections",
                to="ledger.contactentry",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactcorrection",
            constraint=models.CheckConstraint(
                condition=models.Q(("body__regex", "\\S")),
                name="contact_corrections_body_not_blank",
            ),
        ),
        migrations.RunPython(create_guards, restore_guards, elidable=False),
    ]
