"""Autopay plans (docs/qa/PLAN-012f-a-design-follow-up.md D11, D13).

Schema: the table ``autopay_plans`` (lease, operator, store, item, detail, quantity, unit
amount, amount, the catalogue item for reporting, cadence, first date, end rule with its
count or date, state, next date, occurrences made, the large-debit confirmation, who and
when) with CHECKs for its choices, its amounts, the end rule's shape and the next date,
and at most one live plan (active or paused) per lease and item (a partial unique index).
On ``one_off_charges``: the category ``autopay_plan``, ``autopay_plan`` and ``period_no``,
set exactly for that category and only with the collection ``autopay_due`` (a CHECK), one
charge per plan and period (a unique index); the category's CHECK and the CHECKs that read
the list of categories other than ``setup`` are made again with the new value. The
statement column of ``lease_ends`` is left as it is (the owner has not answered its
rename). On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this transaction
queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), and the new text
columns (``autopay_plans.item_name``, ``autopay_plans.description``) get the payment-data
trigger of ``0002_postgresql_guards``.

The reverse refuses while a plan or an occurrence exists: they would be lost.
"""

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

#: Table -> guarded columns created by this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {"autopay_plans": ("item_name", "description")}
CANNOT_REVERSE = "Autopay plans exist; this migration refuses to reverse."


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
    plans = apps.get_model("ledger", "AutopayPlan").objects.exists()
    occurrences = (
        apps.get_model("ledger", "OneOffCharge").objects.filter(category="autopay_plan").exists()
    )
    if plans or occurrences:
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0048_check_method"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.CreateModel(
            name="AutopayPlan",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "item_name",
                    models.CharField(
                        max_length=200, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "description",
                    models.TextField(
                        blank=True, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("quantity", ledger.models.StrictDecimalField(decimal_places=4, max_digits=12)),
                ("unit_amount", ledger.models.MoneyField()),
                ("amount", ledger.models.MoneyField()),
                (
                    "cadence",
                    models.CharField(
                        choices=[
                            ("monthly", "Monthly"),
                            ("weekly", "Weekly"),
                            ("yearly", "Yearly"),
                        ],
                        max_length=32,
                    ),
                ),
                ("first_on", models.DateField()),
                (
                    "end_rule",
                    models.CharField(
                        choices=[
                            ("count", "After a number of payments"),
                            ("date", "On a date"),
                            ("never", "Never"),
                        ],
                        max_length=32,
                    ),
                ),
                ("end_count", models.SmallIntegerField(blank=True, null=True)),
                ("end_on", models.DateField(blank=True, null=True)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("paused", "Paused"),
                            ("stopped", "Stopped"),
                            ("finished", "Finished"),
                        ],
                        default="active",
                        max_length=32,
                    ),
                ),
                ("next_on", models.DateField()),
                ("made_count", models.IntegerField(default=0)),
                ("large_debit_confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "autopay_plans",
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
            name="period_no",
            field=models.IntegerField(blank=True, null=True),
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
                    ("nsf_fee", "Returned payment (NSF) fee"),
                    ("installment", "Installment"),
                    ("autopay_plan", "Autopay plan"),
                ],
                default="setup",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="autopayplan",
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
            model_name="autopayplan",
            name="fee_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="autopay_plans",
                to="ledger.feetype",
            ),
        ),
        migrations.AddField(
            model_name="autopayplan",
            name="lease",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="autopay_plans",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="autopayplan",
            name="operator",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="autopay_plans",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="autopayplan",
            name="store",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="autopay_plans",
                to="ledger.store",
            ),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="autopay_plan",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="occurrences",
                to="ledger.autopayplan",
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
                                    "autopay_plan",
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
                                "autopay_plan",
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
                            "autopay_plan",
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
                                    "autopay_plan",
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
                        ("autopay_plan__isnull", False),
                        ("category", "autopay_plan"),
                        ("collection", "autopay_due"),
                        ("period_no__gte", 1),
                        ("period_no__isnull", False),
                    ),
                    models.Q(
                        models.Q(("category", "autopay_plan"), _negated=True),
                        ("autopay_plan__isnull", True),
                        ("period_no__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_autopay_plan_iff_category",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.UniqueConstraint(
                fields=("autopay_plan", "period_no"), name="one_off_charges_one_per_autopay_period"
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
                            "autopay_plan",
                        ],
                    )
                ),
                name="one_off_charges_category_valid",
            ),
        ),
        migrations.AddIndex(
            model_name="autopayplan",
            index=models.Index(fields=["state", "next_on"], name="autopay_plans_state_next_idx"),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("item_name__regex", "\\S")),
                name="autopay_plans_item_name_not_blank",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("quantity__gt", 0)), name="autopay_plans_quantity_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("unit_amount__gt", 0)),
                name="autopay_plans_unit_amount_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)), name="autopay_plans_amount_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("made_count__gte", 0)),
                name="autopay_plans_made_count_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "amount__gte",
                        django.db.models.expressions.CombinedExpression(
                            django.db.models.expressions.CombinedExpression(
                                models.F("quantity"), "*", models.F("unit_amount")
                            ),
                            "-",
                            models.Value(Decimal("0.005")),
                        ),
                    ),
                    (
                        "amount__lte",
                        django.db.models.expressions.CombinedExpression(
                            django.db.models.expressions.CombinedExpression(
                                models.F("quantity"), "*", models.F("unit_amount")
                            ),
                            "+",
                            models.Value(Decimal("0.005")),
                        ),
                    ),
                ),
                name="autopay_plans_amount_is_quantity_times_unit",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        models.Q(("end_rule", "count"), _negated=True),
                        models.Q(
                            ("end_count__gte", 2),
                            ("end_count__isnull", False),
                            ("end_count__lte", 999),
                            ("end_on__isnull", True),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(("end_rule", "date"), _negated=True),
                        models.Q(
                            ("end_count__isnull", True),
                            ("end_on__gte", models.F("first_on")),
                            ("end_on__isnull", False),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(("end_rule", "never"), _negated=True),
                        models.Q(("end_count__isnull", True), ("end_on__isnull", True)),
                        _connector="OR",
                    ),
                ),
                name="autopay_plans_end_rule_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("next_on__gte", models.F("first_on"))),
                name="autopay_plans_next_on_from_first",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state__in", ["active", "paused"])),
                fields=("lease", "item_name"),
                name="autopay_plans_one_live_per_item",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("cadence__in", ["monthly", "weekly", "yearly"])),
                name="autopay_plans_cadence_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("end_rule__in", ["count", "date", "never"])),
                name="autopay_plans_end_rule_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="autopayplan",
            constraint=models.CheckConstraint(
                condition=models.Q(("state__in", ["active", "paused", "stopped", "finished"])),
                name="autopay_plans_state_valid",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        # Reversed first: nothing is undone while a plan or an occurrence exists.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
