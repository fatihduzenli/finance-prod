"""Indexes for the audit log page (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D8):
its time range, and the entity filter and the Activity lists of the Documents sections.
No row changes.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0020_guard_bidi_marks"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["at"], name="audit_log_at_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["entity_type", "entity_id", "at"], name="audit_log_entity_idx"
            ),
        ),
    ]
