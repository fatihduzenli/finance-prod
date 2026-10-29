"""The user's theme (PLAN-012g A2; DESIGN-SPEC §7.1): Light, Dark or System, stored on
:class:`accounts.models.UserPreference`, rendered by the server as ``<html data-theme>``; and
the dashboard Payment status card's period (PLAN-014 A3), Weekly or Monthly, on the same row.

:func:`theme_for` and :func:`payment_status_period_for` read with one query at most and never
write; :func:`set_theme` and :func:`set_payment_status_period` create the row on the first
choice.
"""

from __future__ import annotations

from typing import Literal

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser

from accounts.models import PaymentStatusPeriod, Theme, UserPreference

ThemeName = Literal["light", "dark", "system"]
THEMES: tuple[ThemeName, ...] = ("light", "dark", "system")
DEFAULT_THEME: ThemeName = "system"


def theme_for(user: AbstractBaseUser | AnonymousUser) -> ThemeName:
    """The stored choice of ``user``; ``"system"`` for an anonymous user or one with no row."""
    if not user.is_authenticated:
        return DEFAULT_THEME
    stored = UserPreference.objects.filter(user_id=user.pk).values_list("theme", flat=True).first()
    return stored if stored in THEMES else DEFAULT_THEME


def set_theme(user: AbstractBaseUser, theme: str) -> None:
    """Store ``theme`` for ``user``; ``ValueError`` for a value outside :data:`THEMES`."""
    if theme not in THEMES:
        raise ValueError("unknown theme")
    preference, _ = UserPreference.objects.get_or_create(user_id=user.pk)
    preference.theme = Theme(theme)
    preference.save(update_fields=["theme", "updated_at"])


PaymentStatusPeriodName = Literal["weekly", "monthly"]
PAYMENT_STATUS_PERIODS: tuple[PaymentStatusPeriodName, ...] = ("weekly", "monthly")
DEFAULT_PAYMENT_STATUS_PERIOD: PaymentStatusPeriodName = "weekly"


def payment_status_period_for(user: AbstractBaseUser | AnonymousUser) -> PaymentStatusPeriodName:
    """The stored period of ``user``; ``"weekly"`` for an anonymous user or one with no row."""
    if not user.is_authenticated:
        return DEFAULT_PAYMENT_STATUS_PERIOD
    stored = (
        UserPreference.objects.filter(user_id=user.pk)
        .values_list("payment_status_period", flat=True)
        .first()
    )
    return stored if stored in PAYMENT_STATUS_PERIODS else DEFAULT_PAYMENT_STATUS_PERIOD


def set_payment_status_period(user: AbstractBaseUser, period: str) -> None:
    """Store ``period`` for ``user``; ``ValueError`` for a value outside
    :data:`PAYMENT_STATUS_PERIODS`."""
    if period not in PAYMENT_STATUS_PERIODS:
        raise ValueError("unknown period")
    preference, _ = UserPreference.objects.get_or_create(user_id=user.pk)
    preference.payment_status_period = PaymentStatusPeriod(period)
    preference.save(update_fields=["payment_status_period", "updated_at"])
