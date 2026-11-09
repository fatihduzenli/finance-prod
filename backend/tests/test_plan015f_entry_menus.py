"""PLAN-015f §3.1: the Notes & follow-ups entries on the store page (and wherever
ledger/contacts/_log_entry.html renders): at most two menus, "Outcome ▾" on an open promise and
"⋯" for Edit / Add correction / Delete; a closed promise's badge names its outcome and day; a
promise whose note is only the auto-sentence shows no note line."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from django.utils.html import escape

from accounts.roles import Role
from ledger.models import ContactEntry, ContactKind, ContactStatus
from ledger.services import contacts
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_contact_log import (
    entry,
    keeping_world,
    page_of,
    promise,
    settled_on,
    table_of,
)
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture: the webhook path)

pytestmark = pytest.mark.django_db


def store_tab(lease_store_id: object, role: Role) -> str:
    client, _ = client_for(role)
    return table_of(page_of(client, f"/stores/{lease_store_id}/?tab=contacts"))


def row_of(log: str, row: ContactEntry) -> str:
    """The ``<li>`` of ``row``: up to the next entry (its menus hold ``</li>`` of their own)."""
    rest = log.split(f'id="contact-{row.pk}"')[1]
    return rest.split('<li class="contact-entry')[0]


def summaries(html: str) -> list[str]:
    return re.findall(r"<summary[^>]*>([^<]*)</summary>", html)


def menu(html: str, element_id: str) -> str:
    return html.split(f'id="{element_id}"')[1].split("</details>")[0]


def items(menu_html: str) -> list[str]:
    """Each item's text, its muted part included."""
    found = re.findall(r'<a role="menuitem"[^>]*>(.*?)</a>', menu_html, re.S)
    return [re.sub(r"<[^>]+>", "", text) for text in found]


def badge(html: str) -> str:
    return str(
        re.findall(r'<span class="badge promise-status promise-\w+">([^<]*)</span>', html)[0]
    )


def a_world() -> tuple[ContactEntry, object]:
    operator = an_operator("Menus Co")
    lease = a_lease(a_store("1501"), operator)
    return promise(operator, lease=lease, body="Pays Friday."), lease.store_id


# --- The controls, per role --------------------------------------------------------------------


def test_an_open_promise_shows_two_menus_for_billing_with_the_three_outcomes() -> None:
    row, store_id = a_world()
    html = row_of(store_tab(store_id, Role.BILLING), row)
    assert summaries(html) == ["Outcome ▾", "⋯"]
    assert html.count('class="menu menu-sm"') == 2
    outcome = menu(html, f"outcome-{row.pk}")
    assert items(outcome) == ["Kept", "Reschedule", "Broken"]
    for name in ("kept", "rescheduled", "broken"):
        link = f'href="/contacts/{row.pk}/outcome/{name}/?next='
        assert link in outcome, name
    # The next= is this page's own path, URL-encoded (same origin).
    assert f"?next=%2Fstores%2F{store_id}%2F%3Ftab%3Dcontacts" in outcome
    more = menu(html, f"more-{row.pk}")
    assert items(more) == ["Add correction"]
    more_summary = 'class="btn btn-sm btn-menu" aria-label="More actions" aria-haspopup="menu">⋯'
    assert f"<summary {more_summary}</summary>" in html
    assert '<summary class="btn btn-sm btn-menu" aria-haspopup="menu">Outcome ▾' in html
    assert outcome.count('<ul role="menu">') == 1 and 'role="none"' in outcome
    # No button row is left.
    assert 'class="btn btn-sm contact-' not in html and "promise-outcomes" not in html


def test_an_admin_also_gets_delete_as_the_danger_item_to_the_confirmation_page() -> None:
    row, store_id = a_world()
    html = row_of(store_tab(store_id, Role.ADMIN), row)
    assert summaries(html) == ["Outcome ▾", "⋯"]
    more = menu(html, f"more-{row.pk}")
    assert items(more) == ["Add correction", "Delete"]
    assert (
        f'<a role="menuitem" class="contact-delete menu-danger" href="/contacts/{row.pk}/delete/">'
        in more
    )


def test_a_viewer_sees_no_menu_on_any_entry() -> None:
    row, store_id = a_world()
    log = store_tab(store_id, Role.VIEWER)
    html = row_of(log, row)
    assert "<details" not in log and "contact-actions" not in html
    assert "Pays Friday." in html


