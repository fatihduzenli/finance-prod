"""The New calendar entry dialog (owner, 2026-11-06; ledger/calendar/_entry_dialog.html).

The dialog is the Log contact frame over the Calendar page for billing: Kind as icon cards,
the Date with the day in words, Title, Text, "E-mail me a reminder" as a switch with its time
(PLAN-015h; tested in ``test_plan015h_reminder_time.py``), one link
search over stores, operators and leases (``ledger:calendar:search``) shown as a removable
chip, the help line, Cancel and Save. The old URL ``ledger:calendar:add`` opens the page with
the dialog open; a save goes to the entry's month at ``#entry-<pk>``; a refusal shows the
dialog again with its errors. The edit page keeps its form with the three selects.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.forms import CALENDAR_LINK_UNKNOWN
from ledger.models import CalendarEntry, CollectionStatus, Lease, Operator, Store
from ledger.services import calendar, search
from ledger.validators import PAYMENT_DATA_MESSAGE
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_plan012fa_d_calendar import part

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
CARD = "4111 1111 1111 1111"
ADD = "/calendar/new/"
SEARCH = "/calendar/search/"


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    return TODAY


@pytest.fixture
def billing() -> Client:
    return client_for(Role.BILLING)[0]


def body_of(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.content.decode()


def dialog_of(body: str) -> str:
    return part(body, "calendar-entry")


def post(client: Client, **overrides: str) -> tuple[int, str, str]:
    data = {
        "kind": "note",
        "on": "2026-10-20",
        "title": "Call the landlord",
        "body": "",
        "link_kind": "",
        "link_id": "",
        **overrides,
    }
    response = client.post(ADD, data)
    location = response["Location"] if response.status_code == 302 else ""
    return response.status_code, location, response.content.decode()


def results(client: Client, q: str) -> list[dict[str, str]]:
    response = client.get(SEARCH, {"q": q})
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    return list(json.loads(response.content)["results"])


# --- The dialog's parts ------------------------------------------------------------------------


def test_the_dialog_has_its_parts_and_labels(billing: Client) -> None:
    body = body_of(billing, "/calendar/")
    dialog = dialog_of(body)
    head = dialog[: dialog.index(">") + 1]
    assert 'class="dialog dialog-sm"' in head and "data-open" not in head and " open" not in head
    assert '<h2 id="calendar-entry-title">New calendar entry</h2>' in dialog
    # Kind: the model's kinds as radio cards, each with its icon and name.
    kinds = re.findall(
        r'<label class="kind-card" for="entry-kind_\d">(<input[^>]*>)<svg class="icon icon-'
        r'([a-z-]+)"[^>]*>.*?</svg><span class="kind-card-name">([^<]+)</span></label>',
        dialog,
        re.S,
    )
    assert [(icon, name) for _, icon, name in kinds] == [("file", "Note"), ("bell", "Reminder")]
    assert 'value="note"' in kinds[0][0] and "checked" in kinds[0][0]
    assert '<fieldset class="field kind-cards" id="entry-kind">' in dialog
    # The Date with the day in words beside it (today, the day shown).
    assert '<label for="entry-on">Date</label>' in dialog
    assert re.search(r'<input type="date" name="on" value="2026-10-06"[^>]*id="entry-on"', dialog)
    assert '<span class="muted" id="entry-day" aria-live="polite">Oct 6, 2026</span>' in dialog
    # Title required, Text optional (3 rows).
    assert re.search(r'<label for="entry-title">Title <span class="required">\*', dialog)
    assert re.search(r'<input type="text" name="title"[^>]*required', dialog)
    assert re.search(r'<label for="entry-body">Text</label>\s*<textarea[^>]*rows="3"', dialog)
    # The switch and its time (PLAN-015h: a select, hidden while the switch is off).
    assert re.search(
        r'<label class="inline" for="entry-email_morning"><input type="checkbox" '
        r'name="email_morning" class="switch" role="switch" id="entry-email_morning"> '
        r"E-mail me a reminder</label>",
        dialog,
    )
    assert re.search(
        r'<span class="entry-time" id="entry-time-wrap" hidden><label class="visually-hidden" '
        r'for="entry-email_at">Reminder time</label><select name="email_at" id="entry-email_at">',
        dialog,
    )
    assert '<option value="09:00" selected>9:00 AM</option>' in dialog
    assert "entry-morning-time" not in dialog
    # One link field over the search route; no Store, Operator or Lease select in the dialog.
    assert '<label for="entry-link">Link to a store, operator or lease (optional)</label>' in dialog
    link = re.search(r'<input type="search" id="entry-link"[^>]*>', dialog)
    assert link is not None and 'data-url="/calendar/search/"' in link.group(0)
    assert "name=" not in link.group(0) and " hidden" not in link.group(0)
    # The only select is the reminder's time.
    assert dialog.count("<select") == 1
    # The chip waits hidden; the hidden fields are empty.
    assert re.search(r'id="entry-link-chip" hidden>', dialog)
    assert '<input type="hidden" name="link_kind" id="entry-link_kind" value="">' in dialog
    assert '<input type="hidden" name="link_id" id="entry-link_id" value="">' in dialog
    assert (
        '<p class="muted log-contact-help" id="entry-help">Shared with all staff. Never write '
        "card or account numbers.</p>"
    ) in dialog
    foot = part(dialog, "cancel-calendar-entry")
    assert foot.startswith('<a class="btn" id="cancel-calendar-entry" href="/calendar/?')
    assert re.search(
        r'<div class="dialog-foot"><a class="btn" id="cancel-calendar-entry" href="[^"]+">Cancel'
        r'</a><button class="btn btn-primary" type="submit" id="save-calendar-entry">Save</button>'
        r"</div>",
        dialog,
    )
    assert re.search(
        r'<form method="post" action="/calendar/new/" id="calendar-entry-form" '
        r'class="js-single-submit[^"]*"',
        dialog,
    )
    assert dialog.count("btn-primary") == 1
    # The opener: the header's primary, a link to the old URL without the script.
    assert re.search(
        r'<a class="btn btn-primary" id="calendar-new-entry" href="/calendar/new/\?on=2026-10-06" '
        r'data-dialog-open="calendar-entry" data-day="2026-10-06"><svg class="icon icon-plus"'
        r"[^>]*>.*?</svg> New entry</a>",
        body,
        re.S,
    )
    assert "Add note or reminder" not in body
    assert "ledger/calendar_dialog.js" in body


def test_the_day_panel_has_add_entry_on_its_day(billing: Client) -> None:
    """Owner, 2026-11-06: "+ Add entry" at the top of the day's panel, a small secondary button
    opening the dialog on the panel's day; without the script the old URL with ?on=."""
    for path, on in (("/calendar/", "2026-10-06"), ("/calendar/?on=2026-12-03", "2026-12-03")):
        panel = part(body_of(billing, path), "calendar-day-panel")
        assert re.search(
            r'<div class="card-head day-panel-head"><h2 class="section-label" '
            r'id="calendar-day-title">[^<]+</h2><a class="btn btn-sm" id="panel-add-entry" '
            rf'href="/calendar/new/\?on={on}" data-dialog-open="calendar-entry" data-day="{on}">'
            r'<svg class="icon icon-plus"[^>]*>.*?</svg> Add entry</a></div>',
            panel,
            re.S,
        ), path
        assert "btn-primary" not in panel


