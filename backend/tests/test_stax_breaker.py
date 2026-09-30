"""Circuit breaker (PLAN-005 §6 row b, and row 15 for the breaker; D9, D10).

Failures are produced by the mock transport through the real transport, so every
count below is of requests that reached (or did not reach) the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from unittest import mock

import httpx
import pytest
from django.test import override_settings

from payments import alerts
from payments.errors import CircuitOpen, ProviderError
from payments.models import SyncState
from payments.stax.adapter import StaxProvider
from payments.stax.breaker import (
    OPEN_MS,
    CircuitBreaker,
    Outcome,
    State,
    get_breaker,
    reset_breaker,
)
from payments.stax.budget import get_budget, reset_budget
from payments.stax.limiter import RateLimiter
from payments.stax.state import BREAKER_ROW, BUDGET_ROW, MemoryStateStore, SyncStateStore
from payments.stax.transport import Method, StaxTransport
from tests.stax_helpers import FakeClock, Handler, Rig, always, make_rig, sequence


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


def _state(breaker: CircuitBreaker) -> State:
    """Read through a call so the type checker does not narrow the state across calls."""
    return breaker.state


def _get(rig: Rig) -> None:
    rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)


def _post(rig: Rig) -> None:
    """Non-idempotent, so one failure is one request (no retry)."""
    rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)


def _fail(rig: Rig, send: Callable[[Rig], None] = _post) -> None:
    with pytest.raises(ProviderError):
        send(rig)


# --- Opening --------------------------------------------------------------------------


def test_opens_after_exactly_three_consecutive_4xx() -> None:
    rig = make_rig(always(400))
    _fail(rig)
    _fail(rig)
    assert _state(rig.breaker) is State.CLOSED
    _fail(rig)
    assert _state(rig.breaker) is State.OPEN
    assert len(rig.alerts) == 1
    with pytest.raises(CircuitOpen):
        _post(rig)
    assert len(rig.recorder.requests) == 3


def test_two_consecutive_4xx_do_not_open() -> None:
    rig = make_rig(
        sequence(
            [
                httpx.Response(404),
                httpx.Response(404),
                httpx.Response(200, json={}),
                httpx.Response(404),
                httpx.Response(404),
                httpx.Response(200, json={}),
            ]
        )
    )
    for _ in range(2):
        _fail(rig)
        _fail(rig)
        _post(rig)
    assert _state(rig.breaker) is State.CLOSED
    assert rig.alerts == []
    assert len(rig.recorder.requests) == 6


MIXED: list[httpx.Response | Exception] = [
    httpx.Response(400),
    httpx.Response(500),
    httpx.Response(422),
    httpx.ConnectError("refused"),
    httpx.Response(503),
]


def test_opens_after_five_mixed_failures_within_sixty_seconds() -> None:
    rig = make_rig(sequence(MIXED))
    for _ in MIXED:
        _fail(rig)
        rig.clock.advance(14_000)  # the fifth failure lands at 56 s
    assert _state(rig.breaker) is State.OPEN
    assert len(rig.alerts) == 1
    with pytest.raises(CircuitOpen):
        _post(rig)
    assert len(rig.recorder.requests) == 5


def test_five_failures_spread_over_sixty_one_seconds_do_not_open() -> None:
    rig = make_rig(sequence([*MIXED, httpx.Response(200, json={})]))
    for index, _ in enumerate(MIXED):
        _fail(rig)
        if index < len(MIXED) - 1:
            rig.clock.advance(61_000 // 4)  # failures at 0, 15.25, 30.5, 45.75 and 61 s
    assert rig.clock.monotonic_ms() == 61_000
    assert _state(rig.breaker) is State.CLOSED
    assert rig.alerts == []
    _post(rig)
    assert len(rig.recorder.requests) == 6


def test_retries_of_one_call_count_toward_the_window() -> None:
    # Four 503s from one idempotent call, then one 400: five failures within 20 s.
    rig = make_rig(sequence([httpx.Response(503)] * 4 + [httpx.Response(400)]))
    _fail(rig, _get)
    assert _state(rig.breaker) is State.CLOSED
    _fail(rig, _get)
    assert _state(rig.breaker) is State.OPEN
    assert len(rig.recorder.requests) == 5


# --- Open, half-open, closed -------------------------------------------------------------


def _opened_rig(later: Handler) -> Rig:
    replies: list[Handler] = [always(400)]

    def reply(request: httpx.Request) -> httpx.Response | Exception:
        return replies[0](request)

    rig = make_rig(reply)
    for _ in range(3):
        _fail(rig)
    assert _state(rig.breaker) is State.OPEN
    # The limiter spaced the three requests; time is now measured from the opening.
    assert rig.breaker.open_until == rig.clock.monotonic_ms() + OPEN_MS
    rig.clock.sleeps.clear()
    replies[0] = later
    return rig


def test_while_open_no_call_reaches_the_network_for_300_seconds() -> None:
    rig = _opened_rig(always(200, {}))
    for step in (0, 1_000, 149_000, 149_999):  # up to 299.999 s after opening
        rig.clock.advance(step)
        with pytest.raises(CircuitOpen):
            _get(rig)
        with pytest.raises(CircuitOpen):
            _post(rig)
    assert len(rig.recorder.requests) == 3
    assert rig.clock.sleeps == []


def test_half_open_allows_exactly_one_trial() -> None:
    rig = _opened_rig(always(200, {}))
    rig.clock.advance(OPEN_MS)
    rig.breaker.before_call()  # the trial
    assert _state(rig.breaker) is State.HALF_OPEN
    with pytest.raises(CircuitOpen):
        rig.breaker.before_call()
    rig.breaker.record(Outcome.SUCCESS)
    assert _state(rig.breaker) is State.CLOSED


def test_a_successful_trial_closes_the_breaker() -> None:
    rig = _opened_rig(always(200, {}))
    rig.clock.advance(OPEN_MS)
    _get(rig)
    assert _state(rig.breaker) is State.CLOSED
    _get(rig)
    assert len(rig.recorder.requests) == 5
    assert len(rig.alerts) == 1


def test_a_failed_trial_reopens_for_another_300_seconds_and_alerts_again() -> None:
    rig = _opened_rig(always(500))
    rig.clock.advance(OPEN_MS)
    _fail(rig, _get)
    # The trial failed once: no retry reached the network, the breaker re-opened.
    assert len(rig.recorder.requests) == 4
    assert _state(rig.breaker) is State.OPEN
    assert len(rig.alerts) == 2
    # Re-opened at the failed trial: refused until 300 s after that moment.
    rig.clock.advance(rig.breaker.open_until - rig.clock.monotonic_ms() - 1)
    with pytest.raises(CircuitOpen):
        _get(rig)
    assert len(rig.recorder.requests) == 4


def test_the_alert_fires_once_per_opening_even_if_failures_keep_arriving() -> None:
    rig = _opened_rig(always(200, {}))
    for _ in range(10):
        rig.breaker.record(Outcome.CLIENT_ERROR)
    assert _state(rig.breaker) is State.OPEN
    assert len(rig.alerts) == 1
    opened_at, reopen_at = rig.alerts[0]
    assert (reopen_at - opened_at).total_seconds() * 1000 == OPEN_MS


def test_the_default_alert_logs_critical_and_sends_a_fatal_sentry_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(MemoryStateStore(), clock.as_clock())
    with mock.patch("sentry_sdk.capture_message") as capture:
        for _ in range(3):
            breaker.record(Outcome.CLIENT_ERROR)
    assert capture.call_count == 1
    assert capture.call_args.kwargs == {"level": "fatal", "fingerprint": ["stax-breaker-open"]}
    critical = [record for record in caplog.records if record.levelname == "CRITICAL"]
    assert len(critical) == 1
    assert alerts.BREAKER_MESSAGE in critical[0].getMessage()


# --- Row 15: state survives a restart ---------------------------------------------------


@pytest.fixture
def _fresh_singletons() -> Iterator[None]:
    reset_breaker()
    reset_budget()
    yield
    reset_breaker()
    reset_budget()


@pytest.mark.django_db
def test_breaker_state_is_read_back_from_sync_state() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(SyncStateStore(), clock.as_clock(), on_open=lambda a, b: None)
    for _ in range(3):
        breaker.record(Outcome.CLIENT_ERROR)
    row = SyncState.objects.get(job_name=BREAKER_ROW)
    assert json.loads(row.cursor)["state"] == "open"

    clock.advance(100_000)
    restarted = CircuitBreaker(SyncStateStore(), clock.as_clock(), on_open=lambda a, b: None)
    assert _state(restarted) is State.OPEN
    with pytest.raises(CircuitOpen):
        restarted.before_call()
    clock.advance(OPEN_MS - 100_000)
    restarted.before_call()  # half-open trial allowed exactly when the 300 s are over
    assert _state(restarted) is State.HALF_OPEN


@pytest.mark.django_db
@pytest.mark.usefixtures("_fresh_singletons")
def test_breaker_and_budget_survive_a_new_adapter_instance() -> None:
    handler = always(401)
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        reply = handler(request)
        assert isinstance(reply, httpx.Response)
        return reply

    def new_adapter() -> StaxProvider:
        # Breaker and budget are the process-wide ones, backed by sync_state.
        return StaxProvider(
            StaxTransport(
                http_transport=httpx.MockTransport(record),
                limiter=RateLimiter(FakeClock().as_clock()),
            )
        )

    first = new_adapter()
    with mock.patch("sentry_sdk.capture_message"):
        for _ in range(3):
            with pytest.raises(ProviderError):
                first.get_invoice("inv-1")
    assert _state(get_breaker()) is State.OPEN
    assert get_budget().counts() == (3, 3)

    # A restart: the process forgets everything it held in memory.
    reset_breaker()
    reset_budget()
    second = new_adapter()
    with pytest.raises(CircuitOpen):
        second.get_invoice("inv-1")
    assert len(requests) == 3
    assert get_budget().counts() == (3, 3)
    assert SyncState.objects.filter(job_name__in=[BREAKER_ROW, BUDGET_ROW]).count() == 2
