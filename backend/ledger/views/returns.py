"""The returns queue (docs/qa/PLAN-008-webhooks.md D6, PLAN-011b D5).

Any role may read the queue; resolving needs ``billing`` (``accounts.registry`` and again
here) and a note. Resolving with a note records who and why; it changes no charge and
calls no provider. **Reschedule** (``returns:reschedule``, billing; the fee waiver admin,
403 otherwise) takes the agreed re-debit date and runs
:func:`ledger.services.nsf.reschedule`: the re-debit and NSF fee charges and one provider
job for the worker. No view calls the provider.

The queue exports as CSV (``returns:export``, PLAN-011 D5), any role.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import RescheduleReturnForm, ResolveReturnForm
from ledger.models import NSF_FEE_FIRST, NSF_FEE_LATER, FeeType, Payment
from ledger.services import csv_export, nsf, returns
from ledger.services.csv_export import CsvWriter
from ledger.services.leases import format_amount
from ledger.views.operators import (
    PAGE_SIZE,
    _actor,
    _role,
    csv_response,
    export_name,
    role_required,
)
from ledger.views.run import _billing_only


def _fee_label(code: str) -> str:
    price = FeeType.objects.filter(code=code).values_list("default_price", flat=True).first()
    return format_amount(price) if price is not None else "no amount set"


@role_required
@require_safe
def return_list(request: HttpRequest) -> HttpResponse:
    page = Paginator(returns.open_returns(), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/returns/list.html",
        {
            "page": page,
            "rows": [returns.row(payment) for payment in page],
            "can_resolve": meets(_role(request), Role.BILLING),
            "can_waive": meets(_role(request), Role.ADMIN),
            "today": timezone.localdate(),
            "nsf_first": _fee_label(NSF_FEE_FIRST),
            "nsf_later": _fee_label(NSF_FEE_LATER),
        },
    )


EXPORT_HEADER = (
    "Operator",
    "Store",
    "Charge kind",
    "Amount (USD)",
    "Return code",
    "Return reason",
    "Returned at",
    "Payment ID",
)


@role_required
@require_safe
def return_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:returns:export`` (PLAN-011 D5): the unresolved returns, as the queue."""
    records = (
        [
            item.operator.display_name if item.operator is not None else "",
            item.store.external_label if item.store is not None else "",
            item.payment.kind,
            item.amount,
            item.code,
            item.reason,
            item.returned_at,
            item.payment.pk,
        ]
        for item in (
            returns.row(payment)
            for payment in returns.open_returns()[: csv_export.CSV_MAX_ROWS + 1].iterator(
                chunk_size=2_000
            )
        )
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("returns"))


@role_required
@require_POST
def return_resolve(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    if not Payment.objects.filter(pk=key).exists():
        raise Http404
    form = ResolveReturnForm(request.POST)
    if not form.is_valid():
        errors = form.errors.get("note") or [returns.RESOLVE_NOTE_REQUIRED]
        messages.error(request, str(errors[0]))
        return redirect("ledger:returns:list")
    outcome = returns.resolve_return(key, note=form.cleaned_data["note"], actor=_actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:returns:list")


@role_required
@require_POST
def return_reschedule(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:returns:reschedule`` (PLAN-011b D5): billing; waiving the fee needs admin."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    if not Payment.objects.filter(pk=key).exists():
        raise Http404
    form = RescheduleReturnForm(request.POST)
    waiving = form.fields["waive_fee"].to_python(request.POST.get("waive_fee"))
    if waiving and not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    if not form.is_valid():
        errors = [str(error) for field_errors in form.errors.values() for error in field_errors]
        messages.error(request, errors[0] if errors else nsf.DATE_REQUIRED)
        return redirect(f"{reverse('ledger:returns:list')}#return-{key}")
    outcome = nsf.reschedule(
        key,
        redebit_date=form.cleaned_data["redebit_date"],
        waive_fee=form.cleaned_data["waive_fee"],
        note=form.cleaned_data["note"],
        actor=_actor(request),
        confirmed=form.cleaned_data["confirm"],
    )
    if outcome.forbidden:
        return forbidden(request)
    if not outcome.ok:
        messages.error(request, outcome.message)
        return redirect(f"{reverse('ledger:returns:list')}#return-{key}")
    messages.success(request, outcome.message)
    if outcome.redebit is not None:
        return redirect("ledger:one_off_charges:detail", pk=outcome.redebit.pk)
    return redirect("ledger:returns:list")
