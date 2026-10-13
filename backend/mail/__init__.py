"""Outbound e-mail (docs/qa/PLAN-011b D3).

The one package that talks to Gmail; nothing outside it imports ``google.*``
(``backend/tests/test_mail.py``). The rest of the app builds a :class:`Message` and
calls ``get_sender().send(message)``, which returns a :class:`SentReceipt` or raises
:class:`MailUnavailable` (a class name, never a message).

:func:`get_sender` returns, for the current settings:

- :class:`~mail.memory.NullSender` when ``MAIL_ENABLED`` is off: nothing is delivered and
  every send is ``suppressed``;
- otherwise the Gmail sender (:class:`~mail.gmail.GmailApiSender`, one per process, built
  on first use from ``GMAIL_SERVICE_ACCOUNT_JSON`` and ``GMAIL_SENDER``), wrapped in
  :class:`~mail.memory.CatchAllSender` when ``MAIL_CATCH_ALL`` is set, so every message
  goes to that address with its recipient named in the subject and the
  ``X-Original-To`` header. Outside production a missing catch-all is refused here too
  (``CatchAllRequired``), besides the boot refusal of the environment guard: a sandbox can
  never mail an operator.

Imports are made inside the functions so that loading this package touches neither
google-auth nor the settings.
"""

from __future__ import annotations

import hashlib
import json
import threading

from mail.base import (
    CATCH_ALL,
    SENT,
    SUPPRESSED,
    Attachment,
    MailSender,
    MailUnavailable,
    Message,
    SentReceipt,
)

__all__ = [
    "CATCH_ALL",
    "SENT",
    "SUPPRESSED",
    "Attachment",
    "MailSender",
    "MailUnavailable",
    "Message",
    "SentReceipt",
    "get_sender",
]

PRODUCTION = "production"

_lock = threading.Lock()
_senders: dict[str, MailSender] = {}


def get_sender() -> MailSender:
    """The sender for the current settings (see the module docstring)."""
    from django.conf import settings

    from mail.memory import CatchAllSender, NullSender

    if not settings.MAIL_ENABLED:
        return NullSender()
    catch_all: str = settings.MAIL_CATCH_ALL
    if not catch_all and settings.APP_ENV != PRODUCTION:
        raise MailUnavailable("CatchAllRequired")
    base = _base_sender()
    return CatchAllSender(base, catch_all) if catch_all else base


def _base_sender() -> MailSender:
    """The process's Gmail sender for the configured mailbox and key, built once."""
    from django.conf import settings

    sender: str = settings.GMAIL_SENDER
    raw: str = settings.GMAIL_SERVICE_ACCOUNT_JSON
    key = hashlib.sha256(f"{sender}\n{raw}".encode()).hexdigest()
    with _lock:
        found = _senders.get(key)
        if found is None:
            found = _build(raw, sender)
            _senders.clear()
            _senders[key] = found
        return found


def _build(raw: str, sender: str) -> MailSender:
    from mail.gmail import GmailApiSender

    try:
        info = json.loads(raw)
    except ValueError:
        raise MailUnavailable("KeyInvalid") from None
    if not isinstance(info, dict):
        raise MailUnavailable("KeyInvalid")
    return GmailApiSender(info, sender)
