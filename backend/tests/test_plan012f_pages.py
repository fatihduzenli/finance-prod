"""PLAN-012f Batch C (docs/qa/PLAN-012f-approved-design.md D7 to D9, D11, §6 C): the dashboard,
the store and operator workspaces and the monthly rent overview on the approved layouts.

The dashboard's header (PLAN-015 Batch 1 removed the KPI, Needs attention, collection and
Recent payments cards these tests pinned; tests/test_dashboard_v2.py covers the new page) and
the Needs attention rows' store titles; the store page's header line, Charges card and totals
row, the four side cards and the figure row (PLAN-015 Batch 3); the operator page's store
selector and the same cards; the grid's header, filter card, legend, cell blocks, tinted month
and totals counts.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import (
    FeeType,
    Lease,
    OneOffCategory,
    RentChargeStatus,
    StoreStatus,
)
from ledger.services import dashboard as blocks
from ledger.services import workspace
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
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


# --- D7: the dashboard ---------------------------------------------------------------------------


def test_the_header_has_the_long_date_and_new_invoice_for_billing_only() -> None:
    billing = page(client_for(Role.BILLING)[0], "/")
    header = section(billing, 'id="page-header"', "</header>")
    # PLAN-015 Batch 1 re-pin: the mockup's sub-line in place of the long date, no search box;
    # New invoice is a secondary button beside the primary Record payment.
    assert (
        '<p class="page-sub" id="dashboard-sub">Collections and balances for October 2026</p>'
        in (header)
    )
    assert 'id="nav-search"' not in header
    # PLAN-012f-b D7: the button opens the "Which store?" picker; the search page for a new
    # invoice is its link without the script (re-pinned from the Stores list).
    assert re.search(
        r'<a class="btn" id="new-invoice" href="/search/\?intent=new_invoice" '
        r'data-dialog-open="store-picker">.*New invoice</a>',
        header,
    )
    viewer = page(client_for(Role.VIEWER)[0], "/")
    assert 'id="new-invoice"' not in viewer


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
    # PLAN-015 Batch 3 (re-pinned): the phone's strip is the figure row of KPI cards v2, at
    # every width, between the action bar and the tabs.
    strip = section(header, 'id="summary-strip"', "</section>")
    assert header.index('id="action-bar"') < header.index('id="summary-strip"')
    assert header.index('id="summary-strip"') < header.index('id="store-tabs"')
    assert re.findall(r'<p class="kpi2-title">([^<]+)</p>', strip) == [
        "Open balance",
        "Rent",
        "Autopay",
    ]
    assert (
        '<span class="kpi2-value tone-unpaid" id="figure-open-balance-value">$2,000.00</span>'
        in strip
    )
    # Re-pinned (Batch 3 findings): the figure is the amount; "per month" is the sub-line.
    assert '<span class="kpi2-value" id="figure-rent-value">$2,000.00</span>' in strip
    assert '<p class="kpi2-sub">per month · debit day 1st</p>' in strip
    assert '<span class="kpi2-value" id="figure-autopay-value">Not set up</span>' in strip


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
    # Re-pinned for PLAN-014 G2: the $3,000 deposit is listed but not in the open balance,
    # which is the header's (and the Aging report's row of the store).
    assert '<span id="charges-balance">$2,000.00</span>' in card
    assert 'id="charges-uncounted"' in card
    assert '<span class="charge-kind">Setup</span>' in card  # the one-off's kind
    assert 'id="create-missing-charges"' in card
    # The contact log card follows on the Charges tab.
    assert html.index('id="tab-charges"') < html.index('id="contact-card"')


def test_the_four_side_cards_read_the_lease() -> None:
    lease = a_store_world()
    html = page(client_for(Role.BILLING)[0], f"/stores/{lease.store_id}/")
    side = html[html.index('<section id="workspace-header"') :]
    labels = re.findall(r'<h2 class="section-label"[^>]*>([^<]+)</h2>', side)
    # PLAN-014b item 3 (re-pinned): the NOTES card always follows the LEASE card.
    assert labels == ["Lease", "Notes", "Next", "Store cost", "Documents"]
    # PLAN-015 Batch 5 §9.1 (re-pinned): the Rent, Autopay and Open balance rows are the figure
    # row's, not the LEASE card's; the "Set up" link is the Autopay card's.
    assert 'id="header-rent"' not in side and 'id="header-autopay"' not in side
    assert 'id="header-open-balance"' not in side and 'id="autopay-set-up"' not in side
    assert 'id="header-since"' not in side  # PLAN-014 B3: the Lease link carries the date
    assert '<dd id="header-debit-day">1st</dd>' in side
    # PLAN-014 I-b: the Deposit row is coloured by its state, as the Sign fee row.
    assert '<dd class="side-unpaid" id="header-deposit">$3,000.00 · not collected</dd>' in side
    assert re.search(
        r'<a class="kpi2-link" href="[^"]*tab=schedules#tab-schedules" id="autopay-set-up">'
        r"<span>Set up</span>",
        section(html, 'id="figure-autopay"', "</section>"),
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
    # PLAN-012f-a D7: All stores by default; a selected store brings its side cards (its phone
    # strip went with PLAN-015 Batch 3), after the operator's own (CONTACT, PAYMENT, RECENT
    # CONTACT, PORTAL).
    lease = a_store_world()
    html = page(
        client_for(Role.BILLING)[0], f"/operators/{lease.operator_id}/?store={lease.store_id}"
    )
    header = section(html, 'id="page-header"', "</header>")
    assert 'id="store-selector"' in header and 'id="operator-actions"' in header
    # PLAN-015 Batch 3 (re-pinned): the header's one figure row is the operator's (§3.2), with
    # a store selected as without; the selected store's figures are in its LEASE card.
    assert header.count('id="summary-strip"') == 1
    strip = section(header, 'id="summary-strip"', "</section>")
    assert re.findall(r'<p class="kpi2-title">([^<]+)</p>', strip) == [
        "Open balance",
        "Stores",
        "Returns this year",
        "Promise",
    ]
    assert 'id="figure-rent"' not in header and 'id="figure-autopay"' not in header
    side = html[html.index('<aside class="workspace-side') :]
    side = side[side.index('id="selected-store-label"') :]
    labels = re.findall(r'<h2 class="section-label"[^>]*>([^<]+)</h2>', side)
    # PLAN-014b item 3 (re-pinned): the NOTES card always follows the LEASE card.
    assert labels == ["Lease", "Notes", "Next", "Store cost", "Documents"]
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
    # PLAN-015 Batch 1 re-pin: the item is a row of the feed's Needs attention kind, its store
    # titled as the store page titles it, its operator beside it.
    html = page(client_for(Role.BILLING)[0], "/?kind=attention")
    row = section(html, 'data-kind="attention"', "</tr>")
    assert (
        '<td class="activity-store" data-role="meta" data-label="Store">#101 0101 MAPLE AVENUE</td>'
        in (row)
    )
    assert '<td class="activity-operator" data-role="meta" data-label="Operator">Maple Co</td>' in (
        row
    )
