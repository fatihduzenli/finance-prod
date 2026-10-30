"""The operator workspace on the reference's frame (docs/qa/PLAN-012f-a-design-follow-up.md D7;
DESIGN-SPEC §4 "Operator workspace", operator-workspace-laptop.png).

The header (the avatar's initials, the name with Active or Archived and "Connected to Stax" only
with a provider customer, the muted line and its omissions), the actions by role, the Store
select (All stores by default, submit on change with a no-script Show), the strip of store cards
(rent and collection line, the balance in red or green, "· overdue"), the Charges card's title
per selection, the right column's cards (CONTACT, PAYMENT with the returns of this calendar year
in local time, RECENT CONTACT, the statement link) with their empty states, escaping, and the
page's query budget. Facts only: nothing here is stored by the page.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    CollectionSchedule,
    ContactEntry,
    ContactKind,
    Lease,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import statement_links
from ledger.services import workspace_cards as cards
from payments.models import ExternalRef, ProviderSnapshot
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_store_workspace import page, with_customer
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
OCTOBER = date(2026, 10, 1)
STARTED = date(2026, 9, 1)
MARKUP = '<img src=x onerror="alert(1)">'


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


def operator_page(operator: Operator, **query: str) -> str:
    url = reverse("ledger:operators:detail", kwargs={"pk": operator.pk})
    return url + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")


def by_id(html: str, element_id: str, tag: str = "section") -> str:
    found = re.search(rf'<{tag}[^>]* id="{element_id}"[^>]*>.*?</{tag}>', html, re.S)
    assert found is not None, element_id
    return found.group(0)


def header(html: str) -> str:
    start = html.index('id="page-header"')
    return html[start : html.index("</header>", start)]


def actions(html: str) -> str:
    found = re.search(r'<div class="page-actions" id="operator-actions">.*?\n  </div>', html, re.S)
    assert found is not None
    return found.group(0)


def three_stores() -> tuple[Operator, list[Lease]]:
    """Fatih Düzenli with leases of stores 1991, 1999 and 2475 (by label)."""
    operator = an_operator("Fatih Düzenli")
    leases = [
        a_lease(a_store(label, number=None), operator, started=STARTED, debit_day=1, rent=rent)
        for label, rent in (("1991", "2000.00"), ("1999", "1500.00"), ("2475", "2100.00"))
    ]
    return operator, leases


def link_method(operator: Operator, *, nickname: str = "Chase", last_four: str = "4421") -> None:
    """A linked bank method and the method list the worker's refresh stores (no provider call)."""
    with_customer(operator)
    ExternalRef.objects.create(
        entity_type="operator",
        entity_id=operator.pk,
        provider="stax_pay",
        external_kind="payment_method",
        external_id="pm-linked-1",
    )
    ref = ExternalRef.objects.get(
        entity_type="operator", entity_id=operator.pk, external_kind="customer"
    )
    ProviderSnapshot.objects.create(
        external_ref=ref,
        payload=[
            {
                "external_id": "pm-linked-1",
                "raw_status": "",
                "method": "bank",
                "last_four": last_four,
                "bank_type": "checking",
                "nickname": nickname,
                "is_default": True,
            }
        ],
        fetched_at=NOW,
    )


def an_entry(operator: Operator, kind: str, day: date, body: str, **fields: object) -> ContactEntry:
    return ContactEntry.objects.create(
        operator=operator, kind=kind, happened_on=day, body=body, **fields
    )


# --- The header ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Fatih Düzenli", "FD"),
        ("fatih", "F"),
        ("  Ana   de la  Cruz ", "AC"),
        ("élodie martin", "ÉM"),
        ("— 7 Eleven", "7E"),
        ("—", "?"),
        ("", "?"),
    ],
)
def test_the_initials_are_the_first_and_last_words_first_letters(name: str, expected: str) -> None:
    assert cards.initials(name) == expected


