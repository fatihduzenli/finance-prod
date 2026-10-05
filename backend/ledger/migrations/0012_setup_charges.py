"""Setup fees (docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md D1, D3, D4, D9).

``fee_types`` and ``setup_charges``; ``payments.setup_charge`` with the kinds ``setup``
and ``refund_deposit`` and their CHECKs; ``invoices.setup_charge`` (one invoice per
setup charge, and at most one of the four sources); ``insurance_premiums.cost``.

The new free-text columns are covered by the card-number guard: on PostgreSQL each new
table gets the payment-data trigger of ``0002_postgresql_guards`` (the function it
created is reused). The reverse drops the triggers before the tables go. Nothing is
created on other vendors; the application validator applies there. No existing row
changes: the new columns are null, and every existing payment keeps its kind.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import django.db.models.deletion
import django.db.models.expressions
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.models
import ledger.validators

#: Table -> guarded columns: every text column without choices and every JSON column.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "fee_types": ("code", "name"),
    "setup_charges": ("vendor", "waive_note", "note", "review_message"),
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
        ("ledger", "0011_invoice_one_per_rent_charge"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FeeType",
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
                    "name",
                    models.CharField(
                        max_length=200, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("default_cost", ledger.models.MoneyField(blank=True, null=True)),
                ("default_price", ledger.models.MoneyField(blank=True, null=True)),
                ("refundable", models.BooleanField(default=False)),
                ("active", models.BooleanField(default=True)),
                ("sort_order", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "fee_types",
                "ordering": ("sort_order", "name"),
            },
        ),
        migrations.CreateModel(
            name="SetupCharge",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("cost", ledger.models.MoneyField(blank=True, null=True)),
                ("price", ledger.models.MoneyField()),
                (
                    "vendor",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("due_date", models.DateField()),
                ("amount_paid", ledger.models.MoneyField(default=Decimal("0.00"))),
                (
                    "balance",
                    models.GeneratedField(
                        db_persist=True,
                        expression=django.db.models.expressions.CombinedExpression(
                            models.F("price"), "-", models.F("amount_paid")
                        ),
                        output_field=models.DecimalField(decimal_places=2, max_digits=12),
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("owed", "Owed"),
                            ("partial", "Partially paid"),
                            ("paid", "Paid"),
                            ("waived", "Waived"),
                        ],
                        default="owed",
                        max_length=32,
                    ),
                ),
                ("waived_at", models.DateTimeField(blank=True, null=True)),
                (
                    "waive_note",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "note",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "review_message",
                    models.TextField(
                        blank=True,
                        default="",
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("legacy", models.BooleanField(default=False)),
                ("needs_review", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "setup_charges",
            },
        ),
        migrations.RemoveConstraint(
            model_name="invoice",
            name="invoices_at_most_one_source",
        ),
        migrations.RemoveConstraint(
            model_name="payment",
            name="payments_kind_valid",
        ),
        migrations.AddField(
            model_name="insurancepremium",
            name="cost",
            field=ledger.models.MoneyField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="payment",
            name="kind",
            field=models.CharField(
                choices=[
                    ("rent", "Rent"),
                    ("insurance", "Insurance"),
                    ("adjustment", "Adjustment"),
                    ("setup", "Setup fee"),
                    ("refund_deposit", "Deposit refund"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="insurancepremium",
            constraint=models.CheckConstraint(
                condition=models.Q(("cost__isnull", True), ("cost__gte", 0), _connector="OR"),
                name="insurance_premiums_cost_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="feetype",
            constraint=models.CheckConstraint(
                condition=models.Q(("name__regex", "\\S")), name="fee_types_name_not_blank"
            ),
        ),
        migrations.AddConstraint(
            model_name="feetype",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("default_cost__isnull", True), ("default_cost__gte", 0), _connector="OR"
                ),
                name="fee_types_default_cost_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="feetype",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("default_price__isnull", True), ("default_price__gt", 0), _connector="OR"
                ),
                name="fee_types_default_price_positive",
            ),
        ),
        migrations.AddField(
            model_name="setupcharge",
            name="fee_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="setup_charges",
                to="ledger.feetype",
            ),
        ),
        migrations.AddField(
            model_name="setupcharge",
            name="lease",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="setup_charges",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="setupcharge",
            name="waived_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="setup_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoices",
                to="ledger.setupcharge",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="setup_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="ledger.setupcharge",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("setup_charge__isnull", False)),
                fields=("setup_charge",),
                name="invoices_one_per_setup_charge",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("charge_adjustment__isnull", True),
                        ("insurance_charge__isnull", True),
                        ("setup_charge__isnull", True),
                    ),
                    models.Q(
                        ("charge_adjustment__isnull", True),
                        ("rent_charge__isnull", True),
                        ("setup_charge__isnull", True),
                    ),
                    models.Q(
                        ("insurance_charge__isnull", True),
                        ("rent_charge__isnull", True),
                        ("setup_charge__isnull", True),
                    ),
                    models.Q(
                        ("charge_adjustment__isnull", True),
                        ("insurance_charge__isnull", True),
                        ("rent_charge__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="invoices_at_most_one_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kind__in", ["setup", "refund_deposit"]), ("setup_charge__isnull", False)
                    ),
                    models.Q(
                        models.Q(("kind__in", ["setup", "refund_deposit"]), _negated=True),
                        ("setup_charge__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payments_setup_kind_iff_setup_charge",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("setup_charge__isnull", True),
                    models.Q(
                        ("insurance_charge__isnull", True),
                        ("rent_charge__isnull", True),
                        ("charge_adjustment__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payments_setup_charge_only_target",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "refund_deposit"), _negated=True),
                    models.Q(
                        ("is_refund", False),
                        ("method__in", ["zelle", "wire", "other"]),
                        ("parent_payment__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payments_refund_deposit_manual",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kind__in", ["rent", "insurance", "adjustment", "setup", "refund_deposit"])
                ),
                name="payments_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="setupcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("price__gt", 0)), name="setup_charges_price_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="setupcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("cost__isnull", True), ("cost__gte", 0), _connector="OR"),
                name="setup_charges_cost_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="setupcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_paid__gte", 0)),
                name="setup_charges_amount_paid_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="setupcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status", "waived"),
                        ("waive_note__regex", "\\S"),
                        ("waived_at__isnull", False),
                    ),
                    models.Q(
                        models.Q(("status", "waived"), _negated=True), ("waived_at__isnull", True)
                    ),
                    _connector="OR",
                ),
                name="setup_charges_waived_has_note",
            ),
        ),
        migrations.AddConstraint(
            model_name="setupcharge",
            constraint=models.UniqueConstraint(
                condition=models.Q(("legacy", False)),
                fields=("lease", "fee_type"),
                name="setup_charges_lease_fee_type_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="setupcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["owed", "partial", "paid", "waived"])),
                name="setup_charges_status_valid",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
