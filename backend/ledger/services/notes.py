"""A store's notes (docs/qa/PLAN-014 B5): the Notes & follow-ups tab's free text.

Billing writes a note about a store (:func:`add`): the note names the store
(``entity_type`` ``store``) and the store's current lease when it is written
(:func:`ledger.services.lease_end.current_lease`, none for a vacant store). Its author or an
admin changes it (:func:`edit`, ``edited_at``) or deletes it with a reason (:func:`delete`, a
soft delete: the row stays, the audit row keeps the body); billing pins and unpins it
(:func:`pin`, :func:`unpin`). Each change is one ``audit_log`` row (``note.created``,
``note.edited`` with the body before and after, ``note.pinned``, ``note.unpinned``,
``note.deleted`` with the body in ``before``). A body is required, at most
:data:`~ledger.models.NOTE_BODY_MAX_LENGTH` characters and free of card or account numbers; a
reason at most :data:`~ledger.models.NOTE_DELETE_REASON_MAX_LENGTH`. Every refusal raises
:class:`NoteRefused` (:class:`NotAllowed` for the wrong person) with a sentence that never
repeats the text.

:func:`for_store` lists the store's live notes, newest first; :func:`pinned_for` the pinned
ones, the latest pinned first, for the NOTES card.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.display import staff_name
from accounts.roles import Role, meets, role_of
from ledger.models import (
    NOTE_BODY_MAX_LENGTH,
    NOTE_DELETE_REASON_MAX_LENGTH,
    AuditLog,
    Note,
    NoteEntityType,
    Store,
)
from ledger.services import lease_end
from ledger.validators import (
    contains_bank_routing_pattern,
    contains_card_number,
    visible_text,
)

ENTITY_NOTE = "note"
#: The NOTES card's most notes.
PINNED_SHOWN = 5
BODY_REQUIRED = "Write the note first."
BODY_TOO_LONG = f"A note is at most {NOTE_BODY_MAX_LENGTH:,} characters."
PAYMENT_DATA = "A note cannot hold a card or account number."
REASON_REQUIRED = "A reason is required: say why this note is deleted."
REASON_TOO_LONG = f"A reason is at most {NOTE_DELETE_REASON_MAX_LENGTH} characters."
REASON_PAYMENT_DATA = "A reason cannot hold a card or account number."
NOT_AUTHOR = "Only the note's author or an admin may change it."
NOT_BILLING = "Only billing or an admin may pin a note."
GONE = "This note was deleted; it cannot be changed."
ADDED = "Note saved."
EDITED = "Note changed."
UNCHANGED = "Nothing was changed."
PINNED = "Note pinned."
UNPINNED = "Note unpinned."
DELETED = "Note deleted."


class NoteRefused(Exception):  # noqa: N818 (reads as an outcome, like CostRefused)
    """The note was not saved; ``message`` says why without repeating the text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotAllowed(NoteRefused):
    """The actor may not change this note."""


def _text(raw: object, *, required: str, too_long: str, limit: int, payment: str) -> str:
    text = visible_text(raw) if isinstance(raw, str) else ""
    if not text:
        raise NoteRefused(required)
    if len(text) > limit:
        raise NoteRefused(too_long)
    if contains_card_number(text) or contains_bank_routing_pattern(text):
        raise NoteRefused(payment)
    return text


def clean_body(raw: object) -> str:
    """The body as stored (invisible characters and the outer spaces dropped); raises
    :class:`NoteRefused` when it is empty, too long or holds payment data."""
    return _text(
        raw,
        required=BODY_REQUIRED,
        too_long=BODY_TOO_LONG,
        limit=NOTE_BODY_MAX_LENGTH,
        payment=PAYMENT_DATA,
    )


def clean_reason(raw: object) -> str:
    return _text(
        raw,
        required=REASON_REQUIRED,
        too_long=REASON_TOO_LONG,
        limit=NOTE_DELETE_REASON_MAX_LENGTH,
        payment=REASON_PAYMENT_DATA,
    )


