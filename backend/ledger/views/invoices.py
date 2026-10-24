"""The invoice builder's screens (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md
D6, D7, D9).

"New invoice" on the store page (the store's lease in effect today preselected; a choice
on a hand-over day; refused when none) and on the operator page (one of the operator's
leases, or the operator alone) saves a **draft** and opens its page. The draft page
(billing) edits the lines (rows are added and removed by a small script; without it the
page shows one blank row and a Remove box per row), the memo and the due
date, and offers Preview, Print view, Send (billing), Charge now (admin, with the
confirmation of PLAN-010d D4), Cancel and Delete (a never-sent draft, billing, no note
and no confirmation page; PLAN-012d D3). After sending it shows the invoice number, the
invoice's state at the payment provider, its latest provider request and each line's
charge. The preview and the print view (a standalone page with the print stylesheet and
no navigation; "Save as PDF" from the browser's print dialog) are read by any role.

The registry checks every POST first; these views refuse the forms' GET below the same
role. :mod:`ledger.services.invoice_builder` re-checks every rule under the draft's row
lock and writes the audit rows. No view calls the provider. Every POST ends in a
redirect; a form with errors is held in the session for the next GET, without any value
that holds a card number. A refusal naming another open draft links its number to that
draft's page.

Each line has one Item box (PLAN-012d D10) on one list: Rent, the draft's lease's
existing open charges, then the catalogue items. A new draft opens with one line; the
script adds lines, removes one with its trash button, numbers the visible lines and keeps
the total live, and fills a Rent line's amount from ``invoices:rent_amount`` (billing,
read-only JSON). Errors name the visible line: a removed row is neither checked nor
counted, and a service refusal is renumbered from its place among the filled lines.

Opened from the store page (``?store=&lease=``, PLAN-012d D2, D8;
:mod:`ledger.views.store_context`), the forms hide the lease, show the read-only line,
keep the context through Save draft, and Send and Delete return to the store page's
Invoices tab.

**How to collect** (docs/qa/PLAN-012e2-fewer-actions.md D3): both forms end with the
choice (:class:`~ledger.forms.CollectForm`). The draft is saved first (a new one created,
an existing one updated), then :func:`ledger.services.invoice_builder.collect` runs the
choice: Save draft opens the draft; Send by e-mail and Record payment now return to the
store page's Invoices tab when opened from it, else to the draft; Schedule for a date
returns to the store page's Charges tab (the draft became the charge), else to the
operator page; Charge now opens its confirmation page (admin; 10d D4). A refusal of the
choice keeps the saved draft and shows the sentence on its page with the choice's fields.
``?collect=<choice>`` pre-selects the radio (anything else, or Charge now for billing:
Save draft); ``?item=<catalogue code>`` fills line 1 of a new draft with that offered
catalogue item and its default price (anything else is ignored).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django import forms
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import (
    ITEMS_LIST_ID,
    RENT_ITEM,
    CancelChargeForm,
    ChargeNowForm,
    CollectForm,
    InvoiceDraftForm,
    InvoiceLineFormSet,
    draft_lines,
    initial_choice,
    kept_rows,
    line_numbers,
    rent_months,
)
from ledger.models import (
    CollectionMethod,
    FeeType,
    InvoiceDraft,
    InvoiceDraftStatus,
    Lease,
    OneOffCharge,
    Operator,
    Store,
)
from ledger.services import contacts, detail_cards, invoice_builder, one_off_charges, workspace
from ledger.services import stores as store_service
from ledger.services.invoice_builder import CollectChoice
from ledger.services.rent_run import CENT, ENTITY_INVOICE
from ledger.services.rent_run import ENTITY_CHARGE as ENTITY_RENT_CHARGE
from ledger.validators import contains_card_number
from ledger.views import store_context
from ledger.views.detail_cards import activity_context
from ledger.views.jobs import job_context
from ledger.views.operators import _actor, _role, role_required
from ledger.views.store_context import StoreContext

#: The session key of a draft form held with its errors.
HELD_KEY = "invoice_draft_form"
LINES_PREFIX = "lines"
NO_LEASE_TODAY = "This store has no lease in effect today: choose the operator's page instead."
NOT_EDITABLE = "This invoice is no longer a draft."
#: A draft's number in a refusal (:data:`ledger.services.invoice_builder.LINE_RENT_ON_DRAFT`).
DRAFT_NUMBER = re.compile(r"DRAFT-[0-9A-F]{8}")
#: The line a service refusal names, renumbered by the visible line (PLAN-012d D10).
LINE_NUMBER = re.compile(r"^Line (\d+): ")
#: The month of the rent-amount endpoint (PLAN-012d D10).
MONTH = re.compile(r"^(\d{4})-(\d{2})$")
BAD_MONTH = "Choose a month."


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


def _item_options(existing: Iterable[OneOffCharge]) -> list[dict[str, str]]:
    """What the Item box's list offers (PLAN-012b D2; PLAN-012d D10): Rent, then the
    existing open charges of the draft's lease (with the charge and its balance for the
    script), then the offered catalogue items, in catalogue order."""
    return [
        {"value": RENT_ITEM, "kind": "rent"},
        *(
            {
                "value": invoice_builder.existing_label(charge),
                "kind": "existing",
                "charge": str(charge.pk),
                "balance": f"{Decimal(charge.balance):.2f}",
            }
            for charge in existing
        ),
        *(
            {"value": fee_type.name, "kind": "item"}
            for fee_type in invoice_builder.offered_fee_types()
        ),
    ]


def _lines_total(formset: Any) -> Decimal:
    """The total of the rows the form shows (the line total of each kept row: quantity,
    empty 1, times the unit amount, rounded to the cent); a value that is not a number
    counts as nothing. Rendered under the table and kept live by the script."""
    total = Decimal("0.00")
    for _, form in kept_rows(formset):
        try:
            unit = Decimal(str(form["unit_amount"].value() or "0"))
            quantity = Decimal(str(form["quantity"].value() or "1"))
            if not (unit.is_finite() and quantity.is_finite()) or unit <= 0 or quantity <= 0:
                continue
            total += (quantity * unit).quantize(CENT, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            continue
    return total


def _line_rows(formset: Any) -> list[dict[str, Any]]:
    """The rows as the table shows them: each form, whether it was removed (its row is
    hidden) and its visible line number (PLAN-012d D10)."""
    numbers = {id(form): number for number, form in kept_rows(formset)}
    return [
        {"form": form, "removed": id(form) not in numbers, "number": numbers.get(id(form))}
        for form in formset.forms
    ]


def _lines_have_rent(formset: Any) -> bool:
    """Whether a kept row's Item is Rent: the table then starts with its Rent columns
    shown (PLAN-012e-3; ``ledger/invoice_lines.js`` keeps them in step afterwards)."""
    rent = invoice_builder.catalogue_key(RENT_ITEM)
    return any(
        invoice_builder.catalogue_key(str(form["item"].value() or "")) == rent
        for _, form in kept_rows(formset)
    )


def _renumber(message: str, numbers: list[int]) -> str:
    """A service refusal names the line by its place among the filled lines; the form
    names it by the visible line (PLAN-012d D10)."""
    match = LINE_NUMBER.match(message)
    if match is None:
        return message
    index = int(match.group(1))
    if not 1 <= index <= len(numbers):
        return message
    return f"Line {numbers[index - 1]}: " + message[match.end() :]


def _rent_leases(operators: Iterable[Operator], draft: InvoiceDraft | None = None) -> list[Lease]:
    """The leases a Rent line offers (PLAN-012b-b D4): the operators' leases in effect or
    starting later (not ended before today), and those the draft's Rent lines name."""
    today = timezone.localdate()
    named = (
        {line.rent_lease_id for line in invoice_builder.lines_of(draft) if line.rent_lease_id}
        if draft is not None
        else set()
    )
    rows = Lease.objects.filter(operator__in=list(operators)).select_related("store", "operator")
    return [
        lease
        for lease in rows.order_by("store__external_label", "started_on", "id")
        if lease.ended_on is None or lease.ended_on >= today or lease.pk in named
    ]


