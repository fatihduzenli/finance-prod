"""The Bank deposits screens (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I2,
I4).

- ``banking:page`` (billing, read-only in the registry; the view refuses below billing): the
  deposits read from the bank, new first by default (filter by state), 50 a page: the date,
  amount, description and sender, the suggestion (store, charge, confidence badge) with
  Confirm, Choose another, Split and Ignore on a new one, Undo on a recorded or ignored one;
  "Fetch now"; when nothing can be fetched, the one sentence that says why.
- ``banking:confirm`` (billing, POST): records the suggested (or chosen) charge or the lease's
  whole balance; the alias checkbox and "keep the rest as a credit".
- ``banking:choose`` (billing): a store's open charges to choose from (by store number); its
  POST records the choice.
- ``banking:split`` (billing): the open charges of one or more stores (by store numbers) with an
  amount each, the total fixed to the deposit's; its POST records them.
- ``banking:ignore``, ``banking:undo``, ``banking:fetch_now`` (billing, POST).
- ``banking:link_token``, ``banking:link_exchange``, ``banking:disconnect`` (admin, POST): Admin
  › Settings › Bank deposits' Connect (a JSON link token for Plaid Link, then the browser's
  public token) and Disconnect.

Every action answers with a redirect and its outcome as a message (a refusal never repeats what
was posted), except ``link_token``, which answers JSON.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from accounts.views import forbidden
from banking import services
from banking.models import BankTransaction, ChargeType, IgnoreReason, Status
from ledger.models import Lease, Store
from ledger.services import list_filters, payments_entry
from ledger.views.operators import _actor, _role, role_required

PAGE = "banking:page"
SETTINGS_CARD = "ledger:settings:index"
PAGE_SIZE = 50
#: The most stores a split lists at once.
SPLIT_STORES_MAX = 10
_STORE_NUMBERS = re.compile(r"\d{1,9}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{0,128}")
#: Status filter values: the states, and "all".
ALL = "all"


def _billing_only(request: HttpRequest) -> HttpResponse | None:
    return None if meets(_role(request), Role.BILLING) else forbidden(request)


def _admin_only(request: HttpRequest) -> HttpResponse | None:
    return None if meets(_role(request), Role.ADMIN) else forbidden(request)


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw)) if isinstance(raw, str) and raw else None
    except ValueError:
        return None


def _transaction(pk: str) -> BankTransaction:
    found = _uuid(pk)
    txn = BankTransaction.objects.filter(pk=found).first() if found else None
    if txn is None:
        raise Http404
    return txn


def _posted(request: HttpRequest, name: str) -> str:
    value = request.POST.get(name, "")
    return value if isinstance(value, str) else ""


def _answer(request: HttpRequest, outcome: services.Outcome, to: str) -> HttpResponse:
    if outcome.ok:
        messages.success(request, outcome.message)
    else:
        messages.error(request, outcome.message)
    return redirect(to)


def _back(request: HttpRequest) -> str:
    """The page with the filter it was posted from (a known state only)."""
    status = _posted(request, "return_status")
    url = reverse(PAGE)
    if status in (*Status.values, ALL):
        url += "?" + urlencode({"status": status})
    return url


# --- The page ---------------------------------------------------------------------------------


@role_required
@require_safe
def page(request: HttpRequest) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    raw = request.GET.get("status", "")
    status = raw if raw in (*Status.values, ALL) else Status.NEW.value
    found = BankTransaction.objects.select_related("confirmed_by", "ignored_by").order_by(
        "-posted_on", "-created_at", "pk"
    )
    if status != ALL:
        found = found.filter(status=status)
    current = Paginator(found, PAGE_SIZE).get_page(request.GET.get("page"))
    today = timezone.localdate()
    choices = {**dict(Status.choices), ALL: "All"}
    return render(
        request,
        "banking/page.html",
        {
            "page": current,
            "rows": services.rows(list(current), today),
            "readiness": services.readiness(),
            "status": status,
            "status_choices": choices.items(),
            "active_filters": list_filters.active(
                request,
                {"status": ("State", choices)},
                accepted={"status": status},
                defaults={"status": Status.NEW.value},
            ),
            "query": urlencode({"status": status}),
            "ignore_reasons": IgnoreReason.choices,
            "is_admin": meets(_role(request), Role.ADMIN),
        },
    )


# --- Recording --------------------------------------------------------------------------------


def _choice(request: HttpRequest) -> services.Choice | None:
    lease = _uuid(_posted(request, "lease"))
    if lease is None:
        return None
    raw_charge = _posted(request, "charge")
    if raw_charge == "whole":
        return services.Choice(lease)
    kind, _, rest = raw_charge.rpartition(":")
    if not kind:
        kind, rest = _posted(request, "charge_type"), raw_charge
    charge = _uuid(rest)
    if charge is None or kind not in ChargeType.values:
        return None
    return services.Choice(lease, kind, charge)


def _confirm(request: HttpRequest, txn: BankTransaction) -> services.Outcome:
    choice = _choice(request)
    if choice is None:
        return services.Outcome(False, services.NO_LEASE)
    return services.confirm(
        txn.pk,
        choice,
        actor=_actor(request),
        keep_credit=bool(request.POST.get("keep_credit")),
        remember=bool(request.POST.get("remember")),
    )


@role_required
@require_POST
def confirm(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    return _answer(request, _confirm(request, _transaction(pk)), _back(request))


def _store(raw: str) -> Store | None:
    text = raw.strip().lstrip("#")
    if _STORE_NUMBERS.fullmatch(text):
        return Store.objects.filter(store_number=int(text)).first()
    found = _uuid(text)
    return Store.objects.filter(pk=found).first() if found else None


def _store_charges(store: Store) -> tuple[Lease | None, list[payments_entry.OpenCharge]]:
    today = timezone.localdate()
    lease = payments_entry.current_lease(store, today)
    if lease is None:
        return None, []
    charges = [
        charge
        for charge in payments_entry.open_charges(lease, today)
        if charge.kind_label != "Deposit"
    ]
    return lease, charges


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def choose(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    txn = _transaction(pk)
    if request.method == "POST":
        return _answer(request, _confirm(request, txn), reverse(PAGE))
    raw = request.GET.get("store", "")
    store = _store(raw) if isinstance(raw, str) and raw else None
    if store is None and txn.suggested_lease_id is not None:
        store = Store.objects.filter(leases__pk=txn.suggested_lease_id).first()
    lease, charges = _store_charges(store) if store is not None else (None, [])
    known = (
        {(lease.pk, alias) for alias in lease.sender_aliases.values_list("normalised", flat=True)}
        if lease is not None
        else set()
    )
    return render(
        request,
        "banking/choose.html",
        {
            "txn": txn,
            "store": store,
            "store_query": raw if isinstance(raw, str) else "",
            "searched": bool(raw),
            "lease": lease,
            "charges": charges,
            "store_title": services.store_title(lease) if lease is not None else "",
            "alias": services.alias_offer(txn, lease.pk if lease else None, known),
        },
    )


def _split_stores(raw: str) -> list[Store]:
    numbers = [int(text) for text in re.findall(r"\d{1,9}", raw)][:SPLIT_STORES_MAX]
    found = {store.store_number: store for store in Store.objects.filter(store_number__in=numbers)}
    return [found[number] for number in dict.fromkeys(numbers) if number in found]


def _split_lines(request: HttpRequest) -> list[services.SplitLine] | services.Outcome:
    lines = []
    pattern = re.compile(r"amount_([0-9a-f-]{36})_([0-9a-f-]{36})")
    for name in request.POST:
        match = pattern.fullmatch(name)
        if match is None:
            continue
        store, charge = _uuid(match.group(1)), _uuid(match.group(2))
        try:
            amount = services.parse_amount(request.POST.get(name))
        except ValueError:
            return services.Outcome(False, services.SPLIT_AMOUNT)
        if store is None or charge is None:
            return services.Outcome(False, services.SPLIT_NOT_OPEN)
        if amount is not None:
            lines.append(services.SplitLine(store, charge, amount))
    return lines


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def split(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    txn = _transaction(pk)
    if request.method == "POST":
        lines = _split_lines(request)
        if isinstance(lines, services.Outcome):
            back = reverse("banking:split", kwargs={"pk": txn.pk})
            typed = _posted(request, "stores")
            return _answer(
                request, lines, back + ("?" + urlencode({"stores": typed}) if typed else "")
            )
        outcome = services.split(
            txn.pk, lines, actor=_actor(request), keep_credit=bool(request.POST.get("keep_credit"))
        )
        if outcome.ok:
            return _answer(request, outcome, reverse(PAGE))
        typed = _posted(request, "stores")
        back = reverse("banking:split", kwargs={"pk": txn.pk})
        return _answer(
            request, outcome, back + ("?" + urlencode({"stores": typed}) if typed else "")
        )
    raw = request.GET.get("stores", "")
    stores = _split_stores(raw if isinstance(raw, str) else "")
    groups: list[dict[str, Any]] = []
    for store in stores:
        lease, charges = _store_charges(store)
        groups.append(
            {
                "store": store,
                "title": services.store_title(lease) if lease is not None else str(store),
                "lease": lease,
                "charges": charges,
            }
        )
    return render(
        request,
        "banking/split.html",
        {
            "txn": txn,
            "stores_query": raw if isinstance(raw, str) else "",
            "groups": groups,
            "searched": bool(raw),
            "amount": Decimal(txn.amount),
        },
    )


@role_required
@require_POST
def ignore(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    txn = _transaction(pk)
    outcome = services.ignore(
        txn.pk, _posted(request, "reason"), _posted(request, "note"), actor=_actor(request)
    )
    return _answer(request, outcome, _back(request))


@role_required
@require_POST
def undo(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    txn = _transaction(pk)
    return _answer(request, services.undo(txn.pk, actor=_actor(request)), _back(request))


@role_required
@require_POST
def fetch_now(request: HttpRequest) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    report = services.fetch(timezone.localdate(), _actor(request))
    return _answer(request, services.Outcome(report.ok, report.message), reverse(PAGE))


# --- Link (admin) -----------------------------------------------------------------------------


@role_required
@require_POST
def link_token(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    outcome, token = services.create_link_token(_actor(request))
    if not outcome.ok:
        return JsonResponse({"error": outcome.message}, status=400)
    return JsonResponse({"link_token": token})


@role_required
@require_POST
def link_exchange(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    public_token = _posted(request, "public_token").strip()
    account_id = _posted(request, "account_id").strip()
    if not _IDENTIFIER.fullmatch(account_id) or not _IDENTIFIER.fullmatch(public_token):
        return _answer(request, services.Outcome(False, services.LINK_BAD_TOKEN), _card_url())
    outcome = services.link(
        _actor(request), public_token, account_id, _posted(request, "institution")
    )
    return _answer(request, outcome, _card_url())


@role_required
@require_POST
def disconnect(request: HttpRequest) -> HttpResponse:
    refused = _admin_only(request)
    if refused is not None:
        return refused
    return _answer(request, services.disconnect(_actor(request)), _card_url())


def _card_url() -> str:
    return f"{reverse(SETTINGS_CARD)}#bank-deposits"