def can_change(note: Note, actor: User | None) -> bool:
    """Whether ``actor`` may edit or delete ``note``: its author, or an admin."""
    if actor is None:
        return False
    return note.author_id == actor.pk or meets(role_of(actor), Role.ADMIN)


def _audit(actor: User | None, action: str, note: Note, before: Any, after: Any) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_NOTE,
        entity_id=str(note.pk),
        before=before,
        after=after,
    )


def _locked(note: Note) -> Note:
    row = Note.objects.select_for_update().get(pk=note.pk)
    if row.deleted_at is not None:
        raise NoteRefused(GONE)
    return row


def add(store: Store, body: object, author: User) -> Note:
    """A new note about ``store`` by ``author``, on the store's current lease."""
    text = clean_body(body)
    lease = lease_end.current_lease(store)
    with transaction.atomic():
        note = Note.objects.create(
            entity_type=NoteEntityType.STORE,
            entity_id=store.pk,
            lease=lease,
            body=text,
            author=author,
            author_label=staff_name(author),
        )
        _audit(
            author,
            "note.created",
            note,
            None,
            {
                "store": str(store.pk),
                "lease": str(lease.pk) if lease is not None else None,
                "body": text,
            },
        )
    return note


def edit(note: Note, body: object, actor: User | None) -> Note:
    """The note with its new body (``edited_at`` now); its author or an admin only."""
    text = clean_body(body)
    with transaction.atomic():
        row = _locked(note)
        if not can_change(row, actor):
            raise NotAllowed(NOT_AUTHOR)
        if row.body == text:
            return row
        before = row.body
        row.body = text
        row.edited_at = timezone.now()
        row.save(update_fields=["body", "edited_at", "updated_at"])
        _audit(actor, "note.edited", row, {"body": before}, {"body": text})
    return row


def _set_pin(note: Note, actor: User | None, pinned: bool) -> Note:
    if actor is None or not meets(role_of(actor), Role.BILLING):
        raise NotAllowed(NOT_BILLING)
    with transaction.atomic():
        row = _locked(note)
        if row.pinned == pinned:
            return row
        before = {"pinned": row.pinned}
        row.pinned = pinned
        row.pinned_at = timezone.now() if pinned else None
        row.save(update_fields=["pinned", "pinned_at", "updated_at"])
        _audit(actor, "note.pinned" if pinned else "note.unpinned", row, before, {"pinned": pinned})
    return row


def pin(note: Note, actor: User | None) -> Note:
    return _set_pin(note, actor, True)


def unpin(note: Note, actor: User | None) -> Note:
    return _set_pin(note, actor, False)


def delete(note: Note, reason: object, actor: User | None) -> Note:
    """Soft delete: the row stays with the reason; the audit row keeps the body."""
    text = clean_reason(reason)
    with transaction.atomic():
        row = _locked(note)
        if actor is None or not can_change(row, actor):
            raise NotAllowed(NOT_AUTHOR)
        row.deleted_at = timezone.now()
        row.deleted_by = actor
        row.delete_reason = text
        row.save(update_fields=["deleted_at", "deleted_by", "delete_reason", "updated_at"])
        _audit(actor, "note.deleted", row, {"body": row.body}, {"delete_reason": text})
    return row


def _live(store: Store) -> QuerySet[Note]:
    return Note.objects.filter(
        entity_type=NoteEntityType.STORE, entity_id=store.pk, deleted_at__isnull=True
    ).select_related("author")


def for_store(store: Store, limit: int | None = None) -> list[Note]:
    """The store's live notes, newest first (one query), at most ``limit``."""
    found = _live(store).order_by("-created_at", "-id")
    return list(found if limit is None else found[:limit])


def pinned_for(store: Store, limit: int = PINNED_SHOWN) -> list[Note]:
    """The store's pinned live notes, the latest pinned first, at most ``limit``."""
    return list(_live(store).filter(pinned=True).order_by("-pinned_at", "-id")[:limit])
