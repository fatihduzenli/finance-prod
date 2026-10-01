"""Store screens: list, detail, create, edit (docs/qa/PLAN-007b-stores.md §1).

Access: every view requires a signed-in, second-factor-verified user with a role
(middleware plus :func:`~ledger.views.operators.role_required`). The list and the detail
are open to any role; create and edit, GET and POST, need ``admin``, and the POST is
checked again by ``accounts.registry``. There is no delete route.

Every change goes through :mod:`ledger.services.stores`, which writes the audit log.
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
from django.views.decorators.http import require_http_methods, require_safe

from accounts.roles import Role, meets
from ledger.forms import StoreCreateForm, StoreForm
from ledger.models import Store
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
    return render(
        request,
        "ledger/stores/detail.html",
        {
            "store": store,
            "leases": store.leases.select_related("operator").order_by("started_on", "id"),
            "can_edit": meets(_role(request), Role.ADMIN),
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
