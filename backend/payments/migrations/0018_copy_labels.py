"""The on-screen labels of the job kinds, the job states and the nightly check's findings
(docs/qa/PLAN-012c-design-pass.md, copy list §4.5).

Only the ``choices`` labels change: every stored value, column, CHECK and trigger is as it
was, and no row is touched (``sqlmigrate`` prints no SQL).
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0017_autopay_stop_job"),
    ]

    operations = [
        migrations.AlterField(
            model_name="job",
            name="kind",
            field=models.CharField(
                choices=[
                    ("operator_sync", "Updating the operator"),
                    ("operator_retry", "Updating the operator again"),
                    ("operator_link", "Connecting the operator"),
                    ("autopay_enable", "Setting up autopay"),
                    ("autopay_pause", "Pausing autopay"),
                    ("autopay_resume", "Resuming autopay"),
                    ("payment_mark", "Marking the invoice paid"),
                    ("payment_retry_mark", "Marking the invoice paid again"),
                    ("payment_refund", "Refunding the payment"),
                    ("payment_void", "Voiding the payment"),
                    ("operator_methods_refresh", "Updating bank methods"),
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
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="job",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Waiting"),
                    ("running", "In progress"),
                    ("done", "Done"),
                    ("failed", "Failed"),
                    ("dead", "Gave up"),
                ],
                default="queued",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="reconciliationmismatch",
            name="kind",
            field=models.CharField(
                choices=[
                    ("invoice_status_differs", "Invoice status differed"),
                    ("payment_restored", "Payment restored"),
                    ("deposit_unmatched", "Bank deposit not matched"),
                    ("settled_payment_undeposited", "Settled payment not deposited"),
                    ("return_missed_by_webhook", "Return not received"),
                    ("pending_too_old", "Pending too long"),
                ],
                max_length=32,
            ),
        ),
    ]
