"""Admin › Outbox (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md D5).

- ``ledger:outbox:page`` (admin, read-only in the registry; the view refuses below admin):
  every message, newest first, 50 a page, with its time, store, operator, template, rule,
  status (and held reason), address and the time the send window lets it go; filters by
  status and by a range of days queued (the list pages' filters component). Each row opens
  its stored subject and body. The address is shown in full: the page is admin's only.
- ``ledger:outbox:send_now`` (admin, POST): "Send now" on a held message
  (:func:`ledger.services.messaging.send_now`); the outcome is a message on the page.
"""

from __future__ import annotations

import uuid
from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

import mail
from finance.dates import day_text
from ledger.models import MessageStatus
from ledger.services import list_filters, messaging
from ledger.views.leases import _admin_only
from ledger.views.operators import _actor, role_required

PAGE_SIZE = 50
#: The days a range may name (anything else is ignored, as an unknown filter value is).
FIRST_YEAR = 2000
LAST_YEAR = 2100


def _day(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        found = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return found if FIRST_YEAR <= found.year <= LAST_YEAR else None


def _day_choice(day: date | None) -> dict[str, str]:
    return {day.isoformat(): day_text(day)} if day is not None else {}


@role_required
@require_safe
def outbox_page(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    raw_status = request.GET.get("status", "")
    status = raw_status if raw_status in MessageStatus.values else ""
    since, until = _day(request.GET.get("from")), _day(request.GET.get("to"))
    found = messaging.outbox(status=status, since=since, until=until)
    page = Paginator(found, PAGE_SIZE).get_page(request.GET.get("page"))
    accepted = {
        "status": status,
        "from": since.isoformat() if since is not None else "",
        "to": until.isoformat() if until is not None else "",
    }
    return render(
        request,
        "ledger/outbox/page.html",
        {
            "active_filters": list_filters.active(
                request,
                {
                    "status": ("Status", dict(MessageStatus.choices)),
                    "from": ("From", _day_choice(since)),
                    "to": ("To", _day_choice(until)),
                },
                accepted=accepted,
            ),
            "page": page,
            "query": urlencode({name: value for name, value in accepted.items() if value}),
            "rows": [(message, messaging.status_text(message)) for message in page],
            "status_choices": MessageStatus.choices,
            "selected": {"status": status, "from": since, "to": until},
            "mail_set_up": mail.is_set_up(),
        },
    )


@role_required
@require_POST
def send_now(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    try:
        message_id = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    outcome = messaging.send_now(message_id, _actor(request))
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:outbox:page")
