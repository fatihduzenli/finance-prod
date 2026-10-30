"""Brands and store notes (docs/qa/PLAN-014 Batch B, B1 and B5).

Schema: the table ``store_types`` becomes ``brands`` (model ``StoreType`` becomes ``Brand``,
its rows kept) and ``stores.store_type_id`` becomes ``stores.brand_id`` (null; the foreign
key still refuses to delete a brand in use). The not-blank CHECK on the name is renamed
with its table (``brands_name_not_blank``) and, on PostgreSQL, so is the payment-data
trigger of ``0051`` (``brands_payment_data_guard``, the same column). On PostgreSQL ``SET
CONSTRAINTS ALL IMMEDIATE`` runs any check this transaction queued before the ``ALTER TABLE``
statements (the lesson of ``0018``), in either direction.

Data (``seed_brands``): the three brands **iFixandRepair**, **Techy** and **Talk&Fix** are
created (or, when a row of that name exists already, whatever its case, updated) active with
``sort_order`` 1 to 3; every other row (0051's Inline store, Kiosk, Cart, Pop-up and Warehouse
and any an admin added) is made inactive. Every store whose brand is now inactive gets no
brand and ``needs_review``, with one ``audit_log`` row ``store.brand_cleared`` (actor none;
``before`` the old brand's name and the store's ``needs_review`` until then, ``after`` no brand
and ``needs_review`` true): the attention kind ``brand_needed`` lists it until a brand is
chosen, and saving a brand clears the flag only when the brand was its one reason.

Notes (B5), on ``notes``: ``lease_id`` (null; a foreign key that refuses to delete a lease a
note names), ``pinned`` (false) and ``pinned_at`` (null; both or neither, CHECK
``notes_pinned_at_matches``), ``updated_at`` (now for the existing rows), ``edited_at``, and the
soft delete of ``0028``'s contact entries: ``deleted_at``, ``deleted_by`` (FK ``auth_user``,
PROTECT) and ``delete_reason`` (guarded text, "" by default; all three or none, CHECK
``notes_deleted_all_or_none``). Adding the columns fills every existing row with false, nulls
and "", which is the whole backfill. On PostgreSQL the notes trigger of
``0002_postgresql_guards`` is recreated with ``delete_reason`` among its arguments; the reverse
restores the columns of ``0002``.

The reverse drops the note columns (the pins, edits and soft deletes made since are lost; the
audit rows ``note.*`` keep them) and renames everything back. Its data step first refuses
(``CANNOT_REVERSE``, the pattern of ``0018``) when any ``store.brand_cleared`` row exists: the
cleared stores' brands cannot be put back. Otherwise (no store was cleared) it makes every
other row active again, except one an admin made inactive in the Django admin (its last
``store_type.*_in_admin`` row says so), and makes the three inactive when this step created
them (no admin row created them).
"""

from __future__ import annotations

import importlib
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

#: The brands this step seeds, in their order (``sort_order`` 1, 2, 3).
SEEDED_BRANDS: tuple[str, ...] = ("iFixandRepair", "Techy", "Talk&Fix")
CLEARED_ACTION = "store.brand_cleared"
ENTITY_STORE = "store"
#: The entity of the Django admin's audit rows on the kinds of store (``StoreTypeAdmin``).
ADMIN_ENTITY = "store_type"
CANNOT_REVERSE = (
    "Stores lost their brand to this migration (audit_log store.brand_cleared); the brands "
    "cannot be restored, so this migration refuses to reverse."
)

OLD_TABLE = "store_types"
NEW_TABLE = "brands"
NOTES = "notes"
_NOTES_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0002_postgresql_guards"
).GUARDED_COLUMNS[NOTES]
#: Table -> guarded columns after this migration: the brands' name keeps the trigger 0051
#: created on ``store_types``, renamed with its table; the notes entry replaces ``0002``'s.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    NEW_TABLE: ("name",),
    NOTES: (*_NOTES_BEFORE, "delete_reason"),
}


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def _rename_trigger(
    schema_editor: BaseDatabaseSchemaEditor, table: str, old: str, new: str
) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        f"ALTER TRIGGER {old}_payment_data_guard ON {table} RENAME TO {new}_payment_data_guard;",
        params=None,
    )


