"""PostgreSQL card-number guard on the provider-side tables (PLAN-004 §4.3, §4.6).

Uses ``finance_reject_payment_data`` from ``ledger.0002_postgresql_guards``. Nothing is
created on any other database vendor; the reverse operation drops the triggers.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

#: Table -> guarded columns (PLAN-004 §4.6 point 1). Not guarded, as identifiers:
#: ``external_refs.external_id``, ``deposits.provider_deposit_id``,
#: ``webhook_events.provider_object_id`` and ``webhook_events.dedupe_hash``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "external_refs": ("entity_type", "provider", "external_kind"),
    "provider_snapshots": ("payload",),
    "deposits": ("provider", "status"),
    "webhook_events": ("event_type", "outcome", "error"),
    "sync_state": ("job_name", "cursor"),
}


def _payment_data_trigger_sql(table: str, columns: tuple[str, ...]) -> str:
    arguments = ", ".join(f"'{column}'" for column in columns)
    return (
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});"
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        schema_editor.execute(_payment_data_trigger_sql(table, columns), params=None)


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_COLUMNS:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
        )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0001_initial"),
        ("ledger", "0002_postgresql_guards"),
    ]

    operations = [
        migrations.RunPython(create_guards, drop_guards, elidable=False),
    ]
