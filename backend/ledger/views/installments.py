"""Installment plan screens (docs/qa/PLAN-012e-lease-end-transfer-installments.md D10, D11).

"Split into installments" (``ledger:one_off_charges:split``, ``ledger:charges:split``;
billing, GET and POST): the first form (how many, the first date, the cadence) is read
from the query string, and the preview it shows writes nothing; the preview's dates and
amounts may be edited and are posted with the confirm, which
:func:`ledger.services.installments.create_plan` checks again under the parent's row
lock. A refused confirm returns to the preview with the edited values in the query
string and the reason as a message. PLAN-012f-c D13: the page is in the Add payment family's
frame (the Plan card, the preview's table, the sticky Summary with the one primary); the
table is rendered here from the query string (or from the first values when the page opens)
and again by ``ledger/split.js`` from ``ledger:plans:split_preview`` (billing, read-only
JSON: :func:`split_preview`) as the fields change.

The plan page (``ledger:plans:detail``) is read by any role. Reschedule
(``ledger:plans:reschedule``) needs ``billing``, Cancel (``ledger:plans:cancel``, "Stop
this plan?" on screen, PLAN-012f-b D16) needs ``admin``; the registry refuses a POST
below the role first, and these views refuse the GET of their forms below it too. Pause
and Resume (``ledger:plans:pause``, ``ledger:plans:resume``; billing, POST only, D16)
show the service's sentence and go back to the store's Autopay tab. No view calls the
payment provider: the service queues the provider work. Every POST ends in a redirect.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from finance.dates import day_text
from ledger.forms import PlanCancelForm, PlanRescheduleForm, SplitForm
from ledger.models import (
    INSTALLMENTS_MAX,
    INSTALLMENTS_MIN,
    LIVE_PLAN_STATUSES,
    InstallmentCollection,
    InstallmentPlan,
    OneOffCharge,
    RentCharge,
    Store,
)
from ledger.services import installments as service
from ledger.services import leases, workspace
from ledger.services import stores as store_service
from ledger.views import store_context
from ledger.views.operators import _actor, _held, _hold, _role, role_required
from ledger.wording import format_amount

#: The preview's confirmation of a large debit (autopay installments above the threshold).
CONFIRM_FIELD = "confirmed"

#: Split into installments' one line (PLAN-012f-c D13); the rest is on the Payments help topic.
SPLIT_LINE = "Equal installments of what is still owed; the last carries the rounding cent."
#: The Summary's line on a returned installment (the Autopay help's Debit again sentence).
NSF_LINE = (
    "A returned installment is debited again only when staff reschedule it, with the NSF fee "
    "added (one amount on an operator's first return, another afterwards)."
)
#: The preview route's ``?parent=``: ``rent:<pk>`` or ``one_off:<pk>`` (D13).
PARENT_RENT = "rent"
PARENT_ONE_OFF = "one_off"
PARENT_PARAM = re.compile(rf"({PARENT_RENT}|{PARENT_ONE_OFF}):([0-9a-fA-F-]{{32,36}})")
PARENT_MISSING = "Choose the charge to split."
#: An ISO day as the date input sends it.
ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
#: At most this many characters of ``?count=`` are read (the range is the form's).
COUNT_LENGTH = 4


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


def _store_of(parent: service.Parent) -> Store | None:
    """The store of ``parent``'s lease, or ``None`` for a one-off charge with no lease."""
    if isinstance(parent, RentCharge):
        return parent.lease.store
    return parent.lease.store if parent.lease is not None else None


def parent_key(parent: service.Parent) -> str:
    """``rent:<pk>`` or ``one_off:<pk>``: the preview route's ``parent`` (D13)."""
    kind = PARENT_RENT if isinstance(parent, RentCharge) else PARENT_ONE_OFF
    return f"{kind}:{parent.pk}"


def _day(value: object) -> date | None:
    """An ISO day as typed, or ``None``."""
    text = str(value or "")
    if not ISO_DAY.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _split_context(parent: service.Parent, url: str, refusal: str) -> dict[str, Any]:
    """What every state of the page shows (D13): the header (the store's breadcrumb, the
    status badge with the balance, the muted line), the page's one line, the collection and
    the Summary's fixed parts."""
    balance = service.balance_of(parent)
    label = service.parent_screen_label(parent)
    store = _store_of(parent)
    operator = service.operator_of(parent)
    status_label = str(parent.get_status_display())
    line = (
        label,
        store_service.title(store) if store is not None else "",
        operator.display_name if operator is not None else "",
        f"{format_amount(balance)} left to pay",
    )
    collection = service.collection_of(parent)
    return {
        "parent": parent,
        # The screen label, under the key the templates read (PLAN-012f-a B4).
        "parent_label": label,
        "parent_url": service.parent_url(parent),
        "crumb_store": store,
        "operator": operator,
        "status": parent.status,
        # PLAN-012f-c D8's badge: "● Due · $2,100.00".
        "status_badge_label": (
            f"{status_label} · {format_amount(balance)}" if balance > 0 else status_label
        ),
        "header_line": " · ".join(part for part in line if part),
        "balance": balance,
        "collection": InstallmentCollection(collection).label,
        "autopay": collection == InstallmentCollection.AUTOPAY_DUE,
        "large_threshold": settings.LARGE_DEBIT_WARNING_AMOUNT,
        "refusal": refusal,
        # PLAN-012b-a D2: the parent's live provider object the confirm releases.
        "release_notice": "" if refusal else service.release_notice(parent),
        "split_line": SPLIT_LINE,
        "nsf_line": NSF_LINE,
        "preview_help": service.PREVIEW_HELP,
        "preview_url": reverse("ledger:plans:split_preview"),
        "parent_key": parent_key(parent),
        "action": url,
        "rows": [],
        "confirm_field": CONFIRM_FIELD,
    }


