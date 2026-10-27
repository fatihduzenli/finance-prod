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

"Void payment" of a payment recorded outside the processor (PLAN-012f-c D2, D5) needs
``billing``: the registry refuses a POST below billing and the view refuses its GET as well.
Nothing is queued and nothing is sent to the provider: the POST calls
:func:`ledger.services.refunds.void_manual_payment` and flashes its outcome. Its dialog shows
over the payment page; ``?store=`` naming another store than the payment's is a 404.
"""

from __future__ import annotations

import uuid
from typing import cast
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
from ledger.forms import RefundForm, VoidForm, VoidManualForm
from ledger.models import Operator, Payment, PaymentMethod, Store
from ledger.services import detail_cards, one_off_charges, refunds
from ledger.services import payments_ledger as ledger
from ledger.services import stores as store_service
from ledger.services.rent_run import charge_review_message
from ledger.validators import PAYMENT_DATA_MESSAGE
from ledger.views import store_context
from ledger.views.detail_cards import activity_context
from ledger.views.jobs import already_submitted, job_context, submit
from ledger.views.operators import (
    PAGE_SIZE,
    _actor,
    _held,
    _hold,
    _role,
    csv_response,
    role_required,
)
from ledger.wording import format_amount
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
            "voided_by",
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
        # PLAN-012d D5: "← Store NNNN".
        "crumb_store": row.store,
    }


@role_required
@require_safe
def payment_detail(request: HttpRequest, pk: str) -> HttpResponse:
    payment = _load(pk)
    return render(request, "ledger/payments/detail.html", _detail_context(request, payment))


def _detail_context(request: HttpRequest, payment: Payment) -> dict[str, object]:
    """The payment page's context (read only); Refund and Void show over it in their
    dialog (PLAN-012f-b E-4)."""
    role = _role(request)
    admin = meets(role, Role.ADMIN)
    facts = refunds.payment_facts(payment)
    summary = _summary(payment, facts)
    row = cast(ledger.LedgerRow, summary["row"])
    payment_refunds = list(payment.refunds.order_by("created_at", "id"))
    return {
        **summary,
        **job_context(payment),
        "refunds": payment_refunds,
        "charge_review": _charge_review(payment),
        # PLAN-012f-a D4: the header's line and the Activity card (the payment's rows
        # and its refunds').
        "header_line": detail_cards.header_line(
            store=store_service.title(row.store) if row.store is not None else "",
            operator=row.operator.display_name if row.operator is not None else "",
            created=payment.created_at,
        ),
        **activity_context(
            [(ENTITY_PAYMENT, refund.pk) for refund in payment_refunds],
            admin=admin,
            store=row.store,
            record=(ENTITY_PAYMENT, payment.pk),
        ),
        "can_refund": admin and facts.can_refund,
        "can_void": admin and facts.can_void,
        # PLAN-012f-c D6: a payment recorded outside the processor, settled: billing.
        "can_void_manual": meets(role, Role.BILLING) and not refunds.manual_void_refusal(payment),
        "parent_transaction": (
            mask_identifier(refunds.transaction_reference(payment.parent_payment))
            if payment.parent_payment is not None
            else ""
        ),
    }


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
                form.add_error(
                    "amount", refunds.AMOUNT_OVER.format(refundable=format_amount(facts.refundable))
                )
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
            **_detail_context(request, payment),
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
            **_detail_context(request, payment),
            **_summary(payment, refunds.payment_facts(payment)),
            "form": form,
            "sentence": refunds.VOID_SENTENCE,
            "confirmation": refunds.void_confirmation(payment),
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def payment_void_manual(request: HttpRequest, pk: str) -> HttpResponse:
    """Void a payment recorded outside the processor (PLAN-012f-c D2, D5): the reason in
    the dialog over the payment page; the POST voids it locally and flashes the outcome."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    payment = _load(pk)
    [row] = ledger.rows_for([payment])
    if row.store is None:
        if request.GET.get("store") or request.GET.get("lease"):
            raise Http404
    else:
        store_context.from_request(request, store=row.store)
    target = f"payment-void-manual:{payment.pk}"
    if request.method == "POST":
        form = VoidManualForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = refunds.void_manual_payment(
                payment.pk,
                reason=data["reason"],
                actor=_actor(request),
                creation_key=data["creation_key"],
            )
            if outcome.ok:
                (messages.info if outcome.replayed else messages.success)(request, outcome.message)
                return redirect("ledger:payments:detail", pk=payment.pk)
            if outcome.field != "reason":
                messages.error(request, outcome.message)
                return redirect("ledger:payments:detail", pk=payment.pk)
            form.add_error("reason", outcome.message)
        if form.holds_payment_data() or PAYMENT_DATA_MESSAGE in form.errors.get("reason", []):
            # Rule 1: a reason refused for payment data comes back empty, with its error.
            form.data = {
                name: "" if name == "reason" else form.data.get(name, "") for name in form.fields
            }
        _hold(request, target, form)
        return redirect(request.get_full_path())
    form = _held(request, target, VoidManualForm) or VoidManualForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(
        request,
        "ledger/payments/void_manual.html",
        {
            **_detail_context(request, payment),
            "form": form,
            "refusal": refunds.manual_void_refusal(payment),
            "sentences": refunds.void_manual_facts(payment),
        },
    )
