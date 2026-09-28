"""App config for staff accounts."""

from __future__ import annotations

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Staff accounts"

    def ready(self) -> None:
        # Connects the login/logout receivers that maintain UserSession rows.
        from accounts import signals  # noqa: F401
