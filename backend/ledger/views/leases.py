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
from typing import Any
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import EnrolForm, LeaseCreateForm, LeaseForm
from ledger.models import (
    ChargeNote,
    DocumentKind,
    Lease,
    LeaseRentHistory,
    Note,
    NoteEntityType,
    Operator,
    ScheduleState,
    Store,
)
from ledger.services import csv_export, one_off_charges
from ledger.services import leases as service
from ledger.services.csv_export import CsvWriter
from ledger.services.leases import LeaseRefused
from ledger.services.one_off_charges import SetupRefused
from ledger.views import documents, store_context
from ledger.views.jobs import busy, job_context, submit
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
from payments import jobs
from payments.models import JobKind
from payments.provider import mask_identifier

ALREADY_SAVED = "This lease was already saved. Nothing was changed."
REFRESH_QUEUED = "Reading the operator's payment methods."
#: The job kinds the enrolment page shows.
REFRESH_KINDS = (JobKind.OPERATOR_METHODS_REFRESH,)


def _load(pk: str) -> Lease:
    """The lease named in the URL, loaded from the database; 404 otherwise."""
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Lease.objects.select_related("store", "operator"), pk=key)


def _initial(lease: Lease) -> dict[str, Any]:
    """The latest rent row, which an edit compares against (it may start later)."""
    rent = service.open_rent(lease)
    return {"rent": rent.amount if rent is not None else None}


# --- Read ---------------------------------------------------------------------------


def _operator_filter(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw or "")
    except ValueError:
        return None


def _listed(request: HttpRequest) -> tuple[QuerySet[Lease], uuid.UUID | None]:
    """The leases the list shows, with the rent in effect today and the note flag."""
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
    return leases, operator


