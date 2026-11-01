"""Authentication, MFA, role and session-lifetime enforcement (PLAN-003 §4).

Installed in this order, after Django's AuthenticationMiddleware and django-otp's
OTPMiddleware:

1. SessionPolicyMiddleware  - ends sessions past the idle or absolute limit.
2. AuthRequiredMiddleware   - 401 for any non-public route without a signed-in user.
3. MfaRequiredMiddleware    - 403 for a signed-in user who has not passed the second
                              factor, except on enrolment and logout.
4. RoleEnforcementMiddleware - 403 for an unsafe request the role registry refuses.

The operator portal's routes (the ``portal:`` namespace, PLAN-014 H1) are exempt from 2 to
4: ``portal.middleware`` signs its own principal in and out, and a staff session opens none
of them. 1 ignores every path under ``/portal/``: browsing the portal neither reads nor
refreshes a staff session's idle timer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponseBase
from django.urls import Resolver404, resolve
from django.utils import timezone
from django_otp import devices_for_user

from accounts import signals, views
from accounts.models import UserSession
from accounts.registry import (
    MFA_EXEMPT_ROUTE_NAMES,
    MFA_SETUP_ROUTE_NAMES,
    PUBLIC_ROUTE_NAMES,
    PUBLIC_UNSAFE_ROUTE_NAMES,
    SAFE_METHODS,
    is_allowed,
    is_portal_route,
    is_public_path,
    rule_for,
    view_name,
)
from accounts.roles import role_of
from portal.middleware import PORTAL_PREFIX

GetResponse = Callable[[HttpRequest], HttpResponseBase]


def _resolved_name(request: HttpRequest) -> str | None:
    """Namespaced route name for the request path, resolved ahead of the URL dispatcher."""
    try:
        match = resolve(request.path_info, getattr(request, "urlconf", None))
    except Resolver404:
        return None
    return str(match.view_name) if match.url_name else None


def _is_public(request: HttpRequest) -> bool:
    return is_public_path(request.path_info) or _resolved_name(request) in PUBLIC_ROUTE_NAMES


def _is_portal(request: HttpRequest) -> bool:
    """A route of the operator portal: its own middleware decides (PLAN-014 H1)."""
    return is_portal_route(_resolved_name(request))


def _is_verified(user: AbstractBaseUser | AnonymousUser) -> bool:
    """True only when django-otp says this session passed the second factor."""
    check = getattr(user, "is_verified", None)
    return bool(callable(check) and check())


class _Middleware:
    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response


class SessionPolicyMiddleware(_Middleware):
    """Idle timeout and absolute session ceiling.

    Expiry flushes the session (logout), so the request continues as anonymous and the
    next middleware answers 401. A signed-in session without a recorded sign-in time is
    treated as expired: it cannot prove it is within the ceiling. A portal path is left
    alone (PLAN-014 H1): it is not staff activity.
    """

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if request.path_info.startswith(PORTAL_PREFIX):
            return self.get_response(request)
        if request.user.is_authenticated:
            self._enforce(request)
        return self.get_response(request)

    @staticmethod
    def _enforce(request: HttpRequest) -> None:
        session = request.session
        now = int(signals.now())
        login_at = session.get(signals.LOGIN_AT_SESSION_KEY)
        last_seen = session.get(signals.LAST_SEEN_SESSION_KEY)
        if (
            not isinstance(login_at, int)
            or not isinstance(last_seen, int)
            or now - login_at >= settings.SESSION_ABSOLUTE_MAX_AGE
            or now - last_seen >= settings.SESSION_COOKIE_AGE
        ):
            logout(request)
            return
        session[signals.LAST_SEEN_SESSION_KEY] = now
        if session.session_key:
            UserSession.objects.filter(session_key=session.session_key).update(
                last_seen_at=timezone.now()
            )


class AuthRequiredMiddleware(_Middleware):
    """401, never a redirect, for a non-public route without a signed-in user."""

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if (
            not request.user.is_authenticated
            and not _is_public(request)
            and not _is_portal(request)
        ):
            return views.unauthenticated(request)
        return self.get_response(request)


class MfaRequiredMiddleware(_Middleware):
    """A signed-in user who has not passed the second factor may only enrol or log out.

    Enrolment is open only while the user has no confirmed device. A user who has a
    device but an unverified session (for example after the device that verified it
    was deleted and another remains) must sign in again: otherwise a password alone
    would be enough to register a new authenticator.
    """

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        user = request.user
        if (
            not user.is_authenticated
            or _is_verified(user)
            or _is_public(request)
            or _is_portal(request)
        ):
            return self.get_response(request)

        name = _resolved_name(request)
        if name in MFA_EXEMPT_ROUTE_NAMES:
            return self.get_response(request)
        has_device = any(True for _ in devices_for_user(user, confirmed=True))
        if name in MFA_SETUP_ROUTE_NAMES and not has_device:
            return self.get_response(request)
        return views.mfa_required(request, can_enrol=not has_device)


class RoleEnforcementMiddleware(_Middleware):
    """Refuse every unsafe request whose route rule the user's role does not meet."""

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., Any],
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> HttpResponseBase | None:
        method = (request.method or "").upper()
        if method in SAFE_METHODS:
            return None
        name = view_name(request)
        if name in PUBLIC_UNSAFE_ROUTE_NAMES or is_portal_route(name):
            return None
        if not request.user.is_authenticated:
            return views.forbidden(request)
        if is_allowed(rule_for(name), method, role_of(request.user)):
            return None
        return views.forbidden(request)
