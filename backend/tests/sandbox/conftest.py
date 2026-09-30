"""Sandbox suites: real calls to the Stax sandbox, never part of a default run.

docs/qa/PLAN-005-stax-client.md §4.5. Two independent locks:

1. Deselection. A test marked ``sandbox`` is deselected unless the ``-m`` expression
   names ``sandbox`` (``uv run pytest -m sandbox``). The repository's documented
   commands (``-m "not e2e"`` and ``-m e2e``) therefore never collect these tests.
2. Enabling variables. Selected without ``STAX_SANDBOX_TESTS=1``, ``STAX_ENV=sandbox``,
   a non-empty ``STAX_SANDBOX_API_KEY`` (and each suite's other variables), every test
   here skips.

Nothing in this directory runs against ``STAX_ENV=live``.
"""

from __future__ import annotations

import os

import pytest
from django.conf import settings

from payments.stax.adapter import StaxProvider
from payments.stax.transport import StaxTransport

SANDBOX_MARKER = "sandbox"
SANDBOX_KEY_VARIABLE = "STAX_SANDBOX_API_KEY"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if SANDBOX_MARKER in (config.getoption("markexpr") or ""):
        return
    deselected = [item for item in items if item.get_closest_marker(SANDBOX_MARKER)]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if not item.get_closest_marker(SANDBOX_MARKER)]


def sandbox_skip_reason(*extra_variables: str) -> str | None:
    """Why the sandbox suite cannot run here, or ``None`` when it may."""
    if os.environ.get("STAX_SANDBOX_TESTS") != "1":
        return "STAX_SANDBOX_TESTS is not 1"
    if settings.STAX_ENV != "sandbox":
        return "STAX_ENV is not sandbox"
    # backend/tests/conftest.py replaces STAX_API_KEY with a fixed fake before settings
    # load, so the real sandbox key arrives through STAX_SANDBOX_API_KEY instead.
    if not os.environ.get(SANDBOX_KEY_VARIABLE):
        return f"{SANDBOX_KEY_VARIABLE} is not set"
    missing = [name for name in extra_variables if not os.environ.get(name)]
    if missing:
        return f"not set: {', '.join(missing)}"
    return None


def sandbox_provider() -> StaxProvider:
    """An adapter whose client authenticates with ``STAX_SANDBOX_API_KEY``.

    The key is read from the environment at call time, handed straight to the
    transport, and never stored in settings or printed.
    """
    return StaxProvider(StaxTransport(api_key=os.environ.get(SANDBOX_KEY_VARIABLE, "")))
