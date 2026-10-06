"""The collections ladder and the outbound e-mail log (docs/qa/PLAN-011b D4).

- ``message_templates``: a collection e-mail's subject and text, admin-edited; the merge
  fields are checked by the model on save (``ledger.merge``).
- ``collection_steps``: the ladder, admin-edited (seeded by ``0025``).
- ``outbound_messages`` is reshaped from the table reserved in PLAN-006 (``0004``), which
  has never had a writer: it is dropped and created again in the D4 shape, so the
  migration **refuses to run, in either direction, when the table has a row**.
- ``operators.handled_personally`` and ``handled_personally_note``.

On PostgreSQL the free-text columns are covered by the
payment-data trigger of ``0002_postgresql_guards`` (the function it created is reused):
the new tables get their own, the old ``outbound_messages`` trigger goes with its table,
and the operators trigger is recreated with the new column. ``body_sha256`` and
``mail_message_id`` are identifiers held to their shape by CHECK constraints instead;
``error`` (a class name) has a CHECK and the guard. The reverse undoes each step in
order. Nothing is created on other vendors; the application validator applies there.
"""

from __future__ import annotations

import importlib
import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

TABLE = "outbound_messages"
OLD_GUARDED_COLUMNS: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0004_reserved_tables"
).GUARDED_COLUMNS[TABLE]
_OPERATORS_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0005_operator_sync_failure_fields"
).GUARDED_COLUMNS["operators"]

#: Table -> guarded columns after this migration. Replaces the ``outbound_messages`` entry
#: of ``0004`` and the ``operators`` entry of ``0005``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "operators": (*_OPERATORS_BEFORE, "handled_personally_note"),
    "message_templates": ("code", "subject", "body"),
    TABLE: ("to_address", "subject", "error"),
}
NEW_TABLES: tuple[str, ...] = ("message_templates", TABLE)
NOT_EMPTY = (
    "The outbound_messages table has rows; this migration drops and recreates it and "
    "refuses to run over existing data."
)


def _refuse_when_rows_exist(schema_editor: BaseDatabaseSchemaEditor) -> None:
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"SELECT 1 FROM {TABLE} LIMIT 1")  # noqa: S608 - constant name
        if cursor.fetchone() is not None:
            raise RuntimeError(NOT_EMPTY)


def refuse_forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _refuse_when_rows_exist(schema_editor)


def refuse_backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _refuse_when_rows_exist(schema_editor)


def _trigger_sql(table: str, columns: tuple[str, ...]) -> str:
    arguments = ", ".join(f"'{column}'" for column in columns)
    return (
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});"
    )


def _drop_trigger(schema_editor: BaseDatabaseSchemaEditor, table: str) -> None:
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
    )


def restore_old_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_trigger_sql(TABLE, OLD_GUARDED_COLUMNS), params=None)


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        _drop_trigger(schema_editor, table)
        schema_editor.execute(_trigger_sql(table, columns), params=None)


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in NEW_TABLES:
        _drop_trigger(schema_editor, table)
    _drop_trigger(schema_editor, "operators")
    schema_editor.execute(_trigger_sql("operators", _OPERATORS_BEFORE), params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0023_contacts_and_calendar"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Forward: refuse before anything changes.
        migrations.RunPython(refuse_forwards, migrations.RunPython.noop, elidable=False),
        # Reverse: the old trigger comes back once the old table is back.
        migrations.RunPython(migrations.RunPython.noop, restore_old_guard, elidable=False),
        migrations.DeleteModel(name="OutboundMessage"),
        migrations.CreateModel(
            name="MessageTemplate",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "code",
                    models.SlugField(
                        max_length=64,
                        unique=True,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "subject",
                    models.CharField(
                        max_length=200, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("body", models.TextField(validators=[ledger.validators.validate_no_payment_data])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "message_templates",
                "ordering": ("code",),
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("subject__regex", "\\S")),
                        name="message_templates_subject_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("body__regex", "\\S")),
                        name="message_templates_body_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("code__regex", "^[a-z0-9_]+$")),
                        name="message_templates_code_shape",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="CollectionStep",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("day_offset", models.PositiveSmallIntegerField()),
                ("active", models.BooleanField(default=True)),
                ("position", models.SmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="steps",
                        to="ledger.messagetemplate",
                    ),
                ),
            ],
            options={
                "db_table": "collection_steps",
                "ordering": ("day_offset", "position"),
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("day_offset__gte", 1), ("day_offset__lte", 365)),
                        name="collection_steps_day_offset_range",
                    ),
                    models.UniqueConstraint(
                        fields=("day_offset",), name="collection_steps_day_unique"
                    ),
                ],
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
                    "charge_entity_type",
                    models.CharField(
                        choices=[
                            ("rent_charge", "Rent charge"),
                            ("one_off_charge", "One-off charge"),
                        ],
                        max_length=32,
                    ),
                ),
                ("charge_entity_id", models.UUIDField()),
                (
                    "to_address",
                    models.EmailField(
                        max_length=254, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "subject",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("body_sha256", models.CharField(blank=True, default="", max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("suppressed", "Suppressed"),
                            ("catch_all", "Sent to the catch-all address"),
                        ],
                        default="queued",
                        max_length=32,
                    ),
                ),
                ("mail_message_id", models.CharField(blank=True, default="", max_length=128)),
                (
                    "error",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=100,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("manual", models.BooleanField(default=False)),
                ("queued_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                (
                    "operator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="outbound_messages",
                        to="ledger.operator",
                    ),
                ),
                (
                    "sent_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "step",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="messages",
                        to="ledger.collectionstep",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.messagetemplate",
                    ),
                ),
            ],
            options={
                "db_table": "outbound_messages",
                "indexes": [
                    models.Index(fields=["operator", "queued_at"], name="outbound_messages_op_idx"),
                    models.Index(
                        fields=["charge_entity_type", "charge_entity_id"],
                        name="outbound_messages_charge_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "status__in",
                                ["queued", "sent", "failed", "suppressed", "catch_all"],
                            )
                        ),
                        name="outbound_messages_status_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("charge_entity_type__in", ["rent_charge", "one_off_charge"])
                        ),
                        name="outbound_messages_charge_entity_type_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("body_sha256__regex", "^([0-9a-f]{64})?$")),
                        name="outbound_messages_body_sha256_hex",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("error__regex", "^([A-Za-z_][A-Za-z0-9_]*)?$")),
                        name="outbound_messages_error_class_name",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("mail_message_id__regex", "^[A-Za-z0-9_-]*$")),
                        name="outbound_messages_mail_message_id_shape",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("manual", False), ("sent_by__isnull", False), _connector="OR"
                        ),
                        name="outbound_messages_manual_has_sender",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("manual", False)),
                        fields=("charge_entity_type", "charge_entity_id", "step"),
                        name="outbound_messages_one_per_charge_step",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="operator",
            name="handled_personally",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="operator",
            name="handled_personally_note",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        # Reverse: refuse before anything changes.
        migrations.RunPython(migrations.RunPython.noop, refuse_backwards, elidable=False),
    ]
