"""Rent charge screens: detail and manual payment (PLAN-007 §4.1, D8).

The detail page is read by any role. Recording a payment (GET of the form and POST)
needs ``billing``; the registry checks the POST first. The service re-checks every rule
under a row lock and writes the audit row; no provider is called. Every POST ends in a
redirect; a form with errors is held in the session for the next GET.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import ManualPaymentForm
from ledger.models import ChargeNote, RentCharge, RentChargeStatus
from ledger.services import rent_run
from ledger.views.operators import _actor, _held, _hold, _role, role_required


def _load(pk: str) -> RentCharge:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(RentCharge.objects.select_related("lease", "lease__store"), pk=key)


@role_required
@require_safe
def charge_detail(request: HttpRequest, pk: str) -> HttpResponse:
    charge = _load(pk)
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
            "can_record": meets(_role(request), Role.BILLING)
            and charge.status != RentChargeStatus.WAIVED
            and charge.balance > 0,
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
            )
            if outcome.ok:
                if outcome.replayed:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                return redirect("ledger:charges:detail", pk=charge.pk)
            form.add_error(outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:charges:record_payment", pk=charge.pk)
    form = _held(request, target, ManualPaymentForm) or ManualPaymentForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(request, "ledger/charges/payment_form.html", {"form": form, "charge": charge})
