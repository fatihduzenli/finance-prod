"""The messaging engine (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md D1 to
D6).

Schema:

- ``company_settings`` (D1, D2): ``sender_email`` and ``reply_to_email`` (addresses, blank),
  ``manager_emails`` and ``sandbox_allowlist`` (text, one address per line, blank). Nothing is
  seeded: no address is written in source (§8).
- ``message_templates`` (D4): ``name`` (at most 80 characters, blank), ``channel`` (``email``
  or ``sms``, default ``email``, CHECK), ``active`` (default true) and ``internal`` (default
  false).
- ``outbound_messages`` (D3): ``subject`` becomes ``rendered_subject``, and the table gains
  ``rendered_body`` (text), ``channel`` (CHECK), ``rule`` (CHECK: ``A`` to ``F``,
  ``final_statement``, ``manual`` or ``ladder``), ``occurrence`` (at least 1),
  ``held_reason`` (CHECK; set exactly when the status is the new ``held``) and
  ``scheduled_for`` (timestamp, null), and two indexes (by lease and by time, for the
  timeline and the Outbox). ``charge_entity_type`` gains ``insurance_charge``. The unique
  constraint becomes one row per (lease, charge, rule, occurrence) unless ``manual``. The
  column ``step`` and the CHECK ``outbound_messages_step_iff_ladder`` go: a message about a
  charge names a charge type and a rule other than ``final_statement``; a final statement
  names a lease end and the rule ``final_statement`` (``outbound_messages_kind_matches_rule``).
- ``collection_steps`` is dropped (D6).

Data, forwards: every message of the ladder gets the rule ``ladder`` and, as its occurrence,
its step's day (so the old one-per-charge-and-step rule holds in the new constraint); every
final statement the rule ``final_statement``. The three ladder templates
(``friendly_reminder``, ``formal_notice``, ``final_notice``) are made inactive (kept for the
history of their messages) and every template is named; the seven templates of plan §9 are
seeded byte for byte (left as they are when the code exists).

Free text is guarded: on PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this
transaction queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), in either
direction, and the payment-data triggers of ``company_settings`` (``0045``),
``message_templates`` and ``outbound_messages`` (``0024``) are recreated with the new and
renamed columns; the reverse restores them. Nothing is created on other vendors; the
application validator applies there.

The reverse refuses (``CANNOT_REVERSE``) while a message carries a rule other than the old
ladder's or a final statement's, or is ``held``; otherwise it recreates ``collection_steps``
with the three seeded steps (and a step for any other day an old message names, with that
message's template), points each old message back at its step, makes the three old
templates active again and deletes the seven seeded templates. The stored bodies and the
new columns are lost.
"""

from __future__ import annotations

import importlib
from typing import Any

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

import ledger.validators

_GUARDS_0024 = importlib.import_module("ledger.migrations.0024_collections_ladder")
_GUARDS_0045 = importlib.import_module("ledger.migrations.0045_company_settings")

#: Table -> guarded columns after this migration. Replaces the ``message_templates`` and
#: ``outbound_messages`` entries of ``0024`` and the ``company_settings`` entry of ``0045``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    "company_settings": (
        *_GUARDS_0045.GUARDED_COLUMNS["company_settings"],
        "sender_email",
        "reply_to_email",
        "manager_emails",
        "sandbox_allowlist",
    ),
    "message_templates": (*_GUARDS_0024.GUARDED_COLUMNS["message_templates"], "name"),
    "outbound_messages": ("to_address", "rendered_subject", "rendered_body", "error"),
}
_BEFORE: dict[str, tuple[str, ...]] = {
    "company_settings": _GUARDS_0045.GUARDED_COLUMNS["company_settings"],
    "message_templates": _GUARDS_0024.GUARDED_COLUMNS["message_templates"],
    "outbound_messages": _GUARDS_0024.GUARDED_COLUMNS["outbound_messages"],
}

