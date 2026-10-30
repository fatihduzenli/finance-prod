"""The collections ladder (docs/qa/PLAN-011b-collections-nsf-attention-calendar.md D4).

**Which charges.** :func:`due_steps` reads the open balances
(:func:`ledger.services.receivables.open_balances`) and keeps the overdue **rent** and
**one-off** charges (insurance is not on the ladder; deposits are never open balances):
a balance above zero, a status that is not waived, voided, paid or (for rent) pending (a
payment on its way), billed to an operator that is not archived and is not marked
*handled personally*, through a lease whose billing details hold an e-mail address (PLAN-014
C5: the charge's lease, else, for a one-off charge without one, its operator's first active
lease; the message goes to that address). Days late are New York days: ``today`` minus the
due date, ``today`` being the project's local date.

**Which step.** For each such charge, the step sent is the active step with the largest
``day_offset`` not above the days late, and only when no step of that offset or later
was already logged for the charge (a ladder row, whatever its status). A charge first
seen 25 days late therefore gets the day-20 step only, never three e-mails at once, and
the ladder never goes back down. One ladder row per (charge, step) is also a unique
constraint, so two ticks at once still queue one.

**Queue and send.** :func:`queue_due` writes one :class:`OutboundMessage` (``queued``,
kind ``ladder``) per due step and a ``message_send`` job for the worker, through
:func:`ledger.services.messages.queue` (PLAN-012e D8). :func:`send` (the job) renders
the template with the charge's figures at that moment (:mod:`ledger.merge`), delivers it
through :func:`mail.get_sender` and records ``sent``, ``catch_all``, ``suppressed`` (mail
off, or the charge or operator no longer qualifies) or ``failed`` with the error's class
name, plus an audit row ``collections.sent``, ``collections.failed`` or
``collections.suppressed``. The body is never stored: only its SHA-256.

**By hand.** :func:`send_step_by_hand` (billing) queues one step for one overdue charge.
When the step is not yet logged for the charge the row is the ladder's own (so the
ladder will not send it again); when it is, a second send needs an explicit
confirmation and is recorded as ``manual``.
"""

from __future__ import annotations

import hashlib
import html
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

