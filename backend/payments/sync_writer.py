"""The only way to write ``invoices.provider_status`` (docs/qa/PLAN-004-schema.md §4.4).

On PostgreSQL a trigger rejects an INSERT with a non-empty ``provider_status``, and an
UPDATE that changes it, unless the transaction has set ``finance.sync_writer = 'on'``.
:func:`provider_write` sets it, transaction-locally, for the duration of its block.

Private to the ``payments`` package: a test fails if any module outside it imports this
one. Staff actions and views never write provider state (docs/CLAUDE.md Rule 2).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import DEFAULT_DB_ALIAS, connections, transaction

SETTING_NAME = "finance.sync_writer"
_SET_SQL = "SELECT set_config(%s, %s, true)"


def _set(using: str, value: str) -> None:
    connection = connections[using]
    if connection.vendor != "postgresql":
        return  # the trigger exists on PostgreSQL only
    with connection.cursor() as cursor:
        cursor.execute(_SET_SQL, [SETTING_NAME, value])


@contextmanager
def provider_write(using: str = DEFAULT_DB_ALIAS) -> Iterator[None]:
    """Open a transaction (or savepoint) in which provider state may be written.

    ``set_config(..., true)`` lasts until the end of the outermost transaction, so the
    setting is switched off again when the block completes; if the block raises, the
    rollback of its transaction or savepoint undoes the setting with everything else.
    """
    with transaction.atomic(using=using):
        _set(using, "on")
        yield
        _set(using, "off")
