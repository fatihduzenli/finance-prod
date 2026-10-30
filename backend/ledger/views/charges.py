"""Rent charge screens: detail, manual payment, the marking retry and linking an invoice
(PLAN-007 §4.1, D8; PLAN-009 D1 to D3, D5; PLAN-008a §2 rule 4), and the Retry of a
split charge's failed release (PLAN-012b-b D13, billing).

The detail page is read by any role and shows the carried balance of earlier charges,
under "← Store NNNN" (PLAN-012d D5). The payment form opened from the store page
(``?store=``) returns to its Payments tab (D8).
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
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from finance.dates import month_text
from ledger.forms import (
    HOW_DEBIT,
    HOW_RECORD,
    AddPaymentForm,
    ChargeNowForm,
    LinkInvoiceForm,
)
from ledger.models import (
    ChargeEntityType,
    ChargeNote,
    Lease,
    Operator,
    RentCharge,
    RentChargeStatus,
    Store,
)
from ledger.services import (
    contacts,
    csv_export,
    installments,
    leases,
    messaging,
    rent_run,
    workspace,
    workspace_cards,
)
from ledger.services import detail_cards as detail_service
from ledger.services import one_off_charges as one_off_service
from ledger.services import stores as store_service
from ledger.services.csv_export import CsvWriter
from ledger.services.rent_run import MarkAction, MarkResult
from ledger.views import store_context
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
from ledger.views.run import _billing_only
from ledger.wording import format_amount
from payments import lease_sync
from payments.models import JobKind

NOTHING_TO_RETRY = "Nothing to retry on this charge."


def _load(pk: str) -> RentCharge:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        RentCharge.objects.select_related("lease", "lease__store", "lease__operator"), pk=key
    )


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
    """``ledger:charges:export`` (PLAN-011 D5): rent charges, of one lease (``lease``), one
    store (``store``, the store page's Charges tab, PLAN-012d D4) or one operator
    (``operator``) when given, newest month first; an unreadable filter is ignored."""
    charges = RentCharge.objects.select_related("lease__store", "lease__operator")
    lease = uuid_param(request, "lease")
    if lease is not None:
        charges = charges.filter(lease_id=lease)
    store = uuid_param(request, "store")
    if store is not None:
        charges = charges.filter(lease__store_id=store)
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
    _drop_debit_notes(request, charge)
    return render(request, "ledger/charges/detail.html", _detail_context(request, charge))


def _detail_context(request: HttpRequest, charge: RentCharge) -> dict[str, Any]:
    """The rent charge page's context (shared with today's bank-debit confirmation, which
    opens over the page). Local data only."""
    role = _role(request)
    billing = meets(role, Role.BILLING)
    retry_wait = rent_run.provider_retry_wait(charge) if billing else None
    jobs = job_context(charge)
    # PLAN-012f-a D4: the invoices with their sublines, the header's line, the tiles' tone,
    # the store's open promise and the Activity card.
    invoice_rows = detail_service.invoice_rows(charge, "rent")
    payments = list(charge.payments.order_by("-settled_on", "-created_at"))
    lease = charge.lease
    balance_open = charge.status != RentChargeStatus.WAIVED and charge.balance > 0
    return {
        **jobs,
        "charge": charge,
        "lease": charge.lease,
        # PLAN-012d D5: "← Store NNNN".
        "crumb_store": charge.lease.store,
        "invoice_rows": invoice_rows,
        "can_open_invoice": billing,
        "payments": payments,
        "header_line": detail_service.header_line(
            store=store_service.title(lease.store),
            operator=lease.operator.display_name if lease.operator is not None else "",
            created=charge.created_at,
            source=detail_service.rent_source(charge),
            due=charge.due_on,
        ),
        "balance_open": balance_open,
        "promise": contacts.open_promise(lease.operator, lease) if balance_open else None,
        "can_record_outcome": billing
        and lease.operator is not None
        and lease.operator.archived_at is None,
        **activity_context(
            [
                *((rent_run.ENTITY_INVOICE, row.invoice.pk) for row in invoice_rows),
                *((rent_run.ENTITY_PAYMENT, row.pk) for row in payments),
            ],
            admin=meets(role, Role.ADMIN),
            store=lease.store,
            record=(rent_run.ENTITY_CHARGE, charge.pk),
        ),
        "notes": ChargeNote.objects.filter(rent_charge=charge).order_by("-created_at"),
        "review_message": rent_run.charge_review_message(charge),
        "carried_balance": rent_run.carried_balance(charge),
        "can_record": billing
        and charge.status != RentChargeStatus.WAIVED
        and charge.balance > 0
        and not rent_run.on_live_plan(rent_charge=charge),
        # PLAN-012e D9, D10: the plan's progress and the Split button.
        **parent_context(
            charge,
            billing=billing,
            admin=meets(role, Role.ADMIN),
            split_url=reverse("ledger:charges:split", kwargs={"pk": charge.pk}),
        ),
        "can_retry_mark": retry_wait == 0 and not jobs["job_refresh"],
        # PLAN-012b-b D13: a failed release of the split charge's invoice or autopay.
        "can_retry_release": billing and installments.retry_release(charge) is not None,
        "can_link_invoice": billing and not invoice_rows,
        "retry_wait_message": (lease_sync.retry_wait_message(retry_wait) if retry_wait else ""),
        # PLAN-011b D4, PLAN-014 D6: the charge's messages and "Send a message now".
        **messaging.charge_context(
            ChargeEntityType.RENT_CHARGE,
            charge.pk,
            billing=billing,
            url=reverse("ledger:charges:send_message", kwargs={"pk": charge.pk}),
        ),
        # PLAN-012f-b D19: the open bank debit from Add payment, beside the payments.
        "open_debit": one_off_service.open_debit(charge),
    }


# --- Add payment (PLAN-012f-b D14; DESIGN-SPEC §4 Add payment) ------------------------


def _typed_amount(value: object) -> Decimal:
    """An amount as typed, or zero for an empty, invalid or negative one."""
    try:
        amount = Decimal(str(value or "").strip() or "0")
    except InvalidOperation:
        return Decimal("0.00")
    return amount if amount.is_finite() and amount > 0 else Decimal("0.00")


def render_add_payment(
    request: HttpRequest,
    form: AddPaymentForm,
    *,
    charge_label: str,
    crumb_label: str,
    charge_url: str,
    store: Store | None,
    operator: Operator | None,
    billing_lease: Lease | None,
    status: str,
    status_label: str,
    amount: Decimal,
    paid: Decimal,
    balance: Decimal,
    due: date | None,
    bank: Any,
    cancel_url: str,
    large_debit: bool = False,
) -> HttpResponse:
    """The Add payment page of a rent or a one-off charge (PLAN-012f-b D14): the header
    (the status badge with the balance, the muted line), the two radio cards, the Note
    card, the SUMMARY (tiles, the "After this payment" line, the one primary whose words
    follow the card) and the OPERATOR card, with the phone and the connection of the lease
    that bills the charge (``billing_lease``, PLAN-014 C5). Local data only."""
    chosen = form.chosen()
    typed = form["debit_amount" if chosen == HOW_DEBIT else "amount"].value()
    this_payment = _typed_amount(typed)
    left = balance - this_payment
    # PLAN-012f-c D8: "● Due · $2,100.00", the badge's dot and label, then the amount.
    badge = f"{status_label} · {format_amount(balance)}" if balance > 0 else status_label
    line = detail_service.header_line(
        store=store_service.title(store) if store is not None else "",
        operator=operator.display_name if operator is not None else "",
        due=due,
    )
    return render(
        request,
        "ledger/payments/add_payment.html",
        {
            "form": form,
            "how_options": form.options(),
            "chosen": chosen,
            "how_debit": HOW_DEBIT,
            "how_record": HOW_RECORD,
            "charge_label": charge_label,
            "crumb_label": crumb_label,
            "charge_url": charge_url,
            "crumb_store": store,
            "operator": operator,
            "status": status,
            "status_badge_label": badge,
            "header_line": " · ".join(part for part in (charge_label, line) if part),
            "amount": amount,
            "paid": paid,
            "balance": balance,
            "this_payment": this_payment,
            "paid_in_full": this_payment > 0 and left <= 0,
            "still_owed": max(left, Decimal("0.00")),
            "bank": bank,
            "connected": bool(leases.customer_ref(billing_lease)),
            "billing_phone": (
                lease_sync.billing_of(billing_lease).phone if billing_lease is not None else ""
            ),
            "returns_this_year": (
                workspace_cards.returns_this_year(operator, timezone.localdate())
                if operator is not None
                else 0
            ),
            "cancel_url": cancel_url,
            "large_debit": large_debit,
        },
    )


def usable_bank(lease: Lease | None) -> Any:
    """The linked bank method of the lease's account as stored, when it is a usable bank
    account (the OPERATOR card's "bank ···· last 4"), or ``None``. No provider call."""
    record = leases.linked_method_record(lease)
    return record if record is not None and record.is_usable_bank else None


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_record_payment(request: HttpRequest, pk: str) -> HttpResponse:
    """Add payment on a rent charge (PLAN-012f-b D14, §10 D19): ``record`` is today's
    manual payment; ``debit`` takes the whole balance as one bank debit, greyed with
    :func:`ledger.services.one_off_charges.rent_debit_options`'s reason: on today it opens
    the confirmation (admin; nothing written here), on a later date it schedules the debit
    (billing) and lands on the rent charge."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    charge = _load(pk)
    context = store_context.for_lease(request, charge.lease)
    detail_url = reverse("ledger:charges:detail", kwargs={"pk": charge.pk})
    done = store_context.return_url(request, context, workspace.TAB_PAYMENTS, detail_url)
    target = f"charge-payment:{charge.pk}"
    options = one_off_service.rent_debit_options(charge)
    admin = meets(_role(request), Role.ADMIN)

    def build(data: Any = None, **kwargs: Any) -> AddPaymentForm:
        return AddPaymentForm(
            data,
            debit_reason=options.reason,
            debit_reason_more=options.reason_more,
            debit_reason_url=options.reason_url,
            balance=options.balance,
            admin=admin,
            **kwargs,
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
                return redirect(done)
            form.add_error(outcome.field, outcome.message)
        _hold(request, target, form)
        return redirect(request.get_full_path())
    _drop_debit_notes(request, charge)
    form = _held(request, target, build) or build(initial={"creation_key": uuid.uuid4()})
    lease = charge.lease
    month = month_text(charge.period_month)
    return render_add_payment(
        request,
        form,
        charge_label=f"Rent {month}, debit {charge.seq}",
        crumb_label=f"Rent {month}, {lease.store.external_label}",
        charge_url=detail_url,
        store=lease.store,
        operator=lease.operator,
        billing_lease=lease,
        status=charge.status,
        status_label=str(charge.get_status_display()),
        amount=charge.amount_due,
        paid=charge.amount_paid,
        balance=charge.balance,
        due=charge.due_on,
        bank=usable_bank(lease),
        cancel_url=done,
        large_debit=options.large,
    )


#: Where a refusal of :func:`ledger.services.one_off_charges.rent_debit` sits on Add payment.
DEBIT_FIELD = {"amount": "debit_amount", "on": "debit_on", "confirm": "confirm_large_debit"}


def _note_key(charge: RentCharge, key: uuid.UUID) -> str:
    """The session entry holding Add payment's note for today's debit until the
    confirmation's POST passes it to :func:`ledger.services.one_off_charges.rent_debit`."""
    return f"rent-debit-note:{charge.pk}:{key}"


def _drop_debit_notes(request: HttpRequest, charge: RentCharge) -> None:
    """Forget every today's-debit note of ``charge`` (PLAN-013a C4): the note lives for one
    request, the confirmation's; a GET of the rent charge page or of Add payment means
    the confirmation was left, so nothing stays behind in the session."""
    prefix = f"rent-debit-note:{charge.pk}:"
    for name in [name for name in request.session.keys() if name.startswith(prefix)]:
        request.session.pop(name, None)


def _debit(
    request: HttpRequest, charge: RentCharge, form: AddPaymentForm, target: str
) -> HttpResponse:
    """Add payment's bank debit of a rent charge (D19): today → the confirmation with the
    page's one-time key (writes nothing); a later date → :func:`rent_debit` → the charge.
    The form has refused an amount other than the balance."""
    data = form.cleaned_data
    key = data["creation_key"]
    if data["debit_on"] == timezone.localdate():
        # The note waits in the session for the confirmation's POST, never in the URL.
        if data["note"]:
            request.session[_note_key(charge, key)] = data["note"]
        url = reverse("ledger:charges:charge_now", kwargs={"pk": charge.pk})
        return redirect(f"{url}?{urlencode({'key': str(key)})}")
    outcome = one_off_service.rent_debit(
        charge,
        data["debit_on"],
        actor=_actor(request),
        confirmed=data["confirm_large_debit"] is True,
        key=key,
        note=data["note"],
    )
    if outcome.ok:
        if outcome.message == one_off_service.DEBIT_ALREADY:
            messages.info(request, outcome.message)
        else:
            messages.success(request, outcome.message)
        return redirect("ledger:charges:detail", pk=charge.pk)
    form.add_error(DEBIT_FIELD.get(outcome.field or "", "how"), outcome.message)
    _hold(request, target, form)
    return redirect(request.get_full_path())


def _page_key(request: HttpRequest) -> uuid.UUID:
    """The one-time key Add payment sent in ``?key=``, else a new one."""
    try:
        return uuid.UUID(request.GET.get("key", ""))
    except ValueError:
        return uuid.uuid4()


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_now(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:charges:charge_now`` (admin; D19): the confirmation of today's bank
    debit of the rent charge, over its page (the amount, the bank method's last four, the
    large-debit box). The GET writes nothing; the POST runs
    :func:`ledger.services.one_off_charges.rent_debit`, which creates the debit and queues
    the single attempt."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    charge = _load(pk)
    store_context.for_lease(request, charge.lease)
    target = f"rent-debit-confirm:{charge.pk}"
    if request.method == "POST":
        form = ChargeNowForm(request.POST)
        if form.is_valid():
            key = form.cleaned_data["key"]
            note_key = _note_key(charge, key) if key is not None else ""
            outcome = one_off_service.rent_debit(
                charge,
                timezone.localdate(),
                actor=_actor(request),
                confirmed=form.cleaned_data["confirm"] is True,
                key=key,
                note=str(request.session.get(note_key, "")) if note_key else "",
            )
            if outcome.ok:
                if note_key:
                    request.session.pop(note_key, None)
                if outcome.message == one_off_service.DEBIT_ALREADY:
                    messages.info(request, outcome.message)
                else:
                    messages.success(request, outcome.message)
                return redirect("ledger:charges:detail", pk=charge.pk)
            form.add_error("confirm" if outcome.field == "confirm" else None, outcome.message)
        _hold(request, target, form)
        return redirect(request.get_full_path())
    form = _held(request, target, ChargeNowForm) or ChargeNowForm(
        initial={"key": _page_key(request)}
    )
    return render(
        request,
        "ledger/charges/charge_now.html",
        {
            **_detail_context(request, charge),
            "form": form,
            "details": one_off_service.rent_debit_request(charge, _actor(request)),
            "one_attempt": one_off_service.CHARGE_NOW_ONE_ATTEMPT,
        },
    )


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
@require_POST
def charge_retry_release(request: HttpRequest, pk: str) -> HttpResponse:
    """Repeat a split charge's failed release (PLAN-012b-b D13): a new job of the same
    kind with the release flag."""
    refused = _billing_only(request)
    if refused is not None:
        return refused
    charge = _load(pk)
    if not busy(request, charge):
        kind = installments.retry_release(charge)
        if kind is None:
            messages.info(request, NOTHING_TO_RETRY)
        else:
            submit(request, kind, charge, {installments.RELEASE_PARAM: True})
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
            "has_invoice": rent_run.charge_invoices(charge).exists(),
            "rows": rent_run.unlinked_invoices(charge),
        },
    )