def test_billing_has_no_delete_item_and_the_delete_page_still_refuses_billing() -> None:
    row, store_id = a_world()
    html = row_of(store_tab(store_id, Role.BILLING), row)
    assert "contact-delete" not in html and "Delete" not in html
    client, _ = client_for(Role.BILLING)
    assert client.get(f"/contacts/{row.pk}/delete/").status_code == 403
    assert client.post(f"/contacts/{row.pk}/delete/", {"reason": "x"}).status_code == 403
    row.refresh_from_db()
    assert row.deleted_at is None


def test_a_call_inside_the_authors_window_shows_only_edit_until_in_the_more_menu() -> None:
    operator = an_operator("Window Co")
    lease = a_lease(a_store("1502"), operator)
    client, author = client_for(Role.BILLING)
    call = entry(operator, lease=lease, actor=author, body="No answer.")
    log = table_of(page_of(client, f"/stores/{lease.store_id}/?tab=contacts"))
    html = row_of(log, call)
    assert summaries(html) == ["⋯"]
    [edit] = items(menu(html, f"more-{call.pk}"))
    assert edit.startswith("Edit until ") and re.fullmatch(r"Edit until \d\d:\d\d", edit)
    assert f'href="/contacts/{call.pk}/edit/"' in html and "contact-edit-until" in html


def test_a_closed_promise_has_no_outcome_menu() -> None:
    operator = an_operator("Closed Co")
    lease = a_lease(a_store("1503"), operator)
    old = promise(operator, lease=lease)
    contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.CALL,
            happened_on=date(2026, 10, 3),
            body="Will not pay.",
            previous=old.pk,
            outcome="broken",
        ),
        actor=None,
    )
    html = row_of(store_tab(lease.store_id, Role.ADMIN), old)
    assert summaries(html) == ["⋯"] and "Outcome ▾" not in html


# --- The badge -------------------------------------------------------------------------------


def closed(outcome: str, **fields: object) -> tuple[ContactEntry, object]:
    operator = an_operator(f"Badge {outcome}")
    lease = a_lease(a_store("1504"), operator)
    old = promise(operator, lease=lease, by=date(2026, 10, 5), amount="400.00")
    added = contacts.add_entry(
        operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE if outcome == "rescheduled" else ContactKind.CALL,
            happened_on=date(2026, 10, 3),
            body="Spoke to the manager.",
            previous=old.pk,
            outcome=outcome,
            lease=lease.pk,
            **fields,  # type: ignore[arg-type]
        ),
        actor=None,
    )
    assert added.ok, added.message
    return old, lease.store_id


def test_kept_by_a_call_reads_kept_on_the_calls_day() -> None:
    old, store_id = closed("kept")
    html = row_of(store_tab(store_id, Role.VIEWER), old)
    assert badge(html) == "Kept on Oct 3"
    assert "recorded" not in html and "payment settled" not in html


def test_kept_by_a_payment_reads_kept_on_the_settled_day() -> None:
    _, lease, _, row = keeping_world()
    settled_on(lease, "500.00", date(2026, 10, 5))
    row.refresh_from_db()
    assert row.status == ContactStatus.KEPT and row.closed_by is None
    html = row_of(store_tab(lease.store_id, Role.VIEWER), row)
    assert badge(html) == "Kept on Oct 5"
    assert "payment settled" not in html


def test_rescheduled_reads_the_new_promises_date() -> None:
    old, store_id = closed("rescheduled", promised_date=date(2026, 12, 5), promised_amount=None)
    html = row_of(store_tab(store_id, Role.VIEWER), old)
    assert badge(html) == "Rescheduled to Dec 5"


def test_broken_reads_broken_and_open_reads_open() -> None:
    old, store_id = closed("broken")
    log = store_tab(store_id, Role.VIEWER)
    assert badge(row_of(log, old)) == "Broken"
    fresh, fresh_store = a_world()
    assert badge(row_of(store_tab(fresh_store, Role.VIEWER), fresh)) == "Open"


def test_the_outcome_text_never_crashes_on_missing_links_or_dates() -> None:
    operator = an_operator("Edge Co")
    call = entry(operator)
    assert contacts.outcome_text(call) == ""  # a call: no promise, no badge
    old = promise(operator)
    assert contacts.outcome_text(old) == ""  # open
    # A rescheduled promise closed with no entry (an automatic close down a chain).
    ContactEntry.objects.filter(pk=old.pk).update(status=ContactStatus.RESCHEDULED)
    old.refresh_from_db()
    assert old.closed_by is None and contacts.outcome_text(old) == "Rescheduled"
    ContactEntry.objects.filter(pk=old.pk).update(status=ContactStatus.KEPT)
    old.refresh_from_db()
    assert contacts.outcome_text(old) == "Kept"
    # A call has no promised date: the note helper keeps its body.
    assert call.promised_date is None and contacts.note_text(call) == call.body


