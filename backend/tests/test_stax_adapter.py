"""The Stax adapter (PLAN-005 §6 rows 7, 8, 9, 11 and 13) and ``provider_ping`` (§4.4).

Requests are inspected as they reached the mock transport: method, path, query and the
exact JSON body.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx
import pytest
from django.core.management import call_command
from django.test import override_settings

from payments.errors import (
    FeatureDisabled,
    ProviderNotPermittedHere,
    ProviderRejected,
    ProviderUnavailable,
    TransportError,
    ValidationFailed,
)
from payments.management.commands import provider_ping
from payments.provider import (
    Amounts,
    CustomerInput,
    CustomerQuery,
    DateRange,
    InvoiceInput,
    InvoiceQuery,
    InvoiceUpdate,
    LineItem,
    OfflinePayment,
    PaymentProvider,
    PayWithStoredMethod,
    ScheduleInput,
    ScheduleQuery,
    ScheduleUpdate,
    TransactionQuery,
)
from payments.stax import adapter, mapping
from payments.stax.adapter import StaxProvider
from payments.stax.transport import Method
from tests.stax_helpers import Rig, always, make_rig, sequence

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[2]
ENDPOINTS_FILE = REPO_ROOT / "docs" / "inputs" / "STAX_ENDPOINTS_2026-09-30.md"
RULE = "DTSTART=20261001T000000Z;FREQ=MONTHLY"
RANGE = DateRange(date(2026, 9, 1), date(2026, 9, 30))


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=False):
        yield


def _amounts() -> Amounts:
    return Amounts(
        total=D("12581.00"),
        subtotal=D("12500.00"),
        tax=D("81.00"),
        line_items=(
            LineItem("Rent", D("1"), D("12000.00")),
            LineItem("Insurance", D("2"), D("250.00"), details="annual"),
        ),
    )


def _body(request: httpx.Request) -> object:
    return json.loads(request.content, parse_float=Decimal)


# --- Every interface method, with valid arguments ------------------------------------

Call = Callable[[StaxProvider], object]

ALL_METHODS: dict[str, Call] = {
    "create_customer": lambda p: p.create_customer(
        CustomerInput("Ada", "Lovelace", "Example LLC", "ada@example.com")
    ),
    "update_customer": lambda p: p.update_customer(
        "cus-1", CustomerInput("Ada", "Lovelace", "Example LLC", "ada@example.com")
    ),
    "get_customer": lambda p: p.get_customer("cus-1"),
    "find_customers": lambda p: p.find_customers(CustomerQuery(operator_email="a@example.com")),
    "list_payment_methods": lambda p: p.list_payment_methods("cus-1"),
    "get_payment_method": lambda p: p.get_payment_method("pm-1"),
    "create_invoice": lambda p: p.create_invoice(InvoiceInput("cus-1", _amounts())),
    "update_invoice": lambda p: p.update_invoice("inv-1", InvoiceUpdate(memo="m")),
    "get_invoice": lambda p: p.get_invoice("inv-1"),
    "list_invoices": lambda p: p.list_invoices(InvoiceQuery()),
    "delete_invoice": lambda p: p.delete_invoice("inv-1"),
    "send_invoice_email": lambda p: p.send_invoice_email("inv-1", ["cc@example.com"]),
    "mark_invoice_paid_cash_or_check": lambda p: p.mark_invoice_paid_cash_or_check(
        "inv-1", OfflinePayment("check", D("10.00"), check_number="1001")
    ),
    "pay_invoice": lambda p: p.pay_invoice(
        "inv-1", PayWithStoredMethod("pm-1", "0f8fad5b-d9cb-469f-a165-70867728950e")
    ),
    "list_transactions": lambda p: p.list_transactions(TransactionQuery(date_range=RANGE)),
    "get_transaction": lambda p: p.get_transaction("txn-1"),
    "refund_transaction": lambda p: p.refund_transaction("txn-1", D("12.50")),
    "void_transaction": lambda p: p.void_transaction("txn-1"),
    "list_deposits": lambda p: p.list_deposits(RANGE),
    "get_deposit": lambda p: p.get_deposit(RANGE, "batch-1"),
    "create_scheduled_invoice": lambda p: p.create_scheduled_invoice(
        ScheduleInput("cus-1", "pm-1", _amounts(), RULE)
    ),
    "get_scheduled_invoice": lambda p: p.get_scheduled_invoice("sch-1"),
    "list_scheduled_invoices": lambda p: p.list_scheduled_invoices(ScheduleQuery()),
    "update_scheduled_invoice": lambda p: p.update_scheduled_invoice(
        "sch-1", ScheduleUpdate(active=False)
    ),
    "delete_scheduled_invoice": lambda p: p.delete_scheduled_invoice("sch-1"),
    "list_ach_rejections": lambda p: p.list_ach_rejections(RANGE),
    "list_webhooks": lambda p: p.list_webhooks(),
    "create_webhook": lambda p: p.create_webhook(
        "https://staging.example.com/api/webhooks/stax/" + "t" * 32 + "/", "update_invoice"
    ),
    "delete_webhook": lambda p: p.delete_webhook("wh-1"),
    "ping": lambda p: p.ping(),
}


def test_the_method_table_covers_the_whole_interface() -> None:
    assert set(ALL_METHODS) == set(PaymentProvider.__abstractmethods__)


# --- Row 7: STAX_CALLER=none --------------------------------------------------------------


@pytest.mark.parametrize("call", ALL_METHODS.values(), ids=ALL_METHODS.keys())
def test_caller_none_refuses_every_method_before_the_network(call: Call) -> None:
    rig = make_rig(always(200, {"id": "x"}))
    with override_settings(STAX_CALLER="none", STAX_PAY_INVOICE_ENABLED=True):
        with pytest.raises(ProviderNotPermittedHere):
            call(StaxProvider(rig.transport))
    assert rig.recorder.requests == []
    assert rig.budget.counts() == (0, 0)


# --- Row 8: pay_invoice --------------------------------------------------------------------


@pytest.mark.parametrize(
    "payment",
    [
        PayWithStoredMethod("pm-1", str(uuid.uuid4())),
        PayWithStoredMethod("", "not-a-uuid"),
    ],
    ids=["valid", "invalid"],
)
def test_pay_invoice_flag_off_raises_before_validation_and_network(
    payment: PayWithStoredMethod,
) -> None:
    rig = make_rig(always(200, {"id": "txn-1"}))
    with pytest.raises(FeatureDisabled):
        StaxProvider(rig.transport).pay_invoice("../bad id", payment)
    assert rig.recorder.requests == []


def test_pay_invoice_flag_on_sends_the_verified_request() -> None:
    rig = make_rig(always(200, {"id": "txn-1", "status": "PENDING", "total": 12581.0}))
    idempotency_id = str(uuid.uuid4())
    with override_settings(STAX_PAY_INVOICE_ENABLED=True):
        record = StaxProvider(rig.transport).pay_invoice(
            "inv-1", PayWithStoredMethod("pm-1", idempotency_id)
        )
    [request] = rig.recorder.requests
    assert request.method == "POST"
    assert request.url.path == "/invoice/inv-1/pay"
    assert dict(request.url.params) == {
        "payment_method_id": "pm-1",
        "idempotency_id": idempotency_id,
        "email_receipt": "0",
    }
    assert str(uuid.UUID(request.url.params["idempotency_id"])) == idempotency_id
    assert request.content == b""
    assert record.external_id == "txn-1"
    assert record.raw_status == "PENDING"
    assert record.total == D("12581.0")


def test_pay_invoice_is_never_retried_and_a_later_attempt_reuses_the_idempotency_id() -> None:
    """PLAN-010d D4: one request per attempt, whatever the failure; the next attempt (a
    new call by the caller) carries the same identifier."""
    rig = make_rig(sequence([httpx.ConnectError("refused"), httpx.Response(200, json={"id": "t"})]))
    idempotency_id = str(uuid.uuid4())
    payment = PayWithStoredMethod("pm-1", idempotency_id)
    with override_settings(STAX_PAY_INVOICE_ENABLED=True):
        with pytest.raises(TransportError):
            StaxProvider(rig.transport).pay_invoice("inv-1", payment)
        assert len(rig.recorder.requests) == 1
        assert rig.clock.sleeps == []
        StaxProvider(rig.transport).pay_invoice("inv-1", payment)
    assert [r.url.params["idempotency_id"] for r in rig.recorder.requests] == [idempotency_id] * 2


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(503),
        httpx.Response(500),
        httpx.Response(429),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
    ids=["503", "500", "429", "timeout", "connect"],
)
def test_pay_invoice_sends_exactly_one_request_on_any_retryable_failure(
    reply: httpx.Response | Exception,
) -> None:
    rig = make_rig(sequence([reply]))
    idempotency_id = str(uuid.uuid4())
    with override_settings(STAX_PAY_INVOICE_ENABLED=True), pytest.raises(ProviderUnavailable):
        StaxProvider(rig.transport).pay_invoice(
            "inv-1", PayWithStoredMethod("pm-1", idempotency_id)
        )
    [request] = rig.recorder.requests
    assert request.url.params["idempotency_id"] == idempotency_id
    assert rig.clock.sleeps == []
    # The failure still counts against the breaker and the failure budget.
    assert rig.budget.counts()[0] == 1


def test_pay_invoice_sends_email_receipt_one_when_asked() -> None:
    rig = make_rig(always(200, {"id": "txn-1"}))
    with override_settings(STAX_PAY_INVOICE_ENABLED=True):
        StaxProvider(rig.transport).pay_invoice(
            "inv-1", PayWithStoredMethod("pm-1", str(uuid.uuid4()), email_receipt=True)
        )
    [request] = rig.recorder.requests
    assert request.url.params["email_receipt"] == "1"
    assert "apply_balance" not in request.url.params


def test_the_other_calls_keep_their_retries() -> None:
    rig = make_rig(sequence([httpx.Response(503), httpx.Response(200, json={"id": "inv-1"})]))
    StaxProvider(rig.transport).get_invoice("inv-1")
    assert len(rig.recorder.requests) == 2
    assert rig.clock.sleeps == [1_000]


# --- Row 9: schedule updates cannot carry total or meta -------------------------------------


def test_schedule_update_has_no_total_or_meta_field() -> None:
    from dataclasses import fields

    assert {field.name for field in fields(ScheduleUpdate)} == {
        "active",
        "rule",
        "email_notification",
    }


def test_schedule_update_body_has_no_total_or_meta_key() -> None:
    rig = make_rig(always(200, {"id": "sch-1", "active": False}))
    StaxProvider(rig.transport).update_scheduled_invoice(
        "sch-1", ScheduleUpdate(active=False, rule=RULE, email_notification=True)
    )
    [request] = rig.recorder.requests
    assert request.method == "PUT"
    assert request.url.path == "/invoice/schedule/sch-1"
    body = _body(request)
    assert body == {"active": False, "rule": RULE, "email_notification": True}
    assert isinstance(body, dict)
    assert "total" not in body
    assert "meta" not in body


# --- Requests as they leave -------------------------------------------------------------


def test_create_invoice_body_is_ach_only_with_exact_money() -> None:
    rig = make_rig(always(200, {"id": "inv-1", "status": "DRAFT", "total": "12581.00"}))
    record = StaxProvider(rig.transport).create_invoice(
        InvoiceInput("cus-1", _amounts(), due_on=date(2026, 10, 1), memo="October", reference="r1")
    )
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("POST", "/invoice")
    assert b"12581.00" in request.content  # a JSON number written exactly
    assert _body(request) == {
        "customer_id": "cus-1",
        "url": adapter.DEFAULT_PAYMENT_PAGE_URL,
        "total": D("12581.00"),
        "meta": {
            "subtotal": "12500.00",
            "tax": "81.00",
            "lineItems": [
                {
                    "item": "Rent",
                    "details": "",
                    "quantity": 1,
                    "price": D("12000.00"),
                    "total": D("12000.00"),
                },
                {
                    "item": "Insurance",
                    "details": "annual",
                    "quantity": 2,
                    "price": D("250.00"),
                    "total": D("500.00"),
                },
            ],
            "isACHPaymentEnabled": True,
            "isCCPaymentEnabled": False,
            "memo": "October",
            "reference": "r1",
        },
        "is_partial_payment_enabled": False,
        "due_at": "2026-10-01 23:59:59",
    }
    assert record.total == D("12581.00")
    assert record.raw_status == "DRAFT"


def test_customer_body_omits_empty_optional_fields() -> None:
    """PLAN-006b §3 row 2: no ``phone`` key when empty, no ``lastname`` key, never ``""``."""
    rig = make_rig(always(200, {"id": "cus-1"}))
    StaxProvider(rig.transport).create_customer(
        CustomerInput("Ada", "", "Example LLC", "ada@example.com", reference="r1")
    )
    [request] = rig.recorder.requests
    assert _body(request) == {
        "firstname": "Ada",
        "company": "Example LLC",
        "email": "ada@example.com",
        "cc_emails": [],
        "reference": "r1",
    }


@pytest.mark.parametrize("method", ["create", "update"])
def test_customer_body_sends_the_phone_as_digits_only(method: str) -> None:
    rig = make_rig(always(200, {"id": "cus-1"}))
    provider = StaxProvider(rig.transport)
    customer = CustomerInput("Ada", "", "Example LLC", "ada@example.com", phone="+1 (415) 555-0132")
    if method == "create":
        provider.create_customer(customer)
    else:
        provider.update_customer("cus-1", customer)
    [request] = rig.recorder.requests
    body = _body(request)
    assert isinstance(body, dict)
    assert body["phone"] == "14155550132"
    assert "lastname" not in body


@pytest.mark.parametrize("method", ["create", "update"])
@pytest.mark.parametrize("phone", ["555-0132", "12345678901234567", "+44 20 7946 0958 ext 12"])
def test_a_customer_with_a_bad_phone_is_refused_before_any_request(method: str, phone: str) -> None:
    """PLAN-006b §3 row 3: ``ValidationFailed`` at the adapter, nothing reaches the transport."""
    rig = make_rig(always(200, {"id": "cus-1"}))
    provider = StaxProvider(rig.transport)
    customer = CustomerInput("Ada", "", "Example LLC", "ada@example.com", phone=phone)
    with pytest.raises(ValidationFailed) as raised:
        if method == "create":
            provider.create_customer(customer)
        else:
            provider.update_customer("cus-1", customer)
    assert phone not in str(raised.value)
    assert rig.recorder.requests == []
    assert rig.budget.counts() == (0, 0)


@pytest.mark.parametrize(
    ("method", "verb", "path"),
    [("create", "POST", "/customer"), ("update", "PUT", "/customer/cus-1")],
)
def test_a_customer_without_an_email_is_sent_without_an_email_key(
    method: str, verb: str, path: str
) -> None:
    """PLAN-006c rule 4: an empty e-mail is allowed and left out of the body."""
    rig = make_rig(always(200, {"id": "cus-1"}))
    provider = StaxProvider(rig.transport)
    customer = CustomerInput("Ada", "", "Example LLC", "", reference="r1")
    if method == "create":
        provider.create_customer(customer)
    else:
        provider.update_customer("cus-1", customer)
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == (verb, path)
    assert _body(request) == {
        "firstname": "Ada",
        "company": "Example LLC",
        "cc_emails": [],
        "reference": "r1",
    }


@pytest.mark.parametrize("method", ["create", "update"])
@pytest.mark.parametrize("value", ["nope", "ada@", " ", "a b@example.com"])
def test_a_customer_with_a_malformed_email_is_refused_before_any_request(
    method: str, value: str
) -> None:
    rig = make_rig(always(200, {"id": "cus-1"}))
    provider = StaxProvider(rig.transport)
    customer = CustomerInput("Ada", "", "Example LLC", value)
    with pytest.raises(ValidationFailed) as raised:
        if method == "create":
            provider.create_customer(customer)
        else:
            provider.update_customer("cus-1", customer)
    assert str(raised.value) == "operator_email must be a valid e-mail address"
    assert rig.recorder.requests == []
    assert rig.budget.counts() == (0, 0)


def test_refund_total_is_a_two_decimal_string() -> None:
    rig = make_rig(always(200, {"id": "txn-2"}))
    StaxProvider(rig.transport).refund_transaction("txn-1", D("12.5"))
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("POST", "/transaction/txn-1/refund")
    assert _body(request) == {"total": "12.50"}


def test_mark_paid_uses_the_method_path_and_query() -> None:
    rig = make_rig(always(200, {"id": "inv-1", "status": "PAID"}))
    StaxProvider(rig.transport).mark_invoice_paid_cash_or_check(
        "inv-1", OfflinePayment("check", D("10"), check_number="1001", note="lobby")
    )
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("POST", "/invoice/inv-1/pay/check")
    assert dict(request.url.params) == {
        "email_receipt": "0",
        "check_number": "1001",
        "payment_note": "lobby",
    }


# --- PLAN-009a: booleans in a query string are "1"/"0" -------------------------------------

#: The 422 body Stax returned on staging 2026-10-02 for ``email_receipt=false``.
EMAIL_RECEIPT_REJECTION = {"email_receipt": ["The email receipt field must be true or false."]}


@pytest.mark.parametrize(("value", "expected"), [(True, "1"), (False, "0")])
def test_query_bool_is_one_or_zero(value: bool, expected: str) -> None:
    assert mapping.query_bool(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(True, "1"), (False, "0")])
def test_offline_and_pay_params_carry_email_receipt_as_one_or_zero(
    value: bool, expected: str
) -> None:
    offline = mapping.offline_payment_params(
        OfflinePayment("cash", D("10.00"), email_receipt=value)
    )
    pay = mapping.pay_params(PayWithStoredMethod("pm-1", str(uuid.uuid4()), email_receipt=value))
    for params in (offline, pay):
        assert params["email_receipt"] == expected
        assert type(params["email_receipt"]) is str
        assert not any(isinstance(v, bool) for v in params.values())


def _stax_like_receipt_check(request: httpx.Request) -> httpx.Response:
    """Answers as Stax did: 422 unless ``email_receipt`` is ``1`` or ``0``."""
    if request.url.params.get("email_receipt") not in {"0", "1"}:
        return httpx.Response(422, json=EMAIL_RECEIPT_REJECTION)
    return httpx.Response(200, json={"id": "inv-1", "status": "PAID"})


def test_mark_paid_sends_email_receipt_zero_on_the_wire() -> None:
    rig = make_rig(_stax_like_receipt_check)
    StaxProvider(rig.transport).mark_invoice_paid_cash_or_check(
        "inv-1", OfflinePayment("cash", D("10.00"))
    )
    [request] = rig.recorder.requests
    assert request.url.path == "/invoice/inv-1/pay/cash"
    assert request.url.query == b"email_receipt=0"


def test_pay_invoice_sends_email_receipt_zero_on_the_wire() -> None:
    rig = make_rig(_stax_like_receipt_check)
    with override_settings(STAX_PAY_INVOICE_ENABLED=True):
        StaxProvider(rig.transport).pay_invoice(
            "inv-1", PayWithStoredMethod("pm-1", str(uuid.uuid4()))
        )
    [request] = rig.recorder.requests
    assert b"email_receipt=0" in request.url.query.split(b"&")


def test_the_staging_email_receipt_rejection_is_a_provider_rejected_naming_the_field() -> None:
    rig = make_rig(always(422, EMAIL_RECEIPT_REJECTION))
    with pytest.raises(ProviderRejected) as caught:
        StaxProvider(rig.transport).mark_invoice_paid_cash_or_check(
            "inv-1", OfflinePayment("cash", D("10.00"))
        )
    assert caught.value.error_fields == ("email_receipt",)
    assert len(rig.recorder.requests) == 1


# --- Staging 2026-10-06: invoice_date_at and due_at are "Y-m-d H:i:s" -------------------

#: The 422 messages Stax gives for a bare date in each body field (Laravel style).
DATE_TIME_REJECTION = {
    "due_at": ["The due at does not match the format Y-m-d H:i:s."],
    "invoice_date_at": ["The invoice date at does not match the format Y-m-d H:i:s."],
}
STAX_DATE_TIME = re.compile(r"\A\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z")


def _stax_like_date_time_check(request: httpx.Request) -> httpx.Response:
    """Answers as Stax does: 422 naming each date field that is not ``Y-m-d H:i:s``."""
    body = json.loads(request.content)
    refused = {
        key: message
        for key, message in DATE_TIME_REJECTION.items()
        if key in body and not STAX_DATE_TIME.match(str(body[key]))
    }
    if refused:
        return httpx.Response(422, json=refused)
    return httpx.Response(200, json={"id": "inv-1", "status": "DRAFT", "total": "12581.00"})


@pytest.mark.parametrize(
    ("end_of_day", "expected"), [(False, "2026-10-06 00:00:00"), (True, "2026-10-06 23:59:59")]
)
def test_stax_datetime_is_date_space_time(end_of_day: bool, expected: str) -> None:
    assert mapping.stax_datetime(date(2026, 10, 6), end_of_day=end_of_day) == expected


def test_the_mock_refuses_a_bare_date_in_both_fields_with_a_provider_rejected() -> None:
    rig = make_rig(_stax_like_date_time_check)
    old_shape = {"customer_id": "cus-1", "invoice_date_at": "2026-10-06", "due_at": "2026-10-16"}
    with pytest.raises(ProviderRejected) as caught:
        rig.transport.send(Method.POST, adapter.INVOICES, json=old_shape, idempotent=False)
    assert caught.value.status == 422
    assert caught.value.error_fields == ("due_at", "invoice_date_at")
    assert len(rig.recorder.requests) == 1


def test_create_invoice_sends_date_times_stax_accepts() -> None:
    rig = make_rig(_stax_like_date_time_check)
    record = StaxProvider(rig.transport).create_invoice(
        InvoiceInput("cus-1", _amounts(), due_on=date(2026, 10, 16), invoice_date=date(2026, 10, 6))
    )
    [request] = rig.recorder.requests
    body = _body(request)
    assert isinstance(body, dict)
    assert body["invoice_date_at"] == "2026-10-06 00:00:00"
    assert body["due_at"] == "2026-10-16 23:59:59"
    assert (record.external_id, record.raw_status) == ("inv-1", "DRAFT")


def test_update_invoice_sends_due_at_as_end_of_day() -> None:
    rig = make_rig(_stax_like_date_time_check)
    StaxProvider(rig.transport).update_invoice("inv-1", InvoiceUpdate(due_on=date(2026, 10, 16)))
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("PUT", "/invoice/inv-1")
    assert _body(request) == {"due_at": "2026-10-16 23:59:59"}
    assert mapping.invoice_update_body(InvoiceUpdate(due_on=date(2026, 10, 16))) == {
        "due_at": "2026-10-16 23:59:59"
    }


def test_send_email_body_is_cc_emails() -> None:
    rig = make_rig(always(200, {}))
    StaxProvider(rig.transport).send_invoice_email("inv-1", ["cc@example.com"])
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("PUT", "/invoice/inv-1/send/email")
    assert _body(request) == {"ccEmails": ["cc@example.com"]}


def test_statement_queries_use_start_and_end_date() -> None:
    rig = make_rig(always(200, []))
    provider = StaxProvider(rig.transport)
    provider.list_deposits(RANGE)
    provider.get_deposit(RANGE, "batch-1")
    provider.list_ach_rejections(RANGE)
    paths = [(r.url.path, dict(r.url.params)) for r in rig.recorder.requests]
    dates = {"startDate": "2026-09-01", "endDate": "2026-09-30"}
    assert paths == [
        ("/query/deposit", dates),
        ("/query/depositDetail", {**dates, "batchId": "batch-1"}),
        ("/query/statement/v3/ach-rejects", dates),
    ]


def test_get_scheduled_invoice_filters_the_list() -> None:
    listing = {"data": [{"id": "sch-0"}, {"id": "sch-1", "rule": RULE, "active": True}]}
    rig = make_rig(always(200, listing))
    record = StaxProvider(rig.transport).get_scheduled_invoice("sch-1")
    assert (record.external_id, record.rule, record.active) == ("sch-1", RULE, True)
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("GET", "/invoice/schedule")
    with pytest.raises(ProviderRejected) as raised:
        StaxProvider(make_rig(always(200, listing)).transport).get_scheduled_invoice("sch-9")
    assert raised.value.status == 404


def test_records_keep_raw_status_and_a_trimmed_summary() -> None:
    rig = make_rig(
        always(
            200,
            {
                "id": "txn-1",
                "status": "PARTIALLY APPLIED",
                "total": "10.00",
                "bank_account": "000123456789",
                "memo": "card 4111 1111 1111 1111",
                "payment_method": {
                    "id": "pm-1",
                    "method": "bank",
                    "bank_routing": "011000015",
                    "bank_account": "000123456789",
                    "nickname": "Operating",
                },
                "customer": {"email": "ada@example.com"},
            },
        )
    )
    record = StaxProvider(rig.transport).get_transaction("txn-1")
    assert record.raw_status == "PARTIALLY APPLIED"
    assert record.raw_summary == {
        "id": "txn-1",
        "status": "PARTIALLY APPLIED",
        "total": "10.00",
        "payment_method": {"id": "pm-1", "method": "bank", "nickname": "Operating"},
    }


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"no_id": True}),
        httpx.Response(200, content=b'{"id": "x", "total": NaN}'),
    ],
    ids=["html", "list", "no-id", "nan"],
)
def test_an_unreadable_success_raises_provider_unavailable(reply: httpx.Response) -> None:
    rig = make_rig(lambda request: reply)
    with pytest.raises(ProviderUnavailable):
        StaxProvider(rig.transport).get_invoice("inv-1")
    assert len(rig.recorder.requests) == 1


# --- Row 11: log lines -----------------------------------------------------------------

BODY_MARKER = "BODY-MARKER-7f3a"
LINE = re.compile(
    r"^stax method=(GET|POST|PUT|DELETE) path=(/\S*) status=(\d{3}|-) ms=\d+ object_id=(\S+)"
    r"( failure=[A-Za-z]+)?( error_fields=[A-Za-z0-9_.,-]+)?$"
)


def test_log_lines_carry_only_method_path_status_duration_and_id(
    caplog: pytest.LogCaptureFixture, fake_stax_api_key: str
) -> None:
    rig = make_rig(
        sequence(
            [
                httpx.Response(200, json={"id": "cus-1", "email": "ada@example.com"}),
                httpx.Response(500, json={"error": BODY_MARKER, "card": "4111111111111111"}),
                httpx.Response(422, text=f"{BODY_MARKER} Bearer {fake_stax_api_key}"),
                httpx.ConnectError(f"{BODY_MARKER} network detail"),
            ]
        )
    )
    provider = StaxProvider(rig.transport)
    customer = CustomerInput("Ada", "Lovelace", BODY_MARKER, "ada@example.com")
    with caplog.at_level(logging.INFO, logger="payments.stax.transport"):
        provider.update_customer("cus-1", customer)
        with pytest.raises(ProviderUnavailable):
            provider.create_invoice(InvoiceInput("cus-1", _amounts(), memo=BODY_MARKER))
        with pytest.raises(ProviderRejected):
            provider.refund_transaction("txn-9", D("4321.98"))
        with pytest.raises(ProviderUnavailable):
            provider.create_customer(customer)

    lines = [r.getMessage() for r in caplog.records if r.name == "payments.stax.transport"]
    assert lines[0].startswith("stax method=PUT path=/customer/{id} status=200 ms=")
    assert lines[0].endswith(" object_id=cus-1")
    assert lines[1].startswith("stax method=POST path=/invoice status=500 ")
    assert lines[1].endswith(" object_id=- failure=ProviderUnavailable")
    assert lines[2].startswith("stax method=POST path=/transaction/{id}/refund status=422 ")
    assert lines[2].endswith(" object_id=txn-9 failure=ProviderRejected error_fields=non-json")
    assert lines[3].startswith("stax method=POST path=/customer status=- ")
    assert lines[3].endswith(" failure=ConnectError")
    assert len(lines) == 4
    for line in lines:
        assert LINE.match(line), line

    everything = caplog.text + "\n".join(lines)
    for forbidden in (
        fake_stax_api_key,
        "Bearer",
        BODY_MARKER,
        "ada@example.com",
        "4111111111111111",
        "12581",
        "4321.98",
        "Lovelace",
    ):
        assert forbidden not in everything


# --- Row 13: every path is a verified path ------------------------------------------------


def _adapter_paths() -> set[str]:
    source = Path(adapter.__file__).read_text(encoding="utf-8")
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/")
    }


def test_every_adapter_path_is_in_the_verified_endpoints_file() -> None:
    verified = ENDPOINTS_FILE.read_text(encoding="utf-8")
    paths = _adapter_paths()
    assert len(paths) >= 15
    for path in paths:
        pattern = rf"`(GET|POST|PUT|DELETE) {re.escape(path)}(`|,| )"
        assert re.search(pattern, verified), f"{path} is not in {ENDPOINTS_FILE.name}"


def test_every_request_path_matches_an_adapter_path() -> None:
    rig = make_rig(always(200, {"id": "x", "merchant_id": "merchant-000011112222", "data": []}))
    provider = StaxProvider(rig.transport)
    with override_settings(STAX_PAY_INVOICE_ENABLED=True):
        for name, call in ALL_METHODS.items():
            try:
                call(provider)
            except (ProviderRejected, ProviderUnavailable):
                assert name in ("get_scheduled_invoice",)
    templates = [
        re.compile("^" + re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(path)) + "$")
        for path in _adapter_paths()
    ]
    for request in rig.recorder.requests:
        assert any(t.match(request.url.path) for t in templates), request.url.path


# --- ping and provider_ping -------------------------------------------------------------

MERCHANT = "merchant-000011112222"


def test_ping_reports_the_identity() -> None:
    rig = make_rig(always(200, {"merchant_id": MERCHANT}))
    with override_settings(STAX_EXPECTED_MERCHANT_ID=MERCHANT):
        identity = StaxProvider(rig.transport).ping()
    assert (identity.provider_name, identity.environment) == ("stax", "sandbox")
    assert identity.matches_expected is True
    assert MERCHANT not in repr(identity)
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("GET", "/self")


def _run_ping(rig: Rig) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(provider_ping, "StaxProvider", lambda: StaxProvider(rig.transport)):
        try:
            call_command("provider_ping", stdout=stdout, stderr=stderr)
            code = 0
        except SystemExit as exit_:
            code = int(exit_.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("expected", "matches"), [(MERCHANT, "yes"), ("", "not set")], ids=["match", "unset"]
)
def test_provider_ping_success(expected: str, matches: str) -> None:
    rig = make_rig(always(200, {"merchant_id": MERCHANT}))
    with override_settings(STAX_EXPECTED_MERCHANT_ID=expected):
        code, out, err = _run_ping(rig)
    assert code == 0
    assert out.splitlines() == [
        "provider: stax",
        "environment: sandbox",
        "merchant: merc…2222",
        f"merchant matches expected: {matches}",
    ]
    assert MERCHANT not in out + err
    assert len(rig.recorder.requests) == 1


def test_provider_ping_mismatch_exits_1() -> None:
    rig = make_rig(always(200, {"merchant": {"id": MERCHANT}}))
    with override_settings(STAX_EXPECTED_MERCHANT_ID="merchant-999999999999"):
        code, out, err = _run_ping(rig)
    assert code == 1
    assert "merchant matches expected: no" in out
    assert "merc…2222" in out
    assert MERCHANT not in out + err
    assert "merchant-999999999999" not in out + err


@pytest.mark.parametrize("status", [401, 403, 500])
def test_provider_ping_failure_exits_2_once_with_the_class_name_only(
    status: int, fake_stax_api_key: str
) -> None:
    rig = make_rig(always(status, {"message": f"{BODY_MARKER} {fake_stax_api_key}"}))
    code, out, err = _run_ping(rig)
    assert code == 2
    assert out == ""
    expected = "ProviderRejected" if status < 500 else "ProviderUnavailable"
    assert err.strip() == f"provider_ping failed: {expected}"
    # 4xx: one request, no loop. 5xx follows the transport's policy for reads only.
    assert len(rig.recorder.requests) == (1 if status < 500 else 4)


def test_provider_ping_refuses_without_worker() -> None:
    rig = make_rig(always(200, {"merchant_id": MERCHANT}))
    with override_settings(STAX_CALLER="none"):
        code, out, err = _run_ping(rig)
    assert code == 2
    assert err.strip() == "provider_ping failed: ProviderNotPermittedHere"
    assert rig.recorder.requests == []


def test_transport_sends_the_bearer_header_and_json_headers(fake_stax_api_key: str) -> None:
    rig = make_rig(always(200, {"id": "x"}))
    rig.transport.send(Method.GET, "/self", idempotent=True)
    [request] = rig.recorder.requests
    assert request.headers["Authorization"] == f"Bearer {fake_stax_api_key}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/json"
