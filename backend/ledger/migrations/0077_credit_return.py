"""A returned bank debit kept as a credit reverses the credit (docs/qa/15/PLAN-015e-credit-return.md
§3, §4 task 1).

Schema:

- ``lease_credits``: ``reversed_at`` (timestamp, null) and ``reversed_reason`` (text, default
  ``""``, guarded): when the credit was reversed because the bank debit it held was returned,
  and the return's code and reason; ``lease_credits_reversed_holds_nothing`` (a reversed
  credit's ``remaining`` is 0).
- ``payments``: ``split_from`` (a payment, null): on a payment held as a credit that "Keep as
  credit" split off another, the payment it came from (the row the processor's transaction
  names); ``payments_split_from_held_row`` (only such a row names one).
- ``leases.review_reason`` gains ``credit_returned`` ("Returned debit: credit reversed").
- ``outbound_messages``: ``kind`` gains ``credit_returned``, ``rule`` gains ``credit_returned``
  (the managers' e-mail) and ``credit_returned_operator`` (the operator's), and
  ``charge_entity_type`` gains ``lease_credit`` (each CHECK rebuilt); the CHECK
  ``outbound_messages_kind_matches_rule`` adds: such a message names a lease credit under one of
  the two rules, and no other message names a credit or uses them.

The new text column is guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check
this transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), and the
``lease_credits`` trigger of ``0042_payment_allocation_and_credits`` is recreated with
``reversed_reason`` among its arguments; the reverse restores ``0042``'s columns. Nothing is
created on other vendors; the application validator applies there.

Data, forwards: every held row split off a payment before this migration is linked to it: each
``payment.held_as_credit`` audit row whose ``held_payment`` is not the payment it audits sets
that held row's ``split_from`` to the payment, when both rows exist (a row linked already is
left as it is). The two templates are seeded, a template already there under the code kept as
it is: ``credit_returned`` (internal, to the managers) and ``credit_returned_operator`` (to the
lease's billing e-mail, the owner's wording of 2026-11-08); each placeholder list is its own
(``ledger.merge.fields_for``).

The reverse refuses (``CANNOT_REVERSE``), with nothing changed, while a reversed credit, a lease
flagged ``credit_returned`` or a message about a reversed credit exists (the schema before it
has no place for them); otherwise it deletes the two templates, restores the trigger and the
CHECKs and drops the columns (the ``split_from`` links can be made again from the audit rows).
"""

from __future__ import annotations

import importlib
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

import ledger.validators

CREDITS = "lease_credits"
_CREDITS_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0042_payment_allocation_and_credits"
).GUARDED_COLUMNS[CREDITS]
#: Table -> guarded columns after this migration: ``lease_credits`` replaces ``0042``'s entry.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    CREDITS: (*_CREDITS_BEFORE, "reversed_reason"),
}
HELD_ACTION = "payment.held_as_credit"
CANNOT_REVERSE = (
    "A reversed credit, a lease flagged for a reversed credit or a message about one exists; "
    "this migration refuses to reverse (the schema before it has no place for them)."
)

