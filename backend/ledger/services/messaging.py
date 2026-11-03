"""The messaging engine (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md D2 to
D8): the queue, the send window, the channels, the send, and the pages' lists.

**Queue.** :func:`queue` renders a template with its placeholder values (:func:`context_for`
builds them; :mod:`ledger.merge` renders, never Django templates) and stores one ``queued``
:class:`~ledger.models.OutboundMessage` with the rendered subject and body, the rule that
queued it (``A`` to ``F``, ``final_statement``, ``manual``) and its occurrence, then its
``message_send`` job, in the caller's transaction. The database keeps one row per (lease,
charge, rule, occurrence) unless the row is a send by hand: a second queue of the same
returns ``None`` and writes nothing. A message without a lease is refused (``ValueError``)
before anything is rendered: the unique rule cannot dedupe a NULL lease on PostgreSQL, and
the row would have no billing e-mail to go to. A template of the ``sms`` channel is refused
(:class:`ChannelUnavailable`, "SMS is not set up."): no SMS provider exists (D8).

**The old ladder is retired for good** (owner, 2026-11-01). :func:`queue` and
:func:`queue_portal`, the only ways a message is stored, refuse a retired rule
(:data:`~ledger.models.RETIRED_RULES`, the old collections ladder) and an inactive template
with ``ValueError`` before anything is rendered: no row, no job, no audit. The ladder's past
rows stay as history; the Outbox shows them with no store ("—"), as it does a portal message
(:data:`~ledger.models.RULES_WITHOUT_STORE`); every other row names its lease's store.

**A lease month (E4).** Rules A and B for rent go three days before the month's debit, before
its rent charge exists (the owner's decision of 2026-10-30: rent charges are still made on
their day). Such a message is about a :class:`RentMonth`: its row names the lease as its
entity (``charge_entity_type`` ``lease_month``) and the month in ``period_month``, one row per
(lease, rule, month). Its values are the month's: "Rent October 2026", the scheduled debit
date and amount.

**Send window (D7).** A message queued between 9:00 and 18:00 New York time goes at once;
one queued outside it waits for the next 9:00 New York (:func:`scheduled_for`; the zone
decides daylight saving): its ``scheduled_for`` and its job's ``run_after`` are that
instant, so the worker leaves it until then.

**Send.** :func:`send` (the ``message_send`` job) delivers the stored text: a message for an
operator (rules ``A`` to ``D``, ``F``, ``manual``) whose charge has closed since is
``suppressed`` (``ChargeClosed``), as is one to an archived operator or a final statement
whose lease end is gone; a message about a lease month is checked again instead of a charge
(:func:`month_closed_reason`: ``LeaseEnded``, ``ScheduleStopped`` for A, ``MonthSettled``);
an operator handled personally holds its messages (``handled_personally``; never the
internal E, which goes to the managers); a message without an address is held
(``no_address``, or ``no_manager_list`` for an internal one).
Then the channel (:func:`channel_for`, e-mail only) sends it: mail not set up holds it
(``not_set_up``); on the sandbox a recipient off the allowlist holds it (``sandbox``) and
nothing is handed to the mail sender; otherwise it is ``sent``, or ``failed`` with the
error's class name. Each outcome is audited (``message.sent``, ``message.held``,
``message.failed``, ``message.suppressed``) with the number of addresses the send was
for (``recipients``; 0 when it stopped before any): an internal message goes to every
manager, while its row names the first.

**Portal messages (PLAN-014 H2).** :func:`queue_portal` queues the invitation to the operator
portal or a password reset: kind and rule ``portal``, about the portal account
(``portal_user``), without a lease, to the account's e-mail (``to_address``), by hand when a
staff member asked for it. The one-time link is never stored: the stored body carries
:data:`LINK_ADDED_AT_SEND` in its place, and :func:`send` asks the template's
:class:`LinkIssuer` (registered in :data:`ONE_TIME_LINKS` by the ``portal`` app) for a fresh
link just before the delivery, written into the outgoing text only. The issuer keeps the link
when the message is sent (an older link of the same kind stops working) and drops it when the
message is held or fails, so a link exists only in a message that left; its ``refusal`` names
why the message is no longer to go (the account disabled, already activated, gone), and the
message is then suppressed. An operator handled personally does not hold a portal message:
that switch is about the collection e-mails.

**One e-mail per lease and day** (owner, 2026-11-02). The daily job queues each rule's row as
before (one per charge, rule and occurrence: the idempotency is unchanged) but without its job
(``queue(..., job=False)``), then :func:`combine` sends one of them for all: the first it is
given, the most urgent (:mod:`ledger.services.reminders` orders D > C > B > A). When there is
more than one, that message's body gains the list of every charge (:func:`charges_text`:
charge · amount · due date · days late, and the total) under its template's text, its subject
stays its template's, and the others become ``combined`` into it (``combined_into``), audited
``message.combined``, never sent on their own. An overdue row held back by the 3-day fold is
``waiting`` (:func:`wait_for_fold`, audited ``message.waiting``) until the morning of its day
(``scheduled_for``), with no job; on that day :func:`release` drops the ones whose charge
closed since (``suppressed``, as at a send) and the rest go with that day's :func:`combine`. At
the send (:func:`_regroup`) a combined message is rebuilt from the charges it lists that are
still open: a row whose charge closed since is ``suppressed`` and left out of the list and the
total, the most urgent open row is sent (its own subject) for the rest, and the stored body is
what was sent; once every charge is closed the message is suppressed. The window, the
holds, the allowlist and *Send now* apply to it as to any message, and the Outbox shows its
outcome on the rows combined into it (:func:`status_text`).

**D for an ended lease.** ``{lease_line}`` (:func:`lease_line`) reads "Continued non-payment may
affect your lease." while the charge's lease is current and "Please contact us to settle this
balance." once it has ended (its status ended, or its end date today or before). ``{pay_line}``
(:func:`pay_line`, D's since ledger 0069) is D's whole payment sentence: "Please pay today at
{pay_link} or contact us at {support_email} to arrange payment. Continued non-payment may affect
your lease." for a current lease, "Please pay at {pay_link} or contact us at {support_email} to
settle this balance." once it has ended, rendered through the same merge.

**No empty placeholder leaves** (owner, 2026-11-02). A placeholder whose value is empty is
rendered as itself (``{support_email}``, :func:`with_gaps`), so the stored text shows the gap.
While the company name or the support e-mail is blank (:func:`settings_incomplete`), every
message that is not internal (the operator's, the final statement, the portal's) is held
``settings_incomplete``; the internal E still goes. A text that still shows a placeholder of its
template (:func:`missing`) is held ``missing_value``, the Outbox naming it ("Missing value:
{support_email}"). Both are checked when the message is stored (:func:`queue`,
:func:`queue_portal`; for the daily job's rows, queued without their job, when :func:`combine`
picks the one to send) and again at the send, where a setting's placeholder is first filled
from the settings as they stand (:func:`fill_settings`). A held message goes with *Send now*,
and :func:`send_held_for_settings` (the Outbox's "Send all held for settings") releases every
message held ``settings_incomplete`` once both settings are set. Such a release (and *Send now*
on such a hold, with the other holds of its lease) is grouped as the daily job groups
(:func:`plan_release`): per lease one e-mail under the most urgent of A to D listing the charges
still open, the others combined into it; a row whose charge closed or month settled, an A whose
debit has passed, and every row about a charge but its most urgent (``Superseded``) are
suppressed. A portal message, a final statement or an F goes as it is.

**By hand.** :func:`send_by_hand` (billing, the charge page's "Send a message now") queues a
``manual`` row of an active, not internal template for an open charge, through the same
queue and window. :func:`send_now` (admin, the Outbox) sends a held message again, only once
mail is set up and, on the sandbox, its address is on the allowlist.
"""

from __future__ import annotations

import hashlib
import html
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone

