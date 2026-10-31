"""An operator's portal account from the staff side (PLAN-014 H2), and the portal messages'
one-time links.

**The PORTAL card's actions** (billing; :mod:`ledger.views.portal_access`), each under the
operator's row lock and audited on the entity ``portal_user``:

- :func:`invite` ("Invite to the portal", and "Resend invitation" while the account is not
  accepted): needs the operator's e-mail, which becomes the account's (lower-cased; another
  operator's account with it refuses). Creates the account inactive the first time, then
  queues the ``portal_invite`` message to that e-mail through the messaging engine
  (:func:`ledger.services.messaging.queue_portal`: the Outbox lists it). Audited
  ``portal_user.invited`` (``resend`` true the second time on).
- An archived operator's account cannot sign in (its sessions end at their next request, its
  links stop working); the card reads "Operator archived" and offers only Disable or Enable.
- :func:`disable` and :func:`enable`: a disabled account cannot sign in, its sessions end at
  their next request and its links stop working; enabling makes it active again when it was
  accepted (else the invitation is still to be accepted). Audited ``portal_user.disabled``,
  ``portal_user.enabled``.
- :func:`send_reset` ("Reset password", an active account only): queues ``portal_reset``.
  Audited ``portal_user.reset_sent``. :func:`request_reset` is the portal's own "Forgot your
  password?" form: the same message for an active account, at most one per
  :data:`RESET_COOLDOWN`, nothing for any other e-mail; the caller answers the same sentence
  whatever happened.

**One-time links.** A portal message is stored with
:data:`ledger.services.messaging.LINK_ADDED_AT_SEND` where its link goes; :func:`register_links`
(on the app's ready) gives the engine an issuer per template: it refuses a message whose
account is gone, whose e-mail changed since, an invitation of an account accepted or disabled,
a reset of an account that cannot sign in; else it makes a new token
(:func:`portal.tokens.issue`) whose link is written into the outgoing text only, kept when the
message was sent (the older link of that kind stops working) and dropped when it was not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from ledger import merge
from ledger.models import (
    AuditLog,
    CompanySettings,
    MessageChannel,
    MessageTemplate,
    Operator,
    OutboundMessage,
)
from ledger.services import messaging
from ledger.services.collections import mask_address
from portal import tokens
from portal.auth import normalize_email
from portal.models import PortalUser, TokenKind

ENTITY = "portal_user"
INVITED = "portal_user.invited"
DISABLED = "portal_user.disabled"
ENABLED = "portal_user.enabled"
RESET_SENT = "portal_user.reset_sent"
#: The portal's own reset form queues at most one message per account in this time.
RESET_COOLDOWN = timedelta(minutes=5)

NO_OPERATOR = "The operator no longer exists."
#: The PORTAL card's account line for an archived operator.
OPERATOR_ARCHIVED = "Operator archived"
ARCHIVED = "The operator is archived."
NO_EMAIL = "Add the operator's e-mail first: the invitation goes there."
TAKEN = "Another operator's portal account uses this e-mail."
ALREADY_ACTIVE = "The operator has accepted the invitation already: send a password reset instead."
IS_DISABLED = "Portal access is disabled: enable it first."
NO_ACCOUNT = "The operator has no portal account yet."
NOT_ACTIVE = "Only an account that signs in can reset its password: resend the invitation."
ALREADY_DISABLED = "Portal access is already disabled."
NOT_DISABLED = "Portal access is not disabled."
NOT_SET_UP = (
    "The message “{name}” is missing or inactive (Admin › Settings › Message templates): "
    "nothing was sent."
)
INVITED_TEXT = "Invitation queued to {email}. Its link works for 7 days, once."
DISABLED_TEXT = "Portal access disabled: the operator is signed out at their next click."
ENABLED_TEXT = "Portal access enabled."
RESET_TEXT = "A link to choose a new password is queued to {email}. It works for one hour, once."

#: Why a portal message is no longer to be sent (class names, the message's ``error``).
USER_GONE = "PortalUserGone"
EMAIL_CHANGED = "PortalEmailChanged"
USER_ACCEPTED = "PortalUserAccepted"
USER_DISABLED = "PortalUserDisabled"
USER_CANNOT_SIGN_IN = "PortalUserCannotSignIn"


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class Card:
    """What the operator page's PORTAL card shows of the account."""

    user: PortalUser | None
    state: str
    can_invite: bool
    can_resend: bool
    can_disable: bool
    can_enable: bool
    can_reset: bool


