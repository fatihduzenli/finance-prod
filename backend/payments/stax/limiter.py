"""Process-wide rate limiter for every Stax request (docs/CLAUDE.md Rule 3, PLAN-005 §4.2).

Two buckets, both of which must have a token before a request leaves: 2 per second
(burst 2) and 100 per minute (burst 100). The minute figure is Stax's published
ceiling on the "API Overview" page and half of the "API Throttling" page's 200
(TODO-VERIFY-001); the second bucket keeps a burst from spending the minute in a
moment.

Each token comes back exactly one period after it was spent. That is the strict form
of a token bucket: unlike continuous refill, it guarantees that no window of one
period ever holds more departures than the bucket's capacity, which is the property
Stax's per-minute counter and the per-second test depend on.

Why process-wide, and why that is enough: the limit Stax enforces is per source IP,
and every replica egresses through the same NAT address. A bucket per ``httpx`` client
or per thread would multiply the budget. One bucket per process is correct only if
one process calls Stax, which D7 guarantees: every process except the single worker
runs with ``STAX_CALLER=none`` and is refused before it reaches this limiter.
"""

from __future__ import annotations

import threading
from collections import deque

from payments.errors import RateLimitWaitExceeded
from payments.stax.clock import SYSTEM_CLOCK, Clock

PER_SECOND = 2
PER_MINUTE = 100
SECOND_MS = 1_000
MINUTE_MS = 60_000
DEFAULT_MAX_WAIT_MS = 30_000


class TokenBucket:
    """``capacity`` tokens; a spent token returns ``period_ms`` after it was spent.

    Not thread-safe on its own; :class:`RateLimiter` holds the lock.
    """

    def __init__(self, capacity: int, period_ms: int) -> None:
        self.capacity = capacity
        self.period_ms = period_ms
        self._spent: deque[int] = deque()

    def _refill(self, now: int) -> None:
        while self._spent and self._spent[0] + self.period_ms <= now:
            self._spent.popleft()

    def wait_ms(self, now: int) -> int:
        """Milliseconds until a token is available (0 when one is available now)."""
        self._refill(now)
        if len(self._spent) < self.capacity:
            return 0
        return self._spent[0] + self.period_ms - now

    def take(self, now: int) -> None:
        self._spent.append(now)


class RateLimiter:
    """The pair of buckets, taken together or not at all."""

    def __init__(self, clock: Clock = SYSTEM_CLOCK) -> None:
        self.clock = clock
        self.buckets = (TokenBucket(PER_SECOND, SECOND_MS), TokenBucket(PER_MINUTE, MINUTE_MS))
        self._lock = threading.Lock()

    def acquire(self, max_wait_ms: int = DEFAULT_MAX_WAIT_MS) -> int:
        """Block until both buckets release a token; return the monotonic departure time.

        Raises :class:`RateLimitWaitExceeded` as soon as it is certain that the wait
        would pass ``max_wait_ms``, including time spent queueing behind other threads.
        Waiters hold the lock while they sleep, so they are served one at a time and
        in the order they obtained it; no token can be taken ahead of a sleeping waiter.
        """
        deadline = self.clock.monotonic_ms() + max_wait_ms
        while True:
            remaining = deadline - self.clock.monotonic_ms()
            if remaining < 0 or not self._lock.acquire(timeout=remaining / SECOND_MS):
                raise RateLimitWaitExceeded("No request slot became free within the allowed wait")
            try:
                now = self.clock.monotonic_ms()
                wait = max(bucket.wait_ms(now) for bucket in self.buckets)
                if wait <= 0:
                    for bucket in self.buckets:
                        bucket.take(now)
                    return now
                if now + wait > deadline:
                    raise RateLimitWaitExceeded(
                        "No request slot became free within the allowed wait"
                    )
                self.clock.sleep_ms(wait)
                now = self.clock.monotonic_ms()
                if max(bucket.wait_ms(now) for bucket in self.buckets) <= 0:
                    for bucket in self.buckets:
                        bucket.take(now)
                    return now
            finally:
                self._lock.release()


_limiter: RateLimiter | None = None
_singleton_lock = threading.Lock()


def get_limiter() -> RateLimiter:
    """The process-wide limiter."""
    global _limiter
    with _singleton_lock:
        if _limiter is None:
            _limiter = RateLimiter()
        return _limiter


def reset_limiter(clock: Clock | None = None) -> RateLimiter:
    """Replace the process-wide limiter (tests only)."""
    global _limiter
    with _singleton_lock:
        _limiter = RateLimiter(clock or SYSTEM_CLOCK)
        return _limiter
