"""The staff calendar and its per-user ICS feed (docs/qa/PLAN-011b D8).

**Items.** A calendar day shows stored entries (:class:`ledger.models.CalendarEntry`,
notes and reminders staff add) and generated items, which are never stored:

- **debit days**: each debit day of an active lease (collection ``active``, started on or
  before the day, not ended before it), on the day it falls in each month (29 to 31 move
  to the month's last day, as the rent run moves them);
- **insurance anniversaries**: the store's open-date anniversary for the lease that
  holds the store that day (:mod:`ledger.services.insurance`), prospective leases and
  closed stores left out;
- **lease ends**: a lease's ``ended_on``.

- **re-debit dates** (PLAN-011b D5): the due date of each re-debit charge a Reschedule
  made, unless the charge is voided.

Generated titles name the store and the operator
and never an amount, a bank detail or a card number; stored titles and bodies pass the
card-number guard when saved. Entries are **shared staff notes**: every user sees every
entry and there is no private entry. :func:`items` reads a fixed number of queries (three).

**Entries.** Billing adds them; the author edits or deletes their own, an admin any
(:func:`can_change`). Each change writes an audit row (``calendar_entry.*``); a delete
records the entry as it was.

**The ICS feed.** A user creates a feed on their settings page; the 32-byte URL-safe
token is shown once and only its SHA-256 is stored (:class:`CalendarFeed`). A new feed
revokes the user's previous one. ``/calendar/feed/<token>.ics`` needs no session: the
token is the credential. :func:`feed_for_token` hashes the token, reads the live row with
that hash and compares the hashes with :func:`hmac.compare_digest`; an unknown or revoked
token is "no feed". The feed holds every item from 30 days before today to 180 days
after, as ``VEVENT`` all-day events with stable UIDs. The feed is per user only so that
each user can revoke their own; it carries the same shared items for everyone. Every
read is audited (``calendar_feed.read``) with the user and the feed's row id, never the
token.

**Morning e-mails** (D8). The daily tick calls :func:`queue_morning_emails`: one
``calendar_email`` job per stored entry of the day with ``email_morning`` (to its author)
and per open promise to pay due that day (to the promise's author), at most one per item
and day (a fixed replay key). The worker runs :func:`send_morning_email`, which sends
through :func:`mail.get_sender`, so the catch-all applies as for every message.
"""

from __future__ import annotations

import calendar as month_calendar
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

import mail
from ledger.models import (
    CALENDAR_TITLE_MAX_LENGTH,
    AuditLog,
    CalendarEntry,
    CalendarEntryKind,
    CalendarFeed,
    CollectionStatus,
    ContactEntry,
    ContactKind,
    ContactStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    StoreStatus,
)
from ledger.services import insurance
from ledger.services.rent_run import falls_on
from ledger.validators import contains_bank_routing_pattern, contains_card_number, visible_text
from ledger.wording import format_amount
from mail.base import SUBJECT_MAX_LENGTH, is_address
from payments import jobs
from payments.models import Job, JobEntity, JobKind

KIND_NOTE = CalendarEntryKind.NOTE.value
KIND_REMINDER = CalendarEntryKind.REMINDER.value
KIND_DEBIT = "debit"
KIND_INSURANCE = "insurance"
KIND_LEASE_END = "lease_end"
KIND_REDEBIT = "redebit"
#: Every item kind, in the order the filter lists them.
KINDS: tuple[str, ...] = (
    KIND_NOTE,
    KIND_REMINDER,
    KIND_DEBIT,
    KIND_REDEBIT,
    KIND_INSURANCE,
    KIND_LEASE_END,
)
KIND_LABELS: dict[str, str] = {
    KIND_NOTE: "Note",
    KIND_REMINDER: "Reminder",
    KIND_DEBIT: "Debit day",
    KIND_REDEBIT: "Re-debit",
    KIND_INSURANCE: "Insurance anniversary",
    KIND_LEASE_END: "Lease end",
}
LEASE_KINDS: frozenset[str] = frozenset({KIND_DEBIT, KIND_INSURANCE, KIND_LEASE_END})
GENERATED_KINDS: frozenset[str] = LEASE_KINDS | {KIND_REDEBIT}

