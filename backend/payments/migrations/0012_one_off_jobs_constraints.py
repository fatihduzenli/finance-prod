"""One-off charge jobs, the constraints (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md
D1).

The last of the three steps of ``0010_one_off_jobs``; it updates no row. It adds the
CHECK constraints of the new closed lists of kinds and entity types, which need the rows
of ``0011_one_off_jobs_rows``. The reverse removes them.
"""

from __future__ import annotations

import importlib

from django.db import migrations, models

_SCHEMA = importlib.import_module("payments.migrations.0010_one_off_jobs")

KINDS: list[str] = _SCHEMA.KINDS
ENTITIES: list[str] = _SCHEMA.ENTITIES


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0011_one_off_jobs_rows"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", KINDS)), name="jobs_kind_valid"
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(("entity_type__in", ENTITIES)), name="jobs_entity_type_valid"
            ),
        ),
    ]
