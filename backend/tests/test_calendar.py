"""The calendar, its entries, the settings page and the ICS feed (docs/qa/PLAN-011b D8, §7).

Entries are shared staff notes: every user's feed carries the same items, and there is no
private entry to leak (the model has no such field). The feed is per user only so that
each user can revoke their own address.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext

from accounts import registry
from accounts.roles import Role
from finance.ratelimit import SlidingWindowLimiter
from ledger.models import (
    AuditLog,
    CalendarEntry,
    CalendarFeed,
    CollectionStatus,
    Lease,
    Store,
)
from ledger.services import attention, calendar
from ledger.views import calendar as calendar_views
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
CARD = "4111 1111 1111 1111"


@pytest.fixture(autouse=True)
def fresh_limiter() -> Iterator[None]:
    calendar_views.FEED_LIMITER.reset()
    yield
    calendar_views.FEED_LIMITER.reset()


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    return TODAY


def a_note(
    on: date = date(2026, 10, 20), title: str = "Visit the store", **fields: object
) -> CalendarEntry:
    return CalendarEntry.objects.create(kind="note", on=on, title=title, **fields)


def feed_path(token: str) -> str:
    return f"/calendar/feed/{token}.ics"


def make_feed(user: User) -> str:
    return calendar.create_feed(user)


def ics_events(body: str) -> list[dict[str, str]]:
    unfolded = body.replace("\r\n ", "")
    events = []
    for block in re.findall(r"BEGIN:VEVENT\r\n(.*?)END:VEVENT", unfolded, re.DOTALL):
        fields: dict[str, str] = {}
        for line in block.split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key] = value
        events.append(fields)
    return events


# --- Generated items -----------------------------------------------------------------------


def test_debit_days_insurance_anniversaries_and_lease_ends_are_generated() -> None:
    store = a_store("2204 ELM PARK", 2204)
    Store.objects.filter(pk=store.pk).update(open_date=date(2023, 10, 14), needs_open_date=False)
    operator = an_operator("Acme")
    lease = a_lease(store, operator, debit_day=5, second_debit_day=20, started=date(2024, 1, 1))
    ended = a_lease(a_store("3409 OAKFIELD", 3409), an_operator("Beta"), debit_day=1)
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 10, 25))
    found = calendar.items(date(2026, 10, 1), date(2026, 10, 31))
    by_kind = {(item.kind, item.on) for item in found}
    assert (calendar.KIND_DEBIT, date(2026, 10, 5)) in by_kind
    assert (calendar.KIND_DEBIT, date(2026, 10, 20)) in by_kind
    assert (calendar.KIND_INSURANCE, date(2026, 10, 14)) in by_kind
    assert (calendar.KIND_LEASE_END, date(2026, 10, 25)) in by_kind
    # The ended lease's debit day before its end is shown; none after it (November).
    assert (calendar.KIND_DEBIT, date(2026, 10, 1)) in by_kind
    titles = {item.title for item in found}
    assert "Debit day: 2204 ELM PARK (Acme), debit 1" in titles
    assert "Insurance anniversary: 2204 ELM PARK (Acme)" in titles
    assert "Lease ends: 3409 OAKFIELD (Beta)" in titles
    assert all("$" not in item.title for item in found)
    debit = next(item for item in found if item.on == date(2026, 10, 5))
    assert debit.url == f"/leases/{lease.pk}/"
    november = calendar.items(date(2026, 11, 1), date(2026, 11, 30))
    assert not any(
        item.kind == calendar.KIND_DEBIT and item.lease_id == ended.pk for item in november
    )


def test_a_debit_day_past_the_months_end_falls_on_its_last_day() -> None:
    a_lease(debit_day=31)
    february = calendar.items(date(2027, 2, 1), date(2027, 2, 28))
    assert [item.on for item in february if item.kind == calendar.KIND_DEBIT] == [date(2027, 2, 28)]


@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1)])
def test_debit_days_on_the_dst_days_stay_on_their_date(day: date) -> None:
    a_lease(debit_day=day.day)
    found = calendar.items(day, day)
    assert [(item.kind, item.on) for item in found] == [(calendar.KIND_DEBIT, day)]


def test_paused_and_prospective_leases_have_no_debit_days() -> None:
    a_lease(status=CollectionStatus.PAUSED)
    prospective = Lease.objects.create(
        store=a_store(), collection_status=CollectionStatus.PROSPECTIVE, payment_channel="unknown"
    )
    assert prospective.pk
    assert calendar.items(date(2026, 10, 1), date(2026, 10, 31)) == []


def test_filters_by_kind_and_record() -> None:
    first = a_lease(a_store("First"), an_operator("One"))
    a_lease(a_store("Second"), an_operator("Two"))
    a_note(store=first.store)
    everything = calendar.items(date(2026, 10, 1), date(2026, 10, 31))
    only_notes = calendar.items(
        date(2026, 10, 1),
        date(2026, 10, 31),
        calendar.Filters(kinds=frozenset({calendar.KIND_NOTE})),
    )
    assert {item.kind for item in only_notes} == {calendar.KIND_NOTE}
    by_store = calendar.items(
        date(2026, 10, 1), date(2026, 10, 31), calendar.Filters(store=first.store_id)
    )
    assert {item.store_id for item in by_store} == {first.store_id}
    by_operator = calendar.items(
        date(2026, 10, 1), date(2026, 10, 31), calendar.Filters(operator=first.operator_id)
    )
    assert by_operator and all(item.operator_id == first.operator_id for item in by_operator)
    assert len(everything) > len(by_store)


def test_items_read_three_queries_whatever_the_rows() -> None:
    def count() -> int:
        with CaptureQueriesContext(connection) as queries:
            calendar.items(date(2026, 10, 1), date(2027, 3, 31))
        return len(queries)

    a_lease()
    a_note()
    small = count()
    for n in range(8):
        a_lease(a_store(f"S{n}"), an_operator(f"O{n}"))
        a_note(title=f"Note {n}", lease=Lease.objects.first())
    # Entries, leases and (PLAN-011b D5) re-debit charges.
    assert count() == small == 3


# --- The page and the entries --------------------------------------------------------------


def entry_post(**overrides: str) -> dict[str, str]:
    data = {
        "kind": "note",
        "on": "2026-10-20",
        "title": "Call the landlord",
        "body": "About the roof.",
        "store": "",
        "operator": "",
        "lease": "",
    }
    return {**data, **overrides}


def test_a_note_for_october_20_shows_on_the_page_and_links_to_its_page(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    response = client.post("/calendar/new/", entry_post())
    assert response.status_code == 302 and response["Location"] == "/calendar/?month=2026-10"
    entry = CalendarEntry.objects.get()
    page = client.get("/calendar/?month=2026-10").content.decode()
    assert f'href="/calendar/{entry.pk}/">Call the landlord</a>' in page
    assert 'data-day="2026-10-20"' in page
    detail = client.get(f"/calendar/{entry.pk}/").content.decode()
    assert "About the roof." in detail and 'id="calendar-edit"' in detail
    assert AuditLog.objects.filter(
        action="calendar_entry.created", entity_id=str(entry.pk)
    ).exists()


def test_the_month_page_moves_and_keeps_its_filters(today: date) -> None:
    client, _ = client_for(Role.VIEWER)
    store = a_store("Kept")
    page = client.get(f"/calendar/?month=2026-12&kind=note&store={store.pk}").content.decode()
    assert "Dec 2026" in page
    assert f"?month=2026-11&amp;kind=note&amp;store={store.pk}" in page
    assert f"?month=2027-01&amp;kind=note&amp;store={store.pk}" in page
    bad = client.get("/calendar/?month=2026-13&store=nonsense").content.decode()
    assert calendar_views.BAD_MONTH in bad and calendar_views.BAD_RECORD in bad
    assert "Oct 2026" in bad


def test_every_role_reads_the_page_and_only_billing_adds(today: date) -> None:
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get("/calendar/").status_code == 200
    assert 'id="calendar-add"' not in viewer.get("/calendar/").content.decode()
    assert viewer.get("/calendar/new/").status_code == 403
    assert viewer.post("/calendar/new/", entry_post()).status_code == 403
    assert not CalendarEntry.objects.exists()
    assert client_for(None)[0].get("/calendar/").status_code == 403
    assert Client().get("/calendar/").status_code == 401


def test_the_author_edits_and_deletes_their_own_an_admin_any_another_billing_none(
    today: date,
) -> None:
    author, author_user = client_for(Role.BILLING)
    other, _ = client_for(Role.BILLING)
    admin, _ = client_for(Role.ADMIN)
    created = author.post("/calendar/new/", entry_post())
    # Failed once in a full PostgreSQL run (2026-10-17) with nothing created; name the refusal.
    assert created.status_code == 302, created.content.decode()[:600]
    entry = CalendarEntry.objects.get()
    assert entry.author == author_user
    # Another billing user: refused, nothing changes.
    response = other.post(f"/calendar/{entry.pk}/edit/", entry_post(title="Hijacked"))
    assert response.status_code == 302 and response["Location"] == f"/calendar/{entry.pk}/"
    other.post(f"/calendar/{entry.pk}/delete/")
    entry.refresh_from_db()
    assert entry.title == "Call the landlord"
    # The author.
    author.post(f"/calendar/{entry.pk}/edit/", entry_post(title="Call the landlord again"))
    entry.refresh_from_db()
    assert entry.title == "Call the landlord again"
    changed = AuditLog.objects.get(action="calendar_entry.changed")
    assert changed.before is not None and changed.before["title"] == "Call the landlord"
    # An admin deletes it; the audit row keeps what it said.
    admin.post(f"/calendar/{entry.pk}/delete/")
    assert not CalendarEntry.objects.exists()
    deleted = AuditLog.objects.get(action="calendar_entry.deleted")
    assert deleted.before is not None and deleted.before["title"] == "Call the landlord again"


def test_a_card_number_in_a_title_or_body_is_refused_and_not_shown_again(today: date) -> None:
    client, user = client_for(Role.BILLING)
    for fields in ({"title": f"Card {CARD}"}, {"body": f"Account {CARD}"}):
        response = client.post("/calendar/new/", entry_post(**fields))
        assert response.status_code == 200
        assert CARD not in response.content.decode()
    assert not CalendarEntry.objects.exists()
    outcome = calendar.save_entry(
        calendar.EntryInput(kind="note", on=TODAY, title=f"x {CARD}"), actor=user, is_admin=False
    )
    assert not outcome.ok and outcome.message == calendar.PAYMENT_DATA


def test_a_blank_or_long_title_is_refused(today: date) -> None:
    _, user = client_for(Role.BILLING)
    for title, message in ((" ​ ", calendar.TITLE_REQUIRED), ("x" * 121, calendar.TITLE_TOO_LONG)):
        outcome = calendar.save_entry(
            calendar.EntryInput(kind="note", on=TODAY, title=title), actor=user, is_admin=False
        )
        assert not outcome.ok and outcome.message == message
    outcome = calendar.save_entry(
        calendar.EntryInput(kind="private", on=TODAY, title="x"), actor=user, is_admin=False
    )
    assert not outcome.ok


def test_todays_entries_are_on_the_attention_list(today: date) -> None:
    operator = an_operator("Acme")
    entry = a_note(on=TODAY, title="Collect the keys", operator=operator)
    a_note(on=TODAY + timedelta(days=1), title="Tomorrow")
    items = [item for item in attention.attention(TODAY).items if item.kind == "calendar_today"]
    assert [(item.text, item.url) for item in items] == [
        ("Note today: Collect the keys", f"/calendar/{entry.pk}/")
    ]
    mine = attention.attention(TODAY, operator=operator.pk).items
    assert [item.kind for item in mine if item.kind == "calendar_today"] == ["calendar_today"]


def test_entries_are_shared_and_cannot_be_private() -> None:
    names = {field.name for field in CalendarEntry._meta.get_fields()}
    assert not names & {"private", "is_private", "visibility", "owner", "shared"}


# --- Settings and the feed -----------------------------------------------------------------


def test_the_settings_page_creates_shows_once_and_stores_only_the_hash(today: date) -> None:
    client, user = client_for(Role.VIEWER)
    page = client.get("/settings/").content.decode()
    assert "You have no calendar feed." in page
    response = client.post("/settings/calendar-feed/new/")
    assert response.status_code == 200
    body = response.content.decode()
    match = re.search(
        r'value="(http://localhost:8000/calendar/feed/([A-Za-z0-9_-]{43})\.ics)"', body
    )
    assert match is not None
    token = match.group(2)
    feed = CalendarFeed.objects.get(user=user)
    assert bytes(feed.token_hash) == hashlib.sha256(token.encode()).digest()
    assert len(bytes(feed.token_hash)) == 32
    # Shown once: the next view of the page does not show it.
    again = client.get("/settings/").content.decode()
    assert token not in again and "You have a calendar feed" in again
    # Never in the audit log.
    for entry in AuditLog.objects.all():
        assert token not in str(entry.before) and token not in str(entry.after)


def test_the_feed_serves_a_calendar_without_a_session(today: date) -> None:
    client, user = client_for(Role.BILLING)
    client.post(
        "/calendar/new/", entry_post(title="Roof, ladder; keys\\spare", body="Line 1\nLine 2")
    )
    entry = CalendarEntry.objects.get()
    a_lease(a_store("Debit Store"), an_operator("Debtor"), debit_day=9)
    token = make_feed(user)
    response = Client().get(feed_path(token))
    assert response.status_code == 200
    assert response["Content-Type"] == "text/calendar; charset=utf-8"
    assert set(response["Cache-Control"].split(", ")) == {"private", "no-store"}
    body = response.content.decode()
    assert body.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and body.endswith(
        "END:VCALENDAR\r\n"
    )
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    events = ics_events(body)
    note = next(event for event in events if event["UID"].startswith(f"entry-{entry.pk}@"))
    assert note["DTSTART;VALUE=DATE"] == "20261020" and note["DTEND;VALUE=DATE"] == "20261021"
    assert note["SUMMARY"] == "Roof\\, ladder\\; keys\\\\spare"
    assert note["DESCRIPTION"] == "Line 1\\nLine 2"
    assert note["URL"] == f"http://localhost:8000/calendar/{entry.pk}/"
    assert any(event["SUMMARY"].startswith("Debit day: Debit Store") for event in events)
    # Stable UIDs: the same events read again.
    second = ics_events(Client().get(feed_path(token)).content.decode())
    assert [event["UID"] for event in second] == [event["UID"] for event in events]
    # Every read is audited with the user, never the token.
    reads = AuditLog.objects.filter(action="calendar_feed.read")
    assert reads.count() == 2
    for read in reads:
        assert read.actor == user and read.after is not None and read.after["user"] == user.pk
        assert token not in str(read.after) and token not in read.entity_id


def test_the_feed_window_is_30_days_back_and_180_ahead(today: date) -> None:
    _, user = client_for(Role.VIEWER)
    a_note(on=TODAY - timedelta(days=30), title="first day")
    a_note(on=TODAY - timedelta(days=31), title="too old")
    a_note(on=TODAY + timedelta(days=180), title="last day")
    a_note(on=TODAY + timedelta(days=181), title="too far")
    token = make_feed(user)
    summaries = {
        event["SUMMARY"] for event in ics_events(Client().get(feed_path(token)).content.decode())
    }
    assert summaries == {"first day", "last day"}


def test_a_user_with_no_entries_gets_an_empty_calendar(today: date) -> None:
    _, user = client_for(Role.VIEWER)
    token = make_feed(user)
    response = Client().get(feed_path(token))
    assert response.status_code == 200
    assert ics_events(response.content.decode()) == []


def test_two_users_feeds_carry_the_same_shared_items(today: date) -> None:
    _, first = client_for(Role.BILLING)
    _, second = client_for(Role.VIEWER)
    calendar.save_entry(
        calendar.EntryInput(kind="reminder", on=date(2026, 10, 21), title="First's reminder"),
        actor=first,
        is_admin=False,
    )
    one = Client().get(feed_path(make_feed(first))).content.decode()
    two = Client().get(feed_path(make_feed(second))).content.decode()
    assert ics_events(one) == ics_events(two)
    assert any(event["SUMMARY"] == "First's reminder" for event in ics_events(two))


@pytest.mark.parametrize(
    "path",
    [
        "/calendar/feed/",
        "/calendar/feed/.ics",
        "/calendar/feed/short.ics",
        f"/calendar/feed/{'A' * 43}.ics",
        f"/calendar/feed/{'A' * 43}",
        f"/calendar/feed/{'A' * 43}.ics/extra",
        "/calendar/feed/../../admin/.ics",
    ],
)
def test_an_unknown_or_malformed_token_is_an_empty_404(
    path: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        response = Client().get(path)
    assert response.status_code == 404 and response.content == b""
    assert "AAAAAAAA" not in caplog.text and "short" not in caplog.text
    assert not AuditLog.objects.filter(action="calendar_feed.read").exists()


def test_a_revoked_or_replaced_token_is_404(today: date) -> None:
    client, user = client_for(Role.VIEWER)
    first = make_feed(user)
    second = make_feed(user)  # replaces the first
    assert Client().get(feed_path(first)).status_code == 404
    assert Client().get(feed_path(second)).status_code == 200
    response = client.post("/settings/calendar-feed/revoke/")
    assert response.status_code == 302
    assert Client().get(feed_path(second)).status_code == 404
    assert CalendarFeed.objects.filter(user=user, revoked_at__isnull=True).count() == 0
    assert AuditLog.objects.filter(action="calendar_feed.revoked").count() == 2


def test_an_inactive_users_token_is_404(today: date) -> None:
    _, user = client_for(Role.VIEWER)
    token = make_feed(user)
    User.objects.filter(pk=user.pk).update(is_active=False)
    assert Client().get(feed_path(token)).status_code == 404


def test_the_feed_takes_get_only(today: date) -> None:
    _, user = client_for(Role.VIEWER)
    token = make_feed(user)
    for method in ("post", "put", "delete"):
        assert getattr(Client(), method)(feed_path(token)).status_code in (403, 404)
    assert Client().head(feed_path(token)).status_code == 200


def test_a_token_from_the_hash_alone_is_useless() -> None:
    _, user = client_for(Role.VIEWER)
    make_feed(user)
    stored = bytes(CalendarFeed.objects.get().token_hash)
    assert Client().get(feed_path(stored.hex())).status_code == 404
    assert calendar.feed_for_token(stored.hex()) is None


def test_tokens_are_256_bit_and_distinct() -> None:
    _, user = client_for(Role.VIEWER)
    tokens = {make_feed(user) for _ in range(20)}
    assert len(tokens) == 20
    assert all(calendar.TOKEN_PATTERN.match(token) for token in tokens)


def test_the_feed_is_rate_limited_per_address(today: date) -> None:
    _, user = client_for(Role.VIEWER)
    token = make_feed(user)
    client = Client(REMOTE_ADDR="203.0.113.7")
    for _ in range(calendar_views.FEED_LIMIT):
        assert client.get(feed_path(token)).status_code == 200
    limited = client.get(feed_path(token))
    assert limited.status_code == 429 and limited["Retry-After"] == "60"
    # Wrong tokens count too, so guessing is limited the same way.
    assert client.get(feed_path("B" * 43)).status_code == 429
    # Another address is not limited.
    assert Client(REMOTE_ADDR="203.0.113.8").get(feed_path(token)).status_code == 200


def test_the_limiter_forgets_after_its_window_and_bounds_its_keys() -> None:
    clock = [0.0]
    limiter = SlidingWindowLimiter(2, 60, max_keys=3, clock=lambda: clock[0])
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    clock[0] = 60.0
    assert limiter.allow("a")
    for key in ("b", "c", "d", "e"):
        assert limiter.allow(key)
    assert len(limiter._log) <= 3


def test_the_client_address_is_the_last_forwarded_entry_outside_local() -> None:
    request = RequestFactory().get(
        "/calendar/feed/x.ics", HTTP_X_FORWARDED_FOR="1.1.1.1, 198.51.100.4", REMOTE_ADDR="10.0.0.1"
    )
    assert calendar_views.client_address(request) == "10.0.0.1"
    with override_settings(APP_ENV="staging"):
        assert calendar_views.client_address(request) == "198.51.100.4"


def test_the_routes_are_registered() -> None:
    assert "ledger:calendar:feed" in registry.PUBLIC_ROUTE_NAMES
    assert "ledger:calendar:feed" not in registry.PUBLIC_UNSAFE_ROUTE_NAMES
    for name in ("ledger:calendar:add", "ledger:calendar:edit", "ledger:calendar:delete"):
        rule = registry.rule_for(name)
        assert rule is not None and rule.min_role is Role.BILLING
    for name in ("finance:feed_create", "finance:feed_revoke"):
        rule = registry.rule_for(name)
        assert rule is not None and rule.min_role is Role.VIEWER


def test_a_user_without_a_role_has_no_settings_page() -> None:
    client, _ = client_for(None)
    assert client.get("/settings/").status_code == 403
    assert client.post("/settings/calendar-feed/new/").status_code == 403
    assert not CalendarFeed.objects.exists()


def test_ics_text_and_folding() -> None:
    assert calendar.ics_text("a,b;c\\d\r\ne") == "a\\,b\\;c\\\\d\\ne"
    line = "SUMMARY:" + "é" * 60
    folded = calendar.fold(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == line


def test_uid_is_stable_for_a_generated_item(today: date) -> None:
    lease = a_lease(debit_day=9)
    one = calendar.items(date(2026, 10, 1), date(2026, 10, 31))
    two = calendar.items(date(2026, 10, 9), date(2026, 10, 9))
    assert [item.uid for item in one] == [item.uid for item in two]
    assert one[0].uid == f"debit-{lease.pk}-1-20261009@localhost"
