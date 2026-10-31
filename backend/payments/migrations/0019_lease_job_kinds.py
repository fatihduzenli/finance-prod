"""The lease job kinds (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md C3,
C7).

PLAN-014 C3 moved the processor customer from the operator to the lease and kept the
stored values of the four operator kinds; this migration renames them and adds the bank
method attach of the hosted fields:

- ``operator_sync`` -> ``lease_sync``, ``operator_retry`` -> ``lease_retry``,
  ``operator_link`` -> ``lease_link``, ``operator_methods_refresh`` ->
  ``lease_methods_refresh``: every existing ``jobs`` row is rewritten (a data step), the
  rows of an operator (queued before ledger migration 0059) included; the runners still run
  those as a no-op;
- ``lease_method_attach`` (new, entity ``lease``).

The ``jobs_kind_valid`` CHECK is dropped before the rewrite and rebuilt on the new list
after it. Reverse: the rows are renamed back, refused (:data:`CANNOT_REVERSE`) while a job
of the new kind exists. No column, trigger or other table changes.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_BEFORE = importlib.import_module("payments.migrations.0017_autopay_stop_job")

#: Stored value before -> after.
RENAMED: dict[str, str] = {
    "operator_sync": "lease_sync",
    "operator_retry": "lease_retry",
    "operator_link": "lease_link",
    "operator_methods_refresh": "lease_methods_refresh",
}
NEW_KINDS: tuple[str, ...] = ("lease_method_attach",)
KINDS = [*(RENAMED.get(kind, kind) for kind in _BEFORE.KINDS), *NEW_KINDS]
CANNOT_REVERSE = "Bank account connection jobs exist; this migration refuses to reverse."

_CHOICES_AFTER = [
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
    ("one_off_payment_retry_mark", "Marking the one-off charge's invoice paid again"),
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
]


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    for before, after in RENAMED.items():
        job.objects.filter(kind=before).update(kind=after)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    job = apps.get_model("payments", "Job")
    if job.objects.filter(kind__in=NEW_KINDS).exists():
        raise RuntimeError(CANNOT_REVERSE)
    for before, after in RENAMED.items():
        job.objects.filter(kind=after).update(kind=before)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0018_copy_labels"),
    ]

    # Forwards: the old CHECK goes, the rows are rewritten, the new CHECK comes. In reverse
    # the new CHECK goes first, then the rows are renamed back (or the reverse refuses),
    # then the old CHECK returns.
    operations = [
        migrations.RemoveConstraint(model_name="job", name="jobs_kind_valid"),
        migrations.AlterField(
            model_name="job",
            name="kind",
            field=models.CharField(choices=_CHOICES_AFTER, max_length=32),
        ),
        migrations.RunPython(forwards, backwards, elidable=False),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", KINDS)), name="jobs_kind_valid"
            ),
        ),
    ]