import mail
from ledger import merge
from ledger.models import (
    AuditLog,
    ChargeEntityType,
    CollectionStep,
    Lease,
    MessageKind,
    MessageStatus,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    OutboundMessage,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import leases, messages, receivables, statement_links
from ledger.validators import contains_bank_routing_pattern, contains_card_number, visible_text
from ledger.wording import format_amount
from mail.base import SUBJECT_MAX_LENGTH
from payments import lease_sync

logger = logging.getLogger(__name__)

ENTITY = "outbound_message"
#: Open-balance kind -> the outbound message's charge type.
CHARGE_TYPES: dict[str, str] = {
    receivables.KIND_RENT: ChargeEntityType.RENT_CHARGE.value,
    receivables.KIND_ONE_OFF: ChargeEntityType.ONE_OFF_CHARGE.value,
}
#: Statuses that keep a charge off the ladder, whatever its balance.
CLOSED_RENT: frozenset[str] = frozenset(
    {RentChargeStatus.WAIVED, RentChargeStatus.PAID, RentChargeStatus.PENDING}
)
CLOSED_ONE_OFF: frozenset[str] = frozenset(
    {OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED, OneOffChargeStatus.PAID}
)
#: ``{store}`` of a one-off charge without a lease.
NO_STORE = "your account"

#: Why a queued message was not sent although mail is on: class names, as ``error``.
CHARGE_CLOSED = "ChargeClosed"
OPERATOR_OPTED_OUT = "OperatorHandledPersonally"
NO_ADDRESS = "NoAddress"
OPERATOR_ARCHIVED = "OperatorArchived"
TEMPLATE_REFUSED = "TemplateRefused"

GONE = "The message no longer exists."
NOT_QUEUED = "Nothing sent: the message is already {status}."
SENT_TEXT = {
    MessageStatus.SENT: "Sent.",
    MessageStatus.CATCH_ALL: "Sent to the test inbox.",
    MessageStatus.SUPPRESSED: "Not sent: mail is off.",
}
SUPPRESSED_TEXT = "Not sent ({cause})."
FAILED_TEXT = "Failed: {cause}."

HAND_QUEUED = "The e-mail will be sent; the log on the operator's page shows when it is sent."
HAND_NEEDS_CONFIRM = (
    "This step was already sent for this charge. Check the box to send it again; "
    "it is recorded as a manual send."
)
HAND_NOT_OVERDUE = "Only an overdue charge with a balance can be chased."
HAND_NO_EMAIL = "The lease billed has no billing e-mail address."
HAND_OPTED_OUT = (
    "This operator is handled personally: no collection e-mail is sent. Change that on the "
    "operator's page first."
)
HAND_ARCHIVED = "The operator is archived."
HAND_UNKNOWN_STEP = "Choose a step of the ladder."


@dataclass(frozen=True, slots=True)
class DueStep:
    charge: receivables.OpenBalance
    charge_type: str
    step: CollectionStep
    days_late: int
    operator: Operator
    #: The lease whose billing e-mail the message goes to (PLAN-014 C5).
    lease: Lease


@dataclass(frozen=True, slots=True)
class QueueReport:
    queued: int
    already: int


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    needs_confirm: bool = False


def days_late(due_on: date, today: date) -> int:
    """New York days between the due date and ``today`` (a project-zone date)."""
    return (today - due_on).days


def _qualifies(row: receivables.OpenBalance) -> bool:
    if row.balance <= 0 or row.operator_id is None:
        return False
    if row.kind == receivables.KIND_RENT:
        return row.status not in CLOSED_RENT
    return row.kind == receivables.KIND_ONE_OFF and row.status not in CLOSED_ONE_OFF


def _reachable_operators(ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Operator]:
    return {
        operator.pk: operator
        for operator in Operator.objects.filter(
            pk__in=list(ids), archived_at__isnull=True, handled_personally=False
        )
    }


def _billing_leases(rows: list[receivables.OpenBalance]) -> dict[uuid.UUID, Lease]:
    """Charge id -> the lease whose billing details bill it (PLAN-014 C5): its lease, else
    its operator's first active lease. Two queries."""
    by_id = {
        lease.pk: lease
        for lease in Lease.objects.filter(
            pk__in={row.lease_id for row in rows if row.lease_id is not None}
        ).select_related("billing")
    }
    first = leases.first_active_leases(
        {row.operator_id for row in rows if row.lease_id is None and row.operator_id is not None}
    )
    found: dict[uuid.UUID, Lease] = {}
    for row in rows:
        lease = (
            by_id.get(row.lease_id)
            if row.lease_id is not None
            else first.get(row.operator_id)
            if row.operator_id is not None
            else None
        )
        if lease is not None:
            found[row.charge_id] = lease
    return found


def billing_email(lease: Lease | None) -> str:
    """The billing e-mail of ``lease``, or ``""``."""
    return lease_sync.billing_of(lease).email if lease is not None else ""


def _logged_offsets(charge_ids: Iterable[uuid.UUID]) -> dict[tuple[str, uuid.UUID], int]:
    """(charge type, charge id) -> the largest step offset already logged by the ladder."""
    rows = (
        OutboundMessage.objects.filter(
            kind=MessageKind.LADDER, manual=False, charge_entity_id__in=list(charge_ids)
        )
        .values("charge_entity_type", "charge_entity_id")
        .annotate(latest=Max("step__day_offset"))
        .order_by()
    )
    return {(row["charge_entity_type"], row["charge_entity_id"]): row["latest"] for row in rows}


def due_steps(today: date) -> list[DueStep]:
    """The step each charge is due for on ``today`` (see the module docstring). A fixed
    number of queries."""
    steps = list(
        CollectionStep.objects.filter(active=True)
        .select_related("template")
        .order_by("day_offset", "position")
    )
    if not steps:
        return []
    first = steps[0].day_offset
    rows = [
        row
        for row in receivables.open_balances(today)
        if _qualifies(row) and days_late(row.due_on, today) >= first
    ]
    if not rows:
        return []
    operators = _reachable_operators({row.operator_id for row in rows if row.operator_id})
    billed = _billing_leases(rows)
    logged = _logged_offsets(row.charge_id for row in rows)
    found = []
    for row in rows:
        operator = operators.get(row.operator_id) if row.operator_id else None
        lease = billed.get(row.charge_id)
        if operator is None or lease is None or not billing_email(lease):
            continue
        late = days_late(row.due_on, today)
        step = [candidate for candidate in steps if candidate.day_offset <= late][-1]
        charge_type = CHARGE_TYPES[row.kind]
        latest = logged.get((charge_type, row.charge_id))
        if latest is not None and latest >= step.day_offset:
            continue
        found.append(DueStep(row, charge_type, step, late, operator, lease))
    return found


def _audit(
    action: str, message: OutboundMessage, actor: User | None, after: dict[str, object]
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
            "step": message.step.day_offset if message.step is not None else None,
            "manual": message.manual,
            **after,
        },
    )


