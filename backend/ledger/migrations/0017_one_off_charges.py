"""One-off charges, the schema (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D1).

``setup_charges`` becomes ``one_off_charges`` (model ``OneOffCharge``); the payment and
invoice columns ``setup_charge`` become ``one_off_charge`` and the payment kind
``setup`` becomes ``one_off``. New columns: ``operator`` (copied from the lease),
``category`` and ``collection`` (closed lists), ``description`` (the invoice line),
``voided_at``, ``voided_by``, ``void_note`` and ``charge_now_key``; ``lease`` and
``fee_type`` become nullable (an operator-level charge has neither); status gains
``voided``. ``deposit_resolutions.setup_charge`` keeps its name and now points at the
renamed table.

The rename is three migrations, each in its own transaction, so that no table is
altered after rows of it were updated in the same transaction (PostgreSQL refuses
that with "pending trigger events" once a deferred foreign-key check is queued; the
staging deployment of 2026-10-06 failed on it):

* this one changes the schema only and updates no row: it drops the old constraints
  and the old payment-data trigger, renames, and adds the columns;
* ``0018_one_off_charges_rows`` only rewrites rows;
* ``0019_one_off_charges_constraints`` adds the new constraints and the new trigger.

The job rows are rewritten by ``payments.0011_one_off_jobs_rows``. The reverse of each
step obeys the same rule.
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

OLD_TABLE = "setup_charges"
TABLE = "one_off_charges"
OLD_GUARDED: tuple[str, ...] = ("vendor", "waive_note", "note", "review_message")


def trigger_sql(table: str, columns: tuple[str, ...]) -> str:
    arguments = ", ".join(f"'{column}'" for column in columns)
    return (
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});"
    )


def drop_trigger(schema_editor: BaseDatabaseSchemaEditor, table: str) -> None:
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
    )


def drop_old_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        drop_trigger(schema_editor, OLD_TABLE)


def restore_old_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(trigger_sql(OLD_TABLE, OLD_GUARDED), params=None)


STATUS_CHOICES = [
    ("owed", "Owed"),
    ("partial", "Partially paid"),
    ("paid", "Paid"),
    ("waived", "Waived"),
    ("voided", "Voided"),
]
CATEGORY_CHOICES = [
    ("setup", "Setup"),
    ("arrears", "Arrears"),
    ("adjustment", "Adjustment"),
    ("damage", "Damage"),
    ("other", "Other"),
]
COLLECTION_CHOICES = [
    ("none", "None"),
    ("autopay_due", "Autopay on the due date"),
    ("email", "Send by email"),
    ("manual", "Manual payment"),
    ("charge_now", "Charge now"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0016_store_cost_corrections"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(drop_old_guard, restore_old_guard, elidable=False),
        migrations.RemoveConstraint(model_name="setupcharge", name="setup_charges_price_positive"),
        migrations.RemoveConstraint(
            model_name="setupcharge", name="setup_charges_cost_not_negative"
        ),
        migrations.RemoveConstraint(
            model_name="setupcharge", name="setup_charges_amount_paid_not_negative"
        ),
        migrations.RemoveConstraint(model_name="setupcharge", name="setup_charges_waived_has_note"),
        migrations.RemoveConstraint(
            model_name="setupcharge", name="setup_charges_lease_fee_type_unique"
        ),
        migrations.RemoveConstraint(model_name="setupcharge", name="setup_charges_status_valid"),
        migrations.RemoveConstraint(model_name="invoice", name="invoices_one_per_setup_charge"),
        migrations.RemoveConstraint(model_name="invoice", name="invoices_at_most_one_source"),
        migrations.RemoveConstraint(
            model_name="payment", name="payments_setup_kind_iff_setup_charge"
        ),
        migrations.RemoveConstraint(model_name="payment", name="payments_setup_charge_only_target"),
        migrations.RemoveConstraint(model_name="payment", name="payments_kind_valid"),
        migrations.RenameModel(old_name="SetupCharge", new_name="OneOffCharge"),
        migrations.AlterModelTable(name="oneoffcharge", table=TABLE),
        migrations.RenameField(
            model_name="invoice", old_name="setup_charge", new_name="one_off_charge"
        ),
        migrations.RenameField(
            model_name="payment", old_name="setup_charge", new_name="one_off_charge"
        ),
        migrations.AlterField(
            model_name="oneoffcharge",
            name="lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="one_off_charges",
                to="ledger.lease",
            ),
        ),
        migrations.AlterField(
            model_name="oneoffcharge",
            name="fee_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="one_off_charges",
                to="ledger.feetype",
            ),
        ),
        migrations.AlterField(
            model_name="oneoffcharge",
            name="status",
            field=models.CharField(choices=STATUS_CHOICES, default="owed", max_length=32),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="operator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="one_off_charges",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="category",
            field=models.CharField(choices=CATEGORY_CHOICES, default="setup", max_length=32),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="description",
            field=models.CharField(
                blank=True,
                default="",
                max_length=120,
                validators=[ledger.validators.validate_no_payment_data],
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="collection",
            field=models.CharField(choices=COLLECTION_CHOICES, default="none", max_length=32),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="voided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="voided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="void_note",
            field=models.TextField(
                blank=True, default="", validators=[ledger.validators.validate_no_payment_data]
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="charge_now_key",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
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
                ],
                max_length=32,
            ),
        ),
    ]
