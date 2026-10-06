"""One-off charge jobs, the rows (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D1).

The second of the three steps of ``0010_one_off_jobs``; it updates rows and changes no
column, constraint or index. Existing job rows of the renamed kinds and of the entity
type ``setup_charge`` are rewritten. The reverse rewrites them back and refuses while a
job of a new kind exists.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_SCHEMA = importlib.import_module("payments.migrations.0010_one_off_jobs")

RENAMED_KINDS: dict[str, str] = _SCHEMA.RENAMED_KINDS
NEW_KINDS: tuple[str, ...] = _SCHEMA.NEW_KINDS
OLD_ENTITY: str = _SCHEMA.OLD_ENTITY
NEW_ENTITY: str = _SCHEMA.NEW_ENTITY
CANNOT_REVERSE = "Jobs of the one-off invoice kinds exist; this migration refuses to reverse."


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    for old, new in RENAMED_KINDS.items():
        job.objects.filter(kind=old).update(kind=new)
    job.objects.filter(entity_type=OLD_ENTITY).update(entity_type=NEW_ENTITY)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    if job.objects.filter(kind__in=NEW_KINDS).exists():
        raise RuntimeError(CANNOT_REVERSE)
    for old, new in RENAMED_KINDS.items():
        job.objects.filter(kind=new).update(kind=old)
    job.objects.filter(entity_type=NEW_ENTITY).update(entity_type=OLD_ENTITY)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0010_one_off_jobs"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
