"""Lease screens: list, detail, create, edit, end, enrol, pause, resume, stop autopay
(PLAN-007 §4.1, PLAN-010b Batch C, PLAN-012e D5 amendment).

Access: every view requires a signed-in, second-factor-verified user with a role
(middleware plus :func:`~ledger.views.operators.role_required`). Unsafe methods are
checked again by ``accounts.registry``: every lease POST needs ``admin`` (D10). The
enrolment page shows the operator's payment methods, so its GET is limited to ``admin``
here too.

No view calls the provider: every change goes through :mod:`ledger.services.leases`,
which writes the audit log. Enable, pause, resume and stop are queued as jobs
(:mod:`ledger.views.jobs`, PLAN-010 D3) that run the service in the worker; the detail
page shows the latest one. The enrolment page lists the methods from the latest
snapshot ("Methods as of ..."); its "Refresh methods" button (a POST to the same URL
with ``refresh_methods``) queues the read, and the page shows that job's state.
Templates receive masked identifiers only, except the payment-method IDs that are the
values of the enrolment choices. Every POST ends in a redirect, whatever the outcome; a
form with errors is held in the session for the next GET, as on the operator screens.

The list exports as CSV (``leases:export``, PLAN-011 D5): the same filter, every page.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import CharField, Exists, OuterRef, Prefetch, Q, QuerySet
from django.db.models.functions import Cast, Lower
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from banking import services as banking
from banking.models import ALIAS_MAX_LENGTH
from finance.dates import day_text
from ledger.forms import (
    SHOWN_WHEN,
    EnrolForm,
    LeaseBillingForm,
    LeaseCreateForm,
    LeaseEditForm,
    LinkForm,
    OperatorCreateForm,
    PauseCollectionForm,
)
from ledger.models import (
    ChargeNote,
    CollectionStatus,
    DocumentKind,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    LeaseBilling,
    LeaseRentHistory,
    Note,
    NoteEntityType,
    Operator,
    PaymentChannel,
    RentCharge,
    ScheduleState,
    Store,
    SyncStatus,
)
from ledger.services import (
    csv_export,
    detail_cards,
    insurance,
    list_filters,
    one_off_charges,
    search,
    workspace,
)
from ledger.services import lease_end as lease_end_service
from ledger.services import leases as service
from ledger.services import move_in as move_in_service
from ledger.services import stores as store_service
from ledger.services.csv_export import CsvWriter
from ledger.services.leases import LeaseRefused
from ledger.services.one_off_charges import SetupRefused
from ledger.views import documents, store_context
from ledger.views.detail_cards import activity_context
from ledger.views.jobs import busy, job_context, submit
from ledger.views.operators import (
    HELD_FORM_KEY,
    PAGE_SIZE,
    _actor,
    _form_dialog,
    _held,
    _hold,
    _role,
    csv_response,
    export_name,
    role_required,
)
from ledger.wording import format_amount, provider_sentence
from payments import jobs, lease_sync
from payments.hosted_fields import HostedFieldsConfig, get_hosted_fields
from payments.models import JobKind
from payments.provider import mask_identifier

ALREADY_SAVED = "This lease was already saved. Nothing was changed."
REFRESH_QUEUED = "Reading the payment methods."
#: The job kinds the enrolment page shows.
REFRESH_KINDS = (JobKind.LEASE_METHODS_REFRESH,)


def _load(pk: str) -> Lease:
    """The lease named in the URL, loaded from the database; 404 otherwise."""
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Lease.objects.select_related("store", "operator"), pk=key)


# --- Read ---------------------------------------------------------------------------


def _operator_filter(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw or "")
    except ValueError:
        return None


def _searched(leases: QuerySet[Lease], query: str) -> QuerySet[Lease]:
    """``leases`` narrowed to those where every word of ``query`` matches the store's number
    (as a prefix, as the stores list matches it) or name, or the operator's display name
    (PLAN-014 A5; case-insensitive "contains"); unchanged when the query has no word."""
    found = search.words(query)
    if not found:
        return leases
    leases = leases.annotate(_search_number=Cast("store__store_number", CharField()))
    for word in found:
        condition = Q(store__external_label__icontains=word) | Q(
            operator__display_name__icontains=word
        )
        number = search.number_prefix(word)
        if number is not None:
            condition |= Q(_search_number__startswith=number)
        leases = leases.filter(condition)
    return leases


def _listed(request: HttpRequest) -> tuple[QuerySet[Lease], uuid.UUID | None, str]:
    """The leases the list shows, with the rent in effect today and the note flag: of the
    ``operator`` filter, and matching the search (``q``, cleaned as the search cleans it)."""
    today = timezone.localdate()
    in_effect = (
        LeaseRentHistory.objects.filter(effective_from__lte=today)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .order_by("-effective_from")
    )
    operator = _operator_filter(request.GET.get("operator"))
    leases = (
        Lease.objects.select_related("store", "operator")
        .prefetch_related(Prefetch("rent_history", queryset=in_effect, to_attr="rent_now"))
        .annotate(
            has_charge_note=Exists(ChargeNote.objects.filter(rent_charge__lease=OuterRef("pk")))
        )
        .order_by("store__external_label", "id")
    )
    if operator is not None:
        leases = leases.filter(operator_id=operator)
    query = search.clean_query(request.GET.get("q"))
    return _searched(leases, query), operator, query


@role_required
@require_safe
def lease_list(request: HttpRequest) -> HttpResponse:
    """Every lease, optionally of one operator (PLAN-009 D9); a lease whose charges carry
    a charge note is marked; the search (PLAN-014 A5) is kept by the paging and export links."""
    leases, operator, search_query = _listed(request)
    page = Paginator(leases, PAGE_SIZE).get_page(request.GET.get("page"))
    operators = list(Operator.objects.order_by(Lower("display_name"), "id"))
    found = {"operator": str(operator) if operator else "", "q": search_query}
    accepted = {name: value for name, value in found.items() if value}
    return render(
        request,
        "ledger/leases/list.html",
        {
            # PLAN-014 A5: the Filters button's count, the chip and Clear.
            "active_filters": list_filters.active(
                request,
                {"operator": ("Operator", {str(item.pk): item.display_name for item in operators})},
                accepted=accepted,
            ),
            "page": page,
            "can_create": meets(_role(request), Role.ADMIN),
            "operators": operators,
            "operator": operator,
            "search_query": search_query,
            "query": urlencode(accepted),
        },
    )


EXPORT_HEADER = (
    "Store",
    "Store number",
    "Operator",
    "Rent now (USD)",
    "Debit day",
    "Second debit day",
    "Channel",
    "Status",
    "Started on",
    "Ended on",
    "Charges have a note",
    "Needs review",
)


@role_required
@require_safe
def lease_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:leases:export`` (PLAN-011 D5): the list's rows, every page."""
    leases, _, _ = _listed(request)

    def records() -> Iterator[list[object]]:
        for lease in leases[: csv_export.CSV_MAX_ROWS + 1].iterator(chunk_size=500):
            rent = lease.rent_now[0].amount if lease.rent_now else None  # type: ignore[attr-defined]
            yield [
                lease.store.external_label,
                lease.store.store_number,
                lease.operator.display_name if lease.operator is not None else "",
                rent,
                lease.debit_day,
                lease.second_debit_day,
                lease.get_payment_channel_display(),
                lease.get_collection_status_display(),
                lease.started_on,
                lease.ended_on,
                bool(lease.has_charge_note),  # type: ignore[attr-defined]
                lease.needs_review,
            ]

    return csv_response(CsvWriter(EXPORT_HEADER).lines(records()), export_name("leases"))


