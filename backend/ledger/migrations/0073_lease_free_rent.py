"""Free rent by days on a lease (docs/qa/15/PLAN-015d §3 (2)).

Schema, on ``leases``: ``free_rent_days`` (0 to 366, default 0: none) and ``free_rent_from``
(``move_in`` or ``next_month``, default ``move_in``), each held by a CHECK. Every existing lease
gets no free rent; no row is rewritten. No text column without choices is added, so no
payment-data trigger changes (an empty ``GUARDED_COLUMNS``).

The reverse drops both columns (the charges keep their concessions; ledger ``0072``'s reverse
refuses while one exists).
"""

from __future__ import annotations

from django.db import migrations, models
from django.db.models import Q

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
FROM_CHOICES = [
    ("move_in", "From the move-in date"),
    ("next_month", "From the 1st of the following month"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0072_rent_charge_void_and_concession"),
    ]

    operations = [
        migrations.AddField(
            model_name="lease",
            name="free_rent_days",
            field=models.SmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="lease",
            name="free_rent_from",
            field=models.CharField(choices=FROM_CHOICES, default="move_in", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="lease",
            constraint=models.CheckConstraint(
                condition=Q(free_rent_days__gte=0, free_rent_days__lte=366),
                name="leases_free_rent_days_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="lease",
            constraint=models.CheckConstraint(
                condition=Q(free_rent_from__in=[value for value, _label in FROM_CHOICES]),
                name="leases_free_rent_from_valid",
            ),
        ),
    ]
