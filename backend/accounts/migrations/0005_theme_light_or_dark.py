"""Appearance is Light or Dark (docs/qa/PLAN-015-design-v2.md §4.4; owner, 2026-11-05).

Schema: ``accounts_userpreference.theme`` keeps its column; its choices lose ``system`` and its
default becomes ``dark`` (Django choices and default only: no SQL for the column). Data: every
stored ``system`` becomes ``dark`` (a display preference: no audit row). Reversible: the reverse
restores the choices and the default; the rows stay ``dark``.
"""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def system_to_dark(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    preference = apps.get_model("accounts", "UserPreference")
    preference.objects.filter(theme="system").update(theme="dark")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_payment_status_period"),
    ]

    operations = [
        migrations.RunPython(system_to_dark, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="userpreference",
            name="theme",
            field=models.CharField(
                choices=[("light", "Light"), ("dark", "Dark")], default="dark", max_length=6
            ),
        ),
    ]