def card(operator: Operator) -> Card:
    """The PORTAL card's account line and the actions it offers (one query). An archived
    operator's account cannot sign in: the card says so and offers only Disable or Enable."""
    user = PortalUser.objects.filter(operator=operator).first()
    if operator.archived_at is not None:
        disabled = user is not None and user.disabled_at is not None
        can_disable = user is not None and not disabled
        return Card(user, OPERATOR_ARCHIVED, False, False, can_disable, disabled, False)
    if user is None:
        return Card(None, "Not invited", True, False, False, False, False)
    if user.disabled_at is not None:
        return Card(user, "Disabled", False, False, False, True, False)
    if user.activated_at is None:
        return Card(user, "Invited, not accepted yet", False, True, True, False, False)
    return Card(user, "Active", False, False, True, False, True)


def _template(code: str) -> MessageTemplate | None:
    return MessageTemplate.objects.filter(
        code=code, active=True, channel=MessageChannel.EMAIL
    ).first()


def _not_set_up(code: str) -> Outcome:
    return Outcome(False, NOT_SET_UP.format(name=code))


def _context(operator: Operator, link_field: str) -> dict[str, str]:
    row = CompanySettings.read()
    return {
        "operator_name": str(operator.display_name),
        "company_name": str(row.name),
        "support_email": str(row.support_email or ""),
        link_field: messaging.LINK_ADDED_AT_SEND,
    }


def _audit(action: str, user: PortalUser, actor: User | None, after: dict[str, object]) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY,
        entity_id=str(user.pk),
        before=None,
        after={"operator": str(user.operator_id), **after},
    )


def _locked_operator(operator_id: uuid.UUID) -> Operator | None:
    return Operator.objects.select_for_update().filter(pk=operator_id).first()


def _locked_user(operator: Operator) -> PortalUser | None:
    return PortalUser.objects.select_for_update().filter(operator=operator).first()


def invite(operator_id: uuid.UUID, actor: User, now: datetime | None = None) -> Outcome:
    """Invite the operator to the portal, or invite again (see the module docstring)."""
    moment = now or timezone.now()
    with transaction.atomic():
        operator = _locked_operator(operator_id)
        if operator is None:
            return Outcome(False, NO_OPERATOR)
        if operator.archived_at is not None:
            return Outcome(False, ARCHIVED)
        email = normalize_email(operator.email)
        if not email:
            return Outcome(False, NO_EMAIL)
        user = _locked_user(operator)
        if user is not None and user.disabled_at is not None:
            return Outcome(False, IS_DISABLED)
        if user is not None and user.activated_at is not None:
            return Outcome(False, ALREADY_ACTIVE)
        if PortalUser.objects.filter(email=email).exclude(operator=operator).exists():
            return Outcome(False, TAKEN)
        template = _template(merge.PORTAL_INVITE_CODE)
        if template is None:
            return _not_set_up(merge.PORTAL_INVITE_CODE)
        resend = user is not None
        try:
            with transaction.atomic():
                if user is None:
                    user = PortalUser(operator=operator)
                user.email = email
                user.invited_by = actor
                user.invited_at = moment
                user.save()
        except IntegrityError:
            return Outcome(False, TAKEN)
        message = messaging.queue_portal(
            template,
            user.pk,
            operator,
            email,
            _context(operator, "set_password_link"),
            actor=actor,
            now=moment,
        )
        _audit(
            INVITED,
            user,
            actor,
            {"to": mask_address(email), "resend": resend, "message": str(message.pk)},
        )
    return Outcome(True, INVITED_TEXT.format(email=email))


def disable(operator_id: uuid.UUID, actor: User, now: datetime | None = None) -> Outcome:
    moment = now or timezone.now()
    with transaction.atomic():
        operator = _locked_operator(operator_id)
        user = _locked_user(operator) if operator is not None else None
        if user is None:
            return Outcome(False, NO_ACCOUNT)
        if user.disabled_at is not None:
            return Outcome(False, ALREADY_DISABLED)
        user.is_active = False
        user.disabled_at = moment
        user.save(update_fields=["is_active", "disabled_at", "updated_at"])
        tokens.void_all(user, TokenKind.INVITE, moment)
        tokens.void_all(user, TokenKind.RESET, moment)
        _audit(DISABLED, user, actor, {"is_active": False})
    return Outcome(True, DISABLED_TEXT)


