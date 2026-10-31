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
NO_LEASE = "A message belongs to a lease."
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
    """The 15 placeholder values of a message about ``charge`` billed through ``lease``
    (D4). ``amount`` is the charge's balance unless given; ``debit_date`` its due date
    unless given; ``days_late`` counted from the due date (never below zero);
    ``retry_date`` and ``return_reason`` empty unless given (rule F). ``{pay_link}`` is the
    portal's Pay page of the store when the operator has a portal account that can sign in,
    else a new signed statement link (:func:`pay_link`, PLAN-014 H4). ``rule`` is accepted for
    the rules that will shape a value of their own. For a :class:`RentMonth` the amount is
    the scheduled one and both dates the scheduled debit (E4)."""
    row = company or CompanySettings.read()
    operator = operator_of(lease, charge)
    store = lease.store
    due = due_of(charge)
    late = days_late if days_late is not None else max((today - due).days, 0)
    last4 = lease_sync.billing_of(lease).bank_last4
    return {
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
    }


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
) -> OutboundMessage | None:
    """Render ``template`` with ``context`` and queue one message and its job (see the
    module docstring); ``None`` when the row exists already (the unique constraint).
    :class:`ChannelUnavailable` for an SMS template; :class:`~ledger.merge.TemplateRefused`
    when the values are not one code's placeholders. A rendered text holding payment data is
    stored as ``failed`` (``PaymentDataRefused``) without its text, and gets no job.
    ``ValueError`` and nothing written when ``lease`` is ``None``. A :class:`RentMonth` is
    stored by its lease and month (``lease_month``, ``period_month``; E4)."""
    if lease is None:
        raise ValueError(NO_LEASE)
    channel_for(template.channel)
    operator = operator_of(lease, charge)
    if operator is None:
        raise ValueError("A message needs the operator billed.")
    moment = now or timezone.now()
    subject = one_line(merge.render(template.subject, context))[:SUBJECT_MAX_LENGTH]
    body = merge.render(template.body, context)
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
    return _store(message, actor)


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
    placeholders."""
    channel_for(template.channel)
    subject = one_line(merge.render(template.subject, context))[:SUBJECT_MAX_LENGTH]
    body = merge.render(template.body, context)
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


def _store(message: OutboundMessage, actor: User | None) -> OutboundMessage | None:
    """Save a rendered message and its ``message_send`` job, audited; ``None`` when the
    unique constraint finds the row already there. A text holding payment data is stored as
    ``failed`` (``PaymentDataRefused``) without its text, and gets no job."""
    try:
        message.check_payment_data()
    except ValidationError:
        message.rendered_subject = message.rendered_body = message.body_sha256 = ""
        message.status = MessageStatus.FAILED
        message.error = PAYMENT_DATA_REFUSED
    audited: dict[str, object] = {"status": message.status, "error": message.error}
    if message.status == MessageStatus.FAILED:  # as a send's failure reads
        audited["recipients"] = 0
    try:
        with transaction.atomic():
            message.save()
            if message.status == MessageStatus.QUEUED:
                jobs.enqueue(
                    JobKind.MESSAGE_SEND, message, None, actor, run_after=message.scheduled_for
                )
            _audit(
                "message.queued" if message.status == MessageStatus.QUEUED else "message.failed",
                message,
                actor,
                audited,
            )
    except IntegrityError:
        return None
    return message


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


def _suppress_reason(message: OutboundMessage) -> str:
    from ledger.services import lease_end

    if message.kind == MessageKind.PORTAL:
        issuer = ONE_TIME_LINKS.get(message.template.code)
        if issuer is None:
            return LINK_UNAVAILABLE
        reason = issuer.refusal(message)
        if reason:
            return reason
    elif message.charge_entity_type == ChargeEntityType.LEASE_MONTH:
        reason = month_closed_reason(message)
        if reason:
            return reason
    elif message.rule == MessageRule.FINAL_STATEMENT:
        if not lease_end.statement_ready(message):
            return STATEMENT_MISSING
    elif message.rule in OPERATOR_FACING_RULES and not _charge_open(
        message.charge_entity_type, message.charge_entity_id
    ):
        return CHARGE_CLOSED
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
    set up, or, on the sandbox, while its address is not on the allowlist; otherwise the row
    is queued again and its job runs at once. Audited ``message.send_now``."""
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
        recipients = _recipients(message)
        if not recipients:
            return Outcome(False, SEND_NOW_NO_ADDRESS)
        if not all(allowlisted(address, found) for address in recipients):
            return Outcome(False, SEND_NOW_NOT_ALLOWED)
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


def status_text(message: OutboundMessage) -> str:
    """ "Held (held on sandbox)", "Failed (ConnectTimeout)", "Sent"…"""
    label = message.get_status_display()
    if message.status == MessageStatus.HELD and message.held_reason:
        return f"{label} ({HeldReason(message.held_reason).label})"
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
        .select_related("template", "sent_by")
        .order_by("-queued_at", "-id")
    )


def outbox(
    *, status: str = "", since: date | None = None, until: date | None = None
) -> QuerySet[OutboundMessage]:
    """Every message, newest first, for the Outbox (D5): of ``status`` when given, queued
    from ``since`` to ``until`` (New York days, both included) when given."""
    found = OutboundMessage.objects.select_related(
        "template", "operator", "lease__store", "sent_by"
    ).order_by("-queued_at", "-id")
    if status:
        found = found.filter(status=status)
    if since is not None:
        found = found.filter(queued_at__gte=datetime.combine(since, time(0), tzinfo=NEW_YORK))
    if until is not None:
        found = found.filter(
            queued_at__lt=datetime.combine(until + timedelta(days=1), time(0), tzinfo=NEW_YORK)
        )
    return found
