"""Rate limiter (PLAN-005 §6 row c): 2 per second and 100 per minute, whatever the load.

The assertions are on departure timestamps: the time each call was released by the
limiter, or the time each request reached the mock transport.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Iterator

import pytest
from django.test import override_settings

from payments.errors import RateLimitWaitExceeded
from payments.stax.clock import SYSTEM_CLOCK
from payments.stax.limiter import (
    MINUTE_MS,
    PER_MINUTE,
    PER_SECOND,
    SECOND_MS,
    RateLimiter,
    get_limiter,
    reset_limiter,
)
from payments.stax.transport import Method
from tests.stax_helpers import FakeClock, always, make_rig


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


def _max_in_window(departures: list[int], width: int) -> int:
    """Largest number of departures inside any half-open window ``[t, t + width)``."""
    ordered = sorted(departures)
    return max(sum(1 for other in ordered if start <= other < start + width) for start in ordered)


def _max_per_calendar_unit(departures: list[int], width: int) -> int:
    return max(Counter(at // width for at in departures).values())


def test_fifty_calls_from_eight_threads_never_exceed_two_per_second() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock.as_clock())
    departures: list[int] = []
    record_lock = threading.Lock()
    calls_per_thread = [7, 7, 6, 6, 6, 6, 6, 6]
    assert sum(calls_per_thread) == 50
    errors: list[BaseException] = []

    def worker(calls: int) -> None:
        try:
            for _ in range(calls):
                departed = limiter.acquire()
                with record_lock:
                    departures.append(departed)
        except BaseException as error:  # surfaced below; a thread cannot fail the test
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(calls,)) for calls in calls_per_thread]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert len(departures) == 50
    assert _max_in_window(departures, SECOND_MS) <= PER_SECOND
    assert _max_per_calendar_unit(departures, SECOND_MS) <= PER_SECOND
    assert _max_in_window(departures, MINUTE_MS) <= PER_MINUTE
    # 50 departures at 2 per second cannot finish before 24 s: the limiter really waited.
    assert max(departures) >= 24_000
    assert sum(clock.sleeps) >= 24_000


def test_the_minute_bucket_holds_under_sustained_load() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock.as_clock())
    departures = [limiter.acquire(max_wait_ms=120_000) for _ in range(250)]
    assert _max_in_window(departures, MINUTE_MS) == PER_MINUTE
    assert _max_in_window(departures, SECOND_MS) == PER_SECOND
    # 100 in the first 50 s, then nothing until the first token returns at 60 s.
    assert departures[99] < 50_000
    assert departures[100] == 60_000


def test_departures_at_the_transport_are_spaced() -> None:
    rig = make_rig(always(200, {"id": "x"}))
    for _ in range(6):
        rig.transport.send(Method.GET, "/self", idempotent=True)
    assert len(rig.recorder.requests) == 6
    assert rig.recorder.arrivals == [0, 0, 1_000, 1_000, 2_000, 2_000]
    assert _max_per_calendar_unit(rig.recorder.arrivals, SECOND_MS) == PER_SECOND


def test_six_calls_with_a_real_clock_take_at_least_two_seconds() -> None:
    limiter = RateLimiter(SYSTEM_CLOCK)
    started = time.monotonic()
    for _ in range(6):
        limiter.acquire()
    assert time.monotonic() - started >= 2.0


def test_a_wait_beyond_max_wait_raises_without_sleeping() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock.as_clock())
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(RateLimitWaitExceeded):
        limiter.acquire(max_wait_ms=500)
    assert clock.sleeps == []
    # A wait within the limit is served.
    assert limiter.acquire(max_wait_ms=1_000) == 1_000
    assert clock.sleeps == [1_000]


def test_a_rate_limit_wait_sends_nothing() -> None:
    rig = make_rig(always(200, {"id": "x"}))
    rig.transport.max_wait_ms = 0
    rig.transport.send(Method.GET, "/self", idempotent=True)
    rig.transport.send(Method.GET, "/self", idempotent=True)
    with pytest.raises(RateLimitWaitExceeded):
        rig.transport.send(Method.GET, "/self", idempotent=True)
    assert len(rig.recorder.requests) == 2


def test_the_limiter_is_one_per_process_and_resettable() -> None:
    first = get_limiter()
    assert get_limiter() is first
    clock = FakeClock()
    replaced = reset_limiter(clock.as_clock())
    try:
        assert get_limiter() is replaced is not first
        assert replaced.clock.monotonic_ms() == 0
    finally:
        reset_limiter()
