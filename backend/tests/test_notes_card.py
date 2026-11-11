"""PLAN-014b item 3 (§6): the store page's NOTES card — ledger.services.notes.card_for (the
pinned notes newest pin first, then the newest unpinned, three at most; the live total; deleted
notes left out; two queries), the card on every tab right after the LEASE card, the one-line
"Add a note…" form for billing and above (a viewer sees none) posting to ledger:notes:add as the
Notes & follow-ups tab's form does, its refusals shown in the card without a 500, "more" for a
long body, "All notes (n) ›", the empty state — and the adversarial cases: a ``<script>`` body
escaped, another store's pinned note never shown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import AuditLog, Note, Store
from ledger.services import notes, workspace
from tests.test_operator_views import client_for
from tests.test_plan014_b_store_page import a_store_with_lease, a_user, page, section

pytestmark = pytest.mark.django_db

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def a_note(store: Store, body: str, *, minutes: int, pinned_minutes: int | None = None) -> Note:
    """A note written ``minutes`` after BASE, pinned ``pinned_minutes`` after BASE if given."""
    _, user = client_for(Role.BILLING)
    note = notes.add(store, body, user)
    Note.objects.filter(pk=note.pk).update(
        created_at=BASE + timedelta(minutes=minutes),
        pinned=pinned_minutes is not None,
        pinned_at=None if pinned_minutes is None else BASE + timedelta(minutes=pinned_minutes),
    )
    return Note.objects.get(pk=note.pk)


def card(html: str) -> str:
    return section(html, "notes-card")


def store_url(store: Store, tab: str = "") -> str:
    return f"/stores/{store.pk}/" + (f"?tab={tab}" if tab else "")


# --- card_for ---------------------------------------------------------------------------------


def test_two_pinned_newest_pin_first_then_the_newest_unpinned_three_of_six() -> None:
    store, _, _ = a_store_with_lease()
    older_pin = a_note(store, "Pinned first, long ago.", minutes=50, pinned_minutes=60)
    newer_pin = a_note(store, "Pinned later.", minutes=1, pinned_minutes=90)
    unpinned = [a_note(store, f"Plain {n}.", minutes=10 * n) for n in range(4)]
    shown = notes.card_for(store)
    assert shown.notes == [newer_pin, older_pin, unpinned[3]]
    assert shown.total == 6


def test_deleted_notes_are_neither_shown_nor_counted() -> None:
    store, _, _ = a_store_with_lease()
    _, admin = client_for(Role.ADMIN)
    gone_pin = a_note(store, "Deleted pin.", minutes=5, pinned_minutes=99)
    gone = a_note(store, "Deleted plain.", minutes=9)
    kept = a_note(store, "Kept.", minutes=1)
    notes.delete(gone_pin, "Wrong store.", admin)
    notes.delete(gone, "Wrong store.", admin)
    shown = notes.card_for(store)
    assert shown.notes == [kept] and shown.total == 1
    assert notes.card_for(a_store_with_lease()[0]) == notes.NotesCard(notes=[], total=0)


def test_card_for_takes_two_queries_whatever_the_data() -> None:
    store, _, _ = a_store_with_lease()
    a_note(store, "One.", minutes=1)

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            list(notes.card_for(store).entries)
        return len(captured)

    few = queries()
    for n in range(12):
        a_note(store, f"More {n}.", minutes=10 + n, pinned_minutes=n if n % 2 else None)
    assert few == queries() == 2


def test_more_follows_the_length_and_the_lines() -> None:
    assert notes.MORE_AFTER == 120  # three lines at the card's width (~45 characters a line)
    assert notes.is_long("x" * 200) and not notes.is_long("x" * 50)
    assert notes.is_long("x" * 121) and not notes.is_long("x" * 120)
    assert notes.is_long("a\nb\nc\nd") and not notes.is_long("a\nb\nc")


# --- the card on the page ---------------------------------------------------------------------


def test_an_empty_store_shows_the_form_and_no_notes_yet_without_all_notes() -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    html = card(page(client, store_url(store)))
    assert 'id="note-card-form"' in html and 'placeholder="Add a note…"' in html
    assert 'id="note-card-save"' in html and ">Save</button>" in html
    assert '<p class="muted">No notes yet.</p>' in html
    assert 'id="notes-card-all"' not in html and 'class="note-item"' not in html


def test_a_viewer_sees_the_notes_but_no_form() -> None:
    store, _, _ = a_store_with_lease()
    a_note(store, "Visible to all.", minutes=1)
    viewer, _ = client_for(Role.VIEWER)
    html = card(page(viewer, store_url(store)))
    assert "Visible to all." in html
    assert 'id="note-card-form"' not in html and 'name="body"' not in html


def test_the_card_lists_author_day_pin_and_all_notes_with_the_total() -> None:
    store, _, _ = a_store_with_lease()
    pinned = a_note(store, "Pinned one.", minutes=1, pinned_minutes=2)
    plain = a_note(store, "Plain one.", minutes=3)
    for n in range(3):
        a_note(store, f"Older {n}.", minutes=-10 - n)
    client, _ = a_user(Role.BILLING)
    html = card(page(client, store_url(store)))
    first = section(html, f"note-item-{pinned.pk}", "</li>")
    assert 'class="note-pin-glyph"' in first and "· Oct 1, 2026" in first
    assert pinned.author_label in first
    assert 'class="note-pin-glyph"' not in section(html, f"note-item-{plain.pk}", "</li>")
    assert html.index("Pinned one.") < html.index("Plain one.")
    assert html.count('class="note-item"') == 3
    link = section(html, "notes-card-all", "</a>")
    assert "All notes (5) ›" in link
    # PLAN-015 Batch 3 (re-pinned): "All notes (n) ›" is the card head's link, after the label.
    tab = workspace.tab_url(store.pk, workspace.TAB_CONTACTS).replace("&", "&amp;")
    assert (
        '<div class="card-head"><h2 class="section-label" id="notes-card-heading">Notes</h2>'
        f'<a class="card-link" href="{tab}" id="notes-card-all">All notes (5) ›</a></div>'
    ) in html
    assert 'class="note-list-all"' not in html
    assert workspace.tab_url(store.pk, workspace.TAB_CONTACTS).replace("&", "&amp;") in html


def test_more_links_a_long_body_to_the_note_on_the_tab_and_not_a_short_one() -> None:
    store, _, _ = a_store_with_lease()
    long = a_note(store, "L" * 200, minutes=3)
    edge = a_note(store, "E" * 121, minutes=2)
    short = a_note(store, "S" * 50, minutes=1)
    client, _ = a_user(Role.BILLING)
    html = card(page(client, store_url(store)))
    entry = section(html, f"note-item-{long.pk}", "</li>")
    assert 'class="card-link note-item-more"' in entry
    assert f'href="/stores/{store.pk}/?tab=contacts#note-{long.pk}"' in entry
    assert "L" * 200 in entry  # the text is whole; CSS cuts it to three lines
    assert "note-item-more" not in section(html, f"note-item-{short.pk}", "</li>")
    assert "note-item-more" in section(html, f"note-item-{edge.pk}", "</li>")


def test_the_card_follows_the_lease_card_on_every_tab() -> None:
    store, _, _ = a_store_with_lease()
    client, _ = client_for(Role.ADMIN)
    for name, _label in workspace.TABS:
        html = page(client, store_url(store, name))
        lease = html.index('id="lease-card"')
        assert lease < html.index('id="notes-card"') < html.index('id="next-card"'), name


def test_the_store_page_queries_do_not_grow_with_notes() -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    a_note(store, "Only one.", minutes=1)
    page(client, store_url(store))  # the session warm

    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            page(client, store_url(store))
        return len(captured)

    few = queries()
    for n in range(15):
        a_note(store, f"Note {n}.", minutes=10 + n, pinned_minutes=n if n % 3 == 0 else None)
    assert queries() == few


# --- adding through the card -------------------------------------------------------------------


def test_billing_adds_a_note_through_the_card_as_the_tab_does() -> None:
    store, lease, _ = a_store_with_lease()
    client, user = a_user(Role.BILLING)
    response = client.post(f"/stores/{store.pk}/notes/", {"body": "From the card.", "from": "card"})
    note = Note.objects.get(entity_id=store.pk)
    assert response.status_code == 302
    assert response["Location"] == f"/stores/{store.pk}/?tab=contacts#note-{note.pk}"
    assert note.author == user and note.lease_id == lease.pk and note.body == "From the card."
    row = AuditLog.objects.get(action="note.created", entity_id=str(note.pk))
    assert row.actor == user
    assert row.after == {"store": str(store.pk), "lease": str(lease.pk), "body": "From the card."}
    client.post(f"/stores/{store.pk}/notes/", {"body": "From the tab."})
    tab_row = AuditLog.objects.filter(action="note.created").exclude(pk=row.pk).get()
    assert set(tab_row.after) == set(row.after) and tab_row.entity_type == row.entity_type


@pytest.mark.parametrize(
    ("body", "message"),
    [("", notes.BODY_REQUIRED), ("   ", notes.BODY_REQUIRED), ("z" * 2001, "2000 characters")],
)
def test_a_refused_body_from_the_card_shows_its_message_in_the_card(
    body: str, message: str
) -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    response = client.post(f"/stores/{store.pk}/notes/", {"body": body, "from": "card"})
    assert response.status_code == 200
    assert not Note.objects.filter(entity_id=store.pk).exists()
    html = response.content.decode()
    shown = card(html)
    assert message in shown and 'id="note-card-error"' in shown and "has-error" in shown
    # The tab's own form stays clean.
    assert "has-error" not in section(html, "note-form", "</form>")


def test_payment_data_from_the_card_is_refused_and_not_shown_again() -> None:
    store, _, _ = a_store_with_lease()
    client, _ = a_user(Role.BILLING)
    response = client.post(
        f"/stores/{store.pk}/notes/", {"body": "Card 4111 1111 1111 1111", "from": "card"}
    )
    assert response.status_code == 200
    assert not Note.objects.filter(entity_id=store.pk).exists()
    assert "4111 1111 1111 1111" not in response.content.decode()
    assert 'id="note-card-error"' in card(response.content.decode())


def test_a_viewer_cannot_post_through_the_card() -> None:
    store, _, _ = a_store_with_lease()
    viewer, _ = client_for(Role.VIEWER)
    response = viewer.post(f"/stores/{store.pk}/notes/", {"body": "Sneaky.", "from": "card"})
    assert response.status_code in (302, 403)
    assert not Note.objects.filter(entity_id=store.pk).exists()


# --- adversarial ------------------------------------------------------------------------------


def test_a_script_body_is_escaped_in_the_card() -> None:
    store, _, _ = a_store_with_lease()
    a_note(store, "<script>alert(1)</script>", minutes=1)
    client, _ = a_user(Role.BILLING)
    html = card(page(client, store_url(store)))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_another_stores_pinned_note_never_shows() -> None:
    store, _, _ = a_store_with_lease()
    other, _, _ = a_store_with_lease()
    a_note(other, "Other store's pin.", minutes=1, pinned_minutes=5)
    a_note(store, "Mine.", minutes=1)
    client, _ = a_user(Role.BILLING)
    html = card(page(client, store_url(store)))
    assert "Other store's pin." not in html and "Mine." in html
    assert "All notes (1) ›" in html
    assert notes.card_for(store).total == 1