def test_the_header_has_the_avatar_the_name_the_active_badge_and_the_muted_line() -> None:
    operator, _ = three_stores()
    Operator.objects.filter(pk=operator.pk).update(
        phone="(201) 555-0142", email="fatih@example.com", sms_consent_at=NOW
    )
    html = page(client_for(Role.VIEWER)[0], operator_page(operator))
    top = header(html)
    assert '<span class="operator-avatar" id="operator-avatar" aria-hidden="true">FD</span>' in top
    title = by_id(top, "operator-title", "h1")
    assert "Fatih Düzenli" in title and "badge-paid" in title and "Active</span>" in title
    assert 'id="provider-connected"' not in top  # no provider customer
    line = by_id(top, "operator-line", "p")
    assert ">(201) 555-0142 · fatih@example.com · SMS consent given · 3 stores</p>" in line, line


def test_the_muted_line_leaves_out_what_is_missing_and_counts_one_store() -> None:
    operator = an_operator("Solo")
    a_lease(a_store("2477"), operator, started=STARTED)
    html = page(client_for(Role.VIEWER)[0], operator_page(operator))
    assert ">no SMS consent · 1 store</p>" in by_id(html, "operator-line", "p")
    nobody = an_operator("Nobody")
    html = page(client_for(Role.VIEWER)[0], operator_page(nobody))
    assert ">no SMS consent · 0 stores</p>" in by_id(html, "operator-line", "p")


def test_connected_to_the_provider_only_with_a_customer_and_archived_replaces_active() -> None:
    operator, _ = three_stores()
    viewer = client_for(Role.VIEWER)[0]
    assert "Connected to Stax" not in header(page(viewer, operator_page(operator)))
    with_customer(operator)
    top = header(page(viewer, operator_page(operator)))
    assert '<span class="provider-pill" id="provider-connected">Connected to Stax</span>' in top
    Operator.objects.filter(pk=operator.pk).update(archived_at=NOW)
    title = by_id(header(page(viewer, operator_page(operator))), "operator-title", "h1")
    assert "Archived Oct 15, 2026" in title and "Active" not in title


def test_a_name_with_markup_is_escaped_in_the_title_the_line_and_the_cards() -> None:
    operator = an_operator(f"Fatih {MARKUP}")
    Operator.objects.filter(pk=operator.pk).update(email="a@example.com", phone=MARKUP)
    a_lease(a_store(f"1991 {MARKUP}"), operator, started=STARTED)
    html = page(client_for(Role.ADMIN)[0], operator_page(operator))
    assert MARKUP not in html
    assert "&lt;img" in by_id(html, "operator-title", "h1")
    assert "&lt;img" in by_id(html, "operator-line", "p")
    assert "&lt;img" in by_id(html, "store-strip", "ul")
    assert '<span class="operator-avatar" id="operator-avatar" aria-hidden="true">FO</span>' in html


# --- The actions by role -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "links", "connect", "archive"),
    [
        (Role.VIEWER, ["Statement"], False, False),
        (Role.BILLING, ["Statement", "Edit"], True, False),
        (Role.ADMIN, ["Statement", "Edit"], True, True),
    ],
)
def test_the_actions_are_statement_edit_and_more_by_role(
    role: Role, links: list[str], connect: bool, archive: bool
) -> None:
    operator, _ = three_stores()
    html = page(client_for(role)[0], operator_page(operator))
    top = actions(html)
    assert re.findall(r'<a class="btn[^"]*"[^>]*>([^<]+)</a>', top) == links
    assert ('id="more-menu"' in top) == (connect or archive)
    assert ('id="connect-provider"' in top) == connect
    assert ('id="archive-operator"' in top) == archive
    # DESIGN-SPEC §4: no invoice or payment action and no primary in the operator's header.
    assert "btn-primary" not in header(html)
    for gone in ("/invoices/new", "/payments/new", 'id="new-invoice"'):
        assert gone not in top, gone


def test_an_archived_operator_has_the_statement_only() -> None:
    operator, _ = three_stores()
    Operator.objects.filter(pk=operator.pk).update(archived_at=NOW)
    top = actions(page(client_for(Role.ADMIN)[0], operator_page(operator)))
    assert re.findall(r'<a class="btn[^"]*"[^>]*>([^<]+)</a>', top) == ["Statement"]
    assert 'id="more-menu"' not in top


# --- The Store select ---------------------------------------------------------------------------


