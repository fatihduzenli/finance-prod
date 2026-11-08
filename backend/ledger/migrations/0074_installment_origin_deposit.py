"""A deposit paid in installments (docs/qa/15/PLAN-015d §3 (3)).

``installment_plans.origin`` gains ``deposit`` (the New lease page's "When: in installments"):
the field's choices and the ``installment_plans_origin_valid`` CHECK accept it. No row is
touched and no column is added (an empty ``GUARDED_COLUMNS``).

The reverse refuses, with one sentence and nothing changed, while a plan of that origin exists.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
NEW_VALUE = "deposit"
ORIGIN_AFTER = [
    ("split", "Split into installments"),
    ("settlement", "Settlement plan"),
    ("deposit", "Deposit installments"),
]
CANNOT_REVERSE = "A deposit installment plan exists; this migration refuses to reverse."


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if apps.get_model("ledger", "InstallmentPlan").objects.filter(origin=NEW_VALUE).exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0073_lease_free_rent"),
    ]

    operations = [
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="installmentplan", name="installment_plans_origin_valid"
        ),
        migrations.AlterField(
            model_name="installmentplan",
            name="origin",
            field=models.CharField(choices=ORIGIN_AFTER, default="split", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=Q(origin__in=[value for value, _label in ORIGIN_AFTER]),
                name="installment_plans_origin_valid",
            ),
        ),
        # Last, so that the reverse checks the rows first.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
