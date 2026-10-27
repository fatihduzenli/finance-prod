"""Installment plan screens (docs/qa/PLAN-012e-lease-end-transfer-installments.md D10, D11).

"Split into installments" (``ledger:one_off_charges:split``, ``ledger:charges:split``;
billing, GET and POST): the first form (how many, the first date, the cadence) is read
from the query string, and the preview it shows writes nothing; the preview's dates and
amounts may be edited and are posted with the confirm, which
:func:`ledger.services.installments.create_plan` checks again under the parent's row
lock. A refused confirm returns to the preview with the edited values in the query
string and the reason as a message.

The plan page (``ledger:plans:detail``) is read by any role. Reschedule
(``ledger:plans:reschedule``) needs ``billing``, Cancel (``ledger:plans:cancel``, "Stop
this plan?" on screen, PLAN-012f-b D16) needs ``admin``; the registry refuses a POST
below the role first, and these views refuse the GET of their forms below it too. Pause
and Resume (``ledger:plans:pause``, ``ledger:plans:resume``; billing, POST only, D16)
show the service's sentence and go back to the store's Autopay tab. No view calls the
payment provider: the service queues the provider work. Every POST ends in a redirect.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.forms import PlanCancelForm, PlanRescheduleForm, SplitForm
from ledger.models import (
    INSTALLMENTS_MAX,
    LIVE_PLAN_STATUSES,
    InstallmentCollection,
    InstallmentPlan,
    OneOffCharge,
    RentCharge,
)
from ledger.services import installments as service
from ledger.services import leases, workspace
from ledger.views.operators import _actor, _held, _hold, _role, role_required

#: The preview's confirmation of a large debit (autopay installments above the threshold).
CONFIRM_FIELD = "confirmed"


def _key(pk: str) -> uuid.UUID:
    try:
        return uuid.UUID(pk)
    except ValueError:
        raise Http404 from None


def _refuse_below(request: HttpRequest, role: Role) -> HttpResponse | None:
    if not meets(_role(request), role):
        return forbidden(request)
    return None


def _load_plan(pk: str) -> InstallmentPlan:
    return get_object_or_404(
        InstallmentPlan.objects.select_related(
            "rent_charge__lease__store",
            "one_off_charge__fee_type",
            "lease__store",
            "operator",
            "created_by",
        ),
        pk=_key(pk),
    )


# --- The parent's pages ------------------------------------------------------------------


def parent_context(
    parent: service.Parent, *, billing: bool, split_url: str, admin: bool = False
) -> dict[str, Any]:
    """What a parent's page shows of installments (D9, D10): its plan's progress and
    installments, the actions on the plan the role may take (PLAN-012f-b D16), and the
    Split button or why it is not offered (billing only)."""
    plan = service.plan_of(parent)
    refusal = service.split_refusal(parent) if billing else ""
    return {
        "plan_progress": service.progress(plan) if plan is not None else None,
        "plan_live": plan is not None and plan.status in LIVE_PLAN_STATUSES,
        "plan_actions": (
            service.plan_actions(plan, billing=billing, admin=admin) if plan is not None else {}
        ),
        "can_split": billing and not refusal,
        "split_refusal": refusal if billing and plan is None else "",
        "split_url": split_url,
        "split_help": service.SPLIT_HELP,
    }


# --- Split (D10) ---------------------------------------------------------------------------


def _edited_lines(data: Any, count: int) -> tuple[list[service.Line] | None, list[tuple[str, str]]]:
    """The lines posted (or carried in the query string) as ``due_<n>`` and
    ``amount_<n>``; ``None`` when one cannot be read. Also the raw values, to show again."""
    raw = [
        (str(data.get(f"due_{number}", "")), str(data.get(f"amount_{number}", "")))
        for number in range(1, count + 1)
    ]
    lines = []
    for due, amount in raw:
        try:
            lines.append(service.Line(date.fromisoformat(due), Decimal(amount.strip())))
        except (ValueError, InvalidOperation):
            return None, raw
    return lines, raw


def _split_page(request: HttpRequest, parent: service.Parent, url: str) -> HttpResponse:
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    if request.method == "POST":
        return _confirm(request, parent, url)
    refusal = service.split_refusal(parent)
    context: dict[str, Any] = {
        "parent": parent,
        # The screen label, under the key the templates read (PLAN-012f-a B4).
        "parent_label": service.parent_screen_label(parent),
        "parent_url": service.parent_url(parent),
        "balance": service.balance_of(parent),
        "collection": InstallmentCollection(service.collection_of(parent)).label,
        "refusal": refusal,
        # PLAN-012b-a D2: the parent's live provider object the confirm releases.
        "release_notice": "" if refusal else service.release_notice(parent),
        "split_help": service.SPLIT_HELP,
        "preview_help": service.PREVIEW_HELP,
        "action": url,
        "lines": [],
    }
    if refusal:
        return render(request, "ledger/installments/split.html", context)
    if "count" not in request.GET:
        form = SplitForm(initial={"first_due": timezone.localdate()})
        return render(request, "ledger/installments/split.html", {**context, "form": form})
    form = SplitForm(request.GET)
    if not form.is_valid():
        return render(request, "ledger/installments/split.html", {**context, "form": form})
    count = form.cleaned_data["count"]
    lines, outcome = service.preview(
        parent, count, form.cleaned_data["first_due"], form.cleaned_data["cadence"]
    )
    if outcome is not None:
        form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        return render(request, "ledger/installments/split.html", {**context, "form": form})
    shown = [(line.due.isoformat(), str(line.amount)) for line in lines]
    if f"due_{count}" in request.GET:
        shown = _edited_lines(request.GET, count)[1]
    amounts = [line.amount for line in lines]
    large = service.collection_of(parent) == InstallmentCollection.AUTOPAY_DUE and bool(
        leases.large_debit_confirmation(amounts)
    )
    return render(
        request,
        "ledger/installments/split.html",
        {
            **context,
            "form": form,
            "lines": [(number, due, amount) for number, (due, amount) in enumerate(shown, start=1)],
            "count": count,
            "first_due": form.cleaned_data["first_due"].isoformat(),
            "cadence": form.cleaned_data["cadence"],
            "large_debit": large,
            "confirm_field": CONFIRM_FIELD,
        },
    )


def _confirm(request: HttpRequest, parent: service.Parent, url: str) -> HttpResponse:
    form = SplitForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(str(error) for error in form.errors.values()))
        return redirect(url)
    count = form.cleaned_data["count"]
    lines, raw = _edited_lines(request.POST, count)
    back = {
        "count": str(count),
        "first_due": form.cleaned_data["first_due"].isoformat(),
        "cadence": form.cleaned_data["cadence"],
    }
    for number, (due, amount) in enumerate(raw[:INSTALLMENTS_MAX], start=1):
        back[f"due_{number}"] = due[:10]
        back[f"amount_{number}"] = amount[:10]
    if lines is None:
        messages.error(request, service.LINES_COUNT)
        return redirect(f"{url}?{urlencode(back)}")
    outcome = service.create_plan(
        parent,
        count,
        form.cleaned_data["first_due"],
        form.cleaned_data["cadence"],
        lines,
        _actor(request),
        confirmed=request.POST.get(CONFIRM_FIELD) == "on",
    )
    if not outcome.ok or outcome.plan is None:
        messages.error(request, outcome.message)
        return redirect(f"{url}?{urlencode(back)}")
    messages.success(request, outcome.message)
    return redirect("ledger:plans:detail", pk=outcome.plan.pk)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def one_off_split(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:one_off_charges:split`` (billing; D10)."""
    charge = get_object_or_404(
        OneOffCharge.objects.select_related("operator", "lease__store", "lease__operator"),
        pk=_key(pk),
    )
    url = reverse("ledger:one_off_charges:split", kwargs={"pk": charge.pk})
    return _split_page(request, charge, url)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def charge_split(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:charges:split`` (billing; D10)."""
    charge = get_object_or_404(
        RentCharge.objects.select_related("lease__store", "lease__operator"), pk=_key(pk)
    )
    url = reverse("ledger:charges:split", kwargs={"pk": charge.pk})
    return _split_page(request, charge, url)


# --- The plan (D11) --------------------------------------------------------------------------


@role_required
@require_safe
def plan_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:plans:detail`` (any role)."""
    plan = _load_plan(pk)
    return render(request, "ledger/installments/plan.html", _plan_context(request, plan))


def _plan_context(request: HttpRequest, plan: InstallmentPlan) -> dict[str, Any]:
    """The plan page's context (read only); Stop shows over it in its dialog
    (PLAN-012f-b E-4)."""
    live = plan.status in LIVE_PLAN_STATUSES
    return {
        "plan": plan,
        # PLAN-012d D5: "← Store NNNN".
        "crumb_store": plan.lease.store if plan.lease is not None else None,
        "progress": service.progress(plan),
        # The screen label, under the key the templates read (PLAN-012f-a B4).
        "parent_label": service.parent_screen_label(plan.parent),
        "parent_url": service.parent_url(plan.parent),
        "can_reschedule": live and meets(_role(request), Role.BILLING),
        "can_cancel": live and meets(_role(request), Role.ADMIN),
        # PLAN-012f-b D16: "Installment plan" or "Settlement plan".
        "plan_name": service.plan_name(plan.origin),
        "plan_help": service.PLAN_HELP,
        # PLAN-012b-a D3: the release of the parent's provider object waits or failed.
        "release_pending": service.release_pending(plan),
        # PLAN-012f-b D3-1: a debit Pause could not stop.
        "debits_out": service.debits_out_while_paused(plan),
    }


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def plan_reschedule(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:plans:reschedule`` (billing; D11)."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    plan = _load_plan(pk)
    target = f"plan-reschedule:{plan.pk}"
    if request.method == "POST":
        form = PlanRescheduleForm(request.POST)
        if form.is_valid():
            outcome = service.reschedule(
                plan,
                form.cleaned_data["first_due"],
                form.cleaned_data["cadence"],
                _actor(request),
            )
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:plans:detail", pk=plan.pk)
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:plans:reschedule", pk=plan.pk)
    form = _held(request, target, PlanRescheduleForm) or PlanRescheduleForm(
        initial={"cadence": plan.cadence}
    )
    return render(
        request,
        "ledger/installments/reschedule.html",
        {
            "plan": plan,
            "form": form,
            # The screen label, under the key the templates read (PLAN-012f-a B4).
            "parent_label": service.parent_screen_label(plan.parent),
            "help": service.RESCHEDULE_HELP,
        },
    )


def _to_autopay_tab(
    request: HttpRequest, plan: InstallmentPlan, outcome: service.Outcome
) -> HttpResponse:
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    if plan.lease is None:
        return redirect("ledger:plans:detail", pk=plan.pk)
    return redirect(workspace.tab_url(plan.lease.store_id, workspace.TAB_SCHEDULES))


@role_required
@require_POST
def plan_pause(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:plans:pause`` (billing; PLAN-012f-b D16): the next debit's schedule is
    released and the plan is paused."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    plan = _load_plan(pk)
    return _to_autopay_tab(request, plan, service.pause(plan, _actor(request)))


@role_required
@require_POST
def plan_resume(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:plans:resume`` (billing; PLAN-012f-b D16): the unpaid debits are set up
    again and the plan is active."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    plan = _load_plan(pk)
    return _to_autopay_tab(request, plan, service.resume(plan, _actor(request)))


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def plan_cancel(request: HttpRequest, pk: str) -> HttpResponse:
    """``ledger:plans:cancel`` (admin; D11)."""
    refused = _refuse_below(request, Role.ADMIN)
    if refused is not None:
        return refused
    plan = _load_plan(pk)
    target = f"plan-cancel:{plan.pk}"
    if request.method == "POST":
        form = PlanCancelForm(request.POST)
        if form.is_valid():
            outcome = service.cancel(plan, form.cleaned_data["note"], _actor(request))
            if outcome.ok:
                messages.success(request, outcome.message)
                return redirect("ledger:plans:detail", pk=plan.pk)
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        _hold(request, target, form)
        return redirect("ledger:plans:cancel", pk=plan.pk)
    form = _held(request, target, PlanCancelForm) or PlanCancelForm()
    return render(
        request,
        "ledger/installments/cancel.html",
        {
            **_plan_context(request, plan),
            "plan": plan,
            "form": form,
            # The screen label, under the key the templates read (PLAN-012f-a B4).
            "parent_label": service.parent_screen_label(plan.parent),
            "plan_name": service.plan_name(plan.origin),
            "help": service.CANCEL_HELP,
        },
    )