@role_required
@require_safe
def lease_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """The lease's terms (PLAN-012d D4): dates, rent history, debit days, the proration
    switch, autopay and its controls, notes and documents. Its charges, invoices and
    payments are on the store page, which the page links."""
    lease = _load(pk)
    return render(request, "ledger/leases/detail.html", _detail_context(request, lease))


def _detail_context(request: HttpRequest, lease: Lease) -> dict[str, Any]:
    """The lease page's context (read only); Turn off autopay shows over it in its dialog
    (PLAN-012f-b E-4)."""
    admin = meets(_role(request), Role.ADMIN)
    provider_ach = lease.payment_channel == PaymentChannel.PROVIDER_ACH
    now = timezone.now()
    today = timezone.localdate()
    live = list(service.live_schedules(lease))
    schedules = []
    linked_states = set()
    # PLAN-012b-b D13: autopay moved past a split month starts on its new first debit.
    moved = service.autopay_move(lease, now)
    for row in live:
        ref = service.schedule_ref(row)
        if ref:
            linked_states.add(row.state)
        next_run = (
            service.next_debit_at(row.debit_day, now)
            if row.state == ScheduleState.ACTIVE and row.debit_day is not None
            else None
        )
        if next_run is not None and moved is not None and str(row.pk) in moved.starts:
            next_run = moved.starts[str(row.pk)]
        schedules.append(
            {
                "row": row,
                "schedule_masked": mask_identifier(ref) if ref else "",
                "next_run": next_run,
                "review_message": service.schedule_review_message(lease, row, today),
            }
        )
    return {
        **job_context(lease),
        "lease": lease,
        # PLAN-012f-a D4: the header's line, the Open balance and Deposit tiles and the
        # Activity card (the lease's rows and its live schedules').
        "header_line": detail_cards.header_line(
            store=store_service.title(lease.store),
            operator=lease.operator.display_name if lease.operator is not None else "",
        ),
        "open_balance": detail_cards.lease_open_balance(lease, today),
        # The store page's DEPOSIT line (one query), as the workspace header reads it.
        "deposit_text": workspace._deposit_text(lease)[0],
        **activity_context(
            [(service.ENTITY_SCHEDULE, row.pk) for row in live],
            admin=admin,
            store=lease.store,
            record=(service.ENTITY_LEASE, lease.pk),
        ),
        "rent": service.current_rent(lease, today),
        "upcoming": service.upcoming_rent(lease, today),
        "history": lease.rent_history.order_by("-effective_from"),
        "schedules": schedules,
        "autopay_moved": moved.text if moved is not None else "",
        "blockers": service.autopay_blockers(lease),
        "notes_info": service.enrolment_notes(lease),
        "notes": Note.objects.filter(entity_type=NoteEntityType.LEASE, entity_id=lease.pk).order_by(
            "-created_at"
        ),
        "can_edit": admin,
        "can_end": admin and lease.ended_on is None,
        # PLAN-012c D8: autopay is set up only on a provider-ACH lease; the enrol
        # page and the service keep their own refusal. Pause, resume and stop follow
        # the live schedules, so a schedule left after a channel change stays
        # stoppable.
        "provider_ach": provider_ach,
        "can_enrol": admin and provider_ach,
        "can_pause": admin and ScheduleState.ACTIVE in linked_states,
        "can_resume": admin and ScheduleState.PAUSED in linked_states,
        "can_stop": admin and bool(service.stoppable_schedules(lease)),
        **documents.section_context(request, lease),
    }


# --- Write --------------------------------------------------------------------------


