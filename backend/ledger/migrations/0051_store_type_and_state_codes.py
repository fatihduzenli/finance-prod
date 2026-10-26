"""Store type and state codes (docs/qa/PLAN-012f-b Batch B, D10 and D11).

Schema: the table ``store_types`` (``name``, unique and not blank; ``sort_order``;
``active``) and, on ``stores``, ``store_type_id`` (null; a foreign key that refuses to
delete a type in use). On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in
either direction, and the new text column (``store_types.name``) gets the payment-data
trigger of ``0002_postgresql_guards``.

Data: the five types are seeded in this order: Inline store, Kiosk, Cart, Pop-up,
Warehouse, and the ``admin`` group gets view, add and change on them (the Django admin;
never delete, as for the fee catalogue in ``0013``). Each store's free-text ``state``
becomes its two-letter code: the value, stripped, matched without regard to case on a code
or a full name of :data:`finance.us_states.US_STATES` (the 50 states and DC, a fixed
list); anything else becomes "" with the raw value kept in an ``audit_log`` row
(``store.state_unmapped``, before ``{"state": raw}``), and the store is listed under
"State needs choosing".

The reverse writes the full names back from the codes (a value that was unmapped stays
"", its raw value is in the audit row), deletes the seeded types no store uses, and drops
the column and the table; the group's permissions are taken back.
"""

from __future__ import annotations

import uuid
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.contrib.auth.management import create_permissions
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators
from finance.us_states import US_STATES

#: Table -> guarded columns created by this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {"store_types": ("name",)}
#: The seeded types, in their order (``sort_order`` 10, 20, ...).
SEEDED_TYPES: tuple[str, ...] = ("Inline store", "Kiosk", "Cart", "Pop-up", "Warehouse")
UNMAPPED_ACTION = "store.state_unmapped"
ADMIN_GROUP = "admin"
#: (model_name, codename) of each permission granted to the admin group.
ADMIN_PERMISSIONS = tuple(
    ("storetype", f"{action}_storetype") for action in ("view", "add", "change")
)
_BY_KEY: dict[str, str] = {
    **{code.casefold(): code for code, _ in US_STATES},
    **{name.casefold(): code for code, name in US_STATES},
}
_NAMES: dict[str, str] = dict(US_STATES)


def state_code(raw: str) -> str:
    """The two-letter code of a free-text state ("Texas", "tx", " TX " are "TX"); "" when
    it matches no code and no full name."""
    return _BY_KEY.get(" ".join(raw.split()).casefold(), "")


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


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


def seed_types(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    store_type = apps.get_model("ledger", "StoreType")
    for index, name in enumerate(SEEDED_TYPES, start=1):
        store_type.objects.get_or_create(name=name, defaults={"sort_order": index * 10})
    # Permissions are normally created after migrate finishes; create them now so they
    # can be attached (as 0045_company_settings does).
    app_config = apps.get_app_config("ledger")
    app_config.models_module = True  # type: ignore[assignment]
    create_permissions(app_config, verbosity=0, apps=apps)
    app_config.models_module = None
    permissions = _permissions(apps)
    if len(permissions) != len(ADMIN_PERMISSIONS):
        raise RuntimeError("A ledger permission is missing.")
    group, _ = apps.get_model("auth", "Group").objects.get_or_create(name=ADMIN_GROUP)
    group.permissions.add(*permissions)


def unseed_types(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    group = apps.get_model("auth", "Group").objects.filter(name=ADMIN_GROUP).first()
    if group is not None:
        group.permissions.remove(*_permissions(apps))
    store_type = apps.get_model("ledger", "StoreType")
    store_type.objects.filter(name__in=SEEDED_TYPES, stores__isnull=True).delete()


def map_states(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    store = apps.get_model("ledger", "Store")
    audit_log = apps.get_model("ledger", "AuditLog")
    for pk, raw in list(store.objects.exclude(state="").values_list("pk", "state")):
        code = state_code(raw)
        if code == raw:
            continue
        store.objects.filter(pk=pk).update(state=code)
        if not code:
            audit_log.objects.create(
                actor=None,
                action=UNMAPPED_ACTION,
                entity_type="store",
                entity_id=str(pk),
                before={"state": raw},
                after={"state": ""},
            )


def unmap_states(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    store = apps.get_model("ledger", "Store")
    for code, name in _NAMES.items():
        store.objects.filter(state=code).update(state=name)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0050_rename_statement_saved_at"),
        ("accounts", "0002_role_groups"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.CreateModel(
            name="StoreType",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=200,
                        unique=True,
                        validators=[ledger.validators.validate_no_payment_data],
                    ),
                ),
                ("sort_order", models.SmallIntegerField(default=0)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "store_types",
                "ordering": ("sort_order", "name"),
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("name__regex", "\\S")),
                        name="store_types_name_not_blank",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="store",
            name="store_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stores",
                to="ledger.storetype",
            ),
        ),
        # On the way back, the rows the reverse rewrote are checked before the ALTER TABLE.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        migrations.RunPython(seed_types, unseed_types, elidable=False),
        migrations.RunPython(map_states, unmap_states, elidable=False),
    ]