def enable(operator_id: uuid.UUID, actor: User) -> Outcome:
    with transaction.atomic():
        operator = _locked_operator(operator_id)
        user = _locked_user(operator) if operator is not None else None
        if user is None:
            return Outcome(False, NO_ACCOUNT)
        if user.disabled_at is None:
            return Outcome(False, NOT_DISABLED)
        user.disabled_at = None
        user.is_active = user.activated_at is not None
        user.save(update_fields=["is_active", "disabled_at", "updated_at"])
        _audit(ENABLED, user, actor, {"is_active": user.is_active})
    return Outcome(True, ENABLED_TEXT)


def _queue_reset(
    user: PortalUser, template: MessageTemplate, actor: User | None, moment: datetime
) -> OutboundMessage:
    return messaging.queue_portal(
        template,
        user.pk,
        user.operator,
        user.email,
        _context(user.operator, "reset_link"),
        actor=actor,
        now=moment,
    )


def send_reset(operator_id: uuid.UUID, actor: User, now: datetime | None = None) -> Outcome:
    """The PORTAL card's "Reset password" (see the module docstring)."""
    moment = now or timezone.now()
    with transaction.atomic():
        operator = _locked_operator(operator_id)
        user = _locked_user(operator) if operator is not None else None
        if user is None:
            return Outcome(False, NO_ACCOUNT)
        if operator is not None and operator.archived_at is not None:
            return Outcome(False, ARCHIVED)
        if not user.can_sign_in:
            return Outcome(False, IS_DISABLED if user.disabled_at is not None else NOT_ACTIVE)
        template = _template(merge.PORTAL_RESET_CODE)
        if template is None:
            return _not_set_up(merge.PORTAL_RESET_CODE)
        message = _queue_reset(user, template, actor, moment)
        _audit(
            RESET_SENT, user, actor, {"to": mask_address(user.email), "message": str(message.pk)}
        )
    return Outcome(True, RESET_TEXT.format(email=user.email))


def request_reset(raw_email: object, now: datetime | None = None) -> None:
    """The portal's "Forgot your password?" (see the module docstring); returns nothing, so
    the page cannot tell an account from none."""
    moment = now or timezone.now()
    email = normalize_email(raw_email)
    if not email:
        return
    with transaction.atomic():
        user = (
            PortalUser.objects.select_for_update()
            .select_related("operator")
            .filter(email=email)
            .first()
        )
        if user is None or not user.can_sign_in or user.operator.archived_at is not None:
            return
        recent = OutboundMessage.objects.filter(
            charge_entity_id=user.pk,
            template__code=merge.PORTAL_RESET_CODE,
            queued_at__gt=moment - RESET_COOLDOWN,
        ).exists()
        template = _template(merge.PORTAL_RESET_CODE)
        if recent or template is None:
            return
        message = _queue_reset(user, template, None, moment)
        _audit(
            RESET_SENT,
            user,
            None,
            {"to": mask_address(user.email), "message": str(message.pk), "by": "portal"},
        )


# --- One-time links --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Issuer:
    kind: str
    route: str

    def _user(self, message: OutboundMessage) -> PortalUser | None:
        return PortalUser.objects.filter(pk=message.charge_entity_id).first()

    def refusal(self, message: OutboundMessage) -> str:
        user = self._user(message)
        if user is None:
            return USER_GONE
        if user.email != message.to_address.strip().lower():
            return EMAIL_CHANGED
        if self.kind == TokenKind.INVITE:
            if user.disabled_at is not None:
                return USER_DISABLED
            if user.activated_at is not None:
                return USER_ACCEPTED
            return ""
        return "" if user.can_sign_in else USER_CANNOT_SIGN_IN

    def issue(self, message: OutboundMessage) -> messaging.OneTimeLink:
        user = PortalUser.objects.get(pk=message.charge_entity_id)
        token, raw = tokens.issue(user, self.kind)
        path = reverse(self.route, kwargs={"token": raw})
        return messaging.OneTimeLink(
            url=f"{str(settings.APP_BASE_URL).rstrip('/')}{path}",
            keep=lambda: tokens.keep(token),
            drop=lambda: tokens.drop(token),
        )


def register_links() -> None:
    """Give the messaging engine the issuers of the two portal templates."""
    messaging.ONE_TIME_LINKS[merge.PORTAL_INVITE_CODE] = _Issuer(TokenKind.INVITE, "portal:invite")
    messaging.ONE_TIME_LINKS[merge.PORTAL_RESET_CODE] = _Issuer(
        TokenKind.RESET, "portal:reset_confirm"
    )