#: The feed's window around today (D8).
FEED_DAYS_BEFORE = 30
FEED_DAYS_AFTER = 180
#: Bytes of randomness in a feed token (256 bits).
TOKEN_BYTES = 32
#: A token as :func:`secrets.token_urlsafe` writes 32 bytes: 43 URL-safe characters.
TOKEN_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")
FEED_SUFFIX = ".ics"
ENTITY_ENTRY = "calendar_entry"
ENTITY_FEED = "calendar_feed"
#: The longest body accepted.
BODY_MAX_LENGTH = 2000

TITLE_REQUIRED = "A title is required."
TITLE_TOO_LONG = f"A title is at most {CALENDAR_TITLE_MAX_LENGTH} characters."
BODY_TOO_LONG = f"The text is at most {BODY_MAX_LENGTH} characters."
PAYMENT_DATA = "This text may not contain card or account numbers."
NOT_YOURS = "Only the author or an admin can change this entry."
SAVED = "Calendar entry saved."
DELETED = "Calendar entry deleted."


@dataclass(frozen=True, slots=True)
class CalendarItem:
    """One item on a day: a stored entry or a generated one."""

    uid: str
    kind: str
    on: date
    title: str
    #: The stored entry's text; empty for a generated item.
    body: str
    url: str
    store_id: uuid.UUID | None = None
    operator_id: uuid.UUID | None = None
    lease_id: uuid.UUID | None = None
    entry: CalendarEntry | None = None

    @property
    def generated(self) -> bool:
        return self.kind in GENERATED_KINDS

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


@dataclass(frozen=True, slots=True)
class Filters:
    kinds: frozenset[str] = frozenset(KINDS)
    store: uuid.UUID | None = None
    operator: uuid.UUID | None = None
    lease: uuid.UUID | None = None


def _uid_host() -> str:
    return urlsplit(settings.APP_BASE_URL).hostname or "finance"


def _store_operator(lease: Lease) -> str:
    label = lease.store.external_label
    return f"{label} ({lease.operator.display_name})" if lease.operator is not None else label


def _matches(item: CalendarItem, filters: Filters) -> bool:
    if item.kind not in filters.kinds:
        return False
    if filters.store is not None and item.store_id != filters.store:
        return False
    if filters.operator is not None and item.operator_id != filters.operator:
        return False
    return filters.lease is None or item.lease_id == filters.lease


def entry_item(entry: CalendarEntry, host: str | None = None) -> CalendarItem:
    """A stored entry as an item; the record links follow its lease when it has one."""
    lease = entry.lease
    store_id = entry.store_id or (lease.store_id if lease is not None else None)
    operator_id = entry.operator_id or (lease.operator_id if lease is not None else None)
    return CalendarItem(
        uid=f"entry-{entry.pk}@{host or _uid_host()}",
        kind=entry.kind,
        on=entry.on,
        title=entry.title,
        body=entry.body,
        url=reverse("ledger:calendar:detail", kwargs={"pk": entry.pk}),
        store_id=store_id,
        operator_id=operator_id,
        lease_id=entry.lease_id,
        entry=entry,
    )


def _entries(start: date, end: date, host: str) -> list[CalendarItem]:
    rows = (
        CalendarEntry.objects.filter(on__gte=start, on__lte=end)
        .select_related("lease", "store", "operator", "author")
        .order_by("on", "created_at", "id")
    )
    return [entry_item(entry, host) for entry in rows]


def _months(start: date, end: date) -> Iterator[tuple[int, int]]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _active_on(lease: Lease, day: date) -> bool:
    return (
        lease.collection_status == CollectionStatus.ACTIVE
        and (lease.started_on is None or lease.started_on <= day)
        and (lease.ended_on is None or lease.ended_on >= day)
    )


