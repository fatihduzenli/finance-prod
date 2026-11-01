"""Account views: the 401/403 pages, "sign out everywhere" and the staff sign-in page.

The sign-in page (PLAN-014 I-b) is django-two-factor's login wizard with two additions:
the password step's "Keep me signed in" box (:class:`StaffAuthenticationForm`) and the
support e-mail for the page's "Need help?" and "Forgot password?" links. "Keep me signed in"
never lengthens a session: the idle and absolute limits (``SESSION_COOKIE_AGE``,
``SESSION_ABSOLUTE_MAX_AGE``, :class:`accounts.middleware.SessionPolicyMiddleware`) hold
either way. Ticked, the session cookie is the persistent one it always was (it survives a
browser restart, within those limits); unticked, it is a browser-session cookie
(``set_expiry(0)``). There is no self-service password reset: "Forgot password?" is a
``mailto:`` to the support e-mail.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth import logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.sessions.models import Session
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from two_factor.views import LoginView

from accounts.models import UserSession
from finance.context_processors import company_settings

#: The login wizard's prefix as the package names it (from its view class, ``LoginView``).
WIZARD_PREFIX = "login_view"
#: The password step's box; its posted name is ``auth-remember``.
REMEMBER_FIELD = "remember"
#: Where the wizard's storage keeps the box's answer between the steps.
KEEP_SIGNED_IN_KEY = "keep_signed_in"


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


class StaffAuthenticationForm(AuthenticationForm):
    """The password step: the username and the password, and "Keep me signed in"."""

    remember = forms.BooleanField(required=False, label="Keep me signed in")


class StaffLoginView(LoginView):  # type: ignore[misc]  # two_factor ships no types
    """``two_factor:login`` (PLAN-014 I-b): the package's wizard with
    :class:`StaffAuthenticationForm` as its password step (see the module docstring)."""

    form_list = (
        (LoginView.AUTH_STEP, StaffAuthenticationForm),
        *LoginView.form_list[1:],
    )

    def get_prefix(self, request: HttpRequest, *args: Any, **kwargs: Any) -> str:
        """The package view's wizard prefix (``login_view``), so the posted names
        (``login_view-current_step``) and the stored steps stay the package's."""
        return WIZARD_PREFIX

    def get_context_data(self, form: forms.Form, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(form, **kwargs)
        context["support_email"] = company_settings(self.request).support_email
        return context

    def process_step(self, form: forms.Form) -> Any:
        """The package keeps nothing of the password step but the user (no password in the
        session); the box's answer is kept beside it for :meth:`done`."""
        found = super().process_step(form)
        if self.steps.current == self.AUTH_STEP:
            keep = form.is_valid() and bool(form.cleaned_data.get(REMEMBER_FIELD))
            self.storage.data[KEEP_SIGNED_IN_KEY] = keep
        return found

    def done(self, form_list: Any, **kwargs: Any) -> HttpResponse:
        keep = self.storage.data.get(KEEP_SIGNED_IN_KEY) is True
        response: HttpResponse = super().done(form_list, **kwargs)
        if not keep:
            # A browser-session cookie; the session's limits are the same either way.
            self.request.session.set_expiry(0)
        return response
