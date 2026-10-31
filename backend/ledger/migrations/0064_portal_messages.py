"""Portal messages (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md H2): the
invitation to the operator portal and the password reset go through the messaging engine.

Schema, ``outbound_messages``: ``kind`` gains ``portal``, ``rule`` gains ``portal`` and
``charge_entity_type`` gains ``portal_user`` (each CHECK rebuilt); the CHECK
``outbound_messages_kind_matches_rule`` adds: a portal message names a portal account under
rule ``portal``, and only a portal message does. No text column is added, so no payment-data
trigger changes (``GUARDED_COLUMNS`` is empty). On PostgreSQL ``SET CONSTRAINTS ALL
IMMEDIATE`` runs any check this transaction queued before the ``ALTER TABLE`` statements, in
either direction.

Data: the two seeded templates, ``portal_invite`` ("H — Portal invitation", with
``{set_password_link}``) and ``portal_reset`` ("H — Portal password reset", with
``{reset_link}``); each placeholder is allowed for its own template only
(``ledger.merge.fields_for``). A template already there under the code is kept as it is.

The reverse refuses (``CANNOT_REVERSE``) while a portal message exists (the schema before it
has no place for one); otherwise it deletes the two templates and restores the CHECKs of
``0063``.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

#: No text column is added (PLAN-012f-b Batch 0: every migration declares what it guards).
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}

CANNOT_REVERSE = (
    "Portal messages of PLAN-014 H2 exist; this migration refuses to reverse (the schema "
    "before it has no place for them)."
)

_INVITE_BODY = (
    "Hello {operator_name},\n\n"
    "{company_name} has set up an online portal for you: your stores, what is open on each, "
    "your invoices and your payments, in one place.\n\n"
    "Choose your password here. The link works once, for 7 days:\n"
    "{set_password_link}\n\n"
    "Questions? Write to {support_email}.\n\n"
    "{company_name}"
)
_RESET_BODY = (
    "Hello {operator_name},\n\n"
    "Choose a new password for the {company_name} portal here. The link works once, for one "
    "hour:\n"
    "{reset_link}\n\n"
    "If you did not ask for this, ignore this e-mail: your password stays as it is.\n\n"
    "{company_name}"
)
#: (code, name, subject, body).
TEMPLATES: tuple[tuple[str, str, str, str], ...] = (
    (
        "portal_invite",
        "H — Portal invitation",
        "Your {company_name} portal account",
        _INVITE_BODY,
    ),
    (
        "portal_reset",
        "H — Portal password reset",
        "Choose a new password for the {company_name} portal",
        _RESET_BODY,
    ),
)
SEEDED_CODES: tuple[str, ...] = tuple(code for code, *_ in TEMPLATES)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def seed_templates(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    for code, name, subject, body in TEMPLATES:
        template.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "internal": False,
                "subject": subject,
                "body": body,
                "channel": "email",
                "active": True,
            },
        )


def unseed_templates(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    message = apps.get_model("ledger", "OutboundMessage")
    if message.objects.filter(kind="portal").exists():
        raise RuntimeError(CANNOT_REVERSE)
    apps.get_model("ledger", "MessageTemplate").objects.filter(code__in=SEEDED_CODES).delete()
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0063_returned_debits"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_kind_valid",
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_rule_valid",
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_charge_entity_type_valid",
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_kind_matches_rule",
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="charge_entity_type",
            field=models.CharField(
                choices=[
                    ("rent_charge", "Rent charge"),
                    ("one_off_charge", "One-off charge"),
                    ("lease_end", "Lease end"),
                    ("insurance_charge", "Insurance charge"),
                    ("lease_month", "Rent month"),
                    ("portal_user", "Portal account"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="kind",
            field=models.CharField(
                choices=[
                    ("ladder", "Charge message"),
                    ("final_statement", "Final statement"),
                    ("portal", "Portal access"),
                ],
                default="ladder",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="rule",
            field=models.CharField(
                choices=[
                    ("A", "A — rent will be debited"),
                    ("B", "B — payment due"),
                    ("C", "C — payment overdue"),
                    ("D", "D — notice: overdue"),
                    ("E", "E — 30 days overdue (internal)"),
                    ("F", "F — payment returned"),
                    ("final_statement", "Final statement"),
                    ("manual", "Sent by hand"),
                    ("ladder", "Old collections ladder"),
                    ("portal", "Portal access"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "charge_entity_type__in",
                        [
                            "rent_charge",
                            "one_off_charge",
                            "lease_end",
                            "insurance_charge",
                            "lease_month",
                            "portal_user",
                        ],
                    )
                ),
                name="outbound_messages_charge_entity_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["ladder", "final_statement", "portal"])),
                name="outbound_messages_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "rule__in",
                        [
                            "A",
                            "B",
                            "C",
                            "D",
                            "E",
                            "F",
                            "final_statement",
                            "manual",
                            "ladder",
                            "portal",
                        ],
                    )
                ),
                name="outbound_messages_rule_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        models.Q(("kind", "ladder"), _negated=True),
                        models.Q(
                            (
                                "charge_entity_type__in",
                                (
                                    "rent_charge",
                                    "one_off_charge",
                                    "insurance_charge",
                                    "lease_month",
                                ),
                            ),
                            models.Q(("rule", "final_statement"), _negated=True),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(("kind", "final_statement"), _negated=True),
                        models.Q(("charge_entity_type", "lease_end"), ("rule", "final_statement")),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(
                            ("charge_entity_type", "lease_month"),
                            ("period_month__isnull", False),
                            ("rule__in", ("A", "B")),
                        ),
                        models.Q(
                            models.Q(("charge_entity_type", "lease_month"), _negated=True),
                            ("period_month__isnull", True),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(
                            ("charge_entity_type", "portal_user"),
                            ("kind", "portal"),
                            ("rule", "portal"),
                        ),
                        models.Q(
                            models.Q(("kind", "portal"), _negated=True),
                            models.Q(("charge_entity_type", "portal_user"), _negated=True),
                            models.Q(("rule", "portal"), _negated=True),
                        ),
                        _connector="OR",
                    ),
                ),
                name="outbound_messages_kind_matches_rule",
            ),
        ),
        migrations.RunPython(seed_templates, unseed_templates, elidable=False),
    ]
