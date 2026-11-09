"""The invoice e-mail links the portal only (docs/qa/15/PLAN-015g-net-rent-line-pay-link.md §3B;
TODO-VERIFY-076, docs/CLAUDE.md Rule 1): the processor's hosted invoice page offered card
payment, so ``{provider_pay_link}`` stops being a placeholder of ``invoice_sent``.

Data only, on ``message_templates``: the ``invoice_sent`` body loses every line that names
``{provider_pay_link}`` (the seeded "Or pay on our payment page: {provider_pay_link}" or an
admin's own wording), and its subject loses the placeholder's text (not seeded so; possible by an
edit), since a text still naming it would no longer validate. When an admin's text changed, one
``audit_log`` row ``message_template.provider_pay_line_removed`` holds the subject and the body
before and after. The seeded text (``0071``'s, untouched) changes with no row: a fresh database
holds it, and the suite's "writes nothing" tests read an empty audit log (the lesson of
``0068``). A template that does not name the placeholder is left alone with no row. A template whose
body or subject would be left empty is not touched: the row
``message_template.provider_pay_line_kept`` says so (``{"reason": "would_be_empty"}``), and the
template fails validation at the send until an admin edits it. No schema change, so no
payment-data trigger changes (an empty ``GUARDED_COLUMNS``).

The reverse is best-effort for an edited body: when the body has the line
``Pay by bank debit (ACH) here: {pay_link}`` and no line naming the placeholder, the seeded line
is put back directly after it; any other body, and the subject, are left as they are.
"""

from __future__ import annotations

import importlib
import string

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
CODE = "invoice_sent"
NAME = "provider_pay_link"
PLACEHOLDER = "{" + NAME + "}"
PORTAL_LINE = "Pay by bank debit (ACH) here: {pay_link}"
SEEDED_LINE = "Or pay on our payment page: " + PLACEHOLDER
REMOVED = "message_template.provider_pay_line_removed"
KEPT = "message_template.provider_pay_line_kept"
#: ``0071``'s seeded (subject, body) of the invoice e-mail.
SEEDED: tuple[str, str] = next(
    (subject, body)
    for code, _, subject, body in importlib.import_module(
        "ledger.migrations.0071_invoice_pdfs"
    ).TEMPLATES
    if code == CODE
)


def names_placeholder(line: str) -> bool:
    """Whether ``line`` names ``{provider_pay_link}`` as a placeholder (a literal ``{{...}}``
    does not); a line whose braces do not pair is read as text."""
    try:
        return any(name == NAME for _, name, _, _ in string.Formatter().parse(line))
    except ValueError:
        return PLACEHOLDER in line


def without_provider_pay_line(body: str) -> str:
    """``body`` without each line that names ``{provider_pay_link}``."""
    return "\n".join(line for line in body.split("\n") if not names_placeholder(line))


def without_placeholder(subject: str) -> str:
    """``subject`` without the placeholder's text, its spaces closed up."""
    if not names_placeholder(subject):
        return subject
    return " ".join(subject.replace(PLACEHOLDER, "").split())


def with_provider_pay_line(body: str) -> str:
    """The reverse: the seeded line back after the portal line, when the body has that line and
    names the placeholder nowhere; ``body`` as it is otherwise."""
    lines = body.split("\n")
    if PORTAL_LINE not in lines or any(names_placeholder(line) for line in lines):
        return body
    at = lines.index(PORTAL_LINE) + 1
    return "\n".join([*lines[:at], SEEDED_LINE, *lines[at:]])


def remove_provider_pay_line(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    audit_log = apps.get_model("ledger", "AuditLog")
    found = template.objects.filter(code=CODE).first()
    if found is None:
        return
    body, subject = without_provider_pay_line(found.body), without_placeholder(found.subject)
    if (body, subject) == (found.body, found.subject):
        return
    if not body.strip() or not subject.strip():
        audit_log.objects.create(
            actor=None,
            action=KEPT,
            entity_type="message_template",
            entity_id=str(found.pk),
            before=None,
            after={"code": CODE, "reason": "would_be_empty"},
        )
        return
    template.objects.filter(pk=found.pk).update(body=body, subject=subject)
    if (found.subject, found.body) == SEEDED:
        return
    audit_log.objects.create(
        actor=None,
        action=REMOVED,
        entity_type="message_template",
        entity_id=str(found.pk),
        before={"code": CODE, "subject": found.subject, "body": found.body},
        after={"code": CODE, "subject": subject, "body": body},
    )


def restore_provider_pay_line(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    template = apps.get_model("ledger", "MessageTemplate")
    found = template.objects.filter(code=CODE).first()
    if found is None:
        return
    body = with_provider_pay_line(found.body)
    if body != found.body:
        template.objects.filter(pk=found.pk).update(body=body)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0077_credit_return"),
    ]

    operations = [
        migrations.RunPython(remove_provider_pay_line, restore_provider_pay_line, elidable=False),
    ]
