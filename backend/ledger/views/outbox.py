"""Admin › Outbox (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md D5).

- ``ledger:outbox:page`` (admin, read-only in the registry; the view refuses below admin):
  every message, newest first, 50 a page, with its time, store, operator, template, rule,
  status (and held reason), address ("the managers (n)" for an internal message, which goes
  to every manager) and the time the send window lets it go; filters by status, by held
  reason and by a range of days queued (the list pages' filters component). Each row opens
  its stored subject and body. The address is shown in full: the page is admin's only. The
  Store cell names the lease's store; "—" only for a row of the retired ladder or a portal
  message (:data:`~ledger.models.RULES_WITHOUT_STORE`), which have no lease.
- ``ledger:outbox:preview`` (admin, GET, read-only in the registry): "Preview next 30 days",
  what the daily reminder job would queue on each coming day, from today's data, queuing
  nothing (:func:`ledger.services.outbox_preview.preview`); no address, no message text.
- ``ledger:outbox:send_now`` (admin, POST): "Send now" on a held message
  (:func:`ledger.services.messaging.send_now`); the outcome is a message on the page.
- ``ledger:outbox:send_held_for_settings`` (admin, POST; owner, 2026-11-02): "Send all held for
  settings", shown while messages are held ``settings_incomplete``, with their number; the
  confirmation says what it will do ("5 held messages → 2 e-mails, 3 superseded",
  :func:`ledger.services.messaging.release_preview`) and the form posts the number back
  (``count``): they are released grouped as at the daily job
  (:func:`ledger.services.messaging.send_held_for_settings`); the outcome is a message on the
  page.
"""

from __future__ import annotations

import uuid
from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

import mail
from finance.dates import day_text
from ledger.models import RULES_WITHOUT_STORE, HeldReason, MessageStatus
from ledger.services import list_filters, messaging, outbox_preview
from ledger.views.leases import _admin_only
from ledger.views.operators import _actor, role_required

PAGE_SIZE = 50
#: The days a range may name (anything else is ignored, as an unknown filter value is).
FIRST_YEAR = 2000
LAST_YEAR = 2100
#: The most digits the confirmed count of "Send all held for settings" may have.
COUNT_DIGITS = 6


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
    raw_reason = request.GET.get("reason", "")
    reason = raw_reason if raw_reason in HeldReason.values else ""
    since, until = _day(request.GET.get("from")), _day(request.GET.get("to"))
    found = messaging.outbox(status=status, since=since, until=until, held_reason=reason)
    page = Paginator(found, PAGE_SIZE).get_page(request.GET.get("page"))
    managers = messaging.managers_to()
    held_for_settings = messaging.held_for_settings().count()
    accepted = {
        "status": status,
        "reason": reason,
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
                    "reason": ("Held for", dict(HeldReason.choices)),
                    "from": ("From", _day_choice(since)),
                    "to": ("To", _day_choice(until)),
                },
                accepted=accepted,
            ),
            "page": page,
            "query": urlencode({name: value for name, value in accepted.items() if value}),
            "rows": [
                (
                    message,
                    messaging.status_text(message),
                    managers if message.template.internal else message.to_address,
                    message.rule in RULES_WITHOUT_STORE,
                )
                for message in page
            ],
            "status_choices": MessageStatus.choices,
            "reason_choices": HeldReason.choices,
            "selected": {"status": status, "reason": reason, "from": since, "to": until},
            "mail_set_up": mail.is_set_up(),
            "held_for_settings": held_for_settings,
            "release": messaging.release_preview() if held_for_settings else None,
        },
    )


@role_required
@require_safe
def preview_page(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    found = outbox_preview.preview(timezone.localdate())
    return render(
        request,
        "ledger/outbox/preview.html",
        {
            "preview": found,
            "rent_note": outbox_preview.RENT_BEFORE_ITS_CHARGE,
            "f_note": outbox_preview.F_NOT_PREDICTABLE,
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


@role_required
@require_POST
def send_held_for_settings(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    raw = request.POST.get("count", "")
    count = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= COUNT_DIGITS else -1
    outcome = messaging.send_held_for_settings(_actor(request), count)
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect("ledger:outbox:page")
