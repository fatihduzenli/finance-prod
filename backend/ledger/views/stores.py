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
store's facts (PLAN-012e-2 D7: the address on one line, the status as a badge) and its
Store cost block (D8: the landlord rent in effect today, or "unknown"; the margin is in
the P&L only); the New store form also saves the first cost row (D9).
Every POST ends in a redirect, whatever the outcome; a form with errors is held in the
session for the next GET, as on the operator and lease screens.

The list exports as CSV (``stores:export``, PLAN-011 D5): the same search, every page.
The search (``?q=``, PLAN-012d D16) keeps the stores whose number (as a prefix), name,
city or operator matches every word; it is kept by the paging and export links.

The detail page is the store's workspace (docs/qa/PLAN-012d-store-workspace.md D1 to D3,
:mod:`ledger.services.workspace`): a header card, the action bar for billing and admin,
and tabs rendered one at a time (``?tab=``): Charges, Invoices (with the store's drafts),
Payments, Schedules, Documents, Contact log (the entries about a lease of the store, with
**Add entry** for billing; PLAN-012 D3) and History (admin). ``stores:record_payment``
(billing; one payment allocated over the open charges, PLAN-012d D6),
``stores:schedule_payment`` (billing) and ``stores:charge_now_pick`` (admin) are the bar's
Record a payment, Schedule a payment and Charge now; the last two moved into the invoice
builder's "How to collect" (PLAN-012e-2 D4) and redirect to the store's New invoice page
with the choice pre-selected (GET only; refused below those roles).
``stores:create_charges`` (POST, billing) is the bar's Create missing charges: the rent
run for this store only (:func:`ledger.services.rent_run.catch_up_store`, PLAN-012d D13),
on the server's today, with what it created as the message, back to the Charges tab.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import QuerySet
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from finance import usps
from ledger.forms import (
    ALLOCATION_PREFIX,
    CREDIT_PREFIX,
    AllocationPaymentForm,
    StoreCostCloseForm,
    StoreCostCorrectForm,
    StoreCostForm,
    StoreCreateForm,
    StoreForm,
)
from ledger.models import (
    DocumentKind,
    Store,
    StoreCost,
    StoreCostSource,
)
from ledger.services import (
    contacts,
    credits,
    csv_export,
    invoice_builder,
    lease_end,
    payments_entry,
    rent_run,
    search,
    store_costs,
    workspace,
)
from ledger.services import stores as service
from ledger.services.csv_export import CsvWriter
from ledger.views import contact_log, documents, workspace_cards
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
    """The stores the list shows for the request's search (PLAN-012d D16: every word
    matches the store number as a prefix, the name, the city or an operator's name;
    :func:`ledger.services.search.filter_stores`), in its order."""
    query = search.clean_query(request.GET.get("q"))
    stores = search.filter_stores(Store.objects.all(), query)
    return stores.order_by(Lower("external_label"), "id"), query


@role_required
@require_safe
def store_list(request: HttpRequest) -> HttpResponse:
    """The stores list (PLAN-012f-a D3): the counts line, the filters (search, State, Status,
    Balance, Sort; :func:`ledger.services.stores.store_list`) over every store, then the page.
    The spreadsheet keeps the search only (its rows are PLAN-011 D5's)."""
    return render(request, "ledger/stores/list.html", _list_context(request))


def _list_context(request: HttpRequest) -> dict[str, Any]:
    """The Stores list page's context, for the list and for New store's page behind its
    dialog (PLAN-012f-b Batch B)."""
    listed = service.store_list(request.GET, timezone.localdate())
    filters = listed.filters
    page = Paginator(listed.rows, PAGE_SIZE).get_page(request.GET.get("page"))
    return {
        "page": page,
        "listed": listed,
        "filters": filters,
        "query": filters.query,
        "status_choices": service.LIST_STATUSES,
        "balance_choices": service.LIST_BALANCES,
        "sort_choices": service.LIST_SORTS,
        "can_create": meets(_role(request), Role.ADMIN),
        "export_query": urlencode({"q": filters.query}) if filters.query else "",
        "pager_query": urlencode(filters.as_query()),
        "filtered_from": listed.total if filters.narrowed else None,
    }


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
    return render(request, "ledger/stores/detail.html", _detail_context(request, _load(pk)))


def _detail_context(
    request: HttpRequest, store: Store, *, forced_tab: str | None = None
) -> dict[str, Any]:
    """The store page's context, for the page and for Edit store's page behind its dialog
    (PLAN-012f-b Batch B). ``forced_tab`` opens that tab whatever the query says, without its
    filter (PLAN-012f-b D15: the "Invoice saved" dialog over the Invoices tab)."""
    today = timezone.localdate()
    role = _role(request)
    admin = meets(role, Role.ADMIN)
    default = workspace.TAB_DOCUMENTS if request.GET.get("preview") else workspace.TAB_CHARGES
    tab = workspace.chosen_tab(forced_tab or request.GET.get("tab"), role, default=default)
    chosen = "" if forced_tab else request.GET.get("filter", "")
    space = workspace.store_workspace(store, today, role, tab=tab, chosen=chosen)
    in_progress = lease_end.store_in_progress(store)
    context: dict[str, Any] = {
        "store": store,
        "space": space,
        "header": space.header,
        **space.data,
        "leases": store.leases.select_related("operator", "lease_end").order_by("started_on", "id"),
        "vacant_since": lease_end.vacant_since(store, today),
        # PLAN-012b-b D9: a lease end whose statement step was saved, with Continue and
        # Cancel (admin); a draft that never got there shows nothing.
        "end_in_progress": in_progress,
        "end_in_progress_transfer": (
            lease_end.draft_transfer(in_progress) if in_progress is not None else None
        ),
        "can_edit": admin,
        "address_line": service.address_line(store),
        "status_value": service.status_badge(store),
        "cost_history": store_costs.history_entries(store),
        "can_correct_cost": admin,
        "open_cost": store_costs.open_cost(store),
        "close_form": StoreCostCloseForm(),
        "billing": meets(role, Role.BILLING),
        # PLAN-012f D8: the header's lines and the side cards.
        **workspace_cards.header_lines(store, space.header.operator),
        **workspace_cards.side_cards(request, store, today),
    }
    if tab in (workspace.TAB_CONTACTS, workspace.TAB_CHARGES):
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
    return context


@role_required
@require_POST
def store_create_charges(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:create_charges`` (billing, PLAN-012d D13): the rent run's charges
    for this store only, today's due debits and the catch-up; the message names what was
    created, or says nothing was missing; back to the Charges tab."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    store = _load(pk)
    report = rent_run.catch_up_store(store, timezone.localdate(), _actor(request))
    text = rent_run.created_message(report)
    if report.created or report.caught_up:
        messages.success(request, text)
    else:
        messages.info(request, text)
    for item in report.skipped[: rent_run.MESSAGE_LIST_MAX]:
        messages.warning(request, f"Skipped {item.lease.store.external_label}: {item.reason}")
    return redirect(workspace.tab_url(store.pk, workspace.TAB_CHARGES))


PAYMENT_FORM = "ledger/stores/record_payment.html"


def _no_lease(request: HttpRequest, store: Store) -> HttpResponse:
    messages.error(request, payments_entry.NO_LEASE)
    return redirect(workspace.tab_url(store.pk, workspace.TAB_CHARGES))


def _forged(request: HttpRequest, form: AllocationPaymentForm) -> bool:
    """Whether the POST names an allocation or a credit input that is not one of the
    listed open charges' (a forged or stale charge id)."""
    return any(
        key.startswith((ALLOCATION_PREFIX, CREDIT_PREFIX)) and key not in form.fields
        for key in request.POST
    )


def _prefilled(data: dict[str, str], charges: list[payments_entry.OpenCharge]) -> dict[str, str]:
    """A re-rendered form's data with the single open charge's allocation filled with
    ``min(amount, balance)`` when it is empty and the amount reads (D6)."""
    if len(charges) != 1:
        return data
    [charge] = charges
    if (data.get(charge.field) or "").strip():
        return data
    try:
        amount = Decimal((data.get("amount") or "").strip())
    except InvalidOperation:
        return data
    if not amount.is_finite() or amount <= 0:
        return data
    return {**data, charge.field: str(min(amount, charge.balance))}


def _unallocated(form: AllocationPaymentForm) -> Decimal:
    """The amount received less the allocations, as the form holds them (the script keeps
    it live; the service checks it again)."""
    if not form.is_bound:
        return Decimal("0.00")

    def read(name: str) -> Decimal:
        try:
            value = Decimal((form.data.get(name) or "").strip() or "0")
        except InvalidOperation:
            return Decimal("0.00")
        return value if value.is_finite() else Decimal("0.00")

    allocated = sum((read(charge.field) for charge in form.charges), Decimal("0.00"))
    return (read("amount") - allocated).quantize(Decimal("0.01"))


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_record_payment(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:record_payment`` (billing; PLAN-012d D6): one payment received,
    allocated by hand over the open charges of the store's current lease, the rest kept as
    a credit on the lease, the credit held applied (:mod:`ledger.services.payments_entry`).
    Returns to the Payments tab."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    store = _load(pk)
    today = timezone.localdate()
    lease = payments_entry.current_lease(store, today)
    if lease is None:
        return _no_lease(request, store)
    charges = payments_entry.open_charges(lease, today)
    held = credits.balance(lease)
    target = f"store-payment:{store.pk}"
    here = reverse("ledger:stores:record_payment", kwargs={"pk": store.pk})
    if request.method == "POST":
        form = AllocationPaymentForm(request.POST, charges=charges)
        if form.is_valid():
            data = form.cleaned_data
            outcome = payments_entry.record_entry(
                store,
                payments_entry.EntryInput(
                    creation_key=data["creation_key"],
                    method=data["method"],
                    amount=data["amount"],
                    paid_on=data["paid_on"],
                    reference=data["reference"],
                    note=data["note"],
                    allocations=form.amounts(ALLOCATION_PREFIX),
                    applied=form.amounts(CREDIT_PREFIX),
                    keep_credit=data["keep_credit"],
                    unlisted=_forged(request, form),
                ),
                actor=_actor(request),
                today=today,
            )
            if outcome.ok:
                if outcome.replayed:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                for text in outcome.provider_messages:
                    messages.warning(request, text)
                return redirect(workspace.tab_url(store.pk, workspace.TAB_PAYMENTS))
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect(here)
    form = _held(
        request,
        target,
        lambda data: AllocationPaymentForm(_prefilled(data, charges), charges=charges),
    ) or AllocationPaymentForm(
        initial={"creation_key": uuid.uuid4(), "paid_on": today}, charges=charges
    )
    return render(
        request,
        PAYMENT_FORM,
        {
            "store": store,
            "lease": lease,
            "form": form,
            "rows": [
                {
                    "charge": charge,
                    "alloc": form[charge.field],
                    "credit": form[charge.credit_field],
                }
                for charge in charges
            ],
            "credit_held": held,
            "single": charges[0] if len(charges) == 1 else None,
            "unallocated": _unallocated(form),
        },
    )


def builder_url(
    store: Store, lease_id: uuid.UUID | None, choice: str, item: str | None = None
) -> str:
    """The store's New invoice page with the "How to collect" choice pre-selected
    (PLAN-012e-2 D4): the store and lease (both checked already), the choice (one of
    :class:`~ledger.services.invoice_builder.CollectChoice`) and a catalogue code; nothing
    the request typed."""
    values = {"store": str(store.pk)}
    if lease_id is not None:
        values["lease"] = str(lease_id)
    values["collect"] = invoice_builder.CollectChoice(choice).value
    if item:
        values["item"] = item
    return reverse("ledger:invoices:new_store", kwargs={"pk": store.pk}) + "?" + urlencode(values)


@role_required
@require_safe
def store_schedule_payment(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:schedule_payment`` (billing; PLAN-012e-2 D4): "Schedule a payment"
    moved into the invoice builder; this route redirects to the store's New invoice page
    with Schedule for a date chosen. A store with no current lease goes to its Charges tab
    as before."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    store = _load(pk)
    lease = payments_entry.current_lease(store, timezone.localdate())
    if lease is None:
        return _no_lease(request, store)
    return redirect(builder_url(store, lease.pk, invoice_builder.CollectChoice.SCHEDULE))


@role_required
@require_safe
def store_charge_now_pick(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:charge_now_pick`` (admin; PLAN-012e-2 D4): "Charge now" moved into
    the invoice builder; this route redirects to the store's New invoice page with Charge
    now chosen. A charge's own Charge now stays on its page."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    store = _load(pk)
    lease = payments_entry.current_lease(store, timezone.localdate())
    return redirect(
        builder_url(
            store, lease.pk if lease is not None else None, invoice_builder.CollectChoice.CHARGE_NOW
        )
    )


# --- Write --------------------------------------------------------------------------

#: The New / Edit store dialog's fields that take both columns (PLAN-012f-b Batch B).
WIDE_FIELDS: tuple[str, ...] = ("address_line1", "other_cost_description")


def _form_dialog(
    form: StoreForm,
    title: str,
    return_url: str,
    *,
    store: Store | None = None,
    document_form: Any = None,
) -> dict[str, Any]:
    """The New / Edit store dialog (PLAN-012f-b Batch B; ledger/stores/_form_dialog.html),
    open over the page behind it; closing it goes to ``return_url``."""
    return {
        "form": form,
        "title": title,
        "return_url": return_url,
        "store": store,
        "document_form": document_form,
        "wide": WIDE_FIELDS,
        # PLAN-012f-b D13: "Check address" only when the USPS credentials are set.
        "address_check": usps.is_configured(),
    }


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
            try:
                store, created = service.create_store(
                    form.cleaned_data,
                    creation_key=form.cleaned_data["creation_key"],
                    actor=_actor(request),
                )
            except service.CostRefused as cost_refused:
                field = cost_refused.outcome.field
                form.add_error(
                    field if field in form.fields else None, cost_refused.outcome.message
                )
            else:
                if not created:
                    messages.info(request, ALREADY_SAVED)
                    return redirect("ledger:stores:detail", pk=store.pk)
                messages.success(request, f"Store {service.title(store)} added.")
                documents.upload_after_create(request, store, "store")
                return redirect("ledger:stores:detail", pk=store.pk)
        _hold(request, "store-create", form)
        return redirect("ledger:stores:create")
    form = _held(request, "store-create", StoreCreateForm) or StoreCreateForm(
        initial={"creation_key": uuid.uuid4()}
    )
    context = _list_context(request)
    context["form_dialog"] = _form_dialog(
        form,
        "New store",
        reverse("ledger:stores:list"),
        document_form=documents.create_form(DocumentKind.OTHER),
    )
    return render(request, "ledger/stores/list.html", context)


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
    context = _detail_context(request, store)
    context["form_dialog"] = _form_dialog(
        form,
        f"Edit store {service.title(store)}",
        reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
        store=store,
    )
    return render(request, "ledger/stores/detail.html", context)


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


COSTS_ANCHOR = "#store-cost"


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


# --- The address check (PLAN-012f-b D13) ---------------------------------------------

CHECK_OFF = "The address check is not set up."
CHECK_FAILED = "The address service did not answer."
CHECK_NO_MATCH = "No match for this address."
STREET_REQUIRED = "Enter a street to check."


@role_required
@require_POST
def check_address(request: HttpRequest) -> HttpResponse:
    """``ledger:stores:check_address`` (POST, billing): the USPS standardised form of the
    posted street, city, state and ZIP, as JSON, for the store form's "Check address".
    Advice only: nothing is written, and the form saves without it."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    limit = usps.FIELD_MAX_LENGTH
    address = usps.Address(
        street=request.POST.get("street", "")[:limit].strip(),
        city=request.POST.get("city", "")[:limit].strip(),
        state=request.POST.get("state", "")[:limit].strip(),
        zip=request.POST.get("zip", "")[:limit].strip(),
    )
    if not address.street:
        return JsonResponse({"verified": False, "reason": STREET_REQUIRED}, status=400)
    if not usps.is_configured():
        return JsonResponse({"verified": False, "reason": CHECK_OFF})
    result = usps.check(address)
    if isinstance(result, usps.Checked):
        return JsonResponse(
            {
                "verified": True,
                "address": {
                    "street": result.street,
                    "city": result.city,
                    "state": result.state,
                    "zip5": result.zip5,
                    "zip4": result.zip4,
                    "dpv_confirmed": result.dpv_confirmed,
                },
            }
        )
    reason = CHECK_NO_MATCH if isinstance(result, usps.NoMatch) else CHECK_FAILED
    return JsonResponse({"verified": False, "reason": reason})
