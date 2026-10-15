"""A lease end left at the blockers step (docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md
D9).

Schema, on ``lease_ends``: ``statement_opened_at`` (datetime, null), set the first time the
statement step renders with nothing blocking. Existing rows keep it null: a draft without
it is not in progress (no banner on the store page; a transfer adopts it).

No free text is added, so no payment-data trigger changes (``GUARDED_COLUMNS`` is empty).
The reverse drops the column plainly (the moment stays in the ``lease_end.statement_opened``
audit rows).
"""

from __future__ import annotations

from django.db import migrations, models

#: Table -> guarded columns added by this migration: none.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0039_transfer_debt_override"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaseend",
            name="statement_opened_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
