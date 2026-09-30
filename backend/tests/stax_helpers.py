"""Shared test doubles for the Stax client tests (PLAN-005 §6).

Nothing here opens a socket: every transport is an ``httpx.MockTransport`` whose handler
records each request that reached it. Time is a :class:`FakeClock` whose ``sleep``
advances time instantly and records the requested duration. All times are integer
milliseconds, as in :mod:`payments.stax.clock`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from payments.stax.breaker import CircuitBreaker
from payments.stax.budget import FailureBudget
from payments.stax.clock import Clock
from payments.stax.limiter import RateLimiter
from payments.stax.state import MemoryStateStore, StateStore
from payments.stax.transport import StaxTransport

#: 2026-09-30T00:00:00Z, the wall time at monotonic zero of every fake clock.
WALL_BASE_MS = 1_790_726_400_000


class FakeClock:
    """Monotonic and wall time that move only when told to, or when someone sleeps."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms
        self._lock = threading.Lock()
        self.sleeps: list[int] = []

    def monotonic_ms(self) -> int:
        with self._lock:
            return self._now

    def wall_ms(self) -> int:
        return WALL_BASE_MS + self.monotonic_ms()

    def sleep_ms(self, milliseconds: int) -> None:
        with self._lock:
            self.sleeps.append(milliseconds)
            self._now += milliseconds

    def advance(self, milliseconds: int) -> None:
        with self._lock:
            self._now += milliseconds

    def as_clock(self) -> Clock:
        return Clock(monotonic_ms=self.monotonic_ms, wall_ms=self.wall_ms, sleep_ms=self.sleep_ms)


Reply = httpx.Response | Exception
Handler = Callable[[httpx.Request], Reply]


@dataclass
class Recorder:
    """An ``httpx.MockTransport`` that records every request and when it arrived."""

    replies: Handler
    clock: FakeClock
    requests: list[httpx.Request] = field(default_factory=list)
    arrivals: list[int] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.arrivals.append(self.clock.monotonic_ms())
        reply = self.replies(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def always(status: int, json: object = None) -> Handler:
    def reply(request: httpx.Request) -> Reply:
        return httpx.Response(status, json=json if json is not None else {})

    return reply


def sequence(replies: Iterable[Reply]) -> Handler:
    """Reply with each item in turn; fail the test loudly if more requests arrive."""
    queue = list(replies)

    def reply(request: httpx.Request) -> Reply:
        if not queue:
            raise AssertionError("more requests reached the transport than the test expected")
        return queue.pop(0)

    return reply


@dataclass
class Rig:
    """A transport wired to fakes, plus everything a test needs to observe it."""

    clock: FakeClock
    recorder: Recorder
    transport: StaxTransport
    breaker: CircuitBreaker
    budget: FailureBudget
    limiter: RateLimiter
    alerts: list[tuple[datetime, datetime]]


def make_rig(
    replies: Handler,
    *,
    clock: FakeClock | None = None,
    store: StateStore | None = None,
) -> Rig:
    clock = clock or FakeClock()
    store = store if store is not None else MemoryStateStore()
    alerts: list[tuple[datetime, datetime]] = []
    breaker = CircuitBreaker(
        store, clock.as_clock(), on_open=lambda opened, reopen: alerts.append((opened, reopen))
    )
    budget = FailureBudget(store, clock.as_clock())
    limiter = RateLimiter(clock.as_clock())
    recorder = Recorder(replies, clock)
    transport = StaxTransport(
        http_transport=recorder.transport(),
        limiter=limiter,
        breaker=breaker,
        budget=budget,
        clock=clock.as_clock(),
    )
    return Rig(clock, recorder, transport, breaker, budget, limiter, alerts)
