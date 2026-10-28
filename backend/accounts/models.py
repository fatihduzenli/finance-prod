"""Sign-in bookkeeping (no IP address is stored, personal data; user agent only) and the
user's display preference (PLAN-012g A2: the theme, Light, Dark or System)."""

from __future__ import annotations

from django.conf import settings
from django.db import models

USER_AGENT_MAX_LENGTH = 200


class UserSession(models.Model):
    """One row per signed-in browser session, so a user's sessions can be listed/ended."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_sessions"
    )
    session_key = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    user_agent = models.CharField(max_length=USER_AGENT_MAX_LENGTH, blank=True)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self) -> str:
        return f"Session of user {self.user_id} since {self.created_at:%Y-%m-%d %H:%M}"


class Theme(models.TextChoices):
    """The appearance a user chose (DESIGN-SPEC §7.1); System follows the device."""

    LIGHT = "light", "Light"
    DARK = "dark", "Dark"
    SYSTEM = "system", "System"


class UserPreference(models.Model):
    """One row per user who has chosen a display preference; no row reads as the defaults
    (:func:`accounts.themes.theme_for`)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preference"
    )
    theme = models.CharField(max_length=6, choices=Theme.choices, default=Theme.SYSTEM)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Preference of user {self.user_id}"
