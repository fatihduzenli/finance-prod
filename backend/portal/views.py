"""The portal's pages (PLAN-014 H1, H3, H6). Every query goes through :mod:`portal.services`
(or :mod:`portal.auth`, :mod:`portal.tokens` and :mod:`portal.invitations` for the sign-in
flow); no view reads a model itself.

Public (``accounts.registry.PORTAL_PUBLIC_ROUTE_NAMES``): ``portal:login`` (GET, POST),
``portal:logout`` (POST), ``portal:invite`` and ``portal:reset_confirm`` (the one-time links,
GET and POST), ``portal:reset_request`` (GET, POST: the same sentence whatever the e-mail),
``portal:not_found`` (any other path under ``/portal/``: the portal's 404).
A link page answers a refused token (unknown, used, expired, of an account that may not use
it) with the same 404 sentence, never logged with its path (the token is in it), and is sent
with ``Referrer-Policy: no-referrer``.

Signed in (``portal.middleware.PortalAuthMiddleware``): ``portal:home``, ``portal:store``,
``portal:invoices``, ``portal:payments`` (a store's id in the URL, answered only when the
store is the operator's: any other id is the portal's 404), ``portal:statement``,
``portal:account`` (GET; POST stores the theme), ``portal:password`` (POST), and, through
:mod:`portal.payments` (PLAN-014 H4, H5), ``portal:pay`` (GET, POST), ``portal:pay_status``
(GET) and ``portal:bank`` (GET, POST; its own Content-Security-Policy: scripts and frames from
this site and the processor's host only).
Every POST is CSRF-protected; every page carries ``X-Robots-Tag: noindex``.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from ledger.models import Store
from ledger.views.calendar import client_address
from portal import auth, invitations, services, tokens
from portal import payments as portal_payments
from portal.middleware import safe_next
from portal.models import PortalToken, PortalUser, TokenKind

LINK_GONE = "This link has expired or was used already. Ask us for a new one."
RESET_SENT = (
    "If this e-mail has a portal account, a link to choose a new password is on its way. "
    "It works for one hour."
)
CHANGED_TEXT = "Your password is changed."

GET_POST = require_http_methods(["GET", "HEAD", "POST"])


def _user(request: HttpRequest) -> PortalUser:
    user: PortalUser = request.portal_user  # type: ignore[attr-defined]
    return user


def _page(
    request: HttpRequest, template: str, *, status: int = 200, **context: Any
) -> HttpResponse:
    user: PortalUser | None = getattr(request, "portal_user", None)
    response = render(
        request,
        template,
        {
            "portal_user": user,
            "portal_theme": user.theme if user is not None else "system",
            "portal_stores": services.store_choices(user) if user is not None else [],
            **context,
        },
        status=status,
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def not_found(request: HttpRequest) -> HttpResponse:
    """``portal:not_found``: the portal's 404, for a foreign store and any unknown path under
    ``/portal/``."""
    return _page(request, "portal/not_found.html", status=404)


def _link_page(request: HttpRequest, template: str, **context: Any) -> HttpResponse:
    response = _page(request, template, **context)
    response["Referrer-Policy"] = "no-referrer"
    return response


def _link_gone(request: HttpRequest) -> HttpResponse:
    response = _page(request, "portal/link_gone.html", status=404, sentence=LINK_GONE)
    response["Referrer-Policy"] = "no-referrer"
    # Django's request logger skips a response marked as logged: its line would carry the
    # path, and so the token.
    response._has_been_logged = True  # type: ignore[attr-defined]
    return response


def _signed_in(request: HttpRequest, user: PortalUser, target: str = "") -> HttpResponse:
    request.portal_session = auth.sign_in_session(  # type: ignore[attr-defined]
        request.portal_session,  # type: ignore[attr-defined]
        user,
    )
    request.portal_user = user  # type: ignore[attr-defined]
    return HttpResponseRedirect(target or reverse("portal:home"))


# --- Signing in ---------------------------------------------------------------------------------


@GET_POST
def login(request: HttpRequest) -> HttpResponse:
    """``portal:login``."""
    following = safe_next(request.POST.get("next") or request.GET.get("next", ""))
    if getattr(request, "portal_user", None) is not None and request.method != "POST":
        return HttpResponseRedirect(following or reverse("portal:home"))
    message = ""
    email = ""
    if request.method == "POST":
        email = auth.normalize_email(request.POST.get("email"))
        result = auth.authenticate(
            request.POST.get("email"), request.POST.get("password"), client_address(request)
        )
        if result.user is not None:
            return _signed_in(request, result.user, following)
        message = result.message
    return _page(
        request,
        "portal/login.html",
        status=200 if not message else 400,
        message=message,
        email=email,
        next=following,
    )


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    """``portal:logout``: the portal session is deleted."""
    request.portal_session.flush()  # type: ignore[attr-defined]
    request.portal_user = None  # type: ignore[attr-defined]
    return redirect("portal:login")


def _link(request: HttpRequest, token: str, kind: str, template: str, use: Any) -> HttpResponse:
    found: PortalToken | None = tokens.find(token, kind)
    if found is None:
        return _link_gone(request)
    problems: list[str] = []
    if request.method == "POST":
        user, problems = use(found, request.POST.get("password"), request.POST.get("confirm"))
        if user is not None:
            return _signed_in(request, user)
    return _link_page(request, template, email=found.user.email, problems=problems)


@GET_POST
def invite(request: HttpRequest, token: str) -> HttpResponse:
    """``portal:invite``: choose the first password from the invitation."""
    return _link(
        request, token, TokenKind.INVITE, "portal/set_password.html", services.accept_invitation
    )


@GET_POST
def reset_confirm(request: HttpRequest, token: str) -> HttpResponse:
    """``portal:reset_confirm``: choose a new password from a reset link."""
    return _link(
        request, token, TokenKind.RESET, "portal/reset_confirm.html", services.reset_password
    )


@GET_POST
def reset_request(request: HttpRequest) -> HttpResponse:
    """``portal:reset_request``: the same answer for any e-mail."""
    sent = False
    if request.method == "POST":
        invitations.request_reset(request.POST.get("email"))
        sent = True
    return _page(request, "portal/reset_request.html", sent=sent, sentence=RESET_SENT)


# --- Pages --------------------------------------------------------------------------------------


@require_safe
def home(request: HttpRequest) -> HttpResponse:
    """``portal:home``: every store of the operator."""
    user = _user(request)
    rows = services.home(user, timezone.localdate())
    return _page(request, "portal/home.html", rows=rows)


@require_safe
def store(request: HttpRequest, pk: str) -> HttpResponse:
    """``portal:store``: the open charges, autopay and the bank on file."""
    user = _user(request)
    found = services.store_of(user, pk)
    if found is None:
        return not_found(request)
    page = services.store_page(user, found, timezone.localdate())
    return _page(request, "portal/store.html", page=page, store=found, today=timezone.localdate())


@require_safe
def invoices(request: HttpRequest, pk: str) -> HttpResponse:
    """``portal:invoices``."""
    user = _user(request)
    found = services.store_of(user, pk)
    if found is None:
        return not_found(request)
    return _page(request, "portal/invoices.html", store=found, rows=services.invoices(user, found))


@require_safe
def payments(request: HttpRequest, pk: str) -> HttpResponse:
    """``portal:payments``."""
    user = _user(request)
    found = services.store_of(user, pk)
    if found is None:
        return not_found(request)
    return _page(request, "portal/payments.html", store=found, rows=services.payments(user, found))


@require_safe
def statement(request: HttpRequest) -> HttpResponse:
    """``portal:statement``: printable, every store of the operator."""
    user = _user(request)
    report = services.statement(user, timezone.localdate())
    return _page(request, "portal/statement.html", report=report)


def _store_or_none(request: HttpRequest, pk: str) -> Store | None:
    return services.store_of(_user(request), pk)


def _bank_url(store: Store, **query: str) -> str:
    url = reverse("portal:bank", kwargs={"pk": store.pk})
    return f"{url}?{urlencode(query)}" if query else url


@GET_POST
def pay(request: HttpRequest, pk: str) -> HttpResponse:
    """``portal:pay`` (PLAN-014 H4): the confirmation of one open charge (``?charge=``) or of
    the store's whole open balance, the amount and the bank's last four; its POST requests
    the payment (:func:`portal.payments.request_payment`) and goes to ``portal:pay_status``.
    The flag off: one sentence, nothing created. No bank method on file: the Bank account
    page first. A charge that is not one of the store's open charges is the portal's 404."""
    user = _user(request)
    found = services.store_of(user, pk)
    if found is None:
        return not_found(request)
    today = timezone.localdate()
    problem = ""
    if request.method == "POST":
        charge_raw = request.POST.get("charge", "")
        outcome = portal_payments.request_payment(
            user,
            found,
            charge_raw,
            request.POST.get("amount"),
            request.POST.get("creation_key"),
            today,
        )
        if outcome is None:
            return not_found(request)
        if outcome.to_bank:
            return HttpResponseRedirect(_bank_url(found, pay="1"))
        if outcome.payment is not None and not outcome.refusal:
            url = reverse("portal:pay_status", kwargs={"pk": found.pk})
            return HttpResponseRedirect(f"{url}?{urlencode({'payment': outcome.payment.pk})}")
        problem = outcome.refusal
    else:
        charge_raw = request.GET.get("charge", "")
    page = portal_payments.pay_page(user, found, charge_raw, today)
    if page is None:
        return not_found(request)
    if page.enabled and not page.has_bank and request.method != "POST":
        return HttpResponseRedirect(_bank_url(found, pay="1"))
    return _page(
        request,
        "portal/pay.html",
        status=400 if problem else 200,
        store=found,
        tab="pay",
        page=page,
        problem=problem,
        unavailable=portal_payments.PAY_UNAVAILABLE,
        creation_key=uuid.uuid4(),
    )


@require_safe
def pay_status(request: HttpRequest, pk: str) -> HttpResponse:
    """``portal:pay_status`` (PLAN-014 H4): the outcome of the user's payment on the store
    (``?payment=``, else the latest), refreshed every 5 seconds while a debit is being
    sent, as Charge now's page; another id is the portal's 404."""
    found = _store_or_none(request, pk)
    if found is None:
        return not_found(request)
    state = portal_payments.payment_state(_user(request), found, request.GET.get("payment"))
    if state is None:
        return not_found(request)
    return _page(
        request,
        "portal/pay_status.html",
        store=found,
        tab="pay",
        state=state,
        refresh=state.waiting,
    )


def _bank_response(
    request: HttpRequest, store: Store, page: portal_payments.BankPage, *, problem: str = ""
) -> HttpResponse:
    response = _page(
        request,
        "portal/bank.html",
        status=400 if problem else 200,
        store=store,
        tab="bank",
        bank=page,
        problem=problem,
        shared_text=portal_payments.shared_replace_text(page),
        needs_bank=request.GET.get("pay") == "1",
        needs_bank_text=portal_payments.PAY_NEEDS_BANK,
        refresh=page.waiting,
    )
    host = page.config.provider_host if page.config is not None else ""
    # Scripts and frames from this site and the processor's host only; never framed.
    scripts = f"'self' {host}" if host else "'self'"
    frames = host or "'none'"
    response["Content-Security-Policy"] = (
        f"script-src {scripts}; frame-src {frames}; frame-ancestors 'none'"
    )
    return response


@GET_POST
def bank(request: HttpRequest, pk: str) -> HttpResponse:
    """``portal:bank`` (PLAN-014 H5): the bank on file (the lease's, or the shared account's
    with "Shared with #1991"), the processor's hosted fields to add or replace it (the fake's
    one field locally; one sentence where bank entry is not set up), and that automatic
    debits are set up by staff. The POST takes only ``payment_method_id`` and queues the
    attach (:func:`portal.payments.request_bank`), then shows how it goes."""
    user = _user(request)
    found = services.store_of(user, pk)
    if found is None:
        return not_found(request)
    if request.method == "POST":
        problem = portal_payments.request_bank(user, found, request.POST)
        if not problem:
            return HttpResponseRedirect(_bank_url(found, sent="1"))
        return _bank_response(
            request, found, portal_payments.bank_page(user, found), problem=problem
        )
    page = portal_payments.bank_page(user, found, after_send=request.GET.get("sent") == "1")
    return _bank_response(request, found, page)


@GET_POST
def account(request: HttpRequest) -> HttpResponse:
    """``portal:account``: the e-mail (read only), the password form and the theme; a POST
    stores the theme and goes back to ``next`` (a portal path) or this page."""
    user = _user(request)
    if request.method == "POST":
        refusal = services.set_theme(user, request.POST.get("theme"))
        if refusal:
            return _page(request, "portal/account.html", status=400, problems=[refusal])
        return HttpResponseRedirect(
            safe_next(request.POST.get("next", "")) or reverse("portal:account")
        )
    changed = request.GET.get("password") == "changed"
    return _page(request, "portal/account.html", changed=changed, changed_text=CHANGED_TEXT)


@require_POST
def password(request: HttpRequest) -> HttpResponse:
    """``portal:password``: change one's own password; this session stays signed in."""
    user = _user(request)
    problems = services.change_password(
        user,
        request.POST.get("current"),
        request.POST.get("password"),
        request.POST.get("confirm"),
    )
    if problems:
        return _page(request, "portal/account.html", status=400, problems=problems)
    auth.refresh_auth_hash(request.portal_session, user)  # type: ignore[attr-defined]
    return HttpResponseRedirect(reverse("portal:account") + "?password=changed")
