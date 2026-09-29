"""App config for the business ledger (docs/qa/PLAN-004-schema.md §4.1)."""

from __future__ import annotations

from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = "ledger"
    verbose_name = "Ledger"
