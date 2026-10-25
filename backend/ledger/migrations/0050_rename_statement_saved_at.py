"""Confirm needs a saved statement (docs/qa/PLAN-012f-a-design-follow-up.md §10, slice F).

Schema, on ``lease_ends``: ``statement_opened_at`` (``0040``) is renamed
``statement_saved_at``; its type (datetime, null) and every row's value are kept. It is
set when the statement step is first saved, and confirm refuses a draft without it.

No free text is added, so no payment-data trigger changes (``GUARDED_COLUMNS`` is empty).
The reverse renames the column back, values kept (no data loss).
"""

from __future__ import annotations

from django.db import migrations

#: Table -> guarded columns added by this migration: none.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0049_autopay_plans"),
    ]

    operations = [
        migrations.RenameField(
            model_name="leaseend",
            old_name="statement_opened_at",
            new_name="statement_saved_at",
        ),
    ]
