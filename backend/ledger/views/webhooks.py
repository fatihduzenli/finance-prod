"""Webhook event screens: list, detail and replay (docs/qa/PLAN-008-webhooks.md D7, D8).

Any role may read; replay needs ``billing`` (``accounts.registry`` and again here).
Provider object IDs are masked to their first and last four characters, in the list,
on the detail page and inside the payload table. The payload shown is the redacted
copy the receiver stored; nothing here reads the provider. A replay is queued as a
``webhook_replay`` job for the worker (PLAN-010 D3); the detail page shows its state.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_safe

from accounts.roles import Role, meets
from ledger.views.jobs import busy, job_context, submit
from ledger.views.operators import PAGE_SIZE, _role, role_required
from ledger.views.run import _billing_only
from payments.models import JobKind, WebhookEvent, WebhookOutcome
from payments.provider import mask_identifier

#: Most payload rows rendered; the rest are counted.
MAX_PAYLOAD_ROWS = 500
_EVENT_TYPE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


def _load(pk: str) -> WebhookEvent:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(WebhookEvent, pk=key)


def _is_identifier_key(path: str) -> bool:
    last = path.rsplit(".", 1)[-1]
    return last == "id" or last.endswith("_id")


def _display(path: str, value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return mask_identifier(text) if _is_identifier_key(path) else text


def payload_rows(value: object, path: str = "") -> list[tuple[str, str]]:
    """The payload flattened to (path, shown value) rows, identifiers masked."""
    if isinstance(value, Mapping):
        if not value:
            return [(path, "{}")]
        rows: list[tuple[str, str]] = []
        for key, item in value.items():
            rows += payload_rows(item, f"{path}.{key}" if path else str(key))
        return rows
    if isinstance(value, list):
        if not value:
            return [(path, "[]")]
        rows = []
        for index, item in enumerate(value):
            rows += payload_rows(item, f"{path}[{index}]")
        return rows
    return [(path, _display(path, value))]


def _outcome_filter(outcome: str) -> Q:
    """An exact outcome, or one of its reasons (``ignored: card``)."""
    return Q(outcome=outcome) | Q(outcome__startswith=f"{outcome}:")


@role_required
@require_safe
def webhook_list(request: HttpRequest) -> HttpResponse:
    outcome = request.GET.get("outcome", "")
    if outcome not in WebhookOutcome.values:
        outcome = ""
    event_type = request.GET.get("type", "")
    if not _EVENT_TYPE.fullmatch(event_type):
        event_type = ""
    events = WebhookEvent.objects.defer("payload").order_by("-received_at", "-id")
    if outcome:
        events = events.filter(_outcome_filter(outcome))
    if event_type:
        events = events.filter(event_type=event_type)
    page = Paginator(events, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [(event, mask_identifier(event.provider_object_id)) for event in page]
    return render(
        request,
        "ledger/webhooks/list.html",
        {
            "page": page,
            "rows": rows,
            "outcome": outcome,
            "event_type": event_type,
            "outcomes": WebhookOutcome.choices,
            "event_types": WebhookEvent.objects.order_by("event_type")
            .values_list("event_type", flat=True)
            .distinct(),
        },
    )


@role_required
@require_safe
def webhook_detail(request: HttpRequest, pk: str) -> HttpResponse:
    event = _load(pk)
    rows = payload_rows(event.payload) if event.payload is not None else []
    return render(
        request,
        "ledger/webhooks/detail.html",
        {
            **job_context(event),
            "event": event,
            "object_id_masked": mask_identifier(event.provider_object_id),
            "payload_rows": rows[:MAX_PAYLOAD_ROWS],
            "payload_rows_hidden": max(len(rows) - MAX_PAYLOAD_ROWS, 0),
            "can_replay": meets(_role(request), Role.BILLING),
        },
    )


@role_required
@require_POST
def webhook_replay(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    event = _load(pk)
    if not busy(request, event):
        submit(request, JobKind.WEBHOOK_REPLAY, event)
    return redirect("ledger:webhooks:detail", pk=event.pk)
