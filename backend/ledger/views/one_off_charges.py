"""One-off charge screens: detail, manual payment, the marking retry, link an invoice,
edit, waive, cancel and the provider retry (docs/qa/PLAN-010b D5, D6, D12; PLAN-010d D6,
D9). Adding a setup charge from the lease page moved into the invoice builder
(PLAN-012e-2 D4): its URL redirects there (:func:`lease_add_setup_charge`). Creating a
one-off charge from the operator or lease page was replaced by the invoice builder
(PLAN-012 D6, :mod:`ledger.views.invoices`); its URLs answer 410 Gone
(:func:`create_form_gone`).

The detail page is read by any role. Recording a payment, retrying the marking of the
provider invoice or a failed provider request, linking an invoice, editing, cancelling
(GET of the forms and POST) need ``billing``; waiving needs
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

import copy
import uuid
from typing import Any
from urllib.parse import urlencode

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
    HOW_DEBIT,
    AddPaymentForm,
    CancelChargeForm,
    ChargeNowForm,
    LinkInvoiceForm,
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
from ledger.services import (
    collections,
    contacts,
    csv_export,
    invoice_builder,
    rent_run,
    workspace,
)
from ledger.services import detail_cards as detail_service
from ledger.services import one_off_charges as service
from ledger.services import stores as store_service
from ledger.services.csv_export import CsvWriter
from ledger.services.installments import plan_label
from ledger.views import store_context, stores
from ledger.views.charges import _report, render_add_payment
from ledger.views.detail_cards import activity_context
from ledger.views.installments import parent_context
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
    (``lease``), of one store's leases (``store``, the store page's Charges tab, PLAN-012d
    D4) or billed to one operator (``operator``, as the operator page lists them) when
    given, newest due first; an unreadable filter is ignored."""
    charges = OneOffCharge.objects.select_related(
        "operator", "lease__store", "lease__operator", "fee_type"
    )
    lease = uuid_param(request, "lease")
    if lease is not None:
        charges = charges.filter(lease_id=lease)
    store = uuid_param(request, "store")
    if store is not None:
        charges = charges.filter(lease__store_id=store)
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
    # Every line of a builder invoice has the invoice (PLAN-012c-a D1); PLAN-012f-a D4: each
    # with its subline and the builder draft that sent it.
    invoice_rows = detail_service.invoice_rows(charge, str(charge.get_category_display()))
    invoices = [row.invoice for row in invoice_rows]
    payments = list(charge.payments.order_by("-settled_on", "-created_at"))
    operator = _operator(charge)
    store = charge.lease.store if charge.lease is not None else None
    balance_open = not closed and charge.balance > 0
    resolution = (
        DepositResolution.objects.filter(setup_charge=charge)
        .select_related("to_lease__store", "created_by")
        .first()
    )
    linked = rent_run.provider_invoice(charge)
    schedule = service.provider_schedule(charge)
    # A setup charge billed on an invoice (PLAN-012 D6) is not edited here.
    setup = charge.category == OneOffCategory.SETUP and charge.collection == CollectionMethod.NONE
    return render(
        request,
        "ledger/one_off_charges/detail.html",
        {
            **jobs,
            "charge": charge,
            "lease": charge.lease,
            # PLAN-012d D5: "← Store NNNN".
            "crumb_store": charge.lease.store if charge.lease is not None else None,
            "operator": operator,
            "invoices": invoices,
            "invoice_rows": invoice_rows,
            "can_open_invoice": billing,
            # PLAN-012b-a D5: an invoice released for the installment plan.
            "released_invoices": service.released_invoices(charge),
            "provider_invoice_masked": mask_identifier(linked[1]) if linked else "",
            "schedule_masked": mask_identifier(schedule) if schedule else "",
            "payments": payments,
            "review_message": service.review_message(charge),
            # Any line of a builder invoice takes a payment of its own (PLAN-012c-a D2).
            "can_record": billing
            and not closed
            and charge.balance > 0
            and not rent_run.on_live_plan(one_off_charge=charge),
            "can_link_invoice": billing and not closed and not invoices,
            "can_edit": billing and not closed and setup,
            "can_waive": admin
            and not closed
            and charge.category != OneOffCategory.INSTALLMENT
            and not service.has_settled_payment(charge)
            and not rent_run.on_live_plan(one_off_charge=charge),
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
            # PLAN-012e D9, D10: an installment's plan, or the parent's plan and Split.
            "installment_plan": charge.plan,
            # PLAN-012f-b D16: "Installment 3 of 10", or "Settlement plan · 3 of 10".
            "installment_label": (
                plan_label(charge.plan, charge.installment_no)
                if charge.plan is not None and charge.installment_no is not None
                else ""
            ),
            **(
                {}
                if charge.plan_id is not None
                else parent_context(
                    charge,
                    billing=billing,
                    split_url=reverse("ledger:one_off_charges:split", kwargs={"pk": charge.pk}),
                )
            ),
            "resolution": resolution,
            # PLAN-012f-a D4: the header's kind badge and muted line, the tiles' tone, the
            # open promise of the charge's store and the Activity card.
            "kind_badge": detail_service.kind_words(charge, resolution),
            "header_line": detail_service.header_line(
                store=store_service.title(store) if store is not None else "",
                operator=operator.display_name if operator is not None else "",
                created=charge.created_at,
                source=detail_service.one_off_source(charge, invoice_rows),
                due=charge.due_date,
            ),
            "balance_open": balance_open,
            "promise": contacts.open_promise(operator, charge.lease) if balance_open else None,
            "can_record_outcome": billing and operator is not None and operator.archived_at is None,
            **activity_context(
                [
                    *((rent_run.ENTITY_INVOICE, row.pk) for row in invoices),
                    *((rent_run.ENTITY_PAYMENT, row.pk) for row in payments),
                ],
                admin=admin,
                store=store,
                record=(service.ENTITY_ONE_OFF_CHARGE, charge.pk),
            ),
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


def _as_debit(charge: OneOffCharge) -> OneOffCharge:
    """The charge as today's bank debit reads it (PLAN-012f-b D14): an e-mail or manual
    charge is switched to Charge now by :func:`ledger.services.one_off_charges.debit`
    before Charge now's rules apply, so the confirmation page reads the same rules. An
    unsaved copy; nothing is written."""
    if charge.collection not in (CollectionMethod.EMAIL, CollectionMethod.MANUAL):
        return charge
    preview = copy.copy(charge)
    preview.collection = CollectionMethod.CHARGE_NOW
    return preview


def _note_key(charge: OneOffCharge, key: uuid.UUID) -> str:
    """The session entry holding Add payment's note for today's debit (C2-2) until the
    confirmation's POST passes it to :func:`ledger.services.one_off_charges.debit`."""
    return f"add-payment-note:{charge.pk}:{key}"


def _page_key(request: HttpRequest) -> uuid.UUID:
    """The one-time key Add payment sent in ``?key=``, else a new one."""
    try:
        return uuid.UUID(request.GET.get("key", ""))
    except ValueError:
        return uuid.uuid4()


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_now(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:one_off_charges:charge_now`` (admin; D4): the confirmation page and its
    POST, which debits the whole open balance today through
    :func:`ledger.services.one_off_charges.debit` (PLAN-012f-b D14: the one path of Charge
    now and of Add payment's bank debit on today; it queues the single attempt). The page
    reads local data only."""
    refused = _refuse_below(request, Role.ADMIN)
    if refused is not None:
        return refused
    charge = _load(pk)
    target = f"one-off-charge-now:{charge.pk}"
    if request.method == "POST":
        form = ChargeNowForm(request.POST)
        if form.is_valid():
            key = form.cleaned_data["key"]
            note_key = _note_key(charge, key) if key is not None else ""
            outcome = service.debit(
                charge,
                service.open_balance(charge),
                timezone.localdate(),
                actor=_actor(request),
                confirmed=form.cleaned_data["confirm"] is True,
                key=key,
                note=str(request.session.get(note_key, "")) if note_key else "",
            )
            if outcome.ok:
                if note_key:
                    request.session.pop(note_key, None)
                if outcome.message == service.DEBIT_ALREADY:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                return redirect("ledger:one_off_charges:detail", pk=charge.pk)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect(request.get_full_path())
    form = _held(request, target, ChargeNowForm) or ChargeNowForm(
        initial={"key": _page_key(request)}
    )
    details = service.charge_now_request(_as_debit(charge), _actor(request))
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


#: Where a refusal of :func:`ledger.services.one_off_charges.debit` sits on Add payment.
DEBIT_FIELD = {"amount": "debit_amount", "on": "debit_on", "confirm": "confirm_large_debit"}


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_payment(request: HttpRequest, pk: str) -> HttpResponse:
    """Add payment on a one-off charge (PLAN-012f-b D14): ``record`` is today's manual
    payment; ``debit`` on today opens Charge now's confirmation (admin; nothing written
    here), on a later date schedules the debit (billing) and lands on the charge."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    charge = _load(pk)
    context = _charge_context(request, charge)
    detail_url = reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk})
    done = store_context.return_url(request, context, workspace.TAB_PAYMENTS, detail_url)
    target = f"one-off-charge-payment:{charge.pk}"
    options = service.bank_debit_options(charge)
    admin = meets(_role(request), Role.ADMIN)

    def build(data: Any = None, **kwargs: Any) -> AddPaymentForm:
        return AddPaymentForm(
            data, debit_reason=options.reason, balance=options.balance, admin=admin, **kwargs
        )

    if request.method == "POST":
        form = build(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if data["how"] == HOW_DEBIT:
                return _debit(request, charge, form, target)
            if already_submitted(data["creation_key"]):
                messages.info(request, rent_run.PAY_ALREADY)
                return redirect(done)
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
                return redirect(done)
            _error(form, outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect(request.get_full_path())
    form = _held(request, target, build) or build(initial={"creation_key": uuid.uuid4()})
    operator = _operator(charge)
    return render_add_payment(
        request,
        form,
        charge_label=charge.label,
        crumb_label=charge.label,
        charge_url=detail_url,
        store=charge.lease.store if charge.lease is not None else None,
        operator=operator,
        status=charge.status,
        status_label=str(charge.get_status_display()),
        amount=charge.price,
        paid=charge.amount_paid,
        balance=charge.balance,
        due=charge.due_date,
        bank=options.method,
        cancel_url=done,
        large_debit=options.large,
    )


def _debit(
    request: HttpRequest, charge: OneOffCharge, form: AddPaymentForm, target: str
) -> HttpResponse:
    """Add payment's bank debit (D14): today → Charge now's confirmation with the page's
    one-time key (writes nothing); a later date → :func:`service.debit` → the charge."""
    data = form.cleaned_data
    key = data["creation_key"]
    if data["debit_on"] == timezone.localdate():
        # The note waits in the session for the confirmation's POST (C2-2), never in the URL.
        if data["note"]:
            request.session[_note_key(charge, key)] = data["note"]
        url = reverse("ledger:one_off_charges:charge_now", kwargs={"pk": charge.pk})
        return redirect(f"{url}?{urlencode({'key': str(key)})}")
    outcome = service.debit(
        charge,
        data["debit_amount"],
        data["debit_on"],
        actor=_actor(request),
        confirmed=data["confirm_large_debit"] is True,
        key=key,
        note=data["note"],
    )
    if outcome.ok:
        if outcome.message == service.DEBIT_ALREADY:
            messages.info(request, outcome.message)
        else:
            messages.success(request, outcome.message)
        return redirect("ledger:one_off_charges:detail", pk=charge.pk)
    _error(form, DEBIT_FIELD.get(outcome.field or "", "how"), outcome.message)
    _hold(request, target, form)
    return redirect(request.get_full_path())


def _charge_context(
    request: HttpRequest, charge: OneOffCharge
) -> store_context.StoreContext | None:
    """The store page a charge's form was opened from (``?store=``, PLAN-012d D8); 404
    for another store, and for a charge without a lease."""
    if charge.lease is None:
        if request.GET.get("store") or request.GET.get("lease"):
            raise Http404
        return None
    return store_context.for_lease(request, charge.lease)


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
            # A failed release is repeated with its flag (PLAN-012b-a D3).
            submit(request, kind, charge, service.retry_params(charge))
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
            "has_invoice": service.invoices_of(charge).exists(),
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
                service.WAIVE_PARENT_ON_PLAN
                if rent_run.on_live_plan(one_off_charge=charge)
                else service.WAIVE_LIVE_INVOICE
                if service.live_provider_invoice(charge)
                else ""
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


def setup_item_code(request: HttpRequest, lease: Lease) -> str:
    """The catalogue item the builder pre-fills on line 1 (PLAN-012e-2 D4): ``?item=``
    when it names an offered catalogue item, else the first setup-checklist item the lease
    has no charge of yet, else none (``""``). Always a code of the catalogue, never the
    request's text."""
    offered = {fee_type.code: fee_type for fee_type in service.offered_types()}
    wanted = request.GET.get("item") or ""
    if wanted in offered:
        return wanted
    taken = set(
        OneOffCharge.objects.filter(lease=lease, legacy=False, fee_type__isnull=False).values_list(
            "fee_type_id", flat=True
        )
    )
    first = next(
        (fee_type for fee_type in service.checklist_types() if fee_type.pk not in taken), None
    )
    return first.code if first is not None else ""


@role_required
@require_safe
def lease_add_setup_charge(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:leases:add_setup_charge`` (billing; PLAN-012e-2 D4): "Add setup charge"
    moved into the invoice builder; this route redirects to the lease's store's New
    invoice page with the lease, Save draft chosen and the setup item on line 1
    (:func:`setup_item_code`). A ``?store=``/``?lease=`` other than the lease's answers
    404, as the form did."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    lease = _load_lease(pk)
    store_context.for_lease(request, lease)
    return redirect(
        stores.builder_url(
            lease.store,
            lease.pk,
            invoice_builder.CollectChoice.DRAFT,
            setup_item_code(request, lease) or None,
        )
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
