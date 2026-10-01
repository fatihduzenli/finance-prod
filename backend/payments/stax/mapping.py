"""Our field names to Stax field names and back, in one place (PLAN-005 D12, §4.2).

Request field names are copied from ``docs/inputs/STAX_ENDPOINTS_2026-09-30.md``, which
Fable extracted from the specification blocks on 2026-09-30. That file lists request
parameters and bodies only, so every key read from a *response* is unverified and
marked TODO-VERIFY where it is read.

Money leaves as a string with two decimals where the specification types the field as
string, and as a JSON number where it types it as number. JSON is written by
:func:`encode_json`, which prints a ``Decimal`` exactly and refuses ``float``; responses
are parsed with ``Decimal`` for every non-integer number, so no amount ever passes
through binary floating point.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError

from ledger.validators import validate_no_payment_data
from payments.errors import ProviderUnavailable
from payments.provider import (
    PAYMENT_METHOD_DELETED,
    AchRejection,
    Amounts,
    CustomerInput,
    CustomerQuery,
    CustomerRecord,
    DateRange,
    DepositRecord,
    InvoiceInput,
    InvoiceQuery,
    InvoiceRecord,
    InvoiceUpdate,
    OfflinePayment,
    PaymentMethodRecord,
    PayWithStoredMethod,
    ScheduleInput,
    ScheduleQuery,
    ScheduleRecord,
    ScheduleUpdate,
    Summary,
    TransactionQuery,
    TransactionRecord,
)

_CENT = Decimal("0.01")

#: docs/CLAUDE.md Rule 1: of a payment-method object, keep at most these keys.
PAYMENT_METHOD_KEYS: tuple[str, ...] = (
    "id",
    "method",
    "card_type",
    "card_last_four",
    "card_exp",
    "nickname",
)
#: Keys whose values never enter a summary, whatever they hold.
_SENSITIVE_KEY = re.compile(
    r"(account|routing|bank_|cvv|cvc|card_number|cardnumber|token|secret|password)",
    re.IGNORECASE,
)

Params = dict[str, str | int | bool]
Body = dict[str, Any]

# --- Encoding -----------------------------------------------------------------------


def money_string(value: Decimal) -> str:
    """``12581`` -> ``"12581.00"``. Exact; the value is validated to two decimals first."""
    return format(value.quantize(_CENT), "f")


def iso_date(value: date) -> str:
    # TODO-VERIFY-026: the date format Stax expects in body and query fields
    # (invoice_date_at, due_at, start_date, end_date, startDate, endDate). ISO 8601
    # (YYYY-MM-DD) is assumed.
    return value.isoformat()


def encode_json(value: object) -> bytes:
    """Serialise a request body. ``Decimal`` becomes an exact JSON number; ``float`` is refused."""
    return "".join(_encode(value)).encode("utf-8")


def _encode(value: object) -> Iterator[str]:
    if value is None:
        yield "null"
    elif isinstance(value, bool):
        yield "true" if value else "false"
    elif isinstance(value, int):
        yield str(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("Only finite Decimal amounts can be encoded")
        yield format(value, "f")
    elif isinstance(value, str):
        yield json.dumps(value)
    elif isinstance(value, Mapping):
        yield "{"
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            yield ("," if index else "") + json.dumps(key) + ":"
            yield from _encode(item)
        yield "}"
    elif isinstance(value, list | tuple):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _encode(item)
        yield "]"
    else:
        raise TypeError(f"Cannot encode {type(value).__name__} in a request body")


def _refuse_constant(name: str) -> None:
    raise ValueError("Non-finite numbers are not accepted")


def decode_json(content: bytes) -> object:
    """Parse a response body; non-integer numbers become ``Decimal``. Empty body is ``None``."""
    if not content.strip():
        return None
    return json.loads(content, parse_float=Decimal, parse_constant=_refuse_constant)


# --- Requests: customers ---------------------------------------------------------------


def customer_body(customer: CustomerInput) -> Body:
    """Page create-customer / update-customer-info.

    An optional field that is empty is left out, never sent as ``""`` (PLAN-006b §2.3).
    """
    body: Body = {
        key: value
        for key, value in (
            ("firstname", customer.first_name),
            ("lastname", customer.last_name),
            ("company", customer.company_name),
            ("email", customer.operator_email),
        )
        if value
    }
    body["cc_emails"] = list(customer.cc_emails)
    if customer.phone:
        body["phone"] = customer.phone
    if customer.reference:
        # TODO-VERIFY-004: whether `reference` is returned on GET and filterable on list.
        body["reference"] = customer.reference
    return body


def customer_params(query: CustomerQuery) -> Params:
    """Page find-all-customers."""
    params: Params = {}
    if query.operator_email:
        params["email"] = query.operator_email
    if query.company_name:
        params["company"] = query.company_name
    if query.reference:
        params["reference"] = query.reference
    return params


# --- Requests: invoices ----------------------------------------------------------------


def _invoice_meta(amounts: Amounts) -> Body:
    """``meta`` of create-an-invoice: subtotal and tax are strings, line items numbers."""
    return {
        "subtotal": money_string(amounts.subtotal),
        "tax": money_string(amounts.tax),
        "lineItems": [
            {
                "item": item.description,
                "details": item.details,
                "quantity": item.quantity,
                "price": item.unit_price,
                "total": item.total.quantize(_CENT),
            }
            for item in amounts.line_items
        ],
    }


def invoice_body(invoice: InvoiceInput, payment_page_url: str) -> Body:
    """Page create-an-invoice. ACH only, never card, full amount due (docs/CLAUDE.md Rule 2)."""
    meta = _invoice_meta(invoice.amounts)
    meta["isACHPaymentEnabled"] = True
    meta["isCCPaymentEnabled"] = False
    if invoice.memo:
        meta["memo"] = invoice.memo
    if invoice.reference:
        # TODO-VERIFY-004: whether meta.reference is returned on GET.
        meta["reference"] = invoice.reference
    body: Body = {
        "customer_id": invoice.customer_external_id,
        "url": payment_page_url,
        "total": invoice.amounts.total,
        "meta": meta,
        "is_partial_payment_enabled": False,
    }
    if invoice.invoice_date is not None:
        body["invoice_date_at"] = iso_date(invoice.invoice_date)
    if invoice.due_on is not None:
        body["due_at"] = iso_date(invoice.due_on)
    return body


def invoice_update_body(update: InvoiceUpdate) -> Body:
    """Page update-an-invoice.

    TODO-VERIFY-029: the specification block for PUT /invoice/{id} lists no body fields
    (only ``id`` and ``invoice_date_at``). The create-an-invoice names are assumed, and
    whether a partial ``meta`` replaces or merges the stored one is unknown.
    """
    body: Body = {}
    if update.amounts is not None:
        body["total"] = update.amounts.total
        body["meta"] = _invoice_meta(update.amounts)
    if update.memo is not None:
        body.setdefault("meta", {})["memo"] = update.memo
    if update.due_on is not None:
        body["due_at"] = iso_date(update.due_on)
    return body


def invoice_params(query: InvoiceQuery) -> Params:
    """Page get-all-invoices."""
    params: Params = {}
    if query.operator_email:
        params["email"] = query.operator_email
    if query.company_name:
        params["company_name"] = query.company_name
    if query.schedule_external_id:
        params["schedule_id"] = query.schedule_external_id
    return params


def offline_payment_params(payment: OfflinePayment) -> Params:
    """Page mark-an-invoice-paid-in-cash-or-check (query parameters)."""
    # TODO-VERIFY-028: type and meaning of `apply_balance` (amount to apply is assumed,
    # as a two-decimal string).
    params: Params = {
        "apply_balance": money_string(payment.amount),
        "email_receipt": payment.email_receipt,
    }
    if payment.check_number:
        params["check_number"] = payment.check_number
    if payment.note:
        params["payment_note"] = payment.note
    return params


def pay_params(payment: PayWithStoredMethod) -> Params:
    """Page pay-an-invoice (query parameters)."""
    return {
        "payment_method_id": payment.payment_method_external_id,
        "idempotency_id": payment.idempotency_id,
        "email_receipt": payment.email_receipt,
    }


def send_email_body(cc: list[str]) -> Body:
    """Page send-an-invoice-via-email."""
    return {"ccEmails": cc}


# --- Requests: transactions, deposits, rejections -------------------------------------


def transaction_params(query: TransactionQuery) -> Params:
    """Page list-and-filter-all-transactions."""
    params: Params = {}
    if query.date_range is not None:
        params["start_date"] = iso_date(query.date_range.start)
        params["end_date"] = iso_date(query.date_range.end)
    if query.customer_external_id:
        params["customer_id"] = query.customer_external_id
    if query.payment_method_external_id:
        params["payment_method_id"] = query.payment_method_external_id
    if query.page is not None:
        params["page"] = query.page
    if query.per_page is not None:
        params["per_page"] = query.per_page
    return params


def refund_body(amount: Decimal) -> Body:
    """Page refund-transaction: ``total`` is a required string."""
    return {"total": money_string(amount)}


def statement_params(date_range: DateRange) -> Params:
    """Pages get-list-of-deposits, get-detail-of-specific-deposit, ach-rejections."""
    return {"startDate": iso_date(date_range.start), "endDate": iso_date(date_range.end)}


# --- Requests: schedules ----------------------------------------------------------------


def schedule_body(schedule: ScheduleInput, payment_page_url: str) -> Body:
    """Page create-a-scheduled-invoice: every amount is a number here.

    Exactly the fields of PLAN-007 D3: ``url``, ``total``, ``rule``, ``customer_id``,
    ``payment_method_id``, ``email_notification`` and ``meta.lineItems`` (``item``,
    ``quantity``, ``price``). Nothing else of ``meta`` is sent; whether schedule
    ``meta`` accepts ``memo``/``reference`` is TODO-VERIFY-035.
    """
    amounts = schedule.amounts
    return {
        "url": payment_page_url,
        "total": amounts.total,
        "rule": schedule.rule,
        "customer_id": schedule.customer_external_id,
        "payment_method_id": schedule.payment_method_external_id,
        "email_notification": schedule.email_notification,
        "meta": {
            "lineItems": [
                {"item": item.description, "quantity": item.quantity, "price": item.unit_price}
                for item in amounts.line_items
            ],
        },
    }


def schedule_update_body(update: ScheduleUpdate) -> Body:
    """Page edit-an-invoice-schedule. Never ``total`` or ``meta`` (D2)."""
    body: Body = {}
    if update.active is not None:
        body["active"] = update.active
    if update.rule is not None:
        body["rule"] = update.rule
    if update.email_notification is not None:
        body["email_notification"] = update.email_notification
    return body


def schedule_params(query: ScheduleQuery) -> Params:
    """Page retrieve-all-scheduled-invoices."""
    return {"customer_id": query.customer_external_id} if query.customer_external_id else {}


# --- Responses ------------------------------------------------------------------------


def _unexpected() -> ProviderUnavailable:
    return ProviderUnavailable(None, "The provider response had an unexpected shape")


def as_object(data: object) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise _unexpected()
    return data


def as_list(data: object) -> list[Mapping[str, object]]:
    # TODO-VERIFY-025: list responses are assumed to be either a bare array or an
    # object whose `data` key holds the array. Only the first page is read
    # (TODO-VERIFY-021: pagination parameters of GET /invoice, /customer,
    # /invoice/schedule).
    if isinstance(data, Mapping):
        data = data.get("data")
    if not isinstance(data, list):
        raise _unexpected()
    return [as_object(item) for item in data]


def _text(obj: Mapping[str, object], key: str) -> str:
    value = obj.get(key)
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _unexpected()
    return str(value)


def _required_id(obj: Mapping[str, object], key: str = "id") -> str:
    value = _text(obj, key)
    if not value:
        raise _unexpected()
    return value


def _money(obj: Mapping[str, object], key: str) -> Decimal | None:
    value = obj.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise _unexpected()
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise _unexpected() from None
    if not amount.is_finite():
        raise _unexpected()
    return amount


def _is_safe_text(value: str) -> bool:
    try:
        validate_no_payment_data(value)
    except ValidationError:
        return False
    return True


def summary(obj: Mapping[str, object]) -> Summary:
    """Scalar fields only, trimmed per docs/CLAUDE.md Rule 1.

    Nested objects are dropped except ``payment_method``, of which only
    :data:`PAYMENT_METHOD_KEYS` survive. Keys that name account, routing or card data
    are dropped, and so is any string that holds a card or account number.
    """
    trimmed: dict[str, Any] = {}
    for key, value in obj.items():
        if _SENSITIVE_KEY.search(key):
            continue
        if key == "payment_method" and isinstance(value, Mapping):
            trimmed[key] = {
                name: str(value[name])
                for name in PAYMENT_METHOD_KEYS
                if isinstance(value.get(name), str | int)
                and not isinstance(value.get(name), bool)
                and _is_safe_text(str(value[name]))
            }
        elif isinstance(value, str):
            if _is_safe_text(value):
                trimmed[key] = value
        elif value is None or isinstance(value, bool | int | Decimal):
            trimmed[key] = value
    return trimmed


# TODO-VERIFY-023: response keys of customer, invoice, transaction and schedule objects
# (`id`, `status`, `total`, `customer_id`, `email`, `company`, `reference`, `rule`,
# `active`) are assumed to match the request field names; the verified tables list
# request fields only.


def customer_record(data: object, fetched_at: datetime) -> CustomerRecord:
    obj = as_object(data)
    return CustomerRecord(
        external_id=_required_id(obj),
        raw_status=_text(obj, "status"),
        fetched_at=fetched_at,
        operator_email=_text(obj, "email"),
        company_name=_text(obj, "company"),
        reference=_text(obj, "reference"),
        raw_summary=summary(obj),
    )


def invoice_record(data: object, fetched_at: datetime) -> InvoiceRecord:
    obj = as_object(data)
    return InvoiceRecord(
        external_id=_required_id(obj),
        raw_status=_text(obj, "status"),
        fetched_at=fetched_at,
        customer_external_id=_text(obj, "customer_id"),
        total=_money(obj, "total"),
        raw_summary=summary(obj),
    )


def transaction_record(data: object, fetched_at: datetime) -> TransactionRecord:
    obj = as_object(data)
    return TransactionRecord(
        external_id=_required_id(obj),
        raw_status=_text(obj, "status"),
        fetched_at=fetched_at,
        total=_money(obj, "total"),
        raw_summary=summary(obj),
    )


def schedule_record(data: object, fetched_at: datetime) -> ScheduleRecord:
    obj = as_object(data)
    active = obj.get("active")
    return ScheduleRecord(
        external_id=_required_id(obj),
        raw_status=_text(obj, "status"),
        fetched_at=fetched_at,
        customer_external_id=_text(obj, "customer_id"),
        total=_money(obj, "total"),
        rule=_text(obj, "rule"),
        active=active if isinstance(active, bool) else None,
        raw_summary=summary(obj),
    )


#: Longest bank type or nickname kept from a payment-method object.
PAYMENT_METHOD_TEXT_MAX_LENGTH = 100
_LAST_FOUR = re.compile(r"[0-9]{4}")


def _safe_short_text(obj: Mapping[str, object], key: str) -> str:
    """A string field, or ``""`` when it is not a string, holds a card or account
    number, or is longer than :data:`PAYMENT_METHOD_TEXT_MAX_LENGTH`."""
    value = obj.get(key)
    if not isinstance(value, str) or len(value) > PAYMENT_METHOD_TEXT_MAX_LENGTH:
        return ""
    return value if _is_safe_text(value) else ""


def payment_method_record(data: object, fetched_at: datetime) -> PaymentMethodRecord:
    """Pages get-all-payment-methods-for-a-customer and get-a-payment-method-by-id.

    Only ``id``, ``method``, ``card_last_four`` (as ``last_four``), ``bank_type``,
    ``nickname``, ``is_default``, ``deleted_at`` and ``purged_at`` are read; every other
    key (``bank_name``, ``bank_holder_type``, ``card_type``, ``card_exp``,
    ``person_name`` and anything not named in the specification) is dropped here and
    never leaves this module (PLAN-007 D1, Rule 1).

    TODO-VERIFY-037: that ``card_last_four`` holds the last four digits of the account
    for a ``bank`` method, and that ``is_default`` is a boolean or 0/1. A last four that
    is not exactly four digits is dropped.
    """
    obj = as_object(data)
    last_four = obj.get("card_last_four")
    is_default = obj.get("is_default")
    deleted = obj.get("deleted_at") or obj.get("purged_at")
    return PaymentMethodRecord(
        external_id=_required_id(obj),
        raw_status=PAYMENT_METHOD_DELETED if deleted else "",
        fetched_at=fetched_at,
        method=_safe_short_text(obj, "method"),
        last_four=(
            last_four if isinstance(last_four, str) and _LAST_FOUR.fullmatch(last_four) else ""
        ),
        bank_type=_safe_short_text(obj, "bank_type"),
        nickname=_safe_short_text(obj, "nickname"),
        is_default=is_default is True or (type(is_default) is int and is_default == 1),
    )


def customer_payment_method_records(
    data: object, customer_external_id: str, fetched_at: datetime
) -> list[PaymentMethodRecord]:
    """The list response, minus any object whose ``customer_id`` names another customer."""
    return [
        payment_method_record(item, fetched_at)
        for item in as_list(data)
        if _text(item, "customer_id") in ("", customer_external_id)
    ]


def deposit_record(data: object, fetched_at: datetime) -> DepositRecord:
    obj = as_object(data)
    # TODO-VERIFY-030: response keys of get-list-of-deposits and
    # get-detail-of-specific-deposit; `batch_id` as the deposit's identifier is assumed.
    return DepositRecord(
        external_id=_required_id(obj, "batch_id"),
        raw_status="",
        fetched_at=fetched_at,
        raw_summary=summary(obj),
    )


def ach_rejection(data: object, fetched_at: datetime) -> AchRejection:
    obj = as_object(data)
    # TODO-VERIFY-022: only these three response keys are named by the specification.
    return AchRejection(
        external_id=_required_id(obj, "ach_reject_transaction_id"),
        raw_status="",
        fetched_at=fetched_at,
        amount=_money(obj, "ach_reject_transaction_amount"),
        raw_summary=summary(obj),
    )


def merchant_id(data: object) -> str:
    """The merchant identifier in the GET /self response.

    TODO-VERIFY-024: the key is not in the verified tables. A top-level `merchant_id`
    is read first, then `merchant.id`.
    """
    obj = as_object(data)
    value = _text(obj, "merchant_id")
    if not value:
        merchant = obj.get("merchant")
        if isinstance(merchant, Mapping):
            value = _text(merchant, "id")
    if not value:
        raise _unexpected()
    return value
