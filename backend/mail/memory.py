"""The sender that reaches no network (docs/qa/PLAN-011b D3): :class:`MemoryMailSender`, for
tests and local use. (Mail off is the Outbox, :mod:`mail.outbox`, PLAN-014 D2.)
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from mail.base import MailUnavailable, Message, SentReceipt


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
