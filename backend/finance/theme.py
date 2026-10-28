"""The Light · Dark · System control's POST (docs/qa/PLAN-012g-dark-theme-and-dashboard.md A3;
DESIGN-SPEC §7.1).

``finance:theme`` (``POST /settings/theme/``, every signed-in role, CSRF-protected) stores
``theme`` for the signed-in user (:func:`accounts.themes.set_theme`) and redirects to ``next``
when it is a path of this site (``url_has_allowed_host_and_scheme``), else to the dashboard. A
value outside :data:`accounts.themes.THEMES` is a 400 with a fixed message that does not echo
it, and nothing is saved. No audit row: a display preference. The form
(``finance/_theme_switch.html``) works without script.
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.themes import THEMES, set_theme
from ledger.views.operators import _actor, role_required

UNKNOWN_THEME = "Choose Light, Dark or System."


@role_required
@require_POST
def set_theme_view(request: HttpRequest) -> HttpResponse:
    """``finance:theme``."""
    chosen = request.POST.get("theme", "")
    if chosen not in THEMES:
        return HttpResponseBadRequest(UNKNOWN_THEME, content_type="text/plain; charset=utf-8")
    set_theme(_actor(request), chosen)
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect("ledger:home")
