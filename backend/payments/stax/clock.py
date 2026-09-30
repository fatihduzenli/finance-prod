"""Injectable time for the limiter, breaker, budget and transport.

All time here is an integer number of milliseconds. Integers keep every comparison
exact (a token spent at t returns at exactly t + 1000), and they keep binary floating
point out of the ``payments`` package entirely, which a schema test enforces.

Decisions use the monotonic clock, which never jumps. Persisted state (``sync_state``)
must survive a restart, where monotonic readings mean nothing, so it is stored as wall
time and converted on the way in and out. Tests pass a fake clock whose ``sleep_ms``
advances time instantly, so nothing in the suite waits for real.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MS_PER_SECOND = 1000
_NS_PER_MS = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // _NS_PER_MS


def _wall_ms() -> int:
    return time.time_ns() // _NS_PER_MS


def _sleep_ms(milliseconds: int) -> None:
    time.sleep(milliseconds / MS_PER_SECOND)


@dataclass(frozen=True, slots=True)
class Clock:
    monotonic_ms: Callable[[], int] = _monotonic_ms
    wall_ms: Callable[[], int] = _wall_ms
    sleep_ms: Callable[[int], None] = _sleep_ms

    def to_wall(self, monotonic_at: int) -> datetime:
        """The wall-clock moment of a monotonic reading."""
        offset = self.wall_ms() - (self.monotonic_ms() - monotonic_at)
        return _EPOCH + timedelta(milliseconds=offset)

    def from_wall(self, wall_at: datetime) -> int:
        """The monotonic reading that corresponds to a wall-clock moment."""
        wall_at_ms = (wall_at - _EPOCH) // timedelta(milliseconds=1)
        return self.monotonic_ms() - (self.wall_ms() - wall_at_ms)


SYSTEM_CLOCK = Clock()
