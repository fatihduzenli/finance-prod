"""The mail interface, its message and its one error (docs/qa/PLAN-011b D3, PLAN-014 D1).

A :class:`Message` is checked when it is made: one recipient address, a subject and
header values without line breaks (no header injection), header names of the ``X-``
form only, and attachments (PLAN-012e D8) with a plain file name, a known content type
and some bytes. :func:`mime_bytes` turns it into the RFC 5322 bytes a sender delivers:
``multipart/mixed`` when there is an attachment, with a ``Reply-To`` when one is given.
:func:`address_lines` reads a list of addresses kept one per line (the company settings'
managers and sandbox allowlist, PLAN-014 D1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Protocol

#: What became of a message: delivered to its recipient, or held in the Outbox (mail not
#: set up, or a recipient off the sandbox allowlist; PLAN-014 D2). The values are the
#: ``outbound_messages.status`` values of the same meaning.
SENT = "sent"
HELD = "held"
DISPOSITIONS: tuple[str, ...] = (SENT, HELD)
#: Why a message is held, as ``SentReceipt.reason`` and ``outbound_messages.held_reason``
#: name it: mail is not set up in this environment, or the recipient is not on the sandbox
#: allowlist.
NOT_SET_UP = "not_set_up"
SANDBOX = "sandbox"

#: One plain address, ASCII, no display name: nothing a header could be split on.
ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
HEADER_NAME = re.compile(r"X-[A-Za-z0-9-]{1,60}")
_LINE_BREAK = re.compile(r"[\r\n\x0b\x0c\x85  ]")
#: An error's class name as :class:`MailUnavailable` carries it.
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
SUBJECT_MAX_LENGTH = 300
#: An attachment's file name: letters, digits, dot, dash and underscore, an extension.
ATTACHMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.[A-Za-z0-9]{1,8}")
#: The attachment types the app sends.
ATTACHMENT_TYPES: frozenset[str] = frozenset({"application/pdf"})
#: Largest attachment: well under the Gmail API's message limit.
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024


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


#: Most addresses one list may hold.
ADDRESS_LINES_MAX = 50
ADDRESS_LINE_REFUSED = "Write one plain e-mail address per line."
ADDRESS_LINES_TOO_MANY = f"A list holds at most {ADDRESS_LINES_MAX} addresses."


def address_lines(text: str) -> list[str]:
    """The addresses of ``text``, one per line: blank lines ignored, each lower-cased, each
    once, in order. ``ValueError`` (with a sentence that repeats no line) when a line holds
    anything but one plain address, or there are more than :data:`ADDRESS_LINES_MAX`."""
    found: list[str] = []
    for line in text.splitlines():
        address = line.strip().lower()
        if not address:
            continue
        if not is_address(address):
            raise ValueError(ADDRESS_LINE_REFUSED)
        if address not in found:
            found.append(address)
    if len(found) > ADDRESS_LINES_MAX:
        raise ValueError(ADDRESS_LINES_TOO_MANY)
    return found


@dataclass(frozen=True, slots=True)
class Attachment:
    """One file sent with a message (PLAN-012e D8)."""

    filename: str
    content_type: str
    data: bytes

    def __post_init__(self) -> None:
        if not ATTACHMENT_NAME.fullmatch(self.filename):
            raise ValueError("An attachment needs a plain file name with an extension.")
        if self.content_type not in ATTACHMENT_TYPES:
            raise ValueError("This attachment type is not sent.")
        if not self.data or len(self.data) > ATTACHMENT_MAX_BYTES:
            raise ValueError("An attachment must hold between 1 byte and 10 MB.")


@dataclass(frozen=True, slots=True)
class Message:
    """One e-mail to one recipient. ``html`` empty means a text-only message."""

    to: str
    subject: str
    text: str
    html: str = ""
    headers: tuple[tuple[str, str], ...] = field(default=())
    attachments: tuple[Attachment, ...] = field(default=())

    def __post_init__(self) -> None:
        if not is_address(self.to):
            raise ValueError("The recipient must be one plain e-mail address.")
        if _LINE_BREAK.search(self.subject) or len(self.subject) > SUBJECT_MAX_LENGTH:
            raise ValueError("The subject must be one line of at most 300 characters.")
        for name, value in self.headers:
            if not HEADER_NAME.fullmatch(name) or _LINE_BREAK.search(value):
                raise ValueError("A header must be an X- name with a one-line value.")
        if not all(isinstance(item, Attachment) for item in self.attachments):
            raise ValueError("Attachments must be Attachment values.")


@dataclass(frozen=True, slots=True)
class SentReceipt:
    """What a sender reports: the provider's message ID (empty when nothing was sent),
    when, the :data:`DISPOSITIONS` value and, for a held message, why
    (:data:`NOT_SET_UP` or :data:`SANDBOX`)."""

    provider_message_id: str
    sent_at: datetime
    disposition: str = SENT
    reason: str = ""


class MailSender(Protocol):
    def send(self, message: Message) -> SentReceipt:
        """Deliver ``message``, or raise :class:`MailUnavailable`."""
        ...


def mime_bytes(message: Message, sender: str, reply_to: str = "") -> bytes:
    """The message as RFC 5322 bytes from ``sender`` (with ``Reply-To`` when ``reply_to`` is
    given, PLAN-014 D1): the text part, and the HTML part as an alternative when there is
    one; with attachments, ``multipart/mixed`` holding that body and each file (PLAN-012e
    D8)."""
    if reply_to and not is_address(reply_to):
        raise ValueError("The Reply-To must be one plain e-mail address.")
    mime = EmailMessage(policy=SMTP)
    mime["From"] = sender
    mime["To"] = message.to
    if reply_to:
        mime["Reply-To"] = reply_to
    mime["Subject"] = message.subject
    for name, value in message.headers:
        mime[name] = value
    mime.set_content(message.text)
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    for attachment in message.attachments:
        maintype, subtype = attachment.content_type.split("/", 1)
        mime.add_attachment(
            attachment.data, maintype=maintype, subtype=subtype, filename=attachment.filename
        )
    return mime.as_bytes()
