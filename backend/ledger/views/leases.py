"""Lease screens: list, detail, create, edit, enrol, pause, resume (PLAN-007 §4.1).

Access: every view requires a signed-in, second-factor-verified user with a role
(middleware plus :func:`~ledger.views.operators.role_required`). Unsafe methods are
checked again by ``accounts.registry``: every lease POST needs ``admin`` (D10). The
enrolment page shows the operator's payment methods, so its GET is limited to ``admin``
here too.

No view calls the provider: every change goes through :mod:`ledger.services.leases`,
which writes the audit log. Enable, pause and resume are queued as jobs
(:mod:`ledger.views.jobs`, PLAN-010 D3) that run the service in the worker; the detail
page shows the latest one. The enrolment page lists the methods from the latest
snapshot ("Methods as of ..."); its "Refresh methods" button (a POST to the same URL
with ``refresh_methods``) queues the read, and the page shows that job's state.
Templates receive masked identifiers only, except the payment-method IDs that are the
values of the enrolment choices. Every POST ends in a redirect, whatever the outcome; a
form with errors is held in the session for the next GET, as on the operator screens.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import EnrolForm, LeaseCreateForm, LeaseForm
from ledger.models import (
    ChargeNote,
    Lease,
    LeaseRentHistory,
    Note,
    NoteEntityType,
    Operator,
    ScheduleState,
)
from ledger.services import leases as service
from ledger.services.leases import LeaseRefused
from ledger.views.jobs import busy, job_context, submit
from ledger.views.operators import PAGE_SIZE, _actor, _held, _hold, _role, role_required
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


@role_required
@require_safe
def lease_list(request: HttpRequest) -> HttpResponse:
    """Every lease, optionally of one operator (PLAN-009 D9); a lease whose charges carry
    a charge note is marked."""
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


@role_required
@require_safe
def lease_detail(request: HttpRequest, pk: str) -> HttpResponse:
    lease = _load(pk)
    admin = meets(_role(request), Role.ADMIN)
    now = timezone.now()
    today = timezone.localdate()
    schedules = []
    linked_states = set()
    for row in service.live_schedules(lease):
        ref = service.schedule_ref(row)
        if ref:
            linked_states.add(row.state)
        next_run = (
            service.next_debit_at(row.debit_day, now)
            if row.state == ScheduleState.ACTIVE and row.debit_day is not None
            else None
        )
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
            "blockers": service.autopay_blockers(lease),
            "notes_info": service.enrolment_notes(lease),
            "charges": lease.rent_charges.order_by("-period_month", "seq"),
            "notes": Note.objects.filter(
                entity_type=NoteEntityType.LEASE, entity_id=lease.pk
            ).order_by("-created_at"),
            "can_edit": admin,
            "can_enrol": admin,
            "can_pause": admin and ScheduleState.ACTIVE in linked_states,
            "can_resume": admin and ScheduleState.PAUSED in linked_states,
        },
    )


# --- Write --------------------------------------------------------------------------


def _admin_only(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    return None


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_create(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    if request.method == "POST":
        existing = service.lease_for_creation_key(request.POST.get("creation_key"))
        if existing is not None:
            messages.info(request, ALREADY_SAVED)
            return redirect("ledger:leases:detail", pk=existing.pk)
        form = LeaseCreateForm(request.POST)
        if form.is_valid():
            lease, created = service.create_lease(
                form.cleaned_data,
                rent=form.cleaned_data["rent"],
                creation_key=form.cleaned_data["creation_key"],
                actor=_actor(request),
            )
            if created:
                messages.success(request, "Lease saved.")
            else:
                messages.info(request, ALREADY_SAVED)
            return redirect("ledger:leases:detail", pk=lease.pk)
        _hold(request, "lease-create", form)
        return redirect("ledger:leases:create")
    form = _held(request, "lease-create", LeaseCreateForm) or LeaseCreateForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(request, "ledger/leases/form.html", {"form": form, "lease": None})


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_edit(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    lease = _load(pk)
    target = f"lease-edit:{lease.pk}"
    if request.method == "POST":
        form = LeaseForm(request.POST, instance=lease)
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
                return redirect("ledger:leases:detail", pk=lease.pk)
        _hold(request, target, form)
        return redirect("ledger:leases:edit", pk=lease.pk)
    form = _held(request, target, lambda data: LeaseForm(data, instance=lease)) or LeaseForm(
        instance=lease, initial=_initial(lease)
    )
    return render(request, "ledger/leases/form.html", {"form": form, "lease": lease})


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