def test_each_day_of_the_month_has_its_labelled_plus(billing: Client) -> None:
    """The day's "+" (.day-add): one per day of the month shown, each opening the dialog on its
    day, named "Add an entry on <day>" for screen readers; none on the other months' days."""
    month = part(body_of(billing, "/calendar/"), "calendar-month")
    found = re.findall(
        r'<a class="day-add" href="/calendar/new/\?on=(2026-\d\d-\d\d)" '
        r'data-dialog-open="calendar-entry" data-day="\1" title="Add an entry on ([^"]+)" '
        r'aria-label="Add an entry on \2"><svg class="icon icon-plus"',
        month,
    )
    assert [day for day, _ in found] == [f"2026-10-{n:02d}" for n in range(1, 32)]
    assert found[5] == ("2026-10-06", "Oct 6, 2026")
    # A click on the day itself is the panel's link, never the dialog.
    assert (
        '<a class="calendar-num-link" href="?month=2026-10&amp;on=2026-10-06" '
        'aria-label="Oct 6, 2026" aria-current="date">'
    ) in month


def test_a_viewer_has_no_dialog_and_no_opener() -> None:
    viewer = client_for(Role.VIEWER)[0]
    body = body_of(viewer, "/calendar/")
    assert 'id="calendar-entry"' not in body and 'id="calendar-new-entry"' not in body
    assert 'id="panel-add-entry"' not in body and "day-add" not in body
    assert "calendar_dialog.js" not in body
    assert viewer.get(ADD).status_code == 403
    assert viewer.get(f"{ADD}?on=2026-10-22").status_code == 403