#: The ladder's templates, made inactive (D6), with the names the list shows.
OLD_TEMPLATES: dict[str, str] = {
    "friendly_reminder": "Old ladder — friendly reminder",
    "formal_notice": "Old ladder — formal notice",
    "final_notice": "Old ladder — final notice",
}
#: The ladder's seeded steps (``0025``): (day, template code, position).
OLD_STEPS: tuple[tuple[int, str, int], ...] = (
    (3, "friendly_reminder", 1),
    (10, "formal_notice", 2),
    (20, "final_notice", 3),
)
FINAL_STATEMENT = ("final_statement", "Final statement")
OLD_RULES = ("ladder", "final_statement")
#: The models whose historical shape the data steps read by name: the steps' model is gone
#: from the code, and the messages' ``step`` column exists only on one side of this
#: migration.
STEP_MODEL = "CollectionStep"
MESSAGE_MODEL = "OutboundMessage"
CANNOT_REVERSE = (
    "Messages of the new rules, or held messages, exist; this migration refuses to reverse "
    "(the collections ladder has no step or status for them)."
)

_F_RETRY = (
    "If we do not hear from you within 2 business days, the payment of {amount} will be "
    "debited again automatically on {retry_date}. To change this, contact us at "
    "{support_email} before then."
)
_F_NO_RETRY = "Please contact us within 2 business days to set up a new payment method."
_F_BODY = (
    "Hello {operator_name},\n"
    "The bank debit of {amount} for {charge_name} at store #{store_number} {store_name}, "
    "made on {debit_date}, was returned by your bank.\n"
    "Reason: {return_reason}\n"
    "A returned-payment (NSF) fee of $100.00 has been added to your account. The original "
    "amount is still owed.\n"
    f"{_F_RETRY}\n"
    "{company_name}"
)

#: (code, name, internal, subject, body) of the seeded templates: plan §9, byte for byte.
TEMPLATES: tuple[tuple[str, str, bool, str, str], ...] = (
    (
        "reminder_autopay",
        "A — Rent will be debited",
        False,
        "Rent for #{store_number} {store_name} will be debited on {debit_date}",
        "Hello {operator_name},\n"
        "This is a reminder that your rent of {amount} for store #{store_number} {store_name} "
        "will be debited from your bank account ending in {bank_last4} on {debit_date}.\n"
        "Please make sure the funds are available. If anything has changed with your "
        "account, reply to this e-mail before {debit_date}.\n"
        "Thank you,\n"
        "{company_name}",
    ),
    (
        "reminder_due",
        "B — Payment due",
        False,
        "Payment due {due_date} — #{store_number} {store_name}",
        "Hello {operator_name},\n"
        "A payment of {amount} for {charge_name} at store #{store_number} {store_name} is due "
        "on {due_date}.\n"
        "You can pay here: {pay_link}\n"
        "If you have already paid, thank you, and please ignore this message.\n"
        "{company_name}",
    ),
    (
        "overdue_day5",
        "C — Payment overdue",
        False,
        "Payment overdue — #{store_number} {store_name}",
        "Hello {operator_name},\n"
        "We have not yet received {amount} for {charge_name} at store #{store_number} "
        "{store_name}, which was due on {due_date}.\n"
        "Please pay as soon as possible: {pay_link}. If you need more time, reply to this "
        "e-mail and let us know when we can expect the payment.\n"
        "{company_name}",
    ),
    (
        "overdue_day10",
        "D — Notice: overdue",
        False,
        "Notice: {amount} overdue — #{store_number} {store_name}",
        "Hello {operator_name},\n"
        "The payment of {amount} for {charge_name} at store #{store_number} {store_name} is "
        "now {days_late} days overdue (due {due_date}).\n"
        "Please pay today at {pay_link} or contact us at {support_email} to arrange payment. "
        "Continued non-payment may affect your lease.\n"
        "{company_name}",
    ),
    (
        "internal_day30",
        "E — 30 days overdue (internal)",
        True,
        "30 days overdue: #{store_number} {store_name} — {amount}",
        "{operator_name} has not paid {amount} for {charge_name} (due {due_date}). Reminders "
        "were sent on day 5 and day 10. Open the store: {store_link}",
    ),
    (
        "returned_debit",
        "F — Payment returned",
        False,
        "Your payment for #{store_number} {store_name} was returned",
        _F_BODY,
    ),
    (
        "returned_debit_no_retry",
        "F — Payment returned (no retry)",
        False,
        "Your payment for #{store_number} {store_name} was returned",
        _F_BODY.replace(_F_RETRY, _F_NO_RETRY),
    ),
)
SEEDED_CODES: tuple[str, ...] = tuple(code for code, *_ in TEMPLATES)


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


def guard_new_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        _replace_trigger(schema_editor, table, columns)


