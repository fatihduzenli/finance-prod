"""The portal's Appearance is Light or Dark too (docs/qa/PLAN-015-design-v2.md §4.4; owner,
2026-11-05): ``portal_users.theme`` takes ``accounts.models.Theme``, which no longer offers
``system``.

Schema: the column keeps its type; its choices lose ``system``, its default becomes ``dark``, and
the CHECK ``portal_users_theme_valid`` is dropped and added again on ``light`` and ``dark``. Data,
between the two: every stored ``system`` becomes ``dark``. No column is added, so no guarded
column (``GUARDED_COLUMNS`` stays 0001's). Reversible: the reverse restores the three-value CHECK,
the choices and the default; the rows stay ``dark``.
"""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def system_to_dark(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    portal_user = apps.get_model("portal", "PortalUser")
    portal_user.objects.filter(theme="system").update(theme="dark")


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0002_portal_payments"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="portaluser", name="portal_users_theme_valid"),
        migrations.RunPython(system_to_dark, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="portaluser",
            name="theme",
            field=models.CharField(
                choices=[("light", "Light"), ("dark", "Dark")], default="dark", max_length=6
            ),
        ),
        migrations.AddConstraint(
            model_name="portaluser",
            constraint=models.CheckConstraint(
                condition=models.Q(("theme__in", ["light", "dark"])),
                name="portal_users_theme_valid",
            ),
        ),
    ]
