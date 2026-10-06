"""The payments ledger, its CSV export, the payment page, refunds and voids
(docs/qa/PLAN-009-payments-ledger.md D4, D6 to D9).

The ledger, the export and the payment page are read only and open to every role
(``accounts.registry`` lists them as read-only routes, so any unsafe method is
refused). Filters are read from the query string by
:func:`ledger.services.payments_ledger.parse_filter`; the export applies the same
filter and streams its rows. Provider identifiers appear masked only.

Refund and void need ``admin``: the registry refuses a POST below admin, and these
views refuse the GET of the confirmation page as well, so a billing user or a viewer
never sees it. :mod:`ledger.services.refunds` enforces every rule and writes the audit
rows. No view calls the provider: a confirmed refund or void is queued as a job
(:mod:`ledger.views.jobs`, PLAN-010 D3) keyed by the form's one-time key, and the
payment page shows the latest one. What can be refused from local data (no
confirmation, nothing refundable, an amount above the refundable one) is refused here
before anything is queued; the service checks it again under the row lock. Every POST
ends in a redirect; a form with errors is held in the session for the next GET.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models.functions import Lower
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import RefundForm, VoidForm
from ledger.models import AuditLog, Operator, Payment, PaymentMethod, Store
from ledger.services import one_off_charges, refunds
from ledger.services import payments_ledger as ledger
from ledger.services.rent_run import charge_review_message
from ledger.views.jobs import already_submitted, job_context, submit
from ledger.views.operators import (
    PAGE_SIZE,
    _held,
    _hold,
    _role,
    csv_response,
    role_required,
)
from payments.models import JobKind
from payments.provider import mask_identifier
from payments.webhooks.handlers.common import ENTITY_PAYMENT


@role_required
@require_safe
def payment_list(request: HttpRequest) -> HttpResponse:
    selected = ledger.parse_filter(request.GET)
    rows = ledger.ledger_payments(selected)
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    query = urlencode(selected.as_query())
    return render(
        request,
        "ledger/payments/list.html",
        {
            "page": page,
            "rows": ledger.rows_for(page),
            "totals": ledger.totals(rows),
            "selected": selected,
            "query": query,
            "date_by_choices": ledger.DATE_BY_CHOICES,
            "method_choices": PaymentMethod.choices,
            "status_choices": ledger.STATUS_CHOICES,
            "operators": Operator.objects.order_by(Lower("display_name"), "id"),
            "stores": Store.objects.order_by("external_label", "id"),
        },
    )


@role_required
@require_safe
def payment_export(request: HttpRequest) -> StreamingHttpResponse:
    selected = ledger.parse_filter(request.GET)
    return csv_response(ledger.csv_lines(selected), f"payments-{timezone.localdate():%Y-%m-%d}.csv")


# --- The payment page, refunds and voids (D6 to D8) ----------------------------------------


def _load(pk: str) -> Payment:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        Payment.objects.select_related(
            "invoice",
            "invoice__operator",
            "rent_charge",
            "rent_charge__lease",
            "rent_charge__lease__store",
            "insurance_charge",
            "one_off_charge",
            "one_off_charge__fee_type",
            "one_off_charge__operator",
            "parent_payment",
            "recorded_by",
        ),
        pk=key,
    )


def _summary(payment: Payment, facts: refunds.PaymentFacts) -> dict[str, object]:
    """What every payment page shows: the row, its operator and store, and the facts."""
    [row] = ledger.rows_for([payment])
    return {
        "payment": payment,
        "row": row,
        "facts": facts,
        "invoice": payment.invoice,
        "charge": payment.rent_charge,
        "one_off_charge": payment.one_off_charge,
    }


@role_required
@require_safe
def payment_detail(request: HttpRequest, pk: str) -> HttpResponse:
    payment = _load(pk)
    admin = meets(_role(request), Role.ADMIN)
    facts = refunds.payment_facts(payment)
    return render(
        request,
        "ledger/payments/detail.html",
        {
            **_summary(payment, facts),
            **job_context(payment),
            "refunds": payment.refunds.order_by("created_at", "id"),
            "charge_review": _charge_review(payment),
            "audit_rows": AuditLog.objects.filter(
                entity_type=ENTITY_PAYMENT, entity_id=str(payment.pk)
            )
            .select_related("actor")
            .order_by("-at", "-id"),
            "can_refund": admin and facts.can_refund,
            "can_void": admin and facts.can_void,
            "parent_transaction": (
                mask_identifier(refunds.transaction_reference(payment.parent_payment))
                if payment.parent_payment is not None
                else ""
            ),
        },
    )


def _charge_review(payment: Payment) -> str:
    if payment.rent_charge is not None:
        return charge_review_message(payment.rent_charge)
    if payment.one_off_charge is not None:
        return one_off_charges.review_message(payment.one_off_charge)
    return ""


def _admin_only(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    return None


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def payment_refund(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    payment = _load(pk)
    target = f"payment-refund:{payment.pk}"
    if request.method == "POST":
        form = RefundForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if already_submitted(data["creation_key"]):
                messages.info(request, refunds.ALREADY_SUBMITTED)
                return redirect("ledger:payments:detail", pk=payment.pk)
            facts = refunds.payment_facts(payment)
            if facts.refund_refusal:
                messages.error(request, facts.refund_refusal)
                return redirect("ledger:payments:refund", pk=payment.pk)
            if data["confirm"] is not True:
                form.add_error("confirm", refunds.CONFIRM_REQUIRED)
            elif data["amount"] > facts.refundable:
                form.add_error("amount", refunds.AMOUNT_OVER.format(refundable=facts.refundable))
            else:
                submit(
                    request,
                    JobKind.PAYMENT_REFUND,
                    payment,
                    {
                        "amount": data["amount"],
                        "confirmed": True,
                        "creation_key": data["creation_key"],
                    },
                    replay_key=data["creation_key"],
                )
                return redirect("ledger:payments:detail", pk=payment.pk)
        _hold(request, target, form)
        return redirect("ledger:payments:refund", pk=payment.pk)
    facts = refunds.payment_facts(payment)
    form = _held(request, target, RefundForm) or RefundForm(
        initial={"creation_key": uuid.uuid4(), "amount": facts.refundable}
    )
    return render(
        request,
        "ledger/payments/refund.html",
        {
            **_summary(payment, facts),
            "form": form,
            "sentence": refunds.REFUND_SENTENCE,
            "confirmation": refunds.REFUND_CONFIRMATION,
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def payment_void(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    payment = _load(pk)
    target = f"payment-void:{payment.pk}"
    if request.method == "POST":
        form = VoidForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if already_submitted(data["creation_key"]):
                messages.info(request, refunds.ALREADY_SUBMITTED)
                return redirect("ledger:payments:detail", pk=payment.pk)
            refusal = refunds.payment_facts(payment).void_refusal
            if refusal:
                messages.error(request, refusal)
                return redirect("ledger:payments:void", pk=payment.pk)
            if data["confirm"] is not True:
                form.add_error("confirm", refunds.CONFIRM_REQUIRED)
            else:
                submit(
                    request,
                    JobKind.PAYMENT_VOID,
                    payment,
                    {"confirmed": True, "creation_key": data["creation_key"]},
                    replay_key=data["creation_key"],
                )
                return redirect("ledger:payments:detail", pk=payment.pk)
        _hold(request, target, form)
        return redirect("ledger:payments:void", pk=payment.pk)
    form = _held(request, target, VoidForm) or VoidForm(initial={"creation_key": uuid.uuid4()})
    return render(
        request,
        "ledger/payments/void.html",
        {
            **_summary(payment, refunds.payment_facts(payment)),
            "form": form,
            "sentence": refunds.VOID_SENTENCE,
            "confirmation": refunds.void_confirmation(payment),
        },
    )