def test_the_day_shown_prefills_the_closed_dialog(billing: Client) -> None:
    dialog = dialog_of(body_of(billing, "/calendar/?on=2026-12-03"))
    assert 'value="2026-12-03"' in dialog and ">Dec 3, 2026</span>" in dialog
    assert 'href="/calendar/?month=2026-12&amp;on=2026-12-03"' in dialog


def test_the_old_url_opens_the_calendar_with_the_dialog_open_on_its_day(billing: Client) -> None:
    body = body_of(billing, f"{ADD}?on=2026-10-22")
    assert '<h2 id="calendar-title">Oct 2026</h2>' in body
    assert '<span aria-current="page">Calendar</span>' in body
    dialog = dialog_of(body)
    head = dialog[: dialog.index(">") + 1]
    assert " open" in head and "data-open" in head
    assert 'data-return-url="/calendar/?month=2026-10&amp;on=2026-10-22"' in head
    assert re.search(r'<input type="date" name="on" value="2026-10-22"', dialog)
    assert ">Oct 22, 2026</span>" in dialog
    # The day selected behind it is the same day.
    assert 'class="calendar-day selected is-empty" data-day="2026-10-22"' in body


def test_the_old_url_without_a_day_or_with_a_bad_one_opens_on_today(billing: Client) -> None:
    for path, message in ((ADD, False), (f"{ADD}?on=nonsense", True)):
        body = body_of(billing, path)
        dialog = dialog_of(body)
        assert " open" in dialog[:200], path
        assert 'value="2026-10-06"' in dialog and ">Oct 6, 2026</span>" in dialog, path
        assert ("Unknown day; showing today." in body) is message, path


# --- Saving ---------------------------------------------------------------------------------------


def test_a_save_without_a_link_goes_to_its_month_at_its_chip(billing: Client) -> None:
    status, location, _ = post(billing, email_morning="on", body="About the roof.")
    entry = CalendarEntry.objects.get()
    assert status == 302 and location == f"/calendar/?month=2026-10#entry-{entry.pk}"
    assert (entry.store_id, entry.operator_id, entry.lease_id) == (None, None, None)
    assert entry.kind == "note" and entry.email_morning and entry.body == "About the roof."
    page = body_of(billing, location.split("#")[0])
    assert f'id="entry-{entry.pk}">Call the landlord</a>' in part(page, "calendar-month")
    assert calendar.SAVED in page


def test_a_reminder_without_the_switch_is_saved_as_such(billing: Client) -> None:
    status, _, _ = post(billing, kind="reminder")
    entry = CalendarEntry.objects.get()
    assert status == 302 and entry.kind == "reminder" and not entry.email_morning


def test_a_store_link_takes_the_stores_current_lease_and_its_operator(billing: Client) -> None:
    store = a_store("Elm Park", 2204)
    old = a_lease(store, an_operator("Old Co"), started=date(2023, 1, 1))
    Lease.objects.filter(pk=old.pk).update(
        ended_on=date(2024, 12, 31), collection_status=CollectionStatus.ENDED
    )
    current = a_lease(store, an_operator("Acme"), started=date(2025, 1, 1))
    status, _, _ = post(billing, link_kind="store", link_id=str(store.pk))
    entry = CalendarEntry.objects.get()
    assert status == 302
    assert (entry.store_id, entry.lease_id, entry.operator_id) == (
        store.pk,
        current.pk,
        current.operator_id,
    )


def test_a_store_without_a_lease_links_the_store_alone(billing: Client) -> None:
    store = a_store("Vacant", 303)
    post(billing, link_kind="store", link_id=str(store.pk))
    entry = CalendarEntry.objects.get()
    assert (entry.store_id, entry.lease_id, entry.operator_id) == (store.pk, None, None)


def test_an_operator_link_links_the_operator_alone(billing: Client) -> None:
    operator = an_operator("Acme")
    a_lease(a_store("Acme store"), operator)
    post(billing, link_kind="operator", link_id=str(operator.pk))
    entry = CalendarEntry.objects.get()
    assert (entry.store_id, entry.lease_id, entry.operator_id) == (None, None, operator.pk)


