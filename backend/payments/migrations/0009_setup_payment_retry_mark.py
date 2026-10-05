"""The ``setup_payment_retry_mark`` job kind (docs/qa/PLAN-010b, Batch B review carry-over):
the manual retry of a failed marking of a setup charge's provider invoice, queued from
the setup charge page as a rent charge's is with ``payment_retry_mark``. Only the closed
list of the kind CHECK changes; no column or trigger.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0008_setup_payment_mark"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="job",
            name="jobs_kind_valid",
        ),
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
                    ("setup_payment_mark", "Mark the provider invoice of a setup charge paid"),
                    (
                        "setup_payment_retry_mark",
                        "Retry marking the provider invoice of a setup charge",
                    ),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        [
                            "operator_sync",
                            "operator_retry",
                            "operator_link",
                            "autopay_enable",
                            "autopay_pause",
                            "autopay_resume",
                            "payment_mark",
                            "payment_retry_mark",
                            "payment_refund",
                            "payment_void",
                            "operator_methods_refresh",
                            "run_match",
                            "webhook_replay",
                            "reconcile_run",
                            "setup_payment_mark",
                            "setup_payment_retry_mark",
                        ],
                    )
                ),
                name="jobs_kind_valid",
            ),
        ),
    ]