def _queue(
    operator: Operator,
    lease: Lease,
    charge_type: str,
    charge_id: uuid.UUID,
    step: CollectionStep,
    *,
    actor: User | None = None,
    manual: bool = False,
) -> OutboundMessage | None:
    """One queued row and its job; ``None`` when the ladder row already exists."""
    try:
        with transaction.atomic():
            message = messages.queue(
                MessageKind.LADDER,
                operator,
                step.template,
                messages.MessageContext(charge_type, charge_id),
                lease=lease,
                actor=actor,
                step=step,
                manual=manual,
            )
            _audit("collections.queued", message, actor, {})
    except IntegrityError:
        return None
    return message


def queue_due(today: date) -> QueueReport:
    """Queue every due step of ``today`` (one row and one job each)."""
    queued = already = 0
    for due in due_steps(today):
        message = _queue(due.operator, due.lease, due.charge_type, due.charge.charge_id, due.step)
        if message is None:
            already += 1
        else:
            queued += 1
    return QueueReport(queued=queued, already=already)


# --- Rendering and sending ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChargeFigures:
    """A charge as the template sees it, read at send time."""

    open: bool
    balance: Decimal
    due_on: date
    store: str


def charge_figures(charge_type: str, charge_id: uuid.UUID) -> ChargeFigures | None:
    if charge_type not in CHARGE_TYPES.values():
        return None
    if charge_type == ChargeEntityType.RENT_CHARGE:
        rent = RentCharge.objects.select_related("lease__store").filter(pk=charge_id).first()
        if rent is None:
            return None
        balance = Decimal(str(rent.balance))
        return ChargeFigures(
            open=balance > 0 and rent.status not in CLOSED_RENT,
            balance=balance,
            due_on=rent.due_on,
            store=rent.lease.store.external_label,
        )
    one_off = (
        OneOffCharge.objects.select_related("lease__store", "fee_type").filter(pk=charge_id).first()
    )
    if one_off is None:
        return None
    balance = Decimal(str(one_off.balance))
    refundable = one_off.fee_type is not None and one_off.fee_type.refundable
    return ChargeFigures(
        open=balance > 0 and one_off.status not in CLOSED_ONE_OFF and not refundable,
        balance=balance,
        due_on=one_off.due_date,
        store=one_off.lease.store.external_label if one_off.lease is not None else NO_STORE,
    )


