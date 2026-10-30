"""The collections staff actions (docs/qa/PLAN-011b D4, D10; PLAN-014 D6).

- ``ledger:operators:handled_personally`` (billing, POST): mark an operator as handled
  personally, with a note, or clear it; its messages are then held.
- ``ledger:charges:send_message`` and ``ledger:one_off_charges:send_message`` (billing,
  POST): "Send a message now" on an open charge, a template of the select
  (:func:`ledger.services.messaging.send_by_hand`, a ``manual`` row through the queue and
  the send window).

The registry refuses each POST below billing and the views check again. The message is
queued for the worker (``message_send``); no view sends mail. Every POST ends in a
redirect with the outcome as a message.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from ledger.models import ChargeEntityType
from ledger.services import collections, messaging
from ledger.views import charges as rent_charges
from ledger.views import one_off_charges
from ledger.views.operators import _actor, _load, role_required
from ledger.views.run import _billing_only


def _uuid(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _report(request: HttpRequest, outcome: collections.Outcome | messaging.Outcome) -> None:
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)


@role_required
@require_POST
def operator_handled_personally(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    operator = _load(pk)
    outcome = collections.set_handled_personally(
        operator.pk,
        request.POST.get("handled_personally") == "on",
        request.POST.get("note", ""),
        _actor(request),
    )
    _report(request, outcome)
    return redirect(f"{reverse('ledger:operators:detail', kwargs={'pk': operator.pk})}#collections")


def _send_message(request: HttpRequest, charge_type: str, charge_id: uuid.UUID) -> None:
    _report(
        request,
        messaging.send_by_hand(
            charge_type, charge_id, _uuid(request.POST.get("template")), _actor(request)
        ),
    )


@role_required
@require_POST
def rent_charge_send_message(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    charge = rent_charges._load(pk)
    _send_message(request, ChargeEntityType.RENT_CHARGE, charge.pk)
    return redirect(f"{reverse('ledger:charges:detail', kwargs={'pk': charge.pk})}#collections")


@role_required
@require_POST
def one_off_charge_send_message(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    charge = one_off_charges._load(pk)
    _send_message(request, ChargeEntityType.ONE_OFF_CHARGE, charge.pk)
    return redirect(
        f"{reverse('ledger:one_off_charges:detail', kwargs={'pk': charge.pk})}#collections"
    )