def rename_trigger(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _rename_trigger(schema_editor, NEW_TABLE, OLD_TABLE, NEW_TABLE)


def unrename_trigger(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    # Runs before the table is renamed back: the trigger is still on ``brands``.
    _rename_trigger(schema_editor, NEW_TABLE, NEW_TABLE, OLD_TABLE)


def _replace_notes_trigger(
    schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]
) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {NOTES}_payment_data_guard ON {NOTES};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {NOTES}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {NOTES} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_notes(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _replace_notes_trigger(schema_editor, GUARDED_COLUMNS[NOTES])


def unguard_notes(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _replace_notes_trigger(schema_editor, _NOTES_BEFORE)


def seed_brands(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    brand = apps.get_model("ledger", "Brand")
    store = apps.get_model("ledger", "Store")
    audit_log = apps.get_model("ledger", "AuditLog")
    seeded = []
    for index, name in enumerate(SEEDED_BRANDS, start=1):
        row = brand.objects.filter(name__iexact=name).order_by("created_at", "id").first()
        if row is None:
            row = brand.objects.create(name=name, sort_order=index, active=True)
        else:
            brand.objects.filter(pk=row.pk).update(name=name, sort_order=index, active=True)
        seeded.append(row.pk)
    brand.objects.exclude(pk__in=seeded).update(active=False)
    cleared = (
        store.objects.filter(brand__active=False)
        .order_by("store_number", "external_label", "id")
        .values_list("pk", "brand__name", "needs_review")
    )
    for pk, brand_name, needs_review in list(cleared):
        audit_log.objects.create(
            actor=None,
            action=CLEARED_ACTION,
            entity_type=ENTITY_STORE,
            entity_id=str(pk),
            before={"brand": brand_name, "needs_review": needs_review},
            after={"brand": None, "needs_review": True},
        )
        store.objects.filter(pk=pk).update(brand=None, needs_review=True)


def _made_inactive_in_admin(audit_log: Any, pk: object) -> bool:
    """Whether the Django admin's last save of this kind of store left it inactive."""
    last = (
        audit_log.objects.filter(entity_type=ADMIN_ENTITY, entity_id=str(pk))
        .order_by("-at", "-id")
        .first()
    )
    return last is not None and isinstance(last.after, dict) and last.after.get("active") is False


def unseed_brands(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    brand = apps.get_model("ledger", "Brand")
    audit_log = apps.get_model("ledger", "AuditLog")
    if audit_log.objects.filter(action=CLEARED_ACTION, entity_type=ENTITY_STORE).exists():
        raise RuntimeError(CANNOT_REVERSE)
    for row in list(brand.objects.all()):
        if row.name in SEEDED_BRANDS:
            created_in_admin = audit_log.objects.filter(
                entity_type=ADMIN_ENTITY,
                entity_id=str(row.pk),
                action=f"{ADMIN_ENTITY}.created_in_admin",
            ).exists()
            if not created_in_admin:
                brand.objects.filter(pk=row.pk).update(active=False)
        elif not _made_inactive_in_admin(audit_log, row.pk):
            brand.objects.filter(pk=row.pk).update(active=True)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0057_payment_void_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(model_name="storetype", name="store_types_name_not_blank"),
        migrations.RenameModel(old_name="StoreType", new_name="Brand"),
        migrations.AlterModelTable(name="brand", table=NEW_TABLE),
        migrations.AlterModelOptions(
            name="brand",
            options={
                "ordering": ("sort_order", "name"),
                "verbose_name": "Brand",
                "verbose_name_plural": "Brands",
            },
        ),
        migrations.AddConstraint(
            model_name="brand",
            constraint=models.CheckConstraint(
                condition=models.Q(("name__regex", "\\S")), name="brands_name_not_blank"
            ),
        ),
        migrations.RenameField(model_name="store", old_name="store_type", new_name="brand"),
        migrations.AlterField(
            model_name="store",
            name="brand",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="stores",
                to="ledger.brand",
                verbose_name="Brand",
            ),
        ),
        migrations.RunPython(rename_trigger, unrename_trigger, elidable=False),
        # PLAN-014 B5: the store notes.
        migrations.AddField(
            model_name="note",
            name="lease",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="notes",
                to="ledger.lease",
            ),
        ),
        migrations.AddField(
            model_name="note", name="pinned", field=models.BooleanField(default=False)
        ),
        migrations.AddField(
            model_name="note",
            name="pinned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="note", name="updated_at", field=models.DateTimeField(auto_now=True)
        ),
        migrations.AddField(
            model_name="note",
            name="edited_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="note",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="note",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="note",
            name="delete_reason",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddConstraint(
            model_name="note",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("pinned", False), ("pinned_at__isnull", True)),
                    models.Q(("pinned", True), ("pinned_at__isnull", False)),
                    _connector="OR",
                ),
                name="notes_pinned_at_matches",
            ),
        ),
        migrations.AddConstraint(
            model_name="note",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("delete_reason", ""),
                        ("deleted_at__isnull", True),
                        ("deleted_by__isnull", True),
                    ),
                    models.Q(
                        ("deleted_at__isnull", False),
                        ("deleted_by__isnull", False),
                        ("delete_reason__regex", "\\S"),
                    ),
                    _connector="OR",
                ),
                name="notes_deleted_all_or_none",
            ),
        ),
        migrations.RunPython(guard_notes, unguard_notes, elidable=False),
        # Reversed after the data step: the rows it rewrote are checked before the ALTER
        # TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
        migrations.RunPython(seed_brands, unseed_brands, elidable=False),
    ]
