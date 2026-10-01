"""The returns queue (docs/qa/PLAN-008-webhooks.md D6).

Any role may read the queue; resolving needs ``billing`` (``accounts.registry`` and again
here) and a note. Resolving records who and why; it changes no charge and calls no
provider. There is no re-debit action anywhere.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from ledger.forms import ResolveReturnForm
from ledger.models import Payment
from ledger.services import returns
from ledger.views.operators import PAGE_SIZE, _actor, _role, role_required
from ledger.views.run import _billing_only


@role_required
@require_safe
def return_list(request: HttpRequest) -> HttpResponse:
    page = Paginator(returns.open_returns(), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/returns/list.html",
        {
            "page": page,
            "rows": [returns.row(payment) for payment in page],
            "can_resolve": meets(_role(request), Role.BILLING),
        },
    )


@role_required
@require_POST
def return_resolve(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    if not Payment.objects.filter(pk=key).exists():
        raise Http404
    form = ResolveReturnForm(request.POST)
    if not form.is_valid():
        errors = form.errors.get("note") or [returns.RESOLVE_NOTE_REQUIRED]
        messages.error(request, str(errors[0]))
        return redirect("ledger:returns:list")
    outcome = returns.resolve_return(key, note=form.cleaned_data["note"], actor=_actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:returns:list")