def test_a_lease_link_links_the_lease_its_store_and_its_operator(billing: Client) -> None:
    lease = a_lease(a_store("Oak", 3409), an_operator("Beta"))
    post(billing, link_kind="lease", link_id=str(lease.pk))
    entry = CalendarEntry.objects.get()
    assert (entry.store_id, entry.lease_id, entry.operator_id) == (
        lease.store_id,
        lease.pk,
        lease.operator_id,
    )


@pytest.mark.parametrize(
    "kind, key",
    [
        ("store", "00000000-0000-0000-0000-000000000000"),
        ("store", "not-a-uuid"),
        ("payment", "00000000-0000-0000-0000-000000000000"),
        ("", "00000000-0000-0000-0000-000000000000"),
        ("store", ""),
        ("operator", "archived"),
        ("lease", "a-store"),
    ],
)
def test_an_unknown_link_is_refused_in_the_dialog(billing: Client, kind: str, key: str) -> None:
    if key == "archived":
        operator = an_operator("Gone Co")
        Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
        key = str(operator.pk)
    elif key == "a-store":
        key = str(a_store("Not a lease").pk)  # a store's id given as a lease
    status, _, body = post(billing, link_kind=kind, link_id=key)
    assert status == 200 and not CalendarEntry.objects.exists()
    dialog = dialog_of(body)
    assert CALENDAR_LINK_UNKNOWN in dialog and " open" in dialog[:200]
    # The bad link is not posted back: the hidden fields are empty, the chip hidden.
    assert '<input type="hidden" name="link_kind" id="entry-link_kind" value="">' in dialog
    assert '<input type="hidden" name="link_id" id="entry-link_id" value="">' in dialog


def test_a_refused_title_shows_the_error_in_the_dialog_with_the_chip_kept(billing: Client) -> None:
    store = a_store("Elm Park", 2204)
    status, _, body = post(
        billing, title="   ", on="2026-12-03", link_kind="store", link_id=str(store.pk)
    )
    assert status == 200 and not CalendarEntry.objects.exists()
    dialog = dialog_of(body)
    assert " open" in dialog[:200]
    assert re.search(r'<div class="field has-error">\s*<label for="entry-title">', dialog)
    assert "A title is required." in dialog
    # The page behind is the posted day's month; the dialog keeps the date and the link.
    assert '<h2 id="calendar-title">Dec 2026</h2>' in body
    assert 'value="2026-12-03"' in dialog and ">Dec 3, 2026</span>" in dialog
    assert '<span id="entry-link-text">Store: #2204 Elm Park</span>' in dialog
    assert re.search(r'id="entry-link-chip">', dialog)
    assert re.search(r'<input type="search" id="entry-link"[^>]* hidden>', dialog)
    assert '<input type="hidden" name="link_kind" id="entry-link_kind" value="store">' in dialog
    assert f'<input type="hidden" name="link_id" id="entry-link_id" value="{store.pk}">' in dialog
    assert 'aria-label="Remove the link">&times;</button>' in dialog


def test_a_lease_chip_is_named_by_its_store_and_start(billing: Client) -> None:
    lease = a_lease(a_store("Oak", 3409), an_operator("Beta"), started=date(2025, 3, 1))
    _, _, body = post(billing, title="", link_kind="lease", link_id=str(lease.pk))
    assert '<span id="entry-link-text">Lease: #3409 Oak from Mar 1, 2025</span>' in body


@pytest.mark.parametrize("field", ["title", "body"])
def test_card_or_account_numbers_are_refused_and_never_shown_again(
    billing: Client, field: str
) -> None:
    status, _, body = post(billing, **{field: f"Card {CARD}"})
    assert status == 200 and not CalendarEntry.objects.exists()
    assert CARD not in body
    assert PAYMENT_DATA_MESSAGE in dialog_of(body)


def test_a_viewer_cannot_save() -> None:
    viewer = client_for(Role.VIEWER)[0]
    status, _, _ = post(viewer)
    assert status == 403 and not CalendarEntry.objects.exists()


def test_the_edit_page_keeps_its_form_with_the_three_selects(billing: Client) -> None:
    _, location, _ = post(billing)
    entry = CalendarEntry.objects.get()
    assert location.endswith(f"#entry-{entry.pk}")
    body = body_of(billing, f"/calendar/{entry.pk}/edit/")
    for name in ("store", "operator", "lease"):
        assert f'<select name="{name}"' in body, name
    assert 'id="calendar-form"' in body


# --- The link's search route ----------------------------------------------------------------------