def test_the_store_select_sits_in_the_tab_row_and_submits_on_change() -> None:
    operator, leases = three_stores()
    html = page(client_for(Role.VIEWER)[0], operator_page(operator, tab="payments"))
    row = by_id(header(html), "operator-tabs-row", "div")
    assert row.index('id="store-tabs"') < row.index('id="store-selector"')
    form = by_id(row, "store-selector", "form")
    assert 'method="get"' in form
    assert '<option value="" selected>All stores (3)</option>' in form
    assert form.count("<option ") == 4
    assert '<input type="hidden" name="tab" value="payments">' in form
    assert '<button class="btn btn-sm" type="submit" id="store-show">Show</button>' in form
    assert 'select.addEventListener("change"' in html and "show.hidden = true" in html
    picked = page(
        client_for(Role.VIEWER)[0], operator_page(operator, store=str(leases[1].store_id))
    )
    assert f'<option value="{leases[1].store_id}" selected>1999</option>' in picked
    assert '<option value="">All stores (3)</option>' in picked


def test_an_operator_without_a_store_has_no_select_and_no_strip() -> None:
    operator = an_operator("Nobody")
    html = page(client_for(Role.VIEWER)[0], operator_page(operator))
    assert 'id="store-selector"' not in html and 'id="store-strip"' not in html
    assert 'id="no-stores"' in html


# --- The strip of store cards ------------------------------------------------------------------


def test_the_strip_has_one_card_per_store_with_rent_collection_and_balance() -> None:
    operator, (first, second, third) = three_stores()
    for lease in (second, third):
        CollectionSchedule.objects.create(
            lease=lease,
            kind=ScheduleKind.RENT,
            seq=1,
            debit_day=1,
            amount=D("1500.00"),
            state=ScheduleState.ACTIVE,
        )
    # First: $3,000.00 due Nov 1 (not yet overdue); second: paid up; third: overdue.
    a_one_off_charge(operator, "3000.00", date(2026, 11, 1), lease=first)
    a_rent_charge(third, OCTOBER, due="2100.00")
    other = a_lease(a_store("9009"), an_operator("Other"), started=STARTED)
    a_rent_charge(other, OCTOBER, due="4321.00")
    html = page(client_for(Role.VIEWER)[0], operator_page(operator))
    strip = by_id(html, "store-strip", "ul")
    assert strip.count('class="store-card"') == 3
    assert strip.index(">1991</a>") < strip.index(">1999</a>") < strip.index(">2475</a>")
    assert_absent(strip, "9009")
    assert "4,321.00" not in strip
    one, two, three = re.findall(r'<li class="store-card".*?</li>', strip, re.S)
    assert reverse("ledger:stores:detail", kwargs={"pk": first.store_id}) in one
    assert "$2,000.00 / month · autopay not set up" in one
    assert '<span class="store-card-balance tone-unpaid">$3,000.00 open</span>' in one
    assert "$1,500.00 / month · bank debit on the 1st" in two
    assert '<span class="store-card-balance tone-paid">$0.00 open</span>' in two
    assert '<span class="store-card-balance tone-unpaid">$2,100.00 open · overdue</span>' in three


def test_a_paused_or_failed_autopay_and_a_store_leased_to_another_say_so() -> None:
    operator, (first, second, third) = three_stores()
    CollectionSchedule.objects.create(
        lease=first, kind=ScheduleKind.RENT, seq=1, amount=D("1.00"), state=ScheduleState.PAUSED
    )
    CollectionSchedule.objects.create(
        lease=second, kind=ScheduleKind.RENT, seq=1, amount=D("1.00"), state=ScheduleState.FAILED
    )
    # An ended schedule is not autopay.
    CollectionSchedule.objects.create(
        lease=third, kind=ScheduleKind.RENT, seq=1, amount=D("1.00"), state=ScheduleState.ENDED
    )
    found = cards.store_cards(operator, [first.store, second.store, third.store], [], TODAY)
    assert [card.collect_line for card in found] == [
        "autopay paused",
        "autopay failed",
        "autopay not set up",
    ]
    # The store is now another operator's: no rent, no lease of this operator.
    first.ended_on = date(2026, 9, 30)
    first.save(update_fields=["ended_on"])
    a_lease(first.store, an_operator("Incoming"), started=date(2026, 10, 1), rent="9999.00")
    [card] = cards.store_cards(operator, [first.store], [], TODAY)
    assert card.rent is None and card.collect_line == cards.NO_LEASE
    html = page(client_for(Role.VIEWER)[0], operator_page(operator))
    assert "9,999.00" not in html


