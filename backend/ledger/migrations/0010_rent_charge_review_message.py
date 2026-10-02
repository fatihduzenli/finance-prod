"""``rent_charges.review_message`` (docs/qa/PLAN-009-payments-ledger.md D2, D3).

Why the payment provider's invoice was not updated after a manual payment: a fixed text
written by the rent run (a partial amount, an invoice already shown paid, or the class
of a failed call and the key names it rejected), or empty. Existing rows get ``''``.

It is free text, so the card-number guard covers it: on PostgreSQL this migration
creates the first payment-data trigger on ``rent_charges``, with the function of
``0002_postgresql_guards``. The reverse drops the trigger before the column. Nothing is
created on other vendors; the application validator applies there.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: Table -> guarded columns after this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "rent_charges": ("review_message",),
}


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    arguments = ", ".join(f"'{column}'" for column in GUARDED_COLUMNS["rent_charges"])
    schema_editor.execute(
        "CREATE TRIGGER rent_charges_payment_data_guard "
        "BEFORE INSERT OR UPDATE ON rent_charges "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS rent_charges_payment_data_guard ON rent_charges;", params=None
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0009_payment_returns_resolution"),
    ]

    operations = [
        migrations.AddField(
            model_name="rentcharge",
            name="review_message",
            field=models.TextField(
                blank=True,
                default="",
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
    ]
