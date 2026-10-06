"""The mail job kinds (docs/qa/PLAN-011b-collections-nsf-attention-calendar.md D4, D8).

Kinds ``message_send`` (a collection e-mail, entity ``outbound_message``) and
``calendar_email`` (a staff user's morning e-mail, entity ``calendar_entry`` or
``contact_entry``). Only the closed lists of the two CHECK constraints change; no column
or trigger, no row. The reverse refuses while a job of a new kind or entity exists.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_BEFORE_KINDS = importlib.import_module("payments.migrations.0013_invoice_pay_job").KINDS
_BEFORE_ENTITIES = importlib.import_module("payments.migrations.0010_one_off_jobs").ENTITIES

NEW_KINDS: tuple[str, ...] = ("message_send", "calendar_email")
NEW_ENTITIES: tuple[str, ...] = ("outbound_message", "calendar_entry", "contact_entry")
KINDS = [*_BEFORE_KINDS, *NEW_KINDS]
ENTITIES = [*_BEFORE_ENTITIES, *NEW_ENTITIES]
CANNOT_REVERSE = "Mail jobs exist; this migration refuses to reverse."


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    return None


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    if (
        job.objects.filter(kind__in=NEW_KINDS).exists()
        or job.objects.filter(entity_type__in=NEW_ENTITIES).exists()
    ):
        raise RuntimeError(CANNOT_REVERSE)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0013_invoice_pay_job"),
    ]

    # The check runs last forwards, so it runs first in reverse, before the old CHECK
    # constraints are put back.
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
                    ("outbound_message", "Outbound message"),
                    ("calendar_entry", "Calendar entry"),
                    ("contact_entry", "Contact entry"),
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
                    ("invoice_pay", "Charge the provider invoice now"),
                    ("message_send", "Send a collection e-mail"),
                    ("calendar_email", "Send a morning e-mail"),
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
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(("entity_type__in", ENTITIES)), name="jobs_entity_type_valid"
            ),
        ),
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
