"""The app's own 403 pages (portal CSRF brief, 2026-11-03): Django's plain "Forbidden" page is
never shown.

- :func:`csrf_failure` is ``CSRF_FAILURE_VIEW``: a form that failed the CSRF check. Under
  ``/portal/`` the portal shell (:func:`portal.views.form_failed`), elsewhere the staff error
  card (``finance/form_failed.html``); both say "Something went wrong with the form" with
  "Try again" (:func:`try_again`). Nothing was saved: the view never ran. Django's reason is
  logged with the route's name, never the path (a link page's token is in it), and never shown.
- :func:`permission_denied` is ``handler403``: the staff "Not allowed" card
  (``accounts/403.html``), and under ``/portal/`` the portal's 404 (the portal shows nothing
  it refuses).
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from accounts.registry import view_name
from accounts.views import forbidden
from portal import views as portal_views
from portal.middleware import PORTAL_PREFIX, safe_next

logger = logging.getLogger("django.security.csrf")

#: The portal's routes that answer POST only: "Try again" cannot open them.
#: (``tests/test_csrf_failure.py`` checks this against the routes.)
PORTAL_POST_ONLY_ROUTE_NAMES: frozenset[str] = frozenset({"portal:logout", "portal:password"})


def _in_portal(request: HttpRequest) -> bool:
    return request.path_info.startswith(PORTAL_PREFIX)


def _same_site_path(raw: str, request: HttpRequest) -> str:
    """The path and query of ``raw`` when it is an address of this site, else ``""``."""
    if not raw or not url_has_allowed_host_and_scheme(
        raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return ""
    parts = urlsplit(raw)
    if not parts.path.startswith("/") or "//" in parts.path or "\\" in raw:
        return ""
    return parts.path + (f"?{parts.query}" if parts.query else "")


def try_again(request: HttpRequest) -> str:
    """Where "Try again" goes: the page the form was on (the ``Referer``, when it is this site
    and the same shell), else the request's own path when it is a page that answers GET, else
    the shell's home (``/portal/`` or the dashboard)."""
    came_from = _same_site_path(request.META.get("HTTP_REFERER", ""), request)
    if _in_portal(request):
        if safe_next(came_from):
            return came_from
        if view_name(request) not in PORTAL_POST_ONLY_ROUTE_NAMES:
            own = safe_next(request.get_full_path())
            if own:
                return own
        return reverse("portal:home")
    if came_from and not came_from.startswith(PORTAL_PREFIX):
        return came_from
    return reverse("ledger:home")


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """``CSRF_FAILURE_VIEW``: the styled 403 of the request's shell."""
    logger.warning("CSRF verification failed (%s) on route %s", reason, view_name(request))
    again = try_again(request)
    if _in_portal(request):
        response = portal_views.form_failed(request, again)
    else:
        response = render(request, "finance/form_failed.html", {"again": again}, status=403)
    # Logged above without the path; Django's own line would carry it, and a link's token.
    response._has_been_logged = True  # type: ignore[attr-defined]
    return response


def permission_denied(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    """``handler403``."""
    if _in_portal(request):
        return portal_views.not_found(request)
    return forbidden(request)
