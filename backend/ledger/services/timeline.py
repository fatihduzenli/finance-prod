"""The Notes & follow-ups tab's one list (docs/qa/PLAN-014 B6, D5).

:func:`entries` is the union of a store's live notes (:func:`ledger.services.notes.for_store`),
the entries about a lease of it (:func:`ledger.services.contacts.store_entries`: calls,
visits, e-mails logged by hand and promises with their outcomes and corrections) and the
messages about a lease of it (:func:`ledger.services.messaging.for_store`: sent, held,
failed or suppressed, with their stored text) and the status changes of its leases
(:func:`status_changes`, PLAN-015b: collection paused with its reason, resumed, the lease
activated on its start date or by staff), newest first: by day (a note's day in New
York, an entry's ``happened_on``, a message's or a change's day in New York), then by when
each was written. ``show`` narrows it (``?show=``, :func:`parse_show`): ``notes``,
``contacts`` (Calls & promises), ``emails`` (the messages) or ``status`` (Lease status);
anything else is ``all``. :class:`Listing`
is the whole list, without the tab's cap, as the store's All entries page pages it. Read
only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, overload

from django.db.models import QuerySet
from django.utils import timezone

from finance.dates import day_text
from ledger.models import AuditLog, ContactEntry, Note, OutboundMessage, Store
from ledger.services import contacts, leases, messaging, notes

NOTE = "note"
CONTACT = "contact"
EMAIL = "email"
STATUS = "status"
SHOW_ALL = "all"
SHOW_NOTES = "notes"
SHOW_CONTACTS = "contacts"
SHOW_EMAILS = "emails"
SHOW_STATUS = "status"
#: The filter's values and words, in the pill's order.
SHOWS: tuple[tuple[str, str], ...] = (
    (SHOW_ALL, "All"),
    (SHOW_NOTES, "Notes"),
    (SHOW_CONTACTS, "Calls & promises"),
    (SHOW_EMAILS, "E-mails"),
    (SHOW_STATUS, "Lease status"),
)
#: What each filter lists, by entry kind.
_KINDS: dict[str, frozenset[str]] = {
    SHOW_ALL: frozenset({NOTE, CONTACT, EMAIL, STATUS}),
    SHOW_NOTES: frozenset({NOTE}),
    SHOW_CONTACTS: frozenset({CONTACT}),
    SHOW_EMAILS: frozenset({EMAIL}),
    SHOW_STATUS: frozenset({STATUS}),
}
#: A status change's words and badge (PLAN-015b): (text, badge label, badge tone).
PAUSED_TEXT = "Collection paused — {reason}"
RESUMED_TEXT = "Collection resumed"
ACTIVATED_TEXT = {
    leases.TRIGGER_START_DATE: "Lease activated (start date)",
    leases.TRIGGER_STAFF: "Lease activated (by staff)",
}
#: PLAN-015d: "Start date changed Nov 15, 2026 → Nov 20, 2026 — <reason>".
START_CHANGED_TEXT = "Start date changed {old} → {new} — {reason}"
EMPTY_EMAILS = "No e-mails yet."
EMPTY_NOTES = "No notes yet."
EMPTY_STATUS = "No status changes yet."


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One row of the list: ``kind`` one of ``note``, ``contact``, ``email``, ``status``;
    ``at`` when it was written; ``payload`` the row itself (a :class:`~ledger.models.Note`,
    a :class:`~ledger.models.ContactEntry`, an :class:`~ledger.models.OutboundMessage` or,
    for a status change, its :class:`~ledger.models.AuditLog` row)."""

    kind: str
    at: datetime
    payload: Any
    #: The day it is listed under (newest first).
    day: date


def parse_show(raw: object) -> str:
    """``raw`` when it is one of :data:`SHOWS`' values, else ``all``."""
    return raw if isinstance(raw, str) and raw in _KINDS else SHOW_ALL


def _note(note: Note) -> TimelineEntry:
    return TimelineEntry(NOTE, note.created_at, note, timezone.localdate(note.created_at))


def _contact(entry: ContactEntry) -> TimelineEntry:
    return TimelineEntry(CONTACT, entry.created_at, entry, entry.happened_on)


def _email(message: OutboundMessage) -> TimelineEntry:
    return TimelineEntry(EMAIL, message.queued_at, message, timezone.localdate(message.queued_at))


def _status(row: AuditLog) -> TimelineEntry:
    return TimelineEntry(STATUS, row.at, row, timezone.localdate(row.at))


def status_changes(store: Store) -> QuerySet[AuditLog]:
    """The status changes of the store's leases (their ``lease.collection_paused``,
    ``lease.collection_resumed``, ``lease.activated`` and, PLAN-015d,
    ``lease.start_date_changed`` audit rows), newest first, with
    their staff member."""
    ids = [str(pk) for pk in store.leases.values_list("pk", flat=True)]
    return (
        AuditLog.objects.filter(
            entity_type=leases.ENTITY_LEASE, entity_id__in=ids, action__in=leases.STATUS_ACTIONS
        )
        .select_related("actor")
        .order_by("-at", "-id")
    )