@role_required
@require_safe
def lease_list(request: HttpRequest) -> HttpResponse:
    """Every lease, optionally of one operator (PLAN-009 D9); a lease whose charges carry
    a charge note is marked."""
    leases, operator = _listed(request)
    page = Paginator(leases, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/leases/list.html",
        {
            "page": page,
            "can_create": meets(_role(request), Role.ADMIN),
            "operators": Operator.objects.order_by(Lower("display_name"), "id"),
            "operator": operator,
            "query": urlencode({"operator": str(operator)}) if operator else "",
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
    leases, _ = _listed(request)

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
    admin = meets(_role(request), Role.ADMIN)
    now = timezone.now()
    today = timezone.localdate()
    schedules = []
    linked_states = set()
    # PLAN-012b-b D13: autopay moved past a split month starts on its new first debit.
    moved = service.autopay_move(lease, now)
    for row in service.live_schedules(lease):
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
    return render(
        request,
        "ledger/leases/detail.html",
        {
            **job_context(lease),
            "lease": lease,
            "rent": service.current_rent(lease, today),
            "upcoming": service.upcoming_rent(lease, today),
            "history": lease.rent_history.order_by("-effective_from"),
            "schedules": schedules,
            "autopay_moved": moved.text if moved is not None else "",
            "blockers": service.autopay_blockers(lease),
            "notes_info": service.enrolment_notes(lease),
            "notes": Note.objects.filter(
                entity_type=NoteEntityType.LEASE, entity_id=lease.pk
            ).order_by("-created_at"),
            "can_edit": admin,
            "can_end": admin and lease.ended_on is None,
            "can_enrol": admin,
            "can_pause": admin and ScheduleState.ACTIVE in linked_states,
            "can_resume": admin and ScheduleState.PAUSED in linked_states,
            "can_stop": admin and bool(service.stoppable_schedules(lease)),
            **documents.section_context(request, lease),
        },
    )


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


def _create_form(data: Any, context: store_context.StoreContext | None) -> LeaseCreateForm:
    initial = {} if context is None else {"store": context.store.pk}
    form = LeaseCreateForm(data, initial=initial)
    if context is not None:
        _lock_known(form, ("store",))
    return form


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
            try:
                lease, created, charges = one_off_charges.create_lease_with_checklist(
                    form.cleaned_data,
                    rent=form.cleaned_data["rent"],
                    creation_key=form.cleaned_data["creation_key"],
                    items=form.setup_items(),
                    actor=_actor(request),
                )
            except SetupRefused as refusal:
                form.add_error(None, _checklist_refusal(refusal))
            else:
                if created:
                    messages.success(request, _saved_message(len(charges)))
                    documents.upload_after_create(request, lease, "lease")
                else:
                    messages.info(request, ALREADY_SAVED)
                return redirect(_saved_url(request, lease, context))
        _hold(request, "lease-create", form)
        return redirect(request.get_full_path())
    held = _held(request, "lease-create", lambda data: _create_form(data, context))
    if held is None:
        held = _create_form(None, context)
        held.initial["creation_key"] = uuid.uuid4()
    return render(
        request,
        "ledger/leases/form.html",
        {
            "form": held,
            "lease": None,
            "document_form": documents.create_form(DocumentKind.LEASE_AGREEMENT),
            "store_context": context,
        },
    )


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


def _edit_form(lease: Lease, data: Any, context: store_context.StoreContext | None) -> LeaseForm:
    form = LeaseForm(data, instance=lease, initial=None if data is not None else _initial(lease))
    if context is not None:
        _lock_known(form, ("store", "operator"))
    return form


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit a lease; opened from the store page (``?store=&lease=``, PLAN-012d D2) the
    store and operator are hidden and the save returns to the store page (D8)."""
    refused = _admin_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    context = store_context.for_lease(request, lease)
    target = f"lease-edit:{lease.pk}"
    if request.method == "POST":
        form = _edit_form(lease, request.POST, context)
        if form.is_valid():
            try:
                service.update_lease(
                    lease.pk,
                    form.cleaned_data,
                    rent=form.cleaned_data["rent"],
                    rent_effective_from=form.cleaned_data.get("rent_effective_from"),
                    actor=_actor(request),
                )
            except LeaseRefused as refusal:
                form.add_error(refusal.field, str(refusal))
            else:
                messages.success(request, "Lease saved.")
                return redirect(_saved_url(request, lease, context))
        _hold(request, target, form)
        return redirect(request.get_full_path())
    form = _held(request, target, lambda data: _edit_form(lease, data, context)) or _edit_form(
        lease, None, context
    )
    return render(
        request,
        "ledger/leases/form.html",
        {"form": form, "lease": lease, "store_context": context},
    )


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
                service.RENT_CHANGES.format(date=debit.changes_on.isoformat())
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
            **(job_context(lease.operator, REFRESH_KINDS) if lease.operator else {}),
            "lease": lease,
            "form": form,
            "blockers": blockers,
            "notes_info": service.enrolment_notes(lease),
            "debits": debits,
            "any_large": any(item["large"] for item in debits),
            "threshold": service.format_amount(settings.LARGE_DEBIT_WARNING_AMOUNT),
            "limit_refusal": service.provider_limit_refusal(amounts),
            "confirmation": service.large_debit_confirmation(amounts),
            "choices": choices,
            "linked_method": service.linked_payment_method(lease.operator),
            "no_bank_message": service.NO_BANK_METHOD,
            "methods_not_read": service.METHODS_NOT_READ,
        },
    )


def _refresh_methods(request: HttpRequest, lease: Lease) -> None:
    """Queue a read of the operator's payment methods; refused, with a message, when
    the operator has no customer reference or a read is already waiting."""
    operator = lease.operator
    if operator is None or not service.customer_ref(operator):
        messages.error(request, service.NO_PROVIDER_CUSTOMER)
        return
    if busy(request, operator, REFRESH_KINDS):
        return
    jobs.refresh_methods(operator, request.user)
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
            **job_context(lease),
            "lease": lease,
            "schedules": [
                {"row": row, "schedule_masked": mask_identifier(ref)} for row, ref in rows
            ],
            "nothing_to_stop": service.NOTHING_TO_STOP,
        },
    )