def statement_link(operator: Operator) -> str:
    """A new signed link to the operator's own statement page, which opens without an
    account (:mod:`ledger.services.statement_links`)."""
    return statement_links.link(operator)


def _long_date(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def merge_values(operator: Operator, figures: ChargeFigures, today: date) -> dict[str, str]:
    return {
        "operator": operator.display_name,
        "store": figures.store,
        "amount": format_amount(figures.balance),
        "due_date": _long_date(figures.due_on),
        "days_late": str(days_late(figures.due_on, today)),
        "statement_link": statement_link(operator),
        "company": str(settings.COMPANY_NAME),
    }


def html_part(text: str) -> str:
    """The HTML alternative: the text, escaped, with its line breaks kept."""
    escaped = html.escape(text, quote=True)
    return "<html><body><p>" + escaped.replace("\n", "<br>\n") + "</p></body></html>"


def one_line(text: str) -> str:
    return " ".join(text.split())


def _skip_reason(message: OutboundMessage, figures: ChargeFigures | None) -> str:
    operator = message.operator
    if figures is None or not figures.open:
        return CHARGE_CLOSED
    if operator.archived_at is not None:
        return OPERATOR_ARCHIVED
    if operator.handled_personally:
        return OPERATOR_OPTED_OUT
    if not billing_email(message.lease):
        return NO_ADDRESS
    return ""


def _finish(
    message: OutboundMessage,
    status: str,
    *,
    error: str = "",
    receipt: mail.SentReceipt | None = None,
) -> None:
    message.status = status
    message.error = error
    if receipt is not None:
        message.mail_message_id = receipt.provider_message_id
        if status != MessageStatus.SUPPRESSED:
            message.sent_at = receipt.sent_at
    message.save()
    actions: dict[str, str] = {
        MessageStatus.SENT: "collections.sent",
        MessageStatus.CATCH_ALL: "collections.sent",
        MessageStatus.SUPPRESSED: "collections.suppressed",
    }
    action = actions.get(status, "collections.failed")
    _audit(action, message, None, {"status": status, "error": error})


def send(message_id: uuid.UUID, today: date | None = None) -> Outcome:
    """The ``message_send`` job: render, deliver and record one queued message."""
    day = today or timezone.localdate()
    with transaction.atomic():
        message = (
            OutboundMessage.objects.select_for_update(of=("self",))
            .select_related("operator", "lease", "template", "step")
            .filter(pk=message_id)
            .first()
        )
        if message is None:
            return Outcome(False, GONE)
        if message.status != MessageStatus.QUEUED:
            return Outcome(True, NOT_QUEUED.format(status=message.get_status_display().lower()))
        if message.kind != MessageKind.LADDER:
            return Outcome(False, GONE)
        figures = charge_figures(message.charge_entity_type, message.charge_entity_id)
        reason = _skip_reason(message, figures)
        if reason or figures is None:
            _finish(message, MessageStatus.SUPPRESSED, error=reason or CHARGE_CLOSED)
            return Outcome(True, SUPPRESSED_TEXT.format(cause=reason or CHARGE_CLOSED))
        operator = message.operator
        address = billing_email(message.lease)
        values = merge_values(operator, figures, day)
        try:
            subject = one_line(merge.render(message.template.subject, values))
            body = merge.render(message.template.body, values)
        except merge.TemplateRefused:
            _finish(message, MessageStatus.FAILED, error=TEMPLATE_REFUSED)
            return Outcome(False, FAILED_TEXT.format(cause=TEMPLATE_REFUSED))
        message.to_address = address
        message.subject = subject[:SUBJECT_MAX_LENGTH]
        message.body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        try:
            message.check_payment_data()
        except ValidationError:
            message.subject = ""
            _finish(message, MessageStatus.FAILED, error="PaymentDataRefused")
            return Outcome(False, FAILED_TEXT.format(cause="PaymentDataRefused"))
        try:
            outgoing = mail.Message(
                to=address, subject=message.subject, text=body, html=html_part(body)
            )
            receipt = mail.get_sender().send(outgoing)
        except mail.MailUnavailable as error:
            _finish(message, MessageStatus.FAILED, error=error.cause)
            return Outcome(False, FAILED_TEXT.format(cause=error.cause))
        except ValueError:
            _finish(message, MessageStatus.FAILED, error="MessageRefused")
            return Outcome(False, FAILED_TEXT.format(cause="MessageRefused"))
        status = MessageStatus(receipt.disposition)
        _finish(message, status, receipt=receipt)
        return Outcome(True, SENT_TEXT[status])


# --- By hand ------------------------------------------------------------------------------


def send_step_by_hand(
    charge_type: str,
    charge_id: uuid.UUID,
    step_id: uuid.UUID | None,
    actor: User,
    *,
    confirm: bool = False,
    today: date | None = None,
) -> Outcome:
    """Queue ``step`` for one charge, by hand (see the module docstring)."""
    day = today or timezone.localdate()
    step = (
        CollectionStep.objects.select_related("template").filter(pk=step_id).first()
        if step_id is not None
        else None
    )
    if step is None:
        return Outcome(False, HAND_UNKNOWN_STEP)
    figures = charge_figures(charge_type, charge_id)
    if figures is None or not figures.open or figures.due_on >= day:
        return Outcome(False, HAND_NOT_OVERDUE)
    operator, lease = _billed(charge_type, charge_id)
    if operator is None:
        return Outcome(False, HAND_NOT_OVERDUE)
    if operator.archived_at is not None:
        return Outcome(False, HAND_ARCHIVED)
    if operator.handled_personally:
        return Outcome(False, HAND_OPTED_OUT)
    if lease is None or not billing_email(lease):
        return Outcome(False, HAND_NO_EMAIL)
    logged = OutboundMessage.objects.filter(
        manual=False, charge_entity_type=charge_type, charge_entity_id=charge_id, step=step
    ).exists()
    if (
        not logged
        and _queue(operator, lease, charge_type, charge_id, step, actor=actor) is not None
    ):
        return Outcome(True, HAND_QUEUED)
    if not confirm:
        return Outcome(False, HAND_NEEDS_CONFIRM, needs_confirm=True)
    _queue(operator, lease, charge_type, charge_id, step, actor=actor, manual=True)
    return Outcome(True, HAND_QUEUED)


def _billed(charge_type: str, charge_id: uuid.UUID) -> tuple[Operator | None, Lease | None]:
    """The operator a charge is billed to and the lease whose billing details bill it."""
    if charge_type == ChargeEntityType.RENT_CHARGE:
        rent = RentCharge.objects.select_related("lease__operator").filter(pk=charge_id).first()
        return (rent.lease.operator, rent.lease) if rent is not None else (None, None)
    one_off = (
        OneOffCharge.objects.select_related("operator", "lease__operator")
        .filter(pk=charge_id)
        .first()
    )
    if one_off is None:
        return None, None
    operator = one_off.operator or (one_off.lease.operator if one_off.lease is not None else None)
    return operator, leases.billing_lease(one_off)


# --- Pages --------------------------------------------------------------------------------


def mask_address(address: str) -> str:
    """``a***@domain``: the first character of the local part and the domain."""
    local, _, domain = address.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


@dataclass(frozen=True, slots=True)
class LogRow:
    """One message as the pages list it: the address masked, never the body."""

    when: object
    #: The ladder step's day; ``None`` for a final statement (PLAN-012e D8).
    step: int | None
    template: str
    status: str
    status_label: str
    to: str
    manual: bool
    error: str
    charge_type: str
    charge_id: uuid.UUID
    #: Who sent it by hand, or ``None`` for the ladder; shown through ``|staff``.
    sent_by: User | None


def _rows(found: Iterable[OutboundMessage]) -> list[LogRow]:
    return [
        LogRow(
            when=message.sent_at or message.queued_at,
            step=message.step.day_offset if message.step is not None else None,
            template=message.template.code,
            status=message.status,
            status_label=message.get_status_display(),
            to=mask_address(message.to_address),
            manual=message.manual,
            error=message.error,
            charge_type=message.charge_entity_type,
            charge_id=message.charge_entity_id,
            sent_by=message.sent_by,
        )
        for message in found
    ]


def operator_log(operator: Operator, limit: int) -> list[LogRow]:
    """The operator's messages, newest first."""
    return _rows(
        OutboundMessage.objects.filter(operator=operator)
        .select_related("step", "template", "sent_by")
        .order_by("-queued_at", "-id")[:limit]
    )


def charge_log(charge_type: str, charge_id: uuid.UUID) -> list[LogRow]:
    """The charge's messages, newest first."""
    return _rows(
        OutboundMessage.objects.filter(charge_entity_type=charge_type, charge_entity_id=charge_id)
        .select_related("step", "template", "sent_by")
        .order_by("-queued_at", "-id")
    )


def charge_context(
    charge_type: str, charge_id: uuid.UUID, *, billing: bool, url: str
) -> dict[str, object]:
    """What a charge page shows of the ladder: its log, and the hand-send form when the
    user is billing and the charge is overdue and open."""
    figures = charge_figures(charge_type, charge_id)
    steps = active_steps() if billing else []
    chaseable = figures is not None and figures.open and figures.due_on < timezone.localdate()
    return {
        "collection_log": charge_log(charge_type, charge_id),
        "collection_steps": steps,
        "can_send_step": billing and chaseable and bool(steps),
        "send_step_url": url,
    }


def active_steps() -> list[CollectionStep]:
    return list(
        CollectionStep.objects.filter(active=True)
        .select_related("template")
        .order_by("day_offset", "position")
    )


# --- Handled personally -------------------------------------------------------------------

#: Most messages the operator page lists.
LOG_LIMIT = 50
NOTE_MAX_LENGTH = 500
HANDLED_NOTE_REQUIRED = "Say who handles this operator in person, and why."
HANDLED_NOTE_TOO_LONG = f"The note is at most {NOTE_MAX_LENGTH} characters."
HANDLED_PAYMENT_DATA = "This text may not contain card or account numbers."
HANDLED_ON = "Collection e-mails are off for this operator: it is handled personally."
HANDLED_OFF = "Collection e-mails are on again for this operator."


def set_handled_personally(
    operator_id: uuid.UUID, handled: bool, note: str, actor: User
) -> Outcome:
    """Mark the operator as handled personally (the ladder skips it) or not. A note is
    required to turn it on. Audited as ``operator.handled_personally_changed``."""
    text = visible_text(note or "")
    if handled and not text:
        return Outcome(False, HANDLED_NOTE_REQUIRED)
    if len(text) > NOTE_MAX_LENGTH:
        return Outcome(False, HANDLED_NOTE_TOO_LONG)
    if contains_card_number(text) or contains_bank_routing_pattern(text):
        return Outcome(False, HANDLED_PAYMENT_DATA)
    with transaction.atomic():
        operator = Operator.objects.select_for_update().get(pk=operator_id)
        if operator.archived_at is not None:
            return Outcome(False, HAND_ARCHIVED)
        before = {
            "handled_personally": operator.handled_personally,
            "note": operator.handled_personally_note,
        }
        operator.handled_personally = handled
        if handled or text:
            operator.handled_personally_note = text
        operator.save(update_fields=["handled_personally", "handled_personally_note", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action="operator.handled_personally_changed",
            entity_type="operator",
            entity_id=str(operator.pk),
            before=before,
            after={
                "handled_personally": operator.handled_personally,
                "note": operator.handled_personally_note,
            },
        )
    return Outcome(True, HANDLED_ON if handled else HANDLED_OFF)
