"""Shared test setup.

pytest-django imports ``finance.settings`` while it starts up, and that import
validates the environment (``finance.env_guard``). The defaults below are therefore
applied at conftest import time, before Django is configured.

Every value here is an obvious placeholder. ``STAX_API_KEY`` and ``SENTRY_DSN`` are
forced rather than defaulted: a test run must never pick up a real key from the shell
and must never be able to talk to a real error tracker.
"""

from __future__ import annotations

import os
from pathlib import Path

import django
import pytest

#: Repository root (this file is backend/tests/conftest.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Obviously fake, distinctive, and safe to grep for in a served asset.
FAKE_STAX_API_KEY = "FAKE-TEST-KEY-do-not-use-XYZ123"

_ENV_DEFAULTS = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}

for _name, _value in _ENV_DEFAULTS.items():
    os.environ.setdefault(_name, _value)

os.environ["STAX_API_KEY"] = FAKE_STAX_API_KEY
os.environ["DJANGO_DEBUG"] = "0"
os.environ.pop("SENTRY_DSN", None)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "finance.settings")

# pytest-django configures Django while it loads initial conftests, which happens
# before this file is imported, so it finds no settings module and skips that step.
# Setting the variables above and then calling django.setup() here is what makes the
# placeholder environment, rather than the developer's shell, the one under test.
django.setup()


@pytest.fixture
def fake_stax_api_key() -> str:
    """The fake key the test environment runs with."""
    return FAKE_STAX_API_KEY


@pytest.fixture
def repo_root() -> Path:
    """Repository root, for tests that run a repository script."""
    return REPO_ROOT
