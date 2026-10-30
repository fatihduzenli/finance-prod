"""The transfer wizard's pages (docs/qa/PLAN-012e-lease-end-transfer-installments.md D6).

Admin only, GET and POST: the registry refuses a POST below admin first, and each view
refuses every method below admin with 403. ``ledger:stores:transfer`` starts (or
resumes) the store's transfer; the steps live under ``ledger:transfers:*`` keyed by the
transfer, so a lease is reached only through its own transfer. The end of the outgoing
lease is the end-of-lease wizard's steps (``ledger:lease_ends:*``), which show the
transfer's steps and come back here: the end date (``ledger:transfers:end`` leads
there), the transfer's blockers, the statement and the settlement, then the incoming
lease and one confirm for both effects. A step whose earlier step is not done redirects
to it; nothing past the blockers is offered while one is listed.

A lease-end balance blocks the confirm (D14): the confirm page shows it, and an admin
allows it to remain with a written reason (``ledger:transfers:override_debt``, POST
only), which the confirm and confirmed pages then show as "Allowed by".

Every change goes through :mod:`ledger.services.transfer`, which audits it; an operator
created on the operator step goes through :func:`ledger.services.operators.create_operator`
as the operator form does (local only, PLAN-014 C3). No view calls the payment provider.
Every POST ends in a redirect; a form with errors is held in the session for the next
GET.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.db import IntegrityError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import (
    OperatorCreateForm,
    TransferConfirmForm,
    TransferDebtOverrideForm,
    TransferIncomingForm,
    TransferOperatorForm,
)
from ledger.models import LeaseEnd, LeaseTransfer, LeaseTransferStatus, Operator, Store
from ledger.services import lease_end, leases, statements
from ledger.services import operators as operator_service
from ledger.services import transfer as service
from ledger.views import wizard_frame
from ledger.views.operators import (
    EMAIL_TAKEN,
    _actor,
    _held,
    _hold,
    _role,
    role_required,
)
from ledger.wording import format_amount, provider_sentence

#: (key, label, route) of each step; ``lease_ends`` routes take the lease end's id.
STEPS: tuple[tuple[str, str, str], ...] = (
    ("operator", "Incoming operator", "ledger:transfers:operator"),
    ("terms", "End date", "ledger:lease_ends:terms"),
    (
        "blockers",
        provider_sentence("Still open at {provider}, and balances"),
        "ledger:transfers:blockers",
    ),
    ("statement", "Statement and deductions", "ledger:lease_ends:statement"),
    ("settlement", "Settlement", "ledger:lease_ends:settlement"),
    ("incoming", "Incoming lease", "ledger:transfers:incoming"),
    ("confirm", "Confirm", "ledger:transfers:confirm"),
)
CLEAR_FIRST = "Clear the listed items first."
OPERATOR_FIRST = "Choose the incoming operator first."
INCOMING_FIRST = "Set the incoming lease first."


def transfer_steps(transfer: LeaseTransfer, current: str) -> list[tuple[str, str, bool]]:
    """(label, link or "", current) per step of a transfer in progress: the operator and
    the end date are always linked, the later steps once the end date is set, the confirm
    once the incoming lease is set (before that it sends back to the incoming step, so it
    is not offered as a link: PLAN-012c D5, a step is a link where it can be opened)."""
    end = transfer.lease_end
    open_steps = end is not None and lease_end.terms_set(end)
    found = []
    for name, label, route in STEPS:
        url = ""
        reachable = name != "confirm" or service.incoming_set(transfer)
        if name != current and reachable and (open_steps or name in ("operator", "terms")):
            pk = end.pk if route.startswith("ledger:lease_ends:") and end is not None else None
            url = reverse(route, kwargs={"pk": pk if pk is not None else transfer.pk})
        found.append((label, url, name == current))
    return found


def _admin(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    return None


def _key(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _load(pk: str) -> LeaseTransfer:
    return get_object_or_404(
        LeaseTransfer.objects.select_related(
            "store",
            "outgoing_lease",
            "outgoing_lease__operator",
            "lease_end",
            "incoming_operator",
            "incoming_lease",
            "draft",
            "debt_override_by",
        ),
        pk=_key(pk),
    )


def _context(transfer: LeaseTransfer, current: str, **extra: Any) -> dict[str, Any]:
    steps = transfer_steps(transfer, current)
    figures = extra.get("figures")
    return {
        "transfer": transfer,
        "end": transfer.lease_end,
        "lease": transfer.outgoing_lease,
        "store": transfer.store,
        "step_links": steps,
        # PLAN-012f-a D8: the frame's step count, Back link and title; the tiles' notes.
        **wizard_frame.frame(steps, transfer.store),
        **(wizard_frame.statement_notes(figures) if figures is not None else {}),
        **extra,
    }


def _draft_only(transfer: LeaseTransfer) -> HttpResponse | None:
    if transfer.status != LeaseTransferStatus.DRAFT:
        return redirect("ledger:transfers:confirm", pk=transfer.pk)
    return None


def _needs_terms(transfer: LeaseTransfer) -> HttpResponse | None:
    end = transfer.lease_end
    if end is None:
        raise Http404
    if not lease_end.terms_set(end):
        return redirect("ledger:lease_ends:terms", pk=end.pk)
    return None


def _needs_statement(request: HttpRequest, transfer: LeaseTransfer) -> HttpResponse | None:
    """PLAN-012f-a §10: confirm needs the lease end's statement step saved in this wizard."""
    end = transfer.lease_end
    if end is None:
        raise Http404
    if end.statement_saved_at is None:
        return wizard_frame.statement_first(request, end)
    return None


