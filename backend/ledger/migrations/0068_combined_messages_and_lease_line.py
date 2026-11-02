"""One e-mail per lease and day, the 3-day fold, and D's wording for an ended lease (owner,
2026-11-02).

Schema, on ``outbound_messages``: ``combined_into`` (a nullable foreign key to another message,
``PROTECT``): the message of the same lease and day that lists this one's charge and is sent for
both. Two statuses join the closed list: ``waiting`` (an overdue message held back by the 3-day
fold) and ``combined`` (listed in ``combined_into``, never sent on its own); the status CHECK is
recreated with them, and a new CHECK keeps ``combined_into`` set exactly on a ``combined`` row.
No text column is added, so no payment-data trigger changes (an empty ``GUARDED_COLUMNS``).

Data, on ``message_templates``: the body of D (``overdue_day10``) gets the placeholder
``{lease_line}`` in place of the sentence seeded by ``0061``, "Continued non-payment may affect
your lease.", **only when the body still holds that exact sentence**; a body an admin edited
without it is left alone, and only then an ``audit_log`` row says so
(``message_template.lease_line_skipped`` with ``{"reason": "edited"}``; no text of the body is
copied into it), so the admin learns that D keeps its own wording. The seeded case writes no audit
row: a fresh database holds the seeded D, and the suite's "writes nothing" tests read an empty
audit log (gate of 2026-11-02).

The reverse writes the sentence back wherever a template body holds ``{lease_line}`` (the
current-lease reading of the placeholder, which the code before this migration does not know),
turns every ``waiting`` row back into ``queued`` and every ``combined`` row into ``suppressed``
(error ``Combined``, ``combined_into`` cleared), then drops the CHECKs, the statuses and the
column. On PostgreSQL any check queued earlier in the transaction runs before the first
``ALTER TABLE`` (the lesson of ``0018``).
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
D_CODE = "overdue_day10"
SENTENCE = "Continued non-payment may affect your lease."
PLACEHOLDER = "{lease_line}"
SKIPPED = "message_template.lease_line_skipped"
#: What a combined row becomes on the reverse (an error is a class-name-shaped word).
COMBINED_BACK = "Combined"

STATUSES_BEFORE = ["queued", "sent", "failed", "suppressed", "catch_all", "held"]
STATUSES_AFTER = [*STATUSES_BEFORE, "waiting", "combined"]
STATUS_CHOICES = [
    ("queued", "Waiting"),
    ("sent", "Sent"),
    ("failed", "Failed"),
    ("suppressed", "Suppressed"),
    ("catch_all", "Sent to the test inbox"),
    ("held", "Held"),
    ("waiting", "Waiting for the 3-day fold"),
    ("combined", "Combined"),
]


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def uncombine(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """The reverse: no row may keep a status the code before this migration does not know."""
    message = apps.get_model("ledger", "OutboundMessage")
    message.objects.filter(status="waiting").update(status="queued")
    message.objects.filter(status="combined").update(
        status="suppressed", error=COMBINED_BACK, combined_into=None
    )


def add_lease_line(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    audit_log = apps.get_model("ledger", "AuditLog")
    found = template.objects.filter(code=D_CODE).first()
    if found is None:
        return
    if SENTENCE in found.body:
        template.objects.filter(pk=found.pk).update(body=found.body.replace(SENTENCE, PLACEHOLDER))
        return
    audit_log.objects.create(
        actor=None,
        action=SKIPPED,
        entity_type="message_template",
        entity_id=str(found.pk),
        before=None,
        after={"code": D_CODE, "reason": "edited"},
    )


def remove_lease_line(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    for pk, body in list(
        template.objects.filter(body__contains=PLACEHOLDER).values_list("pk", "body")
    ):
        template.objects.filter(pk=pk).update(body=body.replace(PLACEHOLDER, SENTENCE))


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0067_lease_billing_bank_name"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_status_valid",
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="combined_into",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="combined",
                to="ledger.outboundmessage",
            ),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="status",
            field=models.CharField(choices=STATUS_CHOICES, default="queued", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=STATUSES_AFTER),
                name="outbound_messages_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=(models.Q(status="combined") & models.Q(combined_into__isnull=False))
                | (~models.Q(status="combined") & models.Q(combined_into__isnull=True)),
                name="outbound_messages_combined_has_message",
            ),
        ),
        # Forward nothing; the reverse runs here, before the CHECKs and statuses go.
        migrations.RunPython(migrations.RunPython.noop, uncombine, elidable=False),
        migrations.RunPython(add_lease_line, remove_lease_line, elidable=False),
    ]
