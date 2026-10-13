"""Outbound messages of more than one kind; the final statement e-mail
(docs/qa/PLAN-012e-lease-end-transfer-installments.md D8).

Schema: ``outbound_messages.kind`` (``ladder`` or ``final_statement``, default
``ladder``), ``outbound_messages.document`` (null; the attachment of a final statement),
``outbound_messages.step`` becomes nullable, and ``charge_entity_type`` gains
``lease_end``. A ladder message names its step and a charge; a final statement names a
lease end and no step, and is made once per lease end. Every existing row is a ladder
message with its step, which the default and the new CHECK say.

Data: the admin-editable template ``final_statement`` is seeded (left as it is when it
exists). Its merge fields are those of :mod:`ledger.merge` for this code.

The reverse refuses while a message other than a ladder one exists, then deletes the
seeded template when no message uses it.
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

CODE = "final_statement"
SUBJECT = "Final statement for {store}"
BODY = (
    "Hello {operator},\n\n"
    "Your lease of {store} ended on {end_date}. Your final statement is attached.\n\n"
    "Refund due to you: {refund_due}\n"
    "Amount you owe: {amount_owed}\n\n"
    "If you have any question about the statement, reply to this e-mail.\n\n"
    "{company}"
)
CANNOT_REVERSE = "Final statement e-mails exist; this migration refuses to reverse."


def seed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("ledger", "MessageTemplate").objects.get_or_create(
        code=CODE, defaults={"subject": SUBJECT, "body": BODY}
    )


def unseed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate").objects.filter(code=CODE).first()
    message = apps.get_model("ledger", "OutboundMessage")
    if template is not None and not message.objects.filter(template=template).exists():
        template.delete()


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if apps.get_model("ledger", "OutboundMessage").objects.exclude(kind="ladder").exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0035_lease_end_transfer"),
    ]

    operations = [
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_charge_entity_type_valid",
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="kind",
            field=models.CharField(
                choices=[("ladder", "Collections ladder"), ("final_statement", "Final statement")],
                default="ladder",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="document",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.document",
            ),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="charge_entity_type",
            field=models.CharField(
                choices=[
                    ("rent_charge", "Rent charge"),
                    ("one_off_charge", "One-off charge"),
                    ("lease_end", "Lease end"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="step",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="messages",
                to="ledger.collectionstep",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(charge_entity_type__in=["rent_charge", "one_off_charge", "lease_end"]),
                name="outbound_messages_charge_entity_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(kind__in=["ladder", "final_statement"]),
                name="outbound_messages_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=(
                    ~Q(kind="ladder")
                    | Q(
                        step__isnull=False,
                        charge_entity_type__in=["rent_charge", "one_off_charge"],
                    )
                )
                & (
                    ~Q(kind="final_statement")
                    | Q(step__isnull=True, charge_entity_type="lease_end")
                ),
                name="outbound_messages_step_iff_ladder",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(kind="final_statement") | Q(document__isnull=True),
                name="outbound_messages_document_only_final_statement",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.UniqueConstraint(
                condition=Q(kind="final_statement", manual=False),
                fields=("charge_entity_type", "charge_entity_id"),
                name="outbound_messages_one_final_statement",
            ),
        ),
        migrations.RunPython(seed, unseed, elidable=False),
        # --- The reverse starts here: it refuses before any constraint changes.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
