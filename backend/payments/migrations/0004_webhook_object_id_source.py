"""``webhook_events.object_id_source`` and a longer ``outcome``
(docs/qa/PLAN-008a-send-invoice-and-manual-link.md §2 rules 1, 3).

``object_id_source`` names the body path the receiver read the object ID from
(``mergeVars.invoice.id`` for a ``send_invoice`` envelope); existing rows get the empty
value. ``outcome`` grows to 64 characters for ``processed: unlinked (n candidates)``;
the card-number guard on it is unchanged (the PostgreSQL trigger names columns, not
their types).
"""

from __future__ import annotations

from django.db import migrations, models

import ledger.validators


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0003_webhook_event_processing"),
    ]

    operations = [
        migrations.AddField(
            model_name="webhookevent",
            name="object_id_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "None"),
                    ("id", "id"),
                    ("mergeVars.invoice.id", "mergeVars.invoice.id"),
                    ("invoice.id", "invoice.id"),
                    ("transaction.id", "transaction.id"),
                    ("data.id", "data.id"),
                    ("batch_id", "batch_id"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="webhookevent",
            name="outcome",
            field=models.CharField(
                blank=True, max_length=64, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddConstraint(
            model_name="webhookevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "object_id_source__in",
                        [
                            "",
                            "id",
                            "mergeVars.invoice.id",
                            "invoice.id",
                            "transaction.id",
                            "data.id",
                            "batch_id",
                        ],
                    )
                ),
                name="webhook_events_object_id_source_valid",
            ),
        ),
    ]
