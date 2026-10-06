"""One-off charges, the rows (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D1).

The second of the three steps of the rename (see ``0017_one_off_charges``); it updates
rows and changes no column, constraint or index.

Existing rows: every charge gets ``operator`` from its lease, ``category='setup'``,
``collection='none'`` and its fee type's name as ``description``; every ``setup``
payment becomes ``one_off``; ``audit_log`` rows of entity type ``setup_charge`` and
actions ``setup_charge.*`` are rewritten to ``one_off_charge`` (and the
``setup_charge`` key of their ``before``/``after`` with them). The reverse puts the
values back, and refuses while a charge exists that the old schema cannot hold (a
category other than ``setup``, no lease, or voided).

On PostgreSQL the append-only trigger of ``audit_log`` is disabled for the rewrite and
enabled again in this same transaction, so a failure rolls both back and the log is
never left writable. The disable runs before any row is updated. Updating an
``audit_log`` row that has an actor queues the deferred foreign-key check of that row
(a row already updated in this transaction is always checked), and PostgreSQL refuses
the ``ALTER TABLE`` of the enable while such a check is pending; so
``SET CONSTRAINTS ALL IMMEDIATE`` runs the pending checks right before it.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Value
from django.db.models.functions import Concat, Substr

OLD_ENTITY = "setup_charge"
NEW_ENTITY = "one_off_charge"
OLD_KIND = "setup"
NEW_KIND = "one_off"
CANNOT_REVERSE = (
    "one_off_charges holds charges the setup_charges schema cannot hold (a category other "
    "than setup, or no lease); this migration refuses to reverse over them."
)


def _set_audit_append_only(schema_editor: BaseDatabaseSchemaEditor, enabled: bool) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    if enabled:
        # Run the deferred checks the updates queued; the ALTER is refused otherwise.
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)
    state = "ENABLE" if enabled else "DISABLE"
    schema_editor.execute(
        f"ALTER TABLE audit_log {state} TRIGGER audit_log_append_only;", params=None
    )


def _rename_key(payload: object, old: str, new: str) -> object:
    if isinstance(payload, dict) and old in payload:
        return {(new if key == old else key): value for key, value in payload.items()}
    return payload


def _rewrite_audit(apps: Apps, old: str, new: str) -> None:
    audit_log = apps.get_model("ledger", "AuditLog")
    prefix = f"{old}."
    audit_log.objects.filter(entity_type=old).update(entity_type=new)
    audit_log.objects.filter(action__startswith=prefix).update(
        action=Concat(Value(f"{new}."), Substr("action", len(prefix) + 1))
    )
    changed = []
    for row in audit_log.objects.filter(
        models.Q(before__has_key=old) | models.Q(after__has_key=old)
    ).iterator():
        row.before = _rename_key(row.before, old, new)
        row.after = _rename_key(row.after, old, new)
        changed.append(row)
    audit_log.objects.bulk_update(changed, ["before", "after"], batch_size=500)


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    charge_model = apps.get_model("ledger", "OneOffCharge")
    payment_model = apps.get_model("ledger", "Payment")
    # The migration is atomic: on a failure the rollback restores the trigger too.
    _set_audit_append_only(schema_editor, False)
    rows = list(charge_model.objects.select_related("lease", "fee_type"))
    for row in rows:
        # Both were required before this migration.
        row.operator_id = row.lease.operator_id if row.lease is not None else None
        row.category = "setup"
        row.collection = "none"
        row.description = row.fee_type.name[:120] if row.fee_type is not None else ""
    charge_model.objects.bulk_update(
        rows, ["operator", "category", "collection", "description"], batch_size=500
    )
    payment_model.objects.filter(kind=OLD_KIND).update(kind=NEW_KIND)
    _rewrite_audit(apps, OLD_ENTITY, NEW_ENTITY)
    _set_audit_append_only(schema_editor, True)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    charge_model = apps.get_model("ledger", "OneOffCharge")
    payment_model = apps.get_model("ledger", "Payment")
    held = charge_model.objects.filter(
        models.Q(lease__isnull=True) | ~models.Q(category="setup") | models.Q(status="voided")
    )
    if held.exists():
        raise RuntimeError(CANNOT_REVERSE)
    _set_audit_append_only(schema_editor, False)
    payment_model.objects.filter(kind=NEW_KIND).update(kind=OLD_KIND)
    _rewrite_audit(apps, NEW_ENTITY, OLD_ENTITY)
    _set_audit_append_only(schema_editor, True)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0017_one_off_charges"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