def test_the_selected_stores_card_is_marked() -> None:
    operator, leases = three_stores()
    html = page(client_for(Role.VIEWER)[0], operator_page(operator, store=str(leases[2].store_id)))
    strip = by_id(html, "store-strip", "ul")
    assert strip.count('aria-current="true"') == 1
    assert f'id="store-card-{leases[2].store_id}" aria-current="true"' in strip


# --- The Charges card ---------------------------------------------------------------------------


def test_the_charges_title_names_all_stores_or_the_selected_one() -> None:
    operator, (first, second, _) = three_stores()
    a_rent_charge(first, OCTOBER, due="1000.00")
    a_rent_charge(second, OCTOBER, due="250.00")
    client = client_for(Role.BILLING)[0]
    every = by_id(page(client, operator_page(operator)), "tab-charges")
    assert '<h2>Charges <span id="charges-scope">· all stores</span></h2>' in every
    assert "<th>Store</th>" in every and 'id="charges-balance">$1,250.00<' in every
    assert ">Open balance · all stores</th>" in every
    one = by_id(page(client, operator_page(operator, store=str(second.store_id))), "tab-charges")
    assert '<span id="charges-scope">· 1999</span>' in one
    assert 'id="charges-balance">$250.00<' in one and ">1991</a>" not in one
    # The filter links keep the selection.
    assert f"?store={second.store_id}&amp;tab=charges&amp;filter=paid" in one


def test_the_charges_title_is_the_store_pages_own_there() -> None:
    _, (first, _, _) = three_stores()
    html = page(
        client_for(Role.BILLING)[0], reverse("ledger:stores:detail", kwargs={"pk": first.store_id})
    )
    assert "<h2>Charges</h2>" in by_id(html, "tab-charges")


# --- CONTACT ---------------------------------------------------------------------------------


def test_the_contact_card_has_phone_email_and_the_sms_consent_badge() -> None:
    operator, _ = three_stores()
    viewer = client_for(Role.VIEWER)[0]
    card = by_id(page(viewer, operator_page(operator)), "contact-card")
    assert 'id="side-phone">—<' in card and 'id="side-email">—<' in card
    assert "badge-neutral" in card and "No consent" in card
    Operator.objects.filter(pk=operator.pk).update(
        phone="(201) 555-0142", email="fatih@example.com", sms_consent_at=NOW
    )
    card = by_id(page(viewer, operator_page(operator)), "contact-card")
    assert 'id="side-phone">(201) 555-0142<' in card
    assert 'id="side-email">fatih@example.com<' in card
    assert "badge-paid" in card and "Consent</span>" in card
    # No language is stored: the card has no Language row.
    assert "Language" not in card


# --- PAYMENT ---------------------------------------------------------------------------------


def test_the_payment_card_empty() -> None:
    operator, _ = three_stores()
    card = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "payment-card")
    assert "<dt>Stax</dt>" in card and 'id="side-customer">Not connected<' in card
    assert 'id="side-bank">—<' in card
    assert 'id="side-returns">0 this year<' in card
    assert 'id="side-promise">—<' in card


def test_the_payment_card_with_customer_bank_returns_and_promise() -> None:
    operator, (first, second, _) = three_stores()
    link_method(operator)
    returned = a_rent_charge(first, OCTOBER, due="2000.00")
    a_payment(returned, "2000.00", None, status=PaymentStatus.RETURNED)
    returned.payments.update(returned_at=datetime(2026, 10, 3, 14, 0, tzinfo=UTC))
    an_entry(
        operator,
        ContactKind.PROMISE,
        date(2026, 10, 12),
        "Will pay",
        lease=first,
        promised_date=date(2026, 11, 1),
        promised_amount=D("3000.00"),
    )
    card = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "payment-card")
    assert 'Customer · <a href="#operator-provider-card">open</a>' in card
    assert 'id="side-bank">Chase ····4421<' in card
    assert 'class="side-strong tone-unpaid">1 this year<' in card
    assert "$3,000.00 by Nov 1, 2026" in card and "promise-open" in card
    # No provider identifier reaches the card.
    assert "pm-linked-1" not in card and "cus-" not in card


