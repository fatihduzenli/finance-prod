"""Guard rules 1-8 from docs/qa/PLAN-002-scaffolding.md §3."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.conf import settings

from finance.env_guard import (
    REQUIRED_STAX_API_BASE_URL,
    WEBHOOK_URL_TOKEN_MIN_LENGTH,
    EnvGuardError,
    validate,
)

#: Canary value: if this ever appears in an error message, the guard is echoing the
#: environment, and a real key would leak the same way (rule 8).
FAKE_VALUE = "FAKE-GUARD-VALUE-do-not-use-ABC789"

LOCAL_ENV = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": REQUIRED_STAX_API_BASE_URL,
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}

DEPLOYED_ENV = {
    "APP_ENV": "staging",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": REQUIRED_STAX_API_BASE_URL,
    "STAX_API_KEY": FAKE_VALUE,
    "STAX_WEBHOOK_URL_TOKEN": "t" * WEBHOOK_URL_TOKEN_MIN_LENGTH,
    "DATABASE_URL": "postgres://finance@db.example.com:5432/finance",
    "APP_BASE_URL": "https://staging.example.com",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}


def _env(base: dict[str, str], **overrides: str | None) -> dict[str, str]:
    """``base`` with overrides applied; a ``None`` override removes the variable."""
    merged = dict(base)
    for name, value in overrides.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


# --- happy paths ---------------------------------------------------------------


def test_local_sandbox_is_valid() -> None:
    config = validate(LOCAL_ENV)
    assert config.app_env == "local"
    assert config.stax_env == "sandbox"
    assert config.debug is False
    assert config.stax_api_key == ""
    assert config.redis_url == ""
    assert config.sentry_dsn == ""


def test_staging_sandbox_is_valid() -> None:
    config = validate(DEPLOYED_ENV)
    assert config.app_env == "staging"
    assert config.stax_webhook_url_token == "t" * WEBHOOK_URL_TOKEN_MIN_LENGTH


def test_optional_variables_are_passed_through() -> None:
    config = validate(_env(LOCAL_ENV, REDIS_URL="redis://localhost:6379/0", SENTRY_DSN="dsn"))
    assert config.redis_url == "redis://localhost:6379/0"
    assert config.sentry_dsn == "dsn"


def test_debug_is_honoured_only_in_local() -> None:
    assert validate(_env(LOCAL_ENV, DJANGO_DEBUG="1")).debug is True
    assert validate(_env(LOCAL_ENV, DJANGO_DEBUG="0")).debug is False
    assert validate(_env(DEPLOYED_ENV, DJANGO_DEBUG="1")).debug is False


# --- rule 1: APP_ENV -----------------------------------------------------------


@pytest.mark.parametrize("app_env", [None, "", "prod", "Local", "production ", "dev"])
def test_rule_1_rejects_bad_app_env(app_env: str | None) -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, APP_ENV=app_env))


# --- rule 2: STAX_ENV ----------------------------------------------------------


@pytest.mark.parametrize("stax_env", [None, "", " sandbox ", "SANDBOX", "Sandbox", "test"])
def test_rule_2_rejects_bad_stax_env(stax_env: str | None) -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, STAX_ENV=stax_env))


def test_rule_2_applies_in_every_app_env() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_ENV=None))
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, APP_ENV="production", STAX_ENV=None))


# --- rule 3 and rule 4: where a live key may be loaded -------------------------


def test_rule_3_rejects_live_in_local() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, STAX_ENV="live"))


def test_rule_3_rejects_live_in_staging() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_ENV="live"))


def test_rule_3_allows_live_in_production() -> None:
    config = validate(_env(DEPLOYED_ENV, APP_ENV="production", STAX_ENV="live"))
    assert config.stax_env == "live"


def test_rule_4_allows_sandbox_in_production() -> None:
    config = validate(_env(DEPLOYED_ENV, APP_ENV="production", STAX_ENV="sandbox"))
    assert config.stax_env == "sandbox"


# --- rule 5: base URL ----------------------------------------------------------


def test_rule_5_pins_base_url_outside_local() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_API_BASE_URL="https://apiprod.fattlabs.com/"))
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_API_BASE_URL="http://apiprod.fattlabs.com"))
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_API_BASE_URL="https://evil.example.com"))


def test_rule_5_allows_a_fake_base_url_in_local() -> None:
    config = validate(_env(LOCAL_ENV, STAX_API_BASE_URL="http://localhost:9999"))
    assert config.stax_api_base_url == "http://localhost:9999"


def test_rule_5_requires_the_variable_everywhere() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, STAX_API_BASE_URL=None))


# --- rule 6: webhook token length ---------------------------------------------


def test_rule_6_rejects_a_short_token_in_deployed_environments() -> None:
    short = "t" * (WEBHOOK_URL_TOKEN_MIN_LENGTH - 1)
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_WEBHOOK_URL_TOKEN=short))
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_WEBHOOK_URL_TOKEN=None))


def test_rule_6_accepts_the_minimum_length() -> None:
    token = "t" * WEBHOOK_URL_TOKEN_MIN_LENGTH
    config = validate(_env(DEPLOYED_ENV, STAX_WEBHOOK_URL_TOKEN=token))
    assert config.stax_webhook_url_token == token


def test_rule_6_does_not_apply_to_local() -> None:
    assert validate(_env(LOCAL_ENV, STAX_WEBHOOK_URL_TOKEN="short")).app_env == "local"


# --- rule 7: sqlite ------------------------------------------------------------


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_rule_7_rejects_sqlite_outside_local(app_env: str) -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, APP_ENV=app_env, DATABASE_URL="sqlite:///./db.sqlite3"))


def test_rule_7_allows_sqlite_in_local() -> None:
    assert validate(LOCAL_ENV).database_url == "sqlite:///:memory:"


def test_database_url_must_be_present_and_have_a_scheme() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, DATABASE_URL=None))
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, DATABASE_URL="db.example.com/finance"))


# --- other required variables --------------------------------------------------


def test_api_key_is_required_in_deployed_environments_only() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_API_KEY=None))
    with pytest.raises(EnvGuardError):
        validate(_env(DEPLOYED_ENV, STAX_API_KEY=""))
    assert validate(_env(LOCAL_ENV, STAX_API_KEY="")).stax_api_key == ""


def test_secret_key_is_required() -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, DJANGO_SECRET_KEY=None))


@pytest.mark.parametrize("app_base_url", [None, "", "staging.example.com", "https://", "ftp://x.y"])
def test_app_base_url_must_be_an_absolute_web_url(app_base_url: str | None) -> None:
    with pytest.raises(EnvGuardError):
        validate(_env(LOCAL_ENV, APP_BASE_URL=app_base_url))


# --- rule 8: messages never contain a value ------------------------------------


def test_rule_8_message_contains_no_environment_values() -> None:
    hostile = {
        "APP_ENV": FAKE_VALUE,
        "STAX_ENV": FAKE_VALUE,
        "STAX_API_BASE_URL": FAKE_VALUE,
        "STAX_API_KEY": FAKE_VALUE,
        "STAX_WEBHOOK_URL_TOKEN": FAKE_VALUE,
        "DATABASE_URL": FAKE_VALUE,
        "APP_BASE_URL": FAKE_VALUE,
        "DJANGO_SECRET_KEY": FAKE_VALUE,
    }
    with pytest.raises(EnvGuardError) as raised:
        validate(hostile)
    message = str(raised.value)
    assert FAKE_VALUE not in message
    assert repr(FAKE_VALUE) not in message
    # It still has to be useful: it names the variables that are wrong.
    assert "APP_ENV" in message
    assert "STAX_ENV" in message


def test_all_problems_are_reported_together() -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({})
    message = str(raised.value)
    for name in ("APP_ENV", "STAX_ENV", "STAX_API_BASE_URL", "DATABASE_URL", "DJANGO_SECRET_KEY"):
        assert name in message


def test_error_is_an_environment_error() -> None:
    assert issubclass(EnvGuardError, EnvironmentError)


# --- LARGE_DEBIT_WARNING_AMOUNT (PLAN-007 §4.3) ------------------------------------------


def test_large_debit_warning_amount_defaults_to_2000() -> None:
    assert validate(LOCAL_ENV).large_debit_warning_amount == Decimal("2000.00")
    assert validate(_env(LOCAL_ENV, LARGE_DEBIT_WARNING_AMOUNT="")).large_debit_warning_amount == (
        Decimal("2000.00")
    )


@pytest.mark.parametrize("value", ["1500", "1500.5", "0.01", "99999.99"])
def test_large_debit_warning_amount_accepts_a_positive_amount(value: str) -> None:
    config = validate(_env(LOCAL_ENV, LARGE_DEBIT_WARNING_AMOUNT=value))
    assert config.large_debit_warning_amount == Decimal(value)
    assert isinstance(config.large_debit_warning_amount, Decimal)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "NaN", "Infinity", "10.001", FAKE_VALUE])
def test_large_debit_warning_amount_refuses_anything_else_without_echoing_it(value: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate(_env(LOCAL_ENV, LARGE_DEBIT_WARNING_AMOUNT=value))
    message = str(raised.value)
    assert "LARGE_DEBIT_WARNING_AMOUNT" in message
    assert FAKE_VALUE not in message


def test_settings_expose_the_threshold_as_a_decimal() -> None:
    assert isinstance(settings.LARGE_DEBIT_WARNING_AMOUNT, Decimal)
    assert settings.LARGE_DEBIT_WARNING_AMOUNT > 0
