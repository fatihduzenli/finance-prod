"""Manual payment allocation and lease credits (docs/qa/PLAN-012d-store-workspace.md D6a,
D6b; ADR-0013).

Schema:

- ``lease_credits``: money received for a lease and not applied (lease, amount, remaining,
  method, received on, reference, note, who, the allocation group, when), with CHECKs:
  amount above zero, remaining between zero and the amount, a manual method (Zelle, wire
  or other), a note that is not blank.
- ``payments``: ``allocation_group`` (UUID, null; the payments of one entry of the store
  page's payment form share it) and ``credit`` (FK ``lease_credits``, null). The method
  ``credit`` (a credit applied to a charge) and the kind ``credit_refund`` (a credit paid
  back at the lease end) join the method and kind CHECKs, which are made again; a credit
  payment names its credit and is never a refund row, only a credit payment or a credit
  refund names a credit, and a credit refund is a manual payment of a credit and nothing
  else.

Existing rows keep both new columns null and no existing method or kind changes, so every
CHECK holds over them. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), and the
new text columns (``lease_credits.note`` and ``reference``) get the payment-data trigger of
``0002_postgresql_guards``. The reverse refuses while a credit, a credit payment or a credit
refund exists: they would be lost or break the method and kind CHECKs made again.
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

#: Table -> guarded columns created by this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {"lease_credits": ("note", "reference")}
CANNOT_REVERSE = "Lease credits or credit payments exist; this migration refuses to reverse."


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


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
    credits = apps.get_model("ledger", "LeaseCredit").objects.exists()
    payments = (
        apps.get_model("ledger", "Payment")
        .objects.filter(models.Q(method="credit") | models.Q(kind="credit_refund"))
        .exists()
    )
    if credits or payments:
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0041_rent_lines"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.CreateModel(
            name="LeaseCredit",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("amount", ledger.models.MoneyField()),
                ("remaining", ledger.models.MoneyField()),
                (
                    "method",
                    models.CharField(
                        choices=[
                            ("ach", "ACH"),
                            ("zelle", "Zelle"),
                            ("wire", "Wire"),
                            ("other", "Other"),
                        ],
                        max_length=32,
                    ),
                ),
                ("received_on", models.DateField()),
                (
                    "reference",
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
                ("allocation_group", models.UUIDField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "lease_credits",
            },
        ),
        migrations.RemoveConstraint(
            model_name="payment",
            name="payments_method_valid",
        ),
        migrations.RemoveConstraint(
            model_name="payment",
            name="payments_kind_valid",
        ),
        migrations.AddField(
            model_name="payment",
            name="allocation_group",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="payment",
            name="kind",
            field=models.CharField(
                choices=[
                    ("rent", "Rent"),
                    ("insurance", "Insurance"),
                    ("adjustment", "Adjustment"),
                    ("one_off", "One-off charge"),
                    ("refund_deposit", "Deposit refund"),
                    ("credit_refund", "Credit refund"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="payment",
            name="method",
            field=models.CharField(
                choices=[
                    ("ach", "ACH"),
                    ("zelle", "Zelle"),
                    ("wire", "Wire"),
                    ("other", "Other"),
                    ("credit", "Credit"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="leasecredit",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="leasecredit",
            name="lease",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="credits",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="credit",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="ledger.leasecredit",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("method", "credit"), _negated=True),
                    models.Q(("credit__isnull", False), ("is_refund", False)),
                    _connector="OR",
                ),
                name="payments_credit_method_has_credit",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("credit__isnull", True),
                    ("method", "credit"),
                    ("kind", "credit_refund"),
                    _connector="OR",
                ),
                name="payments_credit_only_credit_rows",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "credit_refund"), _negated=True),
                    models.Q(
                        ("charge_adjustment__isnull", True),
                        ("credit__isnull", False),
                        ("insurance_charge__isnull", True),
                        ("is_refund", False),
                        ("method__in", ["zelle", "wire", "other"]),
                        ("one_off_charge__isnull", True),
                        ("parent_payment__isnull", True),
                        ("rent_charge__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payments_credit_refund_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        [
                            "rent",
                            "insurance",
                            "adjustment",
                            "one_off",
                            "refund_deposit",
                            "credit_refund",
                        ],
                    )
                ),
                name="payments_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(("method__in", ["ach", "zelle", "wire", "other", "credit"])),
                name="payments_method_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)), name="lease_credits_amount_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=models.Q(("remaining__gte", 0)),
                name="lease_credits_remaining_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=models.Q(("remaining__lte", models.F("amount"))),
                name="lease_credits_remaining_at_most_amount",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=models.Q(("method", "ach"), _negated=True),
                name="lease_credits_method_manual",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=models.Q(("note__regex", "\\S")), name="lease_credits_note_not_blank"
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=models.Q(("method__in", ["ach", "zelle", "wire", "other"])),
                name="lease_credits_method_valid",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        # Reversed first: nothing is undone while a credit or a credit payment exists.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
