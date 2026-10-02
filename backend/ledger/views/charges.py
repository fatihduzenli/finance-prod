"""Rent charge screens: detail, manual payment, the marking retry and linking an invoice
(PLAN-007 §4.1, D8; PLAN-009 D1 to D3, D5; PLAN-008a §2 rule 4).

The detail page is read by any role and shows the carried balance of earlier charges.
Recording a payment (GET of the form and POST), retrying the marking of the provider
invoice and linking an invoice (GET of the list and POST) need ``billing``; the
registry checks the POST first. The service re-checks
every rule under a row lock, makes the provider calls and writes the audit rows. Every
POST ends in a redirect; a form with errors is held in the session for the next GET.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import LinkInvoiceForm, ManualPaymentForm
from ledger.models import ChargeNote, RentCharge, RentChargeStatus
from ledger.services import rent_run
from ledger.services.rent_run import MarkAction, MarkResult
from ledger.views.operators import _actor, _held, _hold, _role, role_required
from ledger.views.run import _billing_only
from payments import operator_sync


def _load(pk: str) -> RentCharge:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(RentCharge.objects.select_related("lease", "lease__store"), pk=key)


def _report(request: HttpRequest, result: MarkResult | None) -> None:
    if result is None or not result.message:
        return
    if result.action is MarkAction.MARKED:
        messages.success(request, result.message)
    elif result.action is MarkAction.FAILED:
        messages.error(request, result.message)
    else:
        messages.warning(request, result.message)


@role_required
@require_safe
def charge_detail(request: HttpRequest, pk: str) -> HttpResponse:
    charge = _load(pk)
    billing = meets(_role(request), Role.BILLING)
    retry_wait = rent_run.provider_retry_wait(charge) if billing else None
    return render(
        request,
        "ledger/charges/detail.html",
        {
            "charge": charge,
            "lease": charge.lease,
            "invoices": charge.invoices.order_by("issue_date", "invoice_number"),
            "payments": charge.payments.order_by("-settled_on", "-created_at"),
            "notes": ChargeNote.objects.filter(rent_charge=charge).order_by("-created_at"),
            "review_message": rent_run.charge_review_message(charge),
            "carried_balance": rent_run.carried_balance(charge),
            "can_record": billing
            and charge.status != RentChargeStatus.WAIVED
            and charge.balance > 0,
            "can_retry_mark": retry_wait == 0,
            "can_link_invoice": billing and not charge.invoices.exists(),
            "retry_wait_message": (
                operator_sync.retry_wait_message(retry_wait) if retry_wait else ""
            ),
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_record_payment(request: HttpRequest, pk: str) -> HttpResponse:
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    charge = _load(pk)
    target = f"charge-payment:{charge.pk}"
    if request.method == "POST":
        form = ManualPaymentForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = rent_run.record_manual_payment(
                charge.pk,
                method=data["method"],
                amount=data["amount"],
                paid_on=data["paid_on"],
                note=data["note"],
                actor=_actor(request),
                creation_key=data["creation_key"],
                reference=data["reference"],
            )
            if outcome.ok:
                if outcome.replayed:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                _report(request, outcome.provider)
                return redirect("ledger:charges:detail", pk=charge.pk)
            form.add_error(outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:charges:record_payment", pk=charge.pk)
    form = _held(request, target, ManualPaymentForm) or ManualPaymentForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(request, "ledger/charges/payment_form.html", {"form": form, "charge": charge})


@role_required
@require_POST
def charge_retry_mark(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    charge = _load(pk)
    _report(request, rent_run.retry_provider_mark(charge.pk, actor=_actor(request)))
    return redirect("ledger:charges:detail", pk=charge.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_link_invoice(request: HttpRequest, pk: str) -> HttpResponse:
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    charge = _load(pk)
    target = f"charge-link-invoice:{charge.pk}"
    if request.method == "POST":
        form = LinkInvoiceForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = rent_run.link_invoice(
                charge.pk,
                data["invoice"],
                confirm=data["confirm"],
                note=data["note"],
                actor=_actor(request),
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:charges:detail", pk=charge.pk)
            form.add_error(outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:charges:link_invoice", pk=charge.pk)
    form = _held(request, target, LinkInvoiceForm) or LinkInvoiceForm()
    return render(
        request,
        "ledger/charges/link_invoice.html",
        {
            "form": form,
            "charge": charge,
            "has_invoice": charge.invoices.exists(),
            "rows": rent_run.unlinked_invoices(charge),
        },
    )
