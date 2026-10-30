"""The Notes & follow-ups tab's one list (docs/qa/PLAN-014 B6).

:func:`entries` is the union of a store's live notes (:func:`ledger.services.notes.for_store`)
and the entries about a lease of it (:func:`ledger.services.contacts.store_entries`: calls,
visits, e-mails logged by hand and promises with their outcomes and corrections), newest first:
by day (a note's day in New York, an entry's ``happened_on``), then by when each was written.
``show`` narrows it (``?show=``, :func:`parse_show`): ``notes``, ``contacts`` (Calls &
promises) or ``emails``, which lists nothing until the messaging engine (Batch D) keeps the
e-mails sent; anything else is ``all``. Read only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.utils import timezone

from ledger.models import ContactEntry, Note, Store
from ledger.services import contacts, notes

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
    was written; ``payload`` the row itself (a :class:`~ledger.models.Note` or a
    :class:`~ledger.models.ContactEntry`)."""

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
    found.sort(key=lambda entry: (entry.day, entry.at), reverse=True)
    return found if limit is None else found[: limit + 1]


def _more(limit: int | None) -> int | None:
    return None if limit is None else limit + 1
