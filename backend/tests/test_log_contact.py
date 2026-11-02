"""The "Log contact" dialog (the owner's slice I-b item 6, 2026-11-01): "+ Log contact" in Notes &
follow-ups opens a dialog over the page (the store page's Notes & follow-ups tab, or the
operator page with the Store select of the operator's leases); Kind as four cards; a call's
outcome chips that may be saved without a note; the promise fields for a promise to pay only,
the amount suggested from the store's open balance; the date defaulting to today as the page
reads it; one help line; the old URLs open the page with the dialog open."""

from __future__ import annotations

import json
import re
from datetime import date

import pytest
from django.test import Client
from django.utils.html import escape

from accounts.roles import Role
from ledger.forms import CALL_NOTE_REQUIRED, CALL_NOTE_TOO_LONG, CONTACT_BODY_MAX_LENGTH
from ledger.models import ContactEntry, ContactKind, Lease, Operator, Store
from ledger.services import contacts
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 1)
CARD = "4111 1111 1111 1111"


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def store_url(store: Store) -> str:
    return f"/stores/{store.pk}/contacts/new/"


def operator_url(operator: Operator) -> str:
    return f"/operators/{operator.pk}/contacts/new/"


def page_of(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.content.decode()


def dialog_of(page: str) -> str:
    start = page.index('<dialog class="dialog dialog-sm" id="log-contact-dialog"')
    return page[start : page.index("</dialog>", start)]


def json_of(page: str, element_id: str) -> object:
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', page)
    assert match is not None, element_id
    return json.loads(match.group(1))


def world() -> tuple[Store, Lease]:
    store = a_store("2477")
    return store, a_lease(store, an_operator("Acme"))


def post(**overrides: str) -> dict[str, str]:
    return {"kind": "call", "happened_on": TODAY.isoformat(), "body": "", **overrides}


def first_entry(page: str) -> str:
    timeline = page.split('id="contact-entries"')[1]
    match = re.search(r'id="contact-([0-9a-f-]{36})"', timeline)
    assert match is not None
    return match.group(1)


# --- The dialog over its page ------------------------------------------------------------------


def test_the_store_url_opens_the_store_page_with_the_dialog_open(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    page = page_of(client, store_url(store))
    # The store page behind, on its Notes & follow-ups tab.
    assert 'id="store-title"' in page and 'id="tab-contacts"' in page
    dialog = dialog_of(page)
    head = dialog.split(">", 1)[0]
    assert " open " in head and head.endswith(" data-open")
    assert f'data-return-url="/stores/{store.pk}/?tab=contacts#tab-contacts"' in dialog
    assert '<h2 id="log-contact-dialog-title">Log contact</h2>' in dialog
    assert "Monvera · Log contact</title>" in page
    # No Store field: the store's lease, hidden.
    assert 'name="lease"' in dialog and '<select name="lease"' not in dialog
    assert f'type="hidden" name="lease" value="{lease.pk}"' in dialog
    assert '<p class="muted" id="log-contact-about">Acme, store 2477</p>' in dialog
    assert f'action="{store_url(store)}"' in dialog
    assert 'id="cancel-contact"' in dialog and 'id="save-contact"' in dialog


def test_the_buttons_say_log_contact_and_open_the_dialog_urls(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    for path in (f"/stores/{store.pk}/?tab=contacts", f"/stores/{store.pk}/"):
        page = page_of(client, path)
        assert f'href="{store_url(store)}"><svg class="icon icon-plus"' in page, path
        assert "Log contact</a>" in page and "Add entry" not in page, path
    operator = page_of(client, f"/operators/{lease.operator_id}/")
    assert lease.operator is not None
    assert f'id="add-contact" href="{operator_url(lease.operator)}"><svg' in operator
    assert "Add entry" not in operator


def test_the_operator_dialog_offers_the_operators_stores_only(today: date) -> None:
    operator = an_operator("Acme")
    first = a_lease(a_store("2476"), operator)
    second = a_lease(a_store("2477"), operator)
    other = a_lease(a_store("9999"), an_operator("Beta"))
    client, _ = client_for(Role.BILLING)
    page = page_of(client, operator_url(operator))
    # The operator page behind.
    assert 'id="operator-contacts-card"' in page
    dialog = dialog_of(page)
    assert f'data-return-url="/operators/{operator.pk}/#contacts"' in dialog
    select = dialog.split('<select name="lease"')[1].split("</select>")[0]
    assert re.findall(r'<option value="([^"]*)"', select) == [str(first.pk), str(second.pk)]
    assert str(other.pk) not in page and "9999" not in select
    assert 'id="log-contact-about"' not in dialog


# --- Kind, outcome chips, promise fields --------------------------------------------------------


def test_kind_is_four_cards_in_a_fieldset_and_the_dependent_fields_name_their_kind(
    today: date,
) -> None:
    store, _ = world()
    client, _ = client_for(Role.BILLING)
    dialog = dialog_of(page_of(client, store_url(store)))
    kinds = dialog.split('id="log-contact-kind"')[1].split("</fieldset>")[0]
    assert "<legend>Kind</legend>" in kinds
    cards = re.findall(
        r'<label class="kind-card" for="(log-kind_\d)"><input type="radio" name="kind" '
        r'value="(\w+)"[^>]*>(<svg class="icon icon-[\w-]+")',
        kinds,
    )
    assert cards == [
        ("log-kind_0", "call", '<svg class="icon icon-phone"'),
        ("log-kind_1", "email", '<svg class="icon icon-mail"'),
        ("log-kind_2", "visit", '<svg class="icon icon-map-pin"'),
        ("log-kind_3", "promise", '<svg class="icon icon-handshake"'),
    ]
    names = re.findall(r'<span class="kind-card-name">([^<]+)</span>', kinds)
    assert names == ["Call", "E-mail", "Visit", "Promise to pay"]
    # Call is chosen by default.
    assert re.search(r'value="call"[^>]*checked', kinds)
    chips = dialog.split('id="log-contact-outcome"')[1].split("</fieldset>")[0]
    assert "<legend>Outcome</legend>" in chips
    values = re.findall(
        r'name="call_outcome" value="(\w+)"[^>]*data-shown-when="log-kind_0"', chips
    )
    assert values == ["answered", "no_answer", "left_message", "wrong_number"]
    assert re.findall(r"<span>([^<]+)</span>", chips) == [
        "Answered",
        "No answer",
        "Left a message",
        "Wrong number",
    ]
    for name in ("promised_date", "promised_amount"):
        control = re.search(rf'<input [^>]*name="{name}"[^>]*>', dialog)
        assert control is not None and 'data-shown-when="log-kind_3"' in control.group(0), name
    assert ">Promised date" in dialog and ">Amount (USD)" in dialog
    assert 'placeholder="What was said, what happens next"' in dialog
    # The page loads the dependent-field script and the dialog's own.
    page = page_of(client, store_url(store))
    assert "ledger/shown_when.js" in page and "ledger/log_contact.js" in page


def test_the_date_defaults_to_today_and_reads_as_the_page_reads_a_day(today: date) -> None:
    store, _ = world()
    client, _ = client_for(Role.BILLING)
    dialog = dialog_of(page_of(client, store_url(store)))
    assert 'name="happened_on" value="2026-11-01"' in dialog
    assert (
        '<span class="help muted" id="log-contact-day" aria-live="polite">Nov 1, 2026</span>'
        in dialog
    )
    # A refused form shows the posted day.
    response = client.post(store_url(store), post(happened_on="2026-10-30"))
    assert response.status_code == 200
    assert (
        'id="log-contact-day" aria-live="polite">Oct 30, 2026</span>' in response.content.decode()
    )


def test_one_help_line_replaces_the_long_help_text(today: date) -> None:
    store, _ = world()
    client, _ = client_for(Role.BILLING)
    dialog = dialog_of(page_of(client, store_url(store)))
    assert dialog.count("Never write card or account numbers.") == 1
    assert '<p class="muted log-contact-help" id="log-contact-help">' in dialog
    for gone in ("A promise needs its date and amount", "Learn more", "New York date.", "only."):
        assert gone not in dialog, gone


# --- Saving -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "body"),
    [
        ("answered", "Answered."),
        ("no_answer", "No answer."),
        ("left_message", "Left a message."),
        ("wrong_number", "Wrong number."),
    ],
)
def test_a_call_with_an_outcome_is_saved_without_a_note(
    today: date, outcome: str, body: str
) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    response = client.post(store_url(store), post(call_outcome=outcome, lease=str(lease.pk)))
    assert response.status_code == 302
    entry = ContactEntry.objects.get()
    assert (entry.kind, entry.body, entry.lease) == (ContactKind.CALL, body, lease)


def test_a_call_with_an_outcome_and_a_note_starts_with_the_sentence(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    client.post(
        store_url(store),
        post(call_outcome="no_answer", body="  Try again Friday. ", lease=str(lease.pk)),
    )
    assert ContactEntry.objects.get().body == "No answer. Try again Friday."


def test_a_call_without_an_outcome_or_a_note_is_refused(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    for body in ("", " ​\n"):
        response = client.post(store_url(store), post(body=body, lease=str(lease.pk)))
        assert response.status_code == 200
        assert escape(CALL_NOTE_REQUIRED) in dialog_of(response.content.decode())
    assert not ContactEntry.objects.exists()


def test_a_visit_without_a_note_is_refused_even_with_a_chip(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    response = client.post(
        store_url(store), post(kind="visit", call_outcome="answered", lease=str(lease.pk))
    )
    assert response.status_code == 200
    assert escape(contacts.BODY_REQUIRED) in dialog_of(response.content.decode())
    assert not ContactEntry.objects.exists()


@pytest.mark.parametrize(
    ("note", "body"), [("", "E-mail sent."), ("Sent the October statement.", None)]
)
def test_an_email_is_saved_without_a_note_with_its_sentence(
    today: date, note: str, body: str | None
) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    response = client.post(store_url(store), post(kind="email", body=note, lease=str(lease.pk)))
    assert response.status_code == 302
    entry = ContactEntry.objects.get()
    assert (entry.kind, entry.body) == (ContactKind.EMAIL, body or f"E-mail sent. {note}")


def test_an_outcome_chip_is_read_for_a_call_only(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    response = client.post(
        store_url(store),
        post(
            kind="visit", call_outcome="wrong_number", body="Met the manager.", lease=str(lease.pk)
        ),
    )
    assert response.status_code == 302
    assert ContactEntry.objects.get().body == "Met the manager."


@pytest.mark.parametrize("value", ["maybe", "No answer.", CARD, "<img src=x onerror=1>"])
def test_an_unknown_outcome_is_refused_and_not_echoed(today: date, value: str) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    response = client.post(store_url(store), post(call_outcome=value, lease=str(lease.pk)))
    assert response.status_code == 200
    page = response.content.decode()
    assert "Choose one of the options." in dialog_of(page)
    assert value not in page and escape(value) not in page
    assert not ContactEntry.objects.exists()


def test_a_promise_needs_its_date_and_amount_but_no_note(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    base = post(kind="promise", lease=str(lease.pk))
    for missing in (
        {"promised_date": "", "promised_amount": "3500.00"},
        {"promised_date": "2026-11-04", "promised_amount": ""},
        {"promised_date": "", "promised_amount": ""},
    ):
        response = client.post(store_url(store), {**base, **missing})
        assert response.status_code == 200
        dialog = dialog_of(response.content.decode())
        assert escape(contacts.PROMISE_FIELDS_REQUIRED) in dialog
        assert escape(contacts.BODY_REQUIRED) not in dialog
    assert not ContactEntry.objects.exists()
    response = client.post(
        store_url(store), {**base, "promised_date": "2026-11-04", "promised_amount": "3500.00"}
    )
    assert response.status_code == 302
    entry = ContactEntry.objects.get()
    assert (entry.kind, str(entry.promised_amount), entry.promised_date, entry.body) == (
        ContactKind.PROMISE,
        "3500.00",
        date(2026, 11, 4),
        "Promised $3,500.00 by Nov 4, 2026.",
    )
    response = client.post(
        store_url(store),
        {
            **base,
            "body": "Owner calls back.",
            "promised_date": "2026-11-06",
            "promised_amount": "5",
            # The operator now has an open promise: the dialog asks what happened to it.
            "previous": str(entry.pk),
            "outcome": "open",
        },
    )
    assert response.status_code == 302
    newest = ContactEntry.objects.order_by("-created_at").first()
    assert newest is not None
    assert newest.body == "Promised $5.00 by Nov 6, 2026. Owner calls back."


def test_the_notes_marker_follows_the_kind(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    marker = '<span id="log-note-required" hidden>'
    shown = '<span id="log-note-required"> <span class="required">'
    # Call without an outcome (the default): required.
    assert shown in dialog_of(page_of(client, store_url(store)))
    refused = {
        "visit": post(kind="visit", lease=str(lease.pk)),
        "call": post(lease=str(lease.pk)),
        "email": post(kind="email", promised_amount="5", lease=str(lease.pk)),
        "promise": post(kind="promise", lease=str(lease.pk)),
        "call-chip": post(call_outcome="answered", promised_amount="5", lease=str(lease.pk)),
    }
    expected = {"visit": shown, "call": shown, "email": marker, "promise": marker}
    expected["call-chip"] = marker
    for name, data in refused.items():
        response = client.post(store_url(store), data)
        assert response.status_code == 200, name
        assert expected[name] in dialog_of(response.content.decode()), name
    # The promise fields always carry their marker (they are shown only for a promise).
    dialog = dialog_of(page_of(client, store_url(store)))
    for label in ("Promised date", "Amount (USD)"):
        assert re.search(rf'>{re.escape(label)} <span class="required">\*', dialog), label
    assert not ContactEntry.objects.exists()


def test_promise_fields_on_another_kind_are_still_refused(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    response = client.post(
        store_url(store),
        post(call_outcome="answered", promised_amount="500.00", lease=str(lease.pk)),
    )
    assert response.status_code == 200
    assert escape(contacts.PROMISE_FIELDS_ONLY) in response.content.decode()
    assert not ContactEntry.objects.exists()


def test_the_amount_suggests_the_stores_open_balance(today: date) -> None:
    store, lease = world()
    a_rent_charge(lease, date(2026, 10, 1), due="1250.00")
    client, _ = client_for(Role.BILLING)
    page = page_of(client, store_url(store))
    assert json_of(page, "promise-amounts") == {str(lease.pk): "1250.00"}
    # Not the input's value: the script fills it when Promise to pay is chosen.
    amount = re.search(r'<input [^>]*name="promised_amount"[^>]*>', dialog_of(page))
    assert amount is not None and "value=" not in amount.group(0)


def test_no_open_balance_suggests_nothing(today: date) -> None:
    store, _ = world()
    client, _ = client_for(Role.BILLING)
    assert json_of(page_of(client, store_url(store)), "promise-amounts") == {}


def test_the_operator_dialog_suggests_each_stores_balance(today: date) -> None:
    operator = an_operator("Acme")
    owing = a_lease(a_store("2476"), operator)
    clear = a_lease(a_store("2477"), operator)
    a_rent_charge(owing, date(2026, 10, 1), due="800.00")
    client, _ = client_for(Role.BILLING)
    amounts = json_of(page_of(client, operator_url(operator)), "promise-amounts")
    assert amounts == {str(owing.pk): "800.00", "": "800.00"}
    assert isinstance(amounts, dict) and str(clear.pk) not in amounts


def test_the_note_is_checked_for_payment_data_and_not_echoed(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    for data in (
        post(call_outcome="no_answer", body=f"Card {CARD}", lease=str(lease.pk)),
        post(kind="visit", body=f"Card {CARD}", lease=str(lease.pk)),
    ):
        response = client.post(store_url(store), data)
        assert response.status_code == 200
        page = response.content.decode()
        assert "card or account numbers" in dialog_of(page)
        assert CARD not in page and CARD.replace(" ", "") not in page
    assert not ContactEntry.objects.exists()


def test_the_note_and_the_sentence_keep_the_length_limit(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    longest = "x" * CONTACT_BODY_MAX_LENGTH
    response = client.post(
        store_url(store), post(call_outcome="answered", body=longest, lease=str(lease.pk))
    )
    assert response.status_code == 200
    assert escape(CALL_NOTE_TOO_LONG) in response.content.decode()
    response = client.post(
        store_url(store), post(kind="visit", body=longest + "x", lease=str(lease.pk))
    )
    assert response.status_code == 200 and not ContactEntry.objects.exists()
    fits = "x" * (CONTACT_BODY_MAX_LENGTH - len("Answered. "))
    response = client.post(
        store_url(store), post(call_outcome="answered", body=fits, lease=str(lease.pk))
    )
    assert response.status_code == 302
    assert len(ContactEntry.objects.get().body) == CONTACT_BODY_MAX_LENGTH


def test_after_saving_the_entry_is_first_in_the_timeline(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    assert lease.operator is not None
    contacts.add_entry(
        lease.operator.pk,
        contacts.ContactInput(
            kind=ContactKind.VISIT, happened_on=TODAY, body="Earlier visit.", lease=lease.pk
        ),
        actor=None,
    )
    response = client.post(
        store_url(store), post(call_outcome="left_message", lease=str(lease.pk)), follow=True
    )
    assert response.redirect_chain == [(f"/stores/{store.pk}/?tab=contacts#tab-contacts", 302)]
    page = response.content.decode()
    added = ContactEntry.objects.get(body="Left a message.")
    assert first_entry(page) == str(added.pk)
    assert 'id="log-contact-dialog"' not in page
    # From the operator page: back to its notes & follow-ups, the entry first there too.
    response = client.post(
        operator_url(lease.operator), post(call_outcome="answered", lease=str(lease.pk))
    )
    assert response["Location"] == f"/operators/{lease.operator.pk}/#contacts"
    newest = ContactEntry.objects.get(body="Answered.")
    assert first_entry(page_of(client, f"/operators/{lease.operator.pk}/")) == str(newest.pk)


def test_a_refusal_shows_the_dialog_again_over_the_page(today: date) -> None:
    store, lease = world()
    client, _ = client_for(Role.BILLING)
    response = client.post(store_url(store), post(kind="visit", lease=str(lease.pk)))
    page = response.content.decode()
    assert response.status_code == 200 and 'id="store-title"' in page
    dialog = dialog_of(page)
    head = dialog.split(">", 1)[0]
    assert " open " in head and head.endswith(" data-open")
    assert re.search(r'value="visit"[^>]*checked', dialog)


# --- Roles --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER])
def test_a_viewer_is_refused_and_sees_no_button(today: date, role: Role) -> None:
    store, lease = world()
    assert lease.operator is not None
    client, _ = client_for(role)
    for url in (store_url(store), operator_url(lease.operator)):
        assert client.get(url).status_code == 403, url
        assert client.post(url, post(call_outcome="answered")).status_code == 403, url
    assert "Log contact" not in page_of(client, f"/stores/{store.pk}/?tab=contacts")
    assert "Log contact" not in page_of(client, f"/operators/{lease.operator.pk}/")
    assert not ContactEntry.objects.exists()
