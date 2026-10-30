"""Senders that hold a message in the Outbox instead of delivering it
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md D2).

- :class:`OutboxSender`: mail is not set up in this environment (``MAIL_ENABLED`` off, no
  service-account key, or no sender address in the company settings). Nothing reaches a
  network; every message is ``held`` with the reason ``not_set_up``.
- :class:`AllowlistSender`: on the sandbox, a recipient that is not on the company settings'
  allowlist is ``held`` with the reason ``sandbox`` and never reaches the inner sender; an
  allowlisted one is delivered by it. :func:`mail.get_sender` wraps the Gmail sender in it
  whenever ``STAX_ENV`` is ``sandbox``, so no path (a reminder, a final statement, a morning
  e-mail) can mail anyone else from a sandbox.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from mail.base import HELD, NOT_SET_UP, SANDBOX, MailSender, Message, SentReceipt


def held(reason: str) -> SentReceipt:
    """The receipt of a message held for ``reason``: nothing was sent."""
    return SentReceipt(
        provider_message_id="", sent_at=datetime.now(UTC), disposition=HELD, reason=reason
    )


class OutboxSender:
    """Mail is not set up: every message is held (``not_set_up``)."""

    def send(self, message: Message) -> SentReceipt:
        return held(NOT_SET_UP)


class AllowlistSender:
    """Delivers through ``inner`` only to an address of ``allowlist`` (compared lower-case);
    holds every other message (``sandbox``)."""

    def __init__(self, inner: MailSender, allowlist: Iterable[str]) -> None:
        self.inner = inner
        self.allowlist = frozenset(address.lower() for address in allowlist)

    def allows(self, address: str) -> bool:
        return address.lower() in self.allowlist

    def send(self, message: Message) -> SentReceipt:
        if not self.allows(message.to):
            return held(SANDBOX)
        return self.inner.send(message)
