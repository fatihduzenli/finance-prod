"""The end-of-lease wizard's pages (docs/qa/PLAN-012e-lease-end-transfer-installments.md
D4, D5, D7).

Admin only, GET and POST: the registry refuses a POST below admin first, and each view
refuses every method below admin with 403. ``ledger:stores:end_lease`` starts (or
resumes) the draft of the store's current lease; the steps live under
``ledger:lease_ends:*`` keyed by the draft, so a lease is reached only through its own
draft, never by a lease id in the URL. A step whose earlier step is not done redirects
to it; nothing past the blockers step is offered while a blocker is listed.

A lease end that is part of a transfer (PLAN-012e D6) shows the transfer's steps: its
reason is fixed, its live items are the transfer's (``ledger:transfers:blockers``), its
settlement leads to the incoming lease and its confirm is the transfer's; until the
transfer's incoming operator is chosen, its terms step leads there (a draft the transfer
adopted, PLAN-012b-b D9). Showing the statement step writes nothing (its GET and HEAD
show the charges not created yet as preview lines); saving it creates the rent charges
the statement needs, saves the deductions and marks the draft in progress
(:func:`ledger.services.lease_end.save_statement`, PLAN-012f-a D).

Every change goes through :mod:`ledger.services.lease_end`, which audits it; no view
calls the payment provider. Every POST ends in a redirect; a form with errors is held in
the session for the next GET.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.contrib import messages
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from documents_storage import StorageUnavailable
from ledger.forms import (
    LeaseEndConfirmForm,
    LeaseEndDeductionsForm,
    LeaseEndSettlementForm,
    LeaseEndTermsForm,
)
from ledger.models import LeaseEnd, LeaseEndReason, LeaseEndStatus, LeaseTransfer, Store
from ledger.services import documents, statements, transfer
from ledger.services import lease_end as service
from ledger.views import wizard_frame
from ledger.views.operators import _actor, _held, _hold, _role, role_required
from ledger.views.transfers import OPERATOR_FIRST, transfer_steps
from ledger.wording import provider_sentence
from payments import jobs
from payments.models import JobKind

STEPS: tuple[tuple[str, str], ...] = (
    ("terms", "End date and reason"),
    ("blockers", provider_sentence("Still open at {provider}")),
    ("statement", "Statement and deductions"),
    ("settlement", "Settlement"),
    ("confirm", "Confirm"),
)
PDF_NOT_READY = "The statement PDF is not stored yet; this page shows when it is."
PDF_UNAVAILABLE = documents.READ_FAILED_MESSAGE


def _admin(request: HttpRequest) -> HttpResponse | None:
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    return None


def _load(pk: str) -> LeaseEnd:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        LeaseEnd.objects.select_related("lease", "lease__store", "lease__operator", "document"),
        pk=key,
    )


def _store(pk: str) -> Store:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(Store, pk=key)


def _steps(end: LeaseEnd, current: str) -> list[tuple[str, str, bool]]:
    """(label, link or "", current) per step: a later step is linked once the terms are
    set and while the end is a draft."""
    open_steps = end.status == LeaseEndStatus.DRAFT and service.terms_set(end)
    return [
        (
            label,
            reverse(f"ledger:lease_ends:{name}", kwargs={"pk": end.pk})
            if name != current and (open_steps or name == "terms")
            else "",
            name == current,
        )
        for name, label in STEPS
    ]


def _context(end: LeaseEnd, current: str, **extra: Any) -> dict[str, Any]:
    held_by = service.draft_transfer(end) if end.status == LeaseEndStatus.DRAFT else None
    steps = transfer_steps(held_by, current) if held_by is not None else _steps(end, current)
    figures = extra.get("figures")
    return {
        "end": end,
        "lease": end.lease,
        "store": end.lease.store,
        "transfer": held_by,
        "step_links": steps,
        # PLAN-012f-a D8: the frame's step count, Back link and title; the tiles' notes.
        **wizard_frame.frame(steps, end.lease.store),
        **(wizard_frame.statement_notes(figures) if figures is not None else {}),
        **extra,
    }


def _draft_only(end: LeaseEnd) -> HttpResponse | None:
    if end.status != LeaseEndStatus.DRAFT:
        return redirect("ledger:lease_ends:confirm", pk=end.pk)
    return None


def _needs_terms(end: LeaseEnd) -> HttpResponse | None:
    if not service.terms_set(end):
        return redirect("ledger:lease_ends:terms", pk=end.pk)
    return None


def _needs_statement(request: HttpRequest, end: LeaseEnd) -> HttpResponse | None:
    """PLAN-012f-a §10: confirm needs the statement step saved in this wizard."""
    if end.statement_saved_at is None:
        return _statement_first(request, end)
    return None


def _statement_first(request: HttpRequest, end: LeaseEnd) -> HttpResponse:
    messages.error(request, service.STATEMENT_REQUIRED)
    return redirect("ledger:lease_ends:statement", pk=end.pk)


def _needs_clear(request: HttpRequest, end: LeaseEnd) -> HttpResponse | None:
    held_by = service.draft_transfer(end)
    if held_by is not None:
        if transfer.blockers(held_by):
            messages.error(request, "Clear the listed items first.")
            return redirect("ledger:transfers:blockers", pk=held_by.pk)
        return None
    if service.blockers(end.lease):
        messages.error(request, "Clear the listed items first.")
        return redirect("ledger:lease_ends:blockers", pk=end.pk)
    return None


def _transfer_of(end: LeaseEnd) -> LeaseTransfer | None:
    return service.draft_transfer(end) if end.status == LeaseEndStatus.DRAFT else None


# --- Start (the store page) ----------------------------------------------------------------


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def store_end_lease(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:stores:end_lease``: start or resume the draft of the store's current
    lease."""
    refused = _admin(request)
    if refused is not None:
        return refused
    store = _store(pk)
    lease = service.current_lease(store)
    draft = (
        LeaseEnd.objects.filter(lease__store=store, status=LeaseEndStatus.DRAFT)
        .select_related("lease")
        .first()
    )
    if draft is not None and service.draft_transfer(draft) is not None:
        return redirect("ledger:stores:transfer", pk=store.pk)
    if request.method == "POST":
        target = draft.lease if draft is not None else lease
        if target is None:
            messages.error(request, service.NOT_CURRENT)
            return redirect("ledger:stores:detail", pk=store.pk)
        try:
            end = service.start(target, _actor(request))
        except ValueError as refusal:
            messages.error(request, str(refusal))
            return redirect("ledger:stores:detail", pk=store.pk)
        return redirect("ledger:lease_ends:terms", pk=end.pk)
    return render(
        request,
        "ledger/lease_ends/start.html",
        {"store": store, "lease": draft.lease if draft is not None else lease, "draft": draft},
    )


