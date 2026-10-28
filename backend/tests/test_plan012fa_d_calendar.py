"""PLAN-012f-a D6: the calendar page on the reference (DESIGN-SPEC §4 Calendar,
docs/design/calendar-laptop.png). The header's breadcrumb, the segmented Month · List (no Week:
no such view exists) and the feed page's link; the month card's ‹ month ›, Today, Show (one kind
or everything), Store and Operator filters; the chips by kind, three to a cell and "+N more";
the legend; the side column's day panel (``?on=`` or today), NEXT 7 DAYS (after today, by day and
kind) and FEED (the user's feed: Get the link or Reset token, the settings page's POST); the
month, the day and the next seven days in one read (:func:`calendar.items_in`, three queries);
the page's query budget; a GET writes nothing."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import AuditLog, CalendarEntry, CalendarFeed, Lease
from ledger.services import calendar
from ledger.views import calendar as calendar_views
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)  # a Tuesday
#: The page's queries for a warm billing client: 14 before PLAN-012f-a D6 (session, user,
#: role and the shell's reads, the three calendar reads, the stores and operators selects),
#: one more for the user's feed on the FEED card. The month, the selected day and the next
#: seven days share the three calendar reads.
PAGE_QUERIES = 16  # +1 since PLAN-012g A2: the theme row


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 10, 6, 16, tzinfo=UTC))
    return TODAY


def a_note(on: date, title: str, kind: str = "note", **fields: object) -> CalendarEntry:
    return CalendarEntry.objects.create(kind=kind, on=on, title=title, **fields)


def page(path: str = "/calendar/", role: Role = Role.BILLING) -> str:
    response = client_for(role)[0].get(path)
    assert response.status_code == 200
    return response.content.decode()


def part(body: str, element_id: str) -> str:
    """The element with ``element_id`` up to its closing tag at the same depth."""
    start = body.index(f'id="{element_id}"')
    start = body.rindex("<", 0, start)
    tag = re.match(r"<(\w+)", body[start:])
    assert tag is not None
    name = tag.group(1)
    depth, at = 0, start
    for match in re.finditer(rf"<(/?){name}\b[^>]*>", body[start:]):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            at = start + match.end()
            break
    return body[start:at]


def visible(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# --- Header ------------------------------------------------------------------------------------


def test_the_header_has_the_breadcrumb_title_views_and_the_feed_link(today: date) -> None:
    body = page()
    assert (
        '<nav class="breadcrumb" aria-label="Breadcrumb" id="breadcrumb"><a href="/reports/">'
        "Reports</a>" in body
    )
    assert "<h1>Calendar</h1>" in body
    views = part(body, "calendar-views")
    assert visible(views) == "Month List"
    assert 'href="?month=2026-10" id="calendar-view-month" aria-current="page"' in views
    assert 'href="?month=2026-10&amp;view=list" id="calendar-view-list">' in views
    subscribe = part(body, "calendar-subscribe")
    assert 'href="/settings/#calendar-feed"' in subscribe
    assert visible(subscribe) == "Subscribe in Google Calendar"


def test_there_is_no_week_view(today: date) -> None:
    body = page("/calendar/?view=week")
    assert "Week" not in visible(body)
    assert "?view=week" not in body
    # An unknown view is the month.
    assert 'id="calendar-view-month" aria-current="page"' in body
    assert "is-list" not in body


def test_the_list_view_is_a_query_parameter_that_the_links_keep(today: date) -> None:
    store = a_store("Kept")
    body = page(f"/calendar/?month=2026-12&kind=note&store={store.pk}&view=list")
    assert 'class="card calendar-card is-list"' in body
    views = part(body, "calendar-views")
    assert 'id="calendar-view-list" aria-current="page"' in views
    # Month drops the view and keeps the month, the kind and the store.
    assert f'href="?month=2026-12&amp;kind=note&amp;store={store.pk}" id="calendar-view-month"' in (
        views
    )
    assert f"?month=2026-11&amp;kind=note&amp;store={store.pk}&amp;view=list" in body
    assert f"?month=2027-01&amp;kind=note&amp;store={store.pk}&amp;view=list" in body
    # The filter form keeps the view.
    assert '<input type="hidden" name="view" value="list">' in part(body, "calendar-filters")
    # The day links keep it too.
    assert (
        f"?month=2026-12&amp;on=2026-12-01&amp;kind=note&amp;store={store.pk}&amp;view=list" in body
    )


# --- The month card ----------------------------------------------------------------------------


def test_the_month_card_has_its_controls(today: date) -> None:
    store = a_store("Card store")
    operator = an_operator("Card Co")
    body = page("/calendar/?kind=reminder")
    nav = part(body, "calendar-nav")
    assert '<h2 id="calendar-title">Oct 2026</h2>' in nav
    assert 'id="calendar-previous" href="?month=2026-09&amp;kind=reminder"' in nav
    assert 'id="calendar-next" href="?month=2026-11&amp;kind=reminder"' in nav
    assert 'id="calendar-this-month" href="?month=2026-10&amp;kind=reminder">Today</a>' in nav
    form = part(body, "calendar-filters")
    kinds = part(form, "calendar-kind")
    options = re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', kinds)
    assert options == [
        ("", "", "Everything"),
        ("note", "", "Note"),
        ("reminder", " selected", "Reminder"),
        ("debit", "", "Debit day"),
        ("redebit", "", "Re-debit"),
        ("insurance", "", "Insurance anniversary"),
        ("lease_end", "", "Lease end"),
    ]
    assert f'<option value="{store.pk}">Card store</option>' in form
    assert f'<option value="{operator.pk}">Card Co</option>' in form
    assert '<option value="">All operators</option>' in form
    assert 'id="calendar-show">Show</button>' in form
    # The checkbox fieldset is gone; one select.
    assert 'type="checkbox"' not in form


def test_everything_is_chosen_without_a_kind_and_several_kinds_say_so(today: date) -> None:
    kinds = part(page("/calendar/"), "calendar-kind")
    assert '<option value="" selected>Everything</option>' in kinds
    several = part(page("/calendar/?kind=note&kind=reminder"), "calendar-kind")
    assert '<option value="" selected>2 kinds</option>' in several
    assert '<option value="">Everything</option>' in several


def test_the_chips_carry_their_kind_and_three_show_in_a_cell(today: date) -> None:
    for n in range(5):
        a_note(date(2026, 10, 20), f"Note {n}")
    a_note(date(2026, 10, 21), "A reminder", kind="reminder")
    lease = a_lease(debit_day=12)
    body = page()
    cell = part(body, "calendar-month")
    day_20 = cell[cell.index('data-day="2026-10-20"') :]
    day_20 = day_20[: day_20.index('data-day="2026-10-21"')]
    chips = re.findall(r'<li class="calendar-item kind-note( calendar-extra)?"', day_20)
    assert chips == ["", "", "", " calendar-extra", " calendar-extra"]
    assert (
        '<li class="calendar-more"><a href="?month=2026-10&amp;on=2026-10-20">+2 more</a></li>'
        in day_20
    )
    # The chip's whole title stays in the markup (the ellipsis is the style's).
    assert 'title="Note 0"><a href="/calendar/' in day_20
    assert 'class="calendar-item kind-reminder" data-kind="reminder"' in cell
    debit = f'class="calendar-item kind-debit" data-kind="debit" title="Debit day: {lease.store}'
    assert debit in cell


def test_three_chips_have_no_more_link(today: date) -> None:
    for n in range(3):
        a_note(date(2026, 10, 20), f"Note {n}")
    body = page()
    assert "calendar-more" not in part(body, "calendar-month")
    assert "calendar-extra" not in part(body, "calendar-month")


def test_today_is_marked_and_each_day_number_selects_it(today: date) -> None:
    body = page()
    assert re.search(r'class="calendar-day today selected is-empty" data-day="2026-10-06"', body)
    assert (
        '<a class="calendar-num-link" href="?month=2026-10&amp;on=2026-10-06" '
        'aria-label="Oct 6, 2026" aria-current="date">' in body
    )
    # Billing has the add link per day of the month; a viewer has none.
    assert 'href="/calendar/new/?on=2026-10-20" title="Add on Oct 20, 2026"' in body
    assert "calendar-add-day" not in page(role=Role.VIEWER)


def test_the_legend_names_every_kind(today: date) -> None:
    legend = part(page(), "calendar-legend")
    swatches = re.findall(r'swatch kind-(\w+)" aria-hidden="true"></span>([^<]+)</li>', legend)
    assert swatches == [(kind, calendar.KIND_LABELS[kind]) for kind in calendar.KINDS]


# --- The side column ---------------------------------------------------------------------------


def test_the_day_panel_shows_today_by_default(today: date) -> None:
    entry = a_note(TODAY, "Collect the keys")
    panel = part(page(), "calendar-day-panel")
    assert '<h2 class="section-label" id="calendar-day-title">Tuesday, Oct 6</h2>' in panel
    assert (
        f'<li class="day-entry kind-note" data-kind="note"><a class="day-entry-title" '
        f'href="/calendar/{entry.pk}/">Collect the keys</a><span class="day-entry-meta">Note</span>'
        in panel
    )
    assert 'id="calendar-add" href="/calendar/new/?on=2026-10-06"' in panel


def test_the_day_panel_follows_on_and_moves_the_month(today: date) -> None:
    a_note(date(2026, 12, 3), "December call")
    body = page("/calendar/?on=2026-12-03")
    assert '<h2 id="calendar-title">Dec 2026</h2>' in body
    panel = part(body, "calendar-day-panel")
    assert "Thursday, Dec 3" in panel and "December call" in panel
    assert 'id="calendar-add" href="/calendar/new/?on=2026-12-03"' in panel
    assert 'class="calendar-day selected" data-day="2026-12-03"' in body
    # A day outside the month shown is still read (one read for the three ranges).
    other = page("/calendar/?month=2026-10&on=2026-12-03")
    assert '<h2 id="calendar-title">Oct 2026</h2>' in other
    assert "December call" in part(other, "calendar-day-panel")
    assert "December call" not in part(other, "calendar-month")


def test_an_empty_day_and_another_years_day_and_a_bad_day(today: date) -> None:
    panel = part(page("/calendar/?on=2027-02-02"), "calendar-day-panel")
    assert "Tuesday, Feb 2, 2027" in panel
    # PLAN-012f-b E-4: the empty state (finance/_empty_state.html).
    assert '<div class="empty-state" id="calendar-day-empty">' in panel
    assert '<p class="empty-state-title">Nothing on this day.</p>' in panel
    for raw in ("nonsense", "2026-13-01", "1999-12-31"):
        body = page(f"/calendar/?on={raw}")
        assert calendar_views.BAD_DAY in body, raw
        assert "Tuesday, Oct 6" in part(body, "calendar-day-panel"), raw
    assert "calendar-add" not in part(page(role=Role.VIEWER), "calendar-day-panel")


def test_next_seven_days_groups_by_day_and_kind(today: date) -> None:
    a_note(TODAY, "Today is not next")
    first = a_note(date(2026, 10, 7), "Call A")
    a_note(date(2026, 10, 7), "Call B")
    a_note(date(2026, 10, 7), "Remind", kind="reminder")
    single = a_note(date(2026, 10, 13), "Last of the week")
    a_note(date(2026, 10, 14), "Eight days on")
    card = part(page(), "calendar-next-days")
    rows = re.findall(r'<li class="next-day kind-(\w+)" data-day="([\d-]+)"', card)
    assert rows == [
        ("note", "2026-10-07"),
        ("reminder", "2026-10-07"),
        ("note", "2026-10-13"),
    ]
    assert (
        'Oct 7 · <a href="?month=2026-10&amp;on=2026-10-07">Note</a></span>'
        '<span class="next-day-count">2</span>' in card
    )
    assert "Call A" not in card and str(first.pk) not in card
    assert f'Oct 13 · <a href="/calendar/{single.pk}/">Last of the week</a></span></li>' in card
    assert "Today is not next" not in card and "Eight days on" not in card


def test_next_seven_days_crosses_the_months_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 10, 28, 16, tzinfo=UTC))
    a_note(date(2026, 11, 4), "Seventh day")
    a_note(date(2026, 11, 5), "Eighth day")
    a_lease(debit_day=1)
    body = page()
    card = part(body, "calendar-next-days")
    assert "Nov 1 · " in card and "Debit day: " in card
    assert "Seventh day" in card and "Eighth day" not in card
    # The October grid and its count hold October's items only (the October 1 debit day).
    month = part(body, "calendar-month")
    assert "Seventh day" not in month and 'data-day="2026-11-01"' not in month
    assert 'id="calendar-count">1 item this month.' in body
    # A grid's cells of the next month stay empty though that day is read for the panel:
    # September 2026 ends on a Wednesday; October 1 (a debit day) is selected.
    september = page("/calendar/?month=2026-09&on=2026-10-01")
    assert "Debit day: " in part(september, "calendar-day-panel")
    grid = part(september, "calendar-month")
    october_1 = grid[grid.index('data-day="2026-10-01"') :].split('class="calendar-day', 1)[0]
    assert "calendar-item" not in october_1


def test_next_seven_days_says_when_nothing_is_due(today: date) -> None:
    card = part(page(), "calendar-next-days")
    assert '<div class="empty-state" id="calendar-next-empty">' in card
    assert '<p class="empty-state-title">Nothing in the next 7 days.</p>' in card


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_the_feed_card_offers_the_settings_pages_actions_to_every_role(
    today: date, role: Role
) -> None:
    client, user = client_for(role)
    card = part(client.get("/calendar/").content.decode(), "calendar-feed-card")
    assert "The same events as an ICS feed for Google Calendar or Outlook." in card
    assert 'action="/settings/calendar-feed/new/"' in card
    assert 'id="calendar-feed-create">Get the link</button>' in card
    assert "Reset token" not in card
    assert 'id="calendar-feed-settings" href="/settings/#calendar-feed">Feed settings</a>' in card
    calendar.create_feed(user)
    card = part(client.get("/calendar/").content.decode(), "calendar-feed-card")
    assert 'id="calendar-feed-reset">Reset token</button>' in card
    assert "Get the link" not in card
    assert "Your address was made Oct 6, 2026 · 12:00" in card
    # The token is never on the page: only its hash is stored.
    assert "/calendar/feed/" not in card


def test_reset_token_is_the_settings_pages_post(today: date) -> None:
    client, user = client_for(Role.VIEWER)
    calendar.create_feed(user)
    old = calendar.live_feed(user)
    response = client.post("/settings/calendar-feed/new/")
    assert response.status_code == 200 and 'id="feed-url"' in response.content.decode()
    assert CalendarFeed.objects.get(pk=old.pk).revoked_at is not None  # type: ignore[union-attr]


# --- Reads, writes, budget ---------------------------------------------------------------------


def test_items_in_reads_three_queries_and_each_item_once() -> None:
    a_lease(debit_day=5)
    a_note(date(2026, 10, 5), "Overlap")
    windows = ((date(2026, 10, 1), date(2026, 10, 31)), (date(2026, 10, 5), date(2026, 10, 5)))
    with CaptureQueriesContext(connection) as queries:
        found = calendar.items_in(windows)
    assert len(queries) == 3
    uids = [item.uid for item in found]
    assert len(uids) == len(set(uids))
    assert found == calendar.items(date(2026, 10, 1), date(2026, 10, 31))
    assert calendar.items_in(()) == []


def test_a_get_writes_nothing(today: date) -> None:
    a_lease()
    a_note(TODAY, "Seen")
    client, _ = client_for(Role.ADMIN)
    before = (AuditLog.objects.count(), CalendarEntry.objects.count(), CalendarFeed.objects.count())
    for path in ("/calendar/", "/calendar/?view=list", "/calendar/?on=2026-10-20"):
        assert client.get(path).status_code == 200
        assert client.head(path).status_code == 200
    after = (AuditLog.objects.count(), CalendarEntry.objects.count(), CalendarFeed.objects.count())
    assert after == before


def test_the_page_reads_a_fixed_number_of_queries(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    client.get("/calendar/")

    def count(path: str) -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(path).status_code == 200
        return len(queries)

    a_lease()
    small = count("/calendar/?on=2026-10-29")
    for n in range(8):
        a_lease(a_store(f"S{n}"), an_operator(f"O{n}"))
        a_note(date(2026, 10, 7), f"Note {n}", lease=Lease.objects.first())
        a_note(date(2026, 11, 2), f"Later {n}")
    assert count("/calendar/?on=2026-10-29") == small == PAGE_QUERIES
    assert count("/calendar/?view=list&on=2027-03-01") == PAGE_QUERIES
