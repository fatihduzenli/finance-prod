"""Admin › Settings (docs/qa/PLAN-014 B2): the app's own settings, one card per section.

Admin only: ``ledger:settings:index`` is read-only in ``accounts.registry`` and its view
refuses anyone below admin (403); the brand actions are ``RouteRule(ADMIN, POST)`` and their
views refuse below admin too. Batch B has one card, **Brands** (:mod:`ledger.services.brands`):
add, rename, make active or inactive, move up or down; each action answers with a redirect
to the page and its outcome as a message (a refusal never repeats what was posted).
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from ledger.services import brands
from ledger.views.leases import _admin_only
from ledger.views.operators import _actor, role_required

INDEX = "ledger:settings:index"


def _brand_id(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _answer(request: HttpRequest, outcome: brands.Outcome) -> HttpResponse:
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(INDEX)


@role_required
@require_safe
def settings_index(request: HttpRequest) -> HttpResponse:
    """``ledger:settings:index`` (admin): the Brands card."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return render(
        request,
        "ledger/settings/index.html",
        {"brand_rows": brands.rows(), "name_max": brands.NAME_MAX},
    )


@role_required
@require_POST
def brand_add(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return _answer(request, brands.add(request.POST.get("name"), _actor(request)))


@role_required
@require_POST
def brand_save(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return _answer(request, brands.rename(_brand_id(pk), request.POST.get("name"), _actor(request)))


@role_required
@require_POST
def brand_toggle(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return _answer(request, brands.toggle(_brand_id(pk), _actor(request)))


@role_required
@require_POST
def brand_move(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    direction = request.POST.get("direction")
    return _answer(
        request,
        brands.move(
            _brand_id(pk), direction if isinstance(direction, str) else "", _actor(request)
        ),
    )
