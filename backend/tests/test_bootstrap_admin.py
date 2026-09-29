"""bootstrap_admin: first admin on a deployed environment, hidden prompt (PLAN-003c)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django_otp import devices_for_user
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.management.commands import bootstrap_admin
from accounts.roles import Role
from tests.auth_helpers import ensure_role_groups, make_user

pytestmark = pytest.mark.django_db

PASSWORD = "Fake-Test-Passphrase-9143"
EMAIL = "admin@example.com"
LOGGER = "accounts.bootstrap_admin"

DEPLOYED_ENV = {
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "STAX_API_KEY": "FAKE-TEST-KEY-do-not-use-XYZ123",
    "STAX_WEBHOOK_URL_TOKEN": "t" * 64,
    # Unreachable on purpose: a refusal must happen before any database access.
    "DATABASE_URL": "postgres://finance@db.example.com:5432/finance",
    "APP_BASE_URL": "https://staging.example.com",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}


@pytest.fixture(autouse=True)
def _staging() -> Iterator[None]:
    ensure_role_groups()
    with override_settings(APP_ENV="staging"):
        yield


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap_admin, "stdin_is_terminal", lambda: True)


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Make the hidden prompt return ``answers`` in order; returns the prompts shown."""
    remaining = list(answers)
    prompts: list[str] = []

    def fake_getpass(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        return remaining.pop(0)

    monkeypatch.setattr(bootstrap_admin, "getpass", fake_getpass)
    return prompts


def _run(email: str = EMAIL) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("bootstrap_admin", "--email", email, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def _no_user() -> bool:
    return not User.objects.filter(username=EMAIL).exists()


@pytest.mark.usefixtures("terminal")
def test_creates_the_first_admin_without_a_device(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    prompts = _answers(monkeypatch, PASSWORD, PASSWORD)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out, err = _run("  Admin@Example.COM ")

    user = User.objects.get(username=EMAIL)
    assert user.email == EMAIL
    assert user.is_active and user.is_staff
    assert not user.is_superuser
    assert list(user.groups.values_list("name", flat=True)) == [Role.ADMIN.value]
    assert user.check_password(PASSWORD)
    assert not TOTPDevice.objects.filter(user=user).exists()
    assert not StaticDevice.objects.filter(user=user).exists()
    assert list(devices_for_user(user, confirmed=None)) == []
    assert len(prompts) == 2

    lines = out.strip().splitlines()
    assert len(lines) == 1
    assert EMAIL in lines[0]
    assert "/account/login/" in lines[0]
    assert "authenticator" in lines[0]
    assert err == ""

    records = [record for record in caplog.records if record.name == LOGGER]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert EMAIL in records[0].getMessage()


@pytest.mark.usefixtures("terminal")
def test_password_appears_in_no_output_log_or_argument(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _answers(monkeypatch, PASSWORD, PASSWORD)
    with caplog.at_level(logging.DEBUG):
        out, err = _run()
    captured = capsys.readouterr()
    for text in (out, err, captured.out, captured.err, caplog.text):
        assert PASSWORD not in text
    for record in caplog.records:
        assert PASSWORD not in record.getMessage()
    assert all(PASSWORD not in argument for argument in sys.argv)


def test_has_no_password_argument() -> None:
    parser = bootstrap_admin.Command().create_parser("manage.py", "bootstrap_admin")
    options = {option for action in parser._actions for option in action.option_strings}
    assert "--email" in options
    assert not any("pass" in option for option in options)
    assert not any(action.dest.startswith("pass") for action in parser._actions)
    assert "--noinput" not in options and "--no-input" not in options
    with pytest.raises(TypeError):
        call_command("bootstrap_admin", email=EMAIL, password=PASSWORD, stdout=StringIO())
    assert _no_user()


def test_ignores_a_password_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PASSWORD", "ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_PASSWORD", "DJANGO_PASSWORD"):
        monkeypatch.setenv(name, PASSWORD)
    monkeypatch.setattr(bootstrap_admin, "stdin_is_terminal", lambda: False)
    with pytest.raises(CommandError, match="interactive terminal"):
        _run()
    assert _no_user()


@pytest.mark.usefixtures("terminal")
def test_second_run_is_refused_with_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, PASSWORD, PASSWORD)
    _run()
    _answers(monkeypatch, PASSWORD, PASSWORD)
    with pytest.raises(CommandError) as raised:
        _run("second@example.com")
    assert raised.value.returncode == 2
    assert PASSWORD not in str(raised.value)
    assert not User.objects.filter(username="second@example.com").exists()
    assert User.objects.count() == 1


@pytest.mark.usefixtures("terminal")
def test_refused_when_any_admin_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    make_user("existing-admin", Role.ADMIN)
    _answers(monkeypatch, PASSWORD, PASSWORD)
    with pytest.raises(CommandError) as raised:
        _run()
    assert raised.value.returncode == 2
    assert _no_user()


@pytest.mark.usefixtures("terminal")
def test_password_mismatch_creates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _answers(monkeypatch, PASSWORD, PASSWORD + "x")
    with pytest.raises(CommandError, match="did not match") as raised:
        _run()
    assert PASSWORD not in str(raised.value)
    assert raised.value.returncode != 0
    assert len(prompts) == 2  # one attempt, no retry loop
    assert _no_user()


@pytest.mark.usefixtures("terminal")
@pytest.mark.parametrize("weak", ["12345678", "Qz7-x", "", "password123", "admin@example.com1"])
def test_weak_password_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, weak: str
) -> None:
    prompts = _answers(monkeypatch, weak, weak)
    with pytest.raises(CommandError, match="rejected") as raised:
        _run()
    if weak:
        assert weak not in str(raised.value)
    assert raised.value.returncode != 0
    assert len(prompts) == 2
    assert _no_user()
    assert not Group.objects.get(name=Role.ADMIN.value).user_set.exists()


def test_no_terminal_is_refused_before_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap_admin, "stdin_is_terminal", lambda: False)
    prompts = _answers(monkeypatch, PASSWORD, PASSWORD)
    with pytest.raises(CommandError, match="interactive terminal") as raised:
        _run()
    assert raised.value.returncode != 0
    assert prompts == []
    assert _no_user()


def test_terminal_check_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO(PASSWORD))
    assert bootstrap_admin.stdin_is_terminal() is False
    monkeypatch.setattr(sys, "stdin", None)
    assert bootstrap_admin.stdin_is_terminal() is False


