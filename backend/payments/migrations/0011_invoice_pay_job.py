"""The ``invoice_pay`` job kind (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D4):
"Charge now", the single pay-an-invoice attempt of a one-off charge, run by the worker.
Only the closed list of the kind CHECK changes; no column or trigger. The reverse
refuses while a job of the new kind exists.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_BEFORE = importlib.import_module("payments.migrations.0010_one_off_jobs")

NEW_KIND = "invoice_pay"
KINDS = [*_BEFORE.KINDS, NEW_KIND]
CANNOT_REVERSE = "Jobs of the invoice_pay kind exist; this migration refuses to reverse."


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return None


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    if job.objects.filter(kind=NEW_KIND).exists():
        raise RuntimeError(CANNOT_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0010_one_off_jobs"),
    ]

    # The check runs last forwards, so it runs first in reverse, before the old CHECK
    # is put back.
    operations = [
        migrations.RemoveConstraint(model_name="job", name="jobs_kind_valid"),
        migrations.AlterField(
            model_name="job",
            name="kind",
            field=models.CharField(
                choices=[
                    ("operator_sync", "Operator sync"),
                    ("operator_retry", "Operator sync retry"),
                    ("operator_link", "Operator link"),
                    ("autopay_enable", "Enable autopay"),
                    ("autopay_pause", "Pause autopay"),
                    ("autopay_resume", "Resume autopay"),
                    ("payment_mark", "Mark the provider invoice paid"),
                    ("payment_retry_mark", "Retry marking the provider invoice"),
                    ("payment_refund", "Refund"),
                    ("payment_void", "Void"),
                    ("operator_methods_refresh", "Refresh payment methods"),
                    ("run_match", "Match invoices"),
                    ("webhook_replay", "Replay webhook event"),
                    ("reconcile_run", "Run reconciliation"),
                    (
                        "one_off_payment_mark",
                        "Mark the provider invoice of a one-off charge paid",
                    ),
                    (
                        "one_off_payment_retry_mark",
                        "Retry marking the provider invoice of a one-off charge",
                    ),
                    ("invoice_create", "Create the provider invoice"),
                    ("invoice_send", "Send the provider invoice by email"),
                    ("invoice_delete", "Delete the provider invoice"),
                    ("schedule_create", "Create the provider schedule"),
                    ("schedule_delete", "Delete the provider schedule"),
                    ("invoice_pay", "Charge the provider invoice now"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", KINDS)), name="jobs_kind_valid"
            ),
        ),
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
