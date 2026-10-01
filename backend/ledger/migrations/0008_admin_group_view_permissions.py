"""Give the ``admin`` group ``view`` permission on stores, operators and leases
(docs/qa/PLAN-007b-stores.md §1).

The three models are registered read-only in the Django admin; this lets an admin-group
user who is not a superuser open their lists. ``billing`` and ``viewer`` get nothing.
The reverse removes exactly these three permissions from the group.
"""

from __future__ import annotations

from typing import Any

from django.apps.registry import Apps
from django.contrib.auth.management import create_permissions
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

ADMIN_GROUP = "admin"
#: (model_name, codename) of each permission granted.
VIEW_PERMISSIONS = (
    ("store", "view_store"),
    ("operator", "view_operator"),
    ("lease", "view_lease"),
)


def _permissions(apps: Apps) -> list[Any]:
    permission_model = apps.get_model("auth", "Permission")
    return [
        permission
        for model, codename in VIEW_PERMISSIONS
        for permission in permission_model.objects.filter(
            content_type__app_label="ledger", content_type__model=model, codename=codename
        )
    ]


def grant_view_permissions(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    # Permissions are normally created after migrate finishes; create them now so they
    # can be attached (as accounts.0002_role_groups does).
    app_config = apps.get_app_config("ledger")
    app_config.models_module = True  # type: ignore[assignment]
    create_permissions(app_config, verbosity=0, apps=apps)
    app_config.models_module = None
    permissions = _permissions(apps)
    if len(permissions) != len(VIEW_PERMISSIONS):
        raise RuntimeError("A ledger view permission is missing.")
    group, _ = apps.get_model("auth", "Group").objects.get_or_create(name=ADMIN_GROUP)
    group.permissions.add(*permissions)


def revoke_view_permissions(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    group = apps.get_model("auth", "Group").objects.filter(name=ADMIN_GROUP).first()
    if group is not None:
        group.permissions.remove(*_permissions(apps))


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0007_collection_schedule_needs_review"),
        ("accounts", "0002_role_groups"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(grant_view_permissions, revoke_view_permissions)]