import mail
from finance.dates import NEW_YORK, day_text, month_long_text
from ledger import merge
from ledger.models import (
    RETIRED_RULES,
    AuditLog,
    ChargeEntityType,
    CollectionSchedule,
    CollectionStatus,
    CompanySettings,
    Document,
    HeldReason,
    InsuranceCharge,
    Lease,
    LeaseEnd,
    MessageChannel,
    MessageKind,
    MessageRule,
    MessageStatus,
    MessageTemplate,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    OutboundMessage,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import insurance, leases, rent_run, statement_links
from ledger.services.collections import mask_address
from ledger.wording import format_amount
from mail.base import SUBJECT_MAX_LENGTH
from payments import jobs, lease_sync
from payments.models import JobKind
from portal import links as portal_links

logger = logging.getLogger(__name__)

ENTITY = "outbound_message"

# --- Rules, reasons and sentences ---------------------------------------------------------

#: The rules whose messages go to the operator and stop once the charge closes.
OPERATOR_FACING_RULES: frozenset[str] = frozenset(
    {
        MessageRule.A,
        MessageRule.B,
        MessageRule.C,
        MessageRule.D,
        MessageRule.F,
        MessageRule.MANUAL,
    }
)
#: Statuses that close a charge to messages, whatever its balance.
CLOSED_RENT: frozenset[str] = frozenset(
    {RentChargeStatus.WAIVED, RentChargeStatus.PAID, RentChargeStatus.PENDING}
)
CLOSED_ONE_OFF: frozenset[str] = frozenset(
    {OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED, OneOffChargeStatus.PAID}
)

#: Why a queued message was not sent at all: class names, as ``error``.
CHARGE_CLOSED = "ChargeClosed"
OPERATOR_ARCHIVED = "OperatorArchived"
STATEMENT_MISSING = "StatementMissing"
PAYMENT_DATA_REFUSED = "PaymentDataRefused"
MESSAGE_REFUSED = "MessageRefused"
#: A message about a lease month (E4) not sent: the lease ended before the month's debit,
#: the rent schedule of an A is no longer on, or the month's rent charge is closed.
LEASE_ENDED = "LeaseEnded"
SCHEDULE_STOPPED = "ScheduleStopped"
MONTH_SETTLED = "MonthSettled"

SMS_UNAVAILABLE = "SMS is not set up."
GONE = "The message no longer exists."
NOT_QUEUED = "Nothing sent: the message is already {status}."
SENT_TEXT = "Sent."
HELD_TEXT = "Held: {reason}."
SUPPRESSED_TEXT = "Not sent ({cause})."
FAILED_TEXT = "Failed: {cause}."

HAND_QUEUED = (
    "The message will be sent; the store's Notes & follow-ups and the Outbox show when it is sent."
)
HAND_NOT_OPEN = "Only an open charge with a balance can be chased."
HAND_NO_EMAIL = "The lease billed has no billing e-mail address."
HAND_OPTED_OUT = (
    "This operator is handled personally: no message is sent. Change that on the operator's "
    "page first."
)
HAND_ARCHIVED = "The operator is archived."
HAND_UNKNOWN_TEMPLATE = "Choose a message to send."

SEND_NOW_QUEUED = "The message will be sent now."
SEND_NOW_NOT_HELD = "Only a held message can be sent from the Outbox."
SEND_NOW_NOT_SET_UP = "Mail is not set up in this environment: the message stays held."
SEND_NOW_NOT_ALLOWED = (
    "This address is not on the sandbox allowlist (Admin › Settings › Mail): the message "
    "stays held."
)
SEND_NOW_NO_ADDRESS = "The message has no address to send to: it stays held."
SEND_NOW_SETTINGS = (
    "Set the company name and support e-mail (Admin › System settings) first: the message "
    "stays held."
)
#: The Outbox's "Send all held for settings" (owner, 2026-11-02).
RELEASE_QUEUED = "Sent now: {text}."
RELEASE_TEXT = (
    "{held} held message{held_plural} → {emails} e-mail{emails_plural}, {suppressed} superseded"
)
SEND_NOW_SUPPRESSED = "The message is no longer to be sent ({cause})."
#: Why a row released from a settings hold is not sent (owner, 2026-11-02): another row about
#: the same charge goes instead, or the debit an A announces has happened.
SUPERSEDED = "Superseded"
DEBIT_PASSED = "DebitPassed"
RELEASE_NONE = "No message is held for the settings."
RELEASE_STILL_INCOMPLETE = (
    "Set the company name and support e-mail (Admin › System settings) first: nothing was sent."
)
RELEASE_COUNT_CHANGED = (
    "The number of messages held for the settings changed: check the Outbox and try again."
)
NO_LEASE = "A message belongs to a lease."
#: Owner, 2026-11-01: the retired ladder queues nothing; an inactive template neither.
RETIRED_RULE = "The old collections ladder is retired: it queues no message."
INACTIVE_TEMPLATE = "The template is inactive: it queues no message."
#: PLAN-014 H2: what a portal message's stored body says where its one-time link goes; the
#: link replaces it in the outgoing text only (:func:`send`).
LINK_ADDED_AT_SEND = "[your personal link is added when this e-mail is sent]"
#: A portal message whose template has no registered issuer is not sent.
LINK_UNAVAILABLE = "LinkUnavailable"
#: Who an internal message goes to, as the Outbox and the store's timeline show it.
MANAGERS_TO = "the managers ({count})"

#: The send window, New York time (D7): from 9:00, until 18:00.
WINDOW_OPENS = time(9, 0)
WINDOW_CLOSES = time(18, 0)

#: A placeholder with no value for this message (an empty last four reads so).
NO_VALUE = "—"

#: ``{lease_line}`` (owner, 2026-11-02): a current lease, and a lease that has ended.
LEASE_LINE_CURRENT = "Continued non-payment may affect your lease."
LEASE_LINE_ENDED = "Please contact us to settle this balance."
#: ``{pay_line}`` (owner, 2026-11-02): D's payment sentence for a current lease, and for one
#: that has ended; each rendered through the merge.
PAY_LINE_CURRENT = (
    "Please pay today at {pay_link} or contact us at {support_email} to arrange payment. "
    "Continued non-payment may affect your lease."
)
PAY_LINE_ENDED = "Please pay at {pay_link} or contact us at {support_email} to settle this balance."

#: The company settings no message to an operator goes without (owner, 2026-11-02).
REQUIRED_SETTINGS: tuple[str, ...] = ("name", "support_email")
#: The placeholders whose value is a company setting: filled again at the send.
SETTING_PLACEHOLDERS: Mapping[str, str] = {
    "company_name": "name",
    "company": "name",
    "support_email": "support_email",
}
#: How the Outbox reads the two holds of an empty placeholder.
SETTINGS_INCOMPLETE_TEXT = (
    "Settings incomplete: set the company name and support e-mail in Admin › System settings"
)
MISSING_VALUE_TEXT = "Missing value: {name}"
#: What staff do about a placeholder without a value, when it is data.
MISSING_VALUE_FIX: Mapping[str, str] = {
    "store_number": "give store {store} a number in its settings",
    "store_name": "give store {store} a name in its settings",
    "operator_name": "give operator {operator} a name on its page",
    "support_email": "set the support e-mail in Admin › System settings",
    "company_name": "set the company name in Admin › System settings",
    "company": "set the company name in Admin › System settings",
}

#: The operator's rules sent as one message per lease and day, most urgent first: the first
#: present gives the message its template and subject (owner, 2026-11-02).
COMBINED_RULES: tuple[str, ...] = (MessageRule.D, MessageRule.C, MessageRule.B, MessageRule.A)

#: The list under a combined message's text (owner, 2026-11-02).
CHARGES_HEADING = "Charges ({count}): charge · amount · due date · days late"
CHARGE_LINE = "- {name} · {amount} · {due} · {days_late}"
CHARGES_TOTAL = "Total: {amount}"
#: How the Outbox reads a row combined into another, and one waiting for the fold.
COMBINED_TEXT = "Combined into the {rule} message: {outcome}"
WAITING_TEXT = "{label} (until {day})"


class ChannelUnavailable(Exception):  # noqa: N818 - reads as the outcome (D8)
    """The channel has no provider (SMS)."""

    def __init__(self, message: str = SMS_UNAVAILABLE) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str


# --- Channels (D2, D8) ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rendered:
    """One message as a channel delivers it: one address, the stored subject and body."""

    to: str
    subject: str
    body: str
    attachments: tuple[mail.Attachment, ...] = ()


@dataclass(frozen=True, slots=True)
class Disposition:
    """What a channel did: ``status`` a :class:`~ledger.models.MessageStatus` value (sent,
    held or failed), with the held reason, the provider's message ID and time, or the
    error's class name."""

    status: str
    reason: str = ""
    provider_message_id: str = ""
    sent_at: datetime | None = None
    error: str = ""


class Channel(Protocol):
    def send(self, rendered: Rendered) -> Disposition: ...


@dataclass(frozen=True, slots=True)
class OneTimeLink:
    """A portal message's link, made at its send (PLAN-014 H2): ``keep`` once it was sent,
    ``drop`` when it was not."""

    url: str
    keep: Callable[[], None]
    drop: Callable[[], None]


class LinkIssuer(Protocol):
    """What makes the one-time link of one portal template (registered by ``portal``)."""

    def refusal(self, message: OutboundMessage) -> str:
        """The class name of why ``message`` is no longer to be sent; ``""`` when it goes."""
        ...

    def issue(self, message: OutboundMessage) -> OneTimeLink: ...


#: Per template code, the issuer of its one-time link (PLAN-014 H2; ``portal.apps``).
ONE_TIME_LINKS: dict[str, LinkIssuer] = {}


def html_part(text: str) -> str:
    """The HTML alternative: the text, escaped, with its line breaks kept."""
    escaped = html.escape(text, quote=True)
    return "<html><body><p>" + escaped.replace("\n", "<br>\n") + "</p></body></html>"


def one_line(text: str) -> str:
    return " ".join(text.split())


def allowlisted(address: str, found: mail.MailSettings | None = None) -> bool:
    """Whether ``address`` may be mailed here: anywhere off the sandbox; on it, only an
    address of the company settings' allowlist."""
    if not mail.on_sandbox():
        return True
    return address.lower() in (found or mail.mail_settings()).sandbox_allowlist


class EmailChannel:
    """The mail sender (:func:`mail.get_sender`), behind the Outbox and the sandbox
    allowlist (D2)."""

    def send(self, rendered: Rendered) -> Disposition:
        found = mail.mail_settings()
        if not mail.is_set_up(found):
            return Disposition(MessageStatus.HELD, HeldReason.NOT_SET_UP)
        if not allowlisted(rendered.to, found):
            return Disposition(MessageStatus.HELD, HeldReason.SANDBOX)
        try:
            outgoing = mail.Message(
                to=rendered.to,
                subject=rendered.subject,
                text=rendered.body,
                html=html_part(rendered.body),
                attachments=rendered.attachments,
            )
            receipt = mail.get_sender().send(outgoing)
        except mail.MailUnavailable as error:
            return Disposition(MessageStatus.FAILED, error=error.cause)
        except ValueError:
            return Disposition(MessageStatus.FAILED, error=MESSAGE_REFUSED)
        if receipt.disposition == mail.HELD:
            return Disposition(MessageStatus.HELD, receipt.reason or HeldReason.NOT_SET_UP)
        return Disposition(
            MessageStatus.SENT,
            provider_message_id=receipt.provider_message_id,
            sent_at=receipt.sent_at,
        )


def channel_for(name: str) -> Channel:
    """The channel of this name: ``email``; ``sms`` raises :class:`ChannelUnavailable`."""
    if name == MessageChannel.EMAIL:
        return EmailChannel()
    if name == MessageChannel.SMS:
        raise ChannelUnavailable
    raise ValueError("Unknown channel")


# --- Send window (D7) -----------------------------------------------------------------------


def scheduled_for(now: datetime) -> datetime:
    """``now`` when it falls in the send window (9:00 to before 18:00 New York); else the
    next 9:00 New York (the same day before 9:00, the next day from 18:00). UTC."""
    local = now.astimezone(NEW_YORK)
    if WINDOW_OPENS <= local.time() < WINDOW_CLOSES:
        return now.astimezone(UTC)
    day = local.date() if local.time() < WINDOW_OPENS else local.date() + timedelta(days=1)
    return datetime.combine(day, WINDOW_OPENS, tzinfo=NEW_YORK).astimezone(UTC)


# --- Placeholder values (D4) ------------------------------------------------------------------

Charge = RentCharge | OneOffCharge | InsuranceCharge


@dataclass(frozen=True, slots=True)
class RentMonth:
    """The rent of one lease for one month, before its charge exists (E4): what rules A and
    B announce three days ahead. ``month`` is the month's 1st; ``debit_date`` the day the
    month's first debit is scheduled (or its rent due, without autopay); ``amount`` what is
    scheduled; ``seq`` the debit the message is for (always the first: one message per
    lease and month)."""

    lease: Lease
    month: date
    debit_date: date
    amount: Decimal
    seq: int = 1


#: What a rule's message is about.
Subject = Charge | RentMonth


def charge_type_of(charge: Subject | LeaseEnd) -> str:
    if isinstance(charge, RentMonth):
        return ChargeEntityType.LEASE_MONTH
    if isinstance(charge, RentCharge):
        return ChargeEntityType.RENT_CHARGE
    if isinstance(charge, OneOffCharge):
        return ChargeEntityType.ONE_OFF_CHARGE
    if isinstance(charge, InsuranceCharge):
        return ChargeEntityType.INSURANCE_CHARGE
    if isinstance(charge, LeaseEnd):
        return ChargeEntityType.LEASE_END
    raise TypeError("A message is about a charge or a lease end.")


def policy_label(year: int) -> str:
    """``Insurance 2026–27``."""
    return f"Insurance {year}–{(year + 1) % 100:02d}"


def rent_month_name(month: date) -> str:
    """ "Rent October 2026": what a message about a lease month calls it (E4)."""
    return f"Rent {month_long_text(month)}"


def charge_name(charge: Subject) -> str:
    """What the message calls the charge: "Rent October 2026" (and its second debit),
    "Insurance 2026–27", or a one-off charge's description; a lease month reads as its
    month's rent."""
    if isinstance(charge, RentMonth):
        return rent_month_name(charge.month)
    if isinstance(charge, RentCharge):
        text = f"Rent {month_long_text(charge.period_month)}"
        return text + (f" (debit {charge.seq})" if charge.seq > 1 else "")
    if isinstance(charge, InsuranceCharge):
        return policy_label(charge.policy_year)
    return charge.label


def due_of(charge: Subject) -> date:
    if isinstance(charge, RentMonth):
        return charge.debit_date
    return charge.due_date if isinstance(charge, OneOffCharge) else charge.due_on


def balance_of(charge: Subject) -> Decimal:
    if isinstance(charge, RentMonth):
        return charge.amount
    if isinstance(charge, InsuranceCharge):
        return insurance.balance(charge)
    return Decimal(str(charge.balance))


def operator_of(lease: Lease | None, charge: Subject | LeaseEnd) -> Operator | None:
    """The operator a message about ``charge`` goes to (or is about)."""
    if isinstance(charge, OneOffCharge) and charge.operator is not None:
        return charge.operator
    if lease is not None and lease.operator is not None:
        return lease.operator
    owner = getattr(charge, "lease", None)
    return owner.operator if owner is not None else None


def pay_link(operator: Operator | None, store_id: uuid.UUID) -> str:
    """``{pay_link}`` (PLAN-014 H4): the portal's Pay page of the store when the operator has
    a portal account that can sign in, else a new signed statement link; ``""`` without an
    operator."""
    if operator is None:
        return ""
    return portal_links.pay_link(operator, store_id) or statement_links.link(operator)


def store_link(lease: Lease) -> str:
    """The absolute URL of the staff page of the lease's store."""
    path = reverse("ledger:stores:detail", kwargs={"pk": lease.store_id})
    return f"{str(settings.APP_BASE_URL).rstrip('/')}{path}"


def lease_ended(lease: Lease, today: date) -> bool:
    """Whether ``lease`` has ended by ``today``: its status ended, or its end date today or
    before."""
    if lease.collection_status == CollectionStatus.ENDED:
        return True
    return lease.ended_on is not None and lease.ended_on <= today


def lease_line(lease: Lease, today: date) -> str:
    """``{lease_line}``: :data:`LEASE_LINE_ENDED` once ``lease`` has ended, else
    :data:`LEASE_LINE_CURRENT`."""
    return LEASE_LINE_ENDED if lease_ended(lease, today) else LEASE_LINE_CURRENT


def pay_line(lease: Lease, today: date, values: Mapping[str, str]) -> str:
    """``{pay_line}``: :data:`PAY_LINE_ENDED` once ``lease`` has ended, else
    :data:`PAY_LINE_CURRENT`, rendered with ``values`` (every placeholder of :data:`FIELDS
    <ledger.merge.FIELDS>`; an empty one shows as itself, :func:`with_gaps`)."""
    sentence = PAY_LINE_ENDED if lease_ended(lease, today) else PAY_LINE_CURRENT
    return merge.render(sentence, with_gaps({**values, "pay_line": ""}))


# --- No empty placeholder leaves (owner, 2026-11-02) --------------------------------------------


def gap(name: str) -> str:
    """How an empty placeholder reads in a stored text: itself, ``{name}``."""
    return "{" + name + "}"


def with_gaps(values: Mapping[str, str]) -> dict[str, str]:
    """``values`` with each empty value replaced by its placeholder (:func:`gap`)."""
    return {name: value if value != "" else gap(name) for name, value in values.items()}


def settings_incomplete(company: CompanySettings) -> list[str]:
    """The company settings among :data:`REQUIRED_SETTINGS` that are blank."""
    return [name for name in REQUIRED_SETTINGS if not str(getattr(company, name) or "").strip()]


def missing(message: OutboundMessage) -> list[str]:
    """The placeholders of the message's template its stored subject or body still shows
    (an empty value, :func:`with_gaps`), in the order of the template's list."""
    text = f"{message.rendered_subject}\n{message.rendered_body}"
    return [name for name in merge.fields_for(message.template.code) if gap(name) in text]


def hold_reason(message: OutboundMessage, company: CompanySettings | None = None) -> str:
    """``settings_incomplete`` for a message that is not internal while a required setting
    is blank; ``missing_value`` for a text that shows a placeholder; ``""`` when it may go."""
    row = company or CompanySettings.read()
    if not message.template.internal and settings_incomplete(row):
        return HeldReason.SETTINGS_INCOMPLETE
    if missing(message):
        return HeldReason.MISSING_VALUE
    return ""


def fill_settings(message: OutboundMessage, company: CompanySettings) -> None:
    """Write the settings as they stand into the placeholders of a setting that the stored
    text shows (a message held while a setting was blank)."""
    fields = merge.fields_for(message.template.code)
    subject, body = message.rendered_subject, message.rendered_body
    for name, setting in SETTING_PLACEHOLDERS.items():
        value = str(getattr(company, setting) or "").strip()
        if name in fields and value:
            subject = subject.replace(gap(name), value)
            body = body.replace(gap(name), value)
    if (subject, body) == (message.rendered_subject, message.rendered_body):
        return
    message.rendered_subject = one_line(subject)[:SUBJECT_MAX_LENGTH]
    message.rendered_body = body
    message.body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()


def context_for(
    lease: Lease,
    charge: Subject,
    rule: str,
    *,
    today: date,
    amount: Decimal | None = None,
    debit_date: date | None = None,
    retry_date: date | None = None,
    days_late: int | None = None,
    return_reason: str = "",
    company: CompanySettings | None = None,
) -> dict[str, str]:
    """The 17 placeholder values of a message about ``charge`` billed through ``lease``
    (D4; ``{lease_line}`` and ``{pay_line}`` read the charge's own lease, else ``lease``).
    ``amount`` is the charge's balance unless given; ``debit_date`` its due date unless given;
    ``days_late`` counted from the due date (never below zero); ``retry_date`` and
    ``return_reason`` empty unless given (rule F). ``{pay_link}`` is the portal's Pay page
    of the store when the operator has a portal account that can sign in, else a new signed
    statement link (:func:`pay_link`, PLAN-014 H4). ``rule`` is accepted for the rules that
    will shape a value of their own. For a :class:`RentMonth` the amount is
    the scheduled one and both dates the scheduled debit (E4)."""
    row = company or CompanySettings.read()
    operator = operator_of(lease, charge)
    store = lease.store
    due = due_of(charge)
    late = days_late if days_late is not None else max((today - due).days, 0)
    last4 = lease_sync.billing_of(lease).bank_last4
    own = getattr(charge, "lease", None) or lease
    values = {
        "operator_name": str(operator.display_name) if operator is not None else "",
        "store_number": str(store.store_number) if store.store_number is not None else "",
        "store_name": str(store.external_label),
        "amount": format_amount(balance_of(charge) if amount is None else amount),
        "charge_name": charge_name(charge),
        "due_date": day_text(due),
        "debit_date": day_text(debit_date or due),
        "retry_date": day_text(retry_date) if retry_date is not None else "",
        "days_late": str(late),
        "bank_last4": last4 or NO_VALUE,
        "return_reason": return_reason,
        "pay_link": pay_link(operator, store.pk),
        "store_link": store_link(lease),
        "support_email": str(row.support_email or ""),
        "company_name": str(row.name),
        "lease_line": lease_line(own, today),
        "pay_line": "",
    }
    values["pay_line"] = pay_line(own, today, values)
    return values


# --- Queue (D3, D7) -------------------------------------------------------------------------


def _audit(
    action: str, message: OutboundMessage, actor: User | None, after: Mapping[str, object]
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY,
        entity_id=str(message.pk),
        before=None,
        after={
            "charge_type": message.charge_entity_type,
            "charge": str(message.charge_entity_id),
            "lease": str(message.lease_id) if message.lease_id else None,
            "rule": message.rule,
            "occurrence": message.occurrence,
            "template": message.template.code,
            "manual": message.manual,
            **after,
        },
    )


def manager_addresses() -> list[str]:
    """The company settings' managers (rule E's recipients); none when the list is empty."""
    try:
        return mail.address_lines(str(CompanySettings.read().manager_emails or ""))
    except ValueError:  # never saved so: the settings form refuses it
        return []


def managers_to() -> str:
    """ "the managers (2)": an internal message's recipients, the list as it stands now."""
    return MANAGERS_TO.format(count=len(manager_addresses()))


def _recipients(message: OutboundMessage) -> list[str]:
    """Where the message goes now: the managers' list for an internal message; else the
    billing e-mail of its lease, read again (it may have changed, or gone, since the queue);
    the address stored when it was queued only for a message without a lease."""
    if message.template.internal:
        return manager_addresses()
    if message.lease is not None:
        current = lease_sync.billing_of(message.lease).email
        return [current] if current else []
    return [message.to_address] if message.to_address else []


def refuse_retired(rule: str, template: MessageTemplate) -> None:
    """``ValueError`` for a retired rule (the old ladder) or an inactive template: neither
    may queue a message (owner, 2026-11-01)."""
    if rule in RETIRED_RULES:
        raise ValueError(RETIRED_RULE)
    if not template.active:
        raise ValueError(INACTIVE_TEMPLATE)


def queue(
    rule: str,
    lease: Lease | None,
    charge: Subject | LeaseEnd,
    template: MessageTemplate,
    context: Mapping[str, str],
    *,
    occurrence: int = 1,
    manual: bool = False,
    actor: User | None = None,
    now: datetime | None = None,
    document: Document | None = None,
    job: bool = True,
) -> OutboundMessage | None:
    """Render ``template`` with ``context`` and queue one message and its job (see the
    module docstring); ``None`` when the row exists already (the unique constraint).
    :class:`ChannelUnavailable` for an SMS template; :class:`~ledger.merge.TemplateRefused`
    when the values are not one code's placeholders. A rendered text holding payment data is
    stored as ``failed`` (``PaymentDataRefused``) without its text, and gets no job.
    ``ValueError`` and nothing written when ``lease`` is ``None``, the rule is retired or the
    template inactive (:func:`refuse_retired`). A :class:`RentMonth` is stored by its lease
    and month (``lease_month``, ``period_month``; E4). A message that may not go yet
    (:func:`hold_reason`: a required company setting blank, a placeholder without a value) is
    stored ``held``, without its job. ``job=False`` stores the row without its job and
    without that check: the daily job's :func:`combine` or :func:`wait_for_fold` decides what
    goes (and :func:`combine` holds the message it sends)."""
    refuse_retired(rule, template)
    if lease is None:
        raise ValueError(NO_LEASE)
    channel_for(template.channel)
    operator = operator_of(lease, charge)
    if operator is None:
        raise ValueError("A message needs the operator billed.")
    moment = now or timezone.now()
    values = with_gaps(context)
    subject = one_line(merge.render(template.subject, values))[:SUBJECT_MAX_LENGTH]
    body = merge.render(template.body, values)
    final = rule == MessageRule.FINAL_STATEMENT
    address = ""
    if template.internal:
        managers = manager_addresses()
        address = managers[0] if managers else ""
    else:
        address = lease_sync.billing_of(lease).email
    message = OutboundMessage(
        operator=operator,
        lease=lease,
        template=template,
        kind=MessageKind.FINAL_STATEMENT if final else MessageKind.LADDER,
        channel=template.channel,
        rule=rule,
        occurrence=occurrence,
        charge_entity_type=charge_type_of(charge),
        charge_entity_id=charge.lease.pk if isinstance(charge, RentMonth) else charge.pk,
        period_month=charge.month if isinstance(charge, RentMonth) else None,
        document=document,
        to_address=address,
        rendered_subject=subject,
        rendered_body=body,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        manual=manual,
        sent_by=actor,
        scheduled_for=scheduled_for(moment),
    )
    return _store(message, actor, job=job)


def queue_portal(
    template: MessageTemplate,
    portal_user_id: uuid.UUID,
    operator: Operator,
    to_address: str,
    context: Mapping[str, str],
    *,
    actor: User | None = None,
    now: datetime | None = None,
) -> OutboundMessage:
    """Queue a portal message (PLAN-014 H2; see the module docstring) about the portal
    account ``portal_user_id`` of ``operator`` to ``to_address``: by hand (``manual``, sent by
    ``actor``) when a staff member asked for it, else (the portal's own reset form) by no one.
    Raises :class:`ChannelUnavailable` for an SMS template and
    :class:`~ledger.merge.TemplateRefused` when ``context`` is not the template's
    placeholders; ``ValueError`` and nothing written for an inactive template. Held, without
    its job, while a required company setting is blank (:func:`hold_reason`)."""
    refuse_retired(MessageRule.PORTAL, template)
    channel_for(template.channel)
    values = with_gaps(context)
    subject = one_line(merge.render(template.subject, values))[:SUBJECT_MAX_LENGTH]
    body = merge.render(template.body, values)
    message = OutboundMessage(
        operator=operator,
        lease=None,
        template=template,
        kind=MessageKind.PORTAL,
        channel=template.channel,
        rule=MessageRule.PORTAL,
        charge_entity_type=ChargeEntityType.PORTAL_USER,
        charge_entity_id=portal_user_id,
        to_address=to_address,
        rendered_subject=subject,
        rendered_body=body,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        manual=actor is not None,
        sent_by=actor,
        scheduled_for=scheduled_for(now or timezone.now()),
    )
    stored = _store(message, actor)
    if stored is None:  # no unique rule covers a row without a lease: a broken CHECK
        raise RuntimeError("The portal message was not stored.")
    return stored


def _refuse_payment_data(message: OutboundMessage) -> None:
    """A text holding payment data: ``failed`` (``PaymentDataRefused``), without its text."""
    try:
        message.check_payment_data()
    except ValidationError:
        message.rendered_subject = message.rendered_body = message.body_sha256 = ""
        message.status = MessageStatus.FAILED
        message.error = PAYMENT_DATA_REFUSED


#: The audit action of a stored message, by its status.
STORED_ACTIONS: Mapping[str, str] = {
    MessageStatus.QUEUED: "message.queued",
    MessageStatus.HELD: "message.held",
}


def _hold_if_gaps(message: OutboundMessage) -> bool:
    """Hold a queued (or waiting) message that may not go yet (:func:`hold_reason`); whether
    it was."""
    reason = hold_reason(message)
    if not reason:
        return False
    message.status = MessageStatus.HELD
    message.held_reason = reason
    return True


def _store(
    message: OutboundMessage, actor: User | None, *, job: bool = True
) -> OutboundMessage | None:
    """Save a rendered message and its ``message_send`` job (unless ``job`` is false),
    audited; ``None`` when the unique constraint finds the row already there. A text holding
    payment data is stored as ``failed`` (``PaymentDataRefused``) without its text, and gets
    no job; one with its job that may not go yet (:func:`hold_reason`) is stored ``held``,
    without a job."""
    _refuse_payment_data(message)
    if job and message.status == MessageStatus.QUEUED:
        _hold_if_gaps(message)
    audited: dict[str, object] = {"status": message.status, "error": message.error}
    if message.status == MessageStatus.HELD:
        audited["held_reason"] = message.held_reason
    if message.status in (MessageStatus.FAILED, MessageStatus.HELD):  # as a send's reads
        audited["recipients"] = 0
    try:
        with transaction.atomic():
            message.save()
            if job and message.status == MessageStatus.QUEUED:
                jobs.enqueue(
                    JobKind.MESSAGE_SEND, message, None, actor, run_after=message.scheduled_for
                )
            _audit(STORED_ACTIONS.get(message.status, "message.failed"), message, actor, audited)
    except IntegrityError:
        return None
    return message


# --- One e-mail per lease and day (owner, 2026-11-02) ----------------------------------------


@dataclass(frozen=True, slots=True)
class ChargeLine:
    """One charge a combined message lists: its name, what is owed, its due date (a lease
    month's debit date) and the days it is late (never below zero)."""

    name: str
    amount: Decimal
    due: date
    days_late: int


def line_for(charge: Subject, amount: Decimal, today: date) -> ChargeLine:
    due = due_of(charge)
    return ChargeLine(charge_name(charge), amount, due, max((today - due).days, 0))


def charges_text(lines: Iterable[ChargeLine]) -> str:
    """The list under a combined message's text: a heading, one line per charge, the
    total."""
    found = list(lines)
    rows = [
        CHARGE_LINE.format(
            name=line.name,
            amount=format_amount(line.amount),
            due=day_text(line.due),
            days_late=line.days_late,
        )
        for line in found
    ]
    total = sum((line.amount for line in found), Decimal("0"))
    return "\n".join(
        [
            CHARGES_HEADING.format(count=len(found)),
            *rows,
            CHARGES_TOTAL.format(amount=format_amount(total)),
        ]
    )


def charges_of(messages: Iterable[OutboundMessage]) -> dict[uuid.UUID, Charge]:
    """Message -> the charge (rent, one-off or insurance) it is about, with its lease and
    store; a message about anything else, or a charge gone, is left out (three queries at
    most)."""
    found = list(messages)
    models: dict[str, type[RentCharge] | type[OneOffCharge] | type[InsuranceCharge]] = {
        ChargeEntityType.RENT_CHARGE: RentCharge,
        ChargeEntityType.ONE_OFF_CHARGE: OneOffCharge,
        ChargeEntityType.INSURANCE_CHARGE: InsuranceCharge,
    }
    charges: dict[uuid.UUID, Charge] = {}
    for kind, model in models.items():
        ids = {m.charge_entity_id for m in found if m.charge_entity_type == kind}
        if not ids:
            continue
        loaded = model.objects.select_related("lease__store", "lease__operator").filter(pk__in=ids)
        by_id: dict[uuid.UUID, Charge] = {charge.pk: charge for charge in loaded}
        for message in found:
            if message.charge_entity_type == kind and message.charge_entity_id in by_id:
                charges[message.pk] = by_id[message.charge_entity_id]
    return charges


def _list_start(body: str) -> int:
    """Where the list of a combined message's body begins (``-1`` when it has none)."""
    return body.rfind("\n\n" + CHARGES_HEADING.split("(", 1)[0])


def _month_line(message: OutboundMessage, today: date) -> ChargeLine:
    """The line of a message about a lease month (E4), read again: its scheduled debit and
    amount (the rent schedule's for A, what the rent run charges for B)."""
    lease = message.lease
    month = message.period_month
    if lease is None or month is None or lease.debit_day is None:  # closed: never listed
        raise ValueError("A lease month's message names its lease and month.")
    debit = rent_run.debit_date(lease, month, lease.debit_day)
    amount: Decimal | None
    if message.rule == MessageRule.A:
        amount = (
            CollectionSchedule.objects.filter(
                lease=lease, kind=ScheduleKind.RENT, state=ScheduleState.ACTIVE
            )
            .order_by("seq")
            .values_list("amount", flat=True)
            .first()
        )
    else:
        amount = rent_run.first_debit_amount(lease, month)
    owed = amount if amount is not None else Decimal("0")
    return line_for(RentMonth(lease, month, debit, owed), owed, today)


def _regroup(message: OutboundMessage, now: datetime) -> OutboundMessage:
    """At the send of a combined message (owner, 2026-11-02): when some of the charges it
    lists closed since the queue, those rows are ``suppressed`` (``ChargeClosed``) and left
    out, and the most urgent row still open (:data:`COMBINED_RULES`) is sent for the rest,
    its own text with the list of the open charges (and their total) rebuilt; the rows still
    open are combined into it. The message to deliver is returned: ``message`` itself when
    nothing closed, or when everything did (the send then suppresses it as before)."""
    listed = list(
        message.combined.select_for_update(of=("self",))
        .select_related("operator", "lease", "template")
        .order_by("queued_at", "id")
    )
    if not listed:
        return message
    members = [message, *listed]
    still = [row for row in members if not _closed_reason(row)]
    if not still or len(still) == len(members):
        return message
    still.sort(key=lambda row: (COMBINED_RULES.index(row.rule), row is not message))
    sending = still[0]
    today = timezone.localdate(now)
    charges = charges_of(row for row in still if row.period_month is None)
    lines = [
        _month_line(row, today)
        if row.period_month is not None
        else line_for(charges[row.pk], balance_of(charges[row.pk]), today)
        for row in still
    ]
    body = sending.rendered_body
    if sending is message and _list_start(body) >= 0:
        body = body[: _list_start(body)]
    if len(still) > 1:
        body = f"{body}\n\n{charges_text(lines)}"
    for row in members:
        if row not in still and row is not message:
            row.combined_into = None
            _finish(row, Disposition(MessageStatus.SUPPRESSED, error=CHARGE_CLOSED))
    if sending is not message:
        sending.status = MessageStatus.QUEUED
        sending.combined_into = None
        sending.scheduled_for = message.scheduled_for
    sending.rendered_body = body
    sending.body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    _refuse_payment_data(sending)
    sending.save()
    for row in still[1:]:
        row.combined_into = sending
        row.save(update_fields=["combined_into"])
    if sending is not message:
        message.combined_into = None
        _finish(message, Disposition(MessageStatus.SUPPRESSED, error=CHARGE_CLOSED))
        _audit(
            "message.regrouped", sending, None, {"status": sending.status, "for": str(message.pk)}
        )
    return sending


def _morning_of(day: date) -> datetime:
    return datetime.combine(day, WINDOW_OPENS, tzinfo=NEW_YORK).astimezone(UTC)


def wait_for_fold(messages: Iterable[OutboundMessage], until: date) -> None:
    """Hold ``messages`` (queued, without a job) back for the 3-day fold: ``waiting`` until
    the morning of ``until`` (``scheduled_for``), audited ``message.waiting``."""
    for message in messages:
        if message.status != MessageStatus.QUEUED:
            continue
        message.status = MessageStatus.WAITING
        message.scheduled_for = _morning_of(until)
        message.save(update_fields=["status", "scheduled_for"])
        _audit("message.waiting", message, None, {"status": message.status, "until": str(until)})


def release(messages: Iterable[OutboundMessage]) -> list[OutboundMessage]:
    """The waiting ``messages`` that still go: one whose charge closed since (or whose
    operator was archived) is ``suppressed`` as at a send, and left out."""
    found: list[OutboundMessage] = []
    for message in messages:
        reason = _suppress_reason(message)
        if reason:
            _finish(message, Disposition(MessageStatus.SUPPRESSED, error=reason))
        else:
            found.append(message)
    return found


def combine(
    rows: list[tuple[OutboundMessage, ChargeLine]], *, now: datetime | None = None
) -> OutboundMessage:
    """Send the first of ``rows`` for all of them (the module docstring; the caller puts the
    most urgent first): with more than one, its body gains :func:`charges_text` and the
    others become ``combined`` into it. Its ``message_send`` job is queued (a waiting row is
    queued again, for the send window from ``now``), unless it may not go yet
    (:func:`hold_reason`): then it is ``held``, audited ``message.held``, without a job."""
    primary = rows[0][0]
    if len(rows) > 1:
        lines = [line for _, line in rows]
        body = f"{primary.rendered_body}\n\n{charges_text(lines)}"
        primary.rendered_body = body
        primary.body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        _refuse_payment_data(primary)
    if primary.status == MessageStatus.WAITING:
        primary.status = MessageStatus.QUEUED
        primary.scheduled_for = scheduled_for(now or timezone.now())
    held = primary.status == MessageStatus.QUEUED and _hold_if_gaps(primary)
    primary.save()
    if held:
        _audit(
            "message.held",
            primary,
            None,
            {"status": primary.status, "held_reason": primary.held_reason, "recipients": 0},
        )
    if primary.status == MessageStatus.QUEUED:
        jobs.enqueue(JobKind.MESSAGE_SEND, primary, None, None, run_after=primary.scheduled_for)
    for message, _ in rows[1:]:
        message.status = MessageStatus.COMBINED
        message.combined_into = primary
        message.save(update_fields=["status", "combined_into"])
        _audit(
            "message.combined",
            message,
            None,
            {"status": message.status, "into": str(primary.pk)},
        )
    return primary


# --- Send (D7) ------------------------------------------------------------------------------


def _charge_open(charge_type: str, charge_id: uuid.UUID) -> bool:
    if charge_type == ChargeEntityType.RENT_CHARGE:
        rent = RentCharge.objects.filter(pk=charge_id).first()
        return rent is not None and rent.balance > 0 and rent.status not in CLOSED_RENT
    if charge_type == ChargeEntityType.ONE_OFF_CHARGE:
        one_off = OneOffCharge.objects.select_related("fee_type").filter(pk=charge_id).first()
        if one_off is None:
            return False
        refundable = one_off.fee_type is not None and one_off.fee_type.refundable
        return one_off.balance > 0 and one_off.status not in CLOSED_ONE_OFF and not refundable
    if charge_type == ChargeEntityType.INSURANCE_CHARGE:
        found = InsuranceCharge.objects.filter(pk=charge_id).first()
        return found is not None and insurance.balance(found) > 0
    return False


def rent_schedule_active(lease_id: uuid.UUID) -> bool:
    """Whether the lease's rent is on autopay: a rent collection schedule of it is active."""
    return CollectionSchedule.objects.filter(
        lease_id=lease_id, kind=ScheduleKind.RENT, state=ScheduleState.ACTIVE
    ).exists()


def month_settled(lease_id: uuid.UUID, month: date) -> bool:
    """Whether the month's (first) rent charge exists already and is closed to messages:
    paid (in advance through the invoice builder, or collected), pending or waived."""
    return RentCharge.objects.filter(
        lease_id=lease_id, period_month=month, seq=1, status__in=CLOSED_RENT
    ).exists()


def lease_ended_by(lease: Lease, day: date) -> bool:
    """Whether the lease is ended, or ends before ``day``: it gets no rent of that day."""
    if lease.collection_status == CollectionStatus.ENDED:
        return True
    return lease.ended_on is not None and lease.ended_on < day


def month_closed_reason(message: OutboundMessage) -> str:
    """Why a message about a lease month (E4) is no longer to be sent, checked again at the
    send as at the queue: ``LeaseEnded`` (the lease ended, or ends before the month's debit
    as its debit day now falls), ``ScheduleStopped`` (an A whose rent schedule is no longer
    on: the debit it announces will not happen), ``MonthSettled`` (the month's rent charge
    exists and is paid, pending or waived); ``""`` when it still goes."""
    lease = message.lease
    month = message.period_month
    if lease is None or month is None or lease.debit_day is None:
        return LEASE_ENDED
    if lease_ended_by(lease, rent_run.debit_date(lease, month, lease.debit_day)):
        return LEASE_ENDED
    if message.rule == MessageRule.A and not rent_schedule_active(lease.pk):
        return SCHEDULE_STOPPED
    if month_settled(lease.pk, month):
        return MONTH_SETTLED
    return ""


def _closed_reason(message: OutboundMessage) -> str:
    """Why what ``message`` is about no longer calls for it (its charge or month closed, its
    statement or link gone); ``""`` when it still does."""
    from ledger.services import lease_end

    if message.kind == MessageKind.PORTAL:
        issuer = ONE_TIME_LINKS.get(message.template.code)
        if issuer is None:
            return LINK_UNAVAILABLE
        return issuer.refusal(message)
    if message.charge_entity_type == ChargeEntityType.LEASE_MONTH:
        return month_closed_reason(message)
    if message.rule == MessageRule.FINAL_STATEMENT:
        return "" if lease_end.statement_ready(message) else STATEMENT_MISSING
    if message.rule in OPERATOR_FACING_RULES and not _charge_open(
        message.charge_entity_type, message.charge_entity_id
    ):
        return CHARGE_CLOSED
    return ""


def _suppress_reason(message: OutboundMessage) -> str:
    """Why ``message`` is not to be sent at all (a combined message is first rebuilt from
    its charges still open, :func:`_regroup`)."""
    reason = _closed_reason(message)
    if reason:
        return reason
    if not message.template.internal and message.operator.archived_at is not None:
        return OPERATOR_ARCHIVED
    return ""


def _finish(message: OutboundMessage, disposition: Disposition, recipients: int = 0) -> str:
    """Record ``disposition`` on the row, audit it with the number of addresses
    the send was for (``recipients``), and return the job's sentence."""
    message.status = disposition.status
    message.held_reason = disposition.reason if disposition.status == MessageStatus.HELD else ""
    message.error = disposition.error
    if disposition.provider_message_id:
        message.mail_message_id = disposition.provider_message_id
    if disposition.status == MessageStatus.SENT:
        message.sent_at = disposition.sent_at or timezone.now()
    message.save()
    action = {
        MessageStatus.SENT: "message.sent",
        MessageStatus.HELD: "message.held",
        MessageStatus.SUPPRESSED: "message.suppressed",
    }.get(MessageStatus(disposition.status), "message.failed")
    _audit(
        action,
        message,
        None,
        {
            "status": message.status,
            "held_reason": message.held_reason,
            "error": message.error,
            "recipients": recipients,
        },
    )
    if disposition.status == MessageStatus.SENT:
        return SENT_TEXT
    if disposition.status == MessageStatus.HELD:
        return HELD_TEXT.format(reason=HeldReason(message.held_reason).label)
    if disposition.status == MessageStatus.SUPPRESSED:
        return SUPPRESSED_TEXT.format(cause=message.error)
    return FAILED_TEXT.format(cause=message.error)


def _deliver(message: OutboundMessage, recipients: list[str]) -> Disposition:
    """Hand the stored text to the channel, once per recipient; the first failure, else a
    send, else the first hold, decides the row."""
    from ledger.services import lease_end

    attachments: tuple[mail.Attachment, ...] = ()
    if message.rule == MessageRule.FINAL_STATEMENT:
        try:
            attachments = (lease_end.statement_attachment(message),)
        except Exception as error:  # a storage or permission failure: its class name only
            return Disposition(MessageStatus.FAILED, error=type(error).__name__)
    channel = channel_for(message.channel)
    body = message.rendered_body
    link: OneTimeLink | None = None
    if message.kind == MessageKind.PORTAL:
        # PLAN-014 H2: the one-time link goes into the outgoing text only, never the row.
        link = ONE_TIME_LINKS[message.template.code].issue(message)
        body = body.replace(LINK_ADDED_AT_SEND, link.url)
    found = [
        channel.send(Rendered(to, message.rendered_subject, body, attachments)) for to in recipients
    ]
    chosen = Disposition(MessageStatus.HELD, HeldReason.NO_ADDRESS)
    for status in (MessageStatus.FAILED, MessageStatus.SENT, MessageStatus.HELD):
        match = next((item for item in found if item.status == status), None)
        if match is not None:
            chosen = match
            break
    if link is not None:
        if chosen.status == MessageStatus.SENT:
            link.keep()
        else:
            link.drop()
    return chosen


def send(message_id: uuid.UUID, now: datetime | None = None) -> Outcome:
    """The ``message_send`` job: deliver one queued message (see the module docstring)."""
    with transaction.atomic():
        message = (
            OutboundMessage.objects.select_for_update(of=("self",))
            .select_related("operator", "lease", "template", "document", "sent_by")
            .filter(pk=message_id)
            .first()
        )
        if message is None:
            return Outcome(False, GONE)
        if message.status != MessageStatus.QUEUED:
            return Outcome(True, NOT_QUEUED.format(status=message.get_status_display().lower()))
        if message.kind == MessageKind.LADDER:
            message = _regroup(message, now or timezone.now())
            if message.status != MessageStatus.QUEUED:  # a text refused for payment data
                return Outcome(True, NOT_QUEUED.format(status=message.get_status_display().lower()))
        reason = _suppress_reason(message)
        if reason:
            return Outcome(
                True, _finish(message, Disposition(MessageStatus.SUPPRESSED, error=reason))
            )
        internal = message.template.internal
        portal = message.kind == MessageKind.PORTAL
        if not internal and not portal and message.operator.handled_personally:
            return Outcome(
                True,
                _finish(message, Disposition(MessageStatus.HELD, HeldReason.HANDLED_PERSONALLY)),
            )
        company = CompanySettings.read()
        fill_settings(message, company)
        gaps = hold_reason(message, company)
        if gaps:
            return Outcome(True, _finish(message, Disposition(MessageStatus.HELD, gaps)))
        recipients = _recipients(message)
        if not recipients:
            held = HeldReason.NO_MANAGER_LIST if internal else HeldReason.NO_ADDRESS
            return Outcome(True, _finish(message, Disposition(MessageStatus.HELD, held)))
        message.to_address = recipients[0]
        disposition = _deliver(message, recipients)
        text = _finish(message, disposition, len(recipients))
        return Outcome(disposition.status != MessageStatus.FAILED, text)


# --- By hand (D6) and from the Outbox (D5) ---------------------------------------------------


def manual_templates() -> list[MessageTemplate]:
    """What "Send a message now" offers: the active e-mail templates for operators."""
    return list(
        MessageTemplate.objects.filter(active=True, internal=False, channel=MessageChannel.EMAIL)
        .exclude(code__in=(merge.FINAL_STATEMENT_CODE, *merge.PORTAL_CODES))
        .order_by("name", "code")
    )


def _load_charge(charge_type: str, charge_id: uuid.UUID) -> Charge | None:
    if charge_type == ChargeEntityType.RENT_CHARGE:
        return (
            RentCharge.objects.select_related("lease__store", "lease__operator")
            .filter(pk=charge_id)
            .first()
        )
    if charge_type == ChargeEntityType.ONE_OFF_CHARGE:
        return (
            OneOffCharge.objects.select_related(
                "operator", "fee_type", "lease__store", "lease__operator"
            )
            .filter(pk=charge_id)
            .first()
        )
    return None


def send_by_hand(
    charge_type: str,
    charge_id: uuid.UUID,
    template_id: uuid.UUID | None,
    actor: User,
    *,
    now: datetime | None = None,
) -> Outcome:
    """Queue ``template`` for one open charge by hand (``rule`` ``manual``): refused, with a
    sentence and nothing written, for a closed charge, an archived or personally handled
    operator, a lease without a billing e-mail, or a template not offered."""
    moment = now or timezone.now()
    template = next(
        (row for row in manual_templates() if template_id is not None and row.pk == template_id),
        None,
    )
    if template is None:
        return Outcome(False, HAND_UNKNOWN_TEMPLATE)
    charge = _load_charge(charge_type, charge_id)
    if charge is None or not _charge_open(charge_type, charge_id):
        return Outcome(False, HAND_NOT_OPEN)
    lease = leases.billing_lease(charge)
    operator = operator_of(lease, charge)
    if operator is None:
        return Outcome(False, HAND_NOT_OPEN)
    if operator.archived_at is not None:
        return Outcome(False, HAND_ARCHIVED)
    if operator.handled_personally:
        return Outcome(False, HAND_OPTED_OUT)
    if lease is None or not lease_sync.billing_of(lease).email:
        return Outcome(False, HAND_NO_EMAIL)
    context = context_for(lease, charge, MessageRule.MANUAL, today=timezone.localdate(moment))
    with transaction.atomic():
        queue(
            MessageRule.MANUAL,
            lease,
            charge,
            template,
            context,
            manual=True,
            actor=actor,
            now=moment,
        )
    return Outcome(True, HAND_QUEUED)


def send_now(message_id: uuid.UUID, actor: User) -> Outcome:
    """Send a held message again (the Outbox's "Send now", admin): refused while mail is not
    set up, while a required company setting is blank (a message that is not internal), or, on
    the sandbox, while its address is not on the allowlist; otherwise the row is queued again
    and its job runs at once. Audited ``message.send_now``. A message held
    ``settings_incomplete`` is released with the other such holds of its lease, grouped as at
    the daily job (:func:`plan_release`)."""
    with transaction.atomic():
        message = (
            OutboundMessage.objects.select_for_update(of=("self",))
            .select_related("template", "lease")
            .filter(pk=message_id)
            .first()
        )
        if message is None:
            return Outcome(False, GONE)
        if message.status != MessageStatus.HELD:
            return Outcome(False, SEND_NOW_NOT_HELD)
        found = mail.mail_settings()
        if not mail.is_set_up(found):
            return Outcome(False, SEND_NOW_NOT_SET_UP)
        if not message.template.internal and settings_incomplete(CompanySettings.read()):
            return Outcome(False, SEND_NOW_SETTINGS)
        recipients = _recipients(message)
        if not recipients:
            return Outcome(False, SEND_NOW_NO_ADDRESS)
        if not all(allowlisted(address, found) for address in recipients):
            return Outcome(False, SEND_NOW_NOT_ALLOWED)
        if message.held_reason == HeldReason.SETTINGS_INCOMPLETE:
            return _send_now_for_settings(message, actor)
        before = {"status": message.status, "held_reason": message.held_reason}
        message.status = MessageStatus.QUEUED
        message.held_reason = ""
        message.save(update_fields=["status", "held_reason"])
        jobs.enqueue(JobKind.MESSAGE_SEND, message, None, actor)
        AuditLog.objects.create(
            actor=actor,
            action="message.send_now",
            entity_type=ENTITY,
            entity_id=str(message.pk),
            before=before,
            after={"status": message.status},
        )
    return Outcome(True, SEND_NOW_QUEUED)


def held_for_settings() -> QuerySet[OutboundMessage]:
    """The messages held ``settings_incomplete``, oldest first."""
    return OutboundMessage.objects.filter(
        status=MessageStatus.HELD, held_reason=HeldReason.SETTINGS_INCOMPLETE
    ).order_by("queued_at", "id")


@dataclass(frozen=True, slots=True)
class Release:
    """What releasing the messages held for the settings does (owner, 2026-11-02): ``held``
    rows (the holds counted), ``sends`` (each one e-mail: the message to send first, then the
    rows combined into it) and ``suppressed`` (each row and why it no longer goes)."""

    held: int
    sends: list[list[OutboundMessage]]
    suppressed: list[tuple[OutboundMessage, str]]

    @property
    def emails(self) -> int:
        return len(self.sends)

    @property
    def text(self) -> str:
        """ "3 held messages → 1 e-mail, 2 superseded"."""
        return RELEASE_TEXT.format(
            held=self.held,
            held_plural="" if self.held == 1 else "s",
            emails=self.emails,
            emails_plural="" if self.emails == 1 else "s",
            suppressed=len(self.suppressed),
        )


def _groups(message: OutboundMessage) -> bool:
    """Whether ``message`` is released grouped with its lease's (an operator's A to D)."""
    return (
        message.kind == MessageKind.LADDER
        and message.rule in COMBINED_RULES
        and message.lease_id is not None
    )


def _charge_key(message: OutboundMessage) -> tuple[object, ...]:
    """What ``message`` is about, for the release: its charge; a lease month's rent charge
    once it exists (so its B and the charge's C or D are one), else the month."""
    if message.period_month is None or message.lease_id is None:
        return (message.charge_entity_type, message.charge_entity_id)
    rent = (
        RentCharge.objects.filter(
            lease_id=message.lease_id, period_month=message.period_month, seq=1
        )
        .values_list("pk", flat=True)
        .first()
    )
    if rent is not None:
        return (ChargeEntityType.RENT_CHARGE, rent)
    return (ChargeEntityType.LEASE_MONTH, message.lease_id, message.period_month)


def _overtaken(message: OutboundMessage, today: date) -> str:
    """``DebitPassed`` for an A whose debit date is before ``today``; ``""`` otherwise."""
    lease = message.lease
    if message.rule != MessageRule.A or message.period_month is None or lease is None:
        return ""
    if lease.debit_day is None:
        return ""
    debit = rent_run.debit_date(lease, message.period_month, lease.debit_day)
    return DEBIT_PASSED if debit < today else ""


def plan_release(held: list[OutboundMessage], today: date) -> Release:
    """How ``held`` (messages held ``settings_incomplete``) go on ``today``, writing nothing:
    an operator's A to D per lease as at the daily job, with the rows combined into them —
    a row whose charge closed or month settled (:func:`_suppress_reason`), or an A whose debit
    has passed, is suppressed; of the rows about one charge only the most urgent stays (D > C
    > B > A; the others ``Superseded``); the rest is one e-mail under the most urgent. Any
    other message (a portal message, a final statement, F) goes as it is."""
    found = list(held)
    members = found + list(
        OutboundMessage.objects.filter(
            status=MessageStatus.COMBINED, combined_into__in=[row.pk for row in found]
        ).select_related("operator", "lease", "template")
    )
    sends: list[list[OutboundMessage]] = []
    suppressed: list[tuple[OutboundMessage, str]] = []
    by_lease: dict[uuid.UUID, list[OutboundMessage]] = {}
    for row in members:
        if _groups(row) and row.lease_id is not None:
            by_lease.setdefault(row.lease_id, []).append(row)
        else:
            sends.append([row])

    def rank(row: OutboundMessage) -> tuple[int, datetime, uuid.UUID]:
        return (COMBINED_RULES.index(row.rule), row.queued_at, row.pk)

    for rows in by_lease.values():
        by_charge: dict[tuple[object, ...], list[OutboundMessage]] = {}
        for row in rows:
            reason = _suppress_reason(row) or _overtaken(row, today)
            if reason:
                suppressed.append((row, reason))
            else:
                by_charge.setdefault(_charge_key(row), []).append(row)
        still: list[OutboundMessage] = []
        for same in by_charge.values():
            same.sort(key=rank)
            still.append(same[0])
            suppressed.extend((row, SUPERSEDED) for row in same[1:])
        if still:
            sends.append(sorted(still, key=rank))
    return Release(len(found), sends, suppressed)


def _apply_release(release: Release, actor: User, today: date) -> None:
    """Write ``release``: each send's first message queued with its job at once (its body
    listing every charge of the send when there is more than one, as :func:`combine`), the
    others combined into it, the suppressed rows suppressed; audited."""
    for group in release.sends:
        primary, rest = group[0], group[1:]
        before = {"status": primary.status, "held_reason": primary.held_reason}
        primary.status = MessageStatus.QUEUED
        primary.held_reason = ""
        primary.combined_into = None
        if _groups(primary):
            body = primary.rendered_body
            if _list_start(body) >= 0:
                body = body[: _list_start(body)]
            if rest:
                charges = charges_of(row for row in group if row.period_month is None)
                lines = [
                    _month_line(row, today)
                    if row.period_month is not None
                    else line_for(charges[row.pk], balance_of(charges[row.pk]), today)
                    for row in group
                ]
                body = f"{body}\n\n{charges_text(lines)}"
            primary.rendered_body = body
            primary.body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
            _refuse_payment_data(primary)
        primary.save()
        if primary.status == MessageStatus.QUEUED:
            jobs.enqueue(JobKind.MESSAGE_SEND, primary, None, actor)
        AuditLog.objects.create(
            actor=actor,
            action="message.send_held_for_settings",
            entity_type=ENTITY,
            entity_id=str(primary.pk),
            before=before,
            after={"status": primary.status, "combined": len(rest)},
        )
        for row in rest:
            row.status = MessageStatus.COMBINED
            row.held_reason = ""
            row.combined_into = primary
            row.save(update_fields=["status", "held_reason", "combined_into"])
            _audit("message.combined", row, actor, {"status": row.status, "into": str(primary.pk)})
    for row, reason in release.suppressed:
        row.combined_into = None
        _finish(row, Disposition(MessageStatus.SUPPRESSED, error=reason))


def _send_now_for_settings(message: OutboundMessage, actor: User) -> Outcome:
    """*Send now* on a message held ``settings_incomplete``: released with every such hold of
    its lease (the caller holds the row lock and checked the settings and the address)."""
    held = [message]
    if _groups(message) and message.lease_id is not None:
        held += list(
            held_for_settings()
            .filter(lease_id=message.lease_id)
            .exclude(pk=message.pk)
            .select_for_update(of=("self",))
            .select_related("operator", "lease", "template")
        )
    message = (
        OutboundMessage.objects.select_related("operator", "lease", "template")
        .filter(pk=message.pk)
        .get()
    )
    held[0] = message
    today = timezone.localdate()
    release = plan_release(held, today)
    _apply_release(release, actor, today)
    reason = next((why for row, why in release.suppressed if row.pk == message.pk), "")
    if reason:
        return Outcome(True, SEND_NOW_SUPPRESSED.format(cause=reason))
    return Outcome(True, SEND_NOW_QUEUED)


def release_preview(today: date | None = None) -> Release:
    """What "Send all held for settings" would do now (the page's confirmation)."""
    return plan_release(
        list(held_for_settings().select_related("operator", "lease", "template")),
        today or timezone.localdate(),
    )


def send_held_for_settings(actor: User, count: int) -> Outcome:
    """The Outbox's "Send all held for settings" (admin; owner, 2026-11-02): release every
    message held ``settings_incomplete`` as :func:`plan_release` groups them (one e-mail per
    lease under the most urgent rule, what the day has overtaken suppressed), each send's job
    at once, audited ``message.send_held_for_settings``. Refused, and nothing written, while a
    required setting is blank or mail is not set up, when none is held so, or when ``count``
    (the holds the page showed and the admin confirmed) is no longer their number. The send
    checks each again."""
    with transaction.atomic():
        if settings_incomplete(CompanySettings.read()):
            return Outcome(False, RELEASE_STILL_INCOMPLETE)
        if not mail.is_set_up():
            return Outcome(False, SEND_NOW_NOT_SET_UP)
        found = list(
            held_for_settings()
            .select_for_update(of=("self",))
            .select_related("operator", "lease", "template")
        )
        if not found:
            return Outcome(False, RELEASE_NONE)
        if len(found) != count:
            return Outcome(False, RELEASE_COUNT_CHANGED)
        today = timezone.localdate()
        release = plan_release(found, today)
        _apply_release(release, actor, today)
    return Outcome(True, RELEASE_QUEUED.format(text=release.text))


# --- Pages ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogRow:
    """One message as the operator and charge pages list it: the address masked."""

    when: object
    #: The template's name, and the rule's label.
    template: str
    rule: str
    status: str
    status_label: str
    held_reason: str
    to: str
    manual: bool
    error: str
    charge_type: str
    charge_id: uuid.UUID
    #: Who sent it by hand, or ``None`` for a rule; shown through ``|staff``.
    sent_by: User | None
    #: "Rent October 2026" for a message about a lease month (E4: ``charge_id`` is then the
    #: lease); empty otherwise.
    charge_name: str = ""


def held_text(message: OutboundMessage) -> str:
    """Why a held message waits, as the Outbox says it: the held reason's label; for the two
    holds of an empty placeholder, what to do or the placeholder ("Missing value:
    {support_email}")."""
    if message.held_reason == HeldReason.SETTINGS_INCOMPLETE:
        return SETTINGS_INCOMPLETE_TEXT
    if message.held_reason == HeldReason.MISSING_VALUE:
        names = missing(message)
        if names:
            text = MISSING_VALUE_TEXT.format(name=gap(names[0]))
            fix = MISSING_VALUE_FIX.get(names[0])
            if fix is None:
                return text
            store = message.lease.store if message.lease is not None else None
            return f"{text} — " + fix.format(
                store=str(store.external_label) if store is not None else "of the lease",
                operator=str(message.operator.display_name) or "of the message",
            )
    return str(HeldReason(message.held_reason).label)


def status_text(message: OutboundMessage) -> str:
    """ "Held (held on sandbox)", "Failed (ConnectTimeout)", "Sent"…; a combined row carries
    the outcome of the message sent for it ("Combined into the D message: Sent"), a waiting
    one its day ("Waiting for the 3-day fold (until Nov 5, 2026)")."""
    label = message.get_status_display()
    if message.status == MessageStatus.COMBINED and message.combined_into is not None:
        primary = message.combined_into
        return COMBINED_TEXT.format(rule=primary.rule, outcome=status_text(primary))
    if message.status == MessageStatus.WAITING and message.scheduled_for is not None:
        return WAITING_TEXT.format(label=label, day=day_text(message.scheduled_for))
    if message.status == MessageStatus.HELD and message.held_reason:
        return f"{label} ({held_text(message)})"
    if message.error:
        return f"{label} ({message.error})"
    return str(label)


def _rows(found: Iterable[OutboundMessage]) -> list[LogRow]:
    return [
        LogRow(
            when=message.sent_at or message.queued_at,
            template=message.template.display_name,
            rule=message.get_rule_display(),
            status=message.status,
            status_label=message.get_status_display(),
            held_reason=(HeldReason(message.held_reason).label if message.held_reason else ""),
            to=mask_address(message.to_address),
            manual=message.manual,
            error=message.error,
            charge_type=message.charge_entity_type,
            charge_id=message.charge_entity_id,
            sent_by=message.sent_by,
            charge_name=(
                rent_month_name(message.period_month) if message.period_month is not None else ""
            ),
        )
        for message in found
    ]


#: Most messages the operator page lists.
LOG_LIMIT = 50


def operator_log(operator: Operator, limit: int) -> list[LogRow]:
    """The operator's messages, newest first."""
    return _rows(
        OutboundMessage.objects.filter(operator=operator)
        .select_related("template", "sent_by")
        .order_by("-queued_at", "-id")[:limit]
    )


def charge_log(charge_type: str, charge_id: uuid.UUID) -> list[LogRow]:
    """The charge's messages, newest first. A rent charge's log also lists its month's A
    and B (E4: stored by the lease and the month, ``lease_month``), named "Rent October
    2026"."""
    about = Q(charge_entity_type=charge_type, charge_entity_id=charge_id)
    if charge_type == ChargeEntityType.RENT_CHARGE:
        rent = RentCharge.objects.filter(pk=charge_id).values("lease_id", "period_month").first()
        if rent is not None:
            about |= Q(
                charge_entity_type=ChargeEntityType.LEASE_MONTH,
                charge_entity_id=rent["lease_id"],
                period_month=rent["period_month"],
            )
    return _rows(
        OutboundMessage.objects.filter(about)
        .select_related("template", "sent_by")
        .order_by("-queued_at", "-id")
    )


def charge_context(
    charge_type: str, charge_id: uuid.UUID, *, billing: bool, url: str
) -> dict[str, object]:
    """What a charge page shows of its messages: the log, and "Send a message now" when the
    user is billing and the charge is open."""
    templates = manual_templates() if billing else []
    can_send = billing and bool(templates) and _charge_open(charge_type, charge_id)
    return {
        "collection_log": charge_log(charge_type, charge_id),
        "message_templates": templates,
        "can_send_message": can_send,
        "send_message_url": url,
    }


def for_store(store_id: uuid.UUID) -> QuerySet[OutboundMessage]:
    """The messages about a lease of the store, newest first (the timeline, D5)."""
    return (
        OutboundMessage.objects.filter(lease__store_id=store_id)
        .select_related("template", "sent_by", "combined_into")
        .order_by("-queued_at", "-id")
    )


def outbox(
    *,
    status: str = "",
    since: date | None = None,
    until: date | None = None,
    held_reason: str = "",
) -> QuerySet[OutboundMessage]:
    """Every message, newest first, for the Outbox (D5): of ``status`` when given, held for
    ``held_reason`` when given, queued from ``since`` to ``until`` (New York days, both
    included) when given."""
    found = OutboundMessage.objects.select_related(
        "template", "operator", "lease__store", "sent_by", "combined_into"
    ).order_by("-queued_at", "-id")
    if status:
        found = found.filter(status=status)
    if held_reason:
        found = found.filter(held_reason=held_reason)
    if since is not None:
        found = found.filter(queued_at__gte=datetime.combine(since, time(0), tzinfo=NEW_YORK))
    if until is not None:
        found = found.filter(
            queued_at__lt=datetime.combine(until + timedelta(days=1), time(0), tzinfo=NEW_YORK)
        )
    return found
