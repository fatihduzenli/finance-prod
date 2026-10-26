"""The rent-charge bank debit from Add payment (docs/qa/PLAN-012f-b §10 D19, slice C2).

``one_off_charges`` gains ``source_rent_charge`` (FK ``rent_charges``, null, PROTECT): the
rent charge a ``redebit`` charge debits when no returned payment is behind it. The CHECK
``one_off_charges_source_payment_iff_redebit`` is replaced by
``one_off_charges_redebit_one_source``: a ``redebit`` charge has exactly one of
``source_payment`` and ``source_rent_charge``; every other category has neither. The
partial unique index ``one_off_charges_one_open_debit_per_rent_charge`` keeps one open
(``owed`` / ``partial``) debit per rent charge. A foreign key is not free text, so no
guard trigger changes (``GUARDED_COLUMNS`` is empty).

The reverse removes the index, the new CHECK and the column, and restores the CHECK of
0026; it fails while a re-debit is sourced by a rent charge alone, as it should.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q

#: No text column is added or changed (the guard lists of the tests read this).
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0055_installment_plan_paused"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="oneoffcharge",
            name="one_off_charges_source_payment_iff_redebit",
        ),
        migrations.AddField(
            model_name="oneoffcharge",
            name="source_rent_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="bank_debits",
                to="ledger.rentcharge",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.CheckConstraint(
                condition=Q(
                    category="redebit",
                    source_payment__isnull=False,
                    source_rent_charge__isnull=True,
                )
                | Q(
                    category="redebit",
                    source_payment__isnull=True,
                    source_rent_charge__isnull=False,
                )
                | (
                    ~Q(category="redebit")
                    & Q(source_payment__isnull=True, source_rent_charge__isnull=True)
                ),
                name="one_off_charges_redebit_one_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="oneoffcharge",
            constraint=models.UniqueConstraint(
                fields=["source_rent_charge"],
                condition=Q(status__in=["owed", "partial"]),
                name="one_off_charges_one_open_debit_per_rent_charge",
            ),
        ),
    ]
