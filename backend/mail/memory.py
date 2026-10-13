"""Senders that reach no network (docs/qa/PLAN-011b D3): :class:`MemoryMailSender` for
tests and local use, :class:`NullSender` when mail is off, and :class:`CatchAllSender`,
which rewrites every message to the catch-all address before its inner sender sees it.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime

from mail.base import (
    CATCH_ALL,
    SUBJECT_MAX_LENGTH,
    SUPPRESSED,
    MailSender,
    MailUnavailable,
    Message,
    SentReceipt,
)

#: The header that names the recipient a catch-all message was meant for.
ORIGINAL_TO = "X-Original-To"


class MemoryMailSender:
    """Keeps every delivered message in :attr:`sent`. Tests may set :attr:`fail_with` to
    a class name to make the next sends raise :class:`MailUnavailable`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent: list[Message] = []
        self.fail_with: str | None = None

    def send(self, message: Message) -> SentReceipt:
        with self._lock:
            if self.fail_with:
                raise MailUnavailable(self.fail_with)
            self.sent.append(message)
            number = len(self.sent)
        return SentReceipt(provider_message_id=f"memory{number}", sent_at=datetime.now(UTC))


class NullSender:
    """Mail is off (``MAIL_ENABLED`` false): nothing is delivered, every send is
    ``suppressed``."""

    def send(self, message: Message) -> SentReceipt:
        return SentReceipt(
            provider_message_id="", sent_at=datetime.now(UTC), disposition=SUPPRESSED
        )


class CatchAllSender:
    """Delivers every message to ``address`` instead of its recipient: the subject starts
    with ``[to: <recipient>] `` and an ``X-Original-To`` header names the recipient; the
    attachments go with it. The receipt says ``catch_all``."""

    def __init__(self, inner: MailSender, address: str) -> None:
        self.inner = inner
        self.address = address

    def send(self, message: Message) -> SentReceipt:
        rewritten = Message(
            to=self.address,
            subject=f"[to: {message.to}] {message.subject}"[:SUBJECT_MAX_LENGTH],
            text=message.text,
            html=message.html,
            headers=(*message.headers, (ORIGINAL_TO, message.to)),
            attachments=message.attachments,
        )
        return replace(self.inner.send(rewritten), disposition=CATCH_ALL)
