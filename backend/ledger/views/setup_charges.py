"""Setup charge screens: detail, manual payment, the marking retry, link an invoice,
edit, waive, and adding one charge from the lease page (docs/qa/PLAN-010b D5, D6, D12).

The detail page is read by any role. Recording a payment, retrying the marking of the
provider invoice, linking an invoice, editing and adding a charge (GET of the form and
POST) need ``billing``; waiving needs ``admin``. The registry checks the POST first;
these views refuse the GET of the forms below the same role.
:mod:`ledger.services.setup_charges` re-checks every rule under a row lock and writes
the audit rows. No view calls the provider: a recorded payment whose invoice is to be
marked is queued as a ``setup_payment_mark`` job keyed by the form's one-time key, the
retry as a ``setup_payment_retry_mark`` job (once a minute, as on rent charges), and the
detail page shows the latest one. Every POST ends in a redirect; a form with errors is
held in the session for the next GET.
"""

from __future__ import annotations

import uuid

from django import forms
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import (
    LinkInvoiceForm,
    ManualPaymentForm,
    SetupChargeAddForm,
    SetupChargeEditForm,
    WaiveForm,
)
from ledger.models import DepositResolution, Lease, SetupCharge, SetupChargeStatus
from ledger.services import rent_run
from ledger.services import setup_charges as service
from ledger.views.charges import _report
from ledger.views.jobs import already_submitted, busy, job_context, submit
from ledger.views.operators import _actor, _held, _hold, _role, role_required
from payments import operator_sync
from payments.models import JobKind


def _load(pk: str) -> SetupCharge:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        SetupCharge.objects.select_related("lease", "lease__store", "lease__operator", "fee_type"),
        pk=key,
    )


def _error(form: forms.BaseForm, field: str | None, message: str) -> None:
    """A refusal on its field, or on the whole form when the form has no such field."""
    form.add_error(field if field in form.fields else None, message)


def _refuse_below(request: HttpRequest, role: Role) -> HttpResponse | None:
    if not meets(_role(request), role):
        return forbidden(request)
    return None


