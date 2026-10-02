"""One reconciliation at a time (docs/qa/PLAN-010-worker-and-reconciliation.md D6).

PostgreSQL: ``pg_try_advisory_lock`` on a fixed 64-bit key, held by this database session
for the whole run and released in ``finally`` (the server also releases it if the
session ends, so a killed run never leaves it held). sqlite (local only) has no advisory
locks: a ``sync_state`` row is taken by a conditional update, and a hold older than
:data:`STALE_AFTER` (longer than any run may last) counts as abandoned.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Q
from django.utils import timezone

from payments.models import SyncState

#: ``int.from_bytes(b"reconcil")``: a fixed key no other lock in this app uses.
ADVISORY_KEY = 0x7265636F6E63696C
#: The sqlite lock row, and the text it holds while taken.
LOCK_ROW = "reconciliation_lock"
HELD = "held"
#: The scheduled job's own time limit (``replicaTimeout``) is one hour.
STALE_AFTER = timedelta(hours=2)


@contextmanager
def held(using: str = DEFAULT_DB_ALIAS) -> Iterator[bool]:
    """``True`` inside the block when this caller holds the lock; ``False`` when another
    run holds it (nothing is waited for)."""
    if connections[using].vendor == "postgresql":
        with _advisory(using) as acquired:
            yield acquired
    else:
        with _row(using) as acquired:
            yield acquired


@contextmanager
def _advisory(using: str) -> Iterator[bool]:
    connection = connections[using]
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_KEY])
        row = cursor.fetchone()
    acquired = bool(row and row[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_KEY])


@contextmanager
def _row(using: str) -> Iterator[bool]:
    now = timezone.now()
    rows = SyncState.objects.using(using)
    rows.get_or_create(job_name=LOCK_ROW)
    # ``last_success_at`` holds the time the lock was taken.
    taken = (
        rows.filter(job_name=LOCK_ROW)
        .filter(~Q(cursor=HELD) | Q(last_success_at__lt=now - STALE_AFTER))
        .update(cursor=HELD, last_success_at=now)
    )
    acquired = taken == 1
    try:
        yield acquired
    finally:
        if acquired:
            rows.filter(job_name=LOCK_ROW, cursor=HELD).update(cursor="")
