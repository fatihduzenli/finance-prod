"""One-off charge screens: detail, manual payment, the marking retry, link an invoice,
edit, waive, cancel, the provider retry and adding a setup charge from the lease page
(docs/qa/PLAN-010b D5, D6, D12; PLAN-010d D6, D9). Creating a one-off charge from the
operator or lease page was replaced by the invoice builder (PLAN-012 D6,
:mod:`ledger.views.invoices`); its URLs answer 410 Gone (:func:`create_form_gone`).

The detail page is read by any role. Recording a payment, retrying the marking of the
provider invoice or a failed provider request, linking an invoice, editing, cancelling
and adding a setup charge (GET of the forms and POST) need ``billing``; waiving needs
``admin``, and so does deleting the live provider invoice of a waived charge (PLAN-011
D0; POST only, queued as ``invoice_delete`` with the waived flag), and so does the Charge
now confirmation page and its POST (D4), which queues the single ``invoice_pay``
attempt and reads the bank method from the stored methods snapshot, never from the
provider. The registry checks the POST first; these views refuse the GET of the forms
below the same role.
:mod:`ledger.services.one_off_charges` re-checks every rule under a row lock and writes
the audit rows. No view calls the provider: the service queues the provider work
(``invoice_create``, ``schedule_create``, ``invoice_delete``, ``schedule_delete``), a
recorded payment whose invoice is to be marked is queued as a ``one_off_payment_mark``
job keyed by the form's one-time key, the marking retry as a
``one_off_payment_retry_mark`` job, and the detail page shows the latest job. Every POST
ends in a redirect; a form with errors is held in the session for the next GET.

``one_off_charges:export`` (PLAN-011 D5) is one-off charges as CSV, of one lease or
operator, any role.
"""

from __future__ import annotations

import uuid

from django import forms
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import (
    CancelChargeForm,
    ChargeNowForm,
    LinkInvoiceForm,
    ManualPaymentForm,
    SetupChargeAddForm,
    SetupChargeEditForm,
    WaiveForm,
)
from ledger.models import (
    ChargeEntityType,
    CollectionMethod,
    DepositResolution,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
)
from ledger.services import collections, csv_export, rent_run
from ledger.services import one_off_charges as service
from ledger.services.csv_export import CsvWriter
from ledger.views.charges import _report
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
from payments import operator_sync
from payments.jobs import is_active, latest_for
from payments.models import JobKind
from payments.provider import mask_identifier

NOTHING_TO_RETRY = "Nothing to retry on this charge."


def _load(pk: str) -> OneOffCharge:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        OneOffCharge.objects.select_related(
            "operator", "lease", "lease__store", "lease__operator", "fee_type"
        ),
        pk=key,
    )


def _error(form: forms.BaseForm, field: str | None, message: str) -> None:
    """A refusal on its field, or on the whole form when the form has no such field."""
    form.add_error(field if field in form.fields else None, message)


def _refuse_below(request: HttpRequest, role: Role) -> HttpResponse | None:
    if not meets(_role(request), role):
        return forbidden(request)
    return None


def _operator(charge: OneOffCharge) -> Operator | None:
    if charge.operator is not None:
        return charge.operator
    return charge.lease.operator if charge.lease is not None else None


EXPORT_HEADER = (
    "Operator",
    "Store",
    "Description",
    "Category",
    "Collection",
    "Due date",
    "Price (USD)",
    "Cost (USD)",
    "Paid (USD)",
    "Balance (USD)",
    "Status",
    "Deposit",
    "Needs review",
)


@role_required
@require_safe
def charge_export(request: HttpRequest) -> StreamingHttpResponse:
    """``ledger:one_off_charges:export`` (PLAN-011 D5): one-off charges, of one lease
    (``lease``) or billed to one operator (``operator``, as the operator page lists them)
    when given, newest due first; an unreadable filter is ignored."""
    charges = OneOffCharge.objects.select_related(
        "operator", "lease__store", "lease__operator", "fee_type"
    )
    lease = uuid_param(request, "lease")
    if lease is not None:
        charges = charges.filter(lease_id=lease)
    operator = uuid_param(request, "operator")
    if operator is not None:
        charges = charges.filter(operator_id=operator)
    records = (
        [
            billed.display_name if (billed := _operator(charge)) is not None else "",
            charge.lease.store.external_label if charge.lease is not None else "",
            charge.label,
            charge.get_category_display(),
            charge.get_collection_display(),
            charge.due_date,
            charge.price,
            charge.cost,
            charge.amount_paid,
            charge.balance,
            charge.get_status_display(),
            charge.fee_type is not None and charge.fee_type.refundable,
            charge.needs_review,
        ]
        for charge in charges.order_by("-due_date", "-created_at", "id")[
            : csv_export.CSV_MAX_ROWS + 1
        ].iterator(chunk_size=2_000)
    )
    return csv_response(CsvWriter(EXPORT_HEADER).lines(records), export_name("one-off-charges"))


