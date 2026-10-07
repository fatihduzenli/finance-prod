"""Provider portability (docs/PROVIDER.md, Prompt 12 item 7), written by the QA owner.

The Stax adapter is reachable only through ``payments.PaymentProvider``: no module outside
``payments/`` imports ``payments.stax``; inside ``payments/`` only the listed modules do; both
the adapter and the fake implement the whole interface; the ledger names no Stax status or
field."""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from payments import fake as fake_module
from payments import provider as provider_module
from payments.stax import adapter as adapter_module

BACKEND = Path(__file__).resolve().parents[1]

#: The modules inside ``payments/`` that may import the adapter package (docs/PROVIDER.md §1).
ALLOWED_INSIDE_PAYMENTS = {
    "payments/__init__.py",
    "payments/health.py",
    "payments/offline_marking.py",
    "payments/operator_sync.py",
    "payments/timezone_audit.py",
    "payments/reconciliation/__init__.py",
    "payments/management/commands/provider_environment_check.py",
    "payments/management/commands/provider_ping.py",
    "payments/management/commands/reconcile.py",
    "payments/management/commands/worker.py",
}


def _imports_stax(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("payments.stax"):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name.startswith("payments.stax") for alias in node.names
        ):
            return True
    return False


def _sources() -> list[Path]:
    return [
        p
        for p in BACKEND.rglob("*.py")
        if "tests" not in p.relative_to(BACKEND).parts
        and "migrations" not in p.relative_to(BACKEND).parts
        and "__pycache__" not in p.parts
    ]


def test_no_module_outside_payments_imports_the_stax_adapter() -> None:
    offenders = [
        str(p.relative_to(BACKEND))
        for p in _sources()
        if p.relative_to(BACKEND).parts[0] != "payments" and _imports_stax(p)
    ]
    assert offenders == []


def test_inside_payments_only_the_listed_modules_import_the_adapter_package() -> None:
    importers = {
        str(p.relative_to(BACKEND))
        for p in _sources()
        if p.relative_to(BACKEND).parts[0] == "payments"
        and p.relative_to(BACKEND).parts[1] != "stax"
        and _imports_stax(p)
    }
    assert importers <= ALLOWED_INSIDE_PAYMENTS, importers - ALLOWED_INSIDE_PAYMENTS
    # The list in docs/PROVIDER.md must not go stale either: every listed module imports it.
    assert ALLOWED_INSIDE_PAYMENTS <= importers, ALLOWED_INSIDE_PAYMENTS - importers


def _interface_methods() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(provider_module.PaymentProvider)
        if not name.startswith("_") and callable(member)
    }


@pytest.mark.parametrize("implementation", [adapter_module.StaxProvider, fake_module.FakeProvider])
def test_every_interface_method_is_implemented(implementation: type) -> None:
    missing = {
        name for name in _interface_methods() if not callable(getattr(implementation, name, None))
    }
    assert missing == set()


STAX_STATUS_STRINGS = re.compile(
    r"""(["'])(PARTIALLY APPLIED|FAKE-PENDING|apiprod\.fattlabs\.com|lineItems|"""
    r"""invoice_date_at|due_at|isACHPaymentEnabled)\1"""
)


def test_the_ledger_names_no_stax_status_or_field_string() -> None:
    """The ledger reads statuses as opaque strings and fields through our dataclasses; the
    one provider value it filters on, ``ATTEMPTED`` (PLAN-011 D1), is documented there."""
    # seed_demo writes demo provider data on purpose (allow-listed in test_stax_isolation).
    offenders = [
        f"{path.relative_to(BACKEND)}: {match.group(2)}"
        for path in _sources()
        if path.relative_to(BACKEND).parts[0] == "ledger" and path.name != "seed_demo.py"
        for match in STAX_STATUS_STRINGS.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
