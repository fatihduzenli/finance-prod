"""Cash as a manual payment method (docs/qa/PLAN-012f-b D14).

``PaymentMethod`` and ``MoneyMethod`` gain ``cash`` ("Cash"), between ``check`` and
``other``. The three ``method`` columns (``payments``, ``lease_credits``,
``deposit_resolutions``) change only their ``choices`` (no SQL). The CHECK constraints
that list the methods are recreated with ``cash`` among them, as 0048 did for ``check``:
``payments_method_valid``, ``lease_credits_method_valid``,
``deposit_resolutions_method_valid`` and the three that name the methods recorded by
hand, ``payments_manual_method_has_note``, ``payments_refund_deposit_manual`` and
``payments_credit_refund_shape``. No row is touched; no text column is added, so no
guard trigger changes (``GUARDED_COLUMNS`` is empty).

The reverse restores the constraints without ``cash``; it fails while a row stores
``cash``, as it should.
"""

from __future__ import annotations

from django.db import migrations, models
from django.db.models import Q

MONEY = [
    ("ach", "Bank debit (ACH)"),
    ("zelle", "Zelle"),
    ("wire", "Wire"),
    ("check", "Check"),
    ("cash", "Cash"),
    ("other", "Other"),
]
MONEY_VALUES = [value for value, _ in MONEY]
PAYMENT = [*MONEY, ("credit", "Credit")]
PAYMENT_VALUES = [value for value, _ in PAYMENT]
#: Methods recorded by hand (``ledger.models.MANUAL_PAYMENT_METHODS``).
BY_HAND = ["zelle", "wire", "check", "cash", "other"]
#: No text column is added or changed (the guard lists of the tests read this).
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0052_operator_kind"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="depositresolution",
            name="deposit_resolutions_method_valid",
        ),
        migrations.RemoveConstraint(
            model_name="leasecredit",
            name="lease_credits_method_valid",
        ),
        migrations.RemoveConstraint(
            model_name="payment",
            name="payments_manual_method_has_note",
        ),
        migrations.RemoveConstraint(
            model_name="payment",
            name="payments_refund_deposit_manual",
        ),
        migrations.RemoveConstraint(
            model_name="payment",
            name="payments_credit_refund_shape",
        ),
        migrations.RemoveConstraint(
            model_name="payment",
            name="payments_method_valid",
        ),
        migrations.AlterField(
            model_name="depositresolution",
            name="method",
            field=models.CharField(blank=True, choices=MONEY, default="", max_length=32),
        ),
        migrations.AlterField(
            model_name="leasecredit",
            name="method",
            field=models.CharField(choices=MONEY, max_length=32),
        ),
        migrations.AlterField(
            model_name="payment",
            name="method",
            field=models.CharField(choices=PAYMENT, max_length=32),
        ),
        migrations.AddConstraint(
            model_name="depositresolution",
            constraint=models.CheckConstraint(
                condition=Q(method__in=[*MONEY_VALUES, ""]),
                name="deposit_resolutions_method_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=Q(method__in=MONEY_VALUES),
                name="lease_credits_method_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=~Q(method__in=BY_HAND) | Q(note__regex=r"\S"),
                name="payments_manual_method_has_note",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=~Q(kind="refund_deposit")
                | Q(is_refund=False, method__in=BY_HAND, parent_payment__isnull=True),
                name="payments_refund_deposit_manual",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=~Q(kind="credit_refund")
                | Q(
                    charge_adjustment__isnull=True,
                    credit__isnull=False,
                    insurance_charge__isnull=True,
                    is_refund=False,
                    method__in=BY_HAND,
                    one_off_charge__isnull=True,
                    parent_payment__isnull=True,
                    rent_charge__isnull=True,
                ),
                name="payments_credit_refund_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=Q(method__in=PAYMENT_VALUES),
                name="payments_method_valid",
            ),
        ),
    ]
