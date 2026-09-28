"""Create the three role groups and give the admin group user-management permissions.

Roles are group membership (accounts.roles). Only the admin group gets Django model
permissions here: user, group, OTP device and session management, which the role
registry already restricts to admin. Billing and viewer get their permissions with the
models they will use (Steps 4+).
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.contrib.auth.management import create_permissions
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

ROLE_GROUP_NAMES = ("admin", "billing", "viewer")

#: (app_label, model_name) whose every permission the admin group receives.
ADMIN_MODELS = (
    ("auth", "user"),
    ("auth", "group"),
    ("otp_totp", "totpdevice"),
    ("otp_static", "staticdevice"),
    ("otp_static", "statictoken"),
    ("accounts", "usersession"),
)


def create_role_groups(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    # Permissions are normally created after migrate finishes; create them now so they
    # can be attached. The models_module flag is the documented way to make a
    # historical app config eligible.
    for label in {app_label for app_label, _ in ADMIN_MODELS}:
        app_config = apps.get_app_config(label)
        app_config.models_module = True  # type: ignore[assignment]
        create_permissions(app_config, verbosity=0, apps=apps)
        app_config.models_module = None

    group_model = apps.get_model("auth", "Group")
    permission_model = apps.get_model("auth", "Permission")
    for name in ROLE_GROUP_NAMES:
        group_model.objects.get_or_create(name=name)

    admin_group = group_model.objects.get(name="admin")
    for app_label, model_name in ADMIN_MODELS:
        permissions = permission_model.objects.filter(
            content_type__app_label=app_label, content_type__model=model_name
        )
        admin_group.permissions.add(*permissions)


def delete_role_groups(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("auth", "Group").objects.filter(name__in=ROLE_GROUP_NAMES).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("otp_totp", "0003_add_timestamps"),
        ("otp_static", "0003_add_timestamps"),
    ]

    operations = [migrations.RunPython(create_role_groups, delete_role_groups)]
