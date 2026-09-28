"""Deployed-environment settings: HTTPS is forced, /healthz is not redirected.

The probe below imports ``finance.settings`` in a fresh process, because the settings
module is imported once per process and the suite already runs it with APP_ENV=local.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from django.test import Client, override_settings

FAKE_KEY = "FAKE-TEST-KEY-do-not-use-XYZ123"

STAGING_ENV = {
    "APP_ENV": "staging",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "STAX_API_KEY": FAKE_KEY,
    "STAX_WEBHOOK_URL_TOKEN": "t" * 64,
    "DATABASE_URL": "postgres://finance@db.example.com:5432/finance",
    "APP_BASE_URL": "https://staging.example.com",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}

LOCAL_ENV = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}

_PROBE = """
import json

from django.conf import settings

print(
    json.dumps(
        {
            "debug": settings.DEBUG,
            "ssl_redirect": settings.SECURE_SSL_REDIRECT,
            "redirect_exempt": list(settings.SECURE_REDIRECT_EXEMPT),
            "allowed_hosts": list(settings.ALLOWED_HOSTS),
        }
    )
)
"""


def _settings_under(repo_root: Path, env: dict[str, str]) -> dict[str, object]:
    """Import the settings module in a fresh process with exactly ``env`` set."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=repo_root,
        env={
            **env,
            "DJANGO_SETTINGS_MODULE": "finance.settings",
            "PYTHONPATH": "backend",
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parsed: dict[str, object] = json.loads(result.stdout)
    return parsed


def test_staging_forces_https_and_exempts_healthz(repo_root: Path) -> None:
    settings_values = _settings_under(repo_root, STAGING_ENV)
    assert settings_values["debug"] is False
    assert settings_values["ssl_redirect"] is True
    assert settings_values["redirect_exempt"] == ["^healthz$"]
    assert settings_values["allowed_hosts"] == ["staging.example.com"]


def test_local_neither_forces_https_nor_exempts_anything(repo_root: Path) -> None:
    settings_values = _settings_under(repo_root, LOCAL_ENV)
    assert settings_values["ssl_redirect"] is False
    assert settings_values["redirect_exempt"] == []


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz$"])
def test_healthz_answers_over_plain_http_while_other_paths_redirect() -> None:
    # A Client built inside the override loads the middleware with these settings.
    client = Client()

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health["Content-Type"].startswith("application/json")

    redirected = client.get("/admin/login/")
    assert redirected.status_code == 301
    assert redirected["Location"].startswith("https://")


@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz$"])
def test_the_exemption_does_not_cover_lookalike_paths() -> None:
    client = Client()
    for path in ("/healthz/", "/healthzz"):
        assert client.get(path).status_code == 301