def _admin_only(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    return None


def _lock_known(form: forms.BaseForm, names: tuple[str, ...]) -> None:
    """A lease form opened from the store page (PLAN-012d D2): the fields the store page
    names are hidden and disabled, so their value is the form's initial one whatever is
    posted; the page shows the read-only line instead."""
    for name in names:
        field = form.fields.get(name)
        if field is not None:
            field.widget = forms.HiddenInput()
            field.disabled = True


def _create_context(request: HttpRequest) -> store_context.StoreContext | None:
    """New lease opened from a vacant store's page (``?store=``): that store, or 404."""
    raw = (request.GET.get("store") or "").strip()
    if not raw:
        return None
    try:
        key = uuid.UUID(raw)
    except ValueError:
        raise Http404 from None
    if request.GET.get("lease"):
        raise Http404
    return store_context.from_request(request, store=get_object_or_404(Store, pk=key))


def enter_refusal() -> str:
    """Why "Enter a bank account" cannot be chosen here, or ``""`` (PLAN-014 C4)."""
    return "" if get_hosted_fields().available() else str(BANK_NOT_SET_UP)


def _other_stores(store: Store | None) -> dict[uuid.UUID, list[str]]:
    """Per operator, "#no Name" of the stores of its current leases (not ended by today and
    not ended in status) other than ``store``, by store number: one query for every
    operator (PLAN-014b item 1, decision 3; the options' ``data-stores``)."""
    today = timezone.localdate()
    leases = (
        Lease.objects.filter(operator__isnull=False)
        .filter(Q(ended_on__isnull=True) | Q(ended_on__gte=today))
        .exclude(collection_status=CollectionStatus.ENDED)
        .order_by("store__store_number", "store__external_label", "id")
        .values_list(
            "operator_id", "store_id", "store__store_number", "store__external_label", "store__city"
        )
    )
    found: dict[uuid.UUID, list[str]] = {}
    for operator_id, store_id, number, label, city in leases:
        if store is not None and store_id == store.pk:
            continue
        title = store_service.title_of(number, label, city)
        named = found.setdefault(operator_id, [])
        if title not in named:
            named.append(title)
    return found


def _initial_operator(request: HttpRequest) -> uuid.UUID | None:
    """``?operator=``: an operator not archived (after "+ New operator" saved one,
    decision 3); an unknown, archived or malformed id is ignored."""
    raw = (request.GET.get("operator") or "").strip()
    try:
        key = uuid.UUID(raw)
    except ValueError:
        return None
    return key if Operator.objects.filter(pk=key, archived_at__isnull=True).exists() else None


def _create_form(
    data: Any,
    context: store_context.StoreContext | None,
    operator: uuid.UUID | None = None,
) -> LeaseCreateForm:
    initial: dict[str, Any] = {} if context is None else {"store": context.store.pk}
    if operator is not None:
        initial["operator"] = operator
    operators = Operator.objects.filter(archived_at__isnull=True).values_list("pk", flat=True)
    form = LeaseCreateForm(
        data,
        initial=initial,
        accounts=service.shareable_accounts(operators),
        enter_refusal=enter_refusal(),
        stores=_other_stores(context.store if context is not None else None),
    )
    if context is not None:
        _lock_known(form, ("store",))
    return form


def _header_badge(store: Store) -> tuple[str, str]:
    """The New lease page's badge beside its title (decision 7, the owner's reference): a
    store without a current lease reads as the Stores list's "Vacant · 23 days" (since its
    last lease ended, ``lease_end.vacant_since``) or "Vacant · never leased"; any other the
    store page's status badge."""
    today = timezone.localdate()
    current = store.leases.filter(Q(ended_on__isnull=True) | Q(ended_on__gte=today)).exclude(
        collection_status=CollectionStatus.ENDED
    )
    if current.exists():
        return str(store.get_status_display()), store_service.status_badge(store)
    since = lease_end_service.vacant_since(store, today)
    last_ended = since - timedelta(days=1) if since is not None else None
    return store_service.vacant_label(last_ended, today), "neutral"


def _operator_dialog(request: HttpRequest) -> dict[str, Any]:
    """ "+ New operator" (decision 3): the operator dialog over this page, posting to
    ``ledger:operators:create`` with ``next`` = this page, which it returns to with
    ``&operator=<new id>``. Its ids carry ``op_`` so that they never meet the Billing
    details' (both forms have an e-mail, a phone and an address)."""
    page = request.get_full_path()
    form = OperatorCreateForm(initial={"creation_key": uuid.uuid4()}, auto_id="op_%s")
    for field in form.fields.values():
        shown = field.widget.attrs.get(SHOWN_WHEN)
        if shown:
            field.widget.attrs[SHOWN_WHEN] = shown.replace("id_", "op_")
    dialog = _form_dialog(form, "New operator", page)
    dialog["next"] = page
    return dialog


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_create(request: HttpRequest) -> HttpResponse:
    """New lease; opened from a vacant store's page (``?store=``, PLAN-012d D2) the store
    is known: its field is hidden and the save returns to the store page."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    context = _create_context(request)
    if request.method == "POST":
        existing = service.lease_for_creation_key(request.POST.get("creation_key"))
        if existing is not None:
            messages.info(request, ALREADY_SAVED)
            return redirect(_saved_url(request, existing, context))
        form = _create_form(request.POST, context)
        if form.is_valid():
            # PLAN-014 C4: the billing details belong to a lease with an operator; without
            # one they are not saved (the page hides them until an operator is chosen).
            has_operator = form.cleaned_data.get("operator") is not None
            account = form.cleaned_data["account"] if has_operator else service.ACCOUNT_LATER
            try:
                lease, created, charges = one_off_charges.create_lease_with_checklist(
                    form.cleaned_data,
                    rent=form.cleaned_data["rent"],
                    creation_key=form.cleaned_data["creation_key"],
                    items=form.setup_items(),
                    actor=_actor(request),
                    billing=form.billing_values() if has_operator else None,
                    account=account,
                )
            except SetupRefused as refusal:
                form.add_error(None, _checklist_refusal(refusal))
            except LeaseRefused as refusal:
                form.add_error(
                    refusal.field if refusal.field in form.fields else None, str(refusal)
                )
            else:
                if not created:
                    messages.info(request, ALREADY_SAVED)
                    return redirect(_saved_url(request, lease, context))
                messages.success(request, _saved_message(len(charges)))
                documents.upload_after_create(request, lease, "lease")
                # Decision 6: the move-in invoice, a draft; the lease stays saved without one.
                move_in = move_in_service.create_draft(
                    lease,
                    charges,
                    creation_key=form.cleaned_data["creation_key"],
                    actor=_actor(request),
                )
                if move_in.draft is None:
                    messages.warning(request, move_in.message)
                if account == service.ACCOUNT_ENTER:
                    return redirect("ledger:leases:bank", pk=lease.pk)
                if move_in.draft is not None and context is not None:
                    return redirect(created_url(lease.store_id, move_in.draft))
                if move_in.draft is not None:
                    messages.info(request, MOVE_IN_DRAFTED.format(number=move_in.draft.number))
                return redirect(_saved_url(request, lease, context))
        _hold(request, "lease-create", form)
        return redirect(request.get_full_path())
    held = _held(request, "lease-create", lambda data: _create_form(data, context))
    if held is None:
        held = _create_form(None, context, _initial_operator(request))
        held.initial["creation_key"] = uuid.uuid4()
    page: dict[str, Any] = {
        "form": held,
        "lease": None,
        "document_form": documents.create_form(DocumentKind.LEASE_AGREEMENT),
        "store_context": context,
        "operator_dialog": _operator_dialog(request),
        "new_operator_url": (
            reverse("ledger:operators:create") + "?" + urlencode({"next": request.get_full_path()})
        ),
        "cancel_url": (
            reverse("ledger:stores:detail", kwargs={"pk": context.store.pk})
            if context is not None
            else reverse("ledger:leases:list")
        ),
    }
    if context is not None:
        page["store_title"] = store_service.title(context.store)
        page["header_badge"] = _header_badge(context.store)
    return render(request, "ledger/leases/form.html", page)


#: Decision 6: after a save from outside a store page, where the move-in draft is.
MOVE_IN_DRAFTED = "The move-in invoice {number} is saved as a draft under Drafts."
#: The store page's query that opens the "Lease created" dialog over its Invoices tab.
CREATED_PARAM = "lease_created"
CREATED_TITLE = "Lease created"
#: The dialog reads "Lease created" (its title) then this question (the owner's sentence).
CREATED_QUESTION = "Send the move-in invoice to {operator} now?"


def created_url(store_id: uuid.UUID, draft: Any) -> str:
    """The store page's Invoices tab with the "Lease created" dialog for ``draft``."""
    page = reverse("ledger:stores:detail", kwargs={"pk": store_id})
    query = urlencode({"tab": workspace.TAB_INVOICES, CREATED_PARAM: str(draft.pk)})
    return f"{page}?{query}#tab-{workspace.TAB_INVOICES}"


def created_dialog(request: HttpRequest, store: Store) -> dict[str, Any] | None:
    """The "Lease created" dialog (decision 6) when the store page's query names a move-in
    draft (``?lease_created=``) still a draft, of a lease of this store, for billing and
    above; ``None`` otherwise (writes nothing). "Send e-mail" posts to
    ``ledger:invoices:send`` for the draft (``?store=``); "Not now" goes back to the tab, the
    draft staying under Drafts."""
    raw = (request.GET.get(CREATED_PARAM) or "").strip()
    if not raw or not meets(_role(request), Role.BILLING):
        return None
    try:
        key = uuid.UUID(raw)
    except ValueError:
        return None
    draft = (
        InvoiceDraft.objects.select_related("operator")
        .filter(pk=key, lease__store=store, status=InvoiceDraftStatus.DRAFT)
        .first()
    )
    if draft is None:
        return None
    return {
        "draft": draft,
        "title": CREATED_TITLE,
        "question": CREATED_QUESTION.format(operator=draft.operator.display_name),
        "return_url": workspace.tab_url(store.pk, workspace.TAB_INVOICES),
        "send_url": reverse("ledger:invoices:send", kwargs={"pk": draft.pk})
        + "?"
        + urlencode({"store": str(store.pk)}),
    }


def _saved_url(
    request: HttpRequest, lease: Lease, context: store_context.StoreContext | None
) -> str:
    """After a lease form: the store page when opened from it (D8), else a local
    ``?next=``, else the lease page."""
    page = reverse("ledger:leases:detail", kwargs={"pk": lease.pk})
    return store_context.return_url(request, context, None, page)


def _saved_message(charges: int) -> str:
    if not charges:
        return "Lease saved."
    return f"Lease saved with {charges} setup charge{'s' if charges != 1 else ''}."


def _checklist_refusal(refusal: SetupRefused) -> str:
    """A refused checklist row, named; nothing was saved."""
    name = refusal.fee_type.name if refusal.fee_type is not None else "Setup checklist"
    return f"{name}: {refusal.message} The lease was not saved."


def _edit_form(lease: Lease, data: Any, *, billed: bool, today: date) -> LeaseEditForm:
    """The Edit lease form (PLAN-015a) of ``lease``: "Prorate the first month" only while the
    first month is not ``billed``; a rent change defaults to the next rent due date."""
    open_row = service.open_rent(lease)
    return LeaseEditForm(
        data,
        instance=lease,
        first_month_billed=billed,
        open_rent=open_row.amount if open_row is not None else None,
        rent_default=service.next_rent_due(lease, today),
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit a lease (PLAN-015a): the debit days, the channel, the proration until the first
    month is billed, and a rent change from a date (empty: the next rent due date). The
    store, operator, start and status are read-only here. Opened from the store page
    (``?store=&lease=``, PLAN-012d D2) the save returns to the store page (D8)."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    context = store_context.for_lease(request, lease)
    target = f"lease-edit:{lease.pk}"
    today = timezone.localdate()
    first_charge = service.first_month_charge(lease)
    billed = first_charge is not None
    if request.method == "POST":
        form = _edit_form(lease, request.POST, billed=billed, today=today)
        if form.is_valid():
            try:
                service.update_lease(
                    lease.pk,
                    form.cleaned_data,
                    rent=form.cleaned_data["rent"],
                    rent_effective_from=form.cleaned_data["rent_effective_from"],
                    actor=_actor(request),
                )
            except LeaseRefused as refusal:
                form.add_error(refusal.field, str(refusal))
            else:
                messages.success(request, "Lease saved.")
                return redirect(_saved_url(request, lease, context))
        _hold(request, target, form)
        return redirect(request.get_full_path())
    form = _held(
        request, target, lambda data: _edit_form(lease, data, billed=billed, today=today)
    ) or _edit_form(lease, None, billed=billed, today=today)
    return render(
        request,
        "ledger/leases/form.html",
        {
            "form": form,
            "lease": lease,
            "store_context": context,
            **_edit_context(lease, today, first_charge),
        },
    )


def _edit_context(lease: Lease, today: date, first_charge: RentCharge | None) -> dict[str, Any]:
    """The Edit lease page's read-only parts (PLAN-015a §3): the rent in effect (or the latest
    row) and the next one, the history newest first, the rent change's default date, the
    billing summary (the cached last four only, Rule 1), whether a schedule with a provider
    reference would be marked Needs review (the rows ``update_lease`` flags), and the day the
    first month was billed."""
    now = service.current_rent(lease, today) or service.open_rent(lease)
    upcoming = service.upcoming_rent(lease, today)
    account = detail_cards.lease_account(lease)
    return {
        "rent_now": now,
        "rent_next": upcoming if upcoming is not None and upcoming != now else None,
        "rent_history": list(lease.rent_history.order_by("-effective_from")),
        "rent_default_date": service.next_rent_due(lease, today),
        "billing_summary": [part for part in (account.company, account.email) if part]
        + [account.bank],
        "has_live_schedule": any(
            not row.needs_review and service.schedule_ref(row)
            for row in service.live_schedules(lease)
        ),
        "first_month_billed": first_charge is not None,
        "first_month_billed_on": first_charge.created_at if first_charge else None,
        "can_change_holder": lease.ended_on is None
        and lease.collection_status != CollectionStatus.ENDED,
    }


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_end(request: HttpRequest, pk: str) -> HttpResponse:
    """The old End lease route (PLAN-010b Batch C): admin only, and it now sends the
    admin to the end-of-lease wizard of the lease's store (PLAN-012e D4)."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    return redirect("ledger:stores:end_lease", pk=lease.store_id)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_enrol(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    target = f"lease-enrol:{lease.pk}"
    if request.method == "POST" and "refresh_methods" in request.POST:
        _refresh_methods(request, lease)
        return redirect("ledger:leases:enrol", pk=lease.pk)
    if request.method == "POST":
        form = EnrolForm(request.POST)
        if form.is_valid():
            if busy(request, lease):
                return redirect("ledger:leases:detail", pk=lease.pk)
            submit(
                request,
                JobKind.AUTOPAY_ENABLE,
                lease,
                {
                    "payment_method_id": form.cleaned_data["payment_method_id"],
                    "confirmed": form.cleaned_data["confirm_large_debit"] is True,
                },
            )
            return redirect("ledger:leases:detail", pk=lease.pk)
        _hold(request, target, form)
        return redirect("ledger:leases:enrol", pk=lease.pk)
    form = _held(request, target, EnrolForm) or EnrolForm()
    blockers = service.autopay_blockers(lease)
    plan = service.debit_plan(lease, timezone.now())
    amounts = [debit.amount for debit in plan]
    debits = [
        {
            "debit": debit,
            "starts": debit.starts,
            "large": service.is_large_debit(debit.amount),
            "changes_message": (
                service.RENT_CHANGES.format(date=day_text(debit.changes_on))
                if debit.changes_on is not None
                else ""
            ),
        }
        for debit in plan
    ]
    choices = service.payment_method_choices(lease) if not blockers else service.MethodChoices()
    return render(
        request,
        "ledger/leases/enrol.html",
        {
            **job_context(service.account_lease(lease) or lease, REFRESH_KINDS),
            "lease": lease,
            "form": form,
            "blockers": blockers,
            "notes_info": service.enrolment_notes(lease),
            "debits": debits,
            "any_large": any(item["large"] for item in debits),
            "threshold": format_amount(settings.LARGE_DEBIT_WARNING_AMOUNT),
            "limit_refusal": service.provider_limit_refusal(amounts),
            "confirmation": service.large_debit_confirmation(amounts),
            "choices": choices,
            "linked_method": service.linked_payment_method(lease),
            # PLAN-014 C5: receipts go to the billing e-mail of the lease's account.
            "receipts_email": lease_sync.billing_of(service.account_lease(lease) or lease).email,
            "no_bank_message": service.NO_BANK_METHOD,
            "methods_not_read": service.METHODS_NOT_READ,
        },
    )


def _refresh_methods(request: HttpRequest, lease: Lease) -> None:
    """Queue a read of the payment methods of the lease's customer, on the lease that
    holds it (PLAN-014 C3); refused, with a message, when there is no customer reference
    or a read is already waiting."""
    owner = service.account_lease(lease)
    if owner is None or not service.customer_ref(lease):
        messages.error(request, service.NO_PROVIDER_CUSTOMER)
        return
    if busy(request, owner, REFRESH_KINDS):
        return
    jobs.refresh_methods(owner, request.user)
    messages.info(request, REFRESH_QUEUED)


@role_required
@require_POST
def lease_pause(request: HttpRequest, pk: str) -> HttpResponse:
    lease = _load(pk)
    if not busy(request, lease):
        submit(request, JobKind.AUTOPAY_PAUSE, lease)
    return redirect("ledger:leases:detail", pk=lease.pk)


@role_required
@require_POST
def lease_resume(request: HttpRequest, pk: str) -> HttpResponse:
    lease = _load(pk)
    if not busy(request, lease):
        submit(request, JobKind.AUTOPAY_RESUME, lease)
    return redirect("ledger:leases:detail", pk=lease.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_stop_autopay(request: HttpRequest, pk: str) -> HttpResponse:
    """Stop autopay (admin, PLAN-012e D5 amendment): a confirmation page listing the
    schedules active or paused at the provider; its POST queues the ``autopay_stop`` job,
    which deletes them at the provider."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    rows = service.stoppable_schedules(lease)
    if request.method == "POST":
        if not rows:
            messages.info(request, service.NOTHING_TO_STOP)
        elif not busy(request, lease):
            submit(request, JobKind.AUTOPAY_STOP, lease)
        return redirect("ledger:leases:detail", pk=lease.pk)
    return render(
        request,
        "ledger/leases/stop_autopay.html",
        {
            **_detail_context(request, lease),
            "stop_schedules": [
                {"row": row, "schedule_masked": mask_identifier(ref)} for row, ref in rows
            ],
            "nothing_to_stop": service.NOTHING_TO_STOP,
        },
    )


# --- Billing details (PLAN-014 C3) ----------------------------------------------------

#: The three forms of the Edit billing details dialog, named by the posted ``form``.
BILLING_DETAILS, BILLING_LINK, BILLING_RETRY = "details", "link", "retry"
BILLING_SAVED = "Billing details saved."


def _billing_only(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    return None


def _held_target(request: HttpRequest) -> str:
    """The target of the form held in the session, without consuming it."""
    held = request.session.get(HELD_FORM_KEY)
    return str(held.get("target", "")) if isinstance(held, dict) else ""


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_billing(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit billing details (billing): the LEASE card's dialog over the lease page, with
    the billing details (company, e-mail, phone, address), the lease's connection to the
    payment provider with "Try again" after a failure, and the Link form for the customer
    and bank-method IDs staff copied from the provider's dashboard. Each POST names its
    form (``form``); nothing here calls the provider: a save that needs the provider
    queues ``lease_sync``, the link ``lease_link``, the retry ``lease_retry``."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    details_target, link_target = f"billing:{lease.pk}", f"billing-link:{lease.pk}"
    archived = lease.operator is not None and lease.operator.archived_at is not None
    if request.method == "POST":
        if archived:
            messages.error(request, lease_sync.MESSAGES[lease_sync.ARCHIVED])
            return redirect("ledger:leases:billing", pk=lease.pk)
        which = request.POST.get("form")
        if which == BILLING_RETRY:
            if not busy(request, lease):
                submit(request, JobKind.LEASE_RETRY, lease)
            return redirect("ledger:leases:billing", pk=lease.pk)
        if which == BILLING_LINK:
            link = LinkForm(request.POST)
            if link.is_valid():
                if not busy(request, lease):
                    submit(
                        request,
                        JobKind.LEASE_LINK,
                        lease,
                        {
                            "customer_id": link.cleaned_data["customer_id"],
                            "payment_method_id": link.cleaned_data["payment_method_id"],
                        },
                    )
                return redirect("ledger:leases:billing", pk=lease.pk)
            _hold(request, link_target, link)
            return redirect("ledger:leases:billing", pk=lease.pk)
        form = LeaseBillingForm(request.POST, instance=lease_sync.billing_of(lease))
        if form.is_valid():
            try:
                updated, _ = lease_sync.update_billing(
                    lease.pk, form.cleaned_data, actor=_actor(request)
                )
            except lease_sync.OperatorArchived:  # archived since the check above
                messages.error(request, lease_sync.MESSAGES[lease_sync.ARCHIVED])
                return redirect("ledger:leases:billing", pk=lease.pk)
            messages.success(request, BILLING_SAVED)
            if updated.sync_status == SyncStatus.PENDING and not busy(request, updated):
                submit(request, JobKind.LEASE_SYNC, updated)
            elif updated.sync_status == SyncStatus.AWAITING_LINK:
                messages.warning(request, lease_sync.AWAITING_LINK_MESSAGE)
            return redirect("ledger:leases:billing", pk=lease.pk)
        _hold(request, details_target, form)
        return redirect("ledger:leases:billing", pk=lease.pk)
    billing = lease_sync.billing_of(lease)
    held = _held_target(request)
    form = (
        _held(
            request,
            details_target,
            lambda data: LeaseBillingForm(data, instance=billing),
        )
        if held == details_target
        else None
    ) or LeaseBillingForm(instance=billing)
    link = (_held(request, link_target, LinkForm) if held == link_target else None) or LinkForm()
    context = _detail_context(request, lease)
    context["billing_dialog"] = _billing_dialog(lease, billing, form, link, archived=archived)
    return render(request, "ledger/leases/detail.html", context)


def _billing_dialog(
    lease: Lease,
    billing: LeaseBilling,
    form: LeaseBillingForm,
    link: LinkForm,
    *,
    archived: bool,
) -> dict[str, Any]:
    """The Edit billing details dialog's context (ledger/leases/_billing_dialog.html):
    masked identifiers only, fixed sentences only."""
    refs = lease_sync.current_refs(lease)
    customer = refs.get(lease_sync.KIND_CUSTOMER, "")
    method = refs.get(lease_sync.KIND_PAYMENT_METHOD, "")
    failed = lease.sync_status == SyncStatus.FAILED
    phone_ok = lease_sync.phone_is_valid(billing)
    retry_in = lease_sync.retry_wait_seconds(lease) if failed and phone_ok else 0
    jobs_now = job_context(lease)
    active = not archived
    return {
        "form": form,
        "link": link,
        "lease": lease,
        "shares": billing.shares_account_of,
        "customer_masked": mask_identifier(customer) if customer else "",
        "payment_method_masked": mask_identifier(method) if method else "",
        "failure_message": (
            lease_sync.MESSAGES.get(lease.sync_failure, lease_sync.DEFAULT_MESSAGE)
            if failed
            else ""
        ),
        "failure_fields": (
            ", ".join(lease.sync_failure_fields.split(","))
            if failed and lease.sync_failure_fields
            else ""
        ),
        "provider_updated_at": (
            lease_sync.provider_updated_at(lease)
            if lease.sync_status == SyncStatus.LINKED
            else None
        ),
        "can_retry": active
        and failed
        and phone_ok
        and not retry_in
        and not jobs_now["job_refresh"],
        "fix_phone_message": (
            lease_sync.MESSAGES[lease_sync.PHONE_INVALID]
            if active and failed and not phone_ok
            else ""
        ),
        "retry_wait_message": lease_sync.retry_wait_message(retry_in)
        if active and retry_in
        else "",
        "can_link": active and billing.shares_account_of_id is None,
        "can_save": active,
        # PLAN-014 I4: the sender names the Bank deposits match reads.
        "aliases": banking.aliases_of(lease),
        "alias_max_length": ALIAS_MAX_LENGTH,
        "return_url": reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
    }


# --- Sender names (PLAN-014 I4) ----------------------------------------------------------


@role_required
@require_POST
def lease_alias_add(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:alias_add`` (billing; PLAN-014 I4): remembers the posted ``text`` as a
    sender name of the lease (:func:`banking.services.add_alias`: normalised, the payment-data
    validator, once per lease). Refused for an archived operator. Returns to the dialog."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    if lease.operator is not None and lease.operator.archived_at is not None:
        messages.error(request, lease_sync.MESSAGES[lease_sync.ARCHIVED])
        return redirect("ledger:leases:billing", pk=lease.pk)
    outcome = banking.add_alias(lease, request.POST.get("text", ""), actor=_actor(request))
    (messages.success if outcome.ok else messages.error)(request, outcome.message)
    return redirect("ledger:leases:billing", pk=lease.pk)


@role_required
@require_POST
def lease_alias_remove(request: HttpRequest, pk: str, alias: str) -> HttpResponse:
    """``ledger:leases:alias_remove`` (billing; PLAN-014 I4): forgets one sender name of the
    lease (:func:`banking.services.remove_alias`); another lease's or an unknown one answers
    with one sentence and changes nothing. Refused for an archived operator."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    try:
        alias_id = uuid.UUID(alias)
    except ValueError:
        raise Http404 from None
    if lease.operator is not None and lease.operator.archived_at is not None:
        messages.error(request, lease_sync.MESSAGES[lease_sync.ARCHIVED])
        return redirect("ledger:leases:billing", pk=lease.pk)
    outcome = banking.remove_alias(lease, alias_id, actor=_actor(request))
    (messages.success if outcome.ok else messages.error)(request, outcome.message)
    return redirect("ledger:leases:billing", pk=lease.pk)


# --- Bank account entry (PLAN-014 C7) ---------------------------------------------------

BANK_NOT_SET_UP = provider_sentence(
    "Bank account entry is not set up on this environment yet. Staff can connect a bank "
    "method entered in the {provider} dashboard from Edit billing details."
)
BANK_NO_CUSTOMER = provider_sentence(
    "The lease is not connected to {provider} yet. Save its billing details with an e-mail "
    "first; the bank account is entered after that."
)
BANK_SHARES = "This lease uses another lease's account. Switch it to its own account first."
BANK_ONLY_THE_ID = "Only the bank method ID can be sent here. Nothing was saved."
BANK_CHECKING = provider_sentence("Checking the bank account with {provider}.")


def _bank_state(lease: Lease) -> tuple[HostedFieldsConfig | None, str]:
    """The hosted fields' configuration for the lease's own customer, or ``None`` with the
    sentence the bank-entry page shows instead (no provider call)."""
    hosted = get_hosted_fields()
    if not hosted.available():
        return None, str(BANK_NOT_SET_UP)
    if lease.operator is not None and lease.operator.archived_at is not None:
        return None, lease_sync.MESSAGES[lease_sync.ARCHIVED]
    if lease_sync.billing_of(lease).shares_account_of_id is not None:
        return None, BANK_SHARES
    customer = lease_sync.current_refs(lease).get(lease_sync.KIND_CUSTOMER, "")
    if not customer:
        return None, str(BANK_NO_CUSTOMER)
    config = hosted.config_for(customer)
    return config, "" if config is not None else str(BANK_NOT_SET_UP)


def bank_csp(config: HostedFieldsConfig) -> str:
    """The bank-entry page's Content-Security-Policy: scripts and frames from the page's own
    origin and the processor's host only (no inline script runs on that page)."""
    host = config.provider_host
    return f"script-src 'self' {host}; frame-src 'self' {host}"


@role_required
@require_safe
def lease_bank(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:bank`` (billing; PLAN-014 C7): the bank-entry dialog over the lease
    page. With hosted fields set up and a lease that holds its own customer, it shows the
    processor's bank-account fields (or the fake's one field, locally), which post only the
    new ``payment_method_id`` to ``ledger:leases:bank_attach``; otherwise one sentence says
    why not."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    config, message = _bank_state(lease)
    replaces = bool(lease_sync.current_refs(lease).get(lease_sync.KIND_PAYMENT_METHOD))
    context = _detail_context(request, lease)
    context["bank_entry"] = {
        "lease": lease,
        "config": config,
        "message": message,
        "csp": bank_csp(config) if config is not None and config.script_url else "",
        "replaces": replaces,
        "title": "Replace the bank account" if replaces else "Add a bank account",
        "return_url": reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
    }
    return render(request, "ledger/leases/bank_entry.html", context)


@role_required
@require_POST
def lease_bank_attach(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:bank_attach`` (billing; PLAN-014 C7): accepts exactly one field,
    ``payment_method_id``, and queues ``lease_method_attach``, which verifies the method
    against the lease's own customer before linking it. Any other field, a second value, an
    id that is not an identifier, a lease that shares another's account or an environment
    without hosted fields is refused and nothing is queued. Returns to the lease page, which
    shows the job until it finishes."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    names = set(request.POST) - {"csrfmiddlewaretoken"}
    values = request.POST.getlist("payment_method_id")
    if names != {"payment_method_id"} or len(values) != 1:
        messages.error(request, BANK_ONLY_THE_ID)
        return redirect("ledger:leases:bank", pk=lease.pk)
    value = values[0].strip()
    if not lease_sync.IDENTIFIER.match(value):
        messages.error(request, service.ATTACH_MESSAGES[service.ATTACH_INVALID_ID])
        return redirect("ledger:leases:bank", pk=lease.pk)
    config, message = _bank_state(lease)
    if config is None:
        messages.error(request, message)
        return redirect("ledger:leases:bank", pk=lease.pk)
    if not busy(request, lease):
        submit(request, JobKind.LEASE_METHOD_ATTACH, lease, {"payment_method_id": value})
        messages.info(request, BANK_CHECKING)
    return redirect("ledger:leases:detail", pk=lease.pk)


# --- Switch to own account (PLAN-014 C9) ------------------------------------------------

SWITCH_KINDS = (JobKind.LEASE_ACCOUNT_SWITCH,)


@role_required
@require_POST
def lease_switch_own(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:switch_own`` (billing; PLAN-014 C9): the LEASE card's "Switch to own
    account". Refused with one sentence and nothing queued when the lease does not share
    another lease's account, holds no customer of its own, its operator is archived, its
    autopay is paused (:func:`ledger.services.leases.switch_refusal`) or a switch of it is
    already waiting; otherwise it queues ``lease_account_switch`` with a fresh move key.
    Returns to the lease page, which shows the job until it finishes."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    reason = service.switch_refusal(lease)
    if not reason and jobs.is_active(jobs.latest_for(lease, SWITCH_KINDS)):
        reason = service.SWITCH_WAITING
    if reason:
        messages.error(request, service.SWITCH_MESSAGES[reason])
    elif submit(request, JobKind.LEASE_ACCOUNT_SWITCH, lease, {"move_key": uuid.uuid4()}):
        messages.info(request, service.SWITCH_QUEUED)
    return redirect("ledger:leases:detail", pk=lease.pk)


# --- Insurance "Bill now" (PLAN-014 I-b) ------------------------------------------------


@role_required
@require_POST
def lease_insurance_bill(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:insurance_bill`` (billing; PLAN-014 I-b): the LEASE card's "Bill now"
    on the insurance row. Creates the current policy year's insurance charge of the lease
    (:func:`ledger.services.insurance.bill_now`, audited with the staff member); refused with
    one sentence and nothing written for an archived operator, a store without an open date,
    a lease that does not owe the year or a year already billed. Returns to the store page."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    if lease.operator is not None and lease.operator.archived_at is not None:
        messages.error(request, lease_sync.MESSAGES[lease_sync.ARCHIVED])
    else:
        outcome = insurance.bill_now(lease, timezone.localdate(), actor=_actor(request))
        (messages.success if outcome.ok else messages.error)(request, outcome.message)
    return redirect("ledger:stores:detail", pk=lease.store_id)


# --- Pause / Resume collection and Activate now (PLAN-015b) ------------------------------

COLLECTION_PAUSED = "Collection paused."
COLLECTION_PAUSED_AUTOPAY = "Collection paused. Autopay is being paused."
COLLECTION_RESUMED = "Collection resumed."
COLLECTION_RESUMED_AUTOPAY = "Collection resumed. Autopay is being resumed."
LEASE_ACTIVATED = "Lease activated."


def _store_dialog(request: HttpRequest, lease: Lease, extra: dict[str, Any]) -> dict[str, Any]:
    """The store page's context with a dialog's own names (``extra``): the dialog opens over
    the lease's store page, as Stop plan does (PLAN-012f-b E-4)."""
    from ledger.views.stores import _detail_context as store_page  # stores imports this module

    return {**store_page(request, lease.store), **extra, "status_lease": lease}


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_pause_collection(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:pause_collection`` (billing; PLAN-015b §3 B): GET is the dialog over
    the store page (what will happen, the required reason); POST pauses collection
    (:func:`ledger.services.leases.pause_collection`) and, when the lease's autopay was on,
    queues its pause (``autopay_pause``, the worker's job; the view never calls the provider).
    A form with errors or a refusal is held for the dialog's next GET (a reason holding a card
    or account number is dropped, never shown again: :func:`ledger.views.operators._hold`);
    done, it returns to the store page."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    target = f"lease-pause-collection:{lease.pk}"
    if request.method == "POST":
        form = PauseCollectionForm(request.POST)
        if form.is_valid():
            try:
                outcome = service.pause_collection(
                    lease.pk, reason=form.cleaned_data["reason"], actor=_actor(request)
                )
            except LeaseRefused as error:
                form.add_error(error.field if error.field in form.fields else None, str(error))
            else:
                if outcome.autopay_paused:
                    submit(request, JobKind.AUTOPAY_PAUSE, lease)
                    messages.success(request, COLLECTION_PAUSED_AUTOPAY)
                else:
                    messages.success(request, COLLECTION_PAUSED)
                return redirect("ledger:stores:detail", pk=lease.store_id)
        _hold(request, target, form)
        return redirect("ledger:leases:pause_collection", pk=lease.pk)
    form = _held(request, target, PauseCollectionForm) or PauseCollectionForm()
    refusal = "" if lease.collection_status == CollectionStatus.ACTIVE else service.PAUSE_NOT_ACTIVE
    return render(
        request,
        "ledger/leases/pause_collection.html",
        _store_dialog(
            request,
            lease,
            {
                "pause_form": form,
                "pause_refusal": refusal,
                "pause_autopay": service.autopay_on(lease),
            },
        ),
    )


@role_required
@require_POST
def lease_resume_collection(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:resume_collection`` (billing; PLAN-015b §3 B): the LEASE card's "Resume
    collection". Resumes collection (:func:`ledger.services.leases.resume_collection`, which
    charges the months owed) and, when the pause paused autopay, queues its resume
    (``autopay_resume``); a refusal is one sentence. Returns to the store page."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    try:
        outcome = service.resume_collection(
            lease.pk, actor=_actor(request), today=timezone.localdate()
        )
    except LeaseRefused as error:
        messages.error(request, str(error))
    else:
        if outcome.autopay_resume:
            submit(request, JobKind.AUTOPAY_RESUME, lease)
            messages.success(request, COLLECTION_RESUMED_AUTOPAY)
        else:
            messages.success(request, COLLECTION_RESUMED)
    return redirect("ledger:stores:detail", pk=lease.store_id)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_activate(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:activate`` (billing; PLAN-015b §3 A): GET is the "Activate now" dialog
    over the store page (the start date becomes today when it is later; the first month is
    billed); POST activates the lease (:func:`ledger.services.leases.activate`). A refusal
    re-renders the dialog with its sentence; done, it returns to the store page."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    today = timezone.localdate()
    refusal = ""
    if lease.collection_status != CollectionStatus.PROSPECTIVE or lease.ended_on is not None:
        refusal = service.NOT_PROSPECTIVE
    elif blockers := service.activation_blockers(lease):
        refusal = blockers[0]
    if request.method == "POST" and not refusal:
        try:
            service.activate(lease.pk, actor=_actor(request), today=today)
        except LeaseRefused as error:
            refusal = str(error)
        else:
            messages.success(request, LEASE_ACTIVATED)
            return redirect("ledger:stores:detail", pk=lease.store_id)
    return render(
        request,
        "ledger/leases/activate.html",
        _store_dialog(
            request,
            lease,
            {
                "activate_refusal": refusal,
                "activate_today": today,
                "activate_moves_start": lease.started_on is None or lease.started_on > today,
            },
        ),
    )
