"""The invoice builder's screens (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md
D6, D7, D9).

"New invoice" on the store page (the store's lease in effect today preselected; a choice
on a hand-over day; refused when none) and on the operator page (one of the operator's
leases, or the operator alone) saves a **draft** and opens its page. The draft page
(billing) edits the lines (rows are added and removed by a small script; without it the
page shows a fixed number of blank rows and a Remove box per row), the memo and the due
date, and offers Preview, Print view, Send (billing), Charge now (admin, with the
confirmation of PLAN-010d D4) and Cancel. After sending it shows the invoice number, the
invoice's state at the payment provider, its latest provider request and each line's
charge. The preview and the print view (a standalone page with the print stylesheet and
no navigation; "Save as PDF" from the browser's print dialog) are read by any role.

The registry checks every POST first; these views refuse the forms' GET below the same
role. :mod:`ledger.services.invoice_builder` re-checks every rule under the draft's row
lock and writes the audit rows. No view calls the provider. Every POST ends in a
redirect; a form with errors is held in the session for the next GET, without any value
that holds a card number.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import (
    CancelChargeForm,
    ChargeNowForm,
    InvoiceDraftForm,
    InvoiceLineFormSet,
    draft_lines,
)
from ledger.models import (
    FeeType,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    Operator,
    Store,
)
from ledger.services import contacts, invoice_builder, one_off_charges
from ledger.validators import contains_card_number
from ledger.views.jobs import job_context
from ledger.views.operators import _actor, _role, role_required

#: The session key of a draft form held with its errors.
HELD_KEY = "invoice_draft_form"
LINES_PREFIX = "lines"
NO_LEASE_TODAY = "This store has no lease in effect today: choose the operator's page instead."
NOT_EDITABLE = "This invoice is no longer a draft."


# --- Loading --------------------------------------------------------------------------


def _key(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _load(pk: str) -> InvoiceDraft:
    return get_object_or_404(
        InvoiceDraft.objects.select_related(
            "operator", "lease", "lease__store", "lease__operator", "sent_invoice"
        ),
        pk=_key(pk),
    )


def _refuse_below(request: HttpRequest, role: Role) -> HttpResponse | None:
    if not meets(_role(request), role):
        return forbidden(request)
    return None


def _operator_leases(operator: Operator) -> list[Lease]:
    return list(
        operator.leases.select_related("store", "operator").order_by(
            "store__external_label", "started_on", "id"
        )
    )


# --- Forms ----------------------------------------------------------------------------


def _fee_types() -> list[FeeType]:
    return invoice_builder.offered_fee_types()


def _formset(
    data: dict[str, Any] | None = None, initial: list[dict[str, Any]] | None = None
) -> Any:
    return InvoiceLineFormSet(
        data,
        initial=initial,
        prefix=LINES_PREFIX,
        form_kwargs={"fee_types": _fee_types()},
    )


def _hold(request: HttpRequest, target: str, errors: Iterable[str]) -> None:
    """Keep the posted form and its error sentences for the next GET of ``target``; a
    value holding a card number is dropped, so the session never stores one."""
    data = {
        name: ("" if contains_card_number(str(value)) else str(value))
        for name, value in request.POST.items()
        if name != "csrfmiddlewaretoken"
    }
    request.session[HELD_KEY] = {"target": target, "data": data, "errors": list(errors)}


def _held(request: HttpRequest, target: str) -> tuple[dict[str, Any] | None, list[str]]:
    held = request.session.pop(HELD_KEY, None)
    if not isinstance(held, dict) or held.get("target") != target:
        return None, []
    return dict(held.get("data") or {}), [str(text) for text in held.get("errors") or []]


def _form_errors(form: Any, formset: Any) -> list[str]:
    """Every error of the header form and the line rows, as sentences."""
    found = [str(error) for errors in form.errors.values() for error in errors]
    for index, row in enumerate(formset.forms, start=1):
        for errors in row.errors.values():
            found.extend(f"Line {index}: {error}" for error in errors)
    found.extend(str(error) for error in formset.non_form_errors())
    return found


def _draft_initial(draft: InvoiceDraft) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header = {
        "lease": str(draft.lease_id) if draft.lease_id else "",
        "memo": draft.memo,
        "due_date": draft.due_date,
    }
    lines = [
        {
            "fee_type": str(line.fee_type_id) if line.fee_type_id else "",
            "description": line.description,
            "quantity": line.quantity,
            "unit_amount": line.unit_amount,
        }
        for line in invoice_builder.lines_of(draft)
    ]
    return header, lines


# --- New invoice ----------------------------------------------------------------------


def _new(
    request: HttpRequest,
    *,
    operator: Operator | None,
    leases: list[Lease],
    allow_none: bool,
    preselected: Lease | None,
    back: str,
    heading: str,
) -> HttpResponse:
    target = f"invoice-new:{request.path}"
    if request.method == "POST":
        form = InvoiceDraftForm(request.POST, leases=leases, allow_none=allow_none)
        formset = _formset(request.POST)
        if form.is_valid() and formset.is_valid():
            lease = form.chosen_lease()
            billed = operator or (lease.operator if lease is not None else None)
            if billed is None:
                outcome = invoice_builder.Outcome(False, one_off_charges.NO_OPERATOR)
            else:
                outcome = invoice_builder.create_draft(
                    billed,
                    lease,
                    form.cleaned_data["memo"],
                    form.cleaned_data["due_date"],
                    draft_lines(formset),
                    _actor(request),
                    creation_key=form.cleaned_data.get("creation_key"),
                )
            if outcome.ok and outcome.draft is not None:
                messages.success(request, outcome.message)
                return redirect("ledger:invoices:draft", pk=outcome.draft.pk)
            _hold(request, target, [outcome.message])
        else:
            _hold(request, target, _form_errors(form, formset))
        return redirect(request.get_full_path())
    data, errors = _held(request, target)
    if data is not None:
        form = InvoiceDraftForm(data, leases=leases, allow_none=allow_none)
        formset = _formset(data)
        form.is_valid()
        formset.is_valid()
    else:
        form = InvoiceDraftForm(
            initial={
                "creation_key": uuid.uuid4(),
                "lease": str(preselected.pk) if preselected is not None else "",
            },
            leases=leases,
            allow_none=allow_none,
        )
        formset = _formset()
    return render(
        request,
        "ledger/invoices/new.html",
        {
            "form": form,
            "formset": formset,
            "errors": errors,
            "back": back,
            "heading": heading,
            "due_days": invoice_builder.DUE_DAYS,
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def new_store(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:new_store`` (billing): a draft on the store's lease in effect
    today (a choice on a hand-over day); refused when none is."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    store = get_object_or_404(Store, pk=_key(pk))
    leases = [
        lease
        for lease in contacts.store_leases(store, timezone.localdate())
        if lease.operator is not None and lease.operator.archived_at is None
    ]
    if not leases:
        messages.error(request, NO_LEASE_TODAY)
        return redirect("ledger:stores:detail", pk=store.pk)
    return _new(
        request,
        operator=None,
        leases=leases,
        allow_none=False,
        preselected=leases[0],
        back=reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
        heading=f"New invoice: store {store.external_label}",
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def new_operator(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:new_operator`` (billing): a draft of the operator, on one of its
    leases (``?lease=`` preselects one) or on none."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    operator = get_object_or_404(Operator, pk=_key(pk))
    if operator.archived_at is not None:
        messages.error(request, invoice_builder.OPERATOR_ARCHIVED)
        return redirect("ledger:operators:detail", pk=operator.pk)
    leases = _operator_leases(operator)
    wanted = request.GET.get("lease", "")
    preselected = next((lease for lease in leases if str(lease.pk) == wanted), None)
    return _new(
        request,
        operator=operator,
        leases=leases,
        allow_none=True,
        preselected=preselected,
        back=reverse("ledger:operators:detail", kwargs={"pk": operator.pk}),
        heading=f"New invoice: {operator.display_name}",
    )


# --- The draft ------------------------------------------------------------------------


def _sent_context(draft: InvoiceDraft) -> dict[str, Any]:
    first = invoice_builder.first_charge(draft)
    if first is None:
        return {"charges": [], "first_charge": None}
    return {
        **job_context(first),
        "charges": invoice_builder.sent_charges(draft),
        "first_charge": first,
        "cancel_refusal": one_off_charges.cancel_refusal(first),
    }


@role_required
@require_safe
def draft_page(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:draft`` (billing): the draft's form while it is a draft, the
    invoice and its charges once sent."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    draft = _load(pk)
    admin = meets(_role(request), Role.ADMIN)
    editable = draft.status == InvoiceDraftStatus.DRAFT
    target = f"invoice-edit:{draft.pk}"
    leases = _operator_leases(draft.operator)
    data, errors = _held(request, target)
    if not editable:
        form, formset = None, None
    elif data is not None:
        form = InvoiceDraftForm(data, leases=leases, allow_none=True)
        formset = _formset(data)
        form.is_valid()
        formset.is_valid()
    else:
        header, lines = _draft_initial(draft)
        form = InvoiceDraftForm(initial=header, leases=leases, allow_none=True)
        formset = _formset(initial=lines)
    sent = _sent_context(draft) if draft.status == InvoiceDraftStatus.SENT else {}
    return render(
        request,
        "ledger/invoices/draft.html",
        {
            **sent,
            "draft": draft,
            "preview": invoice_builder.preview(draft),
            "form": form,
            "formset": formset,
            "errors": errors,
            "editable": editable,
            "can_charge_now": editable and admin and one_off_charges.charge_now_enabled(),
            "can_cancel": draft.status == InvoiceDraftStatus.DRAFT
            or (draft.status == InvoiceDraftStatus.SENT and not sent.get("cancel_refusal")),
            "due_days": invoice_builder.DUE_DAYS,
        },
    )


@role_required
@require_POST
def draft_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:edit`` (billing): Save draft."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    draft = _load(pk)
    target = f"invoice-edit:{draft.pk}"
    form = InvoiceDraftForm(request.POST, leases=_operator_leases(draft.operator), allow_none=True)
    formset = _formset(request.POST)
    if form.is_valid() and formset.is_valid():
        outcome = invoice_builder.update_draft(
            draft.pk,
            form.chosen_lease(),
            form.cleaned_data["memo"],
            form.cleaned_data["due_date"],
            draft_lines(formset),
            _actor(request),
        )
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect("ledger:invoices:draft", pk=draft.pk)
        _hold(request, target, [outcome.message])
    else:
        _hold(request, target, _form_errors(form, formset))
    return redirect("ledger:invoices:draft", pk=draft.pk)


@role_required
@require_POST
def draft_send(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:send`` (billing): the draft becomes its invoice and charges,
    e-mailed once the provider has it."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    draft = _load(pk)
    outcome = invoice_builder.send(draft.pk, _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:invoices:draft", pk=draft.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def draft_charge_now(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:charge_now`` (admin): the confirmation page of 10d D4 (local
    data only) and its POST, which converts the draft for the single attempt."""
    refused = _refuse_below(request, Role.ADMIN)
    if refused is not None:
        return refused
    draft = _load(pk)
    if request.method == "POST":
        form = ChargeNowForm(request.POST)
        confirmed = form.is_valid() and form.cleaned_data["confirm"] is True
        outcome = invoice_builder.charge_now(draft.pk, _actor(request), confirmed=confirmed)
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect("ledger:invoices:draft", pk=draft.pk)
        messages.error(request, outcome.message)
        return redirect("ledger:invoices:charge_now", pk=draft.pk)
    return render(
        request,
        "ledger/invoices/charge_now.html",
        {
            "draft": draft,
            "details": invoice_builder.charge_now_details(draft, _actor(request)),
            "one_attempt": one_off_charges.CHARGE_NOW_ONE_ATTEMPT,
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def draft_cancel(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:cancel`` (billing): a draft is cancelled; a sent invoice's cancel
    (a note required) is the first line's charge's, which voids every line."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    draft = _load(pk)
    sent = draft.status == InvoiceDraftStatus.SENT
    if request.method == "POST":
        form = CancelChargeForm(request.POST)
        if sent and not form.is_valid():
            messages.error(request, one_off_charges.VOID_NOTE_REQUIRED)
            return redirect("ledger:invoices:cancel", pk=draft.pk)
        note = form.cleaned_data.get("note", "") if form.is_valid() else ""
        outcome = invoice_builder.cancel(draft.pk, _actor(request), note=note)
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect("ledger:invoices:draft", pk=draft.pk)
        messages.error(request, outcome.message)
        return redirect("ledger:invoices:cancel", pk=draft.pk)
    first = invoice_builder.first_charge(draft)
    return render(
        request,
        "ledger/invoices/cancel.html",
        {
            "draft": draft,
            "sent": sent,
            "form": CancelChargeForm(),
            "refusal": one_off_charges.cancel_refusal(first) if first is not None else "",
        },
    )


# --- Preview and print view (D7) ------------------------------------------------------


@role_required
@require_safe
def draft_preview(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:preview`` (any role): the invoice as the operator will read it."""
    draft = _load(pk)
    return render(
        request,
        "ledger/invoices/preview.html",
        {
            "draft": draft,
            "preview": invoice_builder.preview(draft),
            "billing": meets(_role(request), Role.BILLING),
        },
    )


@role_required
@require_safe
def draft_print(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:print`` (any role): a standalone page with the print stylesheet
    and no navigation, to save as PDF from the browser's print dialog."""
    draft = _load(pk)
    return render(
        request, "ledger/invoices/print.html", {"preview": invoice_builder.preview(draft)}
    )
