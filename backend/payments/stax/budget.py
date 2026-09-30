"""Failure budget that gates every debit (PLAN-005 D8, TODO-VERIFY-014).

Stax documents two failure limits: 10 failed requests per minute per IP, and 50 failed
transactions per hour per merchant. This budget counts both kinds of failure in one
sliding log: failed HTTP calls (recorded by the transport) and failed payment attempts
reported later, such as a returned ACH debit (recorded by the sync layer through
:meth:`FailureBudget.record_payment_failure`). Debits are refused at half of each
limit: 5 failures in the last 60 seconds or 25 in the last hour.

The log is persisted to ``sync_state`` (D9) so a restart does not forget it.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime

from payments.errors import FailureBudgetExhausted, ValidationFailed
from payments.stax.clock import SYSTEM_CLOCK, Clock
from payments.stax.state import BUDGET_ROW, StateStore, SyncStateStore

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
MINUTE_LIMIT = 5
HOUR_LIMIT = 25


class FailureBudget:
    def __init__(self, store: StateStore, clock: Clock = SYSTEM_CLOCK) -> None:
        self.store = store
        self.clock = clock
        self._lock = threading.Lock()
        #: (monotonic, wall ISO) per failure, oldest first, at most one hour old.
        self._failures: deque[tuple[int, str]] = deque()
        self._load()

    def record_http_failure(self) -> None:
        self._record()

    def record_payment_failure(self, external_id: str) -> None:
        """A payment attempt failed after the fact, e.g. a returned ACH debit."""
        if not external_id or any(character.isspace() for character in external_id):
            raise ValidationFailed("external_id must be a non-empty identifier")
        self._record()

    def check_can_debit(self) -> None:
        """Raise :class:`FailureBudgetExhausted` when a debit must not be attempted."""
        with self._lock:
            now = self.clock.monotonic_ms()
            self._prune(now)
            last_minute = sum(1 for at, _ in self._failures if at > now - MINUTE_MS)
            if last_minute >= MINUTE_LIMIT or len(self._failures) >= HOUR_LIMIT:
                raise FailureBudgetExhausted("Too many recent provider failures to debit now")

    def counts(self) -> tuple[int, int]:
        """(failures in the last minute, failures in the last hour)."""
        with self._lock:
            now = self.clock.monotonic_ms()
            self._prune(now)
            return sum(1 for at, _ in self._failures if at > now - MINUTE_MS), len(self._failures)

    def _record(self) -> None:
        with self._lock:
            now = self.clock.monotonic_ms()
            self._failures.append((now, self.clock.to_wall(now).isoformat()))
            self._prune(now)
            self.store.save(
                BUDGET_ROW, json.dumps({"failures": [wall for _, wall in self._failures]})
            )

    def _prune(self, now: int) -> None:
        while self._failures and self._failures[0][0] <= now - HOUR_MS:
            self._failures.popleft()

    def _load(self) -> None:
        text = self.store.load(BUDGET_ROW)
        if text is None:
            return
        self._failures = deque(
            (self.clock.from_wall(datetime.fromisoformat(wall)), str(wall))
            for wall in json.loads(text).get("failures", [])
        )
        self._prune(self.clock.monotonic_ms())


_budget: FailureBudget | None = None
_singleton_lock = threading.Lock()


def get_budget() -> FailureBudget:
    """The process-wide budget, loaded from ``sync_state`` on first use."""
    global _budget
    with _singleton_lock:
        if _budget is None:
            _budget = FailureBudget(SyncStateStore())
        return _budget


def reset_budget(budget: FailureBudget | None = None) -> None:
    """Replace (or forget, so the next use reloads) the process-wide budget. Tests only."""
    global _budget
    with _singleton_lock:
        _budget = budget