@role_required
@require_safe
def setup_charge_detail(request: HttpRequest, pk: str) -> HttpResponse:
    charge = _load(pk)
    billing = meets(_role(request), Role.BILLING)
    admin = meets(_role(request), Role.ADMIN)
    open_charge = charge.status != SetupChargeStatus.WAIVED
    retry_wait = service.retry_wait(charge) if billing else None
    jobs = job_context(charge)
    return render(
        request,
        "ledger/setup_charges/detail.html",
        {
            **jobs,
            "charge": charge,
            "lease": charge.lease,
            "invoices": charge.invoices.order_by("issue_date", "invoice_number"),
            "payments": charge.payments.order_by("-settled_on", "-created_at"),
            "review_message": service.review_message(charge),
            "can_record": billing and open_charge and charge.balance > 0,
            "can_link_invoice": billing and not charge.invoices.exists(),
            "can_edit": billing and open_charge,
            "can_waive": admin and open_charge and not service.has_settled_payment(charge),
            "can_retry_mark": retry_wait == 0 and not jobs["job_refresh"],
            "retry_wait_message": (
                operator_sync.retry_wait_message(retry_wait) if retry_wait else ""
            ),
            "resolution": DepositResolution.objects.filter(setup_charge=charge)
            .select_related("to_lease__store", "created_by")
            .first(),
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def setup_charge_payment(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"setup-charge-payment:{charge.pk}"
    if request.method == "POST":
        form = ManualPaymentForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if already_submitted(data["creation_key"]):
                messages.info(request, rent_run.PAY_ALREADY)
                return redirect("ledger:setup_charges:detail", pk=charge.pk)
            outcome = service.record_payment(
                charge.pk,
                method=data["method"],
                amount=data["amount"],
                paid_on=data["paid_on"],
                note=data["note"],
                actor=_actor(request),
                creation_key=data["creation_key"],
                reference=data["reference"],
            )
            if outcome.ok and outcome.mark_due and outcome.payment is not None:
                submit(
                    request,
                    JobKind.SETUP_PAYMENT_MARK,
                    charge,
                    {"payment": outcome.payment.pk},
                    replay_key=data["creation_key"],
                )
            if outcome.ok:
                if outcome.replayed:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                _report(request, outcome.provider)
                return redirect("ledger:setup_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:setup_charges:payment", pk=charge.pk)
    form = _held(request, target, ManualPaymentForm) or ManualPaymentForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(
        request, "ledger/setup_charges/payment_form.html", {"form": form, "charge": charge}
    )


@role_required
@require_POST
def setup_charge_retry_mark(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    if not busy(request, charge):
        submit(request, JobKind.SETUP_PAYMENT_RETRY_MARK, charge)
    return redirect("ledger:setup_charges:detail", pk=charge.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def setup_charge_link(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"setup-charge-link:{charge.pk}"
    if request.method == "POST":
        form = LinkInvoiceForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = service.link_invoice(
                charge.pk,
                data["invoice"],
                confirm=data["confirm"],
                note=data["note"],
                actor=_actor(request),
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:setup_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:setup_charges:link", pk=charge.pk)
    form = _held(request, target, LinkInvoiceForm) or LinkInvoiceForm()
    return render(
        request,
        "ledger/setup_charges/link_invoice.html",
        {
            "form": form,
            "charge": charge,
            "has_invoice": charge.invoices.exists(),
            "rows": rent_run.unlinked_invoices(charge),
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def setup_charge_edit(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"setup-charge-edit:{charge.pk}"
    if request.method == "POST":
        form = SetupChargeEditForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = service.edit_charge(
                charge.pk,
                cost=data.get("cost"),
                price=data["price"],
                vendor=data.get("vendor") or "",
                due_date=data["due_date"],
                actor=_actor(request),
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:setup_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:setup_charges:edit", pk=charge.pk)
    form = _held(request, target, SetupChargeEditForm) or SetupChargeEditForm(
        initial={
            "cost": charge.cost,
            "price": charge.price,
            "vendor": charge.vendor,
            "due_date": charge.due_date,
        }
    )
    return render(
        request,
        "ledger/setup_charges/form.html",
        {
            "form": form,
            "charge": charge,
            "lease": charge.lease,
            "price_locked": service.has_payment(charge),
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def setup_charge_waive(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.ADMIN)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"setup-charge-waive:{charge.pk}"
    if request.method == "POST":
        form = WaiveForm(request.POST)
        if form.is_valid():
            note = form.cleaned_data["note"]
            outcome = service.waive(charge.pk, note=note, actor=_actor(request))
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:setup_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:setup_charges:waive", pk=charge.pk)
    form = _held(request, target, WaiveForm) or WaiveForm()
    return render(request, "ledger/setup_charges/waive.html", {"form": form, "charge": charge})


def _load_lease(pk: str) -> Lease:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Lease.objects.select_related("store"), pk=key)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def lease_add_setup_charge(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:add_setup_charge``: one setup charge on an existing lease (D5)."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    lease = _load_lease(pk)
    target = f"lease-add-setup-charge:{lease.pk}"
    if request.method == "POST":
        form = SetupChargeAddForm(request.POST)
        if form.is_valid():
            outcome = service.add_charge(lease.pk, form.item(), actor=_actor(request))
            if outcome.ok and outcome.charge is not None:
                messages.success(request, outcome.message)
                return redirect("ledger:setup_charges:detail", pk=outcome.charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:leases:add_setup_charge", pk=lease.pk)
    form = _held(request, target, SetupChargeAddForm) or SetupChargeAddForm(
        initial={"due_date": lease.started_on or timezone.localdate()}
    )
    return render(
        request, "ledger/setup_charges/form.html", {"form": form, "charge": None, "lease": lease}
    )