@role_required
@require_safe
def charge_detail(request: HttpRequest, pk: str) -> HttpResponse:
    charge = _load(pk)
    billing = meets(_role(request), Role.BILLING)
    admin = meets(_role(request), Role.ADMIN)
    closed = charge.status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)
    retry_wait = service.retry_wait(charge) if billing else None
    jobs = job_context(charge)
    invoices = list(charge.invoices.order_by("issue_date", "invoice_number"))
    linked = rent_run.provider_invoice(charge)
    schedule = service.provider_schedule(charge)
    # A setup charge billed on an invoice (PLAN-012 D6) is not edited here.
    setup = charge.category == OneOffCategory.SETUP and charge.collection == CollectionMethod.NONE
    later_line = service.is_later_line(charge)
    return render(
        request,
        "ledger/one_off_charges/detail.html",
        {
            **jobs,
            "charge": charge,
            "lease": charge.lease,
            "operator": _operator(charge),
            "invoices": invoices,
            "provider_invoice_masked": mask_identifier(linked[1]) if linked else "",
            "schedule_masked": mask_identifier(schedule) if schedule else "",
            "payments": charge.payments.order_by("-settled_on", "-created_at"),
            "review_message": service.review_message(charge),
            # A later line of a builder invoice is paid through its first line's charge.
            "can_record": billing and not closed and charge.balance > 0 and not later_line,
            "can_link_invoice": billing and not closed and not invoices and not later_line,
            "can_edit": billing and not closed and setup,
            "can_waive": admin and not closed and not service.has_settled_payment(charge),
            "waive_refusal": (
                service.WAIVE_LIVE_INVOICE
                if not closed and service.live_provider_invoice(charge) is not None
                else ""
            ),
            "cancel_meaning": service.CANCEL_MEANING,
            "waive_meaning": service.WAIVE_MEANING,
            "delete_invoice_hint": service.DELETE_INVOICE_HINT,
            "can_delete_invoice": admin
            and not jobs["job_refresh"]
            and not service.delete_invoice_refusal(charge),
            "can_cancel": billing
            and not service.cancel_refusal(charge)
            and not jobs["job_refresh"],
            "can_retry_provider": billing
            and not jobs["job_refresh"]
            and service.retry_kind(charge) is not None,
            "can_retry_mark": retry_wait == 0 and not jobs["job_refresh"],
            "retry_wait_message": (
                operator_sync.retry_wait_message(retry_wait) if retry_wait else ""
            ),
            **_charge_now_context(charge, admin, closed, jobs["job_refresh"]),
            "resolution": DepositResolution.objects.filter(setup_charge=charge)
            .select_related("to_lease__store", "created_by")
            .first(),
            # PLAN-011b D4: the charge's collection e-mails and the hand send.
            **collections.charge_context(
                ChargeEntityType.ONE_OFF_CHARGE,
                charge.pk,
                billing=billing,
                url=reverse("ledger:one_off_charges:send_step", kwargs={"pk": charge.pk}),
            ),
        },
    )


def jobs_active(charge: OneOffCharge) -> bool:
    return is_active(latest_for(charge))


