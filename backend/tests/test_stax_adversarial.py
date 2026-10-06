"""Adversarial cases for PLAN-005, written by the QA owner (plan §6 rows 16 to 19)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from django.test import override_settings

from payments.errors import (
    CircuitOpen,
    ProviderError,
    ProviderRejected,
    ProviderUnavailable,
    RateLimitWaitExceeded,
)
from payments.provider import (
    Amounts,
    DateRange,
    InvoiceRecord,
    LineItem,
    PayWithStoredMethod,
    TransactionRecord,
)
from payments.stax.adapter import StaxProvider
from payments.stax.limiter import RateLimiter
from payments.stax.transport import Method
from tests.stax_helpers import FakeClock, Recorder, Rig, always, make_rig, sequence

D = Decimal
FAKE_TOKEN = "FAKE-CI-KEY-XYZ123"
CARD = "4111111111111111"
EMAIL = "someone@example.com"
SECRET_BODY_MARKER = "BODY-MARKER-must-not-be-logged"


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def _leaks(records: list[logging.LogRecord]) -> list[str]:
    text = "\n".join(record.getMessage() for record in records)
    return [
        marker
        for marker in (FAKE_TOKEN, "Bearer", CARD, EMAIL, SECRET_BODY_MARKER, "routing", "12581")
        if marker in text
    ]


# --- 16: hostile and malformed responses --------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": f"card {CARD} routing 021000021 {SECRET_BODY_MARKER}"}),
        httpx.Response(400, json={"message": f"token {FAKE_TOKEN} {EMAIL} {SECRET_BODY_MARKER}"}),
        httpx.Response(200, content=b"<html>not json " + SECRET_BODY_MARKER.encode() + b"</html>"),
        httpx.Response(200, json=[1, 2, 3]),
        httpx.Response(200, json={"unexpected": {"shape": True}, "note": SECRET_BODY_MARKER}),
        httpx.Response(200, content=b""),
        httpx.Response(302, headers={"location": "https://evil.example.com/"}),
        httpx.Response(204),
    ],
)
def test_bodies_never_reach_the_log_whatever_the_response(
    response: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = make_rig(always(response.status_code, None) if False else (lambda request: response))
    provider = StaxProvider(rig.transport)
    try:
        provider.get_invoice("inv-1")
    except ProviderError:
        pass
    assert _leaks(caplog.records) == []
    assert "path=/invoice/{id}" in caplog.text


def test_error_messages_never_carry_the_body_or_token() -> None:
    rig = make_rig(always(422, {"message": f"{FAKE_TOKEN} {CARD} {SECRET_BODY_MARKER}"}))
    with pytest.raises(ProviderRejected) as raised:
        StaxProvider(rig.transport).get_invoice("inv-1")
    for marker in (FAKE_TOKEN, CARD, SECRET_BODY_MARKER):
        assert marker not in str(raised.value)
        assert marker not in repr(raised.value)


def test_a_stall_is_cut_by_the_timeout_setting() -> None:
    """The client carries a 15 s timeout on every phase; a stalled read raises, not hangs."""
    rig = make_rig(always(200, {}))
    client = rig.transport.client
    for phase in ("connect", "read", "write", "pool"):
        assert getattr(client.timeout, phase) == 15
    slow = make_rig(lambda request: httpx.ReadTimeout("stalled"))
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable):
        slow.transport.send(Method.GET, "/self", idempotent=True)
    assert time.monotonic() - started < 2.0  # fake clock: the backoff is not real time
    assert slow.clock.sleeps == [1_000, 4_000, 15_000]


def test_a_redirect_body_is_neither_followed_nor_decoded() -> None:
    hops = sequence(
        [
            httpx.Response(301, headers={"location": "/invoice/other"}),
            httpx.Response(301, headers={"location": "/invoice/other"}),
            httpx.Response(301, headers={"location": "/invoice/other"}),
            httpx.Response(301, headers={"location": "/invoice/other"}),
        ]
    )
    rig = make_rig(hops)
    with pytest.raises(ProviderUnavailable):
        rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
    assert all(str(request.url).endswith("/invoice/inv-1") for request in rig.recorder.requests)


def test_object_ids_cannot_inject_path_segments() -> None:
    rig = make_rig(always(200, {"id": "x"}))
    provider = StaxProvider(rig.transport)
    for hostile in ("../self", "inv 1", "inv/1?x=1", "inv%2F1", "", "a" * 256, "inv\n1"):
        with pytest.raises(ProviderError):
            provider.get_invoice(hostile)
    assert rig.recorder.requests == []


# --- 17: concurrency -----------------------------------------------------------------------


def test_fifty_threads_open_the_breaker_once_and_alert_once() -> None:
    clock = FakeClock()
    rig = make_rig(always(400), clock=clock)
    started = threading.Barrier(50)
    outcomes: list[str] = []
    lock = threading.Lock()

    def call() -> None:
        started.wait(timeout=10)
        try:
            rig.transport.send(Method.GET, "/self", idempotent=True)
        except ProviderRejected:
            result = "rejected"
        except CircuitOpen:
            result = "open"
        except RateLimitWaitExceeded:
            result = "wait"
        else:
            result = "ok"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=call) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == 50
    assert "ok" not in outcomes
    assert len(rig.alerts) == 1, rig.alerts
    # Only the requests that left before the breaker opened reached the network.
    assert len(rig.recorder.requests) <= 3
    assert outcomes.count("rejected") == len(rig.recorder.requests)


def test_no_waiter_starves_beyond_max_wait() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock.as_clock())
    waits: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def call() -> None:
        try:
            asked = clock.monotonic_ms()
            departed = limiter.acquire(max_wait_ms=5_000)
            with lock:
                waits.append(departed - asked)
        except RateLimitWaitExceeded as error:
            with lock:
                errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    # Every served caller waited at most its own allowance, measured from its own ask;
    # nobody was served late, and nobody was silently dropped.
    assert all(wait <= 5_000 for wait in waits)
    assert len(waits) + len(errors) == 30
    assert all(isinstance(error, RateLimitWaitExceeded) for error in errors)


# --- 18: money at the interface -------------------------------------------------------------


@pytest.mark.parametrize("total", [D("12581.00"), D("0.01"), D("9999999999.99")])
def test_money_survives_the_round_trip_to_the_wire(total: Decimal) -> None:
    rig = make_rig(always(200, {"id": "inv-9", "total": str(total), "status": "SENT"}))
    provider = StaxProvider(rig.transport)
    amounts = Amounts(
        total=total, subtotal=total, tax=D("0.00"), line_items=(LineItem("Rent", D("1"), total),)
    )
    record = provider.create_invoice(
        __import__("payments.provider", fromlist=["InvoiceInput"]).InvoiceInput(
            customer_external_id="cus-1", amounts=amounts, due_on=date(2026, 10, 5)
        )
    )
    assert isinstance(record, InvoiceRecord)
    assert record.total == total
    sent = rig.recorder.requests[0].content.decode()
    assert str(total) in sent
    assert "e+" not in sent.lower()


@pytest.mark.parametrize("bad", [12581.0, 0.1, 1e3, float("nan")])
def test_float_money_is_refused_before_anything_is_built(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError, ProviderError)):
        Amounts(total=bad, subtotal=bad, tax=D("0.00"), line_items=(LineItem("Rent", D("1"), bad),))


def test_refund_amount_is_sent_as_a_two_decimal_string() -> None:
    rig = make_rig(always(200, {"id": "txn-2", "total": "5.10", "status": "REFUNDED"}))
    record = StaxProvider(rig.transport).refund_transaction("txn-1", D("5.1"))
    assert isinstance(record, TransactionRecord)
    body = rig.recorder.requests[0].content.decode()
    assert '"5.10"' in body
    assert "5.1," not in body and "5.1}" not in body


# --- 19: idempotency under retry --------------------------------------------------------------


def test_pay_invoice_makes_exactly_one_request_and_a_later_attempt_reuses_the_key() -> None:
    """PLAN-010d D4: Charge now is one request, never an automatic retry; the stored
    idempotency ID is what makes a later, deliberate attempt safe at the provider."""
    from payments.errors import ProviderUnavailable

    key = "0f0a8f7e-1a2b-4c3d-8e9f-0123456789ab"
    payment = PayWithStoredMethod(payment_method_external_id="pm-1", idempotency_id=key)
    for failure in (httpx.ConnectError("boom"), httpx.ReadTimeout("boom")):
        rig = make_rig(sequence([failure]))
        with pytest.raises(ProviderUnavailable):
            StaxProvider(rig.transport).pay_invoice("inv-1", payment)
        assert len(rig.recorder.requests) == 1
        assert rig.clock.sleeps == []
    for status in (429, 500, 502, 503):
        rig = make_rig(always(status))
        with pytest.raises(ProviderError):
            StaxProvider(rig.transport).pay_invoice("inv-1", payment)
        assert len(rig.recorder.requests) == 1, status
        assert rig.clock.sleeps == []
    # The deliberate later attempt carries the very same key.
    rig = make_rig(
        sequence([httpx.Response(200, json={"id": "txn-1", "status": "PENDING", "total": "1.00"})])
    )
    StaxProvider(rig.transport).pay_invoice("inv-1", payment)
    assert [r.url.params.get("idempotency_id") for r in rig.recorder.requests] == [key]


def test_pay_invoice_is_also_refused_on_every_4xx_without_retry() -> None:
    for status in (400, 402, 403, 404, 409, 422):
        rig = make_rig(always(status))
        payment = PayWithStoredMethod(
            payment_method_external_id="pm-1", idempotency_id="0f0a8f7e-1a2b-4c3d-8e9f-0123456789ab"
        )
        with pytest.raises(ProviderRejected):
            StaxProvider(rig.transport).pay_invoice("inv-1", payment)
        assert len(rig.recorder.requests) == 1
        assert rig.clock.sleeps == []


def test_date_range_of_more_than_a_year_never_leaves_the_process() -> None:
    rig = make_rig(always(200, {"data": []}))
    with pytest.raises(ProviderError):
        StaxProvider(rig.transport).list_ach_rejections(
            DateRange(date(2024, 1, 1), date(2026, 1, 2))
        )
    with pytest.raises(ProviderError):
        StaxProvider(rig.transport).list_deposits(DateRange(date(2026, 2, 1), date(2026, 1, 1)))
    assert rig.recorder.requests == []
    assert isinstance(rig, Rig) and isinstance(rig.recorder, Recorder)


# --- PLAN-006a row 7: error field names under hostile bodies -------------------------------------


def _fields_from(body: bytes, caplog: pytest.LogCaptureFixture) -> str:
    caplog.set_level(logging.INFO)
    rig = make_rig(
        lambda request: httpx.Response(
            422, content=body, headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(ProviderRejected):
        rig.transport.send(Method.POST, "/customer", json={"a": "b"}, idempotent=False)
    lines = [
        record.getMessage() for record in caplog.records if "error_fields=" in record.getMessage()
    ]
    assert len(lines) == 1, lines
    return lines[0].split("error_fields=", 1)[1]


def test_five_megabyte_error_body_is_handled_and_logged_as_non_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = b'{"email": ["' + b"x" * (5 * 1024 * 1024) + b'"]}'
    started = time.monotonic()
    fields = _fields_from(body, caplog)
    assert time.monotonic() - started < 5.0
    assert fields == "non-json"
    assert "xxxx" not in caplog.text


@pytest.mark.parametrize(
    ("body", "expected_absent"),
    [
        (b'{"4111111111111111": ["x"]}', "4111111111111111"),
        (b'{"__KEY__": ["x"]}', "__KEY__"),
        (b'{"routing 021000021 acct 123456789012": ["x"]}', "021000021"),
        (b'{"": ["x"], "   ": ["y"]}', "email"),
        (b'{"\\u0000\\n\\t": ["x"]}', "\x00"),
        (b'{"em\\u00e4il \\ud83d\\ude42": ["x"]}', "ä"),
        (b'{"a": {"b": {"c": {"d": {"e": "4111111111111111"}}}}}', "4111111111111111"),
        (b'{"errors": {"card_number": ["4111111111111111"], "cvv": ["123"]}}', "4111111111111111"),
    ],
)
def test_hostile_key_names_and_values_never_reach_the_log(
    body: bytes, expected_absent: str, caplog: pytest.LogCaptureFixture, fake_stax_api_key: str
) -> None:
    body = body.replace(b"__KEY__", fake_stax_api_key.encode())
    expected_absent = expected_absent.replace("__KEY__", fake_stax_api_key)
    fields = _fields_from(body, caplog)
    assert expected_absent not in caplog.text
    assert re.fullmatch(r"[A-Za-z0-9_.,-]+", fields), fields


def test_a_two_thousand_level_deep_body_does_not_crash(caplog: pytest.LogCaptureFixture) -> None:
    body = b"[" * 2000 + b"]" * 2000
    assert _fields_from(body, caplog) in {"non-json", "list"}


def test_numeric_and_boolean_json_bodies(caplog: pytest.LogCaptureFixture) -> None:
    for body in (b"42", b"true", b"null", b'"a string with 4111111111111111"'):
        caplog.clear()
        fields = _fields_from(body, caplog)
        assert fields in {"scalar", "non-json"}
        assert "4111111111111111" not in caplog.text
