"""The Payment status card's period per user
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md A3).

Schema: ``accounts_userpreference.payment_status_period`` (``weekly`` or ``monthly``, default
``weekly``). Existing rows take the default; a user without a row reads as Weekly
(``accounts.themes.payment_status_period_for``). Reversible (drops the column).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_userpreference"),
    ]

    operations = [
        migrations.AddField(
            model_name="userpreference",
            name="payment_status_period",
            field=models.CharField(
                choices=[("weekly", "Weekly"), ("monthly", "Monthly")],
                default="weekly",
                max_length=7,
            ),
        ),
    ]
