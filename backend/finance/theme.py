"""The Light · Dark · System control's POST (docs/qa/PLAN-012g-dark-theme-and-dashboard.md A3;
DESIGN-SPEC §7.1).

``finance:theme`` (``POST /settings/theme/``, every signed-in role, CSRF-protected) stores
``theme`` for the signed-in user (:func:`accounts.themes.set_theme`) and redirects to ``next``
(or, without one, to the ``Referer``: the control does not write the page's own address into
the page) when it is on this site (``url_has_allowed_host_and_scheme`` with this host), else to
the dashboard. A
value outside :data:`accounts.themes.THEMES` is a 400 with a fixed message that does not echo
it, and nothing is saved. No audit row: a display preference. The form
(``finance/_theme_switch.html``) works without script.

``finance:payment_status_period`` (``POST /settings/payment-status/``, PLAN-014 A3; the same
roles, CSRF and error style) stores the dashboard Payment status card's ``period`` (``weekly``
or ``monthly``, :func:`accounts.themes.set_payment_status_period`) and redirects to ``next``
when it is on this site, else to the dashboard (no ``Referer`` fallback: the card's pill always
sends ``next``).
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.themes import (
    PAYMENT_STATUS_PERIODS,
    THEMES,
    set_payment_status_period,
    set_theme,
)
from ledger.views.operators import _actor, role_required

UNKNOWN_THEME = "Choose Light, Dark or System."
UNKNOWN_PERIOD = "Choose Weekly or Monthly."


def _back(request: HttpRequest, target: str) -> HttpResponse:
    """A redirect to ``target`` when it is on this site, else to the dashboard."""
    if url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect("ledger:home")


@role_required
@require_POST
def set_theme_view(request: HttpRequest) -> HttpResponse:
    """``finance:theme``."""
    chosen = request.POST.get("theme", "")
    if chosen not in THEMES:
        return HttpResponseBadRequest(UNKNOWN_THEME, content_type="text/plain; charset=utf-8")
    set_theme(_actor(request), chosen)
    return _back(request, request.POST.get("next", "") or request.headers.get("referer", ""))


@role_required
@require_POST
def set_payment_status_period_view(request: HttpRequest) -> HttpResponse:
    """``finance:payment_status_period``."""
    chosen = request.POST.get("period", "")
    if chosen not in PAYMENT_STATUS_PERIODS:
        return HttpResponseBadRequest(UNKNOWN_PERIOD, content_type="text/plain; charset=utf-8")
    set_payment_status_period(_actor(request), chosen)
    return _back(request, request.POST.get("next", ""))