def test_the_bank_row_without_a_nickname_reads_the_type_and_a_missing_record_is_a_dash() -> None:
    operator, _ = three_stores()
    link_method(operator, nickname="")
    card = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "payment-card")
    assert 'id="side-bank">checking ····4421<' in card
    ProviderSnapshot.objects.all().delete()  # the stored list no longer has the method
    card = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "payment-card")
    assert 'id="side-bank">—<' in card


def test_returns_count_this_calendar_year_in_local_time_this_operator_only() -> None:
    operator, (first, second, _) = three_stores()
    # 2026-01-01 03:00 UTC is Dec 31, 2025 in New York: last year. 05:30 UTC is this year.
    times = [
        datetime(2026, 1, 1, 3, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 5, 30, tzinfo=UTC),
        datetime(2026, 10, 3, 14, 0, tzinfo=UTC),
    ]
    for when in times:
        charge = a_rent_charge(first, date(2026, 1 + times.index(when), 1), due="100.00")
        a_payment(charge, "100.00", None, status=PaymentStatus.RETURNED)
        charge.payments.update(returned_at=when)
    # A settled payment, an unreturned one and another operator's return do not count.
    a_payment(a_rent_charge(second, OCTOBER, due="100.00"), "100.00", TODAY)
    theirs = a_lease(a_store("9009"), an_operator("Other"), started=STARTED)
    other = a_rent_charge(theirs, OCTOBER, due="100.00")
    a_payment(other, "100.00", None, status=PaymentStatus.RETURNED)
    other.payments.update(returned_at=NOW)
    # A one-off returned through the operator's one-off charge counts once.
    one_off = a_one_off_charge(operator, "50.00", OCTOBER, lease=first)
    Payment.objects.create(
        one_off_charge=one_off,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.ACH,
        amount=D("50.00"),
        status=PaymentStatus.RETURNED,
        initiated_on=TODAY,
        returned_at=NOW,
    )
    assert cards.returns_this_year(operator, TODAY) == 3
    assert cards.returns_this_year(operator, date(2025, 12, 31)) == 1


def test_the_promise_row_is_the_newest_open_promise_not_a_deleted_or_closed_one() -> None:
    operator, (first, _, _) = three_stores()
    _, user = client_for(Role.ADMIN)
    an_entry(
        operator,
        ContactKind.PROMISE,
        date(2026, 10, 14),
        "Deleted promise",
        promised_date=date(2026, 12, 1),
        promised_amount=D("999.00"),
        deleted_at=NOW,
        deleted_by=user,
        delete_reason="Typed twice",
    )
    an_entry(
        operator,
        ContactKind.PROMISE,
        date(2026, 10, 13),
        "Kept one",
        promised_date=date(2026, 10, 20),
        promised_amount=D("111.00"),
        status="kept",
    )
    an_entry(
        operator,
        ContactKind.PROMISE,
        date(2026, 10, 2),
        "Open one",
        promised_date=date(2026, 10, 30),
        promised_amount=D("222.00"),
    )
    card = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "payment-card")
    assert "$222.00 by Oct 30, 2026" in card
    assert "999.00" not in card and "111.00" not in card


# --- RECENT CONTACT ------------------------------------------------------------------------------


