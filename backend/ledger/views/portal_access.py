"""The operator page's PORTAL card actions (docs/qa/PLAN-014-per-lease-billing-messaging-portal-
deposits.md H2): billing, POST only, each audited by :mod:`portal.invitations`.

- ``ledger:operators:portal_invite``: "Invite to the portal", and "Resend invitation" while it
  is not accepted; the message goes to the operator's e-mail through the Outbox.
- ``ledger:operators:portal_disable`` / ``portal_enable``: portal access off and on.
- ``ledger:operators:portal_reset``: "Reset password" (an active account).

The operator in the URL is loaded first (404 otherwise); the outcome is a message on the page.
"""

from __future__ import annotations

from collections.abc import Callable

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from ledger.views.operators import _actor, _load, role_required
from ledger.views.run import _billing_only
from portal import invitations


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
    return redirect(f"{reverse('ledger:operators:detail', kwargs={'pk': operator.pk})}#portal-card")


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
