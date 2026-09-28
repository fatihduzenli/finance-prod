"""Fable's adversarial cases for the boot-time environment guard (PLAN-002 §5).

These run against the interface contract only: finance.env_guard.validate / EnvGuardError.
"""

from __future__ import annotations

import pytest

from finance.env_guard import EnvGuardError, validate

FAKE_KEY = "FAKE-LIVE-KEY-SHOULD-NEVER-APPEAR-9f8e7d6c"
TOKEN_64 = "t" * 64


def base(**overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "staging",
        "STAX_ENV": "sandbox",
        "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
        "STAX_API_KEY": FAKE_KEY,
        "STAX_WEBHOOK_URL_TOKEN": TOKEN_64,
        "DATABASE_URL": "postgres://u:p@db:5432/finance",
        "APP_BASE_URL": "https://staging.example.com",
        "DJANGO_SECRET_KEY": "x" * 50,
    }
    env.update(overrides)
    return env


def test_happy_path_staging_sandbox() -> None:
    cfg = validate(base())
    assert cfg.app_env == "staging"
    assert cfg.stax_env == "sandbox"


def test_production_may_run_sandbox_until_step_16() -> None:
    validate(base(APP_ENV="production"))


def test_production_live_is_allowed() -> None:
    validate(base(APP_ENV="production", STAX_ENV="live"))


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_live_outside_production_refused(app_env: str) -> None:
    with pytest.raises(EnvGuardError):
        validate(base(APP_ENV=app_env, STAX_ENV="live"))


@pytest.mark.parametrize(
    "stax_env",
    ["", " sandbox", "sandbox ", "SANDBOX", "Live", "prod", "production", "test", "sandbox\n"],
)
def test_stax_env_must_parse_exactly(stax_env: str) -> None:
    with pytest.raises(EnvGuardError):
        validate(base(STAX_ENV=stax_env))


def test_stax_env_missing_refused_in_every_app_env() -> None:
    for app_env in ("local", "staging", "production"):
        env = base(APP_ENV=app_env)
        del env["STAX_ENV"]
        with pytest.raises(EnvGuardError):
            validate(env)


@pytest.mark.parametrize("app_env", ["", "prod", "dev", "Production", "LOCAL", "stage"])
def test_app_env_must_parse_exactly(app_env: str) -> None:
    with pytest.raises(EnvGuardError):
        validate(base(APP_ENV=app_env))


def test_error_messages_never_contain_values() -> None:
    env = base(APP_ENV="staging", STAX_ENV="live")
    with pytest.raises(EnvGuardError) as exc:
        validate(env)
    text = str(exc.value)
    assert FAKE_KEY not in text
    assert TOKEN_64 not in text
    assert "staging.example.com" not in text


def test_webhook_token_boundary() -> None:
    with pytest.raises(EnvGuardError):
        validate(base(STAX_WEBHOOK_URL_TOKEN="t" * 63))
    validate(base(STAX_WEBHOOK_URL_TOKEN="t" * 64))


def test_webhook_token_missing_in_staging_refused() -> None:
    env = base()
    del env["STAX_WEBHOOK_URL_TOKEN"]
    with pytest.raises(EnvGuardError):
        validate(env)


@pytest.mark.parametrize(
    "url",
    [
        "http://apiprod.fattlabs.com",
        "https://apiprod.fattlabs.com/",
        "https://apiprod.fattlabs.com/v1",
        "https://apisandbox.fattlabs.com",
        "https://apiprod.fattlabs.com.evil.example",
        "",
    ],
)
def test_base_url_must_be_exact_outside_local(url: str) -> None:
    with pytest.raises(EnvGuardError):
        validate(base(STAX_API_BASE_URL=url))


def test_base_url_may_differ_in_local_for_fakes() -> None:
    validate(base(APP_ENV="local", STAX_API_BASE_URL="http://127.0.0.1:9999"))


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_sqlite_refused_outside_local(app_env: str) -> None:
    with pytest.raises(EnvGuardError):
        validate(base(APP_ENV=app_env, DATABASE_URL="sqlite:///tmp/x.db"))


def test_sqlite_allowed_in_local() -> None:
    validate(base(APP_ENV="local", DATABASE_URL="sqlite:///:memory:"))


def test_empty_api_key_refused_in_staging_and_production() -> None:
    for app_env in ("staging", "production"):
        with pytest.raises(EnvGuardError):
            validate(base(APP_ENV=app_env, STAX_API_KEY=""))


def test_empty_api_key_allowed_in_local() -> None:
    validate(base(APP_ENV="local", STAX_API_KEY=""))


def test_config_repr_does_not_leak_secrets() -> None:
    cfg = validate(base())
    assert FAKE_KEY not in repr(cfg)
    assert TOKEN_64 not in repr(cfg)
