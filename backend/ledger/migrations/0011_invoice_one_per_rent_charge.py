"""One invoice per rent charge: a unique partial index on ``invoices.rent_charge``
where it is set (docs/qa/PLAN-008a-send-invoice-and-manual-link.md §2 rules 3, 4).

The auto-link and the manual link check under row locks that the charge has no invoice;
the index makes the rule hold whatever the order of two concurrent links. The rent
run's matching already links at most one invoice per charge, so existing rows satisfy
it.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0010_rent_charge_review_message"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("rent_charge__isnull", False)),
                fields=("rent_charge",),
                name="invoices_one_per_rent_charge",
            ),
        ),
    ]
