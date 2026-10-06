"""Seed the NSF fee types (docs/qa/PLAN-011b D5).

``nsf_fee_first`` (100.00, an operator's first rescheduled return) and ``nsf_fee_later``
(50.00, every later one): not refundable and **inactive**, so the setup checklist never
offers them; the Reschedule action reads their price whatever ``active`` says. The admin
edits the amounts in the Django admin (the admin group already holds the fee type
permissions, 0013). A type that already exists is left as it is.

Its own migration, so that the reverse deletes the rows in a transaction of its own; it
removes only the seeded types no one-off charge uses.
"""

from __future__ import annotations

from decimal import Decimal

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

#: (code, name, default price, sort order).
FEE_TYPES: tuple[tuple[str, str, Decimal, int], ...] = (
    ("nsf_fee_first", "NSF fee, first return", Decimal("100.00"), 900),
    ("nsf_fee_later", "NSF fee, later returns", Decimal("50.00"), 910),
)


def seed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    fee_type = apps.get_model("ledger", "FeeType")
    for code, name, price, sort_order in FEE_TYPES:
        fee_type.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "default_cost": None,
                "default_price": price,
                "refundable": False,
                "active": False,
                "sort_order": sort_order,
            },
        )


def unseed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("ledger", "FeeType").objects.filter(
        code__in=[row[0] for row in FEE_TYPES],
        one_off_charges__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("ledger", "0026_nsf_reschedule_statement_links")]

    operations = [migrations.RunPython(seed, unseed, elidable=False)]
