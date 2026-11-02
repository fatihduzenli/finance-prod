"""The operator page's PORTAL card actions (docs/qa/PLAN-014-per-lease-billing-messaging-portal-
deposits.md H2): billing, POST only, each audited by :mod:`portal.invitations`.

- ``ledger:operators:portal_invite``: "Invite to the portal", and "Resend invitation" while it
  is not accepted; the message goes to the operator's e-mail through the Outbox.
- ``ledger:operators:portal_disable`` / ``portal_enable``: portal access off and on.
- ``ledger:operators:portal_reset``: "Reset password" (an active account).
- ``ledger:operators:portal_copy_link``: "Copy invite link" (admin; the route exists on the
  sandbox only, ``ledger/urls.py``). Its answer is the operator page itself with a dialog open
  on the new one-time link: the link is in that one response only (no message, no redirect,
  no log), which carries ``Cache-Control: no-store`` and ``Referrer-Policy: no-referrer``.

The operator in the URL is loaded first (404 otherwise); the outcome is a message on the page.
"""

from __future__ import annotations

from collections.abc import Callable

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.roles import Role, meets, role_of
from accounts.views import forbidden
from ledger.views.operators import _actor, _detail_context, _load, role_required
from ledger.views.run import _billing_only
from portal import invitations


def _card_url(pk: object) -> str:
    return f"{reverse('ledger:operators:detail', kwargs={'pk': pk})}#portal-card"


def _act(
    request: HttpRequest,
    pk: str,
    action: Callable[..., invitations.Outcome],
) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    operator = _load(pk)
    outcome = action(operator.pk, _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(_card_url(operator.pk))


@role_required
@require_POST
def portal_invite(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:portal_invite`` (billing)."""
    return _act(request, pk, invitations.invite)


@role_required
@require_POST
def portal_disable(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:portal_disable`` (billing)."""
    return _act(request, pk, invitations.disable)


@role_required
@require_POST
def portal_enable(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:portal_enable`` (billing)."""
    return _act(request, pk, invitations.enable)


@role_required
@require_POST
def portal_reset(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:portal_reset`` (billing)."""
    return _act(request, pk, invitations.send_reset)


@role_required
@require_POST
def portal_copy_link(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:portal_copy_link`` (admin): the operator page with the link, once."""
    if not meets(role_of(request.user), Role.ADMIN):
        return forbidden(request)
    operator = _load(pk)
    outcome = invitations.copy_link(operator.pk, _actor(request))
    if not outcome.ok:
        messages.error(request, outcome.message)
        return redirect(_card_url(operator.pk))
    context = _detail_context(request, operator)
    context["copied_link"] = outcome
    response = render(request, "ledger/operators/detail.html", context)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
