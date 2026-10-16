"""The setup checklist's flag (docs/qa/PLAN-012d-store-workspace.md D9).

Schema: ``fee_types.show_in_setup`` (boolean, default false): the item is a row of the
setup checklist (the new lease form and the transfer's incoming step).

Data: the fee types that exist when this runs, are active and are not NSF fees (the
seeded catalogue: deposit, access, sign, architecture, business purchase, phone,
insurance) get ``show_in_setup`` true, so the checklist offers what it offered before;
an item saved from the invoice builder later starts false. The reverse drops the column
plainly (the flag holds no money and no free text). No free text is added, so no
payment-data trigger changes (``GUARDED_COLUMNS`` is empty).
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

#: Table -> guarded columns added by this migration: none.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
#: The NSF fee types (``ledger.models.NSF_FEE_CODES`` when this was written): never on
#: the checklist.
NSF_FEE_CODES = ("nsf_fee_first", "nsf_fee_later")


def show_the_catalogue(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    fee_type = apps.get_model("ledger", "FeeType")
    fee_type.objects.filter(active=True).exclude(code__in=NSF_FEE_CODES).update(show_in_setup=True)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0042_payment_allocation_and_credits"),
    ]

    operations = [
        migrations.AddField(
            model_name="feetype",
            name="show_in_setup",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(show_the_catalogue, migrations.RunPython.noop, elidable=False),
    ]
