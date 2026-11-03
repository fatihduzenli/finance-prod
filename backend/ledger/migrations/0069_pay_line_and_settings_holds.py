"""D's payment sentence for an ended lease, and no message with an empty placeholder (owner,
2026-11-02).

Schema, on ``outbound_messages``: two held reasons join the closed list, ``settings_incomplete``
(the company name or the support e-mail is blank: every operator-facing message is held) and
``missing_value`` (a placeholder the text names has no value); the held-reason CHECK is recreated
with them. No text column is added, so no payment-data trigger changes (an empty
``GUARDED_COLUMNS``).

Data, on ``message_templates``: the body of D (``overdue_day10``) gets the placeholder
``{pay_line}`` in place of the text ``0061`` seeded and ``0068`` gave its ``{lease_line}``,
"Please pay today at {pay_link} or contact us at {support_email} to arrange payment.
{lease_line}", **only when the body still holds that exact text**; a body an admin edited
without it is left alone, and only then an ``audit_log`` row says so
(``message_template.pay_line_skipped`` with ``{"reason": "edited"}``; no text of the body is
copied into it). The seeded case writes no audit row (the lesson of ``0068``'s gate).

The reverse writes the text back wherever a template body holds ``{pay_line}`` (the code before
this migration does not know it), turns every row held for one of the two new reasons into a
row held ``not_set_up`` (held all the same, released by *Send now*), then recreates the CHECK
without them. On PostgreSQL any check queued earlier in the transaction runs before the first
``ALTER TABLE`` (the lesson of ``0018``).
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
D_CODE = "overdue_day10"
SEEDED = (
    "Please pay today at {pay_link} or contact us at {support_email} to arrange payment. "
    "{lease_line}"
)
PLACEHOLDER = "{pay_line}"
SKIPPED = "message_template.pay_line_skipped"
NEW_REASONS = ("settings_incomplete", "missing_value")
#: What a row held for a new reason becomes on the reverse.
HELD_BACK = "not_set_up"

HELD_BEFORE = [
    ("not_set_up", "not sent (mail not set up)"),
    ("sandbox", "held on sandbox"),
    ("no_address", "no billing e-mail"),
    ("no_manager_list", "no manager list"),
    ("handled_personally", "operator handled personally"),
]
HELD_AFTER = [
    *HELD_BEFORE,
    ("settings_incomplete", "settings incomplete"),
    ("missing_value", "missing value"),
]


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def unhold(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """The reverse: no row may keep a held reason the code before this migration does not
    know."""
    message = apps.get_model("ledger", "OutboundMessage")
    message.objects.filter(held_reason__in=NEW_REASONS).update(held_reason=HELD_BACK)


def add_pay_line(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    audit_log = apps.get_model("ledger", "AuditLog")
    found = template.objects.filter(code=D_CODE).first()
    if found is None:
        return
    if SEEDED in found.body:
        template.objects.filter(pk=found.pk).update(body=found.body.replace(SEEDED, PLACEHOLDER))
        return
    audit_log.objects.create(
        actor=None,
        action=SKIPPED,
        entity_type="message_template",
        entity_id=str(found.pk),
        before=None,
        after={"code": D_CODE, "reason": "edited"},
    )


def remove_pay_line(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    for pk, body in list(
        template.objects.filter(body__contains=PLACEHOLDER).values_list("pk", "body")
    ):
        template.objects.filter(pk=pk).update(body=body.replace(PLACEHOLDER, SEEDED))


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0068_combined_messages_and_lease_line"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        # Forward nothing; on the reverse it runs here, before the old CHECK comes back.
        migrations.RemoveConstraint(
            model_name="outboundmessage",
            name="outbound_messages_held_reason_valid",
        ),
        migrations.RunPython(migrations.RunPython.noop, unhold, elidable=False),
        migrations.AlterField(
            model_name="outboundmessage",
            name="held_reason",
            field=models.CharField(blank=True, choices=HELD_AFTER, default="", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.CheckConstraint(
                condition=models.Q(held_reason__in=[*(value for value, _ in HELD_AFTER), ""]),
                name="outbound_messages_held_reason_valid",
            ),
        ),
        migrations.RunPython(add_pay_line, remove_pay_line, elidable=False),
    ]