def _debits(leases: Iterable[Lease], start: date, end: date, host: str) -> list[CalendarItem]:
    found = []
    for lease in leases:
        if lease.collection_status != CollectionStatus.ACTIVE:
            continue
        url = reverse("ledger:leases:detail", kwargs={"pk": lease.pk})
        days = [d for d in (lease.debit_day, lease.second_debit_day) if d is not None]
        for seq, debit_day in enumerate(days, start=1):
            for year, month in _months(start, end):
                day = date(year, month, falls_on(debit_day, year, month))
                if not start <= day <= end or not _active_on(lease, day):
                    continue
                suffix = f", debit {seq}" if len(days) == 2 else ""
                found.append(
                    CalendarItem(
                        uid=f"debit-{lease.pk}-{seq}-{day:%Y%m%d}@{host}",
                        kind=KIND_DEBIT,
                        on=day,
                        title=f"Debit day: {_store_operator(lease)}{suffix}",
                        body="",
                        url=url,
                        store_id=lease.store_id,
                        operator_id=lease.operator_id,
                        lease_id=lease.pk,
                    )
                )
    return found


def _anniversaries(
    leases: Iterable[Lease], start: date, end: date, host: str
) -> list[CalendarItem]:
    found = []
    for lease in leases:
        store = lease.store
        if (
            lease.collection_status == CollectionStatus.PROSPECTIVE
            or store.open_date is None
            or store.status in (StoreStatus.CLOSED, StoreStatus.TERMINATED)
        ):
            continue
        for year in range(max(start.year, store.open_date.year), end.year + 1):
            day = insurance.anniversary(store.open_date, year)
            if not start <= day <= end or not insurance.holds_on(lease, day):
                continue
            if store.closed_at is not None and day >= store.closed_at:
                continue
            found.append(
                CalendarItem(
                    uid=f"insurance-{lease.pk}-{year}@{host}",
                    kind=KIND_INSURANCE,
                    on=day,
                    title=f"Insurance anniversary: {_store_operator(lease)}",
                    body="",
                    url=reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
                    store_id=lease.store_id,
                    operator_id=lease.operator_id,
                    lease_id=lease.pk,
                )
            )
    return found


def _lease_ends(leases: Iterable[Lease], start: date, end: date, host: str) -> list[CalendarItem]:
    return [
        CalendarItem(
            uid=f"lease-end-{lease.pk}@{host}",
            kind=KIND_LEASE_END,
            on=lease.ended_on,
            title=f"Lease ends: {_store_operator(lease)}",
            body="",
            url=reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
            store_id=lease.store_id,
            operator_id=lease.operator_id,
            lease_id=lease.pk,
        )
        for lease in leases
        if lease.ended_on is not None and start <= lease.ended_on <= end
    ]


def _redebits(start: date, end: date, host: str) -> list[CalendarItem]:
    """The re-debit dates of rescheduled returns (PLAN-011b D5): one item per re-debit
    charge due in the range and not voided; the title names the store and the operator,
    never the amount."""
    found = []
    for charge in (
        OneOffCharge.objects.select_related("lease__store", "lease__operator", "operator")
        .filter(category=OneOffCategory.REDEBIT, due_date__gte=start, due_date__lte=end)
        .exclude(status=OneOffChargeStatus.VOIDED)
        .order_by("due_date", "id")
    ):
        lease = charge.lease
        if lease is not None:
            where = _store_operator(lease)
        else:
            where = charge.operator.display_name if charge.operator is not None else ""
        found.append(
            CalendarItem(
                uid=f"redebit-{charge.pk}@{host}",
                kind=KIND_REDEBIT,
                on=charge.due_date,
                title=f"Re-debit: {where}",
                body="",
                url=reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk}),
                store_id=lease.store_id if lease is not None else None,
                operator_id=charge.operator_id_billed,
                lease_id=charge.lease_id,
            )
        )
    return found


