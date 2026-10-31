"""Isolation of the Stax adapter (PLAN-005 §6 row 12, docs/CLAUDE.md Rule 8).

1. No module outside ``backend/payments/`` imports ``payments.stax``.
2. No file under ``backend/`` outside ``backend/payments/`` and ``backend/tests/``
   contains the string ``stax`` (any case), except the files in :data:`ALLOWED`, each
   with its reason. The ledger's ``payment_channel`` value ``"stax"`` is allowed only
   as that quoted value.

Scope: every file under ``backend/`` (Python, templates, static assets, text), skipping
``__pycache__`` and the ``staticfiles`` collectstatic output. Repository files outside
``backend/`` (infra, CI, Dockerfile, docs) are deployment and documentation, not app code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
#: Top-level packages outside the rule, and generated directories skipped at any depth.
EXEMPT_PACKAGES = ("payments", "tests")
GENERATED_DIRECTORIES = {"__pycache__", "staticfiles"}
STAX = re.compile("stax", re.IGNORECASE)

#: File (relative to backend/) -> why it may mention Stax.
ALLOWED: dict[str, str] = {
    "finance/settings.py": "settings (PLAN-005 §6 row 12)",
    "finance/env_guard.py": "settings validation (PLAN-005 §6 row 12)",
    "finance/middleware.py": "the sandbox banner (PLAN-005 §6 row 12)",
    "finance/log_redaction.py": "redacts the STAX_* secret values from log lines (PLAN-003a)",
    "finance/sentry.py": "tags events with STAX_ENV and scrubs Stax credentials (PLAN-002)",
    "finance/urls.py": "/healthz reports STAX_ENV (PLAN-002)",
    "ledger/models.py": "payment_channel value (PLAN-005 §6 row 12)",
    "ledger/migrations/0001_initial.py": "payment_channel value (PLAN-005 §6 row 12)",
    "ledger/management/commands/seed_demo.py": "demo ExternalRef provider name (PLAN-004)",
    "ledger/templates/ledger/documents/_notice.html": (
        "the upload notice says bank forms stay in Stax (PLAN-010c D6, verbatim)"
    ),
    # PLAN-012c D2: the processor's display name is data in the company settings row; these
    # two hold its first value (the seed, and the fallback when the row is missing).
    "ledger/company_defaults.py": "the company settings row's seed values (PLAN-012c D2)",
    "ledger/migrations/0045_company_settings.py": "seeds the company settings row (PLAN-012c D2)",
    # PLAN-014 C7: the bank-entry page is the one page that runs the processor's own script
    # (its hosted bank-account fields); the template and its script name Stax.js.
    "ledger/templates/ledger/leases/bank_entry.html": (
        "the bank-entry page loads Stax.js, the processor's hosted fields (PLAN-014 C7)"
    ),
    "ledger/static/ledger/bank_entry.js": (
        "runs Stax.js's hosted bank-account fields on the bank-entry page (PLAN-014 C7)"
    ),
}
#: In these files the only permitted mention is the quoted payment_channel value.
CHANNEL_VALUE_ONLY = {"ledger/models.py", "ledger/migrations/0001_initial.py"}
CHANNEL_VALUE = re.compile(r"""(["'])stax\1""")


def _files() -> list[Path]:
    return [
        path
        for path in BACKEND.rglob("*")
        if path.is_file()
        and path.relative_to(BACKEND).parts[0] not in EXEMPT_PACKAGES
        and not GENERATED_DIRECTORIES.intersection(path.relative_to(BACKEND).parts[:-1])
    ]


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def test_no_file_outside_payments_mentions_stax_except_the_allowed_ones() -> None:
    offenders = sorted(
        str(path.relative_to(BACKEND))
        for path in _files()
        if STAX.search(_text(path)) and str(path.relative_to(BACKEND)) not in ALLOWED
    )
    assert offenders == []


def test_the_ledger_mentions_stax_only_as_the_channel_value() -> None:
    for relative in CHANNEL_VALUE_ONLY:
        remainder = CHANNEL_VALUE.sub("", _text(BACKEND / relative))
        assert not STAX.search(remainder), relative


def test_every_allowed_file_exists_and_still_needs_its_exception() -> None:
    for relative in ALLOWED:
        assert STAX.search(_text(BACKEND / relative)), f"{relative} no longer mentions Stax"


def test_the_scan_sees_the_whole_backend() -> None:
    scanned = {str(path.relative_to(BACKEND)) for path in _files()}
    assert "finance/settings.py" in scanned
    assert "finance/templates/finance/banner.html" in scanned
    assert not any(name.startswith(("payments/", "tests/")) for name in scanned)


def _imports_stax(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("payments.stax") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("payments.stax") or (
                module == "payments" and any(alias.name == "stax" for alias in node.names)
            ):
                return True
        elif isinstance(node, ast.Call) and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and "payments.stax" in str(argument.value):
                return True
    return False


def test_no_module_outside_payments_imports_the_adapter() -> None:
    offenders = [
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if path.relative_to(BACKEND).parts[0] not in EXEMPT_PACKAGES
        and _imports_stax(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_the_import_check_detects_each_form() -> None:
    for source in (
        "import payments.stax.adapter",
        "from payments.stax import adapter",
        "from payments import stax",
        "importlib.import_module('payments.stax.transport')",
    ):
        assert _imports_stax(ast.parse(source)), source
    assert not _imports_stax(ast.parse("from payments.models import ExternalRef"))