def test_recent_contact_lists_the_last_two_entries_not_deleted() -> None:
    operator, (first, second, _) = three_stores()
    _, admin_user = client_for(Role.ADMIN)
    an_entry(operator, ContactKind.CALL, date(2026, 10, 1), "Oldest call")
    an_entry(operator, ContactKind.CALL, date(2026, 10, 20), "no answer", lease=second)
    promise = an_entry(
        operator,
        ContactKind.PROMISE,
        date(2026, 10, 22),
        "Will pay",
        lease=first,
        promised_date=date(2026, 11, 1),
        promised_amount=D("3000.00"),
    )
    an_entry(
        operator,
        ContactKind.VISIT,
        date(2026, 10, 23),
        "Deleted visit",
        deleted_at=NOW,
        deleted_by=admin_user,
        delete_reason="Wrong operator",
    )
    html = page(client_for(Role.ADMIN)[0], operator_page(operator))
    card = by_id(html, "recent-contact-card")
    items = re.findall(r'<li class="recent-contact">.*?</li>', card, re.S)
    assert len(items) == 2
    assert "avatar-pending" in items[0] and f'href="#contact-{promise.pk}"' in items[0]
    assert "Promise to pay · $3,000.00 by Nov 1, 2026" in items[0]
    assert "Oct 22, 2026 · 1991" in items[0]
    assert "avatar-neutral" in items[1] and "Call · no answer" in items[1]
    assert "Oct 20, 2026 · 1999" in items[1]
    assert "Oldest call" not in card and "Deleted visit" not in card
    contacts_url = reverse("ledger:operators:contacts", kwargs={"pk": operator.pk})
    assert f'<a id="recent-all-contacts" href="{contacts_url}">All entries</a>' in card
    # The entry the card links to is on the page (the Contact log card).
    assert f'id="contact-{promise.pk}"' in html


def test_recent_contact_add_entry_is_billings_and_the_empty_state() -> None:
    operator, _ = three_stores()
    add = reverse("ledger:operators:contacts_add", kwargs={"pk": operator.pk})
    billing = by_id(
        page(client_for(Role.BILLING)[0], operator_page(operator)), "recent-contact-card"
    )
    assert f'id="recent-add-contact" href="{add}"' in billing
    assert 'id="no-recent-contact">No entries yet.<' in billing
    viewer = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "recent-contact-card")
    assert "recent-add-contact" not in viewer
    Operator.objects.filter(pk=operator.pk).update(archived_at=NOW)
    archived = by_id(
        page(client_for(Role.ADMIN)[0], operator_page(operator)), "recent-contact-card"
    )
    assert "recent-add-contact" not in archived


def test_a_long_note_is_cut_and_markup_escaped_in_recent_contact() -> None:
    operator, _ = three_stores()
    an_entry(operator, ContactKind.CALL, TODAY, f"{MARKUP} " + "x" * 80)
    card = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "recent-contact-card")
    assert MARKUP not in card and "&lt;img" in card and "…" in card
    assert "x" * 80 not in card


# --- PORTAL (the statement link) ------------------------------------------------------------------


def test_the_portal_card_reads_the_statement_link_state_and_billing_cancels() -> None:
    operator, _ = three_stores()
    billing = by_id(page(client_for(Role.BILLING)[0], operator_page(operator)), "portal-card")
    assert '<h2 class="section-label" id="portal-card-heading">Portal</h2>' in billing
    assert "Statement link: each collection e-mail carries a link" in billing
    assert "for 30 days" in billing
    assert 'id="statement-link-opened">Never<' in billing
    revoke = reverse("ledger:operators:statement_links_revoke", kwargs={"pk": operator.pk})
    assert f'action="{revoke}"' in billing and 'id="statement-links-revoke-form"' in billing
    viewer = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "portal-card")
    assert "statement-links-revoke-form" not in viewer
    statement_links.record_open(operator)
    Operator.objects.filter(pk=operator.pk).update(statement_links_revoked_at=NOW)
    card = by_id(page(client_for(Role.VIEWER)[0], operator_page(operator)), "portal-card")
    assert 'id="statement-link-opened">Oct 15, 2026 · 12:00<' in card
    assert "Links sent before Oct 15, 2026 · 12:00 were revoked." in card
    # Another operator's open is not this one's.
    other = an_operator("Other")
    statement_links.record_open(other)
    assert cards.statement_link_opened(an_operator("Never opened")) is None
    # The page never shows a token.
    assert statement_links.make_token(operator) not in card


# --- The right column's order and the selected store -------------------------------------------


def test_the_side_column_lists_the_operators_cards_then_the_selected_stores() -> None:
    operator, leases = three_stores()
    client = client_for(Role.BILLING)[0]
    html = page(client, operator_page(operator))
    side = html[html.index('<aside class="workspace-side operator-side"') :]
    labels = re.findall(r'<h2 class="section-label"[^>]*>([^<]+)</h2>', side)
    assert labels == ["Contact", "Payment", "Recent contact", "Portal"]
    html = page(client, operator_page(operator, store=str(leases[0].store_id)))
    side = html[html.index('<aside class="workspace-side operator-side"') :]
    labels = re.findall(r'<h2 class="section-label[^"]*"[^>]*>([^<]+)</h2>', side)
    assert labels == [
        "Contact",
        "Payment",
        "Recent contact",
        "Portal",
        "1991",
        "Lease",
        "Next",
        "Store cost",
        "Documents",
    ]


