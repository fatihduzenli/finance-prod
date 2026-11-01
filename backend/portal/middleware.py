"""The portal's session and its sign-in requirement (PLAN-014 H1).

1. :class:`PortalSessionMiddleware`, for paths under ``/portal/`` only: the portal session is
   Django's database ``SessionStore`` behind a cookie of its own, :data:`COOKIE_NAME` (path
   ``/portal/``, HttpOnly, SameSite Strict, Secure outside local), never the staff
   ``sessionid``; the staff session and middleware never read it. It sets
   ``request.portal_session`` and ``request.portal_user`` (:func:`portal.auth.current_user`:
   30 minutes idle, 12 hours after sign-in, a disabled account or a changed password end
   it). After the view, a signed-in session is saved with the idle timeout and its cookie
   set again; an emptied one (sign-out, expiry) deletes the cookie.
2. :class:`PortalAuthMiddleware`: every ``portal:`` route except
   :data:`accounts.registry.PORTAL_PUBLIC_ROUTE_NAMES` needs a signed-in portal user;
   without one the browser is sent to the portal's sign-in page (with ``next`` for a GET).
   A staff session counts for nothing here, and the staff middleware exempts the ``portal:``
   routes (:mod:`accounts.middleware`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpRequest, HttpResponseBase, HttpResponseRedirect
from django.urls import reverse

from accounts.registry import PORTAL_PUBLIC_ROUTE_NAMES, is_portal_route, view_name
from portal import auth

COOKIE_NAME = "portal_sessionid"
COOKIE_PATH = "/portal/"
PORTAL_PREFIX = "/portal/"

GetResponse = Callable[[HttpRequest], HttpResponseBase]


def _secure() -> bool:
    return settings.APP_ENV != "local"


def safe_next(raw: object) -> str:
    """``raw`` when it is a path of the portal (never another site's address), else ``""``."""
    if not isinstance(raw, str) or not raw.startswith(PORTAL_PREFIX):
        return ""
    if "\\" in raw or "//" in raw or any(ord(char) < 32 for char in raw):
        return ""
    return raw


class PortalSessionMiddleware:
    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if not request.path_info.startswith(PORTAL_PREFIX):
            return self.get_response(request)
        session = SessionStore(session_key=request.COOKIES.get(COOKIE_NAME))
        request.portal_session = session  # type: ignore[attr-defined]
        request.portal_user = auth.current_user(session)  # type: ignore[attr-defined]
        response = self.get_response(request)
        # The sign-in view replaces the session with a new one (portal.auth.sign_in_session).
        return self._finish(request, request.portal_session, response)  # type: ignore[attr-defined]

    @staticmethod
    def _finish(
        request: HttpRequest, session: SessionStore, response: HttpResponseBase
    ) -> HttpResponseBase:
        if session.get(auth.USER_KEY) is not None:
            session.set_expiry(int(auth.IDLE_TIMEOUT.total_seconds()))
            session.save()
            response.set_cookie(
                COOKIE_NAME,
                str(session.session_key),
                max_age=int(auth.IDLE_TIMEOUT.total_seconds()),
                path=COOKIE_PATH,
                secure=_secure(),
                httponly=True,
                samesite="Strict",
            )
        elif COOKIE_NAME in request.COOKIES:
            # Nothing stored is deleted here: an emptied session was deleted by its flush,
            # and a cookie naming any other session is simply dropped.
            response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, samesite="Strict")
        return response


class PortalAuthMiddleware:
    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., Any],
        view_args: tuple[Any, ...],
        view_kwargs: dict[str, Any],
    ) -> HttpResponseBase | None:
        name = view_name(request)
        if not is_portal_route(name) or name in PORTAL_PUBLIC_ROUTE_NAMES:
            return None
        if getattr(request, "portal_user", None) is not None:
            return None
        target = reverse("portal:login")
        if request.method in ("GET", "HEAD"):
            following = safe_next(request.get_full_path())
            if following:
                target += "?" + urlencode({"next": following})
        return HttpResponseRedirect(target)
