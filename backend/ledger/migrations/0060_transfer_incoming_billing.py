"""The transfer's incoming billing details (docs/qa/PLAN-014 Batch C, C4).

Schema, on ``lease_transfers``: ``incoming_billing`` (JSON, default ``{}``: the billing
details of the incoming lease as typed on the Incoming lease step) and ``incoming_account``
(64 characters, default ``"later"``: ``later``, ``enter`` or ``same:<lease id>``). On confirm
the incoming lease's billing row is written from them; the ended lease's billing and
processor references are not touched. Existing transfers get the defaults (an empty billing
row and Later on confirm).

Both columns are guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), and the
``lease_transfers`` trigger of ``0039_transfer_debt_override`` is recreated with the two columns
among its arguments; the reverse restores ``0039``'s columns. Nothing is created on other
vendors; the application validators apply there.

The reverse refuses while a transfer holds anything the new columns keep (billing details or
an account other than Later).
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

import ledger.validators

TRANSFERS = "lease_transfers"
_TRANSFERS_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0039_transfer_debt_override"
).GUARDED_COLUMNS[TRANSFERS]
#: Table -> guarded columns after this migration: ``lease_transfers`` replaces ``0039``'s
#: entry.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TRANSFERS: (*_TRANSFERS_BEFORE, "incoming_billing", "incoming_account"),
}
LATER = "later"
CANNOT_REVERSE = (
    "Lease transfers with incoming billing details exist; this migration refuses to reverse."
)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def _replace_trigger(
    schema_editor: BaseDatabaseSchemaEditor, table: str, columns: tuple[str, ...]
) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        _replace_trigger(schema_editor, table, columns)


def restore_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, TRANSFERS, _TRANSFERS_BEFORE)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    kept = ~Q(incoming_billing={}) | ~Q(incoming_account=LATER)
    if apps.get_model("ledger", "LeaseTransfer").objects.filter(kept).exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0059_lease_billing"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_billing",
            field=models.JSONField(
                blank=True,
                default=dict,
                validators=[ledger.validators.validate_json_has_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_account",
            field=models.CharField(
                blank=True,
                default=LATER,
                max_length=64,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.RunPython(create_guards, restore_guards, elidable=False),
        # --- The reverse starts here: it refuses before anything is dropped.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
