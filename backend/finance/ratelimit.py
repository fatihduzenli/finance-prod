"""A small per-key request limit, kept in the process (docs/qa/PLAN-011b D8).

The calendar feed answers without a session (its token is the credential), so it is
limited per client address: at most ``limit`` requests in any ``window`` seconds. No
database and no cache: a sliding log per key in memory, like the payment provider's
limiter, behind a lock. Each web replica keeps its own log, so with N replicas a client
gets at most N times the limit; that is enough to stop a
guessing loop (a token has 256 bits) and to keep a misbehaving calendar client from
filling the audit log. The number of keys kept is bounded: when it reaches
``max_keys``, the keys whose log is empty after expiry are dropped, and if none is, the
whole map is cleared (a burst of distinct addresses resets the limit, never memory).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window: float,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._log: dict[str, deque[float]] = {}

    def _expire(self, log: deque[float], now: float) -> None:
        while log and log[0] <= now - self.window:
            log.popleft()

    def allow(self, key: str) -> bool:
        """Count one request for ``key``; ``False`` when it is over the limit (not
        counted then)."""
        with self._lock:
            now = self._clock()
            log = self._log.get(key)
            if log is None:
                if len(self._log) >= self.max_keys:
                    self._shrink(now)
                log = self._log[key] = deque()
            self._expire(log, now)
            if len(log) >= self.limit:
                return False
            log.append(now)
            return True

    def _shrink(self, now: float) -> None:
        for key in list(self._log):
            self._expire(self._log[key], now)
            if not self._log[key]:
                del self._log[key]
        if len(self._log) >= self.max_keys:
            self._log.clear()

    def reset(self) -> None:
        with self._lock:
            self._log.clear()
