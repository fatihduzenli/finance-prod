"""A deposit due on a date (docs/qa/15/PLAN-015d §3 (3), Fable's run-3 finding).

Schema, on ``one_off_charges``: ``dated`` (default false), set at creation on the deposit the
New lease page chose "On a date"; held by ``one_off_charges_dated_only_setup`` (a setup
charge, collection ``none``). Every existing charge is not dated: a deposit saved before rides
its invoice as it always did (no reminders of its own, no portal Pay). No row is rewritten
and no text column is added (an empty ``GUARDED_COLUMNS``).

The reverse refuses, with one sentence and nothing changed, while a dated charge exists.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
CANNOT_REVERSE = "A deposit due on a date exists; this migration refuses to reverse."


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if apps.get_model("ledger", "OneOffCharge").objects.filter(dated=True).exists():
        raise RuntimeError(CANNOT_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0075_payment_held_as_credit"),
    ]

    operations = [
        migrations.AddField(
            model_name="oneoffcharge",
            name="dated",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=Q(dated=False) | Q(category="setup", collection="none"),
                name="one_off_charges_dated_only_setup",
            ),
        ),
        # Last, so that the reverse checks the rows first.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
