"""Pause and Resume for installment plans (docs/qa/PLAN-012f-b D16, slice D-3).

``InstallmentPlanStatus`` gains ``paused`` ("Paused"): the ``status`` column of
``installment_plans`` changes only its ``choices`` (no SQL) and its CHECK constraint
``installment_plans_status_valid`` is recreated with ``paused`` among the values, as 0054
did for the cadence. ``status`` is a choice, not free text, so no guard trigger changes
(``GUARDED_COLUMNS`` is empty).

The reverse restores the CHECK without ``paused``; it fails while a plan is paused, as it
should.
"""

from __future__ import annotations

from django.db import migrations, models
from django.db.models import Q

STATUSES = [
    ("active", "Active"),
    ("attention", "Needs attention"),
    ("paused", "Paused"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
]
#: No text column is added or changed (the guard lists of the tests read this).
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0054_installment_settlement"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="installmentplan",
            name="installment_plans_status_valid",
        ),
        migrations.AlterField(
            model_name="installmentplan",
            name="status",
            field=models.CharField(choices=STATUSES, default="active", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="installmentplan",
            constraint=models.CheckConstraint(
                condition=Q(status__in=[value for value, _ in STATUSES]),
                name="installment_plans_status_valid",
            ),
        ),
    ]
