"""The switch to a lease's own account (docs/qa/PLAN-014-per-lease-billing-messaging-portal-
deposits.md C9).

Kind ``lease_account_switch`` (a lease that shares another lease's account moves to the bank
method of its own processor customer; ``ledger.services.leases.switch_to_own_account``),
entity ``lease``. Only the closed list of the ``jobs_kind_valid`` CHECK and the field's
choices change; no column, trigger or row. The reverse refuses while a job of the new kind
exists.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_BEFORE = importlib.import_module("payments.migrations.0019_lease_job_kinds")

NEW_KINDS: tuple[str, ...] = ("lease_account_switch",)
KINDS = [*_BEFORE.KINDS, *NEW_KINDS]
CANNOT_REVERSE = "Switch-to-own-account jobs exist; this migration refuses to reverse."


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return None


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    if job.objects.filter(kind__in=NEW_KINDS).exists():
        raise RuntimeError(CANNOT_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0019_lease_job_kinds"),
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
                    ("lease_sync", "Updating the lease"),
                    ("lease_retry", "Updating the lease again"),
                    ("lease_link", "Connecting the lease"),
                    ("autopay_enable", "Setting up autopay"),
                    ("autopay_pause", "Pausing autopay"),
                    ("autopay_resume", "Resuming autopay"),
                    ("payment_mark", "Marking the invoice paid"),
                    ("payment_retry_mark", "Marking the invoice paid again"),
                    ("payment_refund", "Refunding the payment"),
                    ("payment_void", "Voiding the payment"),
                    ("lease_methods_refresh", "Updating bank methods"),
                    ("run_match", "Updating invoices"),
                    ("webhook_replay", "Processing a message again"),
                    ("reconcile_run", "Running the nightly check"),
                    ("one_off_payment_mark", "Marking the one-off charge's invoice paid"),
                    (
                        "one_off_payment_retry_mark",
                        "Marking the one-off charge's invoice paid again",
                    ),
                    ("invoice_create", "Creating the invoice"),
                    ("invoice_send", "Sending the invoice by e-mail"),
                    ("invoice_delete", "Cancelling the invoice"),
                    ("schedule_create", "Setting up autopay"),
                    ("schedule_delete", "Cancelling autopay"),
                    ("invoice_pay", "Charging the invoice"),
                    ("message_send", "Sending a collection e-mail"),
                    ("calendar_email", "Sending a morning e-mail"),
                    ("disputes_sync", "Updating disputes"),
                    ("dispute_evidence_upload", "Sending dispute evidence"),
                    ("lease_end_documents", "Making the final statement and its e-mail"),
                    ("autopay_stop", "Turning off autopay"),
                    ("lease_method_attach", "Connecting a bank account"),
                    ("lease_account_switch", "Switching to the lease's own account"),
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
