"""An existing open charge on an invoice draft line (docs/qa/PLAN-012d-store-workspace.md
D10).

Schema: ``invoice_draft_lines.existing_charge`` (FK ``one_off_charges``, null): the open
one-off charge of the draft's lease the line bills; on send the charge is attached to
the invoice instead of a new one being created. Such a line is neither a Rent line nor a
catalogue item's (CHECK ``invoice_draft_lines_existing_charge``: no rent lease, no rent
month, no fee type).

Existing rows keep the column null, which the CHECK accepts. No free text is added, so
no payment-data trigger changes (``GUARDED_COLUMNS`` is empty). The reverse drops the
constraint and the column plainly; a draft's existing-charge line then reads as a typed
item of the same text and amount (nothing was sent from a draft).
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

#: Table -> guarded columns added by this migration: none.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0043_fee_type_show_in_setup"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoicedraftline",
            name="existing_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.oneoffcharge",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("existing_charge__isnull", True),
                    models.Q(
                        ("fee_type__isnull", True),
                        ("rent_lease__isnull", True),
                        ("rent_month__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="invoice_draft_lines_existing_charge",
            ),
        ),
    ]
