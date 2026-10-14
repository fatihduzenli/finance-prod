"""Installment plans (docs/qa/PLAN-012e-lease-end-transfer-installments.md D9 to D11).

Schema: the table ``installment_plans`` (a rent charge or a one-off charge, exactly one;
operator, lease, count 2 to 12, first date, cadence, collection, status, who and when,
the cancel's note) with CHECKs for its choices, the count, the parent and the cancel's
note, and at most one plan that is not cancelled per parent (two partial unique
indexes). On ``one_off_charges``: the category ``installment``, ``plan`` and
``installment_no``, set exactly for that category (a CHECK); the category's CHECK and
the CHECKs that read the list of categories other than ``setup`` are made again with the
new value. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), and
the new text column (``installment_plans.note``) gets the payment-data trigger of
``0002_postgresql_guards``.

The reverse refuses while a plan or an installment exists: they would be lost.
"""

import uuid

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: Table -> guarded columns created by this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {"installment_plans": ("note",)}
CANNOT_REVERSE = "Installment plans exist; this migration refuses to reverse."


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
    plans = apps.get_model("ledger", "InstallmentPlan").objects.exists()
    installments = (
        apps.get_model("ledger", "OneOffCharge").objects.filter(category="installment").exists()
    )
    if plans or installments:
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0037_transfer_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.CreateModel(
            name="InstallmentPlan",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("count", models.SmallIntegerField()),
                ("first_due", models.DateField()),
                (
                    "cadence",
                    models.CharField(
                        choices=[
                            ("weekly", "Weekly"),
                            ("biweekly", "Every two weeks"),
                            ("monthly", "Monthly"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "collection",
                    models.CharField(
                        choices=[
                            ("autopay_due", "Autopay on the due date"),
                            ("email", "Send by email"),
                            ("manual", "Manual payment"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("attention", "Needs attention"),
                            ("completed", "Completed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="active",
                        max_length=32,
                    ),
                ),
                ("confirmed_at", models.DateTimeField()),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                (
                    "note",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "installment_plans",
            },
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_fee_type_iff_setup",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_description_required",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_collection_none_only_setup",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_has_operator",
        ),
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_category_valid",
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="installment_no",
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="oneoffcharge",
            name="category",
            field=models.CharField(
                choices=[
                    ("setup", "Setup"),
                    ("arrears", "Arrears"),
                    ("adjustment", "Adjustment"),
                    ("damage", "Damage"),
                    ("other", "Other"),
                    ("redebit", "Re-debit"),
                    ("nsf_fee", "NSF fee"),
                    ("installment", "Installment"),
                ],
                default="setup",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="installmentplan",
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
            model_name="installmentplan",
            name="lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="installment_plans",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="installmentplan",
            name="one_off_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="installment_plans",
                to="ledger.oneoffcharge",
            ),
        ),
        migrations.AddField(
            model_name="installmentplan",
            name="operator",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="installment_plans",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="installmentplan",
            name="rent_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="installment_plans",
                to="ledger.rentcharge",
            ),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="plan",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="installments",
                to="ledger.installmentplan",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        models.Q(("category", "setup"), _negated=True),
                        ("fee_type__isnull", False),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(
                            (
                                "category__in",
                                [
                                    "arrears",
                                    "adjustment",
                                    "damage",
                                    "other",
                                    "redebit",
                                    "nsf_fee",
                                    "installment",
                                ],
                            ),
                            _negated=True,
                        ),
                        ("fee_type__isnull", True),
                        _connector="OR",
                    ),
                ),
                name="one_off_charges_fee_type_iff_setup",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        (
                            "category__in",
                            [
                                "arrears",
                                "adjustment",
                                "damage",
                                "other",
                                "redebit",
                                "nsf_fee",
                                "installment",
                            ],
                        ),
                        _negated=True,
                    ),
                    ("description__regex", "\\S"),
                    _connector="OR",
                ),
                name="one_off_charges_description_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "category__in",
                        [
                            "arrears",
                            "adjustment",
                            "damage",
                            "other",
                            "redebit",
                            "nsf_fee",
                            "installment",
                        ],
                    ),
                    ("collection", "none"),
                    _negated=True,
                ),
                name="one_off_charges_collection_none_only_setup",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("operator__isnull", False),
                    models.Q(
                        ("lease__isnull", False),
                        models.Q(
                            (
                                "category__in",
                                [
                                    "arrears",
                                    "adjustment",
                                    "damage",
                                    "other",
                                    "redebit",
                                    "nsf_fee",
                                    "installment",
                                ],
                            ),
                            _negated=True,
                        ),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_has_operator",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("category", "installment"),
                        ("installment_no__gte", 1),
                        ("installment_no__lte", 12),
                        ("plan__isnull", False),
                    ),
                    models.Q(
                        models.Q(("category", "installment"), _negated=True),
                        ("installment_no__isnull", True),
                        ("plan__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_plan_iff_installment",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "category__in",
                        [
                            "setup",
                            "arrears",
                            "adjustment",
                            "damage",
                            "other",
                            "redebit",
                            "nsf_fee",
                            "installment",
                        ],
                    )
                ),
                name="one_off_charges_category_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("one_off_charge__isnull", True), ("rent_charge__isnull", False)),
                    models.Q(("one_off_charge__isnull", False), ("rent_charge__isnull", True)),
                    _connector="OR",
                ),
                name="installment_plans_one_parent",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("count__gte", 2), ("count__lte", 12)),
                name="installment_plans_count_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("cancelled_at__isnull", False),
                        ("note__regex", "\\S"),
                        ("status", "cancelled"),
                    ),
                    models.Q(
                        models.Q(("status", "cancelled"), _negated=True),
                        ("cancelled_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="installment_plans_cancelled_has_note",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "cancelled"), _negated=True),
                fields=("rent_charge",),
                name="installment_plans_one_per_rent_charge",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "cancelled"), _negated=True),
                fields=("one_off_charge",),
                name="installment_plans_one_per_one_off_charge",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("cadence__in", ["weekly", "biweekly", "monthly"])),
                name="installment_plans_cadence_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("collection__in", ["autopay_due", "email", "manual"])),
                name="installment_plans_collection_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["active", "attention", "completed", "cancelled"])
                ),
                name="installment_plans_status_valid",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        # Reversed first: nothing is undone while a plan or an installment exists.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
