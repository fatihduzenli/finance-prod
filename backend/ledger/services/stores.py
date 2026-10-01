"""Stores created and edited by staff (docs/qa/PLAN-007b-stores.md §1).

Views call the functions below; nothing here talks to the payment provider. Every
change writes one ``audit_log`` row (``store.created``, ``store.updated``).

A store has no column for the one-time key its create form carries, so the key is
recorded in the ``store.created`` audit row, as for leases. Store creation has no parent
row to lock while that row is looked up, so it takes a lock on one ``sync_state`` row
(:data:`CREATION_LOCK`) for the length of the transaction: two submissions of one form
make one store.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction

from ledger.models import AuditLog, Store
from payments.models import SyncState

ENTITY_STORE = "store"
#: The ``sync_state`` row locked while a store is created.
CREATION_LOCK = "store_creation_lock"
#: The store fields staff edit, in form order.
STORE_FIELDS: tuple[str, ...] = (
    "store_number",
    "external_label",
    "city",
    "state",
    "open_date",
    "closed_at",
    "status",
)


def store_for_creation_key(raw: object) -> Store | None:
    """The store a create form with this key already made, if any."""
    try:
        key = uuid.UUID(str(raw))
    except ValueError:
        return None
    return _by_creation_key(key)


def create_store(
    fields: Mapping[str, Any], *, creation_key: uuid.UUID, actor: User | None
) -> tuple[Store, bool]:
    """Save a new store, or return the store this key already made.

    Returns ``(store, created)``. A replayed key never changes the existing row,
    whatever fields came with it.
    """
    with transaction.atomic():
        SyncState.objects.get_or_create(job_name=CREATION_LOCK)
        SyncState.objects.select_for_update().get(job_name=CREATION_LOCK)
        existing = _by_creation_key(creation_key)
        if existing is not None:
            return existing, False
        store = Store(**_editable(fields))
        store.save()
        _audit(
            actor,
            "store.created",
            store,
            None,
            {**_snapshot(store), "creation_key": str(creation_key)},
        )
    return store, True


def update_store(store_id: uuid.UUID, fields: Mapping[str, Any], *, actor: User | None) -> Store:
    """Apply staff edits under a row lock; an edit that changes nothing writes no audit row."""
    with transaction.atomic():
        store = Store.objects.select_for_update().get(pk=store_id)
        before = _snapshot(store)
        for name, value in _editable(fields).items():
            setattr(store, name, value)
        store.save()
        after = _snapshot(store)
        if after != before:
            _audit(actor, "store.updated", store, before, after)
    return store


def _by_creation_key(creation_key: uuid.UUID) -> Store | None:
    entry = AuditLog.objects.filter(
        action="store.created",
        entity_type=ENTITY_STORE,
        after__creation_key=str(creation_key),
    ).first()
    if entry is None:
        return None
    return Store.objects.filter(pk=entry.entity_id).first()


def _editable(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: fields[name] for name in STORE_FIELDS if name in fields}


def _snapshot(store: Store) -> dict[str, Any]:
    return {
        "store_number": store.store_number,
        "external_label": store.external_label,
        "city": store.city,
        "state": store.state,
        "open_date": store.open_date.isoformat() if store.open_date else None,
        "closed_at": store.closed_at.isoformat() if store.closed_at else None,
        "status": store.status,
        "needs_open_date": store.needs_open_date,
    }


def _audit(
    actor: User | None,
    action: str,
    store: Store,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_STORE,
        entity_id=str(store.pk),
        before=before,
        after=after,
    )
