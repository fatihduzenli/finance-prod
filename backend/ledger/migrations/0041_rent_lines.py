"""Rent billed by invoice (docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md D4, D5).

Schema:

- ``invoice_draft_lines``: ``rent_lease`` (FK ``leases``, null) and ``rent_month`` (date,
  null); a Rent line has both, the month's first day and no fee type, any other line
  neither (CHECK ``invoice_draft_lines_rent_line``).
- ``invoice_line_items``: ``rent_charge`` (FK ``rent_charges``, null), the rent charge a
  builder invoice's Rent line bills; a line names at most one charge (CHECK
  ``invoice_line_items_one_charge``).
- ``rent_charges``: ``billed_by_invoice`` (FK ``invoices``, null), the invoice whose Rent
  line made or took the month's charge (the grid shows its number).

Existing rows keep every new column null, which every CHECK accepts. No free text is
added, so no payment-data trigger changes (``GUARDED_COLUMNS`` is empty). The reverse
drops the constraints and the columns plainly.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

#: Table -> guarded columns added by this migration: none.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0040_lease_end_statement_opened"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoicedraftline",
            name="rent_lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="invoicedraftline",
            name="rent_month",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="invoicelineitem",
            name="rent_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoice_lines",
                to="ledger.rentcharge",
            ),
        ),
        migrations.AddField(
            model_name="rentcharge",
            name="billed_by_invoice",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.invoice",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicedraftline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("rent_lease__isnull", True), ("rent_month__isnull", True)),
                    models.Q(
                        ("fee_type__isnull", True),
                        ("rent_lease__isnull", False),
                        ("rent_month__day", 1),
                        ("rent_month__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="invoice_draft_lines_rent_line",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicelineitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("one_off_charge__isnull", True),
                    ("rent_charge__isnull", True),
                    _connector="OR",
                ),
                name="invoice_line_items_one_charge",
            ),
        ),
    ]
