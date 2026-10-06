"""One-off charges, the constraints (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D1).

The last of the three steps of the rename (see ``0017_one_off_charges``); it updates no
row. It adds the constraints of ``one_off_charges`` and the renamed ones of
``invoices`` and ``payments`` (which need the rows of ``0018_one_off_charges_rows``),
and, on PostgreSQL, the table's payment-data trigger: it replaces the ``setup_charges``
one of ``0012_setup_charges`` (dropped by ``0017``) and also covers ``description`` and
``void_note``. The reverse removes them.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_SCHEMA = importlib.import_module("ledger.migrations.0017_one_off_charges")

TABLE: str = _SCHEMA.TABLE
#: Table -> guarded columns. Replaces the ``setup_charges`` entry of ``0012``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: ("vendor", "waive_note", "note", "review_message", "description", "void_note"),
}
#: The categories other than ``setup`` (as ``ledger.models.NON_SETUP_CATEGORIES``).
NON_SETUP = ["arrears", "adjustment", "damage", "other"]


def create_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(_SCHEMA.trigger_sql(TABLE, GUARDED_COLUMNS[TABLE]), params=None)


def drop_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor == "postgresql":
        _SCHEMA.drop_trigger(schema_editor, TABLE)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0018_one_off_charges_rows"),
    ]

    operations = [
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
