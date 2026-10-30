"""PLAN-012f Batch C (docs/qa/PLAN-012f-approved-design.md D7 to D9, D11, §6 C): the dashboard,
the store and operator workspaces and the monthly rent overview on the approved layouts.

The dashboard's four KPI cards with their numbers from seeded data (and the "—" and empty
states), the Needs attention rows with one action each (none for a viewer), the month's
collection percentage and progress segments, the Coming up items in date order with their date
chips, the five recent payments with "Today" and "Yesterday", and each new block's one query;
the store page's header line, Charges card and totals row, the four side cards and the phone
summary strip; the operator page's store selector and the same cards; the grid's header, filter
card, legend, cell blocks, tinted month and totals counts.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import (
    FeeType,
    Lease,
    OneOffCategory,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
    StoreStatus,
)
from ledger.services import dashboard as blocks
from ledger.services import workspace
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)  # a Tuesday
OCTOBER = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.content.decode()


def section(html: str, start: str, end: str) -> str:
    at = html.index(start)
    return html[at : html.index(end, at)]


def kpi(html: str, element_id: str) -> str:
    return section(html, f'id="{element_id}"', "</div></")


# --- D7: the dashboard ---------------------------------------------------------------------------


def seeded() -> dict[str, object]:
    """October: 1,000 paid, 500 pending, 400 of 800 partly paid, 700 unpaid (billed 3,000,
    collected 1,400); an overdue September charge; one pending ACH debit of 500 started on the
    2nd; a waived October charge left out."""
    first = a_lease(a_store("2301", 2301), an_operator("First Co"), debit_day=5)
    second = a_lease(a_store("2302", 2302), an_operator("Second Co"), debit_day=20)
    paid = a_rent_charge(first, OCTOBER, RentChargeStatus.PAID, "1000.00", "1000.00")
    a_payment(paid, "1000.00", date(2026, 10, 5))
    pending = a_rent_charge(second, OCTOBER, RentChargeStatus.PENDING, "500.00", day=2)
    a_payment(pending, "500.00", None, PaymentStatus.PENDING, initiated_on=date(2026, 10, 2))
    third = a_lease(a_store("2303", 2303), an_operator("Third Co"), debit_day=1)
    a_rent_charge(third, OCTOBER, RentChargeStatus.PARTIAL, "800.00", "400.00", day=1)
    fourth = a_lease(a_store("2304", 2304), an_operator("Fourth Co"), debit_day=1)
    a_rent_charge(fourth, OCTOBER, RentChargeStatus.UNPAID, "700.00", day=1)
    a_rent_charge(fourth, date(2026, 9, 1), RentChargeStatus.UNPAID, "700.00", day=1)
    a_rent_charge(a_lease(debit_day=9), OCTOBER, RentChargeStatus.WAIVED, "999.00", day=9)
    return {"first": first, "second": second, "fourth": fourth}


def test_the_four_kpis_read_the_seeded_figures() -> None:
    seeded()
    html = page(client_for(Role.VIEWER)[0], "/")
    figures = section(html, 'id="figures-card"', "</section>")
    labels = re.findall(r'<span class="kpi-label">([^<]+)</span>', figures)
    assert labels == [
        "Unpaid balances",
        "Collected this month",
        "Pending bank debits",
        "Today&#x27;s daily charges",
    ]
    # Open: 500 pending + 400 partial + 700 + 700 (the 999 waived left out); three stores
    # owe, each with a charge due before today (a pending charge is owed until it settles).
    unpaid = kpi(html, "figure-outstanding")
    assert '<span class="kpi-value tone-unpaid">$2,300.00</span>' in unpaid
    assert '<span class="kpi-note">3 stores · 3 overdue</span>' in unpaid
    # Collected this month: 1,000 settled; the month's rent: 1,400 of 3,000 collected (46 %).
    collected = kpi(html, "figure-paid")
    assert '<span class="kpi-value">$1,000.00</span>' in collected
    assert '<span class="kpi-note">46% of Oct 2026 rent</span>' in collected
    # One pending debit of 500 started on Friday the 2nd: five business days, Friday the 9th.
    pending = kpi(html, "figure-pending")
    assert '<span class="kpi-value">$500.00</span>' in pending
    assert '<span class="kpi-note">1 debit · settle by Oct 9, 2026</span>' in pending
    assert 'href="/payments/?status=pending&amp;method=ach"' in html
    run = kpi(html, "figure-run")
    assert '<span class="kpi-value">—</span>' in run
    assert 'not created yet · <a href="/run/">details</a>' in run


def empty_title(html: str, block_id: str) -> str:
    """The line of the empty state whose block has ``block_id``."""
    block = html[html.index(f'<div class="empty-state" id="{block_id}">') :]
    return block.split('<p class="empty-state-title">', 1)[1].split("</p>", 1)[0]


def test_an_empty_dashboard_has_its_dashes_and_sentences() -> None:
    html = page(client_for(Role.BILLING)[0], "/")
    assert '<span class="kpi-note">0 stores · 0 overdue</span>' in html
    assert '<span class="kpi-note">—</span>' in html  # nothing billed this month
    assert '<span class="kpi-note">0 debits</span>' in html
    # PLAN-012f-b E-4: the empty cards are the empty state (finance/_empty_state.html).
    # PLAN-014 A2: the Collection and Coming up cards are gone.
    assert 'id="collection-none"' not in html and 'class="progress' not in html
    assert 'id="coming-up-none"' not in html
    assert empty_title(html, "recent-payments-none") == "No settled payments yet."
    assert 'id="attention-none"' in html


def test_the_header_has_the_long_date_and_new_invoice_for_billing_only() -> None:
    billing = page(client_for(Role.BILLING)[0], "/")
    header = section(billing, 'id="page-header"', "</header>")
    assert '<span class="page-date" id="dashboard-date">Tuesday, Oct 6, 2026</span>' in header
    assert 'id="nav-search"' in header
    # PLAN-012f-b D7: the button opens the "Which store?" picker; the search page for a new
    # invoice is its link without the script (re-pinned from the Stores list).
    assert re.search(
        r'<a class="btn btn-primary" id="new-invoice" href="/search/\?intent=new_invoice" '
        r'data-dialog-open="store-picker">.*New invoice</a>',
        header,
    )
    viewer = page(client_for(Role.VIEWER)[0], "/")
    assert 'id="new-invoice"' not in viewer


def test_the_month_s_collection_has_the_share() -> None:
    # PLAN-014 A2: the card is gone; the share still feeds the "Collected this month" figure.
    seeded()
    collection = blocks.collection_progress(TODAY)
    assert (collection.billed, collection.collected, collection.percent) == (
        D("3000.00"),
        D("1400.00"),
        46,
    )
    html = page(client_for(Role.VIEWER)[0], "/")
    assert "46% of Oct 2026 rent" in kpi(html, "figure-paid")
    assert 'id="collection-card"' not in html


def test_more_collected_than_billed_is_capped_at_one_hundred() -> None:
    lease = a_lease(debit_day=1)
    a_rent_charge(lease, OCTOBER, RentChargeStatus.PAID, "1000.00", "1500.00", day=1)
    assert blocks.collection_progress(TODAY).percent == 100


def test_the_progress_fills_are_decorative_tokens() -> None:
    from tests.test_plan012f_components import TOKENS_CSS, rule

    assert rule(".progress-pending").strip() == "background: var(--progress-pending);"
    assert rule(".progress-partial").strip() == "background: var(--progress-partial);"
    assert rule(".progress-unpaid").strip() == "background: var(--unpaid-bg);"
    assert rule(".progress-paid").strip() == "background: var(--paid-text);"
    tokens = TOKENS_CSS.read_text()
    assert "--progress-pending: #E5B800;" in tokens and "--progress-partial: #F08A24;" in tokens
    assert "pair: --progress" not in tokens  # no text is set on them
    # The contact log's open promise takes the pending family (the reference's "Open").
    assert rule(".badge.promise-open").strip() == (
        "background: var(--pending-bg); color: var(--pending-text);"
    )


def test_the_attention_rows_have_one_button_each_for_billing_and_none_for_a_viewer() -> None:
    seeded()
    billing = page(client_for(Role.BILLING)[0], "/")
    table = section(billing, 'id="attention-rows"', "</table>")
    rows = re.findall(r"<tr class=\"attention-item.*?</tr>", table, re.S)
    assert rows
    for row in rows:
        assert row.count('class="btn btn-sm"') == 1, row
        assert row.count('class="btn') == 1, row
    overdue = next(row for row in rows if 'data-kind="overdue"' in row)
    assert '<span class="badge badge-overdue">' in overdue and "Overdue 35 days</span>" in overdue
    assert '<a class="cell-title" href="/stores/' in overdue and "Fourth Co</span>" in overdue
    assert '<td class="money" data-role="amount" data-label="Amount">$700.00</td>' in overdue
    assert 'id="attention-count">' in billing and 'id="all-unpaid" href="/arrears/"' in billing
    viewer = page(client_for(Role.VIEWER)[0], "/")
    table = section(viewer, 'id="attention-rows"', "</table>")
    assert 'class="btn' not in table and "<form" not in table
    # The viewer follows the item's page from the line under the badge.
    assert re.search(r'<span class="cell-subline attention-details"><a href="/charges/', table)


def test_the_five_recent_payments_say_today_and_yesterday() -> None:
    lease = a_lease(a_store("2310", 2310), an_operator("Pay Co"))
    days = [TODAY, date(2026, 10, 5), date(2026, 10, 1), date(2026, 9, 30), date(2026, 9, 2)]
    for n, day in enumerate([*days, date(2026, 8, 1)]):
        charge = a_rent_charge(
            lease, date(2026, 3 + n, 1), RentChargeStatus.PAID, "100.00", "100.00"
        )
        a_payment(charge, f"{100 + n}.00", day)
    Payment.objects.filter(settled_on=TODAY).update(
        updated_at=datetime(2026, 10, 6, 18, 2, tzinfo=UTC)  # 14:02 in New York
    )
    Payment.objects.create(  # a refund is not a payment received
        rent_charge=lease.rent_charges.first(),
        parent_payment=Payment.objects.filter(settled_on=TODAY).first(),
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("5.00"),
        status=PaymentStatus.SETTLED,
        settled_on=TODAY,
        is_refund=True,
    )
    rows = blocks.recent_payments(TODAY)
    assert [row.when for row in rows] == [
        "Today 14:02",
        "Yesterday",
        "Oct 1, 2026",
        "Sep 30, 2026",
        "Sep 2, 2026",
    ]
    # The store as its page and the grid title it (Fable's review of Batch C).
    assert rows[0].store == "#2310" and rows[0].paid_for == "Rent Mar 2026"
    assert rows[0].method == "Bank debit (ACH)" and rows[0].amount == D("100.00")
    html = page(client_for(Role.VIEWER)[0], "/")
    card = section(html, 'id="recent-payments"', "</section>")
    assert card.count('<tr class="recent-payment">') == 5
    assert ">Today 14:02</a>" in card and ">Yesterday</a>" in card
    assert 'id="all-payments" href="/payments/">All payments →</a>' in card


def test_each_new_block_is_one_query() -> None:
    seeded()
    for build in (
        blocks.pending_debits,
        lambda: blocks.collection_progress(TODAY),
        lambda: blocks.recent_payments(TODAY),
    ):
        with CaptureQueriesContext(connection) as captured:
            build()
        assert len(captured.captured_queries) == 1


def test_business_days_skip_the_weekend() -> None:
    assert blocks.add_business_days(date(2026, 10, 2), 5) == date(2026, 10, 9)
    assert blocks.add_business_days(date(2026, 10, 3), 1) == date(2026, 10, 5)
    assert blocks.add_business_days(date(2026, 10, 6), 0) == date(2026, 10, 6)


# --- D8: the store workspace ---------------------------------------------------------------------


def a_store_world() -> Lease:
    store = a_store("Istanbul", 1991)
    store.address_line1 = "221 Ridge Rd"
    store.city = "North Arlington"
    store.state = "NJ"
    store.postal_code = "07031"
    store.open_date = date(2024, 10, 20)
    store.needs_open_date = False
    store.save()
    lease = a_lease(store, an_operator("fatih"), debit_day=1, rent="2000.00")
    a_rent_charge(lease, OCTOBER, RentChargeStatus.UNPAID, "2000.00", day=1)
    deposit = FeeType.objects.create(code="deposit-c", name="Security deposit", refundable=True)
    assert lease.operator is not None
    a_one_off_charge(
        lease.operator,
        "3000.00",
        date(2026, 11, 1),
        lease=lease,
        category=OneOffCategory.SETUP,
        fee_type=deposit,
        description="",
        collection="none",
    )
    return lease


def test_the_store_header_has_the_crumb_title_line_tabs_and_strip() -> None:
    lease = a_store_world()
    html = page(client_for(Role.ADMIN)[0], f"/stores/{lease.store_id}/")
    header = section(html, 'id="page-header"', "</header>")
    assert '<a href="/stores/">Stores</a>' in header and 'aria-current="page">#1991</a>' in header
    assert re.search(r'id="store-title">#1991 Istanbul <span class="badge badge-paid">', header)
    assert (
        '<p class="store-line" id="store-line">221 Ridge Rd, North Arlington, New Jersey 07031 · '
        "Open since Oct 20, 2024</p>"
    ) in header
    assert (
        '<p class="store-line-phone" id="store-line-phone">fatih · North Arlington, New Jersey</p>'
        in header
    )
    assert 'id="action-bar"' in header
    # The Charges tab counts the open charges the open balance counts (the rent; a deposit is
    # money held, never owed revenue).
    assert re.search(
        r'id="tab-link-charges"[^>]*>Charges '
        r'<span class="count-pill" id="open-charges-count">1</span>',
        header,
    )
    assert 'id="tab-link-history"' in header and ">Activity</a>" in header
    strip = section(header, 'id="summary-strip"', "</dl>")
    assert re.findall(r"<dt>([^<]+)</dt>", strip) == ["Open balance", "Rent", "Autopay"]
    assert 'class="summary-value tone-unpaid">$2,000.00</dd>' in strip
    assert '<dd class="summary-value">$2,000.00</dd>' in strip and ">Not set up</dd>" in strip


def test_the_charges_card_has_its_columns_and_the_open_balance_row() -> None:
    lease = a_store_world()
    html = page(client_for(Role.BILLING)[0], f"/stores/{lease.store_id}/")
    card = section(html, 'id="tab-charges"', "</section>")
    assert re.findall(r"<th[^>]*>([A-Za-z ]+)</th>", section(card, "<thead>", "</thead>")) == [
        "Charge",
        "Due",
        "Status",
        "Amount",
        "Paid",
        "Balance",
    ]
    assert '<tr class="totals"><th scope="row" colspan="5">Open balance</th>' in card
    assert '<span id="charges-balance">$5,000.00</span>' in card
    assert '<span class="charge-kind">Setup</span>' in card  # the one-off's kind
    assert 'id="create-missing-charges"' in card
    # The contact log card follows on the Charges tab.
    assert html.index('id="tab-charges"') < html.index('id="contact-card"')


def test_the_four_side_cards_read_the_lease() -> None:
    lease = a_store_world()
    html = page(client_for(Role.BILLING)[0], f"/stores/{lease.store_id}/")
    side = html[html.index('<section id="workspace-header"') :]
    labels = re.findall(r'<h2 class="section-label"[^>]*>([^<]+)</h2>', side)
    assert labels == ["Lease", "Next", "Store cost", "Documents"]
    assert '<dd id="header-rent"><strong>$2,000.00</strong> / month</dd>' in side
    assert 'id="header-since"' not in side  # PLAN-014 B3: the Lease link carries the date
    assert '<dd id="header-debit-day">1st</dd>' in side
    assert '<dd id="header-deposit">$3,000.00 · not collected</dd>' in side
    assert re.search(
        r'<dd id="header-autopay"><span class="badge">Not set up</span> '
        r'<a href="[^"]*tab=schedules',
        side,
    )
    assert '<dt>Rent debit</dt><dd id="header-next-rent">Nov 1, 2026</dd>' in side
    assert '<dt>Insurance renewal</dt><dd id="header-insurance">Oct 20, 2026</dd>' in side
    assert 'id="change-cost">Change</a>' in side
    assert re.search(
        r'id="documents-upload" href="/stores/[^"]+/\?tab=documents#tab-documents"', side
    )
    assert "No documents yet." in side


def test_the_header_lines_and_ordinals() -> None:
    assert [workspace.ordinal(day) for day in (1, 2, 3, 11, 12, 21, 22, 23, 31)] == [
        "1st", "2nd", "3rd", "11th", "12th", "21st", "22nd", "23rd", "31st",
    ]  # fmt: skip
    twice = a_lease(debit_day=1, second_debit_day=15)
    header = workspace.store_workspace(twice.store, TODAY, Role.VIEWER).header
    assert header.debit_day_text == "1st and 15th"
    assert header.next_rent == date(2026, 10, 15)
    assert header.deposit_text == ""


def test_a_closed_store_has_no_insurance_renewal() -> None:
    lease = a_store_world()
    store = lease.store
    store.closed_at = date(2026, 10, 15)
    store.status = StoreStatus.ACTIVE
    store.save()
    header = workspace.store_workspace(store, TODAY, Role.VIEWER).header
    assert header.next_insurance is None


def test_the_operator_page_has_its_selector_in_the_header_and_the_same_cards() -> None:
    # PLAN-012f-a D7: All stores by default; a selected store brings its phone strip and its
    # side cards, after the operator's own (CONTACT, PAYMENT, RECENT CONTACT, PORTAL).
    lease = a_store_world()
    html = page(
        client_for(Role.BILLING)[0], f"/operators/{lease.operator_id}/?store={lease.store_id}"
    )
    header = section(html, 'id="page-header"', "</header>")
    assert 'id="store-selector"' in header and 'id="operator-actions"' in header
    assert 'id="summary-strip"' in header
    side = html[html.index('<aside class="workspace-side') :]
    side = side[side.index('id="selected-store-label"') :]
    labels = re.findall(r'<h2 class="section-label"[^>]*>([^<]+)</h2>', side)
    assert labels == ["Lease", "Next", "Store cost", "Documents"]
    assert 'id="header-store"' in side and 'id="header-operator-balance">$2,000.00<' in side
    assert 'id="action-bar"' in side


# --- D9: the monthly rent overview ---------------------------------------------------------------


def test_the_grid_has_its_header_filter_card_legend_and_totals() -> None:
    lease = a_lease(a_store("2320", 2320), an_operator("Grid Co"))
    a_rent_charge(lease, OCTOBER, RentChargeStatus.PAID, "100.00", "100.00")
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, "100.00")
    html = page(client_for(Role.VIEWER)[0], "/grid/?from=2026-08&to=2026-11")
    header = section(html, 'id="page-header"', "</header>")
    assert ">Monthly rent overview</h1>" in header
    assert 'class="btn" id="export-csv"' in header and ">Download spreadsheet</a>" in header
    card = section(html, 'id="grid-filter-card"', "</section>")
    for marker in (
        'id="grid-q"',
        'name="from"',
        'name="to"',
        'id="status"',
        'id="channel"',
        ">Apply</button>",  # PLAN-014 A5: the shared filters form's Apply
    ):
        assert marker in card, marker
    legend = section(html, '<p class="legend muted">', "</p>")
    assert legend.count('class="badge badge-') == 6 and '<span class="cell-not-due">' in legend
    assert (
        html.index('id="grid-filter-card"')
        < html.index('class="legend')
        < html.index('id="grid-card"')
    )
    totals = section(html, '<tr class="totals">', "</tr>")
    assert '<span class="cell-subline">paid · unpaid · pending</span>' in totals
    assert '$100.00<span class="cell-subline column-counts">1 · 0 · 0</span>' in totals
    assert '$0.00<span class="cell-subline column-counts">0 · 1 · 0</span>' in totals
    assert 'class="column-total month-current" data-month="2026-10"' in totals
    assert "table.grid .month-col,table.grid td{width:116px;" in html


def test_every_grid_filter_label_wraps_with_its_control() -> None:
    """Fable's review of Batch C: a label and its control are one .filter-field, so a wrap
    moves them together. PLAN-014 A5: the fields are the shared filters panel's; Apply is in
    its footer, no longer beside the last pair."""
    html = page(client_for(Role.VIEWER)[0], "/grid/")
    form = section(html, 'id="filters-panel"', "</form>")
    fields = re.findall(
        r'<span class="filter-field">(.*?)</span>\s*(?=<span class="filter|</div>)', form, re.S
    )
    labels = re.findall(r'<label for="([a-z]+)"', form)
    assert labels == ["from", "to", "operator", "state", "channel", "status"]
    assert len(fields) == len(labels)
    for name, field in zip(labels, fields, strict=True):
        assert f'<label for="{name}"' in field and f'id="{name}"' in field, name
    assert '<button class="btn btn-dark" type="submit" id="filters-apply">Apply</button>' in form
    from tests.test_plan012f_components import rule

    pair = rule(".filter-field")
    for declaration in (
        "display: inline-flex;",
        "align-items: center;",
        "gap: 8px;",
        "white-space: nowrap;",
    ):
        assert declaration in pair, declaration
    assert "flex-wrap: wrap;" in rule(".filter-card .actions")


def test_the_attention_rows_title_their_store_as_the_store_page_does() -> None:
    lease = a_lease(a_store("0101 MAPLE AVENUE", 101), an_operator("Maple Co"))
    a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.UNPAID)
    html = page(client_for(Role.BILLING)[0], "/")
    row = section(html, 'data-kind="overdue"', "</tr>")
    assert (
        f'<a class="cell-title" href="/stores/{lease.store_id}/">#101 0101 MAPLE AVENUE</a>' in row
    )
    assert '<span class="cell-subline">Maple Co</span>' in row
