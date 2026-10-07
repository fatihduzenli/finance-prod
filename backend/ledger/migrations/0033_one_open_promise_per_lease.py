"""One open promise per lease (docs/qa/PLAN-012c-b-promise-outcomes.md D3). Data only.

For every (operator, lease) bucket (no lease is a bucket of its own) with more than one
open promise not deleted, the most recently recorded (``created_at``, then ``id``) stays
open and each other becomes ``rescheduled``. ``closed_by`` is one to one, so each closed
promise names the next newer promise of its bucket (a chain up to the one kept open),
unless that promise already closes another one: then ``closed_by`` stays null. Each change
writes an audit row (actor null, ``contact_entry.outcome_recorded``, ``"migration":
"0033"``), as the service's automatic close does. A second run finds one open promise per
bucket and writes nothing. The reverse is a no-op: the rows keep their history.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def _state(row: Any) -> dict[str, object]:
    return {
        "status": row.status,
        "closed_by": str(row.closed_by_id) if row.closed_by_id else None,
        "kept_by_payment": str(row.kept_by_payment_id) if row.kept_by_payment_id else None,
    }


def close_superseded(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    entries = apps.get_model("ledger", "ContactEntry")
    audit = apps.get_model("ledger", "AuditLog")
    open_rows = entries.objects.filter(kind="promise", status="open", deleted_at__isnull=True)
    buckets: dict[tuple[object, object], list[Any]] = {}
    for row in open_rows.order_by("-created_at", "-id"):
        buckets.setdefault((row.operator_id, row.lease_id), []).append(row)
    for rows in buckets.values():
        for newer, row in pairwise(rows):
            closer = None if entries.objects.filter(closed_by=newer).exists() else newer
            before = _state(row)
            row.status = "rescheduled"
            row.closed_by = closer
            row.save(update_fields=["status", "closed_by"])
            audit.objects.create(
                actor=None,
                action="contact_entry.outcome_recorded",
                entity_type="contact_entry",
                entity_id=str(row.pk),
                before=before,
                after={**_state(row), "migration": "0033"},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0032_builder_item"),
    ]

    operations = [
        migrations.RunPython(close_superseded, migrations.RunPython.noop),
    ]
