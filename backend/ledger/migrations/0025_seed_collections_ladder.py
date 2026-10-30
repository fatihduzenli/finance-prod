"""Seed the collections ladder (docs/qa/PLAN-011b D4).

Three templates and three steps: day 3 ``friendly_reminder``, day 10 ``formal_notice``
and day 20 ``final_notice``. A template or step that already exists is left as it is. The
``admin`` group gets view, add and change on templates and steps (the Django admin edits
them; nothing deletes one); ``billing`` and ``viewer`` get nothing.

Its own migration, so that the reverse deletes the rows in a transaction of its own
before ``0024`` drops the tables (PostgreSQL refuses to drop a table with pending
deferred foreign-key checks in the same transaction). The reverse takes the permissions
back and deletes every step and template; it refuses while an outbound message exists.
"""

from __future__ import annotations

from typing import Any

from django.apps.registry import Apps
from django.contrib.auth.management import create_permissions
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

#: (code, subject, body) of the seeded templates (D4).
TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (
        "friendly_reminder",
        "Friendly reminder: {amount} for {store} is past due",
        "Hello {operator},\n\n"
        "This is a friendly reminder that {amount} for {store} was due on {due_date} and "
        "is now {days_late} days past due.\n\n"
        "You can see your statement here: {statement_link}\n\n"
        "If you have already paid, thank you, and please disregard this message.\n\n"
        "{company}",
    ),
    (
        "formal_notice",
        "Notice: {amount} for {store} is {days_late} days past due",
        "Hello {operator},\n\n"
        "Our records show that {amount} for {store}, due on {due_date}, is still unpaid "
        "{days_late} days later. Please pay it now or contact us to agree a date.\n\n"
        "Your statement: {statement_link}\n\n"
        "{company}",
    ),
    (
        "final_notice",
        "Final notice: {amount} for {store} is {days_late} days past due",
        "Hello {operator},\n\n"
        "This is our final notice: {amount} for {store}, due on {due_date}, is "
        "{days_late} days past due. Please pay it or contact us without delay.\n\n"
        "Your statement: {statement_link}\n\n"
        "{company}",
    ),
)
#: (day offset, template code, position) of the seeded steps (D4).
STEPS: tuple[tuple[int, str, int], ...] = (
    (3, "friendly_reminder", 1),
    (10, "formal_notice", 2),
    (20, "final_notice", 3),
)
MESSAGES_EXIST = "Outbound messages exist; the ladder's seed refuses to reverse."
#: The steps' model, by name: ledger 0061 removed it (PLAN-014 D6), so it exists only in
#: the historical apps.
STEP_MODEL = "CollectionStep"
ADMIN_GROUP = "admin"
ADMIN_PERMISSIONS = tuple(
    (model, f"{action}_{model}")
    for model in ("messagetemplate", "collectionstep")
    for action in ("view", "add", "change")
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
    template_model = apps.get_model("ledger", "MessageTemplate")
    step_model = apps.get_model("ledger", STEP_MODEL)
    templates = {}
    for code, subject, body in TEMPLATES:
        templates[code], _ = template_model.objects.get_or_create(
            code=code, defaults={"subject": subject, "body": body}
        )
    for day_offset, code, position in STEPS:
        step_model.objects.get_or_create(
            day_offset=day_offset, defaults={"template": templates[code], "position": position}
        )
    # Permissions are normally created after migrate finishes; create them now so they
    # can be attached (as 0013_seed_fee_types does).
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
    if apps.get_model("ledger", "OutboundMessage").objects.exists():
        raise RuntimeError(MESSAGES_EXIST)
    group = apps.get_model("auth", "Group").objects.filter(name=ADMIN_GROUP).first()
    if group is not None:
        group.permissions.remove(*_permissions(apps))
    apps.get_model("ledger", STEP_MODEL).objects.all().delete()
    apps.get_model("ledger", "MessageTemplate").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0024_collections_ladder"),
        ("accounts", "0002_role_groups"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(seed, unseed, elidable=False)]
