"""Company settings (docs/qa/PLAN-012c-design-pass.md D2).

Schema: the table ``company_settings``, one row held by a CHECK (``id = 1``), with CHECKs
for the time zone's choices, the names that may not be blank and the logo override's
shape (blank, or a stored document's blob path). Data: the row is seeded (name and short
name "Monvera", no support e-mail, America/New_York, no logo override, the processor's
display name), and the ``admin`` group gets view and change on it (never add or delete:
the admin registers neither). On PostgreSQL the free-text columns (``name``,
``short_name``, ``support_email``, ``logo``, ``processor_name``) get the payment-data
trigger of ``0002_postgresql_guards``.

The reverse drops the table with its row and removes the two permissions from the group.
"""

from typing import Any

import django.core.validators
from django.apps.registry import Apps
from django.contrib.auth.management import create_permissions
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: Table -> guarded columns created by this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "company_settings": ("name", "short_name", "support_email", "logo", "processor_name"),
}
#: The row as first seeded (this migration's own copy; ``ledger.company_defaults``).
SEED: dict[str, str] = {
    "name": "Monvera",
    "short_name": "Monvera",
    "support_email": "",
    "time_zone": "America/New_York",
    "logo": "",
    "processor_name": "Stax",
}
ADMIN_GROUP = "admin"
ADMIN_PERMISSIONS = (
    ("companysettings", "view_companysettings"),
    ("companysettings", "change_companysettings"),
)


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        arguments = ", ".join(f"'{column}'" for column in columns)
        schema_editor.execute(
            f"CREATE TRIGGER {table}_payment_data_guard "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
            params=None,
        )


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_COLUMNS:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
        )


def _permissions(apps: Apps) -> list[Any]:
    permission_model = apps.get_model("auth", "Permission")
    return [
        permission
        for model, codename in ADMIN_PERMISSIONS
        for permission in permission_model.objects.filter(
            content_type__app_label="ledger", content_type__model=model, codename=codename
        )
    ]


def seed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("ledger", "CompanySettings").objects.get_or_create(pk=1, defaults=SEED)
    # Permissions are normally created after migrate finishes; create them now so they
    # can be attached (as 0025_seed_collections_ladder does).
    app_config = apps.get_app_config("ledger")
    app_config.models_module = True  # type: ignore[assignment]
    create_permissions(app_config, verbosity=0, apps=apps)
    app_config.models_module = None
    permissions = _permissions(apps)
    if len(permissions) != len(ADMIN_PERMISSIONS):
        raise RuntimeError("A ledger permission is missing.")
    group, _ = apps.get_model("auth", "Group").objects.get_or_create(name=ADMIN_GROUP)
    group.permissions.add(*permissions)


def unseed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    group = apps.get_model("auth", "Group").objects.filter(name=ADMIN_GROUP).first()
    if group is not None:
        group.permissions.remove(*_permissions(apps))


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0044_draft_existing_charge"),
        ("accounts", "0002_role_groups"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanySettings",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=100, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "short_name",
                    models.CharField(
                        max_length=100, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                (
                    "support_email",
                    models.EmailField(
                        blank=True,
                        max_length=254,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                (
                    "time_zone",
                    models.CharField(
                        choices=[
                            ("America/New_York", "Eastern (New York)"),
                            ("America/Chicago", "Central (Chicago)"),
                            ("America/Denver", "Mountain (Denver)"),
                            ("America/Phoenix", "Mountain, no daylight saving (Phoenix)"),
                            ("America/Los_Angeles", "Pacific (Los Angeles)"),
                            ("America/Anchorage", "Alaska (Anchorage)"),
                            ("Pacific/Honolulu", "Hawaii (Honolulu)"),
                        ],
                        default="America/New_York",
                        max_length=32,
                    ),
                ),
                (
                    "logo",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        validators=[
                            ledger.validators.validate_no_payment_data,
                            django.core.validators.RegexValidator(
                                "\\Adocuments/(store|lease|operator)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\Z",
                                "The logo must be the path of a stored document, or blank.",
                                code="logo_path",
                            ),
                        ],
                    ),
                ),
                (
                    "processor_name",
                    models.CharField(
                        max_length=100, validators=[ledger.validators.validate_no_payment_data]
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "company settings",
                "verbose_name_plural": "company settings",
                "db_table": "company_settings",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("id", 1)), name="company_settings_singleton"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "time_zone__in",
                                [
                                    "America/New_York",
                                    "America/Chicago",
                                    "America/Denver",
                                    "America/Phoenix",
                                    "America/Los_Angeles",
                                    "America/Anchorage",
                                    "Pacific/Honolulu",
                                ],
                            )
                        ),
                        name="company_settings_time_zone_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("name__regex", "\\S")),
                        name="company_settings_name_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("short_name__regex", "\\S")),
                        name="company_settings_short_name_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("processor_name__regex", "\\S")),
                        name="company_settings_processor_name_not_blank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("logo", ""),
                            (
                                "logo__regex",
                                "\\Adocuments/(store|lease|operator)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\Z",
                            ),
                            _connector="OR",
                        ),
                        name="company_settings_logo_path",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        migrations.RunPython(seed, unseed, elidable=False),
    ]
