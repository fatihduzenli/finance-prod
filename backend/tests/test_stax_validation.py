"""Local validation (PLAN-005 §6 row 6): each rule rejects and accepts, and a rejected
request never reaches the transport."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import override_settings

from payments.errors import ValidationFailed
from payments.provider import (
    Amounts,
    CustomerInput,
    CustomerQuery,
    DateRange,
    InvoiceInput,
    InvoiceUpdate,
    LineItem,
    OfflinePayment,
    PayWithStoredMethod,
    ScheduleInput,
    ScheduleUpdate,
    TransactionQuery,
)
from payments.stax import validation
from payments.stax.adapter import StaxProvider
from tests.stax_helpers import always, make_rig

D = Decimal
RULE = "DTSTART=20261001T000000Z;FREQ=MONTHLY"


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def _amounts(
    total: str = "105.00", subtotal: str = "100.00", tax: str = "5.00", *prices: str
) -> Amounts:
    lines = tuple(LineItem("Rent", D("1"), D(price)) for price in (prices or (subtotal,)))
    return Amounts(D(total), D(subtotal), D(tax), lines)


# --- Rule by rule -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["ada@example.com", "a.b+c@sub.example.com"])
def test_email_accepts(value: str) -> None:
    assert validation.email("e", value) == value


@pytest.mark.parametrize("value", ["", "ada", "ada@", "@example.com", "a b@example.com"])
def test_email_rejects(value: str) -> None:
    with pytest.raises(ValidationFailed) as raised:
        validation.email("operator_email", value)
    assert str(raised.value) == "operator_email must be a valid e-mail address"


@pytest.mark.parametrize("value", ["0.01", "1", "12581.00", "9999999999.99"])
def test_positive_amount_accepts(value: str) -> None:
    validation.positive_amount("amount", D(value))


@pytest.mark.parametrize("value", ["0", "0.00", "-1.00", "0.001", "1.005"])
def test_positive_amount_rejects(value: str) -> None:
    with pytest.raises(ValidationFailed):
        validation.positive_amount("amount", D(value))


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), 1.0, 1, "1.00"])
def test_positive_amount_rejects_non_decimal_or_non_finite(value: object) -> None:
    with pytest.raises(ValidationFailed):
        validation.positive_amount("amount", value)  # type: ignore[arg-type]


def test_amounts_accepts_a_consistent_set() -> None:
    validation.amounts(_amounts("105.00", "100.00", "5.00", "60.00", "40.00"))
    validation.amounts(_amounts("100.00", "100.00", "0.00"))


@pytest.mark.parametrize(
    "amounts",
    [
        pytest.param(_amounts("105.01", "100.00", "5.00"), id="total-not-subtotal-plus-tax"),
        pytest.param(_amounts("105.00", "100.00", "5.00", "60.00", "39.99"), id="lines-short"),
        pytest.param(_amounts("100.00", "100.00", "0.00", "100.001"), id="line-three-decimals"),
        pytest.param(_amounts("95.00", "100.00", "-5.00"), id="negative-tax"),
        pytest.param(_amounts("0.00", "0.00", "0.00"), id="zero-total"),
        pytest.param(Amounts(D("1.00"), D("1.00"), D("0"), ()), id="no-lines"),
        pytest.param(
            Amounts(D("1.00"), D("1.00"), D("0"), (LineItem(" ", D("1"), D("1.00")),)),
            id="blank-description",
        ),
        pytest.param(
            Amounts(D("1.00"), D("1.00"), D("0"), (LineItem("Rent", D("0"), D("1.00")),)),
            id="zero-quantity",
        ),
    ],
)
def test_amounts_rejects(amounts: Amounts) -> None:
    with pytest.raises(ValidationFailed):
        validation.amounts(amounts)


def test_idempotency_id_accepts_a_uuid_string() -> None:
    value = str(uuid.uuid4())
    assert validation.idempotency_id(value) == value
    assert validation.idempotency_id(value.upper()) == value.upper()


@pytest.mark.parametrize(
    "value",
    ["", "not-a-uuid", str(uuid.uuid4().hex), "{" + str(uuid.uuid4()) + "}", "x" * 256],
)
def test_idempotency_id_rejects(value: str) -> None:
    with pytest.raises(ValidationFailed):
        validation.idempotency_id(value)


def test_rule_accepts_dtstart() -> None:
    assert validation.rule(RULE) == RULE


@pytest.mark.parametrize("value", ["", "DTSTART=", "FREQ=MONTHLY", "dtstart=2026", "DTSTART=x\n"])
def test_rule_rejects(value: str) -> None:
    with pytest.raises(ValidationFailed):
        validation.rule(value)


def test_date_range_accepts_up_to_366_days() -> None:
    start = date(2026, 1, 1)
    validation.date_range(DateRange(start, start + timedelta(days=1)))
    validation.date_range(DateRange(start, start + timedelta(days=366)))


@pytest.mark.parametrize(
    "value",
    [
        DateRange(date(2026, 1, 2), date(2026, 1, 1)),
        DateRange(date(2026, 1, 1), date(2026, 1, 1)),
        DateRange(date(2026, 1, 1), date(2027, 1, 3)),
    ],
    ids=["reversed", "empty", "367-days"],
)
def test_date_range_rejects(value: DateRange) -> None:
    with pytest.raises(ValidationFailed):
        validation.date_range(value)


@pytest.mark.parametrize("value", ["abc", "a1b2-c3_d4", "0f8fad5b-d9cb-469f-a165-70867728950e"])
def test_external_id_accepts(value: str) -> None:
    assert validation.external_id("id", value) == value


@pytest.mark.parametrize(
    "value", ["", " ", "a b", "a\tb", "abc\n", "../self", "a/b", "a?b", "a#b", "%2e", "x" * 256]
)
def test_external_id_rejects(value: str) -> None:
    with pytest.raises(ValidationFailed):
        validation.external_id("id", value)


# --- Through the adapter: nothing reaches the transport -----------------------------------

Call = Callable[[StaxProvider], object]

REJECTED_CALLS: dict[str, Call] = {
    "customer bad email": lambda p: p.create_customer(CustomerInput("A", "B", "C", "nope")),
    "customer bad cc": lambda p: p.create_customer(
        CustomerInput("A", "B", "C", "a@example.com", cc_emails=("bad",))
    ),
    "update customer bad id": lambda p: p.update_customer(
        "a b", CustomerInput("A", "B", "C", "a@example.com")
    ),
    "find customers bad email": lambda p: p.find_customers(CustomerQuery(operator_email="x")),
    "invoice sums": lambda p: p.create_invoice(
        InvoiceInput("cus-1", _amounts("106.00", "100.00", "5.00"))
    ),
    "invoice bad customer": lambda p: p.create_invoice(InvoiceInput("", _amounts())),
    "update invoice sums": lambda p: p.update_invoice(
        "inv-1", InvoiceUpdate(amounts=_amounts("1.00", "2.00", "0.00"))
    ),
    "get invoice path traversal": lambda p: p.get_invoice("../self"),
    "delete invoice empty id": lambda p: p.delete_invoice(""),
    "send email bad cc": lambda p: p.send_invoice_email("inv-1", ["not-an-email"]),
    "mark paid zero": lambda p: p.mark_invoice_paid_cash_or_check(
        "inv-1", OfflinePayment("cash", D("0.00"))
    ),
    "pay bad idempotency": lambda p: p.pay_invoice("inv-1", PayWithStoredMethod("pm-1", "123")),
    "pay bad method id": lambda p: p.pay_invoice(
        "inv-1", PayWithStoredMethod("pm 1", str(uuid.uuid4()))
    ),
    "transactions range": lambda p: p.list_transactions(
        TransactionQuery(date_range=DateRange(date(2026, 2, 1), date(2026, 1, 1)))
    ),
    "transactions page": lambda p: p.list_transactions(TransactionQuery(page=0)),
    "refund three decimals": lambda p: p.refund_transaction("txn-1", D("1.001")),
    "refund negative": lambda p: p.refund_transaction("txn-1", D("-1.00")),
    "void bad id": lambda p: p.void_transaction("txn 1"),
    "deposits range": lambda p: p.list_deposits(DateRange(date(2025, 1, 1), date(2026, 6, 1))),
    "deposit bad batch": lambda p: p.get_deposit(
        DateRange(date(2026, 1, 1), date(2026, 1, 2)), "a/b"
    ),
    "schedule rule": lambda p: p.create_scheduled_invoice(
        ScheduleInput("cus-1", "pm-1", _amounts(), "FREQ=MONTHLY")
    ),
    "schedule sums": lambda p: p.create_scheduled_invoice(
        ScheduleInput("cus-1", "pm-1", _amounts("1.00", "2.00", "0.00"), RULE)
    ),
    "schedule update rule": lambda p: p.update_scheduled_invoice(
        "sch-1", ScheduleUpdate(rule="RRULE")
    ),
    "schedule update empty": lambda p: p.update_scheduled_invoice("sch-1", ScheduleUpdate()),
    "delete schedule bad id": lambda p: p.delete_scheduled_invoice("sch 1"),
    "get schedule bad id": lambda p: p.get_scheduled_invoice(""),
    "ach range": lambda p: p.list_ach_rejections(DateRange(date(2026, 1, 1), date(2026, 1, 1))),
}


@pytest.mark.parametrize("call", REJECTED_CALLS.values(), ids=REJECTED_CALLS.keys())
def test_a_rejected_request_never_reaches_the_transport(call: Call) -> None:
    rig = make_rig(always(200, {"id": "x"}))
    with pytest.raises(ValidationFailed):
        call(StaxProvider(rig.transport))
    assert rig.recorder.requests == []
    assert rig.budget.counts() == (0, 0)


def test_float_money_is_refused_when_the_input_is_built() -> None:
    with pytest.raises(ValidationFailed):
        LineItem("Rent", D("1"), 100.0)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        OfflinePayment("cash", 10.0)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        Amounts(1.0, D("1.00"), D("0"), ())  # type: ignore[arg-type]