def _charge_now_context(
    charge: OneOffCharge, admin: bool, closed: bool, busy_now: bool
) -> dict[str, object]:
    """The Charge now part of the detail page (D4): the button for an admin when the
    local rules allow it, else the sentence that stops it; a failed attempt's one-minute
    wait. Local data only."""
    if charge.collection != CollectionMethod.CHARGE_NOW or closed:
        return {"charge_now_shown": False}
    refusal = service.charge_now_refusal(charge)
    wait = service.charge_now_wait(charge) if not refusal else 0
    return {
        "charge_now_shown": True,
        "can_charge_now": admin and not refusal and not wait and not busy_now,
        "charge_now_refusal": refusal,
        "charge_now_wait": service.CHARGE_NOW_WAIT.format(seconds=wait) if wait else "",
    }


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_now(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:one_off_charges:charge_now`` (admin; D4): the confirmation page and its
    POST, which queues the single attempt. The page reads local data only."""
    refused = _refuse_below(request, Role.ADMIN)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"one-off-charge-now:{charge.pk}"
    if request.method == "POST":
        form = ChargeNowForm(request.POST)
        if form.is_valid():
            outcome = service.charge_now_confirm(
                charge.pk, actor=_actor(request), confirmed=form.cleaned_data["confirm"] is True
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:one_off_charges:charge_now", pk=charge.pk)
    form = _held(request, target, ChargeNowForm) or ChargeNowForm()
    details = service.charge_now_request(charge, _actor(request))
    wait = details.wait_seconds
    return render(
        request,
        "ledger/one_off_charges/charge_now.html",
        {
            "form": form,
            "charge": charge,
            "details": details,
            "one_attempt": service.CHARGE_NOW_ONE_ATTEMPT,
            "wait_text": service.CHARGE_NOW_WAIT.format(seconds=wait) if wait else "",
            "busy": jobs_active(charge),
            "busy_text": service.CHARGE_NOW_BUSY,
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_payment(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"one-off-charge-payment:{charge.pk}"
    if request.method == "POST":
        form = ManualPaymentForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if already_submitted(data["creation_key"]):
                messages.info(request, rent_run.PAY_ALREADY)
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
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
                    JobKind.ONE_OFF_PAYMENT_MARK,
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
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:one_off_charges:payment", pk=charge.pk)
    form = _held(request, target, ManualPaymentForm) or ManualPaymentForm(
        initial={"creation_key": uuid.uuid4()}
    )
    return render(
        request, "ledger/one_off_charges/payment_form.html", {"form": form, "charge": charge}
    )


@role_required
@require_POST
def charge_retry_mark(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    if not busy(request, charge):
        submit(request, JobKind.ONE_OFF_PAYMENT_RETRY_MARK, charge)
    return redirect("ledger:one_off_charges:detail", pk=charge.pk)


@role_required
@require_POST
def charge_retry(request: HttpRequest, pk: str) -> HttpResponse:
    """The single manual retry of a failed provider request (PLAN-010d D3): a new job."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    if not busy(request, charge):
        kind = service.retry_kind(charge)
        if kind is None:
            messages.info(request, NOTHING_TO_RETRY)
        else:
            submit(request, kind, charge)
    return redirect("ledger:one_off_charges:detail", pk=charge.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_link(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"one-off-charge-link:{charge.pk}"
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
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:one_off_charges:link", pk=charge.pk)
    form = _held(request, target, LinkInvoiceForm) or LinkInvoiceForm()
    return render(
        request,
        "ledger/one_off_charges/link_invoice.html",
        {
            "form": form,
            "charge": charge,
            "has_invoice": charge.invoices.exists(),
            "rows": rent_run.unlinked_invoices(charge),
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_edit(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"one-off-charge-edit:{charge.pk}"
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
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:one_off_charges:edit", pk=charge.pk)
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
        "ledger/one_off_charges/form.html",
        {
            "form": form,
            "charge": charge,
            "lease": charge.lease,
            "price_locked": service.has_payment(charge),
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_waive(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _refuse_below(request, Role.ADMIN)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"one-off-charge-waive:{charge.pk}"
    if request.method == "POST":
        form = WaiveForm(request.POST)
        if form.is_valid():
            note = form.cleaned_data["note"]
            outcome = service.waive(charge.pk, note=note, actor=_actor(request))
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:one_off_charges:waive", pk=charge.pk)
    form = _held(request, target, WaiveForm) or WaiveForm()
    return render(
        request,
        "ledger/one_off_charges/waive.html",
        {
            "form": form,
            "charge": charge,
            "waive_meaning": service.WAIVE_MEANING,
            "refusal": (
                service.WAIVE_LIVE_INVOICE if service.live_provider_invoice(charge) else ""
            ),
        },
    )


@role_required
@require_POST
def charge_delete_invoice(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:one_off_charges:delete_invoice`` (admin; PLAN-011 D0): queue the delete of
    a waived charge's live provider invoice. The charge stays waived."""
    refused = _refuse_below(request, Role.ADMIN)
    if refused is not None:
        return refused
    charge = _load(pk)
    outcome = service.delete_waived_invoice(charge.pk, actor=_actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:one_off_charges:detail", pk=charge.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_cancel(request: HttpRequest, pk: str) -> HttpResponse:
    """Cancel an unpaid one-off charge (PLAN-010d D6): the note, then the queued delete."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"one-off-charge-cancel:{charge.pk}"
    if request.method == "POST":
        form = CancelChargeForm(request.POST)
        if form.is_valid():
            outcome = service.cancel_charge(
                charge.pk, note=form.cleaned_data["note"], actor=_actor(request)
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:one_off_charges:cancel", pk=charge.pk)
    form = _held(request, target, CancelChargeForm) or CancelChargeForm()
    return render(
        request,
        "ledger/one_off_charges/cancel.html",
        {"form": form, "charge": charge, "refusal": service.cancel_refusal(charge)},
    )


def _load_lease(pk: str) -> Lease:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Lease.objects.select_related("store", "operator"), pk=key)


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
                return redirect("ledger:one_off_charges:detail", pk=outcome.charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:leases:add_setup_charge", pk=lease.pk)
    form = _held(request, target, SetupChargeAddForm) or SetupChargeAddForm(
        initial={"due_date": lease.started_on or timezone.localdate()}
    )
    return render(
        request, "ledger/one_off_charges/form.html", {"form": form, "charge": None, "lease": lease}
    )


#: The removed "Create one-off charge" form's answer (PLAN-012 D6).
CREATE_FORM_GONE = (
    "Create one-off charge was replaced by New invoice on the store and operator pages."
)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def create_form_gone(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:operators:new_charge`` and ``ledger:leases:new_charge`` (PLAN-012 D6): the
    removed form answers 410 Gone with a sentence pointing to New invoice, to billing and
    above; below billing it answers 403 as the form did (the registry refuses a POST
    first)."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    return render(
        request, "ledger/one_off_charges/gone.html", {"message": CREATE_FORM_GONE}, status=410
    )
