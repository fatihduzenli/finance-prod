"""Apply pending migrations when the web image starts (docs/qa/PLAN-003a §3).

Two replicas may start together. On PostgreSQL the command therefore holds a session
advisory lock while it migrates, so the second replica waits and then finds nothing
left to apply. Other vendors (sqlite, local and tests) migrate without a lock.

Only ``migrate`` runs, non-interactively. Nothing here creates, flushes or deletes
anything else. A failed migration propagates, so the process exits non-zero and the
image does not start gunicorn.
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

#: Fixed key for the migration lock: the bytes of "FINM" as a 32-bit integer. Kept
#: below 2**31 so it reads back from pg_locks as classid 0, objid LOCK_KEY.
DEPLOY_MIGRATE_LOCK_KEY = 0x46494E4D

#: The longest a replica waits for another replica's migration to finish.
DEPLOY_MIGRATE_LOCK_TIMEOUT_SECONDS = 90

#: Pause between two attempts to take the lock.
LOCK_POLL_INTERVAL_SECONDS = 1.0

_TRY_LOCK_SQL = "SELECT pg_try_advisory_lock(%s)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s)"


class Command(BaseCommand):
    help = "Apply pending migrations; on PostgreSQL, one replica at a time."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write("deploy_migrate: applying pending migrations")
        if connection.vendor == "postgresql":
            _acquire_lock()
            try:
                _migrate()
            finally:
                _release_lock()
        else:
            _migrate()
        self.stdout.write("deploy_migrate: migrations are up to date")


def _migrate() -> None:
    call_command("migrate", interactive=False, verbosity=0)


def _acquire_lock() -> None:
    """Take the session advisory lock, or raise CommandError after the timeout."""
    deadline = time.monotonic() + DEPLOY_MIGRATE_LOCK_TIMEOUT_SECONDS
    while True:
        with connection.cursor() as cursor:
            cursor.execute(_TRY_LOCK_SQL, [DEPLOY_MIGRATE_LOCK_KEY])
            row = cursor.fetchone()
        if row is not None and row[0] is True:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # No connection detail: the message may reach a shared log.
            raise CommandError(
                "Timed out after "
                f"{DEPLOY_MIGRATE_LOCK_TIMEOUT_SECONDS} s waiting for the migration lock; "
                "another deployment is still migrating."
            )
        time.sleep(min(LOCK_POLL_INTERVAL_SECONDS, remaining))


def _release_lock() -> None:
    with connection.cursor() as cursor:
        cursor.execute(_UNLOCK_SQL, [DEPLOY_MIGRATE_LOCK_KEY])
