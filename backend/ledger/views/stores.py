"""Store screens: list, detail, create, edit (docs/qa/PLAN-007b-stores.md §1), and the
landlord rent ("Set landlord rent", docs/qa/PLAN-010b D8), and the corrections of the
cost rows ("Correct" and "Close", docs/qa/PLAN-010c D10).

Access: every view requires a signed-in, second-factor-verified user with a role
(middleware plus :func:`~ledger.views.operators.role_required`). The list and the detail
are open to any role; create and edit, GET and POST, need ``admin``; setting the
landlord rent, GET and POST, needs ``billing``; correcting a cost row (GET and POST) and
closing the open one (POST) need ``admin``. Every POST is checked again by
``accounts.registry``. There is no delete route.

Every change goes through :mod:`ledger.services.stores` or
:mod:`ledger.services.store_costs`, which write the audit log. The detail page shows the
landlord rent in effect today, the operator rent of the lease active today and the
margin, or "unknown" when any of them is unknown (never zero).
Every POST ends in a redirect, whatever the outcome; a form with errors is held in the
session for the next GET, as on the operator and lease screens.

The list exports as CSV (``stores:export``, PLAN-011 D5): the same search, every page.

The detail page is the store's workspace (docs/qa/PLAN-012d-store-workspace.md D1 to D3,
:mod:`ledger.services.workspace`): a header card, the action bar for billing and admin,
and tabs rendered one at a time (``?tab=``): Charges, Invoices (with the store's drafts),
Payments, Schedules, Documents, Contact log (the entries about a lease of the store, with
**Add entry** for billing; PLAN-012 D3) and History (admin). ``stores:record_payment``
(billing) and ``stores:charge_now_pick`` (admin) are the bar's Record a payment and
Charge now; their GET is refused below those roles.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, QuerySet
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import (
    StoreCostCloseForm,
    StoreCostCorrectForm,
    StoreCostForm,
    StoreCreateForm,
    StoreForm,
)
from ledger.models import (
    CollectionMethod,
    DocumentKind,
    OneOffCharge,
    OneOffChargeStatus,
    Store,
    StoreCost,
    StoreCostSource,
)
from ledger.services import (
    contacts,
    csv_export,
    lease_end,
    one_off_charges,
    store_costs,
    workspace,
)
from ledger.services import stores as service
from ledger.services.csv_export import CsvWriter
from ledger.views import contact_log, documents
from ledger.views.leases import _admin_only
from ledger.views.operators import (
    PAGE_SIZE,
    _actor,
    _held,
    _hold,
    _role,
    csv_response,
    export_name,
    role_required,
    search_text,
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


def _listed(request: HttpRequest) -> tuple[QuerySet[Store], str]:
    """The stores the list shows for the request's search, in its order."""
    query = search_text(request)
    stores = Store.objects.all()
    if query:
        stores = stores.filter(Q(external_label__icontains=query) | Q(city__icontains=query))
    return stores.order_by(Lower("external_label"), "id"), query


@role_required
@require_safe
def store_list(request: HttpRequest) -> HttpResponse:
    stores, query = _listed(request)
    page = Paginator(stores, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/stores/list.html",
        {
            "page": page,
            "query": query,
            "can_create": meets(_role(request), Role.ADMIN),
            "export_query": urlencode({"q": query}) if query else "",
        },
    )


EXPORT_HEADER = (
    "Store",
    "Store number",
    "City",
    "State",
    "Open date",
    "Needs open date",
    "Status",
    "Closed on",
    "Needs review",
)


@role_required
@require_safe
def store_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:stores:export`` (PLAN-011 D5): the list's rows, every page."""
    stores, _ = _listed(request)
    records = (
        [
            store.external_label,
            store.store_number,
            store.city,
            store.state,
            store.open_date,
            store.needs_open_date,
            store.get_status_display(),
            store.closed_at,
            store.needs_review,
        ]
        for store in stores[: csv_export.CSV_MAX_ROWS + 1].iterator()
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("stores"))


@role_required
@require_safe
def store_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """The store page as the workspace (PLAN-012d D1 to D3): the header card, the action
    bar (billing and admin), the open tab (``?tab=``, Charges by default; a document
    preview opens Documents), then the store's details, costs and leases."""
    store = _load(pk)
    today = timezone.localdate()
    role = _role(request)
    admin = meets(role, Role.ADMIN)
    default = workspace.TAB_DOCUMENTS if request.GET.get("preview") else workspace.TAB_CHARGES
    tab = workspace.chosen_tab(request.GET.get("tab"), role, default=default)
    space = workspace.store_workspace(
        store, today, role, tab=tab, chosen=request.GET.get("filter", "")
    )
    in_progress = lease_end.store_in_progress(store)
    context: dict[str, Any] = {
        "store": store,
        "space": space,
        "header": space.header,
        **space.data,
        "leases": store.leases.select_related("operator", "lease_end").order_by("started_on", "id"),
        "vacant_since": lease_end.vacant_since(store, today),
        # PLAN-012b-b D9: a lease end whose statement step opened, with Continue and
        # Cancel (admin); a draft that never got there shows nothing.
        "end_in_progress": in_progress,
        "end_in_progress_transfer": (
            lease_end.draft_transfer(in_progress) if in_progress is not None else None
        ),
        "can_edit": admin,
        "can_set_cost": meets(role, Role.BILLING),
        "cost": store_costs.current_cost(store, today),
        "operator_rent": store_costs.operator_rent(store, today),
        "margin": store_costs.margin(store, today),
        "cost_history": store_costs.history_entries(store),
        "can_correct_cost": admin,
        "open_cost": store_costs.open_cost(store),
        "close_form": StoreCostCloseForm(),
        "billing": meets(role, Role.BILLING),
    }
    if tab == workspace.TAB_CONTACTS:
        # PLAN-012 D3: the entries about a lease of this store, and Add entry.
        context.update(
            {
                "contact_rows": contact_log.log_rows(
                    request,
                    list(
                        contacts.store_entries(
                            store, include_deleted=contact_log.include_deleted(request)
                        )[: contacts.PAGE_LIMIT + 1]
                    ),
                    store=store,
                ),
                "contact_limit": contacts.PAGE_LIMIT,
                "contact_add_url": contact_log.store_add_url(request, store, today),
            }
        )
    if tab == workspace.TAB_DOCUMENTS:
        context.update(documents.section_context(request, store))
    return render(request, "ledger/stores/detail.html", context)


