"""Retry policy (PLAN-005 §6 rows a, 4, 5 and 14; D5, D6).

Every assertion counts what actually reached the mock transport and what the transport
asked the clock to sleep.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest
from django.test import override_settings

from payments.errors import (
    ProviderError,
    ProviderNotPermittedHere,
    ProviderRejected,
    ProviderUnavailable,
    TransportError,
)
from payments.provider import (
    Amounts,
    CustomerInput,
    InvoiceInput,
    LineItem,
    PayWithStoredMethod,
    ScheduleInput,
)
from payments.stax import transport as transport_module
from payments.stax.adapter import StaxProvider
from payments.stax.transport import (
    BACKOFF_MS,
    TIMEOUT_SECONDS,
    Method,
    StaxTransport,
    build_client,
)
from tests.stax_helpers import Handler, always, make_rig, sequence

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


# --- PLAN-006a §4 rows 1 to 3: 4xx field names and the created ID in the log ------------

VALUE_MARKER = "VALUE-MARKER-5e1d"
CARD = "4111111111111111"


def _stax_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "payments.stax.transport"]


def test_a_422_logs_its_field_names_and_never_a_value(
    caplog: pytest.LogCaptureFixture, fake_stax_api_key: str
) -> None:
    body = {
        "email": [f"bad {VALUE_MARKER}"],
        "lastname": [f"required {VALUE_MARKER} {CARD} {fake_stax_api_key}"],
    }
    rig = make_rig(always(422, body))
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(ProviderRejected) as raised,
    ):
        _create_customer(StaxProvider(rig.transport))

    lines = _stax_lines(caplog)
    assert len(lines) == 1
    assert lines[0].endswith(" object_id=- failure=ProviderRejected error_fields=email,lastname")
    assert raised.value.error_fields == ("email", "lastname")
    assert raised.value.status == 422
    for text in (caplog.text, str(raised.value), repr(raised.value.args)):
        for forbidden in (VALUE_MARKER, CARD, fake_stax_api_key, "bad", "required"):
            assert forbidden not in text


ERROR_SHAPES: dict[str, tuple[httpx.Response, tuple[str, ...]]] = {
    "errors-object": (
        httpx.Response(422, json={"message": VALUE_MARKER, "errors": {"email": [VALUE_MARKER]}}),
        ("email", "errors", "message"),
    ),
    "error-object": (
        httpx.Response(400, json={"error": {"firstname": VALUE_MARKER, "zip": 12345}}),
        ("error", "firstname", "zip"),
    ),
    "errors-list-not-descended": (
        httpx.Response(422, json={"errors": [VALUE_MARKER, {"inner": VALUE_MARKER}]}),
        ("errors",),
    ),
    "list": (httpx.Response(422, json=[VALUE_MARKER, {"email": VALUE_MARKER}]), ("list",)),
    "scalar-string": (httpx.Response(422, json=VALUE_MARKER), ("scalar",)),
    "scalar-number": (httpx.Response(404, json=12345), ("scalar",)),
    "scalar-null": (httpx.Response(404, content=b"null"), ("scalar",)),
    "non-json": (httpx.Response(422, text=f"<html>{VALUE_MARKER}</html>"), ("non-json",)),
    "empty-body": (httpx.Response(404, content=b""), ("non-json",)),
    "invalid-utf8": (httpx.Response(422, content=b"\xff\xfe{\x00"), ("non-json",)),
    "empty-object": (httpx.Response(422, json={}), ()),
    "nested-values-not-read": (
        httpx.Response(
            422,
            json={"email": {"deep": {"card": CARD, "detail": VALUE_MARKER}}},
        ),
        ("email",),
    ),
}


@pytest.mark.parametrize(
    ("reply", "expected"), ERROR_SHAPES.values(), ids=list(ERROR_SHAPES.keys())
)
def test_error_body_shapes_give_key_names_only(
    caplog: pytest.LogCaptureFixture, reply: httpx.Response, expected: tuple[str, ...]
) -> None:
    rig = make_rig(lambda request: reply)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderRejected) as raised:
        rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
    assert raised.value.error_fields == expected
    (line,) = _stax_lines(caplog)
    assert line.endswith(f" error_fields={','.join(expected) or '-'}")
    for text in (caplog.text, str(raised.value)):
        assert VALUE_MARKER not in text
        assert CARD not in text
        assert "deep" not in text and "inner" not in text


def test_more_than_twenty_keys_are_cut_to_twenty_sorted() -> None:
    body = {f"field_{index:02d}": VALUE_MARKER for index in range(25)}
    rig = make_rig(always(422, body))
    with pytest.raises(ProviderRejected) as raised:
        rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
    assert raised.value.error_fields == tuple(f"field_{index:02d}" for index in range(20))


def test_hostile_key_names_are_reduced_to_the_safe_alphabet(
    caplog: pytest.LogCaptureFixture, fake_stax_api_key: str
) -> None:
    body = {
        "e mail\n=x": 1,
        "<script>alert(1)</script>": 1,
        "a" * 60: 1,
        "line_items.0.amount": 1,
        "ünïcode-ok": 1,
        "": 1,
        "!!!": 1,
        f"card {CARD}": 1,
        "4111-1111-1111-1111": 1,
        fake_stax_api_key: 1,
        f"x{fake_stax_api_key}y": 1,
        "errors": {f"{CARD}": VALUE_MARKER, "zip code": VALUE_MARKER},
    }
    rig = make_rig(always(422, body))
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderRejected) as raised:
        rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)

    fields = raised.value.error_fields
    assert fields == tuple(
        sorted(
            {
                "emailx",
                "scriptalert1script",
                "a" * 40,
                "line_items.0.amount",
                "ncode-ok",
                "redacted",
                "errors",
                "zipcode",
            }
        )
    )
    assert all(len(name) <= 40 for name in fields)
    (line,) = _stax_lines(caplog)
    assert "\n" not in line and "<" not in line and " =" not in line
    for forbidden in (CARD, "4111-1111", fake_stax_api_key, VALUE_MARKER):
        assert forbidden not in caplog.text


def test_only_the_first_64_kb_of_an_error_body_is_parsed() -> None:
    # Valid JSON longer than 64 KB: cut, so not parsed, so reported as non-json.
    big = {"email": "x" * (70 * 1024)}
    rig = make_rig(always(422, big))
    with pytest.raises(ProviderRejected) as raised:
        rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
    assert raised.value.error_fields == ("non-json",)
    assert transport_module.error_fields(b'{"a": 1}' + b" " * (64 * 1024)) == ("a",)
    assert transport_module.error_fields(b" " * (64 * 1024) + b'{"a": 1}') == ("non-json",)


def test_a_deeply_nested_error_body_does_not_crash() -> None:
    body = b'{"email": ' + b"[" * 50_000 + b"]" * 50_000 + b"}"
    assert transport_module.error_fields(body) == ("non-json",)


def test_429_lines_carry_error_fields_and_the_final_error_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig(always(429, {"rate": VALUE_MARKER}))
    with caplog.at_level(logging.INFO), pytest.raises(ProviderUnavailable):
        rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
    lines = _stax_lines(caplog)
    assert len(lines) == 4
    assert all(line.endswith(" failure=ProviderUnavailable error_fields=rate") for line in lines)
    assert VALUE_MARKER not in caplog.text


def test_5xx_and_network_lines_carry_no_error_fields(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig(sequence([httpx.Response(500, json={"x": 1}), httpx.ConnectError("no")]))
    with caplog.at_level(logging.INFO):
        with pytest.raises(ProviderUnavailable):
            rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
        with pytest.raises(TransportError):
            rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
    assert all("error_fields" not in line for line in _stax_lines(caplog))


CREATED_PATHS: dict[str, Callable[[StaxProvider], object]] = {
    "customer": _create_customer,
    "invoice": _create_invoice,
    "schedule": _create_schedule,
    "pay": lambda provider: provider.pay_invoice(
        "inv-1", PayWithStoredMethod("pm-1", "6f1c1b7e-0000-4000-8000-000000000001")
    ),
    "refund": lambda provider: provider.refund_transaction("txn-1", Decimal("5.00")),
    "void": lambda provider: provider.void_transaction("txn-1"),
}


@pytest.mark.parametrize("call", CREATED_PATHS.values(), ids=list(CREATED_PATHS.keys()))
def test_a_2xx_create_logs_the_new_object_id(
    caplog: pytest.LogCaptureFixture, call: Callable[[StaxProvider], object]
) -> None:
    rig = make_rig(always(200, _created_body("new_obj-0001")))
    with (
        override_settings(STAX_PAY_INVOICE_ENABLED=True),
        caplog.at_level(logging.INFO),
    ):
        call(StaxProvider(rig.transport))
    (line,) = _stax_lines(caplog)
    assert " status=200 " in line
    assert line.endswith(" object_id=new_obj-0001")


@pytest.mark.parametrize(
    "bad_id",
    [None, "", 12345, "x" * 65, "id with space", "id/../x", "ïd", ["a"], {"a": 1}, f"{CARD}\n"],
    ids=repr,
)
def test_a_non_conforming_created_id_logs_a_dash(
    caplog: pytest.LogCaptureFixture, bad_id: object
) -> None:
    rig = make_rig(always(200, {**_created_body("ignored"), "id": bad_id}))
    with caplog.at_level(logging.INFO), contextlib.suppress(ProviderError):
        _create_customer(StaxProvider(rig.transport))
    (line,) = _stax_lines(caplog)
    assert line.endswith(" object_id=-")


def test_a_2xx_list_body_on_create_logs_a_dash(caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig(always(200, [{"id": "abc"}]))
    with caplog.at_level(logging.INFO), contextlib.suppress(ProviderError):
        rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
    (line,) = _stax_lines(caplog)
    assert line.endswith(" object_id=-")


def test_a_2xx_get_or_update_logs_the_request_id_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig(always(200, {"id": "other-id"}))
    with caplog.at_level(logging.INFO):
        rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
        rig.transport.send(Method.GET, "/customer", idempotent=True)
        rig.transport.send(
            Method.PUT, "/customer/{id}", object_id="cus-1", json={}, idempotent=True
        )
    lines = _stax_lines(caplog)
    assert lines[0].endswith(" object_id=inv-1")
    assert lines[1].endswith(" object_id=-")
    assert lines[2].endswith(" object_id=cus-1")


def test_an_unreadable_2xx_is_still_provider_unavailable_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig(always_text(200, "not json"))
    with caplog.at_level(logging.INFO), pytest.raises(ProviderUnavailable):
        rig.transport.send(Method.POST, "/customer", json={}, idempotent=False)
    (line,) = _stax_lines(caplog)
    assert line.endswith(" status=200 ms=0 object_id=-")
    assert len(rig.recorder.requests) == 1


def always_text(status: int, text: str) -> Handler:
    return lambda request: httpx.Response(status, text=text)


def _created_body(object_id: str) -> dict[str, object]:
    """A body every create mapping accepts, with ``id`` set."""
    return {
        "id": object_id,
        "status": "ok",
        "customer_id": "cus-1",
        "payment_method_id": "pm-1",
        "total": "100.00",
        "success": True,
        "is_refundable": True,
        "type": "charge",
        "rule": "DTSTART=20261001T000000Z;FREQ=MONTHLY",
        "email": "ada@example.com",
        "created_at": "2026-09-30 00:00:00",
    }
