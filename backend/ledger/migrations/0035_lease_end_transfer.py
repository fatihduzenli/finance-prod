"""Lease end and transfer (docs/qa/PLAN-012e-lease-end-transfer-installments.md D4 to D7).

Schema:

- ``lease_ends`` (the end-of-lease wizard's state and confirmed figures, one per lease)
  and ``lease_end_deductions`` (its typed deductions);
- ``lease_transfers``, created empty for the transfer wizard (D6, Batch B);
- ``deposit_resolutions.offset_amount`` (default 0): the part of a withheld deposit
  applied to the lease's open charges as "deposit offset" payments, never above the
  withheld amount;
- ``documents.kind`` gains ``final_statement``, which a store alone keeps.

Existing rows: every deposit resolution gets an offset of 0 (nothing was offset before),
every document keeps its kind. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any
check this transaction queued before the ``ALTER TABLE`` statements (the lesson of
``0018``).

Free text is guarded: on PostgreSQL the new tables get the payment-data trigger of
``0002_postgresql_guards`` over their text and JSON columns. Nothing is created on
other vendors; the application validator applies there.

The reverse refuses while a lease end, a transfer, a final statement or a deposit
offset exists: their rows would be lost or would break the restored constraints.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import F, Q

import ledger.models
import ledger.validators

#: Table -> guarded columns created by this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "lease_ends": ("reason_note", "refund_reference", "settlement_note", "statement_json"),
    "lease_end_deductions": ("description",),
}
KINDS_BEFORE = [
    "lease_agreement",
    "amendment",
    "floor_plan",
    "photo",
    "notice",
    "other",
    "dispute_evidence",
]
KINDS = [*KINDS_BEFORE, "final_statement"]
CANNOT_REVERSE = (
    "Lease ends, transfers, final statements or deposit offsets exist; this migration "
    "refuses to reverse."
)


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


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if (
        apps.get_model("ledger", "LeaseEnd").objects.exists()
        or apps.get_model("ledger", "LeaseTransfer").objects.exists()
        or apps.get_model("ledger", "Document").objects.filter(kind="final_statement").exists()
        or apps.get_model("ledger", "DepositResolution").objects.exclude(offset_amount=0).exists()
    ):
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


def _user_fk() -> Any:
    return models.ForeignKey(
        blank=True,
        null=True,
        on_delete=django.db.models.deletion.PROTECT,
        related_name="+",
        to=settings.AUTH_USER_MODEL,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0034_proration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        # --- Documents: the new kind, kept by a store only.
        migrations.RemoveConstraint(model_name="document", name="documents_kind_valid"),
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
                    ("final_statement", "Final statement"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=Q(kind__in=KINDS), name="documents_kind_valid"
            ),
        ),
        migrations.AddConstraint(
            model_name="document",
            constraint=models.CheckConstraint(
                condition=~Q(kind="final_statement") | Q(store__isnull=False),
                name="documents_final_statement_on_store",
            ),
        ),
        # --- Deposit resolutions: the offset part of a withheld deposit.
        migrations.AddField(
            model_name="depositresolution",
            name="offset_amount",
            field=ledger.models.MoneyField(default=Decimal("0.00")),
        ),
        migrations.AddConstraint(
            model_name="depositresolution",
            constraint=models.CheckConstraint(
                condition=Q(offset_amount=0)
                | (Q(offset_amount__gt=0) & Q(offset_amount__lte=F("withheld_amount"))),
                name="deposit_resolutions_offset_within_withheld",
            ),
        ),
        # --- Lease ends.
        migrations.CreateModel(
            name="LeaseEnd",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("ended_on", models.DateField(blank=True, null=True)),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("moved_out", "Moved out"),
                            ("terminated", "Terminated"),
                            ("transfer", "Transfer to another operator"),
                            ("other", "Other"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                (
                    "reason_note",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("prorate_last_month", models.BooleanField(default=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("draft", "Draft"), ("confirmed", "Confirmed")],
                        default="draft",
                        max_length=32,
                    ),
                ),
                (
                    "refund_method",
                    models.CharField(
                        blank=True,
                        choices=[("zelle", "Zelle"), ("wire", "Wire"), ("check", "Check")],
                        default="",
                        max_length=32,
                    ),
                ),
                ("refund_on", models.DateField(blank=True, null=True)),
                (
                    "refund_reference",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=100,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "settlement_note",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "statement_json",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        validators=[ledger.validators.validate_json_has_no_payment_data],
                    ),
                ),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", _user_fk()),
                (
                    "debt_charge",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.oneoffcharge",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.document",
                    ),
                ),
                (
                    "lease",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lease_end",
                        to="ledger.lease",
                    ),
                ),
                (
                    "refund_payment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.payment",
                    ),
                ),
            ],
            options={
                "db_table": "lease_ends",
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(
                            reason__in=["moved_out", "terminated", "transfer", "other", ""]
                        ),
                        name="lease_ends_reason_valid",
                    ),
                    models.CheckConstraint(
                        condition=Q(status__in=["draft", "confirmed"]),
                        name="lease_ends_status_valid",
                    ),
                    models.CheckConstraint(
                        condition=Q(refund_method__in=["zelle", "wire", "check", ""]),
                        name="lease_ends_refund_method_valid",
                    ),
                    models.CheckConstraint(
                        condition=~Q(status="confirmed")
                        | (Q(ended_on__isnull=False, confirmed_at__isnull=False) & ~Q(reason="")),
                        name="lease_ends_confirmed_complete",
                    ),
                    models.CheckConstraint(
                        condition=Q(status="confirmed") | Q(confirmed_at__isnull=True),
                        name="lease_ends_confirmed_at_iff_confirmed",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="LeaseEndDeduction",
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
                            ("damage", "Damage"),
                            ("cleaning", "Cleaning"),
                            ("unpaid_balance", "Unpaid balance offset"),
                            ("other", "Other"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "description",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=200,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("amount", ledger.models.MoneyField()),
                ("position", models.SmallIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "lease_end",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deductions",
                        to="ledger.leaseend",
                    ),
                ),
            ],
            options={
                "db_table": "lease_end_deductions",
                "ordering": ("position", "created_at"),
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(kind__in=["damage", "cleaning", "unpaid_balance", "other"]),
                        name="lease_end_deductions_kind_valid",
                    ),
                    models.CheckConstraint(
                        condition=Q(amount__gt=0), name="lease_end_deductions_amount_positive"
                    ),
                ],
            },
        ),
        # --- Transfers (D6, Batch B): the table only.
        migrations.CreateModel(
            name="LeaseTransfer",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", _user_fk()),
                (
                    "draft",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.invoicedraft",
                    ),
                ),
                (
                    "incoming_lease",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transfers_in",
                        to="ledger.lease",
                    ),
                ),
                (
                    "lease_end",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transfer",
                        to="ledger.leaseend",
                    ),
                ),
                (
                    "outgoing_lease",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transfers_out",
                        to="ledger.lease",
                    ),
                ),
                (
                    "store",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transfers",
                        to="ledger.store",
                    ),
                ),
            ],
            options={
                "db_table": "lease_transfers",
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(incoming_lease__isnull=True)
                        | ~Q(incoming_lease=F("outgoing_lease")),
                        name="lease_transfers_two_leases",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        # --- The reverse starts here: it refuses before anything is dropped.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