def items(start: date, end: date, filters: Filters | None = None) -> list[CalendarItem]:
    """Every item from ``start`` to ``end`` (both included) that ``filters`` keeps, by day:
    stored entries first, then generated ones, each by title. Three queries."""
    chosen = filters or Filters()
    host = _uid_host()
    found: list[CalendarItem] = []
    if chosen.kinds & {KIND_NOTE, KIND_REMINDER}:
        found += _entries(start, end, host)
    if KIND_REDEBIT in chosen.kinds:
        found += _redebits(start, end, host)
    if chosen.kinds & LEASE_KINDS:
        leases = list(
            Lease.objects.select_related("store", "operator")
            .filter(
                Q(collection_status=CollectionStatus.ACTIVE)
                | Q(ended_on__gte=start, ended_on__lte=end)
                | Q(store__open_date__isnull=False)
            )
            .exclude(collection_status=CollectionStatus.PROSPECTIVE)
            .order_by("store__external_label", "id")
        )
        if KIND_DEBIT in chosen.kinds:
            found += _debits(leases, start, end, host)
        if KIND_INSURANCE in chosen.kinds:
            found += _anniversaries(leases, start, end, host)
        if KIND_LEASE_END in chosen.kinds:
            found += _lease_ends(leases, start, end, host)
    kept = [item for item in found if _matches(item, chosen)]
    return sorted(kept, key=lambda item: (item.on, item.generated, item.title.casefold(), item.uid))


def today_entries(today: date) -> list[CalendarItem]:
    """The stored entries of ``today`` (the attention list shows them). One query."""
    return _entries(today, today, _uid_host())


# --- Month view ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonthDay:
    day: date
    in_month: bool
    items: tuple[CalendarItem, ...]


def month_weeks(month: date, found: Iterable[CalendarItem]) -> list[list[MonthDay]]:
    """The weeks (Sunday first) that cover ``month``, each day with its items."""
    by_day: dict[date, list[CalendarItem]] = {}
    for item in found:
        by_day.setdefault(item.on, []).append(item)
    weeks = month_calendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
    return [
        [MonthDay(day, day.month == month.month, tuple(by_day.get(day, ()))) for day in week]
        for week in weeks
    ]


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    last = first.replace(day=month_calendar.monthrange(first.year, first.month)[1])
    return first, last


# --- Entries -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryInput:
    kind: str
    on: date
    title: str
    body: str = ""
    email_morning: bool = False
    store: uuid.UUID | None = None
    operator: uuid.UUID | None = None
    lease: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    ok: bool
    message: str
    field: str | None = None
    entry: CalendarEntry | None = None


def can_change(entry: CalendarEntry, user: User, *, is_admin: bool) -> bool:
    """The author changes their own entry; an admin changes any."""
    return is_admin or (entry.author_id is not None and entry.author_id == user.pk)


def _refusal(data: EntryInput, title: str, body: str) -> tuple[str, str] | None:
    if data.kind not in (KIND_NOTE, KIND_REMINDER):
        return "Choose a note or a reminder.", "kind"
    if not title:
        return TITLE_REQUIRED, "title"
    if len(title) > CALENDAR_TITLE_MAX_LENGTH:
        return TITLE_TOO_LONG, "title"
    if len(body) > BODY_MAX_LENGTH:
        return BODY_TOO_LONG, "body"
    for field, text in (("title", title), ("body", body)):
        if contains_card_number(text) or contains_bank_routing_pattern(text):
            return PAYMENT_DATA, field
    return None


def _state(entry: CalendarEntry) -> dict[str, object]:
    return {
        "kind": entry.kind,
        "on": entry.on.isoformat(),
        "title": entry.title,
        "body": entry.body,
        "email_morning": entry.email_morning,
        "store": str(entry.store_id) if entry.store_id else None,
        "operator": str(entry.operator_id) if entry.operator_id else None,
        "lease": str(entry.lease_id) if entry.lease_id else None,
    }


def _audit(
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: object,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )


