"""The provider interface, the fake, and the PLAN-005 configuration values (§4.1, §4.3)."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from finance.env_guard import EnvGuardError, validate
from payments import fake
from payments.errors import (
    CircuitOpen,
    FailureBudgetExhausted,
    FeatureDisabled,
    IdentityMismatch,
    ProviderError,
    ProviderNotPermittedHere,
    ProviderRejected,
    ProviderUnavailable,
    RateLimitWaitExceeded,
    TransportError,
    ValidationFailed,
)
from payments.fake import FakeProvider
from payments.provider import (
    Amounts,
    CustomerInput,
    CustomerQuery,
    InvoiceInput,
    LineItem,
    PaymentProvider,
    PayWithStoredMethod,
    mask_identifier,
)
from payments.stax.adapter import StaxProvider

D = Decimal

#: PLAN-005 §4.1, in snake_case (D1).
PLANNED_METHODS = {
    "create_customer",
    "update_customer",
    "get_customer",
    "find_customers",
    # PLAN-007 D1: read-only payment-method calls.
    "list_payment_methods",
    "get_payment_method",
    "create_invoice",
    "update_invoice",
    "get_invoice",
    "list_invoices",
    "delete_invoice",
    "send_invoice_email",
    "mark_invoice_paid_cash_or_check",
    "pay_invoice",
    "list_transactions",
    "get_transaction",
    "refund_transaction",
    "void_transaction",
    "list_deposits",
    "get_deposit",
    "create_scheduled_invoice",
    "get_scheduled_invoice",
    "list_scheduled_invoices",
    "update_scheduled_invoice",
    "delete_scheduled_invoice",
    "list_ach_rejections",
    "list_webhooks",
    "create_webhook",
    "delete_webhook",
    "ping",
}

LOCAL_ENV = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}


def _amounts() -> Amounts:
    return Amounts(D("10.00"), D("10.00"), D("0.00"), (LineItem("Rent", D("1"), D("10.00")),))


# --- Interface ------------------------------------------------------------------------


def test_the_interface_has_exactly_the_planned_methods() -> None:
    assert set(PaymentProvider.__abstractmethods__) == PLANNED_METHODS


def test_no_card_method_exists() -> None:
    names = {name for name, _ in inspect.getmembers(PaymentProvider)}
    for forbidden in ("charge", "create_card_payment_method", "tokenize", "create_payment_method"):
        assert forbidden not in names


@pytest.mark.parametrize("provider_class", [FakeProvider, StaxProvider])
def test_both_providers_implement_everything(provider_class: type[PaymentProvider]) -> None:
    assert not inspect.isabstract(provider_class)
    provider_class()  # building one touches neither the network nor the database


def test_the_error_hierarchy() -> None:
    for error in (
        ValidationFailed,
        ProviderNotPermittedHere,
        FeatureDisabled,
        CircuitOpen,
        FailureBudgetExhausted,
        RateLimitWaitExceeded,
        TransportError,
        ProviderRejected,
        ProviderUnavailable,
        IdentityMismatch,
    ):
        assert issubclass(error, ProviderError)
    assert ProviderRejected(404).status == 404
    assert "merchant-x" not in str(IdentityMismatch("merchant-x"))


def test_inputs_are_frozen() -> None:
    customer = CustomerInput("A", "B", "C", "a@example.com")
    with pytest.raises(FrozenInstanceError):
        customer.operator_email = "b@example.com"  # type: ignore[misc]


@pytest.mark.parametrize("bad", [10.0, 10, "10.00", True, D("NaN")], ids=repr)
def test_money_must_be_a_finite_decimal(bad: object) -> None:
    with pytest.raises(ValidationFailed):
        Amounts(bad, D("10.00"), D("0.00"), ())  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        LineItem("Rent", D("1"), bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "masked"),
    [("merchant-000011112222", "merc…2222"), ("123456789", "1234…6789"), ("12345678", "********")],
)
def test_mask_identifier(value: str, masked: str) -> None:
    assert mask_identifier(value) == masked


# --- Fake -----------------------------------------------------------------------------


def test_the_fake_round_trips_and_records_calls() -> None:
    provider = FakeProvider()
    customer = provider.create_customer(CustomerInput("A", "B", "C", "a@example.com"))
    assert provider.get_customer(customer.external_id) == customer
    assert provider.find_customers(CustomerQuery(operator_email="a@example.com")) == [customer]
    invoice = provider.create_invoice(InvoiceInput(customer.external_id, _amounts()))
    assert invoice.total == D("10.00")
    assert [name for name, _ in provider.calls] == [
        "create_customer",
        "get_customer",
        "find_customers",
        "create_invoice",
    ]


def test_the_fake_can_be_told_to_fail_once() -> None:
    provider = FakeProvider()
    provider.fail_next("get_invoice", ProviderUnavailable(503))
    with pytest.raises(ProviderUnavailable):
        provider.get_invoice("fake-invoice-1")
    with pytest.raises(ProviderRejected):
        provider.get_invoice("fake-invoice-1")  # the injected failure was used up


def test_the_fake_keeps_pay_invoice_behind_the_flag() -> None:
    provider = FakeProvider()
    with pytest.raises(FeatureDisabled):
        provider.pay_invoice("x", PayWithStoredMethod("pm", "0f8fad5b-d9cb-469f-a165-70867728950e"))
    assert provider.calls == []


def test_the_fake_knows_no_provider() -> None:
    assert "stax" not in Path(fake.__file__).read_text(encoding="utf-8").lower()


# --- Configuration (§4.3) -----------------------------------------------------------------


def test_configuration_defaults() -> None:
    config = validate(LOCAL_ENV)
    assert config.stax_caller == "none"
    assert config.stax_pay_invoice_enabled is False
    assert config.stax_expected_merchant_id == ""


@pytest.mark.parametrize(
    ("name", "value", "field", "expected"),
    [
        ("STAX_CALLER", "worker", "stax_caller", "worker"),
        ("STAX_CALLER", "none", "stax_caller", "none"),
        ("STAX_PAY_INVOICE_ENABLED", "true", "stax_pay_invoice_enabled", True),
        ("STAX_PAY_INVOICE_ENABLED", "false", "stax_pay_invoice_enabled", False),
        ("STAX_EXPECTED_MERCHANT_ID", "m-1", "stax_expected_merchant_id", "m-1"),
    ],
)
def test_configuration_accepts(name: str, value: str, field: str, expected: object) -> None:
    assert getattr(validate({**LOCAL_ENV, name: value}), field) == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STAX_CALLER", "web"),
        ("STAX_CALLER", "Worker"),
        ("STAX_CALLER", "worker "),
        ("STAX_PAY_INVOICE_ENABLED", "1"),
        ("STAX_PAY_INVOICE_ENABLED", "TRUE"),
        ("STAX_PAY_INVOICE_ENABLED", "FAKE-CANARY-do-not-echo"),
    ],
)
def test_configuration_rejects_other_values_without_echoing(name: str, value: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({**LOCAL_ENV, name: value})
    assert name in str(raised.value)
    assert value not in str(raised.value)


def test_expected_merchant_id_is_not_in_the_config_repr() -> None:
    config = validate({**LOCAL_ENV, "STAX_EXPECTED_MERCHANT_ID": "merchant-canary-0001"})
    assert "merchant-canary-0001" not in repr(config)


def test_env_example_documents_the_three_names_with_empty_values(repo_root: Path) -> None:
    lines = (repo_root / ".env.example").read_text(encoding="utf-8").splitlines()
    for name in ("STAX_CALLER", "STAX_PAY_INVOICE_ENABLED", "STAX_EXPECTED_MERCHANT_ID"):
        assert f"{name}=" in lines
