"""Account views: the 401/403 pages and "sign out everywhere"."""

from __future__ import annotations

from django.contrib.auth import logout
from django.contrib.sessions.models import Session
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.models import UserSession


def _relative_next(request: HttpRequest) -> str | None:
    """The current path as a login ``next`` target, only if it is a same-site path."""
    target = request.get_full_path()
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return None
    if not url_has_allowed_host_and_scheme(target, allowed_hosts=set()):
        return None
    return target


def unauthenticated(request: HttpRequest) -> HttpResponse:
    """401 for a request without a session. No redirect (PLAN-003 §4)."""
    return render(request, "accounts/401.html", {"next": _relative_next(request)}, status=401)


def mfa_required(request: HttpRequest, *, can_enrol: bool) -> HttpResponse:
    """403 for a signed-in user who has not passed the second factor."""
    return render(request, "accounts/mfa_required.html", {"can_enrol": can_enrol}, status=403)


def forbidden(request: HttpRequest) -> HttpResponse:
    """403 for a request the role registry refuses."""
    return render(request, "accounts/403.html", status=403)


def _delete_sessions_of(user_pk: str) -> None:
    """Delete every live session that belongs to the user, whatever its key.

    Sessions are matched by decoding them rather than through UserSession rows alone,
    so a session whose key was rotated (password change) is not missed.
    """
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        if str(session.get_decoded().get("_auth_user_id")) == user_pk:
            session.delete()


@require_POST
def sign_out_everywhere(request: HttpRequest) -> HttpResponse:
    """End every session the current user holds, this one included. POST only, CSRF on."""
    user = request.user
    user_pk = str(user.pk)
    # logout() flushes this session so it cannot be saved back at the end of the request.
    logout(request)
    _delete_sessions_of(user_pk)
    UserSession.objects.filter(user_id=user_pk).delete()
    return render(request, "accounts/signed_out_everywhere.html")