@role_required
@require_safe
def store_record_payment(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:record_payment`` (billing; PLAN-012d D2). Batch B of PLAN-012d
    builds the payment form here (one amount allocated over the store's open charges,
    D6). Until then the bar's "Record a payment" opens the per-charge choice: the
    Charges tab, open charges, each with its Record payment link."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    store = _load(pk)
    return redirect(workspace.tab_url(store.pk, workspace.TAB_CHARGES))


@role_required
@require_safe
def store_charge_now_pick(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:charge_now_pick`` (admin; PLAN-012d D2): the store's one-off
    charges collected by Charge now that are still owed, each with the sentence that
    stops it or a button to its existing confirmation page. Local data only."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    store = _load(pk)
    candidates = (
        OneOffCharge.objects.filter(
            lease__store=store, collection=CollectionMethod.CHARGE_NOW, balance__gt=0
        )
        .exclude(status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .select_related("operator", "lease__operator", "lease__store", "fee_type")
        .order_by("due_date", "created_at", "id")
    )
    rows = [
        {"charge": charge, "refusal": one_off_charges.charge_now_refusal(charge)}
        for charge in candidates
    ]
    return render(
        request,
        "ledger/stores/charge_now_pick.html",
        {
            "store": store,
            "eligible": [row for row in rows if not row["refusal"]],
            "refused": [row for row in rows if row["refusal"]],
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
                documents.upload_after_create(request, store, "store")
            else:
                messages.info(request, ALREADY_SAVED)
            return redirect("ledger:stores:detail", pk=store.pk)
        _hold(request, "store-create", form)
        return redirect("ledger:stores:create")
    form = _held(request, "store-create", StoreCreateForm) or StoreCreateForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(
        request,
        "ledger/stores/form.html",
        {"form": form, "store": None, "document_form": documents.create_form(DocumentKind.OTHER)},
    )


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


COSTS_ANCHOR = "#store-costs"


def _cost_row(store: Store, row: str) -> StoreCost:
    """A cost row of ``store`` named in the URL; 404 otherwise."""
    try:
        key = uuid.UUID(row)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(StoreCost, pk=key, store=store)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_correct_cost(request: HttpRequest, pk: str, row: str) -> HttpResponse:
    """``ledger:stores:correct_cost``: correct one live cost row (admin, D10)."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    store = _load(pk)
    cost = _cost_row(store, row)
    target = f"store-correct-cost:{cost.pk}"
    if request.method == "POST":
        form = StoreCostCorrectForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = store_costs.correct_cost(
                store,
                cost,
                store_costs.CostValues(
                    landlord_rent=data["landlord_rent"],
                    other_monthly_cost=data.get("other_monthly_cost"),
                    other_cost_description=data.get("other_cost_description") or "",
                    landlord_lease_end=data.get("landlord_lease_end"),
                    notes=data.get("notes") or "",
                ),
                data["effective_from"],
                data.get("effective_to"),
                data["change_note"],
                _actor(request),
            )
            if outcome.ok:
                if outcome.action == store_costs.ACTION_UNCHANGED:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                return redirect(
                    reverse("ledger:stores:detail", kwargs={"pk": store.pk}) + COSTS_ANCHOR
                )
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:stores:correct_cost", pk=store.pk, row=cost.pk)
    form = _held(request, target, StoreCostCorrectForm) or StoreCostCorrectForm(
        initial={
            "landlord_rent": cost.landlord_rent,
            "effective_from": cost.effective_from,
            "effective_to": cost.effective_to,
            "other_monthly_cost": cost.other_monthly_cost,
            "other_cost_description": cost.other_cost_description,
            "landlord_lease_end": cost.landlord_lease_end,
            "notes": cost.notes,
        }
    )
    return render(
        request, "ledger/stores/cost_correct.html", {"form": form, "store": store, "cost": cost}
    )


@role_required
@require_POST
def store_close_cost(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:close_cost``: end the open cost row on a date (admin, D10)."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    store = _load(pk)
    destination = reverse("ledger:stores:detail", kwargs={"pk": store.pk}) + COSTS_ANCHOR
    form = StoreCostCloseForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, str(error))
        return redirect(destination)
    outcome = store_costs.close_cost(
        store, form.cleaned_data["effective_to"], form.cleaned_data["change_note"], _actor(request)
    )
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(destination)
