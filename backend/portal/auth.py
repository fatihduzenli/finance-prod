"""Signing in to the portal (PLAN-014 H1): the password check, the lockout and the session.

**Password.** Django's hashers (``check_password``/``make_password``) on
:attr:`portal.models.PortalUser.password`; an e-mail without an account is checked against a
throwaway hash (a usable one, of a random password, made at import) so it takes the same time.
Only an account that can sign in (accepted, not disabled, its operator not archived) passes. A
wrong password, an unknown e-mail, an account not yet accepted, a disabled one and one whose
operator is archived all answer :data:`WRONG` (no account enumeration).

**Lockout.** Every attempt is a :class:`portal.models.PortalLoginAttempt` (the keyed hash of
the e-mail, the client address, the time, the outcome). :data:`MAX_FAILURES` failures within
:data:`FAILURE_WINDOW` for one e-mail (counted since its last success: a success resets the
e-mail's count) **or** from one address (not reset by a success, so one good account cannot
clear an address that is guessing others) lock that e-mail or that address for
:data:`LOCKOUT` from the last of those failures. While locked, the login answers
:data:`TRY_AGAIN` ("Try again in n minutes") without checking the password or recording the
attempt, the same for an e-mail with or without an account. Attempts older than a day are
deleted as new failures are written.

**Session** (:mod:`portal.middleware`). :func:`sign_in_session` deletes the portal session
and starts a new one (a new key: rotated at sign-in) holding the user's id, the sign-in time,
the last request's time and a keyed hash of the stored password (a password change or reset
ends every other session at its next request). :func:`current_user` reads it back: no user
after :data:`IDLE_TIMEOUT` without a request or :data:`ABSOLUTE_TIMEOUT` after sign-in, for an
account that can no longer sign in (disabled, or its operator archived), or once the password
changed; the session is then emptied.
"""

from __future__ import annotations

import math
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from ipaddress import ip_address

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.backends.base import SessionBase
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from portal.models import PortalLoginAttempt, PortalUser

MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)
#: Attempts older than this are deleted.
ATTEMPT_RETENTION = timedelta(days=1)
IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_TIMEOUT = timedelta(hours=12)
#: Longest e-mail looked up; longer input is a wrong e-mail.
EMAIL_MAX_LENGTH = 254
PASSWORD_MAX_LENGTH = 4096

WRONG = "The e-mail or the password is not right."
TRY_AGAIN = "Too many attempts. Try again in {minutes}."

USER_KEY = "portal_user_id"
LOGIN_AT_KEY = "portal_login_at"
LAST_SEEN_KEY = "portal_last_seen_at"
AUTH_HASH_KEY = "portal_auth_hash"

#: Checked when the e-mail has no account (or the account no password), so that answer takes
#: as long as a wrong password: a usable hash, as an unusable one is refused without hashing.
_THROWAWAY = make_password(secrets.token_urlsafe(32))


def normalize_email(raw: object) -> str:
    text = raw.strip().lower() if isinstance(raw, str) else ""
    return text if len(text) <= EMAIL_MAX_LENGTH else ""


def email_hash(email: str) -> str:
    """The keyed SHA-256 of the lower-cased e-mail: an attempt row never holds the e-mail."""
    return salted_hmac("portal.login-attempt", email.lower(), algorithm="sha256").hexdigest()


def clean_ip(raw: str) -> str | None:
    try:
        return str(ip_address(raw))
    except ValueError:
        return None


