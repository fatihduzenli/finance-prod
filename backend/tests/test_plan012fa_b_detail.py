"""PLAN-012f-a Batch B, slice B2 (docs/qa/PLAN-012f-a-design-follow-up.md D4; DESIGN-SPEC §4
Charge detail): the one-off charge page and the rent charge page as the drawing's detail page, and
the frame of their siblings (the payment, the sent invoice, the lease and the calendar entry).

The header's muted line names the parts that exist; the Amount, Paid and Balance tiles (the
balance in the unpaid red while open); the Invoices card's subline and the provider's status as a
teal pill whose "view" opens the builder's page (billing); the empty Payments card and the table;
the Activity card newest first, at most twenty rows with "All activity" for an admin; the DETAILS
fields; the open promise card; the More menu and the MORE card by role; each sibling's tiles and
cards; the query budget of each page, constant as its rows grow.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.roles import Role
from finance.dates import day_text
from ledger.models import (
    AuditLog,
    CalendarEntry,
    ContactKind,
    FeeType,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceLineItem,
    Lease,
    OneOffCategory,
    OneOffCharge,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import contacts, detail_cards
from payments import invoice_sync
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_invoice,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
DUE = date(2026, 11, 1)


# --- The world -----------------------------------------------------------------------------


class World:
    operator: Operator
    lease: Lease
    deposit: OneOffCharge
    invoice: Invoice
    draft: InvoiceDraft
    rent: RentCharge


def a_world(*, promise: bool = True) -> World:
    """The drawing's data: #1991 Istanbul leased to fatih; the $3,000.00 security deposit billed
    on a two-line builder invoice (memo "move-in") that the provider shows as SENT; a promise of
    $3,000.00 by Nov 1; a rent charge of Oct 2026."""
    world = World()
    world.operator = an_operator("fatih")
    world.lease = a_lease(
        a_store("Istanbul", 1991),
        world.operator,
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    world.deposit = a_one_off_charge(
        world.operator,
        "3000.00",
        DUE,
        lease=world.lease,
        fee_type=FeeType.objects.get(code="security_deposit"),
        category=OneOffCategory.SETUP,
        collection="email",
        description="Security deposit",
        cost=None,
        note="Line 2 of the move-in invoice",
    )
    world.invoice = an_invoice(
        world.operator,
        9,
        "6000.00",
        one_off_charge=world.deposit,
        memo="move-in",
        issue_date=date(2026, 10, 23),
        due_date=date(2026, 11, 7),
    )
    # The provider's status is written by the sync layer only (Rule 2; a trigger on PostgreSQL).
    invoice_sync.record_provider_status(world.invoice.pk, "SENT")
    world.invoice.refresh_from_db()
    for position in (1, 2):
        InvoiceLineItem.objects.create(
            invoice=world.invoice,
            position=position,
            description=f"Line {position}",
            quantity=D("1"),
            unit_price=D("3000.00"),
            line_total=D("3000.00"),
            one_off_charge=world.deposit if position == 1 else None,
        )
    world.draft = InvoiceDraft.objects.create(
        operator=world.operator,
        lease=world.lease,
        memo="move-in",
        status=InvoiceDraftStatus.SENT,
        sent_invoice=world.invoice,
    )
    if promise:
        added = contacts.add_entry(
            world.operator.pk,
            contacts.ContactInput(
                kind=ContactKind.PROMISE,
                happened_on=date(2026, 10, 22),
                body="Will pay the deposit by the first.",
                promised_date=DUE,
                promised_amount=D("3000.00"),
                lease=world.lease.pk,
            ),
            actor=None,
        )
        assert added.ok, added.message
    world.rent = a_rent_charge(
        world.lease, date(2026, 10, 1), status=RentChargeStatus.UNPAID, due="2000.00"
    )
    return world


def one_off_path(charge: OneOffCharge) -> str:
    return reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk})


def rent_path(charge: RentCharge) -> str:
    return reverse("ledger:charges:detail", kwargs={"pk": charge.pk})


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def between(html: str, start: str, end: str) -> str:
    assert start in html, start
    return html.split(start, 1)[1].split(end, 1)[0]


def header_of(html: str) -> str:
    return between(html, 'id="page-header"', "</header>")


def text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def audit_row(kind: str, pk: object, action: str, actor: User | None, note: str = "") -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=kind,
        entity_id=str(pk),
        after={"note": note} if note else {"amount": "1.00"},
    )


# --- The header ----------------------------------------------------------------------------


def test_the_header_line_names_store_operator_creation_source_and_due_day() -> None:
    world = a_world()
    header = header_of(page(client_for(Role.VIEWER)[0], one_off_path(world.deposit)))
    created = day_text(world.deposit.created_at)
    assert (
        f'<p class="store-line" id="detail-line">#1991 Istanbul · fatih · created {created} '
        "from the invoice builder · due Nov 1, 2026</p>"
    ) in header
    # The breadcrumb: Stores › #1991 › Charges › the charge, Charges opening the store's tab.
    crumb = between(header, 'id="breadcrumb"', "</nav>")
    assert f'href="/stores/{world.lease.store_id}/?tab=charges#tab-charges"' in crumb
    assert text(crumb).endswith("Stores › #1991 › Charges › Security deposit")
    # The status badge and the kind badge.
    title = between(header, "<h1", "</h1>")
    assert 'id="charge-status"><span class="badge badge-unpaid">' in title and "Owed" in title
    assert '<span class="badge kind-badge" id="charge-kind">Deposit · refundable</span>' in title


def test_the_header_line_leaves_out_what_is_unknown() -> None:
    operator = an_operator("Solo Co")
    charge = a_one_off_charge(operator, "50.00", DUE, description="Sign repair")
    header = header_of(page(client_for(Role.VIEWER)[0], one_off_path(charge)))
    created = day_text(charge.created_at)
    line = between(header, 'id="detail-line">', "</p>")
    assert line == f"Solo Co · created {created} · due Nov 1, 2026"
    assert " from " not in line and "#" not in line
    assert detail_cards.header_line() == ""
    assert detail_cards.header_line(store="#1", due=DUE) == "#1 · due Nov 1, 2026"
    assert (
        detail_cards.header_line(operator="x", created=date(2026, 1, 2), source="a plan")
        == "x · created Jan 2, 2026 from a plan"
    )


def test_the_sources_come_from_existing_fields() -> None:
    operator = an_operator("Src Co")
    legacy = a_one_off_charge(operator, "1.00", DUE, legacy=True)
    # Read from the field alone (a saved re-debit needs its returned payment).
    redebit = OneOffCharge(operator=operator, category=OneOffCategory.REDEBIT, price=D("1.00"))
    plain = a_one_off_charge(operator, "1.00", DUE)
    assert detail_cards.one_off_source(legacy, []) == detail_cards.SOURCE_SPREADSHEET
    assert detail_cards.one_off_source(redebit, []) == detail_cards.SOURCE_RETURN
    assert detail_cards.one_off_source(plain, []) == ""
    world = a_world(promise=False)
    assert detail_cards.rent_source(world.rent) == ""
    world.rent.billed_by_invoice = world.invoice
    assert detail_cards.rent_source(world.rent) == detail_cards.SOURCE_BUILDER


def test_the_kind_badge_reads_the_fee_type_and_the_resolution() -> None:
    world = a_world(promise=False)
    assert detail_cards.kind_words(world.deposit, None) == "Deposit · refundable"
    world.deposit.amount_paid = D("10.00")
    assert detail_cards.kind_words(world.deposit, None) == "Deposit · held, refundable"
    damage = a_one_off_charge(world.operator, "5.00", DUE)
    assert detail_cards.kind_words(damage, None) == "Damage"


# --- Tiles, invoices, payments -------------------------------------------------------------


def test_the_tiles_show_amount_paid_and_an_open_balance_in_red() -> None:
    world = a_world()
    html = page(client_for(Role.VIEWER)[0], one_off_path(world.deposit))
    tiles = between(html, 'id="detail-tiles"', "</section>")
    assert '<span class="kpi-label">Amount</span><span class="kpi-value">$3,000.00</span>' in tiles
    assert '<span class="kpi-label">Paid</span><span class="kpi-value">$0.00</span>' in tiles
    assert (
        '<span class="kpi-label">Balance</span><span class="kpi-value tone-unpaid">$3,000.00</span>'
    ) in tiles
    OneOffCharge.objects.filter(pk=world.deposit.pk).update(amount_paid=D("3000.00"), status="paid")
    tiles = between(
        page(client_for(Role.VIEWER)[0], one_off_path(world.deposit)),
        'id="detail-tiles"',
        "</section>",
    )
    assert '<span class="kpi-value">$0.00</span>' in tiles and "tone-unpaid" not in tiles


def test_the_invoices_card_names_the_memo_lines_and_the_provider_status_as_a_pill() -> None:
    world = a_world()
    billing = page(client_for(Role.BILLING)[0], one_off_path(world.deposit))
    card = between(billing, 'id="invoices-card"', "</section>")
    assert '<span class="cell-title">INV-2026-0009</span>' in card
    assert '<span class="cell-subline">move-in · 2 lines</span>' in card
    assert "Oct 23, 2026" in card and "Nov 7, 2026" in card and "$6,000.00" in card
    view = reverse("ledger:invoices:draft", kwargs={"pk": world.draft.pk})
    assert f'<span class="provider-pill">SENT · <a href="{view}">view</a></span>' in card
    viewer = between(
        page(client_for(Role.VIEWER)[0], one_off_path(world.deposit)),
        'id="invoices-card"',
        "</section>",
    )
    assert '<span class="provider-pill">SENT</span>' in viewer and view not in viewer
    rows = detail_cards.invoice_rows(world.deposit, "Setup")
    assert [(row.subline, row.draft_id) for row in rows] == [("move-in · 2 lines", world.draft.pk)]


def test_an_invoice_without_memo_or_lines_reads_the_kind() -> None:
    world = a_world(promise=False)
    an_invoice(world.operator, 10, "2000.00", rent_charge=world.rent)
    [row] = detail_cards.invoice_rows(world.rent, "rent")
    assert row.subline == "rent" and row.draft_id is None


def test_the_payments_card_is_empty_with_its_next_step_or_a_table() -> None:
    world = a_world()
    billing = page(client_for(Role.BILLING)[0], one_off_path(world.deposit))
    card = between(billing, 'id="payments-card"', "</section>")
    assert 'id="payments-empty"' in card and "No payment yet." in card
    pay = reverse("ledger:one_off_charges:payment", kwargs={"pk": world.deposit.pk})
    assert (
        f'<a href="{pay}" id="payments-empty-add">Add a payment</a> · or wait for the invoice'
        in (card)
    )
    assert 'class="icon icon-banknote"' in card and 'id="payments"' not in card
    viewer = between(
        page(client_for(Role.VIEWER)[0], one_off_path(world.deposit)),
        'id="payments-card"',
        "</section>",
    )
    assert "Wait for the invoice" in viewer and pay not in viewer
    Payment.objects.create(
        one_off_charge=world.deposit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.CHECK,
        note="Check 1001",
        amount=D("100.00"),
        status=PaymentStatus.SETTLED,
        initiated_on=date(2026, 10, 2),
        settled_on=date(2026, 10, 2),
    )
    card = between(
        page(client_for(Role.VIEWER)[0], one_off_path(world.deposit)),
        'id="payments-card"',
        "</section>",
    )
    assert '<table id="payments"' in card and 'id="payments-empty"' not in card
    assert "$100.00" in card and "Oct 2, 2026" in card


# --- Activity --------------------------------------------------------------------------------


def test_the_activity_card_lists_the_charges_rows_newest_first_with_note_and_user() -> None:
    world = a_world()
    _, user = client_for(Role.BILLING, "fatih-user")
    audit_row("one_off_charge", world.deposit.pk, "one_off_charge.created", user)
    audit_row("invoice", world.invoice.pk, "invoice.sent", user, note="move-in invoice")
    audit_row("rent_charge", world.rent.pk, "rent_charge.created", user)  # another charge
    html = page(client_for(Role.VIEWER)[0], one_off_path(world.deposit))
    card = between(html, 'id="activity-card"', "</section>")
    rows = re.findall(r'<li class="activity-row">(.*?)</li>', card, re.S)
    assert len(rows) == 2
    assert text(rows[0]).endswith("Invoice sent by fatih-user · move-in invoice")
    assert text(rows[1]).endswith("Charge created by fatih-user")
    assert '<strong class="activity-action">Invoice sent</strong>' in rows[0]
    assert '<q class="activity-note">move-in invoice</q>' in rows[0]
    assert '<strong class="activity-action">Charge created</strong>' in rows[1]
    assert re.search(
        r'<span class="activity-time">[A-Z][a-z]{2} \d{1,2}, 2026 · \d\d:\d\d</span>', rows[0]
    )
    assert "activity-all" not in card


def test_the_activity_card_is_capped_at_twenty_with_all_activity_for_an_admin() -> None:
    world = a_world(promise=False)
    for number in range(detail_cards.ACTIVITY_SIZE + 3):
        audit_row("rent_charge", world.rent.pk, f"rent_charge.step_{number:02d}", None)
    admin = page(client_for(Role.ADMIN)[0], rent_path(world.rent))
    card = between(admin, 'id="activity-card"', "</section>")
    actions = re.findall(r'<strong class="activity-action">(.*?)</strong>', card)
    assert len(actions) == detail_cards.ACTIVITY_SIZE
    assert actions[0] == "Charge step 22" and actions[-1] == "Charge step 03"
    history = f"/stores/{world.lease.store_id}/?tab=history#tab-history"
    assert f'<a class="card-link" id="activity-all" href="{history}">All activity</a>' in card
    billing = between(
        page(client_for(Role.BILLING)[0], rent_path(world.rent)), 'id="activity-card"', "</section>"
    )
    assert "activity-all" not in billing and 'id="activity-newest">The newest 20<' in billing


def test_an_empty_activity_card_says_so() -> None:
    world = a_world(promise=False)
    card = between(
        page(client_for(Role.VIEWER)[0], rent_path(world.rent)), 'id="activity-card"', "</section>"
    )
    assert 'id="activity-empty"' in card


def test_the_action_words() -> None:
    assert detail_cards.action_words("one_off_charge.payment_recorded") == "Charge payment recorded"
    assert detail_cards.action_words("payment.created_from_provider") == (
        "Payment created from provider"
    )
    assert detail_cards.action_words("lease") == "Lease"
    assert detail_cards.action_words("odd_thing.done") == "Odd thing done"


def test_an_identifier_shaped_note_is_masked() -> None:
    world = a_world(promise=False)
    audit_row("rent_charge", world.rent.pk, "rent_charge.linked", None, note="abcd1234efgh5678")
    card = between(
        page(client_for(Role.VIEWER)[0], rent_path(world.rent)), 'id="activity-card"', "</section>"
    )
    assert "abcd1234efgh5678" not in card


# --- The right column --------------------------------------------------------------------------


def test_the_details_card_holds_the_charges_fields() -> None:
    world = a_world()
    charge = a_one_off_charge(
        world.operator,
        "250.00",
        DUE,
        lease=world.lease,
        cost=D("100.00"),
        vendor="Sign Co",
        note="Front sign",
    )
    html = page(client_for(Role.VIEWER)[0], one_off_path(charge))
    side = between(html, '<aside class="detail-side"', "</aside>")
    details = between(side, 'id="charge-summary"', "</section>")
    assert '<h2 class="section-label">Details</h2>' in details
    for label, value in (
        ("Category", "Damage"),
        ("Collected by", "Manual payment"),
        ("Due", "Nov 1, 2026"),
        ("Vendor", "Sign Co"),
        ("Cost", "$100.00"),
        ("Price", "$250.00"),
        ("Internal note", "Front sign"),
    ):
        assert re.search(rf"<dt>{label}</dt><dd[^>]*>{re.escape(value)}</dd>", details), label


def test_the_promise_card_shows_the_open_promise_and_its_outcomes_for_billing() -> None:
    world = a_world()
    billing = page(client_for(Role.BILLING)[0], one_off_path(world.deposit))
    card = between(billing, 'id="promise-card"', "</section>")
    assert "Open promise" in card and 'id="promise-text">$3,000.00 by Nov 1, 2026</span>' in card
    for word in ("Kept", "Reschedule", "Broken"):
        assert f">{word}</a>" in card, word
    viewer = page(client_for(Role.VIEWER)[0], one_off_path(world.deposit))
    card = between(viewer, 'id="promise-card"', "</section>")
    assert "$3,000.00 by Nov 1, 2026" in card and "Kept" not in card
    # The rent charge of the same store shows it too.
    assert 'id="promise-card"' in page(client_for(Role.VIEWER)[0], rent_path(world.rent))


def test_no_promise_card_without_an_open_promise_or_an_open_balance() -> None:
    world = a_world(promise=False)
    viewer = client_for(Role.VIEWER)[0]
    assert 'id="promise-card"' not in page(viewer, one_off_path(world.deposit))
    other = a_one_off_charge(world.operator, "20.00", DUE, lease=world.lease)
    OneOffCharge.objects.filter(pk=other.pk).update(amount_paid=D("20.00"), status="paid")
    added = contacts.add_entry(
        world.operator.pk,
        contacts.ContactInput(
            kind=ContactKind.PROMISE,
            happened_on=date(2026, 10, 22),
            body="Will pay.",
            promised_date=DUE,
            promised_amount=D("3000.00"),
            lease=world.lease.pk,
        ),
        actor=None,
    )
    assert added.ok, added.message
    assert 'id="promise-card"' in page(viewer, one_off_path(world.deposit))
    # A paid charge does not show the store's promise.
    assert 'id="promise-card"' not in page(viewer, one_off_path(other))


def test_the_more_card_and_menu_follow_the_role() -> None:
    world = a_world(promise=False)
    path = one_off_path(world.deposit)
    viewer = page(client_for(Role.VIEWER)[0], path)
    assert 'id="more-card"' not in viewer and 'id="more-menu"' not in viewer
    assert 'id="charge-actions"' not in viewer
    billing = page(client_for(Role.BILLING)[0], path)
    header = header_of(billing)
    assert header.count("btn-primary") == 1 and 'id="record-payment"' in header
    # Split into installments sits in the header where it is offered (the rent charge).
    rent_header = header_of(page(client_for(Role.BILLING)[0], rent_path(world.rent)))
    assert 'id="split-installments"' in rent_header and rent_header.count("btn-primary") == 1
    more = between(billing, 'id="more-card"', "</section>")
    assert 'id="more-cancel-charge"' in more and 'class="detail-danger"' in more
    assert "/waive/" not in billing  # admin only
    admin = page(client_for(Role.ADMIN)[0], path)
    assert 'id="more-waive-charge"' in admin and 'id="waive-charge"' in header_of(admin)
    # The menu and the card hold the same URLs.
    menu = between(header_of(admin), 'id="more-menu"', "</details>")
    card = between(admin, 'id="more-card"', "</section>")
    assert sorted(re.findall(r'href="([^"]+)"', menu)) == sorted(
        re.findall(r'href="([^"]+)"', card.split('id="close-actions"')[0])
    )


def test_a_viewer_sees_no_action_on_the_rent_charge_and_billing_no_void() -> None:
    world = a_world(promise=False)
    viewer = page(client_for(Role.VIEWER)[0], rent_path(world.rent))
    for action in ("/payments/new", "record_payment", "/split/", "/link-invoice/", "more-card"):
        assert action not in viewer, action
    payment = a_payment(world.rent, "100.00", date(2026, 10, 2))
    detail = reverse("ledger:payments:detail", kwargs={"pk": payment.pk})
    billing = page(client_for(Role.BILLING)[0], detail)
    assert "/void/" not in billing and 'id="more-card"' not in billing


# --- The siblings ----------------------------------------------------------------------------


def frame(html: str) -> tuple[str, str]:
    """The main column and the side column; both exist, DETAILS heads the side."""
    assert '<div class="detail-grid">' in html
    side = between(html, '<aside class="detail-side"', "</aside>")
    assert '<h2 class="section-label">Details</h2>' in side
    return between(html, '<div class="detail-main">', '<aside class="detail-side"'), side


def tile_labels(html: str) -> list[str]:
    tiles = between(html, 'id="detail-tiles"', "</section>")
    return re.findall(r'<span class="kpi-label">(.*?)</span>', tiles)


def test_the_payment_page_has_the_family_frame() -> None:
    world = a_world(promise=False)
    payment = a_payment(world.rent, "500.00", date(2026, 10, 2))
    html = page(
        client_for(Role.ADMIN)[0], reverse("ledger:payments:detail", kwargs={"pk": payment.pk})
    )
    main, side = frame(html)
    assert tile_labels(html) == ["Amount", "Refunded", "Remaining"]
    for card in (
        'id="refunds-card"',
        'id="invoice-card"',
        'id="charge-card"',
        'id="activity-card"',
    ):
        assert card in main, card
    assert "<dt>Method</dt><dd>Bank debit (ACH)</dd>" in side and "<dt>Reference</dt>" in side
    assert 'id="more-void-payment"' not in side  # a settled ACH payment is refunded, not voided
    crumb = between(header_of(html), 'id="breadcrumb"', "</nav>")
    assert text(crumb).endswith("Stores › #1991 › Payments › Payment of $500.00")


def test_the_sent_invoice_page_has_the_family_frame() -> None:
    world = a_world(promise=False)
    html = page(
        client_for(Role.BILLING)[0], reverse("ledger:invoices:draft", kwargs={"pk": world.draft.pk})
    )
    main, side = frame(html)
    assert tile_labels(html) == ["Total", "Paid", "Balance"]
    assert 'id="sent-invoice-card"' in main and 'id="activity-card"' in main
    assert '<dd id="sent-provider-status">SENT</dd>' in side and 'id="draft-total"' in side
    assert "move-in" in side


def test_the_lease_page_has_the_family_frame() -> None:
    world = a_world(promise=False)
    html = page(
        client_for(Role.ADMIN)[0], reverse("ledger:leases:detail", kwargs={"pk": world.lease.pk})
    )
    main, side = frame(html)
    assert tile_labels(html) == ["Rent", "Open balance", "Deposit"]
    tiles = between(html, 'id="detail-tiles"', "</section>")
    assert "$3,500.00" in tiles  # the rent in effect
    # The open balance as the receivables read it (the store page's figure): the rent charge.
    assert '<span class="kpi-value tone-unpaid">$2,000.00</span>' in tiles
    assert "$3,000.00 · not collected" in tiles
    for card in ('id="rent-history-card"', 'id="autopay-card"', 'id="activity-card"'):
        assert card in main, card
    assert 'id="end-lease"' in header_of(html) and 'id="more-end-lease"' in side


def test_the_calendar_entry_page_has_the_family_frame() -> None:
    entry = CalendarEntry.objects.create(kind="note", on=date(2026, 10, 22), title="Inspect")
    html = page(
        client_for(Role.VIEWER)[0], reverse("ledger:calendar:detail", kwargs={"pk": entry.pk})
    )
    main, side = frame(html)
    assert 'id="activity-card"' in main and "<dt>Kind</dt>" in side


# --- Query budgets --------------------------------------------------------------------------

#: The queries of each page for a warm client, measured on this world before and after this
#: slice (PLAN-012f-a D4): one-off 35 → 37 (the open promise, the Activity rows), rent 28 → 28
#: (the same two, less the invoices read twice), payment 17 → 17 (the Activity rows replace the
#: audit rows), sent invoice 25 → 26 (the Activity rows), lease 24 → 30 (the receivables read
#: of the Open balance tile, four to six queries, and the Activity rows), calendar entry
#: 10 → 11 (the Activity rows). None grows with the rows. PLAN-012f-b §10 D19 (slice C2):
#: rent 28 → 30 (the open bank debit shown beside the payments, and the split refusal's
#: open-debit guard).
BUDGETS = {
    "one_off": 37,
    "rent": 30,
    "payment": 17,
    "invoice": 26,
    "lease": 30,
    "calendar": 11,
}


def deposit_payment(charge: OneOffCharge, day: int) -> Payment:
    return Payment.objects.create(
        one_off_charge=charge,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.CHECK,
        note=f"Check {day}",
        amount=D("1.00"),
        status=PaymentStatus.SETTLED,
        initiated_on=date(2026, 10, day),
        settled_on=date(2026, 10, day),
    )


def count(client: Client, path: str) -> int:
    with CaptureQueriesContext(connection) as queries:
        response = client.get(path)
    assert response.status_code == 200, path
    return len(queries)


def test_each_detail_page_keeps_its_query_budget_as_its_rows_grow() -> None:
    world = a_world()
    payment = a_payment(world.rent, "100.00", date(2026, 10, 2))
    entry = CalendarEntry.objects.create(
        kind="note", on=date(2026, 10, 22), title="Inspect", store=world.lease.store
    )
    paths = {
        "one_off": one_off_path(world.deposit),
        "rent": rent_path(world.rent),
        "payment": reverse("ledger:payments:detail", kwargs={"pk": payment.pk}),
        "invoice": reverse("ledger:invoices:draft", kwargs={"pk": world.draft.pk}),
        "lease": reverse("ledger:leases:detail", kwargs={"pk": world.lease.pk}),
        "calendar": reverse("ledger:calendar:detail", kwargs={"pk": entry.pk}),
    }
    client = client_for(Role.ADMIN)[0]
    # One settled payment on each charge before the first count, so that the rules that stop
    # at the first settled payment read the same rows in both counts.
    deposit_payment(world.deposit, 2)
    for path in paths.values():  # warm the session and what a first visit reads once
        page(client, path)
    small = {name: count(client, path) for name, path in paths.items()}
    for day in range(3, 9):
        a_payment(world.rent, "10.00", date(2026, 10, day))
        deposit_payment(world.deposit, day)
        a_rent_charge(world.lease, date(2026, day, 1), due="100.00")
    for number in range(30):
        for kind, pk in (
            ("one_off_charge", world.deposit.pk),
            ("rent_charge", world.rent.pk),
            ("payment", payment.pk),
            ("invoice", world.invoice.pk),
            ("lease", world.lease.pk),
            ("calendar_entry", entry.pk),
        ):
            audit_row(kind, pk, f"{kind}.step_{number}", None)
    large = {name: count(client, path) for name, path in paths.items()}
    assert large == small
    for name, found in small.items():
        assert found <= BUDGETS[name], (name, found)
