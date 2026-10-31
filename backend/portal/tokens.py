"""The portal's one-time links (PLAN-014 H1): the invitation (7 days) and the password reset
(1 hour).

A token is 32 random bytes (``secrets.token_urlsafe``); only its SHA-256 is stored
(:class:`portal.models.PortalToken`). :func:`issue` makes a new token without touching the
others; :func:`keep` makes it the one live token of its kind (every older unused token of that
kind is voided: its ``used_at`` is set), :func:`drop` voids it. The messaging engine calls
:func:`issue` when a portal message is delivered and :func:`keep` or :func:`drop` after
(:mod:`portal.invitations`), so a link exists only in a message that was sent.

:func:`find` answers the live token a raw value names, or ``None`` for any other value: too
long, unknown, of the other kind, used, voided, expired, or of an account that may not use it
(an invitation of an account already activated or disabled, or whose operator is archived; a
reset of an account that cannot sign in). The stored hash is compared with
:func:`hmac.compare_digest`. Nothing here logs a token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from django.utils import timezone

from portal.models import PortalToken, PortalUser, TokenKind

INVITE_LIFETIME = timedelta(days=7)
RESET_LIFETIME = timedelta(hours=1)
LIFETIMES: dict[str, timedelta] = {
    TokenKind.INVITE: INVITE_LIFETIME,
    TokenKind.RESET: RESET_LIFETIME,
}
#: A raw token longer than this is refused before any work (``token_urlsafe(32)`` is 43).
TOKEN_MAX_LENGTH = 100


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(user: PortalUser, kind: str, now: datetime | None = None) -> tuple[PortalToken, str]:
    """A new token of ``kind`` for ``user`` and its raw value; the older ones stay as they
    are until :func:`keep`."""
    moment = now or timezone.now()
    raw = secrets.token_urlsafe(32)
    token = PortalToken.objects.create(
        user=user, kind=kind, token_hash=digest(raw), expires_at=moment + LIFETIMES[kind]
    )
    return token, raw


def keep(token: PortalToken, now: datetime | None = None) -> None:
    """Make ``token`` the one live token of its kind: void every older unused one."""
    PortalToken.objects.filter(
        user_id=token.user_id, kind=token.kind, used_at__isnull=True
    ).exclude(pk=token.pk).update(used_at=now or timezone.now())


def drop(token: PortalToken, now: datetime | None = None) -> None:
    """Void ``token`` (its message did not leave)."""
    PortalToken.objects.filter(pk=token.pk, used_at__isnull=True).update(
        used_at=now or timezone.now()
    )


def void_all(user: PortalUser, kind: str, now: datetime | None = None) -> None:
    PortalToken.objects.filter(user=user, kind=kind, used_at__isnull=True).update(
        used_at=now or timezone.now()
    )


def _usable_by(user: PortalUser, kind: str) -> bool:
    if kind == TokenKind.INVITE:
        return (
            user.activated_at is None
            and user.disabled_at is None
            and user.operator.archived_at is None
        )
    return user.can_sign_in


def find(raw: str, kind: str, now: datetime | None = None) -> PortalToken | None:
    """The live token of ``kind`` that ``raw`` names (see the module docstring)."""
    if not raw or len(raw) > TOKEN_MAX_LENGTH:
        return None
    wanted = digest(raw)
    token = (
        PortalToken.objects.select_related("user", "user__operator")
        .filter(token_hash=wanted)
        .first()
    )
    if token is None or not hmac.compare_digest(token.token_hash, wanted):
        return None
    moment = now or timezone.now()
    if token.kind != kind or token.used_at is not None or token.expires_at <= moment:
        return None
    if not _usable_by(token.user, kind):
        return None
    return token


def use(token: PortalToken, now: datetime | None = None) -> bool:
    """Mark ``token`` used, once: ``False`` when another request used it first."""
    moment = now or timezone.now()
    updated = PortalToken.objects.filter(
        pk=token.pk, used_at__isnull=True, expires_at__gt=moment
    ).update(used_at=moment)
    return updated == 1
