"""``collection_schedules.needs_review`` (PLAN-007 D5).

Set when a lease changes under a provider schedule (rent, debit day or operator). The
schedule is never edited for an amount; staff pause it and re-enable autopay. Existing
rows get ``false``. The reverse drops the column.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0006_operator_phone_rule"),
    ]

    operations = [
        migrations.AddField(
            model_name="collectionschedule",
            name="needs_review",
            field=models.BooleanField(default=False),
        ),
    ]
