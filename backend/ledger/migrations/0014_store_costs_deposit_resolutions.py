"""Store costs and deposit resolutions (docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md
D7, D8).

``store_costs``: what the business pays the landlord for a store, date-effective, with
one open row per store (a partial unique index) and no two rows of a store starting on
the same day. ``deposit_resolutions``: one row per deposit resolved at the end of its
lease (refunded, withheld or transferred).

The new free-text columns are covered by the card-number guard: on PostgreSQL each new
table gets the payment-data trigger of ``0002_postgresql_guards`` (the function it
created is reused). The reverse drops the triggers before the tables go. Nothing is
created on other vendors; the application validator applies there. No existing row
changes.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.models
import ledger.validators

#: Table -> guarded columns: every text column without choices.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "store_costs": ("other_cost_description", "notes"),
    "deposit_resolutions": ("note",),
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
        ("ledger", "0013_seed_fee_types"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DepositResolution",
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
                            ("refunded", "Refunded"),
                            ("withheld", "Withheld"),
                            ("transferred", "Transferred"),
                        ],
                        max_length=32,
                    ),
                ),
                ("amount", ledger.models.MoneyField()),
                ("withheld_amount", ledger.models.MoneyField(default=Decimal("0.00"))),
                ("resolved_on", models.DateField()),
                (
                    "method",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("ach", "ACH"),
                            ("zelle", "Zelle"),
                            ("wire", "Wire"),
                            ("other", "Other"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                ("note", models.TextField(validators=[ledger.validators.validate_no_payment_data])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "setup_charge",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deposit_resolution",
                        to="ledger.setupcharge",
                    ),
                ),
                (
                    "to_lease",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deposits_received",
                        to="ledger.lease",
                    ),
                ),
            ],
            options={
                "db_table": "deposit_resolutions",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="deposit_resolutions_amount_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("withheld_amount__gte", 0)),
                        name="deposit_resolutions_withheld_amount_not_negative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("note__regex", "\\S")),
                        name="deposit_resolutions_note_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                models.Q(("kind", "refunded"), _negated=True),
                                models.Q(("method__in", ["", "ach"]), _negated=True),
                                _connector="OR",
                            ),
                            models.Q(
                                models.Q(("kind__in", ["withheld", "transferred"]), _negated=True),
                                ("method", ""),
                                _connector="OR",
                            ),
                        ),
                        name="deposit_resolutions_method_iff_refunded",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                models.Q(("kind", "transferred"), _negated=True),
                                ("to_lease__isnull", False),
                                _connector="OR",
                            ),
                            models.Q(
                                models.Q(("kind__in", ["refunded", "withheld"]), _negated=True),
                                ("to_lease__isnull", True),
                                _connector="OR",
                            ),
                        ),
                        name="deposit_resolutions_to_lease_iff_transferred",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("kind__in", ["refunded", "withheld", "transferred"])),
                        name="deposit_resolutions_kind_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("method__in", ["ach", "zelle", "wire", "other", ""])),
                        name="deposit_resolutions_method_valid",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="StoreCost",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("landlord_rent", ledger.models.MoneyField()),
                ("other_monthly_cost", ledger.models.MoneyField(blank=True, null=True)),
                (
                    "other_cost_description",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("landlord_lease_end", models.DateField(blank=True, null=True)),
                ("effective_from", models.DateField()),
                ("effective_to", models.DateField(blank=True, null=True)),
                (
                    "source",
                    models.CharField(
                        choices=[("import", "Import"), ("staff", "Staff")], max_length=32
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "store",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="costs",
                        to="ledger.store",
                    ),
                ),
            ],
            options={
                "db_table": "store_costs",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("landlord_rent__gte", 0)),
                        name="store_costs_landlord_rent_not_negative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("other_monthly_cost__isnull", True),
                            ("other_monthly_cost__gte", 0),
                            _connector="OR",
                        ),
                        name="store_costs_other_monthly_cost_not_negative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("effective_to__isnull", True),
                            ("effective_to__gte", models.F("effective_from")),
                            _connector="OR",
                        ),
                        name="store_costs_effective_order",
                    ),
                    models.UniqueConstraint(
                        fields=("store", "effective_from"), name="store_costs_store_from_unique"
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("effective_to__isnull", True)),
                        fields=("store",),
                        name="store_costs_one_open_row",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("source__in", ["import", "staff"])),
                        name="store_costs_source_valid",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