# --- The budget ----------------------------------------------------------------------------------


@pytest.mark.parametrize("selected", [False, True])
def test_the_page_renders_within_a_fixed_number_of_queries(selected: bool) -> None:
    operator = an_operator("Budget Ops")
    leases = [
        a_lease(a_store(f"70{n}"), operator, started=STARTED, channel=PaymentChannel.WIRE)
        for n in range(3)
    ]
    link_method(operator)
    client = client_for(Role.ADMIN)[0]
    query = {"store": str(leases[0].store_id)} if selected else {}

    def render() -> int:
        with CaptureQueriesContext(connection) as queries:
            page(client, operator_page(operator, **query))
        return len(queries)

    for lease in leases:
        charge = a_rent_charge(lease, OCTOBER, due="1000.00")
        a_one_off_charge(operator, "20.00", OCTOBER, lease=lease)
        a_payment(charge, "10.00", None, status=PaymentStatus.RETURNED)
        an_entry(operator, ContactKind.CALL, TODAY, "Called", lease=lease)
    small = render()
    for lease in leases:
        CollectionSchedule.objects.create(
            lease=lease, kind=ScheduleKind.RENT, seq=1, amount=D("1.00"), state=ScheduleState.ACTIVE
        )
        for month in range(1, 9):
            more = a_rent_charge(lease, date(2026, month, 1), due="100.00")
            a_payment(more, "10.00", None, status=PaymentStatus.RETURNED)
            a_one_off_charge(operator, "20.00", date(2026, month, 3), lease=lease)
            an_entry(operator, ContactKind.CALL, date(2026, month, 2), "Called", lease=lease)
        statement_links.record_open(operator)
    for store in (a_store(f"71{n}") for n in range(3)):
        a_lease(store, operator, started=STARTED)
    large = render()
    # Measured (sqlite, a linked bank method): 51 with All stores, 62 with a store selected
    # (the page read 52 before D7, with the first store selected by default and no method).
    # The operator's cards read the store cards' leases, rents and schedules (3), the bank
    # record (3), the returns count (1), the open promise with its prefetch (2) and the
    # statement link's last open (1); the selected store adds its bar and side cards. Three
    # more stores change nothing. PLAN-012f-b Batch D (D-3) adds one: Needs review's rule for
    # plans paused over thirty days reads the operator's paused plans (one query, no audit
    # read while none is paused): 52 and 63. PLAN-012f-c Batch A (D3) adds one: Needs
    # review's rule for voided payments whose invoice stays marked paid reads the void audit
    # rows (one query, nothing more while there is none): 53 and 64. PLAN-012g Batch A (A2) adds
    # one, the theme row: 54 and 65.
    assert large == small, (small, large)
    # PLAN-014 B3: +2 with a store selected (the sign fee, the insurance charges and their
    # balances for the LEASE card): 67.
    assert small <= (67 if selected else 54), small


def test_the_page_writes_nothing() -> None:
    operator, _ = three_stores()
    link_method(operator)
    client = client_for(Role.ADMIN)[0]
    with CaptureQueriesContext(connection) as queries:
        page(client, operator_page(operator))
    writes = [
        query["sql"]
        for query in queries.captured_queries
        if re.match(r"\s*(INSERT|UPDATE|DELETE)", query["sql"], re.I)
        # The sign-in session's own bookkeeping (every page does it).
        and not re.search(r'"(django_session|accounts_usersession)"', query["sql"])
    ]
    assert writes == [], writes


def test_an_unknown_uuid_for_the_store_is_all_stores() -> None:
    operator, _ = three_stores()
    html = page(client_for(Role.VIEWER)[0], operator_page(operator, store=str(uuid.uuid4())))
    assert '<option value="" selected>All stores (3)</option>' in html
    assert '<span id="charges-scope">· all stores</span>' in html
    assert timezone.now() == NOW
