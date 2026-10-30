"""The notes & follow-ups as the operator page, the store page and the full lists show it
(docs/qa/PLAN-012 D2, D9): each entry with what the request's user may do on it.

Kept apart from :mod:`ledger.views.contacts` so that the operator and store pages, which
that module builds on, can use it without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role, meets, role_of
from ledger.models import ContactEntry, ContactKind, ContactStatus, Note, Store
from ledger.services import contacts, notes


@dataclass(frozen=True, slots=True)
class LogRow:
    """One entry as the log shows it to the request's user."""

    entry: ContactEntry
    can_edit: bool
    edit_until: datetime | None
    can_correct: bool
    can_delete: bool
    #: Kept, Reschedule and Broken on an open promise (PLAN-012c-b D2).
    can_record_outcome: bool
    #: "Closed $X by D as ..." when the entry recorded a promise's outcome, and the link to
    #: that promise's row (PLAN-012c-b D5); both ``""`` otherwise.
    closes_text: str = ""
    closes_url: str = ""


def include_deleted(request: HttpRequest) -> bool:
    """Billing and admin see deleted entries (struck through, with the reason); a viewer
    does not."""
    return meets(role_of(request.user), Role.BILLING)


def _closes_url(request: HttpRequest, promise: ContactEntry, store: Store | None) -> str:
    """The closed promise's row: on this page, unless the page is ``store``'s and the
    promise is not about a lease of it; then on the operator page."""
    anchor = f"#contact-{promise.pk}"
    if store is not None and (promise.lease is None or promise.lease.store_id != store.pk):
        return reverse("ledger:operators:detail", kwargs={"pk": promise.operator_id}) + anchor
    return request.get_full_path() + anchor


def log_rows(
    request: HttpRequest, entries: list[ContactEntry], *, store: Store | None = None
) -> list[LogRow]:
    """The entries with what the request's user may do on each: the author edits inside
    the window; any other billing user, or the author after it, adds a correction; an
    admin deletes; billing records the outcome of an open promise of an operator not
    archived. Nothing on a deleted entry. An entry that closed a promise names it and links
    to it (D5); ``store`` is the store whose page lists ``entries``, if any."""
    role = role_of(request.user)
    billing = meets(role, Role.BILLING)
    admin = meets(role, Role.ADMIN)
    user = request.user if isinstance(request.user, User) else None
    now = timezone.now()
    rows = []
    for entry in entries:
        closed = contacts.closed_promise(entry)
        editable = billing and contacts.can_edit(entry, user, now)
        live = entry.deleted_at is None
        rows.append(
            LogRow(
                entry=entry,
                can_edit=editable,
                edit_until=contacts.edit_closes_at(entry) if editable else None,
                can_correct=billing and live and not editable,
                can_delete=admin and live,
                can_record_outcome=billing
                and live
                and entry.kind == ContactKind.PROMISE
                and entry.status == ContactStatus.OPEN
                and entry.operator.archived_at is None,
                closes_text=contacts.closes_sentence(closed) if closed is not None else "",
                closes_url=_closes_url(request, closed, store) if closed is not None else "",
            )
        )
    return rows


def store_add_url(request: HttpRequest, store: Store, today: date) -> str:
    """The store's Add entry link (PLAN-012 D3, PLAN-012c D1) for billing when the store
    has a lease of an operator not archived (:func:`contacts.store_contact_lease`); ``""``
    otherwise."""
    if not meets(role_of(request.user), Role.BILLING):
        return ""
    if contacts.store_contact_lease(store, today) is None:
        return ""
    return reverse("ledger:stores:contacts_add", kwargs={"pk": store.pk})


@dataclass(frozen=True, slots=True)
class NoteRow:
    """One store note as the tab shows it to the request's user (PLAN-014 B5): billing pins
    it; its author or an admin edits and deletes it."""

    note: Note
    can_pin: bool
    can_change: bool


def note_rows(request: HttpRequest, found: list[Note]) -> list[NoteRow]:
    billing = meets(role_of(request.user), Role.BILLING)
    user = request.user if isinstance(request.user, User) else None
    return [
        NoteRow(note=note, can_pin=billing, can_change=billing and notes.can_change(note, user))
        for note in found
    ]
