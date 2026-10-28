"""The user's theme (PLAN-012g A2; DESIGN-SPEC §7.1): Light, Dark or System, stored on
:class:`accounts.models.UserPreference`, rendered by the server as ``<html data-theme>``.

:func:`theme_for` reads it with one query at most and never writes; :func:`set_theme`
creates the row on the first choice.
"""

from __future__ import annotations

from typing import Literal, cast

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser

from accounts.models import Theme, UserPreference

ThemeName = Literal["light", "dark", "system"]
THEMES: tuple[ThemeName, ...] = ("light", "dark", "system")
DEFAULT_THEME: ThemeName = "system"


def theme_for(user: AbstractBaseUser | AnonymousUser) -> ThemeName:
    """The stored choice of ``user``; ``"system"`` for an anonymous user or one with no row."""
    if not user.is_authenticated:
        return DEFAULT_THEME
    stored = UserPreference.objects.filter(user_id=user.pk).values_list("theme", flat=True).first()
    return cast(ThemeName, stored) if stored in THEMES else DEFAULT_THEME


def set_theme(user: AbstractBaseUser, theme: str) -> None:
    """Store ``theme`` for ``user``; ``ValueError`` for a value outside :data:`THEMES`."""
    if theme not in THEMES:
        raise ValueError("unknown theme")
    preference, _ = UserPreference.objects.get_or_create(user_id=user.pk)
    preference.theme = Theme(theme)
    preference.save(update_fields=["theme", "updated_at"])