# --- The note line -------------------------------------------------------------------------


def test_a_promise_with_only_the_auto_sentence_shows_one_line() -> None:
    operator = an_operator("Sentence Co")
    lease = a_lease(a_store("1505"), operator)
    sentence = contacts.promise_sentence(Decimal("1500.00"), date(2026, 10, 5))
    assert sentence == "Promised $1,500.00 by Oct 5, 2026."
    row = promise(operator, lease=lease, body=sentence)
    # The form writes the sentence; whitespace around it still counts as the sentence.
    padded = promise(operator, lease=lease, body=f"  {sentence}\n")
    log = store_tab(lease.store_id, Role.BILLING)
    for found in (row, padded):
        html = row_of(log, found)
        assert 'class="contact-text"' not in html
        assert "$1,500.00 by Oct 5, 2026" in html  # the kind line keeps amount and date


def test_a_promise_with_a_typed_note_shows_the_whole_note() -> None:
    operator = an_operator("Typed Co")
    lease = a_lease(a_store("1506"), operator)
    body = "Promised $1,500.00 by Oct 5, 2026. Paying from the second account."
    row = promise(operator, lease=lease, body=body)
    html = row_of(store_tab(lease.store_id, Role.BILLING), row)
    assert f'<p class="contact-text">{escape(body)}</p>' in html


def test_a_sentence_for_another_amount_is_a_note() -> None:
    operator = an_operator("Other Co")
    lease = a_lease(a_store("1507"), operator)
    row = promise(operator, lease=lease, body="Promised $9.00 by Oct 5, 2026.")
    html = row_of(store_tab(lease.store_id, Role.BILLING), row)
    assert '<p class="contact-text">Promised $9.00 by Oct 5, 2026.</p>' in html


def test_a_long_unicode_note_renders_escaped_in_one_note_line() -> None:
    operator = an_operator("Unicode Co")
    lease = a_lease(a_store("1508"), operator)
    body = ("Çağrı — 支払い <b>soon</b> 💸 " * 40)[:500].strip()
    row = promise(operator, lease=lease, body=body)
    html = row_of(store_tab(lease.store_id, Role.BILLING), row)
    assert html.count('class="contact-text"') == 1
    assert "<b>soon</b>" not in html and escape(body) in html


def test_a_deleted_promise_keeps_its_struck_body() -> None:
    operator = an_operator("Deleted Co")
    lease = a_lease(a_store("1509"), operator)
    sentence = contacts.promise_sentence(Decimal("1500.00"), date(2026, 10, 5))
    row = promise(operator, lease=lease, body=sentence)
    admin, admin_user = client_for(Role.ADMIN)
    assert contacts.delete_entry(row, "Wrong operator.", actor=admin_user).ok
    log = table_of(page_of(admin, f"/stores/{lease.store_id}/?tab=contacts"))
    html = row_of(log, row)
    assert f'<s class="contact-text">{sentence}</s>' in html
    assert "<details" not in html


# --- The pages carry the menu script -----------------------------------------------------------


def test_every_page_listing_entries_carries_the_menu_script_with_arrow_keys() -> None:
    row, store_id = a_world()
    client, _ = client_for(Role.BILLING)
    operator_id = row.operator_id
    for path in (
        f"/stores/{store_id}/",
        f"/stores/{store_id}/?tab=contacts",
        f"/stores/{store_id}/contacts/",
        f"/operators/{operator_id}/",
        f"/operators/{operator_id}/contacts/",
    ):
        html = page_of(client, path)
        assert f'id="more-{row.pk}"' in html, path
        assert 'document.querySelectorAll("details.menu")' in html, path
        assert '"ArrowDown", "ArrowUp", "Home", "End"' in html, path


def test_the_store_page_lists_the_charges_card_entries_with_the_same_menus() -> None:
    row, store_id = a_world()
    client, _ = client_for(Role.BILLING)
    html = page_of(client, f"/stores/{store_id}/")
    card = html.split('id="contact-card"')[1].split("</section>")[0]
    assert summaries(row_of(card, row)) == ["Outcome ▾", "⋯"]
