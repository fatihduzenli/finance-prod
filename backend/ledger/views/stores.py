"""Store screens: list, detail, create, edit (docs/qa/PLAN-007b-stores.md §1), and the
landlord rent ("Set landlord rent", docs/qa/PLAN-010b D8).

Access: every view requires a signed-in, second-factor-verified user with a role
(middleware plus :func:`~ledger.views.operators.role_required`). The list and the detail
are open to any role; create and edit, GET and POST, need ``admin``; setting the
landlord rent, GET and POST, needs ``billing``. Every POST is checked again by
``accounts.registry``. There is no delete route.

Every change goes through :mod:`ledger.services.stores` or
:mod:`ledger.services.store_costs`, which write the audit log. The detail page shows the
landlord rent in effect today, the operator rent of the lease active today and the
margin, or "unknown" when any of them is unknown (never zero).
Every POST ends in a redirect, whatever the outcome; a form with errors is held in the
session for the next GET, as on the operator and lease screens.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import StoreCostForm, StoreCreateForm, StoreForm
from ledger.models import Store, StoreCostSource
from ledger.services import store_costs
from ledger.services import stores as service
from ledger.views.leases import _admin_only
from ledger.views.operators import (
    CONTROL_CHARACTERS,
    PAGE_SIZE,
    SEARCH_MAX_LENGTH,
    _actor,
    _held,
    _hold,
    _role,
    role_required,
)

ALREADY_SAVED = "This store was already saved. Nothing was changed."


def _load(pk: str) -> Store:
    """The store named in the URL, loaded from the database; 404 otherwise."""
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Store, pk=key)


# --- Read ---------------------------------------------------------------------------


@role_required
@require_safe
def store_list(request: HttpRequest) -> HttpResponse:
    query = CONTROL_CHARACTERS.sub("", request.GET.get("q", "")).strip()[:SEARCH_MAX_LENGTH]
    stores = Store.objects.all()
    if query:
        stores = stores.filter(Q(external_label__icontains=query) | Q(city__icontains=query))
    page = Paginator(stores.order_by(Lower("external_label"), "id"), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "ledger/stores/list.html",
        {"page": page, "query": query, "can_create": meets(_role(request), Role.ADMIN)},
    )


@role_required
@require_safe
def store_detail(request: HttpRequest, pk: str) -> HttpResponse:
    store = _load(pk)
    today = timezone.localdate()
    return render(
        request,
        "ledger/stores/detail.html",
        {
            "store": store,
            "leases": store.leases.select_related("operator").order_by("started_on", "id"),
            "can_edit": meets(_role(request), Role.ADMIN),
            "can_set_cost": meets(_role(request), Role.BILLING),
            "cost": store_costs.current_cost(store, today),
            "operator_rent": store_costs.operator_rent(store, today),
            "margin": store_costs.margin(store, today),
            "cost_history": store_costs.history(store),
        },
    )


# --- Write --------------------------------------------------------------------------


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_create(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    if request.method == "POST":
        existing = service.store_for_creation_key(request.POST.get("creation_key"))
        if existing is not None:
            messages.info(request, ALREADY_SAVED)
            return redirect("ledger:stores:detail", pk=existing.pk)
        form = StoreCreateForm(request.POST)
        if form.is_valid():
            store, created = service.create_store(
                form.cleaned_data,
                creation_key=form.cleaned_data["creation_key"],
                actor=_actor(request),
            )
            if created:
                messages.success(request, "Store saved.")
            else:
                messages.info(request, ALREADY_SAVED)
            return redirect("ledger:stores:detail", pk=store.pk)
        _hold(request, "store-create", form)
        return redirect("ledger:stores:create")
    form = _held(request, "store-create", StoreCreateForm) or StoreCreateForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(request, "ledger/stores/form.html", {"form": form, "store": None})


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_edit(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    store = _load(pk)
    target = f"store-edit:{store.pk}"
    if request.method == "POST":
        form = StoreForm(request.POST, instance=store)
        if form.is_valid():
            service.update_store(store.pk, form.cleaned_data, actor=_actor(request))
            messages.success(request, "Store saved.")
            return redirect("ledger:stores:detail", pk=store.pk)
        _hold(request, target, form)
        return redirect("ledger:stores:edit", pk=store.pk)
    form = _held(request, target, lambda data: StoreForm(data, instance=store)) or StoreForm(
        instance=store
    )
    return render(request, "ledger/stores/form.html", {"form": form, "store": store})


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_set_cost(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:set_cost``: the landlord rent from a date (billing, D8)."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    store = _load(pk)
    target = f"store-set-cost:{store.pk}"
    if request.method == "POST":
        form = StoreCostForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = store_costs.set_cost(
                store,
                data["landlord_rent"],
                data["effective_from"],
                other_monthly_cost=data.get("other_monthly_cost"),
                other_cost_description=data.get("other_cost_description") or "",
                landlord_lease_end=data.get("landlord_lease_end"),
                notes=data.get("notes") or "",
                source=StoreCostSource.STAFF,
                actor=_actor(request),
            )
            if outcome.ok:
                if outcome.action == store_costs.ACTION_UNCHANGED:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                return redirect("ledger:stores:detail", pk=store.pk)
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:stores:set_cost", pk=store.pk)
    current = store_costs.open_cost(store)
    initial = {}
    if current is not None:
        initial = {
            "landlord_rent": current.landlord_rent,
            "other_monthly_cost": current.other_monthly_cost,
            "other_cost_description": current.other_cost_description,
            "landlord_lease_end": current.landlord_lease_end,
            "notes": current.notes,
        }
    form = _held(request, target, StoreCostForm) or StoreCostForm(initial=initial)
    return render(
        request, "ledger/stores/cost_form.html", {"form": form, "store": store, "current": current}
    )
