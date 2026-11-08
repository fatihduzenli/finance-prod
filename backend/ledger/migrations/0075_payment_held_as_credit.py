"""Rent money held as a credit (docs/qa/15/PLAN-015d §3 (1), Fable's finding on "Keep as credit").

Change start date's "Keep as credit" no longer writes refund rows: the payment that paid the
rent of days no longer the lease's is re-pointed from its rent charge to a lease credit, its
method, amount and processor reference unchanged. ``payments_credit_only_credit_rows`` accepts
that row: a ``rent`` payment, not a refund, naming no charge and naming the credit. No row is
touched and no column is added (an empty ``GUARDED_COLUMNS``).

The reverse refuses, with one sentence and nothing changed, while such a row exists.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
NAME = "payments_credit_only_credit_rows"
BEFORE = Q(credit__isnull=True) | Q(method="credit") | Q(kind="credit_refund")
HELD = Q(
    kind="rent",
    is_refund=False,
    rent_charge__isnull=True,
    insurance_charge__isnull=True,
    charge_adjustment__isnull=True,
    one_off_charge__isnull=True,
)
CANNOT_REVERSE = "A payment held as a credit exists; this migration refuses to reverse."


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    payment = apps.get_model("ledger", "Payment")
    held = payment.objects.filter(credit__isnull=False).exclude(method="credit")
    if held.exclude(kind="credit_refund").exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0074_installment_origin_deposit"),
    ]

    operations = [
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(model_name="payment", name=NAME),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(condition=BEFORE | HELD, name=NAME),
        ),
        # Last, so that the reverse checks the rows first.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
