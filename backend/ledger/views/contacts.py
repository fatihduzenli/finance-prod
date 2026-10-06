"""An operator's contact log (docs/qa/PLAN-011b D7).

- ``ledger:operators:contacts`` (any role): every entry, newest first, paginated; the
  operator page shows the latest :data:`ledger.services.contacts.PAGE_LIMIT`.
- ``ledger:operators:contacts_add`` (billing; the registry refuses the POST below billing
  and the view refuses the form's GET too): add an entry, optionally closing an open
  promise. A refusal shows the form again with the reason.
- ``ledger:operators:contacts_export`` (any role): every entry as CSV.
"""

from __future__ import annotations

import uuid

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, QueryDict, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_safe

from accounts.roles import Role, meets
from ledger.forms import ContactEntryForm
from ledger.models import Operator
from ledger.services import contacts
from ledger.services.leases import format_amount
from ledger.validators import contains_bank_routing_pattern, contains_card_number
from ledger.views.operators import (
    PAGE_SIZE,
    _actor,
    _load,
    _role,
    csv_response,
    role_required,
)
from ledger.views.run import _billing_only


def drop_payment_data(form: forms.Form) -> None:
    """Blank every submitted value that holds a card or account number before the form is
    shown again: its errors stay, the number never goes back to the browser."""
    submitted = form.data
    if not isinstance(submitted, QueryDict):
        return
    data = submitted.copy()
    for name in list(data.keys()):
        if any(
            contains_card_number(str(value)) or contains_bank_routing_pattern(str(value))
            for value in data.getlist(name)
        ):
            data[name] = ""
    form.data = data


def promise_choices(operator: Operator) -> list[tuple[str, str]]:
    """The open promises a new entry may close, as form choices."""
    return [
        (
            str(promise.pk),
            f"{format_amount(promise.promised_amount)} by {promise.promised_date}"
            if promise.promised_amount is not None
            else f"Promise of {promise.happened_on}",
        )
        for promise in contacts.open_promises(operator)
    ]


def contact_form(operator: Operator, data: QueryDict | None = None) -> ContactEntryForm:
    return ContactEntryForm(
        data,
        promises=promise_choices(operator),
        initial={"happened_on": timezone.localdate(), "kind": "call"},
    )


@role_required
@require_safe
def contacts_page(request: HttpRequest, pk: str) -> HttpResponse:
    operator = _load(pk)
    page = Paginator(contacts.entries(operator), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "ledger/operators/contacts.html",
        {
            "operator": operator,
            "page": page,
            "can_add": meets(_role(request), Role.BILLING) and operator.archived_at is None,
        },
    )


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def contacts_add(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    operator = _load(pk)
    if operator.archived_at is not None:
        messages.error(request, contacts.ARCHIVED)
        return redirect("ledger:operators:detail", pk=operator.pk)
    if request.method != "POST":
        return render(
            request,
            "ledger/operators/contact_form.html",
            {"operator": operator, "form": contact_form(operator)},
        )
    form = contact_form(operator, request.POST)
    if form.is_valid():
        data = form.cleaned_data
        outcome = contacts.add_entry(
            operator.pk,
            contacts.ContactInput(
                kind=data["kind"],
                happened_on=data["happened_on"],
                body=data["body"],
                promised_date=data.get("promised_date"),
                promised_amount=data.get("promised_amount"),
                closes=uuid.UUID(data["closes"]) if data.get("closes") else None,
            ),
            actor=_actor(request),
        )
        if outcome.ok:
            messages.success(request, outcome.message)
            return redirect(
                f"{reverse('ledger:operators:detail', kwargs={'pk': operator.pk})}#contacts"
            )
        form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
    drop_payment_data(form)
    return render(
        request,
        "ledger/operators/contact_form.html",
        {"operator": operator, "form": form},
    )


@role_required
@require_safe
def contacts_export(request: HttpRequest, pk: str) -> StreamingHttpResponse:
    operator = _load(pk)
    return csv_response(
        contacts.csv_lines(operator),
        f"contacts-{operator.pk}-{timezone.localdate():%Y-%m-%d}.csv",
    )
