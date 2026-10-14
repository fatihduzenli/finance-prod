"""A lease-end balance allowed to remain after a transfer
(docs/qa/PLAN-012e-lease-end-transfer-installments.md D14).

Schema, on ``lease_transfers``: ``debt_override_amount`` (money, null),
``debt_override_reason`` (guarded free text, blank), ``debt_override_by`` (the admin,
null) and ``debt_override_at`` (null). A CHECK holds the four together: none set, or the
moment, the amount and a reason that is not blank. Existing rows have none set.

Free text is guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in
either direction, and the ``lease_transfers`` trigger of ``0037`` is recreated with
``debt_override_reason`` among its arguments; the reverse restores the columns of
``0037``. Nothing is created on other vendors; the application validator applies there.

The reverse drops the four columns (an allowance's reason stays in the audit log).
"""

from __future__ import annotations

import importlib

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

import ledger.models
import ledger.validators

TABLE = "lease_transfers"
_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0037_transfer_fields"
).GUARDED_COLUMNS[TABLE]

#: Table -> guarded columns after this migration. Replaces the ``lease_transfers`` entry
#: of ``0037``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: (*_BEFORE, "debt_override_reason"),
}


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {TABLE}_payment_data_guard ON {TABLE};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {TABLE}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS[TABLE])


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _BEFORE)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0038_installments"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.AddField(
            model_name="leasetransfer",
            name="debt_override_amount",
            field=ledger.models.MoneyField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="debt_override_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="debt_override_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="debt_override_reason",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.CheckConstraint(
                condition=Q(
                    debt_override_at__isnull=True,
                    debt_override_amount__isnull=True,
                    debt_override_by__isnull=True,
                    debt_override_reason="",
                )
                | Q(
                    debt_override_at__isnull=False,
                    debt_override_amount__isnull=False,
                    debt_override_reason__regex="\\S",
                ),
                name="lease_transfers_debt_override_complete",
            ),
        ),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
        # Reversed first: any check queued runs before the ALTER TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
    ]
