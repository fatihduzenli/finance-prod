"""The Notes & follow-ups tab's one list (docs/qa/PLAN-014 B6, D5).

:func:`entries` is the union of a store's live notes (:func:`ledger.services.notes.for_store`),
the entries about a lease of it (:func:`ledger.services.contacts.store_entries`: calls,
visits, e-mails logged by hand and promises with their outcomes and corrections) and the
messages about a lease of it (:func:`ledger.services.messaging.for_store`: sent, held,
failed or suppressed, with their stored text), newest first: by day (a note's day in New
York, an entry's ``happened_on``, a message's day queued in New York), then by when each
was written. ``show`` narrows it (``?show=``, :func:`parse_show`): ``notes``, ``contacts``
(Calls & promises) or ``emails`` (the messages); anything else is ``all``. :class:`Listing`
is the whole list, without the tab's cap, as the store's All entries page pages it. Read
only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, overload

from django.utils import timezone

from ledger.models import ContactEntry, Note, OutboundMessage, Store
from ledger.services import contacts, messaging, notes

NOTE = "note"
CONTACT = "contact"
EMAIL = "email"
SHOW_ALL = "all"
SHOW_NOTES = "notes"
SHOW_CONTACTS = "contacts"
SHOW_EMAILS = "emails"
#: The filter's values and words, in the pill's order.
SHOWS: tuple[tuple[str, str], ...] = (
    (SHOW_ALL, "All"),
    (SHOW_NOTES, "Notes"),
    (SHOW_CONTACTS, "Calls & promises"),
    (SHOW_EMAILS, "E-mails"),
)
#: What each filter lists, by entry kind.
_KINDS: dict[str, frozenset[str]] = {
    SHOW_ALL: frozenset({NOTE, CONTACT, EMAIL}),
    SHOW_NOTES: frozenset({NOTE}),
    SHOW_CONTACTS: frozenset({CONTACT}),
    SHOW_EMAILS: frozenset({EMAIL}),
}
EMPTY_EMAILS = "No e-mails yet."
EMPTY_NOTES = "No notes yet."


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One row of the list: ``kind`` one of ``note``, ``contact``, ``email``; ``at`` when it
    was written; ``payload`` the row itself (a :class:`~ledger.models.Note`, a
    :class:`~ledger.models.ContactEntry` or an :class:`~ledger.models.OutboundMessage`)."""

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
    found.sort(key=lambda entry: (entry.day, entry.at), reverse=True)
    return found if limit is None else found[: limit + 1]


def _more(limit: int | None) -> int | None:
    return None if limit is None else limit + 1


def empty_text(show: str) -> str:
    """What an empty list says for ``show``: "No e-mails yet." and "No notes yet."; ``""``
    for the others (the page words its own "No entries yet")."""
    return {SHOW_EMAILS: EMPTY_EMAILS, SHOW_NOTES: EMPTY_NOTES}.get(parse_show(show), "")


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