def _write(entry: CalendarEntry, data: EntryInput, title: str, body: str) -> None:
    entry.kind = data.kind
    entry.on = data.on
    entry.title = title
    entry.body = body
    entry.email_morning = data.email_morning
    entry.store_id = data.store
    entry.operator_id = data.operator
    entry.lease_id = data.lease


def save_entry(
    data: EntryInput, *, actor: User, is_admin: bool, entry_id: uuid.UUID | None = None
) -> EntryOutcome:
    """Create an entry, or change ``entry_id`` (its author or an admin). The title and
    the body are stored as :func:`visible_text`. Refusals change nothing."""
    title = visible_text(data.title or "")
    body = visible_text(data.body or "")
    refusal = _refusal(data, title, body)
    if refusal is not None:
        return EntryOutcome(False, refusal[0], refusal[1])
    with transaction.atomic():
        if entry_id is None:
            entry = CalendarEntry(author=actor)
            before = None
        else:
            entry = CalendarEntry.objects.select_for_update().get(pk=entry_id)
            if not can_change(entry, actor, is_admin=is_admin):
                return EntryOutcome(False, NOT_YOURS)
            before = _state(entry)
        _write(entry, data, title, body)
        try:
            entry.save()
        except ValidationError:
            return EntryOutcome(False, PAYMENT_DATA, "body")
        action = "calendar_entry.created" if before is None else "calendar_entry.changed"
        _audit(actor, action, ENTITY_ENTRY, entry.pk, before, _state(entry))
    return EntryOutcome(True, SAVED, entry=entry)


def delete_entry(entry_id: uuid.UUID, *, actor: User, is_admin: bool) -> EntryOutcome:
    """Delete an entry (its author or an admin); the audit row keeps what it said."""
    with transaction.atomic():
        entry = CalendarEntry.objects.select_for_update().get(pk=entry_id)
        if not can_change(entry, actor, is_admin=is_admin):
            return EntryOutcome(False, NOT_YOURS)
        before = _state(entry)
        pk = entry.pk
        entry.delete()
        _audit(actor, "calendar_entry.deleted", ENTITY_ENTRY, pk, before, None)
    return EntryOutcome(True, DELETED)


# --- Feeds ---------------------------------------------------------------------------------


def token_hash(token: str) -> bytes:
    """The SHA-256 digest of a token, as stored."""
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


def live_feed(user: User) -> CalendarFeed | None:
    return CalendarFeed.objects.filter(user=user, revoked_at__isnull=True).first()


def create_feed(user: User) -> str:
    """A new feed for ``user``, revoking the live one; the token, to show once."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with transaction.atomic():
        _revoke(user)
        feed = CalendarFeed.objects.create(user=user, token_hash=token_hash(token))
        _audit(user, "calendar_feed.created", ENTITY_FEED, feed.pk, None, {"user": user.pk})
    return token


def _revoke(user: User) -> bool:
    feed = (
        CalendarFeed.objects.select_for_update().filter(user=user, revoked_at__isnull=True).first()
    )
    if feed is None:
        return False
    feed.revoked_at = timezone.now()
    feed.save(update_fields=["revoked_at"])
    _audit(
        user,
        "calendar_feed.revoked",
        ENTITY_FEED,
        feed.pk,
        {"revoked_at": None},
        {"revoked_at": feed.revoked_at.isoformat()},
    )
    return True


def revoke_feed(user: User) -> bool:
    """Revoke the user's live feed; ``False`` when there was none."""
    with transaction.atomic():
        return _revoke(user)


def feed_for_token(token: str) -> CalendarFeed | None:
    """The live feed of ``token``, or ``None`` (unknown, malformed or revoked)."""
    if not TOKEN_PATTERN.match(token):
        return None
    digest = token_hash(token)
    feed = (
        CalendarFeed.objects.select_related("user")
        .filter(token_hash=digest, revoked_at__isnull=True)
        .first()
    )
    if feed is None or not hmac.compare_digest(bytes(feed.token_hash), digest):
        return None
    if not feed.user.is_active:
        return None
    return feed


