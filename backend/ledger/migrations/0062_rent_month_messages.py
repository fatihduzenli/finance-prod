"""Rules A and B for rent keyed to the lease and the month
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md E4, the owner's decision of
2026-10-30): the reminder three days before a month's rent is sent before the month's rent
charge exists, so the message names the lease and the month instead of a charge.

Schema, ``outbound_messages``:

- ``period_month`` (date, null): the month (its 1st) of a message about a lease month.
- ``charge_entity_type`` gains ``lease_month`` (CHECK ``outbound_messages_charge_entity_type
  _valid``).
- ``outbound_messages_kind_matches_rule`` is rebuilt: as before, and a ``lease_month`` row
  carries a ``period_month`` and the rule ``A`` or ``B``, while every other row carries none.
- ``outbound_messages_one_per_lease_charge_rule`` (one row per lease, charge, rule and
  occurrence unless sent by hand) now reads only rows without a month: a lease month's rows
  all name the lease as their entity, month after month.
- ``outbound_messages_one_per_lease_month``: one row per (lease, rule, month) where the month
  is set; a second run of the reminders the same day writes nothing.

No text column is added, so the payment-data trigger of ``outbound_messages`` (``0061``) is
unchanged. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this transaction
queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in either direction.

The reverse refuses (``CANNOT_REVERSE``) while a ``lease_month`` row exists: the schema
before it has no type or month for it. Otherwise it drops the constraints and the column in
order and restores the constraints of ``0061``.
"""

from __future__ import annotations

from typing import Any

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

MESSAGE_MODEL = "OutboundMessage"
LEASE_MONTH = "lease_month"
CANNOT_REVERSE = (
    "Messages about a lease month exist; this migration refuses to reverse (the messages "
    "before it have no type or month for them)."
)

CHARGE_TYPE_CHOICES = [
    ("rent_charge", "Rent charge"),
    ("one_off_charge", "One-off charge"),
    ("lease_end", "Lease end"),
    ("insurance_charge", "Insurance charge"),
    (LEASE_MONTH, "Rent month"),
]
#: The charge types a message about a charge names, as ``0061`` had them.
OLD_CHARGE_TYPES = ["rent_charge", "one_off_charge", "insurance_charge"]


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    message: Any = apps.get_model("ledger", MESSAGE_MODEL)
    if message.objects.filter(charge_entity_type=LEASE_MONTH).exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


def _kind_matches_rule(*, lease_month: bool) -> models.CheckConstraint:
    """The CHECK of ``0061``, and with ``lease_month`` this migration's rows of a month."""
    types = [*OLD_CHARGE_TYPES, LEASE_MONTH] if lease_month else OLD_CHARGE_TYPES
    condition = (
        ~Q(kind="ladder") | (Q(charge_entity_type__in=types) & ~Q(rule="final_statement"))
    ) & (~Q(kind="final_statement") | Q(charge_entity_type="lease_end", rule="final_statement"))
    if lease_month:
        condition &= Q(
            charge_entity_type=LEASE_MONTH, period_month__isnull=False, rule__in=["A", "B"]
        ) | (~Q(charge_entity_type=LEASE_MONTH) & Q(period_month__isnull=True))
    return models.CheckConstraint(condition=condition, name="outbound_messages_kind_matches_rule")


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0061_messaging_engine"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="outbound_messages_kind_matches_rule"
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="outbound_messages_charge_entity_type_valid"
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="outbound_messages_one_per_lease_charge_rule"
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="period_month",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="charge_entity_type",
            field=models.CharField(max_length=32, choices=CHARGE_TYPE_CHOICES),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(charge_entity_type__in=[value for value, _ in CHARGE_TYPE_CHOICES]),
                name="outbound_messages_charge_entity_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage", constraint=_kind_matches_rule(lease_month=True)
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.UniqueConstraint(
                condition=Q(manual=False, period_month__isnull=True),
                fields=("lease", "charge_entity_type", "charge_entity_id", "rule", "occurrence"),
                name="outbound_messages_one_per_lease_charge_rule",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.UniqueConstraint(
                condition=Q(period_month__isnull=False),
                fields=("lease", "rule", "period_month"),
                name="outbound_messages_one_per_lease_month",
            ),
        ),
        # Reversed first: refused while a lease month's message exists, then any check
        # queued runs before the ALTER TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
