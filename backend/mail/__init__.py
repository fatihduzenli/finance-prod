"""Outbound e-mail (docs/qa/PLAN-011b D3, PLAN-014 D1 and D2).

The one package that talks to Gmail; nothing outside it imports ``google.*``
(``backend/tests/test_mail.py``). The rest of the app builds a :class:`Message` and
calls ``get_sender().send(message)``, which returns a :class:`SentReceipt` (``sent``, or
``held`` with its reason) or raises :class:`MailUnavailable` (a class name, never a
message).

:func:`get_sender` returns, for the current settings:

- :class:`~mail.outbox.OutboxSender` when mail is not set up (:func:`is_set_up` false:
  ``MAIL_ENABLED`` off, no ``GMAIL_SERVICE_ACCOUNT_JSON``, or no sender address in the
  company settings): nothing is delivered and every message is held (``not_set_up``);
- otherwise the Gmail sender (:class:`~mail.gmail.GmailApiSender`, one per process and
  sender, built on first use from the key, sending as the company settings'
  ``sender_email`` with its ``reply_to_email``), wrapped, when ``STAX_ENV`` is
  ``sandbox``, in :class:`~mail.outbox.AllowlistSender`: a recipient off the company
  settings' ``sandbox_allowlist`` is held (``sandbox``). A sandbox can never mail anyone
  else.

Imports are made inside the functions so that loading this package touches neither
google-auth, the settings nor the database.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass

from mail.base import (
    HELD,
    NOT_SET_UP,
    SANDBOX,
    SENT,
    Attachment,
    MailSender,
    MailUnavailable,
    Message,
    SentReceipt,
    address_lines,
)

__all__ = [
    "HELD",
    "NOT_SET_UP",
    "SANDBOX",
    "SENT",
    "Attachment",
    "MailSender",
    "MailUnavailable",
    "Message",
    "SentReceipt",
    "address_lines",
    "get_sender",
    "is_set_up",
    "mail_settings",
]

#: The ``STAX_ENV`` of the sandbox environment, where only the allowlist is reached.
SANDBOX_ENV = "sandbox"

_lock = threading.Lock()
_senders: dict[str, MailSender] = {}


@dataclass(frozen=True, slots=True)
class MailSettings:
    """The company settings' mail fields (PLAN-014 D1); the lists as parsed addresses."""

    sender_email: str
    reply_to_email: str
    sandbox_allowlist: tuple[str, ...]


def _lines(text: str) -> tuple[str, ...]:
    """The addresses of a stored list; an unreadable list (never saved so) holds none."""
    try:
        return tuple(address_lines(text))
    except ValueError:
        return ()


def mail_settings() -> MailSettings:
    """The company settings' mail fields, read now (one query)."""
    from ledger.models import CompanySettings

    row = CompanySettings.read()
    return MailSettings(
        sender_email=str(row.sender_email or "").strip().lower(),
        reply_to_email=str(row.reply_to_email or "").strip().lower(),
        sandbox_allowlist=_lines(str(row.sandbox_allowlist or "")),
    )


def key_present() -> bool:
    """Whether this environment holds the service-account key (never its value)."""
    from django.conf import settings

    return bool(settings.GMAIL_SERVICE_ACCOUNT_JSON)


def is_set_up(found: MailSettings | None = None) -> bool:
    """Mail leaves the app: ``MAIL_ENABLED``, the key, and a sender address."""
    from django.conf import settings

    if not settings.MAIL_ENABLED or not key_present():
        return False
    return bool((found or mail_settings()).sender_email)


def on_sandbox() -> bool:
    from django.conf import settings

    return bool(settings.STAX_ENV == SANDBOX_ENV)


def get_sender() -> MailSender:
    """The sender for the current settings (see the module docstring)."""
    from mail.outbox import AllowlistSender, OutboxSender

    found = mail_settings()
    if not is_set_up(found):
        return OutboxSender()
    base = _base_sender()
    return AllowlistSender(base, found.sandbox_allowlist) if on_sandbox() else base


def _base_sender() -> MailSender:
    """The process's Gmail sender for the configured key, sender and Reply-To, built once."""
    from django.conf import settings

    found = mail_settings()
    raw: str = settings.GMAIL_SERVICE_ACCOUNT_JSON
    key = hashlib.sha256(
        f"{found.sender_email}\n{found.reply_to_email}\n{raw}".encode()
    ).hexdigest()
    with _lock:
        sender = _senders.get(key)
        if sender is None:
            sender = _build(raw, found.sender_email, found.reply_to_email)
            _senders.clear()
            _senders[key] = sender
        return sender


def _build(raw: str, sender: str, reply_to: str) -> MailSender:
    from mail.gmail import GmailApiSender

    try:
        info = json.loads(raw)
    except ValueError:
        raise MailUnavailable("KeyInvalid") from None
    if not isinstance(info, dict):
        raise MailUnavailable("KeyInvalid")
    return GmailApiSender(info, sender, reply_to=reply_to)