def minutes_text(remaining: timedelta) -> str:
    minutes = max(1, math.ceil(remaining.total_seconds() / 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _locked_until(times: list[datetime]) -> datetime | None:
    """The end of the lockout that ``times`` (failures, oldest first) earned, if any: 15
    minutes after the last failure of any run of :data:`MAX_FAILURES` within the window."""
    end = None
    for first, last in zip(times, times[MAX_FAILURES - 1 :], strict=False):
        if last - first <= FAILURE_WINDOW:
            end = last + LOCKOUT
    return end


def locked_for(hashed: str, ip: str | None, now: datetime) -> timedelta | None:
    """How long the e-mail (by its hash) or the address stays locked; ``None`` when free."""
    since = now - FAILURE_WINDOW - LOCKOUT
    last_ok = (
        PortalLoginAttempt.objects.filter(email_hash=hashed, ok=True, at__gt=since)
        .order_by("-at")
        .values_list("at", flat=True)
        .first()
    )
    by_email = PortalLoginAttempt.objects.filter(
        email_hash=hashed, ok=False, at__gt=last_ok or since
    )
    ends = [_locked_until(list(by_email.order_by("at").values_list("at", flat=True)))]
    if ip is not None:
        by_ip = PortalLoginAttempt.objects.filter(ip=ip, ok=False, at__gt=since)
        ends.append(_locked_until(list(by_ip.order_by("at").values_list("at", flat=True))))
    latest = max((end for end in ends if end is not None), default=None)
    if latest is None or latest <= now:
        return None
    return latest - now


@dataclass(frozen=True, slots=True)
class SignIn:
    user: PortalUser | None
    message: str = ""


def _record(hashed: str, ip: str | None, now: datetime, *, ok: bool) -> None:
    PortalLoginAttempt.objects.create(email_hash=hashed, ip=ip, at=now, ok=ok)
    if not ok:
        PortalLoginAttempt.objects.filter(at__lt=now - ATTEMPT_RETENTION).delete()


def authenticate(
    raw_email: object, raw_password: object, raw_ip: str, now: datetime | None = None
) -> SignIn:
    """Check a sign-in (see the module docstring); the user on success, else the sentence."""
    moment = now or timezone.now()
    email = normalize_email(raw_email)
    password = raw_password if isinstance(raw_password, str) else ""
    hashed = email_hash(email)
    ip = clean_ip(raw_ip)
    locked = locked_for(hashed, ip, moment)
    if locked is not None:
        return SignIn(None, TRY_AGAIN.format(minutes=minutes_text(locked)))
    user = (
        PortalUser.objects.select_related("operator").filter(email=email).first() if email else None
    )
    if len(password) > PASSWORD_MAX_LENGTH:
        password = ""
    if user is None or not user.password:
        check_password(password, _THROWAWAY)
        ok = False
    else:
        ok = check_password(password, user.password) and user.can_sign_in
    _record(hashed, ip, moment, ok=ok)
    if not ok or user is None:
        locked = locked_for(hashed, ip, moment)
        if locked is not None:
            return SignIn(None, TRY_AGAIN.format(minutes=minutes_text(locked)))
        return SignIn(None, WRONG)
    PortalUser.objects.filter(pk=user.pk).update(last_login_at=moment)
    user.last_login_at = moment
    return SignIn(user)


def auth_hash(user: PortalUser) -> str:
    return salted_hmac("portal.session", user.password, algorithm="sha256").hexdigest()


def sign_in_session(
    old: SessionBase, user: PortalUser, now: datetime | None = None
) -> SessionStore:
    """A new portal session for ``user``, under a new key (see the module docstring); the old
    one is deleted when it was a portal session (a cookie naming any other is left alone)."""
    stamp = int((now or timezone.now()).timestamp())
    if old.get(USER_KEY) is not None:
        old.flush()
    session = SessionStore()
    session[USER_KEY] = str(user.pk)
    session[LOGIN_AT_KEY] = stamp
    session[LAST_SEEN_KEY] = stamp
    session[AUTH_HASH_KEY] = auth_hash(user)
    return session


def refresh_auth_hash(session: SessionBase, user: PortalUser) -> None:
    """Keep this session signed in after its own password change."""
    session.cycle_key()
    session[AUTH_HASH_KEY] = auth_hash(user)


def current_user(session: SessionBase, now: datetime | None = None) -> PortalUser | None:
    """The signed-in portal user of ``session``, or ``None`` (the session emptied when it
    held one that no longer counts)."""
    key = session.get(USER_KEY)
    if key is None:
        return None
    stamp = int((now or timezone.now()).timestamp())
    login_at, last_seen = session.get(LOGIN_AT_KEY), session.get(LAST_SEEN_KEY)
    user = None
    try:
        user_id = uuid.UUID(str(key))
    except ValueError:
        user_id = None
    if (
        user_id is not None
        and isinstance(login_at, int)
        and isinstance(last_seen, int)
        and stamp - login_at < ABSOLUTE_TIMEOUT.total_seconds()
        and stamp - last_seen < IDLE_TIMEOUT.total_seconds()
    ):
        user = PortalUser.objects.select_related("operator").filter(pk=user_id).first()
    if (
        user is None
        or not user.can_sign_in
        or not constant_time_compare(str(session.get(AUTH_HASH_KEY, "")), auth_hash(user))
    ):
        session.flush()
        return None
    session[LAST_SEEN_KEY] = stamp
    return user
