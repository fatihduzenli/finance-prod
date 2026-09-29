"""App config for the provider-side tables (docs/qa/PLAN-004-schema.md §4.2)."""

from __future__ import annotations

from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    name = "payments"
    verbose_name = "Payment provider records"