def feed_window(today: date) -> tuple[date, date]:
    return today - timedelta(days=FEED_DAYS_BEFORE), today + timedelta(days=FEED_DAYS_AFTER)


def record_read(feed: CalendarFeed, events: int) -> None:
    _audit(
        feed.user,
        "calendar_feed.read",
        ENTITY_FEED,
        feed.pk,
        None,
        {"user": feed.user.pk, "events": events},
    )


# --- ICS (RFC 5545) ------------------------------------------------------------------------


def ics_text(value: str) -> str:
    """A TEXT value: backslash, semicolon and comma escaped, line breaks as ``\\n``."""
    escaped = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return escaped.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def fold(line: str) -> str:
    """A content line folded at 75 octets, continuation lines starting with a space."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts: list[str] = []
    current = ""
    size = 0
    limit = 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append(current)
            current, size, limit = "", 0, 74
        current += char
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def ics(found: Iterable[CalendarItem], *, now: datetime, base_url: str) -> str:
    """The items as one VCALENDAR of all-day VEVENTs."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Finance//Staff calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Finance calendar",
        f"X-WR-TIMEZONE:{settings.TIME_ZONE}",
    ]
    for item in found:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{item.uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{item.on:%Y%m%d}",
            f"DTEND;VALUE=DATE:{item.on + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{ics_text(item.title)}",
            f"CATEGORIES:{ics_text(item.kind_label)}",
            f"URL:{base_url.rstrip('/')}{item.url}",
        ]
        if item.body:
            lines.append(f"DESCRIPTION:{ics_text(item.body)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(f"{fold(line)}\r\n" for line in lines)


# --- Morning e-mails (D8) ------------------------------------------------------------------

#: The ``params`` key of a ``calendar_email`` job: the day (ISO) it is the morning of.
MORNING_DAY = "on"
#: Fixed namespace of the morning e-mails' replay keys: one job per item and day, so a
#: second tick of the same day queues nothing.
_MORNING_NAMESPACE = uuid.UUID("6f1d3c52-6a8e-4b8e-9d4c-2f7f1a0b9e11")
MORNING_GONE = "Nothing sent: the item no longer exists."
MORNING_NOT_DUE = "Nothing sent: the item no longer asks for a morning e-mail that day."
MORNING_NO_ADDRESS = "Not sent: the author has no e-mail address."
MORNING_SENT = {
    mail.SENT: "Sent.",
    mail.CATCH_ALL: "Sent to the test inbox.",
    mail.SUPPRESSED: "Not sent: mail is off.",
}
MORNING_FAILED = "Failed: {cause}."


@dataclass(frozen=True, slots=True)
class MorningReport:
    entries: int
    promises: int


def _morning_key(entity_type: str, pk: uuid.UUID, today: date) -> uuid.UUID:
    return uuid.uuid5(_MORNING_NAMESPACE, f"{entity_type}:{pk}:{today.isoformat()}")


def _queue_morning(entity: CalendarEntry | ContactEntry, today: date) -> bool:
    """Queue one morning e-mail job; ``False`` when that day's job already exists."""
    key = _morning_key(type(entity).__name__, entity.pk, today)
    if Job.objects.filter(replay_key=key).exists():
        return False
    jobs.enqueue(JobKind.CALENDAR_EMAIL, entity, {MORNING_DAY: today.isoformat()}, None, key)
    return True


def queue_morning_emails(today: date) -> MorningReport:
    """Queue the morning e-mails of ``today``: each stored entry of the day that asks for
    one (to its author), and each open promise to pay due that day (to the author of the
    promise). Entries and promises without an author are left out."""
    entries = CalendarEntry.objects.filter(
        on=today, email_morning=True, author__isnull=False
    ).order_by("created_at", "id")
    promises = ContactEntry.objects.filter(
        kind=ContactKind.PROMISE,
        promised_date=today,
        status=ContactStatus.OPEN,
        deleted_at__isnull=True,
        author__isnull=False,
    ).order_by("created_at", "id")
    queued_entries = sum(_queue_morning(entry, today) for entry in entries)
    queued_promises = sum(_queue_morning(promise, today) for promise in promises)
    return MorningReport(entries=queued_entries, promises=queued_promises)


def _author_address(author: User | None) -> str:
    if author is None or not author.is_active or not is_address(author.email or ""):
        return ""
    return str(author.email)


def _entry_message(entry: CalendarEntry, address: str) -> mail.Message:
    link = f"{settings.APP_BASE_URL.rstrip('/')}{entry_item(entry).url}"
    text = f"{entry.get_kind_display()} for {entry.on:%Y-%m-%d}: {entry.title}\n\n"
    if entry.body:
        text += f"{entry.body}\n\n"
    text += f"Open it: {link}\n"
    subject = " ".join(f"{entry.get_kind_display()}: {entry.title}".split())
    return mail.Message(to=address, subject=subject[:SUBJECT_MAX_LENGTH], text=text)


def _promise_message(promise: ContactEntry, address: str) -> mail.Message:
    operator = promise.operator
    path = reverse("ledger:operators:detail", kwargs={"pk": operator.pk})
    amount = format_amount(promise.promised_amount) if promise.promised_amount is not None else ""
    text = (
        f"{operator.display_name} promised to pay {amount} by "
        f"{promise.promised_date:%Y-%m-%d}.\n\n{promise.body}\n\n"
        f"Contact log: {settings.APP_BASE_URL.rstrip('/')}{path}#contacts\n"
    )
    subject = " ".join(f"Promise to pay due today: {operator.display_name}".split())
    return mail.Message(to=address, subject=subject[:SUBJECT_MAX_LENGTH], text=text)


def _morning_item(
    entity_type: str, entity_id: uuid.UUID, on: date
) -> tuple[CalendarEntry | ContactEntry | None, User | None]:
    if entity_type == JobEntity.CALENDAR_ENTRY:
        entry = CalendarEntry.objects.select_related("author", "lease").filter(pk=entity_id).first()
        if entry is None or not entry.email_morning or entry.on != on:
            return None, None
        return entry, entry.author
    promise = (
        ContactEntry.objects.select_related("author", "operator")
        .filter(
            pk=entity_id,
            kind=ContactKind.PROMISE,
            promised_date=on,
            status=ContactStatus.OPEN,
            deleted_at__isnull=True,
        )
        .first()
    )
    return (promise, promise.author) if promise is not None else (None, None)


def send_morning_email(entity_type: str, entity_id: uuid.UUID, on: date) -> tuple[bool, str]:
    """The ``calendar_email`` job: one morning e-mail to the item's author, if the item
    still asks for it on ``on``. Audited as ``calendar.morning_email`` with the outcome,
    never the address or the text."""
    exists = (
        CalendarEntry.objects.filter(pk=entity_id).exists()
        if entity_type == JobEntity.CALENDAR_ENTRY
        else ContactEntry.objects.filter(pk=entity_id).exists()
    )
    if not exists:
        return True, MORNING_GONE
    item, author = _morning_item(entity_type, entity_id, on)
    if item is None:
        return True, MORNING_NOT_DUE
    address = _author_address(author)
    if not address:
        _audit(
            None, "calendar.morning_email", entity_type, entity_id, None, {"outcome": "no_address"}
        )
        return False, MORNING_NO_ADDRESS
    message = (
        _entry_message(item, address)
        if isinstance(item, CalendarEntry)
        else _promise_message(item, address)
    )
    try:
        receipt = mail.get_sender().send(message)
    except mail.MailUnavailable as error:
        _audit(
            None,
            "calendar.morning_email",
            entity_type,
            entity_id,
            None,
            {"outcome": "failed", "error": error.cause},
        )
        return False, MORNING_FAILED.format(cause=error.cause)
    _audit(
        None,
        "calendar.morning_email",
        entity_type,
        entity_id,
        None,
        {"outcome": receipt.disposition, "on": on.isoformat()},
    )
    return True, MORNING_SENT[receipt.disposition]