def _split_page(request: HttpRequest, parent: service.Parent, url: str) -> HttpResponse:
    """The page (PLAN-012f-c D13): the Plan card's GET form, the preview's table (the
    confirm form; rendered here from the query string, or from the first values when the
    page opens, and again by ``ledger/split.js`` from :func:`split_preview` as the fields
    change) and the Summary, whose primary submits the confirm form."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    if request.method == "POST":
        return _confirm(request, parent, url)
    refusal = service.split_refusal(parent)
    context = _split_context(parent, url, refusal)
    if refusal:
        return render(request, "ledger/installments/split.html", context)
    bound = "count" in request.GET
    form = (
        SplitForm(request.GET) if bound else SplitForm(initial={"first_due": timezone.localdate()})
    )
    plan: tuple[int, date, str] | None = None
    if not bound:
        plan = (int(form["count"].value()), timezone.localdate(), str(form["cadence"].value()))
    elif form.is_valid():
        data = form.cleaned_data
        plan = (data["count"], data["first_due"], data["cadence"])
    first_due = plan[1] if plan is not None else _day(form["first_due"].value())
    context |= {
        "form": form,
        "count_value": form["count"].value(),
        "cadence_value": form["cadence"].value(),
        "cadence_choices": form.fields["cadence"].choices,  # type: ignore[attr-defined]
        "first_due_label": format_html(
            'First installment date <span class="muted" id="first-due-text">{}</span>',
            day_text(first_due) if first_due is not None else "",
        ),
        "count_min": INSTALLMENTS_MIN,
        "count_max": INSTALLMENTS_MAX,
    }
    if plan is None:
        return render(request, "ledger/installments/split.html", context)
    count, first, cadence = plan
    lines, outcome = service.preview(parent, count, first, cadence)
    if outcome is not None:
        if bound:
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        else:
            context["preview_message"] = outcome.message
        return render(request, "ledger/installments/split.html", context)
    payload = service.preview_payload(lines)
    shown = [(line.due.isoformat(), str(line.amount)) for line in lines]
    if f"due_{count}" in request.GET:
        shown = _edited_lines(request.GET, count)[1]
    amounts = [line.amount for line in lines]
    large = bool(context["autopay"]) and bool(leases.large_debit_confirmation(amounts))
    return render(
        request,
        "ledger/installments/split.html",
        {
            **context,
            "rows": [
                {
                    "number": number,
                    "due": due,
                    "amount": amount,
                    "rounding": number == count and payload["cent_on_last"],
                }
                for number, (due, amount) in enumerate(shown, start=1)
            ],
            "count": count,
            "first_due": first.isoformat(),
            "cadence": cadence,
            # The Summary's "3 × $333.33" (D13).
            "installments_text": f"{count} × {format_amount(lines[0].amount)}",
            "last_due": _day(shown[-1][0]) or lines[-1].due,
            "large_debit": large,
        },
    )


@role_required
@require_safe
def split_preview(request: HttpRequest) -> HttpResponse:
    """``ledger:plans:split_preview`` (billing; read-only JSON, PLAN-012f-c D13): Split into
    installments' preview of ``?parent=`` (``rent:<pk>`` or ``one_off:<pk>``), ``?count=``
    (2 to 12), ``?first=`` (an ISO date, today or later) and ``?cadence=`` (the form's
    cadences): :func:`ledger.services.installments.preview_payload`, or 400 with
    ``{"ok": false, "message": ...}`` naming the first rule refused (a charge that cannot
    be split says why). ``?store=`` naming another store than the charge's is a 404, as an
    unknown charge is. Nothing is written."""
    refused = _refuse_below(request, Role.BILLING)
    if refused is not None:
        return refused
    parent = _preview_parent(request.GET.get("parent", ""))
    if parent is None:
        return _preview_refused(PARENT_MISSING)
    store = _store_of(parent)
    if store is None:
        if request.GET.get("store") or request.GET.get("lease"):
            raise Http404
    else:
        store_context.from_request(request, store=store)
    refusal = service.split_refusal(parent)
    if refusal:
        return _preview_refused(refusal)
    # Only an ISO day is read as the first date; anything else is refused as missing.
    first = _day(request.GET.get("first", ""))
    form = SplitForm(
        {
            "count": request.GET.get("count", "")[:COUNT_LENGTH],
            "first_due": first.isoformat() if first is not None else "",
            "cadence": request.GET.get("cadence", ""),
        }
    )
    if not form.is_valid():
        name = next(name for name in ("count", "first_due", "cadence") if name in form.errors)
        return _preview_refused(str(form.errors[name][0]))
    data = form.cleaned_data
    lines, outcome = service.preview(parent, data["count"], data["first_due"], data["cadence"])
    if outcome is not None:
        return _preview_refused(outcome.message)
    return JsonResponse(service.preview_payload(lines))


def _preview_parent(text: str) -> service.Parent | None:
    """The charge ``text`` names (``rent:<pk>``, ``one_off:<pk>``), ``None`` when it names
    none in that form; 404 when no such charge exists."""
    match = PARENT_PARAM.fullmatch(text)
    if match is None:
        return None
    try:
        key = uuid.UUID(match.group(2))
    except ValueError:
        return None
    if match.group(1) == PARENT_RENT:
        return get_object_or_404(
            RentCharge.objects.select_related("lease__store", "lease__operator"), pk=key
        )
    return get_object_or_404(
        OneOffCharge.objects.select_related("operator", "lease__store", "lease__operator"),
        pk=key,
    )


def _preview_refused(message: str) -> JsonResponse:
    return JsonResponse({"ok": False, "message": message}, status=400)


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