@pytest.mark.usefixtures("terminal")
@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-email",
        "admin@",
        "@example.com",
        "a b@example.com",
        "admin@example.com\nx",
        ("a" * 140) + "@example.com",
    ],
)
def test_invalid_email_is_refused_before_prompting(
    monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    prompts = _answers(monkeypatch, PASSWORD, PASSWORD)
    with pytest.raises(CommandError, match="--email") as raised:
        _run(email)
    assert raised.value.returncode != 0
    assert prompts == []
    assert User.objects.count() == 0


def test_email_is_required() -> None:
    with pytest.raises(CommandError, match="--email"):
        call_command("bootstrap_admin", stdout=StringIO(), stderr=StringIO())


@pytest.mark.usefixtures("terminal")
@pytest.mark.parametrize(
    ("username", "email"),
    [
        ("admin@example.com", "someone@example.com"),
        ("someone", "admin@example.com"),
        ("ADMIN@EXAMPLE.COM", "other@example.com"),
        ("other", "Admin@Example.com"),
    ],
)
def test_existing_user_with_the_same_email_is_not_elevated(
    monkeypatch: pytest.MonkeyPatch, username: str, email: str
) -> None:
    existing = User.objects.create_user(username=username, email=email, password=PASSWORD)
    existing.groups.add(Group.objects.get(name=Role.VIEWER.value))
    _answers(monkeypatch, PASSWORD, PASSWORD)
    with pytest.raises(CommandError, match="already exists") as raised:
        _run()
    assert raised.value.returncode != 0
    existing.refresh_from_db()
    assert not existing.is_staff and not existing.is_superuser
    assert list(existing.groups.values_list("name", flat=True)) == [Role.VIEWER.value]
    assert User.objects.count() == 1
    assert not Group.objects.get(name=Role.ADMIN.value).user_set.exists()


@pytest.mark.usefixtures("terminal")
def test_refused_in_local_pointing_to_seed_local_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _answers(monkeypatch, PASSWORD, PASSWORD)
    with override_settings(APP_ENV="local"), pytest.raises(CommandError) as raised:
        _run()
    assert raised.value.returncode == 2
    assert "seed_local_admin" in str(raised.value)
    assert prompts == []
    assert _no_user()


@pytest.mark.usefixtures("terminal")
def test_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, PASSWORD, PASSWORD)
    with override_settings(APP_ENV="production"):
        _run()
    assert User.objects.filter(username=EMAIL, is_staff=True).exists()


@pytest.mark.usefixtures("terminal")
def test_created_admin_cannot_reach_admin_before_enrolment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answers(monkeypatch, PASSWORD, PASSWORD)
    _run()
    client = Client()
    client.force_login(User.objects.get(username=EMAIL))  # password only, no device
    for path in ("/admin/", "/admin/auth/user/"):
        assert client.get(path).status_code == 403, path
    assert client.post("/admin/auth/user/add/", {"username": "x"}).status_code == 403
    assert client.get("/account/two_factor/setup/").status_code == 200


def test_without_a_terminal_a_deployed_process_exits_non_zero(repo_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, "backend/manage.py", "bootstrap_admin", "--email", EMAIL],
        cwd=repo_root,
        env={**DEPLOYED_ENV, "APP_ENV": "staging", "PATH": os.environ.get("PATH", "")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "interactive terminal" in result.stderr
    assert result.stdout == ""


def test_local_process_exits_2(repo_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, "backend/manage.py", "bootstrap_admin", "--email", EMAIL],
        cwd=repo_root,
        env={**DEPLOYED_ENV, "APP_ENV": "local", "PATH": os.environ.get("PATH", "")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "seed_local_admin" in result.stderr
