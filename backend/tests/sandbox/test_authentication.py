"""Sandbox: the authentication check (PLAN-005 §4.5). One GET /self, no retry."""

from __future__ import annotations

import pytest
from django.test import override_settings

from tests.sandbox.conftest import sandbox_provider, sandbox_skip_reason

_reason = sandbox_skip_reason()
pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.django_db,
    pytest.mark.skipif(_reason is not None, reason=_reason or ""),
]


def test_ping_returns_an_identity() -> None:
    from django.conf import settings

    assert settings.STAX_ENV == "sandbox"
    with override_settings(STAX_CALLER="worker"):
        identity = sandbox_provider().ping()
    assert identity.provider_name == "stax"
    assert identity.environment == "sandbox"
    assert identity.merchant_id
