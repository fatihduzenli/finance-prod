"""Rent charge screens: detail, manual payment, the marking retry and linking an invoice
(PLAN-007 §4.1, D8; PLAN-009 D1 to D3, D5; PLAN-008a §2 rule 4).

The detail page is read by any role and shows the carried balance of earlier charges.
Recording a payment (GET of the form and POST), retrying the marking of the provider
invoice and linking an invoice (GET of the list and POST) need ``billing``; the
registry checks the POST first. The service re-checks
every rule under a row lock and writes the audit rows. No view calls the provider: a
recorded payment whose invoice is to be marked, and the retry, are queued as jobs
(:mod:`ledger.views.jobs`, PLAN-010 D3); the detail page shows the latest one. Every
POST ends in a redirect; a form with errors is held in the session for the next GET.

``charges:export`` (PLAN-011 D5) is rent charges as CSV, of one lease or operator, any role.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import LinkInvoiceForm, ManualPaymentForm
from ledger.models import ChargeEntityType, ChargeNote, RentCharge, RentChargeStatus
from ledger.services import collections, csv_export, rent_run
from ledger.services.csv_export import CsvWriter
from ledger.services.rent_run import MarkAction, MarkResult
from ledger.views.jobs import already_submitted, busy, job_context, submit
from ledger.views.operators import (
    _actor,
    _held,
    _hold,
    _role,
    csv_response,
    export_name,
    role_required,
    uuid_param,
)
from ledger.views.run import _billing_only
from payments import operator_sync
from payments.models import JobKind


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


EXPORT_HEADER = (
    "Store",
    "Store number",
    "Operator",
    "Rent month",
    "Debit of the month",
    "Due date",
    "Amount due (USD)",
    "Paid (USD)",
    "Balance (USD)",
    "Status",
    "Needs review",
)


@role_required
@require_safe
def charge_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:charges:export`` (PLAN-011 D5): rent charges, of one lease (``lease``) or
    one operator (``operator``) when given, newest month first as the lease page lists
    them; an unreadable filter is ignored."""
    charges = RentCharge.objects.select_related("lease__store", "lease__operator")
    lease = uuid_param(request, "lease")
    if lease is not None:
        charges = charges.filter(lease_id=lease)
    operator = uuid_param(request, "operator")
    if operator is not None:
        charges = charges.filter(lease__operator_id=operator)
    records = (
        [
            charge.lease.store.external_label,
            charge.lease.store.store_number,
            charge.lease.operator.display_name if charge.lease.operator is not None else "",
            f"{charge.period_month:%Y-%m}",
            charge.seq,
            charge.due_on,
            charge.amount_due,
            charge.amount_paid,
            charge.balance,
            charge.get_status_display(),
            charge.needs_review,
        ]
        for charge in charges.order_by(
            "-period_month", "lease__store__external_label", "seq", "id"
        )[: csv_export.CSV_MAX_ROWS + 1].iterator(chunk_size=2_000)
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("rent-charges"))


@role_required
@require_safe
def charge_detail(request: HttpRequest, pk: str) -> HttpResponse:
    charge = _load(pk)
    billing = meets(_role(request), Role.BILLING)
    retry_wait = rent_run.provider_retry_wait(charge) if billing else None
    jobs = job_context(charge)
    return render(
        request,
        "ledger/charges/detail.html",
        {
            **jobs,
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
            "can_retry_mark": retry_wait == 0 and not jobs["job_refresh"],
            "can_link_invoice": billing and not charge.invoices.exists(),
            "retry_wait_message": (
                operator_sync.retry_wait_message(retry_wait) if retry_wait else ""
            ),
            # PLAN-011b D4: the charge's collection e-mails and the hand send.
            **collections.charge_context(
                ChargeEntityType.RENT_CHARGE,
                charge.pk,
                billing=billing,
                url=reverse("ledger:charges:send_step", kwargs={"pk": charge.pk}),
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
            if already_submitted(data["creation_key"]):
                messages.info(request, rent_run.PAY_ALREADY)
                return redirect("ledger:charges:detail", pk=charge.pk)
            outcome = rent_run.record_manual_payment(
                charge.pk,
                method=data["method"],
                amount=data["amount"],
                paid_on=data["paid_on"],
                note=data["note"],
                actor=_actor(request),
                creation_key=data["creation_key"],
                reference=data["reference"],
                defer_mark=True,
            )
            if outcome.ok and outcome.mark_due and outcome.payment is not None:
                submit(
                    request,
                    JobKind.PAYMENT_MARK,
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
    if not busy(request, charge):
        submit(request, JobKind.PAYMENT_RETRY_MARK, charge)
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
