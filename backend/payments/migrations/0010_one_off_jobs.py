"""One-off charge jobs (docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D1 to D3, D6).

The job kinds ``setup_payment_mark`` and ``setup_payment_retry_mark`` become
``one_off_payment_mark`` and ``one_off_payment_retry_mark``, and the job entity type
``setup_charge`` becomes ``one_off_charge``; existing job rows are rewritten. New kinds:
``invoice_create``, ``invoice_send``, ``invoice_delete``, ``schedule_create`` and
``schedule_delete``. Only the closed lists of the CHECK constraints and the rows
change; no column or trigger. The reverse rewrites the rows back and refuses while a
job of a new kind exists.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

RENAMED_KINDS: dict[str, str] = {
    "setup_payment_mark": "one_off_payment_mark",
    "setup_payment_retry_mark": "one_off_payment_retry_mark",
}
NEW_KINDS: tuple[str, ...] = (
    "invoice_create",
    "invoice_send",
    "invoice_delete",
    "schedule_create",
    "schedule_delete",
)
OLD_ENTITY = "setup_charge"
NEW_ENTITY = "one_off_charge"
CANNOT_REVERSE = "Jobs of the one-off invoice kinds exist; this migration refuses to reverse."

KINDS_BEFORE = [
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
]
KINDS = [*KINDS_BEFORE, *RENAMED_KINDS.values(), *NEW_KINDS]
ENTITIES = [
    "operator",
    "lease",
    "rent_charge",
    "payment",
    "rent_run",
    "webhook_event",
    "reconciliation",
    NEW_ENTITY,
]


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    for old, new in RENAMED_KINDS.items():
        job.objects.filter(kind=old).update(kind=new)
    job.objects.filter(entity_type=OLD_ENTITY).update(entity_type=NEW_ENTITY)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    if job.objects.filter(kind__in=NEW_KINDS).exists():
        raise RuntimeError(CANNOT_REVERSE)
    for old, new in RENAMED_KINDS.items():
        job.objects.filter(kind=new).update(kind=old)
    job.objects.filter(entity_type=NEW_ENTITY).update(entity_type=OLD_ENTITY)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0009_setup_payment_retry_mark"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="job", name="jobs_kind_valid"),
        migrations.RemoveConstraint(model_name="job", name="jobs_entity_type_valid"),
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
                    ("one_off_charge", "One-off charge"),
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
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(forwards, backwards, elidable=False),
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
