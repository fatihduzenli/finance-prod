"""The boot-time environment check and its configuration (PLAN-006 §6 row 9, D1, D2).

Every Stax reply here comes from an ``httpx.MockTransport`` (tests/stax_helpers.py);
nothing opens a socket.
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path
from unittest import mock

import httpx
import pytest
from django.core.management import call_command
from django.test import override_settings

import payments
from finance.env_guard import EnvGuardError, validate
from payments.errors import ProviderNotPermittedHere
from payments.fake import FakeProvider
from payments.management.commands import provider_environment_check as command
from payments.stax.adapter import StaxProvider
from payments.stax.transport import Method
from tests.stax_helpers import Rig, always, make_rig

MERCHANT = "ea824f2b-0000-0000-0000-000000000000"
OTHER_MERCHANT = "00000000-0000-0000-0000-999999999999"
BACKEND = Path(__file__).resolve().parents[1]


def self_reply(*, is_sandbox: object = True, merchant: str = MERCHANT) -> dict[str, object]:
    body: dict[str, object] = {"id": merchant, "company_name": "Example Co"}
    if is_sandbox is not None:
        body["is_sandbox"] = is_sandbox
    return {"merchant": body}


def run(rig: Rig | None) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    factory = (lambda: rig.transport) if rig is not None else _no_transport
    with mock.patch.object(command, "StaxTransport", factory):
        try:
            call_command("provider_environment_check", stdout=stdout, stderr=stderr)
            code = 0
        except SystemExit as exit_:
            code = int(exit_.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def _no_transport() -> None:
    raise AssertionError("no request may be made on this path")


# --- Row 9: exit codes ------------------------------------------------------------------------


@pytest.mark.parametrize(("expected", "verdict"), [(MERCHANT, "yes"), ("", "not set")])
def test_sandbox_key_with_sandbox_env_starts(expected: str, verdict: str) -> None:
    rig = make_rig(always(200, self_reply()))
    with override_settings(STAX_ENV="sandbox", STAX_EXPECTED_MERCHANT_ID=expected):
        code, out, err = run(rig)
    assert code == 0, err
    assert out.splitlines() == [
        "environment: sandbox",
        "merchant: ea82…0000",
        "merchant is_sandbox: True",
        f"merchant matches expected: {verdict}",
        "provider environment check: ok",
    ]
    assert MERCHANT not in out + err
    [request] = rig.recorder.requests
    assert (request.method, request.url.path) == ("GET", "/self")


def test_live_key_with_live_env_starts() -> None:
    rig = make_rig(always(200, self_reply(is_sandbox=False)))
    with override_settings(STAX_ENV="live", STAX_EXPECTED_MERCHANT_ID=""):
        code, _, err = run(rig)
    assert code == 0, err


@pytest.mark.parametrize(
    ("stax_env", "flag"),
    [
        ("sandbox", False),
        ("live", True),
        ("sandbox", None),  # missing: unknown is not agreement
        ("sandbox", "true"),  # a string is not a boolean
        ("live", 0),
    ],
)
def test_environment_disagreement_exits_3(stax_env: str, flag: object) -> None:
    rig = make_rig(always(200, self_reply(is_sandbox=flag)))
    with override_settings(STAX_ENV=stax_env, STAX_EXPECTED_MERCHANT_ID=MERCHANT):
        code, out, err = run(rig)
    assert code == 3
    assert "does not match STAX_ENV" in err
    assert "ok" not in out
    assert len(rig.recorder.requests) == 1


def test_environment_is_checked_before_the_merchant() -> None:
    rig = make_rig(always(200, self_reply(is_sandbox=False, merchant=OTHER_MERCHANT)))
    with override_settings(STAX_ENV="sandbox", STAX_EXPECTED_MERCHANT_ID=MERCHANT):
        code, _, _ = run(rig)
    assert code == 3


def test_expected_merchant_mismatch_exits_1() -> None:
    rig = make_rig(always(200, self_reply(merchant=OTHER_MERCHANT)))
    with override_settings(STAX_ENV="sandbox", STAX_EXPECTED_MERCHANT_ID=MERCHANT):
        code, out, err = run(rig)
    assert code == 1
    assert "different merchant" in err
    assert OTHER_MERCHANT not in out + err
    assert MERCHANT not in out + err
    assert "0000…9999" in out


@pytest.mark.parametrize(
    ("reply", "name", "requests"),
    [
        (httpx.ConnectError("refused"), "TransportError", 4),
        (httpx.Response(500, json={"message": "BODY-CANARY"}), "ProviderUnavailable", 4),
        (httpx.Response(401, json={"message": "BODY-CANARY"}), "ProviderRejected", 1),
        (httpx.Response(200, content=b"not json"), "ProviderUnavailable", 1),
        (httpx.Response(200, json={"merchant": {}}), "ProviderUnavailable", 1),
    ],
    ids=["connect-error", "500", "401", "not-json", "no-merchant-id"],
)
def test_unreachable_or_unreadable_exits_2_with_the_class_name_only(
    reply: httpx.Response | Exception, name: str, requests: int, fake_stax_api_key: str
) -> None:
    rig = make_rig(lambda request: reply)
    code, out, err = run(rig)
    assert code == 2
    assert out == ""
    assert err.strip() == f"provider environment check failed: {name}"
    assert "BODY-CANARY" not in out + err
    assert fake_stax_api_key not in out + err
    # The command sends once; only the transport's own policy for reads repeats a
    # network error or a 5xx (PLAN-005 D6). A 4xx is never repeated.
    assert len(rig.recorder.requests) == requests


def test_local_without_a_key_is_skipped_with_no_request() -> None:
    with override_settings(APP_ENV="local", STAX_API_KEY=""):
        code, out, err = run(None)
    assert code == 0
    assert out.strip() == "provider environment check: skipped (local, no key)"
    assert err == ""


def test_deployed_without_a_key_refuses_to_start() -> None:
    with override_settings(APP_ENV="staging", STAX_API_KEY=""):
        code, _, err = run(None)
    assert code == 2
    assert "no key" in err


# --- Row 9: the STAX_CALLER gate is bypassed on this path only --------------------------------


def test_the_check_runs_although_this_process_is_not_the_caller() -> None:
    rig = make_rig(always(200, self_reply()))
    with override_settings(STAX_CALLER="none", STAX_EXPECTED_MERCHANT_ID=""):
        code, _, err = run(rig)
        assert code == 0, err
        assert len(rig.recorder.requests) == 1
        # Every other way to the same endpoint is still refused before the network.
        with pytest.raises(ProviderNotPermittedHere):
            rig.transport.send(Method.GET, "/self", idempotent=True)
        with pytest.raises(ProviderNotPermittedHere):
            StaxProvider(rig.transport).ping()
    assert len(rig.recorder.requests) == 1


def test_boot_check_bypasses_only_the_caller_gate_not_the_breaker() -> None:
    rig = make_rig(always(401, {}))
    with override_settings(STAX_CALLER="none"):
        for _ in range(3):  # three 4xx open the breaker (PLAN-005 §3)
            run(rig)
        code, _, err = run(rig)
    assert code == 2
    assert err.strip() == "provider environment check failed: CircuitOpen"
    assert len(rig.recorder.requests) == 3


def test_only_the_boot_check_passes_boot_check_true() -> None:
    callers = []
    for path in BACKEND.rglob("*.py"):
        if path.relative_to(BACKEND).parts[0] == "tests":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "boot_check":
                callers.append(str(path.relative_to(BACKEND)))
    assert callers == ["payments/management/commands/provider_environment_check.py"]


# --- D1, D2: image and infrastructure ------------------------------------------------------------


def test_the_image_runs_the_check_after_migrations_and_before_gunicorn(repo_root: Path) -> None:
    [cmd] = [
        line
        for line in (repo_root / "Dockerfile").read_text().splitlines()
        if line.startswith("CMD ")
    ]
    assert (
        "python backend/manage.py deploy_migrate && "
        "python backend/manage.py provider_environment_check && exec gunicorn "
    ) in cmd
    assert ";" not in cmd and "||" not in cmd
    # One process owns the rate limiter and breaker (D1): threads, never extra workers.
    assert " --workers 1 --worker-class gthread --threads 4 " in cmd


def test_staging_makes_the_single_web_replica_the_caller(repo_root: Path) -> None:
    params = (repo_root / "infra/parameters/staging.bicepparam").read_text()
    assert re.search(r"^param staxCaller = 'worker'$", params, re.MULTILINE)
    assert re.search(r"^param webMaxReplicas = 1$", params, re.MULTILINE)
    assert re.search(r"^param staxExpectedMerchantId = '[0-9a-f-]{36}'$", params, re.MULTILINE)
    main = (repo_root / "infra/main.bicep").read_text()
    assert "param staxCaller string = 'none'" in main
    assert "param staxExpectedMerchantId string = ''" in main
    web = (repo_root / "infra/modules/containerAppWeb.bicep").read_text()
    for name, param in (
        ("STAX_CALLER", "staxCaller"),
        ("STAX_EXPECTED_MERCHANT_ID", "staxExpectedMerchantId"),
    ):
        assert f"name: '{name}'\n    value: {param}\n" in web


# --- STAX_PROVIDER and get_provider (§4.2) -----------------------------------------------------

LOCAL_ENV = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}


def test_stax_provider_defaults_to_stax_and_accepts_fake_locally() -> None:
    assert validate(LOCAL_ENV).stax_provider == "stax"
    assert validate({**LOCAL_ENV, "STAX_PROVIDER": "stax"}).stax_provider == "stax"
    assert validate({**LOCAL_ENV, "STAX_PROVIDER": "fake"}).stax_provider == "fake"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_the_fake_provider_is_refused_outside_local(app_env: str) -> None:
    env = {
        **LOCAL_ENV,
        "APP_ENV": app_env,
        "DATABASE_URL": "postgres://placeholder@example.com/db",
        "STAX_API_KEY": "FAKE-KEY",
        "STAX_WEBHOOK_URL_TOKEN": "x" * 64,
        "STAX_PROVIDER": "fake",
    }
    with pytest.raises(EnvGuardError) as raised:
        validate(env)
    assert "STAX_PROVIDER may only be 'fake' when APP_ENV is 'local'" in str(raised.value)


@pytest.mark.parametrize("value", ["Fake", "memory", "fake ", "FAKE-CANARY-do-not-echo"])
def test_other_provider_values_are_refused_without_echo(value: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({**LOCAL_ENV, "STAX_PROVIDER": value})
    assert "STAX_PROVIDER" in str(raised.value)
    assert value not in str(raised.value)


def test_env_example_documents_stax_provider_empty(repo_root: Path) -> None:
    assert "STAX_PROVIDER=" in (repo_root / ".env.example").read_text().splitlines()


@pytest.fixture
def fresh_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payments, "_providers", {})


@pytest.mark.usefixtures("fresh_providers")
def test_get_provider_returns_one_adapter_per_process_without_a_request() -> None:
    with override_settings(STAX_PROVIDER="stax"):
        provider = payments.get_provider()
        assert isinstance(provider, StaxProvider)
        assert payments.get_provider() is provider


@pytest.mark.usefixtures("fresh_providers")
def test_get_provider_returns_the_fake_when_selected() -> None:
    with override_settings(STAX_PROVIDER="fake"):
        provider = payments.get_provider()
        assert isinstance(provider, FakeProvider)
        assert payments.get_provider() is provider
