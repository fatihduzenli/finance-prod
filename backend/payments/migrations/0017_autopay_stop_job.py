"""The stop-autopay job (docs/qa/PLAN-012e-lease-end-transfer-installments.md D5
amendment).

Kind ``autopay_stop`` (delete every rent and insurance schedule of a lease active or
paused at the payment provider), entity ``lease``. Only the closed list of the kind CHECK
constraint changes; no column or trigger, no row. The reverse refuses while a job of the
new kind exists.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_BEFORE = importlib.import_module("payments.migrations.0016_lease_end_jobs")

NEW_KINDS: tuple[str, ...] = ("autopay_stop",)
KINDS = [*_BEFORE.KINDS, *NEW_KINDS]
CANNOT_REVERSE = "Stop-autopay jobs exist; this migration refuses to reverse."


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return None


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    if job.objects.filter(kind__in=NEW_KINDS).exists():
        raise RuntimeError(CANNOT_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0016_lease_end_jobs"),
    ]

    # The check runs last forwards, so it runs first in reverse, before the old CHECK
    # constraint is put back.
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
                    ("message_send", "Send a collection e-mail"),
                    ("calendar_email", "Send a morning e-mail"),
                    ("disputes_sync", "Read the disputes"),
                    ("dispute_evidence_upload", "Send dispute evidence"),
                    ("lease_end_documents", "Make the final statement and its e-mail"),
                    ("autopay_stop", "Stop autopay"),
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
