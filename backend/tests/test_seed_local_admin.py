"""seed_local_admin: local only, password from the environment, TOTP printed (PLAN-003)."""

from __future__ import annotations

import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django_otp.plugins.otp_static.models import StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

pytestmark = pytest.mark.django_db

SEED_PASSWORD = "test-only-seed-password-not-secret-9"
EMAIL = "admin@example.com"

DEPLOYED_ENV = {
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "STAX_API_KEY": "FAKE-TEST-KEY-do-not-use-XYZ123",
    "STAX_WEBHOOK_URL_TOKEN": "t" * 64,
    # Unreachable on purpose: a refusal must happen before any database access.
    "DATABASE_URL": "postgres://finance@db.example.com:5432/finance",
    "APP_BASE_URL": "https://staging.example.com",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
    "LOCAL_ADMIN_PASSWORD": SEED_PASSWORD,
}


def _seed(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("LOCAL_ADMIN_PASSWORD", SEED_PASSWORD)
    out = StringIO()
    call_command("seed_local_admin", stdout=out)
    return out.getvalue()


def test_creates_a_staff_admin_with_confirmed_totp(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _seed(monkeypatch)
    user = User.objects.get(username=EMAIL)
    assert user.email == EMAIL
    assert user.is_staff and user.is_active
    assert not user.is_superuser
    assert list(user.groups.values_list("name", flat=True)) == ["admin"]
    assert user.check_password(SEED_PASSWORD)
    device = TOTPDevice.objects.get(user=user)
    assert device.confirmed and device.name == "default"
    tokens = list(StaticToken.objects.filter(device__user=user).values_list("token", flat=True))
    assert len(tokens) == 5

    assert "LOCAL ONLY" in output
    assert device.config_url in output
    assert output.count("otpauth://totp/") == 1
    for token in tokens:
        assert token in output
    assert SEED_PASSWORD not in output


def test_rerun_updates_instead_of_duplicating(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(monkeypatch)
    first_tokens = set(StaticToken.objects.values_list("token", flat=True))
    user = User.objects.get(username=EMAIL)
    user.is_superuser = True
    user.save()
    _seed(monkeypatch)
    assert User.objects.filter(username=EMAIL).count() == 1
    assert TOTPDevice.objects.count() == 1
    assert StaticToken.objects.count() == 5
    assert set(StaticToken.objects.values_list("token", flat=True)) != first_tokens
    assert not User.objects.get(username=EMAIL).is_superuser


@pytest.mark.parametrize("value", [None, ""])
def test_requires_the_password_variable(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("LOCAL_ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("LOCAL_ADMIN_PASSWORD", value)
    with pytest.raises(CommandError, match="LOCAL_ADMIN_PASSWORD"):
        call_command("seed_local_admin", stdout=StringIO())
    assert not User.objects.filter(username=EMAIL).exists()


def test_rejects_a_weak_password_without_echoing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_ADMIN_PASSWORD", "12345678")
    with pytest.raises(CommandError, match="rejected") as raised:
        call_command("seed_local_admin", stdout=StringIO())
    assert "12345678" not in str(raised.value)
    assert not User.objects.filter(username=EMAIL).exists()


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_refused_in_process_outside_local(monkeypatch: pytest.MonkeyPatch, app_env: str) -> None:
    monkeypatch.setenv("LOCAL_ADMIN_PASSWORD", SEED_PASSWORD)
    with override_settings(APP_ENV=app_env), pytest.raises(CommandError) as raised:
        call_command("seed_local_admin", stdout=StringIO())
    assert raised.value.returncode == 2
    assert not User.objects.filter(username=EMAIL).exists()


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_refused_with_exit_2_in_a_deployed_process(repo_root: Path, app_env: str) -> None:
    result = subprocess.run(
        [sys.executable, "backend/manage.py", "seed_local_admin"],
        cwd=repo_root,
        env={**DEPLOYED_ENV, "APP_ENV": app_env, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "APP_ENV=local" in result.stderr
    # Exit 2, not the database error exit 1: nothing was attempted against the database.
    assert "otpauth://" not in result.stdout
    assert SEED_PASSWORD not in result.stdout + result.stderr