def _formset(
    data: dict[str, Any] | None = None,
    initial: list[dict[str, Any]] | None = None,
    *,
    rent_leases: Iterable[Lease] = (),
    default_lease: Lease | None = None,
    existing: Iterable[OneOffCharge] = (),
) -> Any:
    return InvoiceLineFormSet(
        data,
        initial=initial,
        prefix=LINES_PREFIX,
        form_kwargs={
            "rent_leases": list(rent_leases),
            "default_lease": default_lease,
            "months": rent_months(timezone.localdate()),
            "existing": list(existing),
        },
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


def _form_errors(form: Any, formset: Any, collect_form: CollectForm | None = None) -> list[str]:
    """Every error of the header form, the line rows and How to collect, as sentences."""
    found = [str(error) for errors in form.errors.values() for error in errors]
    # Numbered by the visible line: a removed row is neither checked nor counted.
    for index, row in kept_rows(formset):
        for errors in row.errors.values():
            found.extend(f"Line {index}: {error}" for error in errors)
    found.extend(str(error) for error in formset.non_form_errors())
    if collect_form is not None:
        found.extend(str(error) for errors in collect_form.errors.values() for error in errors)
    return found


# --- How to collect (PLAN-012e-2 D3) ---------------------------------------------------------

#: The chosen collection of a converted draft, by its charges' collection.
COLLECTION_LABELS = {
    CollectionMethod.EMAIL: "Send by e-mail",
    CollectionMethod.CHARGE_NOW: "Charge now",
    CollectionMethod.MANUAL: "Record payment now",
}


def _can_charge_now(request: HttpRequest) -> bool:
    return meets(_role(request), Role.ADMIN) and one_off_charges.charge_now_enabled()


def _collect_form(request: HttpRequest, data: Any = None, *, can_schedule: bool) -> CollectForm:
    """How to collect, bound to ``data`` (a POST or a held form), else with the radio
    ``?collect=`` names (:func:`~ledger.forms.initial_choice`). Schedule for a date is
    offered only where ``can_schedule`` (:func:`ledger.services.invoice_builder.can_schedule`)."""
    can = _can_charge_now(request)
    if data is not None:
        return CollectForm(data, can_charge_now=can, can_schedule=can_schedule)
    chosen = initial_choice(
        request.GET.get("collect"), can_charge_now=can, can_schedule=can_schedule
    )
    return CollectForm(initial={"collect": chosen}, can_charge_now=can, can_schedule=can_schedule)


def _posted_lease(data: Any, leases: Iterable[Lease], default: Lease | None) -> Lease | None:
    """The lease a posted (or held) form names, among ``leases``; else ``default``."""
    wanted = str(data.get("lease") or "") if data is not None else ""
    return next((lease for lease in leases if str(lease.pk) == wanted), default)


def _item_lines(request: HttpRequest) -> list[dict[str, Any]] | None:
    """Line 1 of a new draft from ``?item=<catalogue code>``: the offered catalogue item
    of that code with its default price, else ``None`` (the code is ignored)."""
    code = request.GET.get("item") or ""
    fee_type: FeeType | None = next(
        (row for row in invoice_builder.offered_fee_types() if row.code == code), None
    )
    if fee_type is None:
        return None
    return [{"item": fee_type.name, "unit_amount": fee_type.default_price}]


def _collected(
    request: HttpRequest,
    draft: InvoiceDraft,
    collect_form: CollectForm,
    context: StoreContext | None,
    saved: str,
) -> HttpResponse:
    """After the draft was saved (``saved`` is that sentence): run the chosen collection
    and go where it says (see the module docstring)."""
    choice = collect_form.choice()
    if choice in (CollectChoice.DRAFT, CollectChoice.CHARGE_NOW):
        messages.success(request, saved)
        if choice == CollectChoice.CHARGE_NOW:
            return redirect("ledger:invoices:charge_now", pk=draft.pk)
        return redirect(_draft_url(draft, context))
    lease = draft.lease
    data = collect_form.cleaned_data
    outcome = invoice_builder.collect(
        draft.pk,
        choice,
        actor=_actor(request),
        confirmed=data.get("confirm_large_debit") is True,
        due_date=data.get("schedule_on"),
        payment=collect_form.payment(),
    )
    if not outcome.ok:
        messages.success(request, saved)
        _hold(request, f"invoice-edit:{draft.pk}", [str(outcome.message)])
        return redirect(_draft_url(draft, context))
    messages.success(request, outcome.message)
    if choice == CollectChoice.SCHEDULE:
        if context is not None:
            return redirect(context.tab_url(workspace.TAB_CHARGES))
        if lease is not None:
            return redirect(workspace.tab_url(lease.store_id, workspace.TAB_CHARGES))
        return redirect("ledger:operators:detail", pk=draft.operator_id)
    if context is not None:
        return redirect(context.tab_url(workspace.TAB_INVOICES))
    back = store_context.local_next(request.GET.get("next"))
    if choice == CollectChoice.EMAIL and back:
        return redirect(back)
    return redirect(_draft_url(draft, context))


def _error_items(errors: Iterable[str], operators: Iterable[Operator]) -> list[dict[str, str]]:
    """The error sentences as the form shows them: a sentence naming another open draft
    of the same operators ("... is already on draft DRAFT-...", PLAN-012d) links that
    draft's number to its page; the sentence itself stays as the service wrote it."""
    texts = [str(error) for error in errors]
    numbers = {match.group(0) for text in texts for match in DRAFT_NUMBER.finditer(text)}
    drafts: dict[str, str] = {}
    if numbers:
        for draft in InvoiceDraft.objects.filter(
            operator__in=list(operators), status=InvoiceDraftStatus.DRAFT
        ).only("pk"):
            if draft.number in numbers:
                drafts[draft.number] = reverse("ledger:invoices:draft", kwargs={"pk": draft.pk})
    items = []
    for text in texts:
        match = DRAFT_NUMBER.search(text)
        if match is None or match.group(0) not in drafts:
            items.append({"text": text})
            continue
        items.append(
            {
                "text": text[: match.start()],
                "number": match.group(0),
                "url": drafts[match.group(0)],
                "after": text[match.end() :],
            }
        )
    return items


def _hide_lease(form: InvoiceDraftForm) -> None:
    """A draft opened from the store page with its lease (PLAN-012d D2): the lease is
    known, so its field is hidden and the read-only line names it."""
    form.fields["lease"].widget = forms.HiddenInput()


def _draft_context(request: HttpRequest, draft: InvoiceDraft) -> StoreContext | None:
    """The store page a draft was opened from (``?store=``); 404 for a store other than
    the draft's lease's, and for an operator-level draft."""
    if draft.lease is None:
        if request.GET.get("store") or request.GET.get("lease"):
            raise Http404
        return None
    return store_context.for_lease(request, draft.lease)


def _draft_url(draft: InvoiceDraft, context: StoreContext | None) -> str:
    return reverse("ledger:invoices:draft", kwargs={"pk": draft.pk}) + (
        context.query if context is not None else ""
    )


def _draft_initial(draft: InvoiceDraft) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header = {
        "lease": str(draft.lease_id) if draft.lease_id else "",
        "memo": draft.memo,
        "due_date": draft.due_date,
    }
    lines = [
        {
            "item": RENT_ITEM,
            "rent_lease": str(line.rent_lease_id),
            "rent_month": f"{line.rent_month:%Y-%m}" if line.rent_month else "",
            "description": line.description,
            "quantity": line.quantity,
            "unit_amount": line.unit_amount,
        }
        if line.rent_lease_id is not None
        else {
            "item": invoice_builder.existing_label(line.existing_charge),
            "existing_charge": str(line.existing_charge_id),
            "description": line.description,
            "quantity": line.quantity,
            "unit_amount": line.unit_amount,
        }
        if line.existing_charge is not None
        else {
            "item": line.item,
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
    context: StoreContext | None = None,
    store: Store | None = None,
) -> HttpResponse:
    target = f"invoice-new:{request.path}"
    operators = (
        [operator]
        if operator is not None
        else list(
            {lease.operator.pk: lease.operator for lease in leases if lease.operator}.values()
        )
    )
    rent_leases = _rent_leases(operators)
    existing = invoice_builder.open_charges(preselected)
    if request.method == "POST":
        form = InvoiceDraftForm(request.POST, leases=leases, allow_none=allow_none)
        formset = _formset(
            request.POST, rent_leases=rent_leases, default_lease=preselected, existing=existing
        )
        collect_form = _collect_form(
            request,
            request.POST,
            can_schedule=invoice_builder.can_schedule(
                _posted_lease(request.POST, leases, None if allow_none else preselected)
            ),
        )
        if form.is_valid() and formset.is_valid() and collect_form.is_valid():
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
                return _collected(request, outcome.draft, collect_form, context, outcome.message)
            _hold(request, target, [_renumber(outcome.message, line_numbers(formset))])
        else:
            _hold(request, target, _form_errors(form, formset, collect_form))
        return redirect(request.get_full_path())
    data, errors = _held(request, target)
    if data is not None:
        form = InvoiceDraftForm(data, leases=leases, allow_none=allow_none)
        formset = _formset(
            data, rent_leases=rent_leases, default_lease=preselected, existing=existing
        )
        collect_form = _collect_form(
            request,
            data,
            can_schedule=invoice_builder.can_schedule(_posted_lease(data, leases, preselected)),
        )
        form.is_valid()
        formset.is_valid()
        collect_form.is_valid()
    else:
        form = InvoiceDraftForm(
            initial={
                "creation_key": uuid.uuid4(),
                "lease": str(preselected.pk) if preselected is not None else "",
            },
            leases=leases,
            allow_none=allow_none,
        )
        formset = _formset(
            initial=_item_lines(request),
            rent_leases=rent_leases,
            default_lease=preselected,
            existing=existing,
        )
        collect_form = _collect_form(
            request, can_schedule=invoice_builder.can_schedule(preselected)
        )
    if context is not None and context.lease is not None:
        _hide_lease(form)
    return render(
        request,
        "ledger/invoices/new.html",
        {
            "form": form,
            "formset": formset,
            "item_options": _item_options(existing),
            "items_list_id": ITEMS_LIST_ID,
            "lines_total": _lines_total(formset),
            "line_rows": _line_rows(formset),
            "lines_have_rent": _lines_have_rent(formset),
            "rent_amount_url": _rent_amount_url(operators),
            "errors": _error_items(errors, operators),
            "store_context": context,
            "back": back,
            "heading": heading,
            # The page header's breadcrumb (PLAN-012f D10): the store or the operator the
            # builder was opened from.
            "crumb_store": store,
            "crumb_operator": operator,
            "due_days": invoice_builder.DUE_DAYS,
            "collect_form": collect_form,
            "collect_total": _lines_total(formset),
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
    context = store_context.from_request(request, store=store)
    if not leases:
        messages.error(request, NO_LEASE_TODAY)
        return redirect("ledger:stores:detail", pk=store.pk)
    if context is not None and context.lease is not None and context.lease not in leases:
        # A lease of this store that is not in effect today: the choice stays open.
        context = StoreContext(store=store, lease=None)
    preselected = context.lease if context is not None and context.lease else leases[0]
    return _new(
        request,
        operator=None,
        leases=leases,
        allow_none=False,
        preselected=preselected,
        back=(
            context.tab_url(workspace.TAB_INVOICES)
            if context is not None
            else reverse("ledger:stores:detail", kwargs={"pk": store.pk})
        ),
        heading=f"New invoice: store {store.external_label}",
        context=context,
        store=store,
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
    wanted_store = request.GET.get("store", "")
    if wanted_store and wanted_store not in {str(lease.store_id) for lease in leases}:
        # PLAN-012d D2: a store of another operator is never pre-filled.
        raise Http404
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
    rents = invoice_builder.sent_rent_charges(draft)
    if first is None:
        # An invoice of rent alone (PLAN-012b-b D5): its job acts on the rent charge.
        return {
            **(job_context(rents[0]) if rents else {}),
            "charges": [],
            "rent_charges": rents,
            "first_charge": None,
            "cancel_refusal": invoice_builder.RENT_ALONE_CANCEL if rents else "",
            "collection_label": COLLECTION_LABELS[CollectionMethod.EMAIL],
        }
    return {
        **job_context(first),
        "collection_label": COLLECTION_LABELS.get(
            CollectionMethod(first.collection), first.get_collection_display()
        ),
        "charges": invoice_builder.sent_charges(draft),
        "rent_charges": rents,
        "first_charge": first,
        "cancel_refusal": one_off_charges.cancel_refusal(first),
        # PLAN-012b-a D5: a live single-charge invoice is released by the split.
        "invoice_live": one_off_charges.live_provider_invoice(first) is not None,
    }


def _sent_detail(request: HttpRequest, draft: InvoiceDraft, sent: dict[str, Any]) -> dict[str, Any]:
    """The sent invoice page in the detail family (PLAN-012f-a D4): the Paid and Balance tiles
    (its charges' figures), the header's line and the Activity card (the invoice's rows, its
    draft's and its charges')."""
    invoice = draft.sent_invoice
    if invoice is None:
        return {}
    charges = list(sent.get("charges", []))
    rents = list(sent.get("rent_charges", []))
    store = draft.lease.store if draft.lease is not None else None
    return {
        "sent_paid": sum((row.amount_paid for row in [*charges, *rents]), Decimal("0.00")),
        "sent_balance": sum((row.balance for row in [*charges, *rents]), Decimal("0.00")),
        "header_line": detail_cards.header_line(
            store=store_service.title(store) if store is not None else "",
            operator=draft.operator.display_name,
            created=invoice.created_at,
            due=invoice.due_date,
        ),
        **activity_context(
            [
                (invoice_builder.ENTITY_DRAFT, draft.pk),
                *((one_off_charges.ENTITY_ONE_OFF_CHARGE, row.pk) for row in charges),
                *((ENTITY_RENT_CHARGE, row.pk) for row in rents),
            ],
            admin=meets(_role(request), Role.ADMIN),
            store=store,
            record=(ENTITY_INVOICE, invoice.pk),
        ),
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
    context = _draft_context(request, draft)
    editable = draft.status == InvoiceDraftStatus.DRAFT
    target = f"invoice-edit:{draft.pk}"
    leases = _operator_leases(draft.operator)
    rent_leases = _rent_leases([draft.operator], draft)
    existing = invoice_builder.open_charges(draft.lease) if editable else []
    data, errors = _held(request, target)
    schedulable = editable and invoice_builder.can_schedule(
        draft.lease, invoice_builder.lines_of(draft)
    )
    collect_form: CollectForm | None = None
    if not editable:
        form, formset = None, None
    elif data is not None:
        form = InvoiceDraftForm(data, leases=leases, allow_none=True)
        formset = _formset(
            data, rent_leases=rent_leases, default_lease=draft.lease, existing=existing
        )
        collect_form = _collect_form(request, data, can_schedule=schedulable)
        form.is_valid()
        formset.is_valid()
        collect_form.is_valid()
    else:
        header, lines = _draft_initial(draft)
        form = InvoiceDraftForm(initial=header, leases=leases, allow_none=True)
        formset = _formset(
            initial=lines, rent_leases=rent_leases, default_lease=draft.lease, existing=existing
        )
        collect_form = _collect_form(request, can_schedule=schedulable)
    if form is not None and context is not None:
        _hide_lease(form)
    sent = _sent_context(draft) if draft.status == InvoiceDraftStatus.SENT else {}
    if sent:
        sent.update(_sent_detail(request, draft, sent))
    preview = invoice_builder.preview(draft)
    preview_total = _lines_total(formset) if formset is not None else preview.total
    return render(
        request,
        "ledger/invoices/draft.html",
        {
            **sent,
            "draft": draft,
            "preview": preview,
            "form": form,
            "formset": formset,
            "item_options": _item_options(existing) if editable else [],
            "items_list_id": ITEMS_LIST_ID,
            "lines_total": _lines_total(formset) if formset is not None else None,
            "line_rows": _line_rows(formset) if formset is not None else [],
            "lines_have_rent": formset is not None and _lines_have_rent(formset),
            "rent_amount_url": _rent_amount_url([draft.operator], draft),
            "errors": _error_items(errors, [draft.operator]),
            "editable": editable,
            "store_context": context,
            "context_query": context.query if context is not None else "",
            "crumb_store": draft.lease.store if draft.lease is not None else None,
            "collect_form": collect_form,
            "collect_total": preview_total if editable else None,
            "can_cancel": draft.status == InvoiceDraftStatus.DRAFT
            or (draft.status == InvoiceDraftStatus.SENT and not sent.get("cancel_refusal")),
            "due_days": invoice_builder.DUE_DAYS,
        },
    )


@role_required
@require_POST
def draft_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:edit`` (billing): the draft saved, then collected as How to
    collect says (PLAN-012e-2 D3)."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    draft = _load(pk)
    context = _draft_context(request, draft)
    target = f"invoice-edit:{draft.pk}"
    leases = _operator_leases(draft.operator)
    form = InvoiceDraftForm(request.POST, leases=leases, allow_none=True)
    formset = _formset(
        request.POST,
        rent_leases=_rent_leases([draft.operator], draft),
        default_lease=draft.lease,
        existing=invoice_builder.open_charges(draft.lease),
    )
    collect_form = _collect_form(
        request,
        request.POST,
        can_schedule=invoice_builder.can_schedule(_posted_lease(request.POST, leases, None)),
    )
    if form.is_valid() and formset.is_valid() and collect_form.is_valid():
        outcome = invoice_builder.update_draft(
            draft.pk,
            form.chosen_lease(),
            form.cleaned_data["memo"],
            form.cleaned_data["due_date"],
            draft_lines(formset),
            _actor(request),
        )
        if outcome.ok and outcome.draft is not None:
            return _collected(request, outcome.draft, collect_form, context, outcome.message)
        _hold(request, target, [_renumber(outcome.message, line_numbers(formset))])
    else:
        _hold(request, target, _form_errors(form, formset, collect_form))
    return redirect(_draft_url(draft, context))


@role_required
@require_POST
def draft_send(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:send`` (billing): the draft becomes its invoice and charges,
    e-mailed once the provider has it."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    draft = _load(pk)
    context = _draft_context(request, draft)
    outcome = invoice_builder.send(draft.pk, _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
        if context is not None:
            # PLAN-012d D8: sent from the store page, back to its Invoices tab.
            return redirect(context.tab_url(workspace.TAB_INVOICES))
        # PLAN-012d D14: sent from the operator page (``?next=``), back to it.
        back = store_context.local_next(request.GET.get("next"))
        if back:
            return redirect(back)
    else:
        messages.error(request, outcome.message)
    return redirect(_draft_url(draft, context))


@role_required
@require_POST
def draft_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:invoices:delete_draft`` (billing; PLAN-012d D3): a never-sent draft is
    deleted outright, without a note or a confirmation page; a sent invoice is refused
    (it keeps its cancel form). Back to the store page's Invoices tab when opened from
    it, else to ``?next=`` (a path on this site: the operator page's Invoices tab,
    PLAN-012d D14), else to the operator page's Invoices tab."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    draft = _load(pk)
    context = _draft_context(request, draft)
    outcome = invoice_builder.delete_draft(draft.pk, _actor(request))
    if context is not None:
        back = context.tab_url(workspace.TAB_INVOICES)
    elif outcome.ok:
        back = store_context.local_next(request.GET.get("next")) or (
            workspace.operator_tab_url(draft.operator_id, workspace.TAB_INVOICES)
            + f"#tab-{workspace.TAB_INVOICES}"
        )
    else:
        back = _draft_url(draft, None)
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(back)


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


# --- The rent amount of a line (PLAN-012d D10) ------------------------------------------


def _rent_amount_url(operators: Iterable[Operator], draft: InvoiceDraft | None = None) -> str:
    """The endpoint the Rent line's script reads, bound to the draft (or, for a new
    draft of one operator, to that operator)."""
    url = reverse("ledger:invoices:rent_amount")
    if draft is not None:
        return f"{url}?draft={draft.pk}"
    billed = list(operators)
    return f"{url}?operator={billed[0].pk}" if len(billed) == 1 else url


def _month(text: str) -> date | None:
    match = MONTH.match(text)
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 2000 <= year <= 2100):
        return None
    return date(year, month, 1)


@role_required
@require_safe
def rent_amount(request: HttpRequest) -> HttpResponse:
    """``ledger:invoices:rent_amount`` (billing; read-only JSON): the default amount of a
    Rent line of ``?lease=`` and ``?month=YYYY-MM`` (the rent run's charge's, else the
    month's rent, prorated for a partial first month) with its proration text, or why the
    month cannot be billed (``amount`` null). ``?operator=`` or ``?draft=`` bind the lease:
    404 for a lease of another operator, as for an unknown lease; 400 for a bad month.
    Local data only; no identifier is returned."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    lease = (
        Lease.objects.select_related("store", "operator")
        .filter(pk=_key(request.GET.get("lease", "")))
        .first()
    )
    if lease is None or lease.operator_id is None:
        raise Http404
    wanted = request.GET.get("operator", "")
    if wanted and _key(wanted) != lease.operator_id:
        raise Http404
    draft_key = request.GET.get("draft", "")
    if draft_key:
        draft = InvoiceDraft.objects.filter(pk=_key(draft_key)).only("operator_id").first()
        if draft is None or draft.operator_id != lease.operator_id:
            raise Http404
    month = _month(request.GET.get("month", ""))
    if month is None:
        return JsonResponse({"amount": None, "text": "", "message": BAD_MONTH}, status=400)
    amount, text, message = invoice_builder.rent_amount(lease, month)
    return JsonResponse(
        {
            "amount": f"{amount:.2f}" if amount is not None else None,
            "text": text,
            "message": message,
        }
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