@dataclass(frozen=True, slots=True)
class StatusWords:
    """How a status change reads on the list: its sentence and the status it left the lease
    in, as a badge (label and tone)."""

    text: str
    label: str
    tone: str


def status_words(row: AuditLog) -> StatusWords:
    """The words of a status change (:func:`status_changes`): "Collection paused — <reason>"
    (Paused), "Collection resumed" (Active), "Lease activated (start date)" or "(by staff)"
    (Active)."""
    after = row.after or {}
    if row.action == leases.COLLECTION_PAUSED_ACTION:
        return StatusWords(PAUSED_TEXT.format(reason=after.get("reason", "")), "Paused", "pending")
    if row.action == leases.COLLECTION_RESUMED_ACTION:
        return StatusWords(RESUMED_TEXT, "Active", "paid")
    if row.action == leases.START_DATE_CHANGED_ACTION:
        text = START_CHANGED_TEXT.format(
            old=_day_words(after.get("old_start")),
            new=_day_words(after.get("new_start")),
            reason=after.get("reason", ""),
        )
        active = after.get("collection_status") == "active"
        return StatusWords(
            text, "Active" if active else "Prospective", "paid" if active else "neutral"
        )
    trigger = str(after.get("trigger", ""))
    text = ACTIVATED_TEXT.get(trigger, ACTIVATED_TEXT[leases.TRIGGER_STAFF])
    return StatusWords(text, "Active", "paid")


def _day_words(value: object) -> str:
    try:
        return day_text(date.fromisoformat(str(value)))
    except ValueError:
        return "—"


def entries(
    store: Store, show: str, *, include_deleted: bool = False, limit: int | None = None
) -> list[TimelineEntry]:
    """The store's list for ``show`` (see the module docstring), newest first; with
    ``limit``, at most ``limit + 1`` rows (one more than shown says there are more), each
    source read at most that far."""
    kinds = _KINDS[parse_show(show)]
    found: list[TimelineEntry] = []
    if NOTE in kinds:
        found += [_note(note) for note in notes.for_store(store, limit=_more(limit))]
    if CONTACT in kinds:
        rows = contacts.store_entries(store, include_deleted=include_deleted)
        if limit is not None:
            rows = rows[: limit + 1]
        found += [_contact(entry) for entry in rows]
    if EMAIL in kinds:
        sent = messaging.for_store(store.pk)
        if limit is not None:
            sent = sent[: limit + 1]
        found += [_email(message) for message in sent]
    if STATUS in kinds:
        changes = status_changes(store)
        if limit is not None:
            changes = changes[: limit + 1]
        found += [_status(row) for row in changes]
    found.sort(key=lambda entry: (entry.day, entry.at), reverse=True)
    return found if limit is None else found[: limit + 1]


def _more(limit: int | None) -> int | None:
    return None if limit is None else limit + 1


def empty_text(show: str) -> str:
    """What an empty list says for ``show``: "No e-mails yet.", "No notes yet." and "No
    status changes yet."; ``""`` for the others (the page words its own "No entries yet")."""
    return {
        SHOW_EMAILS: EMPTY_EMAILS,
        SHOW_NOTES: EMPTY_NOTES,
        SHOW_STATUS: EMPTY_STATUS,
    }.get(parse_show(show), "")


class Listing:
    """The store's whole list for ``show`` as a :class:`~django.core.paginator.Paginator`
    pages it: :meth:`count` counts each source, and a page reads each source only as far as
    the page's end (both are newest first, so the first rows of the union come from the
    first rows of each)."""

    def __init__(self, store: Store, show: str, *, include_deleted: bool) -> None:
        self.store = store
        self.show = parse_show(show)
        self.include_deleted = include_deleted

    def count(self) -> int:
        kinds = _KINDS[self.show]
        total = 0
        if NOTE in kinds:
            total += notes.count_for_store(self.store)
        if CONTACT in kinds:
            total += contacts.store_entries(
                self.store, include_deleted=self.include_deleted
            ).count()
        if EMAIL in kinds:
            total += messaging.for_store(self.store.pk).count()
        if STATUS in kinds:
            total += status_changes(self.store).count()
        return total

    def __len__(self) -> int:
        return self.count()

    def __iter__(self) -> Iterator[TimelineEntry]:
        return iter(entries(self.store, self.show, include_deleted=self.include_deleted))

    @overload
    def __getitem__(self, index: int) -> TimelineEntry: ...
    @overload
    def __getitem__(self, index: slice) -> list[TimelineEntry]: ...
    def __getitem__(self, index: int | slice) -> TimelineEntry | list[TimelineEntry]:
        if isinstance(index, int):
            return list(self)[index]
        found = entries(
            self.store, self.show, include_deleted=self.include_deleted, limit=index.stop
        )
        return found[index]
