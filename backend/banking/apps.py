"""The banking app's configuration (PLAN-014 I1)."""

from __future__ import annotations

from django.apps import AppConfig


class BankingConfig(AppConfig):
    name = "banking"
    verbose_name = "Bank deposits"
    default_auto_field = "django.db.models.BigAutoField"
