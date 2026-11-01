"""The Bank deposits module's two company settings
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I2):
``company_settings.bank_deposits_enabled`` and ``bank_auto_confirm_exact``, both false, edited
on Admin › Settings › Bank deposits. No text column is added, so no payment-data trigger
changes (an empty ``GUARDED_COLUMNS``). The reverse drops the two columns; the bank tables of
``banking.0001_initial``, which depends on this migration, are reversed first.
"""

from __future__ import annotations

from django.db import migrations, models

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}


class Migration(migrations.Migration):
    dependencies = [("ledger", "0064_portal_messages")]

    operations = [
        migrations.AddField(
            model_name="companysettings",
            name="bank_deposits_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="bank_auto_confirm_exact",
            field=models.BooleanField(default=False),
        ),
    ]
