"""Retry policy (PLAN-005 §6 rows a, 4, 5 and 14; D5, D6).

Every assertion counts what actually reached the mock transport and what the transport
asked the clock to sleep.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest
from django.test import override_settings

from payments.errors import (
    ProviderNotPermittedHere,
    ProviderRejected,
    ProviderUnavailable,
    TransportError,
)
from payments.provider import Amounts, CustomerInput, InvoiceInput, LineItem, ScheduleInput
from payments.stax.adapter import StaxProvider
from payments.stax.transport import (
    BACKOFF_MS,
    TIMEOUT_SECONDS,
    Method,
    StaxTransport,
    build_client,
)
from tests.stax_helpers import always, make_rig, sequence

FOUR_XX = [400, 401, 403, 404, 409, 422]
RETRIED_STATUSES = [429, 500, 502, 503, 504]
NETWORK_ERRORS = [httpx.ConnectError("refused"), httpx.ReadTimeout("stalled")]


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


def _amounts() -> Amounts:
    return Amounts(
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        tax=Decimal("0.00"),
        line_items=(LineItem("Rent", Decimal("1"), Decimal("100.00")),),
    )


# --- Row a: 4xx is never retried ----------------------------------------------------


@pytest.mark.parametrize("status", FOUR_XX)
def test_a_4xx_is_sent_once_and_never_retried(status: int) -> None:
    rig = make_rig(always(status))
    with pytest.raises(ProviderRejected) as raised:
        rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
    assert raised.value.status == status
    assert len(rig.recorder.requests) == 1
    assert rig.clock.sleeps == []


@pytest.mark.parametrize("status", FOUR_XX)
def test_a_4xx_on_an_adapter_write_is_sent_once(status: int) -> None:
    rig = make_rig(always(status))
    provider = StaxProvider(rig.transport)
    with pytest.raises(ProviderRejected):
        provider.refund_transaction("txn-1", Decimal("5.00"))
    with pytest.raises(ProviderRejected):
        provider.void_transaction("txn-1")
    assert len(rig.recorder.requests) == 2
    assert rig.clock.sleeps == []


# --- Row 4: 429, 5xx and network errors on idempotent calls ------------------------------


@pytest.mark.parametrize("status", RETRIED_STATUSES)
def test_retryable_status_is_tried_four_times_with_1_4_15_backoff(status: int) -> None:
    rig = make_rig(always(status))
    with pytest.raises(ProviderUnavailable) as raised:
        rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
    assert raised.value.status == status
    assert len(rig.recorder.requests) == 4
    assert rig.clock.sleeps == [1_000, 4_000, 15_000] == list(BACKOFF_MS)


@pytest.mark.parametrize("error", NETWORK_ERRORS, ids=lambda error: type(error).__name__)
def test_network_error_is_retried_the_same_way(error: Exception) -> None:
    rig = make_rig(lambda request: error)
    with pytest.raises(TransportError):
        rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
    assert len(rig.recorder.requests) == 4
    assert rig.clock.sleeps == [1_000, 4_000, 15_000]


def test_transport_error_is_a_provider_unavailable() -> None:
    assert issubclass(TransportError, ProviderUnavailable)


def test_recovery_on_the_third_attempt_returns_the_body() -> None:
    rig = make_rig(
        sequence(
            [
                httpx.Response(503),
                httpx.ConnectError("refused"),
                httpx.Response(200, json={"id": "inv-1"}),
            ]
        )
    )
    data = rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
    assert data == {"id": "inv-1"}
    assert len(rig.recorder.requests) == 3
    assert rig.clock.sleeps == [1_000, 4_000]


# --- Row 5: the three non-idempotent creates -------------------------------------------


def _create_customer(provider: StaxProvider) -> object:
    return provider.create_customer(
        CustomerInput("Ada", "Lovelace", "Example LLC", "ada@example.com")
    )


def _create_invoice(provider: StaxProvider) -> object:
    return provider.create_invoice(InvoiceInput("cus-1", _amounts()))


def _create_schedule(provider: StaxProvider) -> object:
    return provider.create_scheduled_invoice(
        ScheduleInput("cus-1", "pm-1", _amounts(), "DTSTART=20261001T000000Z;FREQ=MONTHLY")
    )


Create = Callable[[StaxProvider], object]
CREATES: dict[str, Create] = {
    "POST /customer": _create_customer,
    "POST /invoice": _create_invoice,
    "POST /invoice/schedule/": _create_schedule,
}


@pytest.mark.parametrize("create", CREATES.values(), ids=CREATES.keys())
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_create_is_not_retried_after_a_5xx(create: Create, status: int) -> None:
    rig = make_rig(always(status))
    with pytest.raises(ProviderUnavailable):
        create(StaxProvider(rig.transport))
    assert len(rig.recorder.requests) == 1
    assert rig.clock.sleeps == []


@pytest.mark.parametrize("create", CREATES.values(), ids=CREATES.keys())
@pytest.mark.parametrize("error", NETWORK_ERRORS, ids=lambda error: type(error).__name__)
def test_create_is_not_retried_after_a_network_error(create: Create, error: Exception) -> None:
    rig = make_rig(lambda request: error)
    with pytest.raises(TransportError):
        create(StaxProvider(rig.transport))
    assert len(rig.recorder.requests) == 1
    assert rig.clock.sleeps == []


@pytest.mark.parametrize("create", CREATES.values(), ids=CREATES.keys())
def test_create_is_retried_after_429(create: Create) -> None:
    rig = make_rig(
        sequence([httpx.Response(429), httpx.Response(429), httpx.Response(200, json={"id": "x"})])
    )
    create(StaxProvider(rig.transport))
    assert len(rig.recorder.requests) == 3
    assert rig.clock.sleeps == [1_000, 4_000]
    assert {request.method for request in rig.recorder.requests} == {"POST"}


@pytest.mark.parametrize("create", CREATES.values(), ids=CREATES.keys())
def test_create_gives_up_after_four_429s(create: Create) -> None:
    rig = make_rig(always(429))
    with pytest.raises(ProviderUnavailable):
        create(StaxProvider(rig.transport))
    assert len(rig.recorder.requests) == 4
    assert rig.clock.sleeps == [1_000, 4_000, 15_000]


# --- Row 14: no retry or redirect hidden in httpx --------------------------------------


def test_the_http_client_has_no_retries_and_no_redirects() -> None:
    client = build_client("https://apiprod.fattlabs.com", "FAKE-KEY-for-inspection")
    try:
        assert client.follow_redirects is False
        transport = client._transport
        assert isinstance(transport, httpx.HTTPTransport)
        assert transport._pool._retries == 0
        timeout = client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (
            TIMEOUT_SECONDS,
        ) * 4
        assert TIMEOUT_SECONDS == 15.0
    finally:
        client.close()


def test_the_default_transport_builds_that_client_from_settings() -> None:
    transport = StaxTransport()
    try:
        client = transport.client
        assert isinstance(client._transport, httpx.HTTPTransport)
        assert client._transport._pool._retries == 0
        assert client.follow_redirects is False
        assert str(client.base_url).rstrip("/") == "https://apiprod.fattlabs.com"
    finally:
        transport.close()


def test_the_client_uses_the_settings_key_when_no_override_is_given() -> None:
    with override_settings(STAX_API_KEY="FAKE-SETTINGS-KEY"):
        transport = StaxTransport()
        try:
            assert transport.client.headers["Authorization"] == "Bearer FAKE-SETTINGS-KEY"
        finally:
            transport.close()


def test_an_api_key_override_replaces_the_settings_key() -> None:
    with override_settings(STAX_API_KEY="FAKE-SETTINGS-KEY"):
        transport = StaxTransport(api_key="FAKE-OVERRIDE-KEY")
        try:
            assert transport.client.headers["Authorization"] == "Bearer FAKE-OVERRIDE-KEY"
        finally:
            transport.close()


def test_an_empty_api_key_override_is_refused_not_replaced_by_settings() -> None:
    with override_settings(STAX_API_KEY="FAKE-SETTINGS-KEY"):
        transport = StaxTransport(api_key="")
        with pytest.raises(ProviderNotPermittedHere):
            _ = transport.client


def test_a_redirect_is_not_followed_and_not_retried() -> None:
    rig = make_rig(always(302))
    with pytest.raises(ProviderUnavailable):
        rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
    assert len(rig.recorder.requests) == 1
