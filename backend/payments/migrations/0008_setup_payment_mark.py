"""The ``setup_payment_mark`` job kind and its ``setup_charge`` entity type
(docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md D6): a manual payment on a setup charge
whose provider invoice is to be marked paid is queued for the worker, as a rent charge's
is with ``payment_mark``. Only the closed lists of the CHECK constraints change; no
column or trigger.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0007_reconciliation"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="job",
            name="jobs_kind_valid",
        ),
        migrations.RemoveConstraint(
            model_name="job",
            name="jobs_entity_type_valid",
        ),
        migrations.AlterField(
            model_name="job",
            name="entity_type",
            field=models.CharField(
                choices=[
                    ("operator", "Operator"),
                    ("lease", "Lease"),
                    ("rent_charge", "Rent charge"),
                    ("payment", "Payment"),
                    ("rent_run", "Rent run"),
                    ("webhook_event", "Webhook event"),
                    ("reconciliation", "Reconciliation"),
                    ("setup_charge", "Setup charge"),
                ],
                max_length=32,
            ),
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
                        ],
                    )
                ),
                name="jobs_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "entity_type__in",
                        [
                            "operator",
                            "lease",
                            "rent_charge",
                            "payment",
                            "rent_run",
                            "webhook_event",
                            "reconciliation",
                            "setup_charge",
                        ],
                    )
                ),
                name="jobs_entity_type_valid",
            ),
        ),
    ]
