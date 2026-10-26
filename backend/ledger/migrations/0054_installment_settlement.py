"""Schedule settlement on the installment engine (docs/qa/PLAN-012f-b D16).

``InstallmentCadence`` gains ``yearly`` ("Yearly"): the ``cadence`` column of
``installment_plans`` changes only its ``choices`` (no SQL) and its CHECK constraint
``installment_plans_cadence_valid`` is recreated with ``yearly`` among the values, as 0048
did for the payment methods. ``installment_plans`` gains ``origin`` (``split`` "Split into
installments", ``settlement`` "Settlement plan"; default ``split``, so every existing plan
is a split) with its CHECK ``installment_plans_origin_valid``. ``origin`` is a choice, not
free text, so no guard trigger changes (``GUARDED_COLUMNS`` is empty).

The reverse drops ``origin`` and restores the cadence CHECK without ``yearly``; it fails
while a plan stores ``yearly``, as it should.
"""

from __future__ import annotations

from django.db import migrations, models
from django.db.models import Q

CADENCES = [
    ("weekly", "Weekly"),
    ("biweekly", "Every two weeks"),
    ("monthly", "Monthly"),
    ("yearly", "Yearly"),
]
ORIGINS = [("split", "Split into installments"), ("settlement", "Settlement plan")]
#: No text column is added or changed (the guard lists of the tests read this).
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0053_cash_method"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="installmentplan",
            name="installment_plans_cadence_valid",
        ),
        migrations.AlterField(
            model_name="installmentplan",
            name="cadence",
            field=models.CharField(choices=CADENCES, max_length=32),
        ),
        migrations.AddField(
            model_name="installmentplan",
            name="origin",
            field=models.CharField(choices=ORIGINS, default="split", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=Q(cadence__in=[value for value, _ in CADENCES]),
                name="installment_plans_cadence_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=Q(origin__in=[value for value, _ in ORIGINS]),
                name="installment_plans_origin_valid",
            ),
        ),
    ]
