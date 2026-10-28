"""The user's display preference (docs/qa/PLAN-012g-dark-theme-and-dashboard.md A2).

Schema: ``accounts_userpreference``, one row per user (``user_id`` unique, CASCADE), ``theme``
(``light``, ``dark`` or ``system``, default ``system``) and ``updated_at``. No backfill: a user
without a row reads as System (``accounts.themes.theme_for``). Reversible (drops the table).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_role_groups"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserPreference",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "theme",
                    models.CharField(
                        choices=[("light", "Light"), ("dark", "Dark"), ("system", "System")],
                        default="system",
                        max_length=6,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="preference",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
