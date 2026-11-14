"""The Outbox's one list (docs/qa/15/PLAN-015k-reminder-email-outbox-activity.md §3): the
messaging engine's messages (:class:`~ledger.models.OutboundMessage`) and the reminder and
promise-to-pay e-mails sent to staff (:func:`ledger.services.calendar.send_morning_email`),
which are not messages: their only record is the audit row ``calendar.morning_email``.

Read only. A staff e-mail is listed when its row's outcome is sent, held or failed (a row with
no address is not a message). The filters read both sources: a status of sent, held or failed
and a held reason of ``not_set_up`` or ``sandbox`` select rows of both (an older held row,
stored without its reason, matches no reason); any other status or reason selects engine
messages only; the From and To days are New York days of the queue time or the audit time.

**Paging.** The page's keys come from one ordered query over both sources (a ``UNION ALL`` of
time, source and key; the count is that union's), so no request reads every row; then only the
page's rows are read, one query per source and one per kind of item a staff row names (the
calendar entries, the promises): a fixed number of queries whatever the number of rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.core.paginator import Page, Paginator
from django.db.models import CharField, F, QuerySet, Value
from django.db.models.functions import Cast, LPad

import mail
from finance.dates import NEW_YORK
from ledger.models import (
    AuditLog,
    CalendarEntry,
    ContactEntry,
    HeldReason,
    MessageStatus,
    Operator,
    OutboundMessage,
)
from ledger.services import calendar, messaging
from ledger.services.detail_cards import MORNING_EMAIL_ACTION, PROMISE_ENTITY
from mail.base import is_address
from payments.models import JobEntity

PAGE_SIZE = 50
#: The source column of the union: an engine message or a staff e-mail's audit row.
MESSAGE = "m"
STAFF = "s"
FAILED = MessageStatus.FAILED.value
#: The outcomes of a staff e-mail that make it a row (``no_address`` sent nothing).
STAFF_STATUSES: tuple[str, ...] = (mail.SENT, mail.HELD, FAILED)
#: The held reasons a staff e-mail can carry (the receipt's).
STAFF_REASONS: tuple[str, ...] = (mail.NOT_SET_UP, mail.SANDBOX)
#: Digits of an audit row's key in the union: wide enough for any ``bigint``, so the keys
#: of one time compare as numbers do.
_KEY_WIDTH = 20
#: The entity types a staff e-mail's audit row is written with (the ``calendar_email`` job's):
#: the audit table's ``(entity_type, entity_id, at)`` index narrows the scan by them.
STAFF_ENTITIES: tuple[str, ...] = (JobEntity.CALENDAR_ENTRY.value, JobEntity.CONTACT_ENTRY.value)
REMINDER_TEMPLATE = "Reminder e-mail"
PROMISE_TEMPLATE = "Promise to pay e-mail"


@dataclass(frozen=True, slots=True)
class StaffEmail:
    """One staff e-mail as the Outbox shows it."""

    pk: int
    at: datetime
    #: "Reminder e-mail" or "Promise to pay e-mail".
    template: str
    #: The stored subject, else the one rebuilt from the item while it exists, else "".
    subject: str
    #: A promise's operator; ``None`` for a calendar entry or a promise that is gone.
    operator: Operator | None
    #: ``sent``, ``held`` or ``failed`` (the engine's status values).
    status: str
    #: The line under the status chip: the held reason as the engine words it, or the
    #: failure's class name; "" when the row stores neither.
    rest: str
    #: The stored address, else the author's current one (``current_address``), else "".
    to: str
    current_address: bool


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=NEW_YORK)


def staff_emails(
    *, status: str = "", since: date | None = None, until: date | None = None, held_reason: str = ""
) -> QuerySet[AuditLog]:
    """The audit rows of the staff e-mails the filters select (none when a filter names a
    status or reason no staff e-mail can have)."""
    found = AuditLog.objects.filter(
        entity_type__in=list(STAFF_ENTITIES),
        action=MORNING_EMAIL_ACTION,
        after__outcome__in=list(STAFF_STATUSES),
    )
    if status and status not in STAFF_STATUSES:
        return found.none()
    if held_reason and held_reason not in STAFF_REASONS:
        return found.none()
    if status:
        found = found.filter(after__outcome=status)
    if held_reason:
        found = found.filter(after__reason=held_reason)
    if since is not None:
        found = found.filter(at__gte=_day_start(since))
    if until is not None:
        found = found.filter(at__lt=_day_start(until + timedelta(days=1)))
    return found


def keys(
    *, status: str = "", since: date | None = None, until: date | None = None, held_reason: str = ""
) -> QuerySet[Any]:
    """Every row of the list as ``{"moment", "source", "key"}``, newest first: the one
    ordered union the paginator counts and slices."""
    filters: dict[str, Any] = {
        "status": status,
        "since": since,
        "until": until,
        "held_reason": held_reason,
    }
    messages = (
        messaging.outbox(**filters)
        .select_related(None)
        .order_by()
        .annotate(
            moment=F("queued_at"),
            source=Value(MESSAGE, output_field=CharField()),
            key=Cast("id", output_field=CharField()),
        )
        .values("moment", "source", "key")
    )
    staff = (
        staff_emails(**filters)
        .order_by()
        .annotate(
            moment=F("at"),
            source=Value(STAFF, output_field=CharField()),
            key=LPad(Cast("id", output_field=CharField()), _KEY_WIDTH, Value("0")),
        )
        .values("moment", "source", "key")
    )
    return messages.union(staff, all=True).order_by("-moment", "-source", "-key")


def page(
    number: str | None,
    *,
    status: str = "",
    since: date | None = None,
    until: date | None = None,
    held_reason: str = "",
) -> tuple[Page[Any], list[OutboundMessage | StaffEmail]]:
    """Page ``number`` of the list (the paginator's reading: anything unreadable is page 1,
    past the end the last page) and its rows in order."""
    found = keys(status=status, since=since, until=until, held_reason=held_reason)
    shown = Paginator(found, PAGE_SIZE).get_page(number)
    return shown, rows(list(shown.object_list))


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def rows(found: Sequence[dict[str, Any]]) -> list[OutboundMessage | StaffEmail]:
    """The rows of ``found`` (keys of :func:`keys`), in its order."""
    message_ids = [_uuid(item["key"]) for item in found if item["source"] == MESSAGE]
    audit_ids = [int(item["key"]) for item in found if item["source"] == STAFF]
    messages: dict[uuid.UUID, OutboundMessage] = {}
    if message_ids:
        messages = {
            message.pk: message
            for message in messaging.outbox().filter(pk__in=[m for m in message_ids if m])
        }
    staff: dict[int, StaffEmail] = staff_rows(audit_ids) if audit_ids else {}
    result: list[OutboundMessage | StaffEmail] = []
    for item in found:
        if item["source"] == MESSAGE:
            pk = _uuid(item["key"])
            if pk is not None and pk in messages:
                result.append(messages[pk])
        elif int(item["key"]) in staff:
            result.append(staff[int(item["key"])])
    return result


def staff_rows(ids: Sequence[int]) -> dict[int, StaffEmail]:
    """The staff e-mails of the audit rows ``ids``, by row id (three queries)."""
    audits = list(AuditLog.objects.filter(pk__in=ids))
    entry_ids = {_uuid(row.entity_id) for row in audits if row.entity_type != PROMISE_ENTITY}
    promise_ids = {_uuid(row.entity_id) for row in audits if row.entity_type == PROMISE_ENTITY}
    entries = {
        entry.pk: entry
        for entry in CalendarEntry.objects.select_related("author").filter(
            pk__in=[pk for pk in entry_ids if pk is not None]
        )
    }
    promises = {
        promise.pk: promise
        for promise in ContactEntry.objects.select_related("author", "operator").filter(
            pk__in=[pk for pk in promise_ids if pk is not None]
        )
    }
    found: dict[int, StaffEmail] = {}
    for row in audits:
        after = row.after if isinstance(row.after, dict) else {}
        pk = _uuid(row.entity_id)
        item: CalendarEntry | ContactEntry | None
        if row.entity_type == PROMISE_ENTITY:
            item = promises.get(pk) if pk is not None else None
        else:
            item = entries.get(pk) if pk is not None else None
        found[row.pk] = _staff_email(row, after, item)
    return found


def _text(after: dict[str, Any], name: str) -> str:
    value = after.get(name)
    return value if isinstance(value, str) else ""


def _staff_email(
    row: AuditLog, after: dict[str, Any], item: CalendarEntry | ContactEntry | None
) -> StaffEmail:
    promise = item if isinstance(item, ContactEntry) else None
    status = _text(after, "outcome")
    subject = _text(after, "subject")
    if not subject and isinstance(item, CalendarEntry):
        subject = calendar.entry_subject(item)
    elif not subject and promise is not None:
        subject = calendar.promise_subject(promise)
    rest = ""
    reason = _text(after, "reason")
    if status == mail.HELD and reason in STAFF_REASONS:
        rest = str(HeldReason(reason).label)
    elif status == FAILED:
        rest = _text(after, "error")
    to = _text(after, "to")
    current = False
    if not to and item is not None and item.author is not None:
        address = str(item.author.email or "")
        if is_address(address):
            to, current = address, True
    return StaffEmail(
        pk=row.pk,
        at=row.at,
        template=PROMISE_TEMPLATE if row.entity_type == PROMISE_ENTITY else REMINDER_TEMPLATE,
        subject=subject,
        operator=promise.operator if promise is not None else None,
        status=status,
        rest=rest,
        to=to,
        current_address=current,
    )
