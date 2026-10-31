"""The portal's principal and its bookkeeping (PLAN-014 H1).

- :class:`PortalUser`: one per operator, never a staff ``User`` (no ``AbstractBaseUser``, no
  entanglement with ``AUTH_USER_MODEL``). ``email`` is lower-cased, unique, and the operator's
  e-mail when the invitation was made; ``password`` is a Django hasher's value
  (``make_password``/``check_password``), empty until the invitation is accepted. The states:
  invited (``is_active`` false, ``activated_at`` empty), active, disabled (``is_active`` false,
  ``disabled_at`` set).
- :class:`PortalToken`: a one-time link's SHA-256 (never the token), its kind (invitation, 7
  days; reset, 1 hour), its expiry and its use.
- :class:`PortalLoginAttempt`: one row per sign-in attempt, by the keyed hash of the e-mail
  and the client address, for the lockout (:mod:`portal.auth`).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from accounts.models import Theme
from ledger.models import GuardedModel, Operator
from ledger.validators import validate_no_payment_data

#: The SHA-256 of a token or an e-mail, in lower-case hexadecimal.
HEX_64 = r"^[0-9a-f]{64}$"


class PortalUser(GuardedModel):
    operator = models.ForeignKey(Operator, on_delete=models.PROTECT, related_name="portal_users")
    email = models.EmailField(unique=True, validators=[validate_no_payment_data])
    #: A Django password hasher's value; empty until the invitation is accepted.
    password = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    invited_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    theme = models.CharField(max_length=6, choices=Theme.choices, default=Theme.SYSTEM)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "portal_users"
        constraints = [
            models.UniqueConstraint(fields=["operator"], name="portal_users_one_per_operator"),
            models.CheckConstraint(
                condition=Q(email=Lower("email")), name="portal_users_email_lower_case"
            ),
            models.CheckConstraint(
                condition=Q(theme__in=Theme.values), name="portal_users_theme_valid"
            ),
            # Active means accepted and not disabled.
            models.CheckConstraint(
                condition=Q(is_active=False)
                | Q(activated_at__isnull=False, disabled_at__isnull=True),
                name="portal_users_active_is_activated",
            ),
        ]

    def __str__(self) -> str:
        return f"Portal user {self.pk}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)

    @property
    def can_sign_in(self) -> bool:
        return self.is_active and self.activated_at is not None and self.disabled_at is None


class TokenKind(models.TextChoices):
    INVITE = "invite", "Invitation"
    RESET = "reset", "Password reset"


class PortalToken(GuardedModel):
    user = models.ForeignKey(PortalUser, on_delete=models.CASCADE, related_name="tokens")
    kind = models.CharField(max_length=6, choices=TokenKind.choices)
    #: The SHA-256 of the token; the token itself is never stored.
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    #: Set when the link was used, or voided by a newer link of the same kind.
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "portal_tokens"
        indexes = [models.Index(fields=["user", "kind"], name="portal_tokens_user_kind_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=TokenKind.values), name="portal_tokens_kind_valid"
            ),
            models.CheckConstraint(
                condition=Q(token_hash__regex=HEX_64), name="portal_tokens_token_hash_hex"
            ),
        ]

    def __str__(self) -> str:
        return f"Portal {self.kind} token {self.pk}"


class PortalLoginAttempt(models.Model):
    #: The keyed SHA-256 of the lower-cased e-mail typed (:func:`portal.auth.email_hash`).
    email_hash = models.CharField(max_length=64)
    #: The client address; null when it was not an IP address.
    ip = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField()
    ok = models.BooleanField(default=False)

    class Meta:
        db_table = "portal_login_attempts"
        indexes = [
            models.Index(fields=["email_hash", "at"], name="portal_attempts_email_idx"),
            models.Index(fields=["ip", "at"], name="portal_attempts_ip_idx"),
            models.Index(fields=["at"], name="portal_attempts_at_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(email_hash__regex=HEX_64), name="portal_login_attempts_email_hash_hex"
            ),
        ]

    def __str__(self) -> str:
        return f"Portal sign-in attempt {self.pk}"