def unguard_new_columns(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in _BEFORE.items():
        _replace_trigger(schema_editor, table, columns)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


# --- Rules of the existing messages -------------------------------------------------------


def set_rules(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    message: Any = apps.get_model("ledger", MESSAGE_MODEL)
    message.objects.filter(kind="final_statement").update(rule="final_statement", occurrence=1)
    for row in message.objects.filter(kind="ladder").select_related("step"):
        row.rule = "ladder"
        row.occurrence = row.step.day_offset if row.step is not None else 1
        row.save(update_fields=["rule", "occurrence"])
    run_pending_checks(apps, schema_editor)


def restore_steps(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    message: Any = apps.get_model("ledger", MESSAGE_MODEL)
    step: Any = apps.get_model("ledger", STEP_MODEL)
    steps = {row.day_offset: row for row in step.objects.all()}
    for row in message.objects.filter(rule="ladder"):
        row.step = steps[row.occurrence]
        row.save(update_fields=["step"])
    run_pending_checks(apps, schema_editor)


# --- The ladder retires; the seven templates ------------------------------------------------


def retire_ladder(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    for code, name in OLD_TEMPLATES.items():
        template.objects.filter(code=code).update(active=False, name=name)
    template.objects.filter(code=FINAL_STATEMENT[0], name="").update(name=FINAL_STATEMENT[1])
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
    run_pending_checks(apps, schema_editor)


def restore_ladder(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    message = apps.get_model("ledger", "OutboundMessage")
    if (
        message.objects.exclude(rule__in=OLD_RULES).exists()
        or message.objects.filter(status="held").exists()
    ):
        raise RuntimeError(CANNOT_REVERSE)
    template = apps.get_model("ledger", "MessageTemplate")
    step: Any = apps.get_model("ledger", STEP_MODEL)
    template.objects.filter(code__in=OLD_TEMPLATES).update(active=True)
    templates = {row.code: row for row in template.objects.filter(code__in=OLD_TEMPLATES)}
    for day_offset, code, position in OLD_STEPS:
        if code in templates:
            step.objects.get_or_create(
                day_offset=day_offset,
                defaults={"template": templates[code], "position": position},
            )
    # A step an admin added (another day) comes back with the template its messages used.
    others: dict[int, Any] = {}
    for row in message.objects.filter(rule="ladder").order_by("queued_at"):
        others.setdefault(row.occurrence, row.template_id)
    for day_offset, template_id in others.items():
        step.objects.get_or_create(
            day_offset=day_offset, defaults={"template_id": template_id, "position": day_offset}
        )
    used = set(message.objects.values_list("template_id", flat=True))
    template.objects.filter(code__in=SEEDED_CODES).exclude(pk__in=used).delete()
    run_pending_checks(apps, schema_editor)


def _choice(choices: list[tuple[str, str]], **kwargs: Any) -> models.CharField[Any, Any]:
    return models.CharField(max_length=32, choices=choices, **kwargs)


STATUS_CHOICES = [
    ("queued", "Waiting"),
    ("sent", "Sent"),
    ("failed", "Failed"),
    ("suppressed", "Suppressed"),
    ("catch_all", "Sent to the test inbox"),
    ("held", "Held"),
]
CHARGE_TYPE_CHOICES = [
    ("rent_charge", "Rent charge"),
    ("one_off_charge", "One-off charge"),
    ("lease_end", "Lease end"),
    ("insurance_charge", "Insurance charge"),
]
CHANNEL_CHOICES = [("email", "E-mail"), ("sms", "SMS")]
RULE_CHOICES = [
    ("A", "A — rent will be debited"),
    ("B", "B — payment due"),
    ("C", "C — payment overdue"),
    ("D", "D — notice: overdue"),
    ("E", "E — 30 days overdue (internal)"),
    ("F", "F — payment returned"),
    ("final_statement", "Final statement"),
    ("manual", "Sent by hand"),
    ("ladder", "Old collections ladder"),
]
HELD_CHOICES = [
    ("not_set_up", "not sent (mail not set up)"),
    ("sandbox", "held on sandbox"),
    ("no_address", "no billing e-mail"),
    ("no_manager_list", "no manager list"),
    ("handled_personally", "operator handled personally"),
]
KIND_CHOICES = [("ladder", "Charge message"), ("final_statement", "Final statement")]


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0060_transfer_incoming_billing"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        # --- company_settings (D1, D2) ---
        migrations.AddField(
            model_name="companysettings",
            name="sender_email",
            field=models.EmailField(
                blank=True,
                default="",
                max_length=254,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="reply_to_email",
            field=models.EmailField(
                blank=True,
                default="",
                max_length=254,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="manager_emails",
            field=models.TextField(
                blank=True, default="", validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddField(
            model_name="companysettings",
            name="sandbox_allowlist",
            field=models.TextField(
                blank=True, default="", validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        # --- message_templates (D4) ---
        migrations.AddField(
            model_name="messagetemplate",
            name="name",
            field=models.CharField(
                blank=True,
                default="",
                max_length=80,
                validators=[ledger.validators.validate_no_payment_data],
            ),
        ),
        migrations.AddField(
            model_name="messagetemplate",
            name="channel",
            field=_choice(CHANNEL_CHOICES, default="email"),
        ),
        migrations.AddField(
            model_name="messagetemplate",
            name="active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="messagetemplate",
            name="internal",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="messagetemplate",
            constraint=models.CheckConstraint(
                condition=Q(channel__in=["email", "sms"]), name="message_templates_channel_valid"
            ),
        ),
        # --- outbound_messages (D3) ---
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="outbound_messages_step_iff_ladder"
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="outbound_messages_one_per_charge_step"
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="outbound_messages_status_valid"
        ),
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="outbound_messages_charge_entity_type_valid"
        ),
        migrations.RenameField(
            model_name="outboundmessage", old_name="subject", new_name="rendered_subject"
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="rendered_body",
            field=models.TextField(
                blank=True, validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="channel",
            field=_choice(CHANNEL_CHOICES, default="email"),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="rule",
            field=_choice(RULE_CHOICES, default="ladder"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="occurrence",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="held_reason",
            field=_choice(HELD_CHOICES, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="outboundmessage",
            name="scheduled_for",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="status",
            field=_choice(STATUS_CHOICES, default="queued"),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="charge_entity_type",
            field=_choice(CHARGE_TYPE_CHOICES),
        ),
        migrations.AlterField(
            model_name="outboundmessage",
            name="kind",
            field=_choice(KIND_CHOICES, default="ladder"),
        ),
        migrations.RunPython(guard_new_columns, unguard_new_columns, elidable=False),
        migrations.RunPython(set_rules, restore_steps, elidable=False),
        migrations.RemoveField(model_name="outboundmessage", name="step"),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(status__in=[value for value, _ in STATUS_CHOICES]),
                name="outbound_messages_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(charge_entity_type__in=[value for value, _ in CHARGE_TYPE_CHOICES]),
                name="outbound_messages_charge_entity_type_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(channel__in=["email", "sms"]), name="outbound_messages_channel_valid"
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(rule__in=[value for value, _ in RULE_CHOICES]),
                name="outbound_messages_rule_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(held_reason__in=[*(value for value, _ in HELD_CHOICES), ""]),
                name="outbound_messages_held_reason_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=(
                    ~Q(kind="ladder")
                    | (
                        Q(
                            charge_entity_type__in=[
                                "rent_charge",
                                "one_off_charge",
                                "insurance_charge",
                            ]
                        )
                        & ~Q(rule="final_statement")
                    )
                )
                & (
                    ~Q(kind="final_statement")
                    | Q(charge_entity_type="lease_end", rule="final_statement")
                ),
                name="outbound_messages_kind_matches_rule",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=Q(occurrence__gte=1), name="outbound_messages_occurrence_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=(Q(status="held") & ~Q(held_reason=""))
                | (~Q(status="held") & Q(held_reason="")),
                name="outbound_messages_held_has_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.UniqueConstraint(
                condition=Q(manual=False),
                fields=("lease", "charge_entity_type", "charge_entity_id", "rule", "occurrence"),
                name="outbound_messages_one_per_lease_charge_rule",
            ),
        ),
        migrations.AddIndex(
            model_name="outboundmessage",
            index=models.Index(fields=["lease", "queued_at"], name="outbound_messages_lease_idx"),
        ),
        migrations.AddIndex(
            model_name="outboundmessage",
            index=models.Index(fields=["queued_at"], name="outbound_messages_queued_idx"),
        ),
        # --- the ladder retires (D6) ---
        migrations.RunPython(retire_ladder, restore_ladder, elidable=False),
        migrations.DeleteModel(name="CollectionStep"),
        # Reversed first: any check queued runs before the ALTER TABLE statements.
        migrations.RunPython(migrations.RunPython.noop, run_pending_checks, elidable=False),
    ]
