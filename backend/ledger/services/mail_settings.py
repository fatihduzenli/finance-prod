"""Admin › Settings › Mail (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md D1,
D2): the company's sender, Reply-To, managers' list and sandbox allowlist.

:func:`save` (admin) writes the four fields of the company settings row: each address blank
or one plain address, each list one address per line (blank lines ignored, lower-cased, each
once; :func:`mail.address_lines`). A refusal is a sentence naming the field, never the value,
and writes nothing. A change is audited ``company_settings.updated`` with the fields before
and after, as the Django admin audited the row.

:func:`status` says whether mail leaves the app here, from the presence of the
service-account key (never its value), ``MAIL_ENABLED`` and the sender address.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

import mail
from ledger.models import AuditLog, CompanySettings
from ledger.validators import validate_no_payment_data
from mail.base import ADDRESS_LINES_MAX, ADDRESS_LINES_TOO_MANY, is_address

ENTITY = "company_settings"
FIELDS: tuple[str, ...] = ("sender_email", "reply_to_email", "manager_emails", "sandbox_allowlist")
LABELS: dict[str, str] = {
    "sender_email": "Send as",
    "reply_to_email": "Reply-To",
    "manager_emails": "Managers",
    "sandbox_allowlist": "Sandbox allowlist",
}
ADDRESS_REFUSED = "{label}: write one plain e-mail address, or leave it empty."
LIST_REFUSED = "{label}: write one plain e-mail address per line."
LIST_TOO_LONG = "{label}: a list holds at most {count} addresses."
PAYMENT_DATA = "{label} cannot hold a card or account number."
SAVED = "Mail settings saved."
UNCHANGED = "Nothing changed."

SET_UP = "Mail: set up."
NOT_SET_UP_NO_KEY = "Mail: not set up (no key in this environment)."
NOT_SET_UP_OFF = "Mail: not set up (mail is turned off in this environment)."
NOT_SET_UP_NO_SENDER = "Mail: not set up (no Send as address)."


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str


def status() -> str:
    """One sentence: whether mail leaves the app here, and if not, why."""
    from django.conf import settings

    if not mail.key_present():
        return NOT_SET_UP_NO_KEY
    if not settings.MAIL_ENABLED:
        return NOT_SET_UP_OFF
    if not mail.mail_settings().sender_email:
        return NOT_SET_UP_NO_SENDER
    return SET_UP


def _text(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


def clean(data: Mapping[str, object]) -> tuple[dict[str, str], str]:
    """The four fields as stored, and ``""``; or ``{}`` and the refusal."""
    found: dict[str, str] = {}
    for name in ("sender_email", "reply_to_email"):
        address = _text(data.get(name)).strip().lower()
        if address and not is_address(address):
            return {}, ADDRESS_REFUSED.format(label=LABELS[name])
        found[name] = address
    for name in ("manager_emails", "sandbox_allowlist"):
        try:
            found[name] = "\n".join(mail.address_lines(_text(data.get(name))))
        except ValueError as error:
            if str(error) == ADDRESS_LINES_TOO_MANY:
                return {}, LIST_TOO_LONG.format(label=LABELS[name], count=ADDRESS_LINES_MAX)
            return {}, LIST_REFUSED.format(label=LABELS[name])
    for name, value in found.items():
        try:
            validate_no_payment_data(value)
        except ValidationError:
            return {}, PAYMENT_DATA.format(label=LABELS[name])
    return found, ""


def save(data: Mapping[str, object], actor: User) -> Outcome:
    """Write the Mail card's fields (see the module docstring)."""
    found, refusal = clean(data)
    if refusal:
        return Outcome(False, refusal)
    with transaction.atomic():
        row = CompanySettings.current()
        row = CompanySettings.objects.select_for_update().get(pk=row.pk)
        before = {name: getattr(row, name) for name in FIELDS}
        if before == found:
            return Outcome(True, UNCHANGED)
        for name, value in found.items():
            setattr(row, name, value)
        row.save(update_fields=[*FIELDS, "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action=f"{ENTITY}.updated",
            entity_type=ENTITY,
            entity_id=str(row.pk),
            before=before,
            after=found,
        )
    return Outcome(True, SAVED)
