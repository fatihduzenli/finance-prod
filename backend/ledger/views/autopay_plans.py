"""The Autopay tab's actions on an autopay plan ("Repeating debits", docs/qa/PLAN-012f-a
D14): Pause and Resume (billing), Stop and Change end (admin), each a POST under
``/stores/<pk>/plans/<plan>/…``; Stop is final and removes the pending debit's provider
schedule, so its GET is a confirmation page first (admin).

Access: a signed-in, verified user with a role (middleware and
:func:`~ledger.views.operators.role_required`); every POST is checked again by
``accounts.registry`` and here, and :mod:`ledger.services.autopay_plans` checks the role
once more. The plan must belong to the store in the URL, else 404 and nothing changes.
Each view calls the service, shows its sentence and goes back to the store's Autopay tab;
the service writes the audit rows.
"""

from __future__ import annotations

import uuid

from django import forms
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.roles import Role, meets
from accounts.views import forbidden
from ledger.models import LIVE_AUTOPAY_STATES, AutopayEndRule, AutopayPlan
from ledger.services import autopay_plans, workspace
from ledger.views.operators import _actor, _role, role_required
from ledger.views.stores import _detail_context as _store_page
from ledger.views.stores import _load

#: The Change end form's words when a value does not parse.
BAD_END = "Check the end of the plan. Nothing was changed."


class PlanEndForm(forms.Form):
    """Change end: the rule and its count or date. The service checks the rule's
    bounds (the count's range, the date not before the first debit)."""

    end_rule = forms.ChoiceField(choices=AutopayEndRule.choices)
    end_count = forms.IntegerField(required=False)
    end_on = forms.DateField(required=False)


def _plan_of(pk: str, plan: str) -> AutopayPlan:
    """The plan named in the URL, of the store named in the URL; 404 otherwise."""
    store = _load(pk)
    try:
        key = uuid.UUID(plan)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(AutopayPlan, pk=key, store=store)


def _done(request: HttpRequest, plan: AutopayPlan, outcome: autopay_plans.Outcome) -> HttpResponse:
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(workspace.tab_url(plan.store_id, workspace.TAB_SCHEDULES))


@role_required
@require_POST
def plan_pause(request: HttpRequest, pk: str, plan: str) -> HttpResponse:
    """``ledger:stores:plan_pause`` (billing): the plan stops making debits; its pending
    debit is cancelled."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    found = _plan_of(pk, plan)
    return _done(request, found, autopay_plans.pause(found, _actor(request)))


@role_required
@require_POST
def plan_resume(request: HttpRequest, pk: str, plan: str) -> HttpResponse:
    """``ledger:stores:plan_resume`` (billing): a paused plan makes debits again from the
    first date of its series after today."""
    if not meets(_role(request), Role.BILLING):
        return forbidden(request)
    found = _plan_of(pk, plan)
    return _done(request, found, autopay_plans.resume(found, _actor(request)))


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def plan_stop(request: HttpRequest, pk: str, plan: str) -> HttpResponse:
    """``ledger:stores:plan_stop`` (admin): GET asks first (the plan, what will happen,
    "cannot be undone"; a provider write is a deliberate step, docs/CLAUDE.md Rule 2);
    POST ends the plan for good, its pending debit cancelled."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    found = _plan_of(pk, plan)
    if request.method == "POST":
        outcome = autopay_plans.stop(found, _actor(request))
        if not outcome.ok:
            messages.error(request, outcome.message)
            return redirect("ledger:stores:plan_stop", pk=found.store_id, plan=found.pk)
        return _done(request, found, outcome)
    live = found.state in LIVE_AUTOPAY_STATES
    # PLAN-012f-b E-4: the question shows in its dialog over the store's Autopay tab.
    context = _store_page(request, found.store, forced_tab=workspace.TAB_SCHEDULES)
    context.update(
        {
            "stop_plan": found,
            "stop_line": workspace.plan_line(found),
            "stop_refusal": "" if live else autopay_plans.NOT_LIVE,
        }
    )
    return render(request, "ledger/stores/plan_stop.html", context)


@role_required
@require_POST
def plan_end(request: HttpRequest, pk: str, plan: str) -> HttpResponse:
    """``ledger:stores:plan_end`` (admin): the plan's end rule rewritten; a rule already
    met finishes the plan."""
    if not meets(_role(request), Role.ADMIN):
        return forbidden(request)
    found = _plan_of(pk, plan)
    form = PlanEndForm(request.POST)
    if not form.is_valid():
        return _done(request, found, autopay_plans.Outcome(False, BAD_END))
    data = form.cleaned_data
    outcome = autopay_plans.change_end(
        found,
        end_rule=data["end_rule"],
        end_count=data.get("end_count"),
        end_on=data.get("end_on"),
        actor=_actor(request),
    )
    return _done(request, found, outcome)
