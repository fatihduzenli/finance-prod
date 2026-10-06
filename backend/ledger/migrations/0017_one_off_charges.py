"""One-off charges (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D1).

``setup_charges`` becomes ``one_off_charges`` (model ``OneOffCharge``); the payment and
invoice columns ``setup_charge`` become ``one_off_charge`` and the payment kind
``setup`` becomes ``one_off``. New columns: ``operator`` (copied from the lease),
``category`` and ``collection`` (closed lists), ``description`` (the invoice line),
``voided_at``, ``voided_by``, ``void_note`` and ``charge_now_key``; ``lease`` and
``fee_type`` become nullable (an operator-level charge has neither); status gains
``voided``. ``deposit_resolutions.setup_charge`` keeps its name and now points at the
renamed table.

Existing rows: every charge gets ``operator`` from its lease, ``category='setup'``,
``collection='none'`` and its fee type's name as ``description``; every ``setup``
payment becomes ``one_off``; ``audit_log`` rows of entity type ``setup_charge`` and
actions ``setup_charge.*`` are rewritten to ``one_off_charge`` (and the
``setup_charge`` key of their ``before``/``after`` with them). On PostgreSQL the
append-only trigger of ``audit_log`` is disabled for that one statement set and enabled
again in the same transaction. The job rows are rewritten by
``payments.0010_one_off_jobs``.

On PostgreSQL the table's payment-data trigger (from ``0012_setup_charges``) is
replaced by one named for the new table that also covers ``description`` and
``void_note``. The reverse restores everything, and refuses while a charge exists that
the old schema cannot hold (a category other than ``setup``, or no lease).
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Value
from django.db.models.functions import Concat, Substr

import ledger.validators

OLD_TABLE = "setup_charges"
TABLE = "one_off_charges"
OLD_GUARDED: tuple[str, ...] = ("vendor", "waive_note", "note", "review_message")
#: Table -> guarded columns. Replaces the ``setup_charges`` entry of ``0012``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: ("vendor", "waive_note", "note", "review_message", "description", "void_note"),
}
OLD_ENTITY = "setup_charge"
NEW_ENTITY = "one_off_charge"
OLD_KIND = "setup"
NEW_KIND = "one_off"
CANNOT_REVERSE = (
    "one_off_charges holds charges the setup_charges schema cannot hold (a category other "
    "than setup, or no lease); this migration refuses to reverse over them."
)


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


def drop_old_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        _drop_trigger(schema_editor, OLD_TABLE)


def restore_old_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(_trigger_sql(OLD_TABLE, OLD_GUARDED), params=None)


def create_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(_trigger_sql(TABLE, GUARDED_COLUMNS[TABLE]), params=None)


def drop_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        _drop_trigger(schema_editor, TABLE)


def _set_audit_append_only(schema_editor: BaseDatabaseSchemaEditor, enabled: bool) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    state = "ENABLE" if enabled else "DISABLE"
    schema_editor.execute(
        f"ALTER TABLE audit_log {state} TRIGGER audit_log_append_only;", params=None
    )


def _rename_key(payload: object, old: str, new: str) -> object:
    if isinstance(payload, dict) and old in payload:
        return {(new if key == old else key): value for key, value in payload.items()}
    return payload


def _rewrite_audit(apps: Apps, schema_editor: BaseDatabaseSchemaEditor, old: str, new: str) -> None:
    audit_log = apps.get_model("ledger", "AuditLog")
    prefix = f"{old}."
    _set_audit_append_only(schema_editor, False)
    try:
        audit_log.objects.filter(entity_type=old).update(entity_type=new)
        audit_log.objects.filter(action__startswith=prefix).update(
            action=Concat(Value(f"{new}."), Substr("action", len(prefix) + 1))
        )
        changed = []
        for row in audit_log.objects.filter(
            models.Q(before__has_key=old) | models.Q(after__has_key=old)
        ).iterator():
            row.before = _rename_key(row.before, old, new)
            row.after = _rename_key(row.after, old, new)
            changed.append(row)
        audit_log.objects.bulk_update(changed, ["before", "after"], batch_size=500)
    finally:
        _set_audit_append_only(schema_editor, True)


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    charge_model = apps.get_model("ledger", "OneOffCharge")
    payment_model = apps.get_model("ledger", "Payment")
    rows = list(charge_model.objects.select_related("lease", "fee_type"))
    for row in rows:
        # Both were required before this migration.
        row.operator_id = row.lease.operator_id if row.lease is not None else None
        row.category = "setup"
        row.collection = "none"
        row.description = row.fee_type.name[:120] if row.fee_type is not None else ""
    charge_model.objects.bulk_update(
        rows, ["operator", "category", "collection", "description"], batch_size=500
    )
    payment_model.objects.filter(kind=OLD_KIND).update(kind=NEW_KIND)
    _rewrite_audit(apps, schema_editor, OLD_ENTITY, NEW_ENTITY)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    charge_model = apps.get_model("ledger", "OneOffCharge")
    payment_model = apps.get_model("ledger", "Payment")
    held = charge_model.objects.filter(
        models.Q(lease__isnull=True) | ~models.Q(category="setup") | models.Q(status="voided")
    )
    if held.exists():
        raise RuntimeError(CANNOT_REVERSE)
    payment_model.objects.filter(kind=NEW_KIND).update(kind=OLD_KIND)
    _rewrite_audit(apps, schema_editor, NEW_ENTITY, OLD_ENTITY)


#: The categories other than ``setup`` (as ``ledger.models.NON_SETUP_CATEGORIES``).
NON_SETUP = ["arrears", "adjustment", "damage", "other"]
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
        migrations.RunPython(forwards, backwards, elidable=False),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("price__gt", 0)), name="one_off_charges_price_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("cost__isnull", True), ("cost__gte", 0), _connector="OR"),
                name="one_off_charges_cost_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_paid__gte", 0)),
                name="one_off_charges_amount_paid_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
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
                name="one_off_charges_waived_has_note",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status", "voided"),
                        ("void_note__regex", "\\S"),
                        ("voided_at__isnull", False),
                    ),
                    models.Q(
                        models.Q(("status", "voided"), _negated=True), ("voided_at__isnull", True)
                    ),
                    _connector="OR",
                ),
                name="one_off_charges_voided_has_note",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=(~models.Q(category="setup") | models.Q(fee_type__isnull=False))
                & (~models.Q(category__in=NON_SETUP) | models.Q(fee_type__isnull=True)),
                name="one_off_charges_fee_type_iff_setup",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=~models.Q(category__in=NON_SETUP) | models.Q(description__regex="\\S"),
                name="one_off_charges_description_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=~models.Q(collection="none", category__in=NON_SETUP),
                name="one_off_charges_collection_none_only_setup",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(operator__isnull=False)
                | (models.Q(lease__isnull=False) & ~models.Q(category__in=NON_SETUP)),
                name="one_off_charges_has_operator",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.UniqueConstraint(
                condition=models.Q(("legacy", False)),
                fields=("lease", "fee_type"),
                name="one_off_charges_lease_fee_type_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["owed", "partial", "paid", "waived", "voided"])),
                name="one_off_charges_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("category__in", ["setup", "arrears", "adjustment", "damage", "other"])
                ),
                name="one_off_charges_category_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("collection__in", ["none", "autopay_due", "email", "manual", "charge_now"])
                ),
                name="one_off_charges_collection_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("one_off_charge__isnull", False)),
                fields=("one_off_charge",),
                name="invoices_one_per_one_off_charge",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("charge_adjustment__isnull", True),
                        ("insurance_charge__isnull", True),
                        ("one_off_charge__isnull", True),
                    ),
                    models.Q(
                        ("charge_adjustment__isnull", True),
                        ("one_off_charge__isnull", True),
                        ("rent_charge__isnull", True),
                    ),
                    models.Q(
                        ("insurance_charge__isnull", True),
                        ("one_off_charge__isnull", True),
                        ("rent_charge__isnull", True),
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
                        ("kind__in", ["one_off", "refund_deposit"]),
                        ("one_off_charge__isnull", False),
                    ),
                    models.Q(
                        models.Q(("kind__in", ["one_off", "refund_deposit"]), _negated=True),
                        ("one_off_charge__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payments_one_off_kind_iff_one_off_charge",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("one_off_charge__isnull", True),
                    models.Q(
                        ("insurance_charge__isnull", True),
                        ("rent_charge__isnull", True),
                        ("charge_adjustment__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payments_one_off_charge_only_target",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kind__in", ["rent", "insurance", "adjustment", "one_off", "refund_deposit"])
                ),
                name="payments_kind_valid",
            ),
        ),
        migrations.RunPython(create_guard, drop_guard, elidable=False),
    ]