def _needs_operator(request: HttpRequest, transfer: LeaseTransfer) -> HttpResponse | None:
    if not service.operator_set(transfer):
        messages.error(request, OPERATOR_FIRST)
        return redirect("ledger:transfers:operator", pk=transfer.pk)
    return None


def _needs_clear(request: HttpRequest, transfer: LeaseTransfer) -> HttpResponse | None:
    if service.blockers(transfer):
        messages.error(request, CLEAR_FIRST)
        return redirect("ledger:transfers:blockers", pk=transfer.pk)
    return None


# --- Start (the store page) ----------------------------------------------------------------


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_transfer(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:transfer``: start or resume the store's transfer."""
    refused = _admin(request)
    if refused is not None:
        return refused
    store = get_object_or_404(Store, pk=_key(pk))
    found = service.in_progress(store)
    if request.method == "POST":
        try:
            transfer = service.start(store, _actor(request))
        except ValueError as refusal:
            messages.error(request, str(refusal))
            return redirect("ledger:stores:detail", pk=store.pk)
        return redirect("ledger:transfers:operator", pk=transfer.pk)
    lease = found.outgoing_lease if found is not None else lease_end.current_lease(store)
    # PLAN-012b-b D9: a plain lease end in progress (its statement step saved) is
    # continued or cancelled first; one that never got there is adopted on Start.
    plain = (
        LeaseEnd.objects.filter(lease=lease).first()
        if found is None and lease is not None
        else None
    )
    return render(
        request,
        "ledger/transfers/start.html",
        {
            "store": store,
            "lease": lease,
            "draft": found,
            "end_in_progress": plain if lease_end.in_progress(plain) else None,
        },
    )


# --- Steps -----------------------------------------------------------------------------------


def _operator_create_form(data: dict[str, Any] | None = None) -> OperatorCreateForm:
    if data is None:
        return OperatorCreateForm(initial={"creation_key": uuid.uuid4()})
    return OperatorCreateForm(data)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def operator(request: HttpRequest, pk: str) -> HttpResponse:
    """Step 1: pick the incoming operator from a search by name, or create one."""
    refused = _admin(request)
    if refused is not None:
        return refused
    transfer = _load(pk)
    done = _draft_only(transfer)
    if done is not None:
        return done
    target = f"transfer-operator:{transfer.pk}"
    if request.method == "POST":
        if request.POST.get("action") == "create":
            return _create_operator(request, transfer, target)
        pick = TransferOperatorForm(request.POST)
        chosen = None
        if pick.is_valid():
            chosen = Operator.objects.filter(pk=pick.cleaned_data["operator"]).first()
        outcome = service.set_operator(transfer, chosen, actor=_actor(request))
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect("ledger:transfers:end", pk=transfer.pk)
        messages.error(request, outcome.message)
        return redirect("ledger:transfers:operator", pk=transfer.pk)
    query = request.GET.get("q", "")
    form = _held(request, target, _operator_create_form) or _operator_create_form()
    return render(
        request,
        "ledger/transfers/operator.html",
        _context(
            transfer,
            "operator",
            query=query,
            results=service.search_operators(query, transfer),
            create_form=form,
        ),
    )


def _create_operator(request: HttpRequest, transfer: LeaseTransfer, target: str) -> HttpResponse:
    """The operator form on the step: saved as on the operator page, then chosen."""
    form = _operator_create_form(request.POST)
    if form.is_valid():
        key = form.cleaned_data["creation_key"]
        try:
            created_operator, created = operator_service.create_operator(
                form.cleaned_data, creation_key=key, actor=_actor(request)
            )
        except IntegrityError:
            form.add_error("email", EMAIL_TAKEN)
        else:
            if created:
                messages.success(request, "Operator saved.")
            outcome = service.set_operator(transfer, created_operator, actor=_actor(request))
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:transfers:end", pk=transfer.pk)
            messages.error(request, outcome.message)
            return redirect("ledger:transfers:operator", pk=transfer.pk)
    _hold(request, target, form)
    return redirect("ledger:transfers:operator", pk=transfer.pk)


@role_required
@require_safe
def end(request: HttpRequest, pk: str) -> HttpResponse:
    """Step 2: the end of the outgoing lease, on the end-of-lease wizard's pages."""
    refused = _admin(request)
    if refused is not None:
        return refused
    transfer = _load(pk)
    done = _draft_only(transfer)
    if done is not None:
        return done
    if transfer.lease_end is None:
        raise Http404
    return redirect("ledger:lease_ends:terms", pk=transfer.lease_end.pk)


@role_required
@require_safe
def blockers(request: HttpRequest, pk: str) -> HttpResponse:
    """Step 3: what is live at the payment provider and what the outgoing operator still
    owes, each with its page."""
    refused = _admin(request)
    if refused is not None:
        return refused
    transfer = _load(pk)
    done = _draft_only(transfer) or _needs_terms(transfer)
    if done is not None:
        return done
    return render(
        request,
        "ledger/transfers/blockers.html",
        _context(transfer, "blockers", blockers=service.blockers(transfer)),
    )


def _incoming_form(
    transfer: LeaseTransfer, data: dict[str, Any] | None = None
) -> TransferIncomingForm:
    fee_types = service.setup_types()
    offered = service.deposit_type() is not None
    if data is not None:
        return TransferIncomingForm(data, fee_types=fee_types, deposit_offered=offered)
    initial: dict[str, Any]
    if service.incoming_set(transfer):
        initial = {
            "started_on": transfer.incoming_started_on,
            "rent": transfer.incoming_rent,
            "debit_day": transfer.incoming_debit_day,
            "second_debit_day": transfer.incoming_second_debit_day,
            "payment_channel": transfer.incoming_channel,
            "prorate_first_month": transfer.incoming_prorate_first_month,
            "deposit_amount": transfer.deposit_amount,
        }
        for line in service.setup_lines(transfer):
            # The items already chosen keep their price (PLAN-012d D9: a price means
            # charged).
            initial[f"setup_{line.fee_type.code}_price"] = line.price
    else:
        outgoing = transfer.outgoing_lease
        end = transfer.lease_end
        rent = leases.open_rent(outgoing)
        initial = {
            "started_on": (
                end.ended_on + timedelta(days=1)
                if end is not None and end.ended_on is not None
                else None
            ),
            "rent": rent.amount if rent is not None else None,
            "debit_day": outgoing.debit_day,
            "second_debit_day": outgoing.second_debit_day,
            "prorate_first_month": True,
            "deposit_amount": service.default_deposit(transfer),
        }
    return TransferIncomingForm(initial=initial, fee_types=fee_types, deposit_offered=offered)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def incoming(request: HttpRequest, pk: str) -> HttpResponse:
    """Step 6: the incoming lease, its deposit and its setup items."""
    refused = _admin(request)
    if refused is not None:
        return refused
    transfer = _load(pk)
    done = (
        _draft_only(transfer)
        or _needs_operator(request, transfer)
        or _needs_terms(transfer)
        or _needs_clear(request, transfer)
    )
    if done is not None:
        return done
    target = f"transfer-incoming:{transfer.pk}"
    if request.method == "POST":
        form = _incoming_form(transfer, request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = service.set_incoming(
                transfer,
                started_on=data["started_on"],
                rent=data["rent"],
                debit_day=data["debit_day"],
                second_debit_day=data["second_debit_day"],
                channel=data["payment_channel"],
                prorate_first_month=data["prorate_first_month"] is True,
                deposit_amount=data["deposit_amount"],
                setup=[service.SetupInput(fee, price) for fee, price in form.setup_items()],
                actor=_actor(request),
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:transfers:confirm", pk=transfer.pk)
            field = {"channel": "payment_channel"}.get(outcome.field or "", outcome.field)
            form.add_error(field if field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:transfers:incoming", pk=transfer.pk)
    form = _held(request, target, lambda data: _incoming_form(transfer, data)) or _incoming_form(
        transfer
    )
    return render(
        request,
        "ledger/transfers/incoming.html",
        _context(transfer, "incoming", form=form),
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def confirm(request: HttpRequest, pk: str) -> HttpResponse:
    """Step 7: the "are you sure" page for both effects and the confirm; once confirmed,
    the transfer's page."""
    refused = _admin(request)
    if refused is not None:
        return refused
    transfer = _load(pk)
    if transfer.status == LeaseTransferStatus.CONFIRMED:
        if request.method == "POST":
            messages.info(request, service.NOT_DRAFT)
            return redirect("ledger:transfers:confirm", pk=transfer.pk)
        return render(
            request,
            "ledger/transfers/confirmed.html",
            {"transfer": transfer, "store": transfer.store, "end": transfer.lease_end},
        )
    done = (
        _needs_operator(request, transfer)
        or _needs_terms(transfer)
        or _needs_clear(request, transfer)
        or _needs_statement(request, transfer)
    )
    if done is None and not service.incoming_set(transfer):
        messages.error(request, INCOMING_FIRST)
        done = redirect("ledger:transfers:incoming", pk=transfer.pk)
    if done is not None:
        return done
    target = f"transfer-confirm:{transfer.pk}"
    if request.method == "POST":
        form = TransferConfirmForm(request.POST)
        if form.is_valid():
            outcome = service.confirm(transfer, _actor(request), confirmed=True)
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:transfers:confirm", pk=transfer.pk)
            if outcome.field == "statement" and transfer.lease_end is not None:
                return wizard_frame.statement_first(request, transfer.lease_end)
            form.add_error(None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:transfers:confirm", pk=transfer.pk)
    form = _held(request, target, TransferConfirmForm) or TransferConfirmForm()
    end_row = transfer.lease_end
    if end_row is None:
        raise Http404
    figures = statements.final(end_row)
    return render(
        request,
        "ledger/transfers/confirm.html",
        _context(
            transfer,
            "confirm",
            form=form,
            figures=figures,
            setup=service.setup_lines(transfer),
            deposit_type=service.deposit_type(),
            settlement_missing=figures.refund_due > 0 and not end_row.refund_method,
            debt=figures.debt,
            debt_amount=format_amount(figures.debt),
            debt_allowed=_debt_allowed(transfer, figures.debt),
            override_form=TransferDebtOverrideForm(),
        ),
    )


def _debt_allowed(transfer: LeaseTransfer, debt: Decimal) -> bool:
    """Whether the balance on the page is within the admin's allowance (D14)."""
    allowed = transfer.debt_override_amount
    return transfer.debt_override_at is not None and allowed is not None and debt <= allowed


@role_required
def override_debt(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:transfers:override_debt`` (admin, POST only; D14): allow the lease-end
    balance to remain on the outgoing operator with a written reason. Below admin every
    method is refused with 403 first."""
    refused = _admin(request)
    if refused is not None:
        return refused
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    transfer = _load(pk)
    form = TransferDebtOverrideForm(request.POST)
    if not form.is_valid():
        # The form's own sentences, as messages: the confirm page holds its own form.
        for error in form.errors.get("reason", []):
            messages.error(request, str(error))
        return redirect("ledger:transfers:confirm", pk=transfer.pk)
    outcome = service.override_debt(transfer, form.cleaned_data["reason"], _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:transfers:confirm", pk=transfer.pk)


@role_required
@require_POST
def cancel(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin(request)
    if refused is not None:
        return refused
    transfer = _load(pk)
    store = transfer.store
    outcome = service.cancel(transfer, _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
        return redirect("ledger:stores:detail", pk=store.pk)
    messages.error(request, outcome.message)
    return redirect("ledger:transfers:confirm", pk=transfer.pk)
