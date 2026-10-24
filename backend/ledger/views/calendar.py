"""The staff calendar, its ICS feed and the user's settings page (docs/qa/PLAN-011b D8).

- ``ledger:calendar:page`` (any role): a month, previous and next, filtered by item kind
  and by store, operator or lease; every item links to its page.
- ``ledger:calendar:detail`` (any role): one stored entry.
- ``ledger:calendar:add`` and ``ledger:calendar:edit`` (billing; the author edits their
  own entry, an admin any) and ``ledger:calendar:delete`` (the same). The registry
  refuses the POST below billing; the views refuse the forms' GET below billing too.
- ``ledger:calendar:feed``: ``/calendar/feed/<token>.ics``, public (the token is the
  credential, :mod:`ledger.services.calendar`). Every refusal is the same empty 404,
  marked as logged so that Django's request log never writes the path with the token;
  the view's own log line names the path as :data:`LOGGED_PATH`. At most
  :data:`FEED_LIMIT` requests a minute per client address (:mod:`finance.ratelimit`,
  kept in each process), then 429. The client address is the last ``X-Forwarded-For``
  entry outside local (the one the platform's ingress appends), else ``REMOTE_ADDR``.
- ``finance:settings`` (any role): the user's own calendar feed: create (the token and
  URL are shown once, on the page the POST answers with, which no cache keeps) and
  revoke. Only the user's own feed is ever touched.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotFound,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from accounts.roles import Role, meets
from finance.ratelimit import SlidingWindowLimiter
from ledger.forms import CalendarEntryForm
from ledger.models import CalendarEntry, Lease, Operator, Store
from ledger.services import calendar
from ledger.services.pnl import add_months
from ledger.views.contacts import drop_payment_data
from ledger.views.detail_cards import activity_context
from ledger.views.operators import _actor, _role, role_required
from ledger.views.run import _billing_only

#: Requests a minute per client address on the feed (D8).
FEED_LIMIT = 60
FEED_WINDOW_SECONDS = 60
FEED_LIMITER = SlidingWindowLimiter(FEED_LIMIT, FEED_WINDOW_SECONDS)
#: How the feed's route appears in log lines: never with the token.
LOGGED_PATH = "/calendar/feed/[token].ics"
BAD_MONTH = "Unknown month; showing this month."
BAD_RECORD = "Unknown record; showing every record."
BAD_DAY = "Unknown day; showing today."
#: The page's two views (PLAN-012f-a D6): the month grid, or the days with items as a list
#: (the phone's list, a laptop choice by ``?view=list``). There is no week view.
VIEW_MONTH = "month"
VIEW_LIST = "list"
NO_FEED = "You have no calendar feed."
FEED_REVOKED = "Calendar feed revoked. Its address no longer works."

logger = logging.getLogger(__name__)


# --- The month page ------------------------------------------------------------------------


def _month(raw: str, today: date) -> tuple[date, str]:
    if not raw:
        return today.replace(day=1), ""
    try:
        year, month = (int(part) for part in raw.split("-", 1))
        if not 2000 <= year <= 2100:
            raise ValueError
        return date(year, month, 1), ""
    except ValueError:
        return today.replace(day=1), BAD_MONTH


def _day(raw: str) -> tuple[date | None, str]:
    """``?on=``: the day the side panel shows (an ISO day from 2000 to 2100), else none."""
    if not raw:
        return None, ""
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None, BAD_DAY
    if not 2000 <= day.year <= 2100:
        return None, BAD_DAY
    return day, ""


def _record(raw: str, model: type[Store] | type[Operator] | type[Lease]) -> uuid.UUID | None:
    try:
        key = uuid.UUID(raw)
    except ValueError:
        return None
    return key if model._default_manager.filter(pk=key).exists() else None


def _filters(request: HttpRequest) -> tuple[calendar.Filters, dict[str, str], list[str]]:
    params = request.GET
    messages_: list[str] = []
    chosen = [kind for kind in params.getlist("kind") if kind in calendar.KINDS]
    kinds = frozenset(chosen) if chosen else frozenset(calendar.KINDS)
    records: dict[str, uuid.UUID | None] = {}
    for name, model in (("store", Store), ("operator", Operator), ("lease", Lease)):
        raw = (params.get(name, "") or "").strip()
        records[name] = _record(raw, model) if raw else None
        if raw and records[name] is None:
            messages_.append(BAD_RECORD)
    query = {name: str(value) for name, value in records.items() if value is not None}
    return (
        calendar.Filters(
            kinds=kinds,
            store=records["store"],
            operator=records["operator"],
            lease=records["lease"],
        ),
        query,
        sorted(set(messages_)),
    )


@role_required
@require_safe
def calendar_page(request: HttpRequest) -> HttpResponse:
    """``ledger:calendar:page``: one month of items (PLAN-012f-a D6: the month card, or the
    list by ``?view=list``; the side column's day, ``?on=`` or today, the next seven days and
    the user's feed). The month, the day and the next seven days are read together
    (:func:`calendar.items_in`, three queries)."""
    today = timezone.localdate()
    selected, day_message = _day((request.GET.get("on", "") or "").strip())
    raw_month = (request.GET.get("month", "") or "").strip()
    if selected is not None and not raw_month:
        month, month_message = selected.replace(day=1), ""
    else:
        month, month_message = _month(raw_month, today)
    view = VIEW_LIST if request.GET.get("view") == VIEW_LIST else VIEW_MONTH
    filters, record_query, notes = _filters(request)
    start, end = calendar.month_bounds(month)
    day = selected or today
    found = calendar.items_in(((start, end), (day, day), calendar.next_days_window(today)), filters)
    in_month = [item for item in found if start <= item.on <= end]
    kind_query = [("kind", kind) for kind in calendar.KINDS if kind in filters.kinds]
    if len(kind_query) == len(calendar.KINDS):
        kind_query = []
    view_query = [("view", VIEW_LIST)] if view == VIEW_LIST else []
    keep = urlencode([*kind_query, *record_query.items(), *view_query])
    # The view's links keep the month, the day, the kinds and the records; never the view.
    keep_all_but_view = urlencode(
        [("month", f"{month:%Y-%m}"), *([("on", day.isoformat())] if selected else [])]
        + [*kind_query, *record_query.items()]
    )

    def link(target: date) -> str:
        query = urlencode([("month", f"{target:%Y-%m}")])
        return f"?{query}&{keep}" if keep else f"?{query}"

    return render(
        request,
        "ledger/calendar/page.html",
        {
            "month": month,
            "today": today,
            "day": day,
            "view": view,
            "weeks": calendar.month_weeks(month, in_month),
            "count": len(in_month),
            "day_items": calendar.on_day(found, day),
            "next_days": calendar.next_days(found, today),
            "keep": keep,
            "month_view_link": f"?{keep_all_but_view}",
            "list_view_link": f"?{keep_all_but_view}&view={VIEW_LIST}",
            "previous_link": link(add_months(month, -1)),
            "next_link": link(add_months(month, 1)),
            "this_month_link": link(today.replace(day=1)),
            "kinds": [(kind, calendar.KIND_LABELS[kind]) for kind in calendar.KINDS],
            "kind_choice": next(iter(filters.kinds)) if len(filters.kinds) == 1 else "",
            "several_kinds": 1 < len(filters.kinds) < len(calendar.KINDS),
            "filters": filters,
            "record_query": record_query,
            "messages_shown": [m for m in (month_message, day_message, *notes) if m],
            "feed": calendar.live_feed(_actor(request)),
            "stores": Store.objects.order_by("external_label", "id"),
            "operators": Operator.objects.filter(archived_at__isnull=True).order_by(
                "display_name", "id"
            ),
            "can_add": meets(_role(request), Role.BILLING),
        },
    )


def _entry(pk: str) -> CalendarEntry:
    try:
        key = uuid.UUID(pk)
    except ValueError:
        raise Http404 from None
    return get_object_or_404(
        CalendarEntry.objects.select_related("store", "operator", "lease__store", "author"),
        pk=key,
    )


@role_required
@require_safe
def calendar_detail(request: HttpRequest, pk: str) -> HttpResponse:
    entry = _entry(pk)
    role = _role(request)
    can_change = meets(role, Role.BILLING) and calendar.can_change(
        entry, _actor(request), is_admin=meets(role, Role.ADMIN)
    )
    return render(
        request,
        "ledger/calendar/detail.html",
        {
            "entry": entry,
            "can_change": can_change,
            # PLAN-012f-a D4: the entry's Activity card.
            **activity_context(
                [],
                admin=meets(role, Role.ADMIN),
                store=entry.store,
                record=(calendar.ENTITY_ENTRY, entry.pk),
            ),
        },
    )


def _input(form: CalendarEntryForm) -> calendar.EntryInput:
    data = form.cleaned_data

    def key(name: str) -> uuid.UUID | None:
        return uuid.UUID(data[name]) if data.get(name) else None

    return calendar.EntryInput(
        kind=data["kind"],
        on=data["on"],
        title=data["title"],
        body=data.get("body") or "",
        email_morning=bool(data.get("email_morning")),
        store=key("store"),
        operator=key("operator"),
        lease=key("lease"),
    )


def _save(request: HttpRequest, entry: CalendarEntry | None) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    role = _role(request)
    actor = _actor(request)
    if entry is not None and not calendar.can_change(
        entry, actor, is_admin=meets(role, Role.ADMIN)
    ):
        messages.error(request, calendar.NOT_YOURS)
        return redirect("ledger:calendar:detail", pk=entry.pk)
    if request.method == "POST":
        form = CalendarEntryForm(request.POST)
        if form.is_valid():
            outcome = calendar.save_entry(
                _input(form),
                actor=actor,
                is_admin=meets(role, Role.ADMIN),
                entry_id=entry.pk if entry is not None else None,
            )
            if outcome.ok and outcome.entry is not None:
                messages.success(request, outcome.message)
                return redirect(f"{reverse('ledger:calendar:page')}?month={outcome.entry.on:%Y-%m}")
            form.add_error(outcome.field if outcome.field in form.fields else None, outcome.message)
        drop_payment_data(form)
    elif entry is not None:
        form = CalendarEntryForm(
            initial={
                "kind": entry.kind,
                "on": entry.on,
                "title": entry.title,
                "body": entry.body,
                "email_morning": entry.email_morning,
                "store": str(entry.store_id or ""),
                "operator": str(entry.operator_id or ""),
                "lease": str(entry.lease_id or ""),
            }
        )
    else:
        initial: dict[str, object] = {"kind": calendar.KIND_NOTE}
        try:
            initial["on"] = date.fromisoformat(request.GET.get("on", "") or "")
        except ValueError:
            initial["on"] = timezone.localdate()
        form = CalendarEntryForm(initial=initial)
    return render(request, "ledger/calendar/form.html", {"form": form, "entry": entry})


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def calendar_add(request: HttpRequest) -> HttpResponse:
    return _save(request, None)


@role_required
@require_http_methods(["GET", "HEAD", "POST"])
def calendar_edit(request: HttpRequest, pk: str) -> HttpResponse:
    return _save(request, _entry(pk))


@role_required
@require_POST
def calendar_delete(request: HttpRequest, pk: str) -> HttpResponse:
    refused = _billing_only(request)
    if refused is not None:
        return refused
    entry = _entry(pk)
    month = f"{entry.on:%Y-%m}"
    outcome = calendar.delete_entry(
        entry.pk, actor=_actor(request), is_admin=meets(_role(request), Role.ADMIN)
    )
    if not outcome.ok:
        messages.error(request, outcome.message)
        return redirect("ledger:calendar:detail", pk=entry.pk)
    messages.success(request, outcome.message)
    return redirect(f"{reverse('ledger:calendar:page')}?month={month}")


# --- The feed ------------------------------------------------------------------------------


def client_address(request: HttpRequest) -> str:
    """The client's address: outside local, the last ``X-Forwarded-For`` entry (added by
    the platform's ingress, which a client cannot forge); else ``REMOTE_ADDR``."""
    if settings.APP_ENV != "local":
        forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", "") or "")
        last = forwarded.split(",")[-1].strip()
        if last:
            return last[:64]
    return str(request.META.get("REMOTE_ADDR", "") or "")[:64]


def _refused(reason: str, status: int = 404) -> HttpResponse:
    logger.warning("Calendar feed refused (%s): path=%s", reason, LOGGED_PATH)
    response = HttpResponseNotFound() if status == 404 else HttpResponse(status=status)
    if status == 429:
        response["Retry-After"] = str(int(FEED_WINDOW_SECONDS))
    # Django's request logger skips a response marked as logged; its line would carry
    # the real path, token included.
    response._has_been_logged = True  # type: ignore[attr-defined]
    return response


def calendar_feed(request: HttpRequest, rest: str) -> HttpResponse:
    """``rest`` is everything after ``/calendar/feed/``: the token and ``.ics``."""
    if request.method not in ("GET", "HEAD"):
        return _refused("method")
    if not FEED_LIMITER.allow(client_address(request)):
        return _refused("rate limit", 429)
    if not rest.endswith(calendar.FEED_SUFFIX):
        return _refused("token")
    feed = calendar.feed_for_token(rest[: -len(calendar.FEED_SUFFIX)])
    if feed is None:
        return _refused("token")
    start, end = calendar.feed_window(timezone.localdate())
    found = calendar.items(start, end)
    body = calendar.ics(found, now=timezone.now(), base_url=settings.APP_BASE_URL)
    calendar.record_read(feed, len(found))
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="finance.ics"'
    response["Cache-Control"] = "private, no-store"
    return response


# --- Settings ------------------------------------------------------------------------------


def _feed_url(request: HttpRequest, token: str) -> str:
    path = reverse("ledger:calendar:feed", kwargs={"rest": f"{token}{calendar.FEED_SUFFIX}"})
    return f"{settings.APP_BASE_URL.rstrip('/')}{path}"


def _settings(request: HttpRequest, *, new_url: str = "") -> HttpResponse:
    feed = calendar.live_feed(_actor(request))
    return render(
        request,
        "finance/settings.html",
        {"feed": feed, "new_url": new_url},
    )


@role_required
@require_safe
def settings_page(request: HttpRequest) -> HttpResponse:
    """``finance:settings``: the user's calendar feed."""
    return _settings(request)


@role_required
@require_POST
def feed_create(request: HttpRequest) -> HttpResponse:
    """A new feed for the user (the old one revoked); its address shown on this answer
    only. Not a redirect: the token is never kept anywhere to show it again."""
    token = calendar.create_feed(_actor(request))
    return _settings(request, new_url=_feed_url(request, token))


@role_required
@require_POST
def feed_revoke(request: HttpRequest) -> HttpResponse:
    if calendar.revoke_feed(_actor(request)):
        messages.success(request, FEED_REVOKED)
    else:
        messages.info(request, NO_FEED)
    return redirect("finance:settings")
