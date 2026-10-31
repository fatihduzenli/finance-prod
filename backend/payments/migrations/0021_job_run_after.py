"""The send window of a job (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md
D7).

Schema, on ``jobs``: ``run_after`` (timestamp, null) and the index ``(status, run_after)``.
:func:`payments.jobs.claim_next` leaves a queued job alone until ``run_after`` has passed; a
null ``run_after`` (every existing row) runs at once, as before. No trigger changes: the
column holds no text.

The reverse drops the index and the column; a job waiting for its time then runs at the next
claim.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0020_lease_account_switch"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="run_after",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(fields=["status", "run_after"], name="jobs_status_run_after_idx"),
        ),
    ]