def a_world() -> tuple[Store, Operator, Lease]:
    istanbul = a_store("Istanbul", 1991)
    Store.objects.filter(pk=istanbul.pk).update(city="Atlanta")
    fatih = an_operator("Fatih Co")
    Operator.objects.filter(pk=fatih.pk).update(email="fatih@example.com")
    lease = a_lease(istanbul, fatih, started=date(2025, 3, 1))
    return istanbul, fatih, lease


def test_the_route_is_read_only_and_named() -> None:
    assert reverse("ledger:calendar:search") == SEARCH
    assert "ledger:calendar:search" in registry.READ_ONLY_ROUTE_NAMES
    assert registry.rule_for("ledger:calendar:search") is None


def test_each_kind_answers_its_rows(billing: Client) -> None:
    store, operator, lease = a_world()
    found = results(billing, "istanbul")
    assert found == [
        {"kind": "store", "id": str(store.pk), "label": "#1991 Istanbul", "sub": "Atlanta"},
        {
            "kind": "lease",
            "id": str(lease.pk),
            "label": "#1991 Istanbul from Mar 1, 2025",
            "sub": "Fatih Co",
        },
    ]
    operators = results(billing, "fatih")
    assert [row["kind"] for row in operators] == ["store", "operator", "lease"]
    assert operators[1] == {
        "kind": "operator",
        "id": str(operator.pk),
        "label": "Fatih Co",
        "sub": "f***@example.com",
    }
    assert [row["kind"] for row in results(billing, "#1991")] == ["store", "lease"]
    assert results(billing, "nowhere") == []


def test_an_archived_operator_is_not_offered(billing: Client) -> None:
    operator = an_operator("Gone Co")
    Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
    assert results(billing, "gone") == []


def test_markup_in_a_name_is_data(billing: Client) -> None:
    a_store('<b onmouseover="x">Bad &amp;</b>', 9)
    response = billing.get(SEARCH, {"q": "bad"})
    assert b"<" not in response.content and b">" not in response.content
    assert b"&" not in response.content
    assert json.loads(response.content)["results"][0]["label"] == (
        '#9 <b onmouseover="x">Bad &amp;</b>'
    )


@pytest.mark.parametrize("q", ["", "   ", "\x00\x1f", None])
def test_an_empty_query_lists_nothing(billing: Client, q: str | None) -> None:
    a_world()
    response = billing.get(SEARCH) if q is None else billing.get(SEARCH, {"q": q})
    assert response.status_code == 200
    assert json.loads(response.content) == {"results": []}


def test_the_query_is_cleaned_and_cut_at_100_characters(billing: Client) -> None:
    a_store("x" * 100, 7)
    assert [row["label"] for row in results(billing, "x" * 150)] == ["#7 " + "x" * 100]
    assert [row["label"] for row in results(billing, "\x00Istan\x07bul")] == []
    a_world()
    assert [row["kind"] for row in results(billing, "\x00istanbul\x1f")] == ["store", "lease"]


def test_at_most_eight_of_each_kind_in_three_queries(billing: Client) -> None:
    for number in range(3000, 3010):
        a_lease(a_store(f"Many {number}", number), an_operator(f"Many Co {number}"))
    with CaptureQueriesContext(connection) as queries:
        found = search.link_rows("many")
    assert len(queries) == 3
    kinds = [row["kind"] for row in found]
    assert kinds == ["store"] * 8 + ["operator"] * 8 + ["lease"] * 8
    assert [row["label"] for row in found[:8]] == [f"#{n} Many {n}" for n in range(3000, 3008)]


def test_every_role_reads_it_and_nothing_else_does() -> None:
    a_world()
    viewer = client_for(Role.VIEWER)[0]
    assert len(results(viewer, "istanbul")) == 2
    anonymous = Client().get(SEARCH, {"q": "istanbul"})
    assert anonymous.status_code in (302, 401, 403) and b"Istanbul" not in anonymous.content
    no_role = client_for(None)[0].get(SEARCH, {"q": "istanbul"})
    assert no_role.status_code == 403 and b"Istanbul" not in no_role.content


def test_a_post_is_refused_and_writes_nothing() -> None:
    a_world()
    billing = client_for(Role.BILLING)[0]
    response = billing.post(SEARCH, {"q": "istanbul"})
    assert response.status_code in (403, 405)
    assert b"Istanbul" not in response.content
    assert not CalendarEntry.objects.exists()