# --- Steps -----------------------------------------------------------------------------------


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def terms(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin(request)
    if refused is not None:
        return refused
    end = _load(pk)
    done = _draft_only(end)
    if done is not None:
        return done
    target = f"lease-end-terms:{end.pk}"
    held_by = _transfer_of(end)
    if held_by is not None and not transfer.operator_set(held_by):
        # PLAN-012b-b D9: a draft adopted by a transfer continues on the transfer's
        # steps, the incoming operator first.
        messages.info(request, OPERATOR_FIRST)
        return redirect("ledger:transfers:operator", pk=held_by.pk)
    fixed = LeaseEndReason.TRANSFER if held_by is not None else ""
    if request.method == "POST":
        form = LeaseEndTermsForm(request.POST, fixed_reason=fixed)
        if form.is_valid():
            data = form.cleaned_data
            outcome = service.set_terms(
                end,
                data["ended_on"],
                data["reason"],
                data["prorate_last_month"] is True,
                note=data["reason_note"],
                actor=_actor(request),
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                if held_by is not None:
                    return redirect("ledger:transfers:blockers", pk=held_by.pk)
                return redirect("ledger:lease_ends:blockers", pk=end.pk)
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:lease_ends:terms", pk=end.pk)
    initial = {
        "ended_on": end.ended_on or timezone.localdate(),
        "reason": end.reason,
        "reason_note": end.reason_note,
        "prorate_last_month": end.prorate_last_month,
    }
    form = _held(
        request, target, lambda data: LeaseEndTermsForm(data, fixed_reason=fixed)
    ) or LeaseEndTermsForm(initial=initial, fixed_reason=fixed)
    return render(request, "ledger/lease_ends/terms.html", _context(end, "terms", form=form))


@role_required
@require_safe
def blockers(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin(request)
    if refused is not None:
        return refused
    end = _load(pk)
    done = _draft_only(end) or _needs_terms(end)
    if done is not None:
        return done
    held_by = _transfer_of(end)
    if held_by is not None:
        return redirect("ledger:transfers:blockers", pk=held_by.pk)
    found = service.blockers(end.lease)
    return render(
        request, "ledger/lease_ends/blockers.html", _context(end, "blockers", blockers=found)
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def statement(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin(request)
    if refused is not None:
        return refused
    end = _load(pk)
    done = _draft_only(end) or _needs_terms(end) or _needs_clear(request, end)
    if done is not None:
        return done
    target = f"lease-end-deductions:{end.pk}"
    if request.method == "POST":
        form = LeaseEndDeductionsForm(request.POST)
        if form.is_valid():
            lines = [
                service.DeductionInput(kind, description, amount)
                for kind, description, amount in form.lines()
            ]
            outcome = service.save_statement(end, lines, actor=_actor(request))
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:lease_ends:settlement", pk=end.pk)
            form.add_error(None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:lease_ends:statement", pk=end.pk)
    # PLAN-012f-a D: GET and HEAD write nothing; the charges not created yet are preview
    # lines of the figures until the step is saved.
    saved = list(end.deductions.all())
    shown = (
        [service.DeductionInput(row.kind, row.description, row.amount) for row in saved]
        if saved
        else service.default_deductions(end)
    )
    initial: dict[str, Any] = {}
    for index, item in enumerate(shown, start=1):
        initial[f"kind_{index}"] = item.kind
        initial[f"description_{index}"] = item.description
        initial[f"amount_{index}"] = item.amount
    form = _held(request, target, LeaseEndDeductionsForm) or LeaseEndDeductionsForm(initial=initial)
    return render(
        request,
        "ledger/lease_ends/statement.html",
        _context(end, "statement", form=form, figures=statements.final(end)),
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def settlement(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin(request)
    if refused is not None:
        return refused
    end = _load(pk)
    done = _draft_only(end) or _needs_terms(end) or _needs_clear(request, end)
    if done is not None:
        return done
    target = f"lease-end-settlement:{end.pk}"
    if request.method == "POST":
        form = LeaseEndSettlementForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            outcome = service.set_settlement(
                end,
                data["method"],
                data["refund_on"],
                data["reference"],
                data["note"],
                actor=_actor(request),
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                held_by = _transfer_of(end)
                if held_by is not None:
                    return redirect("ledger:transfers:incoming", pk=held_by.pk)
                return redirect("ledger:lease_ends:confirm", pk=end.pk)
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:lease_ends:settlement", pk=end.pk)
    form = _held(request, target, LeaseEndSettlementForm) or LeaseEndSettlementForm(
        initial={
            "method": end.refund_method,
            "refund_on": end.refund_on or timezone.localdate(),
            "reference": end.refund_reference,
            "note": end.settlement_note,
        }
    )
    figures = statements.final(end)
    return render(
        request,
        "ledger/lease_ends/settlement.html",
        # PLAN-012c D8: the refund's method, date and reference only when a refund is due;
        # the form and the service keep every rule for them whatever the page shows.
        _context(end, "settlement", form=form, figures=figures, refund=figures.refund_due > 0),
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def confirm(request: HttpRequest, pk: str) -> HttpResponse:
    """The "are you sure" page and the confirm; once confirmed, the lease end's page."""
    refused = _admin(request)
    if refused is not None:
        return refused
    end = _load(pk)
    if end.status == LeaseEndStatus.CONFIRMED:
        if request.method == "POST":
            messages.info(request, service.NOT_DRAFT)
            return redirect("ledger:lease_ends:confirm", pk=end.pk)
        job = jobs.latest_for(end, [JobKind.LEASE_END_DOCUMENTS])
        return render(
            request,
            "ledger/lease_ends/confirmed.html",
            _context(
                end,
                "confirm",
                figures=statements.from_json(end.statement_json),
                job_text=jobs.describe(job) if job is not None else "",
                log=service.statement_log(end),
            ),
        )
    held_by = _transfer_of(end)
    if held_by is not None:
        return redirect("ledger:transfers:confirm", pk=held_by.pk)
    done = _needs_terms(end) or _needs_clear(request, end) or _needs_statement(request, end)
    if done is not None:
        return done
    target = f"lease-end-confirm:{end.pk}"
    if request.method == "POST":
        form = LeaseEndConfirmForm(request.POST)
        if form.is_valid():
            outcome = service.confirm(end, _actor(request), confirmed=True)
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:lease_ends:confirm", pk=end.pk)
            if outcome.field == "statement":
                return _statement_first(request, end)
            form.add_error(None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:lease_ends:confirm", pk=end.pk)
    form = _held(request, target, LeaseEndConfirmForm) or LeaseEndConfirmForm()
    figures = statements.final(end)
    return render(
        request,
        "ledger/lease_ends/confirm.html",
        _context(
            end,
            "confirm",
            form=form,
            figures=figures,
            settlement_missing=figures.refund_due > 0 and not end.refund_method,
        ),
    )


@role_required
@require_POST
def cancel(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin(request)
    if refused is not None:
        return refused
    end = _load(pk)
    store = end.lease.store
    outcome = service.cancel(end, _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
        return redirect("ledger:stores:detail", pk=store.pk)
    messages.error(request, outcome.message)
    return redirect("ledger:lease_ends:confirm", pk=end.pk)


@role_required
@require_safe
def pdf(request: HttpRequest, pk: str) -> HttpResponseBase:
    """The stored statement PDF, as a download (audited as the document's download)."""
    refused = _admin(request)
    if refused is not None:
        return refused
    end = _load(pk)
    if end.document is None:
        messages.info(request, PDF_NOT_READY)
        return redirect("ledger:lease_ends:confirm", pk=end.pk)
    try:
        opened = documents.open_for(end.document, _actor(request), inline=False)
    except documents.DocumentHidden:
        raise Http404 from None
    except StorageUnavailable:
        messages.error(request, PDF_UNAVAILABLE)
        return redirect("ledger:lease_ends:confirm", pk=end.pk)
    response = StreamingHttpResponse(opened.chunks, content_type=opened.content_type)
    for name, value in opened.headers.items():
        response[name] = value
    return response
