"""The mail interface, its message and its one error (docs/qa/PLAN-011b D3).

A :class:`Message` is checked when it is made: one recipient address, a subject and
header values without line breaks (no header injection), header names of the ``X-``
form only. :func:`mime_bytes` turns it into the RFC 5322 bytes a sender delivers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Protocol

#: What became of a message: delivered to its recipient, delivered to the catch-all
#: address instead, or not delivered because mail is off. The values are the
#: ``outbound_messages.status`` values of the same meaning.
SENT = "sent"
CATCH_ALL = "catch_all"
SUPPRESSED = "suppressed"
DISPOSITIONS: tuple[str, ...] = (SENT, CATCH_ALL, SUPPRESSED)

#: One plain address, ASCII, no display name: nothing a header could be split on.
ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
HEADER_NAME = re.compile(r"X-[A-Za-z0-9-]{1,60}")
_LINE_BREAK = re.compile(r"[\r\n\x0b\x0c\x85  ]")
#: An error's class name as :class:`MailUnavailable` carries it.
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
SUBJECT_MAX_LENGTH = 300


class MailUnavailable(Exception):  # noqa: N818 - the name is the contract (PLAN-011b D3)
    """The message could not be delivered.

    Carries a class name only, never a message: a provider's error text can echo a
    token, an address or the request, none of which belongs in a log line, a row or a
    page.
    """

    def __init__(self, cause: str) -> None:
        name = cause if _CLASS_NAME.fullmatch(cause) else "MailUnavailable"
        super().__init__(name)
        self.cause = name


def is_address(value: str) -> bool:
    return bool(ADDRESS.fullmatch(value))


@dataclass(frozen=True, slots=True)
class Message:
    """One e-mail to one recipient. ``html`` empty means a text-only message."""

    to: str
    subject: str
    text: str
    html: str = ""
    headers: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        if not is_address(self.to):
            raise ValueError("The recipient must be one plain e-mail address.")
        if _LINE_BREAK.search(self.subject) or len(self.subject) > SUBJECT_MAX_LENGTH:
            raise ValueError("The subject must be one line of at most 300 characters.")
        for name, value in self.headers:
            if not HEADER_NAME.fullmatch(name) or _LINE_BREAK.search(value):
                raise ValueError("A header must be an X- name with a one-line value.")


@dataclass(frozen=True, slots=True)
class SentReceipt:
    """What a sender reports: the provider's message ID (empty when nothing was sent),
    when, and the :data:`DISPOSITIONS` value."""

    provider_message_id: str
    sent_at: datetime
    disposition: str = SENT


class MailSender(Protocol):
    def send(self, message: Message) -> SentReceipt:
        """Deliver ``message``, or raise :class:`MailUnavailable`."""
        ...


def mime_bytes(message: Message, sender: str) -> bytes:
    """The message as RFC 5322 bytes from ``sender``: the text part, and the HTML part as
    an alternative when there is one."""
    mime = EmailMessage(policy=SMTP)
    mime["From"] = sender
    mime["To"] = message.to
    mime["Subject"] = message.subject
    for name, value in message.headers:
        mime[name] = value
    mime.set_content(message.text)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    return mime.as_bytes()
