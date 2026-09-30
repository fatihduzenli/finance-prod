"""Circuit breaker in front of every Stax request (docs/CLAUDE.md Rule 3, PLAN-005 §4.2).

Stax blocks the source IP for an hour after 10 failed requests in a minute
(TODO-VERIFY-001). This breaker opens well before that:

* after 3 consecutive 4xx responses (429 excepted: it is the rate limiter's signal and
  is retried under D6), because a run of rejections means our requests are malformed
  and every further one is a wasted failure; or
* after 5 failures of any kind (4xx, 429, 5xx, network) within 60 seconds.

Open means every call raises :class:`CircuitOpen` without touching the network, for
300 seconds. Then one trial call is let through (half-open): success closes the
breaker, failure re-opens it for another 300 seconds. Each opening persists the state
to ``sync_state`` (D9) and then calls the alert hook exactly once (D10).
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from payments import alerts
from payments.errors import CircuitOpen
from payments.stax.clock import SYSTEM_CLOCK, Clock
from payments.stax.state import BREAKER_ROW, StateStore, SyncStateStore

CONSECUTIVE_4XX_LIMIT = 3
FAILURE_LIMIT = 5
FAILURE_WINDOW_MS = 60_000
OPEN_MS = 300_000


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Outcome(StrEnum):
    """What one request attempt produced, as the breaker counts it."""

    SUCCESS = "success"
    CLIENT_ERROR = "client_error"  # 4xx other than 429
    THROTTLED = "throttled"  # 429
    SERVER_ERROR = "server_error"  # 5xx, and any other unexpected status
    NETWORK_ERROR = "network_error"


AlertHook = Callable[[datetime, datetime], None]


class CircuitBreaker:
    def __init__(
        self,
        store: StateStore,
        clock: Clock = SYSTEM_CLOCK,
        on_open: AlertHook = alerts.breaker_opened,
    ) -> None:
        self.clock = clock
        self.store = store
        self.on_open = on_open
        self._lock = threading.Lock()
        self.state = State.CLOSED
        self.open_until = 0
        self._open_until_wall: str | None = None
        self.consecutive_4xx = 0
        #: (monotonic, wall ISO) per failure; the wall time is fixed when it happens so
        #: the persisted text does not drift between the two clocks.
        self._failures: deque[tuple[int, str]] = deque()
        self._trial_in_flight = False
        self._saved = ""
        self._load()

    # --- Gate ----------------------------------------------------------------------

    def before_call(self) -> None:
        """Raise :class:`CircuitOpen` unless a request may be sent now."""
        with self._lock:
            now = self.clock.monotonic_ms()
            if self.state is State.OPEN:
                if now < self.open_until:
                    raise CircuitOpen("The payment provider circuit is open")
                self.state = State.HALF_OPEN
                self._trial_in_flight = False
                self._save()
            if self.state is State.HALF_OPEN:
                if self._trial_in_flight:
                    raise CircuitOpen("The payment provider circuit is half-open; trial in flight")
                self._trial_in_flight = True

    def abandon(self) -> None:
        """The call passed the gate but never reached the network; free the trial slot."""
        with self._lock:
            self._trial_in_flight = False

    # --- Outcomes ------------------------------------------------------------------

    def record(self, outcome: Outcome) -> None:
        with self._lock:
            now = self.clock.monotonic_ms()
            if outcome is Outcome.SUCCESS:
                self.consecutive_4xx = 0
                if self.state is State.HALF_OPEN:
                    self.state = State.CLOSED
                    self._failures.clear()
                    self._trial_in_flight = False
                self._save()
                return

            self._failures.append((now, self.clock.to_wall(now).isoformat()))
            self._prune(now)
            if outcome is Outcome.CLIENT_ERROR:
                self.consecutive_4xx += 1
            else:
                self.consecutive_4xx = 0

            if self.state is State.HALF_OPEN:
                self._open(now)
            elif self.state is State.CLOSED and (
                self.consecutive_4xx >= CONSECUTIVE_4XX_LIMIT
                or len(self._failures) >= FAILURE_LIMIT
            ):
                self._open(now)
            else:
                # Already open (a call that was in flight when it opened): count only.
                self._save()

    def _prune(self, now: int) -> None:
        while self._failures and self._failures[0][0] <= now - FAILURE_WINDOW_MS:
            self._failures.popleft()

    def _open(self, now: int) -> None:
        self.state = State.OPEN
        self.open_until = now + OPEN_MS
        opened_at = self.clock.to_wall(now)
        reopen_at = self.clock.to_wall(self.open_until)
        self._open_until_wall = reopen_at.isoformat()
        self._trial_in_flight = False
        self.consecutive_4xx = 0
        self._failures.clear()
        self._save()
        self.on_open(opened_at, reopen_at)

    # --- Persistence (D9) ------------------------------------------------------------

    def _save(self) -> None:
        text = json.dumps(
            {
                "state": self.state.value,
                "open_until": self._open_until_wall if self.state is State.OPEN else None,
                "consecutive_4xx": self.consecutive_4xx,
                "failures": [wall for _, wall in self._failures],
            },
            sort_keys=True,
        )
        if text != self._saved:
            self.store.save(BREAKER_ROW, text)
            self._saved = text

    def _load(self) -> None:
        text = self.store.load(BREAKER_ROW)
        if text is None:
            return
        data = json.loads(text)
        self.state = State(data["state"])
        if self.state is State.HALF_OPEN:
            # A trial was in flight when the process stopped; allow a new one.
            self._trial_in_flight = False
        if data.get("open_until"):
            self._open_until_wall = str(data["open_until"])
            self.open_until = self.clock.from_wall(datetime.fromisoformat(self._open_until_wall))
        self.consecutive_4xx = int(data.get("consecutive_4xx", 0))
        self._failures = deque(
            (self.clock.from_wall(datetime.fromisoformat(wall)), str(wall))
            for wall in data.get("failures", [])
        )
        self._prune(self.clock.monotonic_ms())
        self._saved = text


_breaker: CircuitBreaker | None = None
_singleton_lock = threading.Lock()


def get_breaker() -> CircuitBreaker:
    """The process-wide breaker, loaded from ``sync_state`` on first use."""
    global _breaker
    with _singleton_lock:
        if _breaker is None:
            _breaker = CircuitBreaker(SyncStateStore())
        return _breaker


def reset_breaker(breaker: CircuitBreaker | None = None) -> None:
    """Replace (or forget, so the next use reloads) the process-wide breaker. Tests only."""
    global _breaker
    with _singleton_lock:
        _breaker = breaker