_OPERATOR_BODY = (
    "Hello {operator_name},\n\n"
    "Your bank returned your payment of {amount} made on {debit_date} (reason: "
    "{return_reason}). It had been kept as a credit on store #{store_number} {store_name}; "
    "that credit is reversed and your balance is now {balance}. Please contact us at "
    "{support_email}.\n\n"
    "{company_name}"
)
_INTERNAL_BODY = (
    "The bank returned the debit of {debit_date} (reason: {return_reason}) from "
    "{operator_name} for store #{store_number} {store_name}. It had been kept as a credit of "
    "{amount} on the {lease}; that credit is reversed.\n"
    "Charges owed again: {charges_owed}\n"
    "The lease is flagged Needs review. Open the store: {store_link}"
)
#: (code, name, internal, subject, body).
TEMPLATES: tuple[tuple[str, str, bool, str, str], ...] = (
    (
        "credit_returned",
        "Credit reversed: returned debit (internal)",
        True,
        "Credit reversed: #{store_number} {store_name} — {amount}",
        _INTERNAL_BODY,
    ),
    (
        "credit_returned_operator",
        "Credit reversed: returned debit",
        False,
        "Your payment for #{store_number} {store_name} was returned",
        _OPERATOR_BODY,
    ),
)
SEEDED_CODES: tuple[str, ...] = tuple(code for code, *_ in TEMPLATES)
NEW_RULES = ("credit_returned", "credit_returned_operator")


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
    _replace_trigger(schema_editor, CREDITS, _CREDITS_BEFORE)
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def link_split_rows(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Link each held row split off a payment before this migration to that payment."""
    audit: Any = apps.get_model("ledger", "AuditLog")
    payment: Any = apps.get_model("ledger", "Payment")
    rows = audit.objects.filter(action=HELD_ACTION, entity_type="payment").order_by("at", "id")
    for entity_id, after in rows.values_list("entity_id", "after"):
        held_id = str((after or {}).get("held_payment") or "")
        if not held_id or held_id == entity_id:
            continue  # the whole payment was re-pointed: nothing was split off it
        if not payment.objects.filter(pk=entity_id).exists():
            continue
        payment.objects.filter(
            pk=held_id, split_from__isnull=True, credit__isnull=False, kind="rent"
        ).update(split_from_id=entity_id)
    run_pending_checks(apps, schema_editor)


def seed_templates(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    for code, name, internal, subject, body in TEMPLATES:
        template.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "internal": internal,
                "subject": subject,
                "body": body,
                "channel": "email",
                "active": True,
            },
        )


def unseed_templates(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("ledger", "MessageTemplate").objects.filter(code__in=SEEDED_CODES).delete()
    run_pending_checks(apps, schema_editor)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if (
        apps.get_model("ledger", "LeaseCredit").objects.filter(reversed_at__isnull=False).exists()
        or apps.get_model("ledger", "Lease")
        .objects.filter(review_reason="credit_returned")
        .exists()
        or apps.get_model("ledger", "OutboundMessage").objects.filter(rule__in=NEW_RULES).exists()
    ):
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0076_one_off_charge_dated"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(
            model_name="lease",
            name="leases_review_reason_valid",
        ),
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
            model_name="leasecredit",
            name="reversed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasecredit",
            name="reversed_reason",
            field=models.TextField(
                blank=True, default="", validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="split_from",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="split_rows",
                to="ledger.payment",
            ),
        ),
        migrations.AlterField(
            model_name="lease",
            name="review_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("wire", "Pays by wire"),
                    ("dormant", "Dormant"),
                    ("closed", "Closed"),
                    ("unknown", "Unknown"),
                    ("own_customer", "Own processor customer needed"),
                    ("returned_debit", "Returned debit: new payment method needed"),
                    ("redebit_limit", "Re-debit limit reached (Nacha)"),
                    ("credit_returned", "Returned debit: credit reversed"),
                ],
                default="",
                max_length=32,
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
                    ("lease_credit", "Lease credit"),
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
                    ("credit_returned", "Credit reversed"),
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
                    ("credit_returned", "Credit reversed (internal)"),
                    ("credit_returned_operator", "Credit reversed"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="lease",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "review_reason__in",
                        [
                            "wire",
                            "dormant",
                            "closed",
                            "unknown",
                            "own_customer",
                            "returned_debit",
                            "redebit_limit",
                            "credit_returned",
                            "",
                        ],
                    )
                ),
                name="leases_review_reason_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasecredit",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reversed_at__isnull", True), ("remaining", 0), _connector="OR"
                ),
                name="lease_credits_reversed_holds_nothing",
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
                            "lease_credit",
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
                    (
                        "kind__in",
                        [
                            "ladder",
                            "final_statement",
                            "portal",
                            "invoice",
                            "receipt",
                            "credit_returned",
                        ],
                    )
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
                            "credit_returned",
                            "credit_returned_operator",
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
                    models.Q(
                        models.Q(
                            ("charge_entity_type", "lease_credit"),
                            ("kind", "credit_returned"),
                            ("rule__in", ("credit_returned", "credit_returned_operator")),
                        ),
                        models.Q(
                            models.Q(("kind", "credit_returned"), _negated=True),
                            models.Q(("charge_entity_type", "lease_credit"), _negated=True),
                            models.Q(
                                ("rule__in", ("credit_returned", "credit_returned_operator")),
                                _negated=True,
                            ),
                        ),
                        _connector="OR",
                    ),
                ),
                name="outbound_messages_kind_matches_rule",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("split_from__isnull", True),
                    models.Q(("credit__isnull", False), ("is_refund", False), ("kind", "rent")),
                    _connector="OR",
                ),
                name="payments_split_from_held_row",
            ),
        ),
        migrations.RunPython(create_guards, restore_guards, elidable=False),
        migrations.RunPython(link_split_rows, migrations.RunPython.noop, elidable=False),
        migrations.RunPython(seed_templates, unseed_templates, elidable=False),
        # Last, so that the reverse checks the rows first.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
