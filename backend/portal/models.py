"""The portal's principal and its bookkeeping (PLAN-014 H1).

- :class:`PortalUser`: one per operator, never a staff ``User`` (no ``AbstractBaseUser``, no
  entanglement with ``AUTH_USER_MODEL``). ``email`` is lower-cased, unique, and the operator's
  e-mail when the invitation was made; ``password`` is a Django hasher's value
  (``make_password``/``check_password``), empty until the invitation is accepted. The states:
  invited (``is_active`` false, ``activated_at`` empty), active, disabled (``is_active`` false,
  ``disabled_at`` set). An account signs in only while its operator is not archived
  (:attr:`PortalUser.can_sign_in`).
- :class:`PortalToken`: a one-time link's SHA-256 (never the token), its kind (invitation, 7
  days; reset, 1 hour), its expiry and its use.
- :class:`PortalLoginAttempt`: one row per sign-in attempt, by the keyed hash of the e-mail
  and the client address, for the lockout (:mod:`portal.auth`).
- :class:`PortalPayment` (PLAN-014 H4): a portal user's own bank debit of one charge or of a
  store's whole open balance: the lease, the amount confirmed, the page's one-time
  ``creation_key`` (a replay finds the first request), and its state (requested, paid,
  failed, refused). One :class:`PortalPaymentCharge` per charge paid: the charge (a rent or
  a one-off charge), the one-off charge the debit runs on (the charge itself, or the rent
  charge's bank debit), its amount, its ``portal_pay`` job and its own state. No free text.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from accounts.models import Theme
from ledger.models import GuardedModel, Lease, MoneyField, OneOffCharge, Operator
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
        """Accepted, not disabled, and its operator not archived (load ``operator`` with
        the user to spare a query)."""
        return (
            self.is_active
            and self.activated_at is not None
            and self.disabled_at is None
            and self.operator.archived_at is None
        )


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


class PortalPaymentStatus(models.TextChoices):
    REQUESTED = "requested", "Requested"
    #: Every charge's debit was accepted (it settles in a few days, as any bank debit).
    PAID = "paid", "Submitted"
    FAILED = "failed", "Failed"
    #: Refused when it was requested (a charge changed since the page); nothing was debited.
    REFUSED = "refused", "Refused"


#: A charge's own states: a refusal is the whole payment's, never one charge's.
CHARGE_STATUSES = (
    PortalPaymentStatus.REQUESTED.value,
    PortalPaymentStatus.PAID.value,
    PortalPaymentStatus.FAILED.value,
)


class PortalPayment(GuardedModel):
    user = models.ForeignKey(PortalUser, on_delete=models.PROTECT, related_name="payments")
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="+")
    amount = MoneyField()
    #: The Pay page's one-time key: a second submission finds the first request.
    creation_key = models.UUIDField(unique=True)
    status = models.CharField(
        max_length=9, choices=PortalPaymentStatus.choices, default=PortalPaymentStatus.REQUESTED
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "portal_payments"
        indexes = [
            models.Index(fields=["user", "created_at"], name="portal_payments_user_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=PortalPaymentStatus.values),
                name="portal_payments_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="portal_payments_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"Portal payment {self.pk}"


class PortalChargeKind(models.TextChoices):
    RENT = "rent", "Rent"
    ONE_OFF = "one_off", "One-off"


class PortalPaymentCharge(GuardedModel):
    payment = models.ForeignKey(PortalPayment, on_delete=models.PROTECT, related_name="charges")
    kind = models.CharField(max_length=7, choices=PortalChargeKind.choices)
    #: The rent charge or the one-off charge paid (as ``receivables.OpenBalance.charge_id``).
    charge_id = models.UUIDField()
    #: The one-off charge the debit runs on: the charge itself, or the rent charge's bank debit.
    debit = models.ForeignKey(OneOffCharge, on_delete=models.PROTECT, related_name="+")
    amount = MoneyField()
    status = models.CharField(
        max_length=9, choices=PortalPaymentStatus.choices, default=PortalPaymentStatus.REQUESTED
    )
    job = models.ForeignKey(
        "payments.Job", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "portal_payment_charges"
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "kind", "charge_id"], name="portal_payment_charges_unique"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=PortalChargeKind.values),
                name="portal_payment_charges_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=CHARGE_STATUSES),
                name="portal_payment_charges_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="portal_payment_charges_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"Portal payment charge {self.pk}"
