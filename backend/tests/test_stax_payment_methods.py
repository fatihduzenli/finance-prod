"""Payment methods through the adapter and the fake (PLAN-007 §6 row 2, D1).

Requests are inspected as they reached the mock transport. Every value below is an
obvious placeholder: the account and routing numbers are the public test pattern of
the existing adapter tests, used only to prove they never survive.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from django.test import override_settings

from payments.errors import ProviderRejected, ProviderUnavailable, ValidationFailed
from payments.fake import FakeProvider
from payments.provider import PAYMENT_METHOD_DELETED, PaymentMethodRecord
from payments.stax import adapter
from payments.stax.adapter import StaxProvider
from tests.stax_helpers import always, make_rig

ACCOUNT = "000123456789"
ROUTING = "011000015"
CARD = "4111111111111111"
PERSON = "Ada Placeholder"

#: Every key the specification names on a payment-method object, with hostile values.
FULL_BANK = {
    "id": "pm-bank-1",
    "method": "bank",
    "bank_type": "checking",
    "bank_name": "Example Bank",
    "bank_holder_type": "business",
    "card_last_four": "6789",
    "card_type": "",
    "card_exp": "",
    "person_name": PERSON,
    "nickname": "Operating",
    "is_default": True,
    "customer_id": "cus-1",
    "deleted_at": None,
    "purged_at": None,
    # Not in the specification; must be dropped whatever it is called.
    "bank_account": ACCOUNT,
    "bank_routing": ROUTING,
    "account_number": ACCOUNT,
    "meta": {"routing": ROUTING},
}
CARD_METHOD = {
    "id": "pm-card-1",
    "method": "card",
    "card_last_four": "1111",
    "card_type": "visa",
    "card_exp": "1229",
    "person_name": PERSON,
    "nickname": "Corporate card",
    "is_default": False,
    "customer_id": "cus-1",
}
RECORD_FIELDS = {
    "external_id",
    "raw_status",
    "fetched_at",
    "method",
    "last_four",
    "bank_type",
    "nickname",
    "is_default",
}


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


def test_the_record_has_exactly_the_planned_fields() -> None:
    assert {field.name for field in dataclasses.fields(PaymentMethodRecord)} == RECORD_FIELDS


def test_list_uses_the_verified_path_and_keeps_only_the_planned_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig(always(200, [FULL_BANK, CARD_METHOD]))
    with caplog.at_level(logging.DEBUG):
        bank, card = StaxProvider(rig.transport).list_payment_methods("cus-1")
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("GET", "/customer/cus-1/payment-method")
    assert request.content == b""
    assert (bank.external_id, bank.method, bank.last_four, bank.bank_type) == (
        "pm-bank-1",
        "bank",
        "6789",
        "checking",
    )
    assert (bank.nickname, bank.is_default, bank.raw_status) == ("Operating", True, "")
    assert bank.is_usable_bank
    assert (card.method, card.is_usable_bank) == ("card", False)
    everything = repr(bank) + repr(card) + caplog.text
    for forbidden in (ACCOUNT, ROUTING, PERSON, "Example Bank", "business", "visa", "1229"):
        assert forbidden not in everything


def test_list_accepts_an_object_with_a_data_key() -> None:
    rig = make_rig(always(200, {"data": [FULL_BANK]}))
    [record] = StaxProvider(rig.transport).list_payment_methods("cus-1")
    assert record.external_id == "pm-bank-1"


def test_a_method_of_another_customer_is_dropped() -> None:
    other = {**FULL_BANK, "id": "pm-other", "customer_id": "cus-2"}
    rig = make_rig(always(200, [FULL_BANK, other]))
    records = StaxProvider(rig.transport).list_payment_methods("cus-1")
    assert [record.external_id for record in records] == ["pm-bank-1"]


def test_get_uses_the_verified_path() -> None:
    rig = make_rig(always(200, FULL_BANK))
    record = StaxProvider(rig.transport).get_payment_method("pm-bank-1")
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("GET", "/payment-method/pm-bank-1")
    assert record.external_id == "pm-bank-1"
    assert ACCOUNT not in repr(record)


@pytest.mark.parametrize("key", ["deleted_at", "purged_at"])
def test_a_deleted_or_purged_method_is_marked_and_not_usable(key: str) -> None:
    rig = make_rig(always(200, {**FULL_BANK, key: "2026-09-30 10:00:00"}))
    record = StaxProvider(rig.transport).get_payment_method("pm-bank-1")
    assert record.raw_status == PAYMENT_METHOD_DELETED
    assert not record.is_usable_bank


@pytest.mark.parametrize(
    ("value", "kept"),
    [("6789", "6789"), ("678", ""), ("67890", ""), (6789, ""), ("67a9", ""), (None, "")],
)
def test_last_four_is_kept_only_as_four_digits(value: object, kept: str) -> None:
    rig = make_rig(always(200, {**FULL_BANK, "card_last_four": value}))
    assert StaxProvider(rig.transport).get_payment_method("pm-1").last_four == kept


@pytest.mark.parametrize(
    "nickname",
    [f"card {CARD}", f"acct {ACCOUNT} routing {ROUTING}", "x" * 101, 12345],
    ids=["card", "account", "too-long", "not-text"],
)
def test_an_unsafe_nickname_is_dropped(nickname: object) -> None:
    rig = make_rig(always(200, {**FULL_BANK, "nickname": nickname}))
    record = StaxProvider(rig.transport).get_payment_method("pm-1")
    assert record.nickname == ""
    assert ACCOUNT not in repr(record) and CARD not in repr(record)


@pytest.mark.parametrize(("value", "flag"), [(True, True), (1, True), (False, False), (0, False)])
def test_is_default_accepts_a_boolean_or_one(value: object, flag: bool) -> None:
    rig = make_rig(always(200, {**FULL_BANK, "is_default": value}))
    assert StaxProvider(rig.transport).get_payment_method("pm-1").is_default is flag


@pytest.mark.parametrize("bad", ["", "../cus", "cus 1", "cus/1", "cus%2F1"])
def test_a_bad_customer_id_is_refused_before_any_request(bad: str) -> None:
    rig = make_rig(always(200, []))
    with pytest.raises(ValidationFailed):
        StaxProvider(rig.transport).list_payment_methods(bad)
    with pytest.raises(ValidationFailed):
        StaxProvider(rig.transport).get_payment_method(bad)
    assert rig.recorder.requests == []


def test_an_object_without_an_id_is_unreadable() -> None:
    rig = make_rig(always(200, [{"method": "bank"}]))
    with pytest.raises(ProviderUnavailable):
        StaxProvider(rig.transport).list_payment_methods("cus-1")


def test_the_log_line_names_the_template_and_no_account_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig(always(200, [FULL_BANK]))
    with caplog.at_level(logging.INFO, logger="payments.stax.transport"):
        StaxProvider(rig.transport).list_payment_methods("cus-1")
    [line] = [r.getMessage() for r in caplog.records if r.name == "payments.stax.transport"]
    assert line.startswith("stax method=GET path=/customer/{id}/payment-method status=200 ")
    assert ACCOUNT not in line and ROUTING not in line


def test_the_payment_method_paths_are_the_verified_ones() -> None:
    assert adapter.CUSTOMER_PAYMENT_METHODS == "/customer/{customerId}/payment-method"
    assert adapter.PAYMENT_METHOD == "/payment-method/{id}"


def test_no_method_creates_or_changes_a_payment_method() -> None:
    names = {name for name in dir(StaxProvider) if "payment_method" in name}
    assert names == {"list_payment_methods", "get_payment_method"}


# --- The fake -----------------------------------------------------------------------------


def _record(external_id: str, method: str = "bank") -> PaymentMethodRecord:
    return PaymentMethodRecord(
        external_id=external_id,
        raw_status="",
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
        method=method,
        last_four="6789",
    )


def test_the_fake_lists_only_the_customers_methods_and_records_the_calls() -> None:
    fake = FakeProvider()
    fake.add_payment_method("cus-1", _record("pm-1"))
    fake.add_payment_method("cus-2", _record("pm-2"))
    assert [r.external_id for r in fake.list_payment_methods("cus-1")] == ["pm-1"]
    assert fake.get_payment_method("pm-2").external_id == "pm-2"
    with pytest.raises(ProviderRejected):
        fake.get_payment_method("pm-9")
    assert [name for name, _ in fake.calls] == [
        "list_payment_methods",
        "get_payment_method",
        "get_payment_method",
    ]
