"""One invoice PDF, receipt PDFs and the invoice e-mails behind a company setting
(docs/qa/15/PLAN-015c-invoice-pdfs.md; ADR-0020).

Schema, on ``company_settings``: the company's address (``address_line1``, ``address_line2``,
``city``, ``state``, ``postal_code``, each optional, default ``""``), printed in the PDFs' band;
``invoice_sender`` (``processor``, the default, or ``company``, with its CHECK
``company_settings_invoice_sender_valid``): who e-mails the invoices; ``logo_png``, the PNG copy
of the logo override the renderer and the portal read (empty: the brand logo). The five address
columns are guarded: the ``company_settings`` trigger of ``0061_messaging_engine`` is recreated
with them among its arguments (the reverse restores ``0061``'s columns). ``logo_png`` holds image
bytes, never text, and is not guarded.

Schema, on ``outbound_messages``: ``kind`` gains ``invoice`` and ``receipt``, ``rule`` gains
``invoice`` and ``receipt``, ``charge_entity_type`` gains ``invoice`` and ``payment`` (each CHECK
rebuilt); the CHECK ``outbound_messages_kind_matches_rule`` adds: an invoice message names an
invoice under rule ``invoice``, a receipt a payment under rule ``receipt``, and no other message
names either. Their PDF is rendered at the send, never stored: no ``document`` is named. On
PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this transaction queued before the
``ALTER TABLE`` statements (the lesson of ``0018``), in either direction.

Data: the two seeded templates, ``invoice_sent`` ("Invoice e-mailed by the company") and
``receipt`` ("Receipt for a payment recorded by hand"); each placeholder list is its own
(``ledger.merge.fields_for``). A template already there under the code is kept as it is.

The reverse refuses (``CANNOT_REVERSE``) while an invoice or receipt message exists (the schema
before it has no place for one); otherwise it deletes the two templates, restores the trigger and
the CHECKs of ``0064`` and drops the columns (the address, the sender choice and the logo copy:
the copy is made again when the logo is saved).
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

COMPANY = "company_settings"
_COMPANY_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0061_messaging_engine"
).GUARDED_COLUMNS[COMPANY]
#: Table -> guarded columns after this migration: ``company_settings`` replaces ``0061``'s entry.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    COMPANY: (
        *_COMPANY_BEFORE,
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
    ),
}

CANNOT_REVERSE = (
    "Invoice or receipt messages of PLAN-015c exist; this migration refuses to reverse (the "
    "schema before it has no place for them)."
)

_INVOICE_BODY = (
    "Hello {operator_name},\n\n"
    "Your invoice {invoice_number} for store #{store_number} {store_name} is attached: "
    "{amount}, due {due_date}.\n\n"
    "Pay by bank debit (ACH) here: {pay_link}\n"
    "Or pay on our payment page: {provider_pay_link}\n"
    "Paying by wire or Zelle? Put {invoice_number} in the memo.\n\n"
    "Questions? Write to {support_email}.\n\n"
    "{company_name}"
)
_RECEIPT_BODY = (
    "Hello {operator_name},\n\n"
    "Thank you: we received {amount} on {paid_on} for {applied_to} at store #{store_number} "
    "{store_name}. Your receipt is attached.\n\n"
    "Questions? Write to {support_email}.\n\n"
    "{company_name}"
)
#: (code, name, subject, body).
TEMPLATES: tuple[tuple[str, str, str, str], ...] = (
    (
        "invoice_sent",
        "Invoice e-mailed by the company",
        "Invoice {invoice_number} — {amount} due {due_date}",
        _INVOICE_BODY,
    ),
    (
        "receipt",
        "Receipt for a payment recorded by hand",
        "Receipt for {amount}",
        _RECEIPT_BODY,
    ),
)
SEEDED_CODES: tuple[str, ...] = tuple(code for code, *_ in TEMPLATES)
NEW_KINDS = ("invoice", "receipt")


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def _replace_trigger(
    schema_editor: BaseDatabaseSchemaEditor, table: str, columns: tuple[str, ...]
) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {table}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        _replace_trigger(schema_editor, table, columns)


def restore_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, COMPANY, _COMPANY_BEFORE)
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
    if message.objects.filter(kind__in=NEW_KINDS).exists():
        raise RuntimeError(CANNOT_REVERSE)
    apps.get_model("ledger", "MessageTemplate").objects.filter(code__in=SEEDED_CODES).delete()
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0070_payment_channel_invoice"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_charge_entity_type_valid",
        ),
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
            name="outbound_messages_kind_matches_rule",
        ),
        migrations.AddField(
            model_name="companysettings",
            name="address_line1",
            field=models.CharField(
                blank=True,
                default="",
                max_length=200,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="address_line2",
            field=models.CharField(
                blank=True,
                default="",
                max_length=200,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="city",
            field=models.CharField(
                blank=True,
                default="",
                max_length=100,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="invoice_sender",
            field=models.CharField(
                choices=[("processor", "The payment processor"), ("company", "The company")],
                default="processor",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="logo_png",
            field=models.BinaryField(blank=True, default=b""),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="postal_code",
            field=models.CharField(
                blank=True,
                default="",
                max_length=20,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="state",
            field=models.CharField(
                blank=True,
                default="",
                max_length=50,
                validators=[ledger.validators.validate_no_payment_data],
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
                    ("insurance_charge", "Insurance charge"),
                    ("lease_month", "Rent month"),
                    ("portal_user", "Portal account"),
                    ("invoice", "Invoice"),
                    ("payment", "Payment"),
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
                    ("invoice", "Invoice"),
                    ("receipt", "Receipt"),
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
                    ("invoice", "Invoice e-mailed"),
                    ("receipt", "Receipt e-mailed"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="companysettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("invoice_sender__in", ["processor", "company"])),
                name="company_settings_invoice_sender_valid",
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
                            "invoice",
                            "payment",
                        ],
                    )
                ),
                name="outbound_messages_charge_entity_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kind__in", ["ladder", "final_statement", "portal", "invoice", "receipt"])
                ),
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
                            "invoice",
                            "receipt",
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
                    models.Q(
                        models.Q(
                            ("charge_entity_type", "invoice"),
                            ("kind", "invoice"),
                            ("rule", "invoice"),
                        ),
                        models.Q(
                            models.Q(("kind", "invoice"), _negated=True),
                            models.Q(("charge_entity_type", "invoice"), _negated=True),
                            models.Q(("rule", "invoice"), _negated=True),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(
                            ("charge_entity_type", "payment"),
                            ("kind", "receipt"),
                            ("rule", "receipt"),
                        ),
                        models.Q(
                            models.Q(("kind", "receipt"), _negated=True),
                            models.Q(("charge_entity_type", "payment"), _negated=True),
                            models.Q(("rule", "receipt"), _negated=True),
                        ),
                        _connector="OR",
                    ),
                ),
                name="outbound_messages_kind_matches_rule",
            ),
        ),
        migrations.RunPython(create_guards, restore_guards, elidable=False),
        migrations.RunPython(seed_templates, unseed_templates, elidable=False),
    ]
