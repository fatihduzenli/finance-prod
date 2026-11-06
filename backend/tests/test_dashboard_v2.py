"""The dashboard as Design v2 (docs/qa/PLAN-015-design-v2.md §4.2, §6 Batch 1): each KPI against
the report it mirrors for this month, quarter and year; the delta and the percent of what is
due; the Overdue sub-line's Needs attention count; the Bank deposits card with and without a
connection; the Collections bars (Monthly and Yearly) and their tooltip values; the operator
tiles; the Recent activity feed's seven kinds, search, kind filter and paging; the period
parser; the spreadsheet; the roles; the moved and removed cards; a flat query count; the
statement timeout; the labels and glyphs; Rule 8. Adversarial: a payment voided after its
period, a kept promise, a ``<script>`` search, ``?period=../etc``, ``?page=9999`` and
``?page=x``."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from banking.models import IgnoreReason, Status
from finance.templatetags.components import parse_period
from ledger.models import (
    CollectionStatus,
    ContactEntry,
    ContactKind,
    ContactStatus,
    InvoiceStatus,
    Lease,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import attention, dashboard_v2, grid, receivables, reports
from ledger.views import dashboard as dashboard_view
from ledger.views.reports import TOO_SLOW, ReportTooSlow
from ledger.wording import provider_name
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_invoice,
    an_operator,
)
from tests.test_banking import a_connection, deposit
from tests.test_operator_views import client_for
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 11, 5)  # a Thursday
NOV = date(2026, 11, 1)
OCT = date(2026, 10, 1)
SEP = date(2026, 9, 1)
AUG = date(2026, 8, 1)
JAN = date(2026, 1, 1)
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def page(client: Client, query: str = "") -> str:
    response = client.get(f"/{query}")
    assert response.status_code == 200, response.status_code
    return response.content.decode()


def section(html: str, start: str, end: str = "</section>") -> str:
    at = html.index(start)
    return html[at : html.index(end, at)]


def figure(html: str, element_id: str) -> str:
    return section(html, f'id="{element_id}"', "</a></div></div>")


def settled(charge: RentCharge, amount: str, on: date, method: str = PaymentMethod.ACH) -> Payment:
    return Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=method,
        amount=D(amount),
        status=PaymentStatus.SETTLED,
        initiated_on=on,
        settled_on=on,
        note="" if method == PaymentMethod.ACH else "Received by hand",
    )


def a_promise(
    operator: Operator,
    lease: Lease | None,
    *,
    on: date,
    by: date,
    amount: str,
    status: str = ContactStatus.OPEN,
) -> ContactEntry:
    return ContactEntry.objects.create(
        operator=operator,
        lease=lease,
        kind=ContactKind.PROMISE,
        happened_on=on,
        promised_date=by,
        promised_amount=D(amount),
        body="Will pay.",
        status=status,
    )


def world() -> dict[str, Any]:
    """Three stores over a year: rent billed each month, paid by debit (and once by hand),
    one damage fee, overdue September rent on one store, a voided payment."""
    north = an_operator("Northwind Stores")
    harbor = an_operator("Harbor Lane Retail")
    first = a_lease(a_store("0101 MAPLE AVENUE", 101), north, debit_day=1, started=JAN)
    second = a_lease(a_store("0114 CEDAR STREET", 114), harbor, debit_day=3, started=JAN)
    third = a_lease(a_store("0132 BIRCH LANE", 132), harbor, debit_day=1, started=JAN)
    for month in range(1, 12):
        start = date(2026, month, 1)
        for lease, rent in ((first, "1000.00"), (second, "2000.00")):
            if lease is second and start == SEP:
                a_rent_charge(lease, start, RentChargeStatus.UNPAID, rent, day=3)
                continue
            if start == NOV and lease is second:
                a_rent_charge(lease, start, RentChargeStatus.DUE, rent, day=3)
                continue
            charge = a_rent_charge(
                lease, start, RentChargeStatus.PAID, rent, rent, day=lease.debit_day
            )
            settled(charge, rent, start.replace(day=lease.debit_day or 1) + timedelta(days=1))
    by_hand = a_rent_charge(third, OCT, RentChargeStatus.PAID, "500.00", "500.00", day=1)
    settled(by_hand, "500.00", date(2026, 10, 2), PaymentMethod.ZELLE)
    a_one_off_charge(harbor, "250.00", date(2026, 11, 10), lease=second)
    # Settled in October, voided in November: no money came in, in any period.
    voided = a_rent_charge(third, SEP, RentChargeStatus.PAID, "500.00", "500.00", day=1)
    gone = settled(voided, "500.00", date(2026, 10, 3))
    Payment.objects.filter(pk=gone.pk).update(
        status=PaymentStatus.VOIDED, voided_at=datetime(2026, 11, 2, 15, tzinfo=UTC)
    )
    return {"north": north, "harbor": harbor, "first": first, "second": second, "third": third}


# --- The period -------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "../etc", "MONTH", "week", None, "<script>", "year\x00"])
def test_the_period_parser_falls_back_to_the_month(raw: object) -> None:
    assert parse_period(raw) == "month"
    assert dashboard_v2.window(parse_period(raw), TODAY) == dashboard_v2.Window("month", NOV, NOV)


def test_the_windows_and_their_previous_periods() -> None:
    month = dashboard_v2.window("month", TODAY)
    quarter = dashboard_v2.window("quarter", TODAY)
    year = dashboard_v2.window("year", TODAY)
    assert (month.start, month.end) == (NOV, date(2026, 11, 30))
    assert (quarter.start, quarter.end) == (OCT, date(2026, 12, 31))
    assert (year.start, year.end) == (JAN, date(2026, 12, 31))
    assert month.previous() == dashboard_v2.Window("month", OCT, OCT)
    assert quarter.previous() == dashboard_v2.Window("quarter", date(2026, 7, 1), SEP)
    assert year.previous() == dashboard_v2.Window("year", date(2025, 1, 1), date(2025, 12, 1))
    first = dashboard_v2.window("quarter", date(2026, 2, 14))
    assert (first.from_month, first.to_month) == (JAN, date(2026, 3, 1))


def test_a_hostile_period_is_the_month_on_the_page() -> None:
    html = page(client_for(Role.VIEWER)[0], "?period=../etc")
    assert 'name="period" value="month" aria-pressed="true"' in html
    assert "../etc" not in html
    assert "Due this month" in html


# --- The KPIs against their reports -----------------------------------------------------------


@pytest.mark.parametrize("period", ["month", "quarter", "year"])
def test_each_kpi_equals_its_report_for_the_same_range(period: str) -> None:
    world()
    deposit("900.00", posted_on=date(2026, 11, 2))
    deposit("400.00", posted_on=date(2026, 10, 20))
    found = dashboard_v2.kpis(period, TODAY)
    chosen = dashboard_v2.window(period, TODAY)
    selected = reports.ReportRange(chosen.from_month, chosen.to_month)
    # Due: Revenue by month's total billed; its rent part is the grid's rent of those months.
    revenue = reports.revenue_by_month(selected).total
    assert found.due.amount == revenue.total_billed
    built = grid.build(grid.GridQuery(chosen.from_month, chosen.to_month), TODAY)
    grid_rent = sum(
        (cell.amount_due for row in built.rows for cell in row.cells if cell.status), D("0")
    )
    assert revenue.rent_billed == grid_rent
    # Collected: Payments by method's total (the voided payment counts nowhere).
    assert found.collected.amount == reports.payments_by_method(selected).total.total
    # Overdue: the Aging report's total less its Current bucket; its stores and oldest days.
    aging = receivables.aging(TODAY)
    current = aging.totals.buckets[receivables.AGING_CURRENT]
    # September's 2,000 and November's 2,000 (due the 3rd) of store 114.
    assert found.overdue.amount == aging.totals.total - current == D("4000.00")
    past_due = [
        row
        for row in aging.rows
        if row.store_id is not None and row.total - row.buckets[receivables.AGING_CURRENT] > 0
    ]
    assert found.overdue.stores == len(past_due) == 1
    assert found.overdue.oldest_days == (TODAY - date(2026, 9, 3)).days == 63
    # Bank deposits: the credits the Bank deposits page lists for the range (every state).
    from banking.models import BankTransaction

    credits = BankTransaction.objects.filter(posted_on__gte=chosen.start, posted_on__lte=chosen.end)
    assert found.bank.amount == sum((txn.amount for txn in credits), D("0"))
    assert found.bank.unmatched == BankTransaction.objects.filter(status=Status.NEW).count() == 2


def test_the_due_card_counts_the_stores_billed_and_names_the_next_due_day() -> None:
    world()
    due = dashboard_v2.kpis("month", TODAY).due
    # November: 101 rent (due the 1st), 114 rent and fee (due the 3rd and the 10th).
    assert due.title == "Due this month" and due.stores == 2
    assert due.amount == D("3250.00")
    # The rent days of November are the 1st and the 3rd, both before today: the first is named.
    assert due.next_due == NOV and due.sub == "2 stores · due Nov 1st"
    assert dashboard_v2.kpis("quarter", TODAY).due.title == "Due this quarter"
    assert dashboard_v2.kpis("year", TODAY).due.title == "Due this year"


@pytest.mark.parametrize(
    ("day", "words"),
    [
        (date(2026, 11, 1), "Nov 1st"),
        (date(2026, 11, 2), "Nov 2nd"),
        (date(2026, 11, 3), "Nov 3rd"),
        (date(2026, 11, 4), "Nov 4th"),
        (date(2026, 11, 11), "Nov 11th"),
        (date(2026, 11, 12), "Nov 12th"),
        (date(2026, 11, 13), "Nov 13th"),
        (date(2026, 11, 21), "Nov 21st"),
        (date(2026, 11, 22), "Nov 22nd"),
        (date(2026, 11, 23), "Nov 23rd"),
        (date(2026, 12, 31), "Dec 31st"),
    ],
)
def test_the_due_day_reads_as_the_mockup(day: date, words: str) -> None:
    assert dashboard_v2.ordinal_day(day) == words


@pytest.mark.parametrize(
    ("collected", "due", "percent", "sub"),
    [
        ("0.00", "1000.00", 0, "0% of what is due"),
        ("770.00", "1000.00", 77, "77% of what is due"),
        ("1100.00", "1000.00", 110, "110% of what is due"),
        ("776.99", "1000.00", 77, "77% of what is due"),
        ("10.00", "0.00", None, "nothing due"),
    ],
)
def test_the_percent_of_what_is_due(
    collected: str, due: str, percent: int | None, sub: str
) -> None:
    found = dashboard_v2.percent_of(D(collected), D(due))
    assert found == percent
    assert dashboard_v2.Collected(D(collected), found, None).sub == sub


@pytest.mark.parametrize(
    ("current", "previous", "delta", "badge", "tone"),
    [
        ("1042.00", "1000.00", D("4.2"), "↑ 4.2%", "paid"),
        ("970.00", "1000.00", D("-3.0"), "↓ 3.0%", "unpaid"),
        ("1000.00", "1000.00", D("0.0"), "0.0%", "neutral"),
        ("1000.00", "0.00", None, "", "neutral"),
    ],
)
def test_the_delta_against_the_previous_period(
    current: str, previous: str, delta: Decimal | None, badge: str, tone: str
) -> None:
    found = dashboard_v2.change_of(D(current), D(previous))
    assert found == delta
    card = dashboard_v2.Collected(D(current), None, found)
    assert (card.badge, card.badge_tone) == (badge, tone)


def test_the_delta_compares_with_the_previous_period_and_skips_a_voided_payment() -> None:
    world()
    current, previous = dashboard_v2.collected_by_window(dashboard_v2.window("month", TODAY))
    october = reports.payments_by_method(reports.ReportRange(OCT, OCT)).total.total
    assert previous == october == D("3500.00")  # 1,000 + 2,000 + 500 by hand; not the voided 500
    assert current == D("1000.00")
    html = page(client_for(Role.VIEWER)[0])
    collected = figure(html, "figure-collected")
    assert '<span class="kpi2-pill kpi2-pill-unpaid">↓ 71.4%</span>' in collected
    assert '<p class="kpi2-sub">30% of what is due</p>' in collected  # 1,000 of 3,250


def test_without_a_previous_figure_there_is_no_badge() -> None:
    lease = a_lease(a_store("0201", 201), debit_day=1)
    charge = a_rent_charge(lease, NOV, RentChargeStatus.PAID, "100.00", "100.00", day=1)
    settled(charge, "100.00", date(2026, 11, 2))
    collected = figure(page(client_for(Role.VIEWER)[0]), "figure-collected")
    assert "kpi2-pill" not in collected


def test_the_overdue_sub_line_carries_the_needs_attention_count() -> None:
    world()
    found = attention.attention(TODAY, leases=attention.dashboard_leases())
    count = len(found.items) + sum(found.more.values())
    assert count > 0
    overdue = dashboard_v2.kpis("month", TODAY).overdue
    assert overdue.attention == count
    assert overdue.sub == f"1 store · oldest 63 days · {count} need attention"
    html = page(client_for(Role.VIEWER)[0])
    assert f"oldest 63 days · {count} need attention</p>" in figure(html, "figure-overdue")
    assert (
        dashboard_v2.Overdue(D("1"), 1, 2, 1).sub == "1 store · oldest 2 days · 1 needs attention"
    )
    assert dashboard_v2.Overdue(D("0"), 0, 0, 0).sub == "0 stores"


def test_the_bank_card_without_and_with_a_connection_and_its_unmatched_badge() -> None:
    client = client_for(Role.BILLING)[0]
    bank = figure(page(client), "figure-bank")
    assert '<p class="kpi2-sub">no bank connected</p>' in bank
    assert "kpi2-pill" not in bank and '<span class="kpi2-value">$0.00</span>' in bank
    a_connection()
    deposit("1200.00", posted_on=date(2026, 11, 3))
    matched = deposit("300.00", posted_on=date(2026, 11, 4))
    matched.status = Status.IGNORED  # still a credit of the month, no longer unmatched
    matched.ignore_reason = IgnoreReason.NOT_RENT
    matched.ignored_at = datetime(2026, 11, 4, 15, tzinfo=UTC)
    matched.save()
    deposit("50.00", posted_on=date(2026, 10, 30))  # before the month: not its figure
    bank = figure(page(client), "figure-bank")
    assert '<p class="kpi2-sub">Example Bank ····0000</p>' in bank
    assert '<span class="kpi2-value">$1,500.00</span>' in bank
    assert '<span class="kpi2-pill kpi2-pill-pending">2 unmatched</span>' in bank
    assert '<a class="kpi2-link" href="/admin-settings/bank-deposits/">' in bank


def test_a_viewer_sees_the_bank_figure_without_the_link_to_a_page_it_cannot_open() -> None:
    bank = figure(page(client_for(Role.VIEWER)[0]), "figure-bank")
    assert "Bank deposits" in bank and "kpi2-link" not in bank


# --- Collections --------------------------------------------------------------------------------


def test_the_collections_total_bars_and_current_month() -> None:
    world()
    found = dashboard_v2.collections(2026, TODAY)
    assert [bar.label for bar in found.bars] == [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
    ]
    assert [bar.current for bar in found.bars] == [False] * 10 + [True]
    assert found.bars[8].collected == D("1000.00")  # September: 114 left unpaid
    assert found.bars[9].collected == D("3500.00")  # October: the voided payment left out
    assert found.bars[10].title == "November 2026"
    assert found.bars[10].due == D("3250.00")
    ranged = reports.payments_by_method(reports.ReportRange(JAN, NOV))
    assert found.total == ranged.total.total == sum((bar.collected for bar in found.bars), D("0"))
    assert found.since == "collected from January to November 2026"
    html = page(client_for(Role.VIEWER)[0])
    card = section(html, 'id="collections-card"')
    assert f'<p class="collections-total" id="collections-total">${found.total:,.2f}</p>' in card
    assert card.count('<g class="collection-bar') == 11
    assert card.count('class="collection-bar is-current"') == 1
    november = section(card, 'class="collection-bar is-current"', "</g>")
    assert 'data-label="November 2026" data-collected="$1,000.00" data-due="$3,250.00"' in november
    assert "<title>November 2026: collected $1,000.00, due $3,250.00</title>" in november
    assert 'fill="var(--green)"' in november
    assert 'fill="var(--chart-income)"' in card
    assert "$4k</text>" in card  # the y axis in thousands (the largest bar is 3,500)
    assert "Current month" in card and 'id="chart-monthly" aria-current="page"' in card


def test_yearly_draws_the_last_five_years() -> None:
    world()
    found = dashboard_v2.collections_by_year(TODAY)
    assert [bar.label for bar in found.bars] == ["2022", "2023", "2024", "2025", "2026"]
    assert [bar.current for bar in found.bars] == [False] * 4 + [True]
    assert found.bars[-1].collected == dashboard_v2.collections(2026, TODAY).total
    assert found.yearly and found.since == "collected from 2022 to November 2026"
    html = page(client_for(Role.VIEWER)[0], "?chart=yearly&period=quarter")
    card = section(html, 'id="collections-card"')
    assert card.count('<g class="collection-bar') == 5
    assert 'id="chart-yearly" aria-current="page"' in card and "Current year" in card
    # The bars' links keep the period; the period scopes the KPIs only.
    assert 'href="?period=quarter" id="chart-monthly"' in card
    assert "Due this quarter" in html


def test_the_chart_scale_and_its_labels() -> None:
    from finance.templatetags.charts import nice_step, tick_label

    assert nice_step(D("0")) == D("1000")
    assert nice_step(D("142600")) == D("50000")
    assert nice_step(D("3500")) == D("1000")
    assert nice_step(D("700")) == D("200")
    assert tick_label(D("120000"), D("40000")) == "$120k"
    assert tick_label(D("2500"), D("2500")) == "$2.5k"
    assert tick_label(D("400"), D("200")) == "$400"


# --- Operator tiles -------------------------------------------------------------------------------


def test_the_operator_tiles_order_chips_store_counts_and_links() -> None:
    paid_up = an_operator("Northwind Stores")
    lease = a_lease(a_store("0101", 101), paid_up, debit_day=1, started=JAN)
    a_rent_charge(lease, OCT, RentChargeStatus.PAID, "1000.00", "1000.00", day=1)
    overdue = an_operator("Harbor Lane Retail")
    for number in (114, 115, 116, 117):
        a_lease(a_store(f"0{number}", number), overdue, debit_day=1, started=JAN)
    first = Lease.objects.filter(operator=overdue).order_by("store__store_number").first()
    assert first is not None
    a_rent_charge(first, SEP, RentChargeStatus.UNPAID, "13950.00", day=21)
    partial = an_operator("Pinecrest Markets")
    pine = a_lease(a_store("0132", 132), partial, debit_day=1, started=JAN)
    a_rent_charge(pine, NOV, RentChargeStatus.PARTIAL, "3000.00", "866.66", day=10)
    promising = an_operator("Summit Corner Shops")
    summit = a_lease(a_store("0127", 127), promising, debit_day=1, started=JAN)
    a_rent_charge(summit, OCT, RentChargeStatus.UNPAID, "4800.00", day=1)
    a_promise(promising, summit, on=TODAY, by=date(2026, 11, 20), amount="4800.00")
    a_lease(a_store("0300", 300), an_operator("Zed Ended"), status=CollectionStatus.ENDED)
    tiles = dashboard_v2.operator_tiles(TODAY)
    assert [(tile.name, tile.balance, tile.stores, tile.tone, tile.chip) for tile in tiles] == [
        ("Harbor Lane Retail", D("13950.00"), 4, "overdue", "Overdue 45 days"),
        ("Summit Corner Shops", D("4800.00"), 1, "pending", "Promise · Nov 20"),
        ("Pinecrest Markets", D("2133.34"), 1, "partial", "Partial"),
        ("Northwind Stores", D("0.00"), 1, "paid", "Paid up"),
    ]
    html = page(client_for(Role.VIEWER)[0])
    card = section(html, 'id="operators-card"')
    assert '<a class="card-link" id="all-operators" href="/operators/">All operators ›</a>' in card
    assert f'href="/operators/{overdue.pk}/"' in card
    assert '<span class="avatar-tile" aria-hidden="true">HL</span>' in card
    assert '<span class="operator-tile-balance tone-unpaid">$13,950.00</span>' in card
    assert '<span class="operator-tile-balance">$0.00</span>' in card
    assert "<span>Paid up</span>" in card and "4 stores" in card and "1 store<" in card


def test_the_tiles_stop_at_four_by_balance_then_name() -> None:
    for name in ("Delta", "alpha", "Charlie", "Bravo", "Echo"):
        a_lease(a_store(name), an_operator(name), debit_day=1, started=JAN)
    names = [tile.name for tile in dashboard_v2.operator_tiles(TODAY)]
    assert names == ["alpha", "Bravo", "Charlie", "Delta"]


# --- The Recent activity feed -------------------------------------------------------------------


def seven_kinds() -> dict[str, Any]:
    north = an_operator("Northwind Stores")
    harbor = an_operator("Harbor Lane Retail")
    summit = an_operator("Summit Corner Shops")
    pine = an_operator("Pinecrest Markets")
    maple = a_lease(a_store("0101 MAPLE AVENUE", 101), north, debit_day=1, started=JAN)
    cedar = a_lease(a_store("0114 CEDAR STREET", 114), harbor, debit_day=1, started=JAN)
    oak = a_lease(a_store("0127 OAK RIDGE ROAD", 127), summit, debit_day=1, started=JAN)
    birch = a_lease(a_store("0132 BIRCH LANE", 132), pine, debit_day=1, started=JAN)
    paid = a_rent_charge(maple, OCT, RentChargeStatus.PAID, "1066.67", "1066.67", day=1)
    settled(paid, "1066.67", date(2026, 11, 4))
    sent_charge = a_rent_charge(cedar, NOV, RentChargeStatus.UNPAID, "2450.00", day=1)
    an_invoice(
        harbor,
        11,
        "2450.00",
        rent_charge=sent_charge,
        internal_status=InvoiceStatus.SENT,
        sent_at=datetime(2026, 11, 3, 15, tzinfo=UTC),
    )
    a_promise(summit, oak, on=date(2026, 11, 2), by=date(2026, 11, 20), amount="4800.00")
    part = a_rent_charge(birch, NOV, RentChargeStatus.PARTIAL, "2000.00", "1066.66", day=1)
    settled(part, "1066.66", date(2026, 11, 1), PaymentMethod.ZELLE)
    harbor_view = a_lease(
        a_store("0145 HARBOR VIEW DRIVE", 145), harbor, status=CollectionStatus.ENDED, started=JAN
    )
    Lease.objects.filter(pk=harbor_view.pk).update(ended_on=date(2026, 10, 13))
    returned_charge = a_rent_charge(oak, OCT, RentChargeStatus.UNPAID, "4800.00", day=1)
    Payment.objects.create(
        rent_charge=returned_charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("4800.00"),
        status=PaymentStatus.RETURNED,
        initiated_on=date(2026, 10, 15),
        returned_at=datetime(2026, 10, 20, 15, tzinfo=UTC),
        return_code="R01",
        return_reason="Insufficient funds",
    )
    a_promise(harbor, cedar, on=date(2026, 10, 25), by=date(2026, 10, 30), amount="500.00")
    return {"north": north, "harbor": harbor, "maple": maple}


def rows_of(html: str) -> list[str]:
    table = section(html, 'id="activity-table"', "</table>")
    return re.findall(r'<tr class="feed-row".*?</tr>', table, re.S)


def chip_of(row: str) -> tuple[str, str]:
    found = re.search(
        r'<span class="status-chip status-chip-(\w+)"><svg class="icon icon-([\w-]+)".*?</svg>'
        r"<span>([^<]*)</span>",
        row,
        re.S,
    )
    assert found, row
    return found.group(1), f"{found.group(2)} {found.group(3)}"


def test_each_of_the_seven_kinds_renders_with_its_chip() -> None:
    seven_kinds()
    found = dashboard_v2.activity_rows(TODAY, "", "")
    assert [(row.activity, row.store, row.operator, row.on) for row in found] == [
        ("Payment received", "#101 0101 MAPLE AVENUE", "Northwind Stores", date(2026, 11, 4)),
        ("Invoice sent", "#114 0114 CEDAR STREET", "Harbor Lane Retail", date(2026, 11, 3)),
        ("Promise to pay", "#127 0127 OAK RIDGE ROAD", "Summit Corner Shops", date(2026, 11, 2)),
        (f"Paid outside {provider_name()}", "#132 0132 BIRCH LANE", "Pinecrest Markets", NOV),
        ("Promise broken", "#114 0114 CEDAR STREET", "Harbor Lane Retail", date(2026, 10, 31)),
        (
            "Payment returned (NSF)",
            "#127 0127 OAK RIDGE ROAD",
            "Summit Corner Shops",
            date(2026, 10, 20),
        ),
        ("Lease ended", "#145 0145 HARBOR VIEW DRIVE", "Harbor Lane Retail", date(2026, 10, 13)),
    ]
    html = page(client_for(Role.VIEWER)[0], "?page=1")
    chips = [chip_of(row) for row in rows_of(html)]
    assert chips == [
        ("paid", "check Paid"),
        ("unpaid", "dot Unpaid"),
        ("pending", "dot Due Nov 20, 2026"),
        ("partial", "dot Partial"),
        ("unpaid", "dot Broken"),
    ]
    second = page(client_for(Role.VIEWER)[0], "?page=2")
    assert [chip_of(row) for row in rows_of(second)] == [
        ("overdue", "dot Insufficient funds"),
        ("neutral", "circle Vacant · 23 days"),
    ]
    assert "<strong>—</strong>" not in second and ">—</td>" in second  # a lease end has no amount
    assert 'id="activity-card"' in html and "1–5 of 7" in html


def test_a_kept_promise_is_a_promise_to_pay_and_never_broken() -> None:
    operator = an_operator("Kept Co")
    lease = a_lease(a_store("0400", 400), operator, debit_day=1, started=JAN)
    a_promise(
        operator,
        lease,
        on=date(2026, 10, 20),
        by=date(2026, 10, 28),
        amount="900.00",
        status=ContactStatus.KEPT,
    )
    rows = dashboard_v2.activity_rows(TODAY, "", "promises")
    assert [(row.activity, row.chip, row.tone) for row in rows] == [
        ("Promise to pay", "Kept", "paid")
    ]


def test_a_relet_store_reads_ended_not_vacant() -> None:
    store = a_store("0500", 500)
    old = a_lease(store, an_operator("Old Co"), status=CollectionStatus.ENDED, started=JAN)
    Lease.objects.filter(pk=old.pk).update(ended_on=date(2026, 10, 31))
    a_lease(store, an_operator("New Co"), started=NOV, debit_day=1)
    [row] = dashboard_v2.activity_rows(TODAY, "", "leases")
    assert (row.chip, row.tone) == ("Ended", "neutral")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("101", ["Payment received"]),
        ("#114", ["Invoice sent", "Promise broken"]),
        ("harbor lane", ["Invoice sent", "Promise broken", "Lease ended"]),
        ("1,066.67", ["Payment received"]),
        ("$1,066.66", ["Paid outside {provider}"]),
        ("4800", ["Promise to pay", "Payment returned (NSF)"]),
        ("summit 4800.00", ["Promise to pay", "Payment returned (NSF)"]),
        ("nobody", []),
    ],
)
def test_the_search_matches_store_operator_and_amount(query: str, expected: list[str]) -> None:
    seven_kinds()
    found = [row.activity for row in dashboard_v2.activity_rows(TODAY, query, "")]
    assert found == [name.replace("{provider}", provider_name()) for name in expected]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("payments", ["Payment received", "Paid outside {provider}"]),
        ("invoices", ["Invoice sent"]),
        ("promises", ["Promise to pay", "Promise broken"]),
        ("returns", ["Payment returned (NSF)"]),
        ("leases", ["Lease ended"]),
        ("bogus", None),
    ],
)
def test_the_kind_filter(kind: str, expected: list[str] | None) -> None:
    seven_kinds()
    found = [row.activity for row in dashboard_v2.activity_rows(TODAY, "", kind)]
    if expected is None:  # an unknown kind is every kind
        assert len(found) == 7
    else:
        assert found == [name.replace("{provider}", provider_name()) for name in expected]


def test_the_needs_attention_kind_lists_todays_items_with_their_chips() -> None:
    seven_kinds()
    needs = dashboard_v2.needs_attention(TODAY)
    rows = dashboard_v2.activity_rows(TODAY, "", "attention")
    assert len(rows) == len(needs.items) > 0
    assert all(row.kind == "attention" and row.chip and row.tone for row in rows)
    html = page(client_for(Role.VIEWER)[0], "?kind=attention")
    assert '<option value="attention" selected>Needs attention</option>' in html
    assert all(chip_of(row) for row in rows_of(html))


def twenty_four_payments() -> None:
    lease = a_lease(a_store("0600", 600), an_operator("Paging Co"), debit_day=1, started=JAN)
    for n in range(24):
        month = date(2024 + (n // 12), n % 12 + 1, 1)
        charge = a_rent_charge(lease, month, RentChargeStatus.PAID, "10.00", "10.00", day=1)
        settled(charge, f"{10 + n}.00", TODAY - timedelta(days=n))


@pytest.mark.parametrize(
    ("query", "counts", "first"),
    [
        ("", "1–5 of 24", "$10.00"),
        ("?page=3", "11–15 of 24", "$20.00"),
        ("?page=9999", "21–24 of 24", "$30.00"),
        ("?page=x", "1–5 of 24", "$10.00"),
        ("?page=-1", "1–5 of 24", "$10.00"),
    ],
)
def test_paging_five_rows_a_page(query: str, counts: str, first: str) -> None:
    twenty_four_payments()
    html = page(client_for(Role.VIEWER)[0], query)
    rows = rows_of(html)
    assert len(rows) == (4 if "9999" in query else 5)
    assert f"<strong>{first}</strong>" in rows[0]
    footer = section(html, 'class="pager list-footer"', "</div>")
    assert f'<span class="pager-count">{counts}</span>' in footer
    assert "‹ Previous" in footer and "Next ›" in footer


def test_the_pager_keeps_the_search_and_the_kind() -> None:
    twenty_four_payments()
    html = page(client_for(Role.VIEWER)[0], "?q=paging&kind=payments&period=year")
    footer = section(html, 'class="pager list-footer"', "</div>")
    assert 'href="?period=year&amp;q=paging&amp;kind=payments&amp;page=2"' in footer
    form = section(html, 'id="activity-filters"', "</form>")
    assert '<input type="hidden" name="period" value="year">' in form
    assert 'value="paging"' in form


def test_an_empty_feed_says_so() -> None:
    html = page(client_for(Role.VIEWER)[0])
    assert '<p class="muted activity-empty" id="activity-empty">No activity matches.</p>' in html
    assert 'id="activity-table"' not in html
    assert '<span class="pager-count">0 of 0</span>' in html


def test_a_script_search_is_echoed_escaped_and_cleaned() -> None:
    seven_kinds()
    hostile = '<script>alert("x")</script>\x00\x1f' + "a" * 300
    html = page(client_for(Role.VIEWER)[0], "?" + urlencode({"q": hostile}))
    assert "<script>alert" not in html and "\x00" not in html
    form = section(html, 'id="activity-filters"', "</form>")
    value = re.search(r'name="q" id="activity-q" value="([^"]*)"', form)
    assert value is not None
    # Cut to 100 characters, then its control characters dropped, as the lists clean theirs.
    assert unescape(value.group(1)) == hostile[:100].replace("\x00", "").replace("\x1f", "")
    assert value.group(1).startswith("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;")


# --- The spreadsheet ----------------------------------------------------------------------------


def export(client: Client, query: str = "") -> tuple[list[list[str]], str]:
    response = client.get(f"/dashboard.csv{query}")
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    return list(csv.reader(io.StringIO(body))), response["Content-Disposition"]


def test_the_spreadsheet_holds_the_kpis_then_every_activity_row() -> None:
    seven_kinds()
    twenty_four_payments()
    client = client_for(Role.BILLING)[0]
    rows, disposition = export(client, "?period=quarter&page=3")
    assert disposition == 'attachment; filename="dashboard-quarter-2026-11-05.csv"'
    assert rows[0] == [
        "Section",
        "Kind",
        "Store",
        "Operator",
        "Date",
        "Amount (USD)",
        "Status",
        "Detail",
    ]
    found = dashboard_v2.kpis("quarter", TODAY)
    assert rows[1] == [
        "Figure",
        "Due this quarter",
        "",
        "",
        "",
        f"{found.due.amount:.2f}",
        "",
        found.due.sub,
    ]
    assert rows[2][:2] == ["Figure", "Collected"] and rows[2][5] == f"{found.collected.amount:.2f}"
    assert rows[3][1] == "Overdue" and rows[3][7] == found.overdue.sub
    assert rows[4][1] == "Bank deposits" and rows[4][7] == "no bank connected"
    activity = dashboard_v2.activity_rows(TODAY, "", "")
    assert len(rows) == 5 + len(activity) == 5 + 31  # every page, not the page asked for
    assert [row[1] for row in rows[5:]] == [row.activity for row in activity]
    html = page(client, "?period=quarter")
    assert f'<span class="kpi2-value">${found.due.amount:,.2f}</span>' in html
    assert '<span class="pager-count">1–5 of 31</span>' in html


def test_the_spreadsheet_follows_the_search_and_the_kind() -> None:
    seven_kinds()
    rows, disposition = export(client_for(Role.ADMIN)[0], "?q=harbor&kind=promises")
    assert disposition == 'attachment; filename="dashboard-month-2026-11-05.csv"'
    assert [row[1] for row in rows[5:]] == ["Promise broken"]
    assert rows[5][4] == "2026-10-31" and rows[5][5] == "500.00" and rows[5][6] == "Broken"


def test_the_download_link_carries_the_page_s_query() -> None:
    html = page(client_for(Role.BILLING)[0], "?period=year&q=101&kind=payments&page=2")
    assert 'href="/dashboard.csv?period=year&amp;q=101&amp;kind=payments"' in html


# --- Roles ----------------------------------------------------------------------------------------


def test_a_viewer_opens_the_page_without_the_buttons() -> None:
    client = client_for(Role.VIEWER)[0]
    html = page(client)
    for element in ("download-spreadsheet", "new-invoice", "record-payment", "store-picker"):
        assert f'id="{element}"' not in html, element
    assert 'class="segmented period-control"' in html
    assert client.get("/dashboard.csv").status_code == 403
    assert client.post("/").status_code == 403


@pytest.mark.parametrize("role", [Role.BILLING, Role.ADMIN])
def test_billing_and_above_see_the_buttons(role: Role) -> None:
    html = page(client_for(role)[0])
    actions = section(html, 'id="dashboard-actions"', "</div></div>")
    assert '<a class="btn" id="download-spreadsheet" href="/dashboard.csv">' in actions
    assert '<a class="btn" id="new-invoice" href="/search/?intent=new_invoice"' in actions
    assert (
        '<a class="btn btn-primary" id="record-payment" href="/search/?intent=record_payment">'
        in actions
    )
    assert actions.index("download-spreadsheet") < actions.index("new-invoice")
    assert actions.index("new-invoice") < actions.index("record-payment")


def test_record_payment_asks_for_the_store_then_opens_its_add_payment_page() -> None:
    lease = a_lease(a_store("0700 ELM", 700), an_operator("Elm Co"))
    billing = client_for(Role.BILLING)[0]
    response = billing.get("/search/?intent=record_payment")
    assert response.status_code == 200
    html = response.content.decode()
    assert "<h1>Add payment — which store?</h1>" in html
    assert '<input type="hidden" name="intent" value="record_payment">' in html
    jump = billing.get("/search/", {"intent": "record_payment", "q": "0700"})
    assert jump.status_code == 302
    assert jump["Location"] == f"/stores/{lease.store_id}/payments/new/"
    viewer = client_for(Role.VIEWER)[0]
    assert "which store?" not in viewer.get("/search/?intent=record_payment").content.decode()


# --- Moved and removed cards ----------------------------------------------------------------------


def test_the_moved_cards_are_on_daily_charges() -> None:
    world()
    html = client_for(Role.VIEWER)[0].get("/run/").content.decode()
    for element in ("today-run", "failed-attempts", "not-sent-card", "last-reconciliation-card"):
        assert f'id="{element}"' in html, element
    assert html.index('id="insurance-due"') < html.index('id="today-run"')
    assert "Debits due today:" in html and "No nightly check has run yet." in html


def test_the_dashboard_no_longer_draws_the_removed_cards() -> None:
    world()
    html = page(client_for(Role.BILLING)[0])
    for gone in (
        "payment-status",
        "coming-up",
        "recent-payments",
        "attention",
        "cashflow-card",
        "today-run",
        "failed-attempts",
        "not-sent-card",
        "last-reconciliation-card",
    ):
        assert f'id="{gone}"' not in html, gone
    for gone in ('class="donut"', ">Coming up<", ">Recent payments<"):
        assert_absent(html, gone)
    assert 'id="mini-calendar"' in html and "Next 7 days" in html
    order = [
        html.index(f'id="{name}"')
        for name in (
            "figures-card",
            "collections-card",
            "operators-card",
            "activity-card",
            "mini-calendar",
        )
    ]
    assert order == sorted(order)


# --- Queries and the timeout ----------------------------------------------------------------------


def volume(stores: int) -> None:
    for n in range(stores):
        operator = an_operator(f"Volume {n:02d}")
        lease = a_lease(a_store(f"08{n:02d}", 800 + n), operator, debit_day=1, started=JAN)
        for month in (SEP, OCT, NOV):
            status = RentChargeStatus.UNPAID if month == SEP else RentChargeStatus.PAID
            charge = a_rent_charge(
                lease,
                month,
                status,
                "100.00",
                "0.00" if status == RentChargeStatus.UNPAID else "100.00",
                day=1,
            )
            if status == RentChargeStatus.PAID:
                a_payment(charge, "100.00", month + timedelta(days=1))
        a_promise(operator, lease, on=date(2026, 10, 25), by=date(2026, 10, 30), amount="50.00")
        a_promise(operator, None, on=date(2026, 11, 2), by=date(2026, 11, 20), amount="60.00")


def count_queries(client: Client, query: str = "") -> int:
    with CaptureQueriesContext(connection) as captured:
        page(client, query)
    return len(captured.captured_queries)


@pytest.mark.parametrize("query", ["", "?kind=attention&period=year&chart=yearly"])
def test_the_query_count_is_flat_in_stores_operators_and_payments(query: str) -> None:
    client = client_for(Role.BILLING)[0]
    volume(1)
    page(client)  # warm the session row
    few = count_queries(client, query)
    volume(5)
    many = count_queries(client, query)
    assert many == few, (few, many)


def test_the_statement_timeout_wraps_the_figures(monkeypatch: pytest.MonkeyPatch) -> None:
    world()
    seen: list[str] = []

    def too_slow(compute: Any) -> Any:
        seen.append("bounded")
        raise ReportTooSlow

    monkeypatch.setattr(dashboard_view, "bounded", too_slow)
    client = client_for(Role.BILLING)[0]
    html = page(client)
    assert seen == ["bounded"]
    assert f'<p class="warning" id="dashboard-too-slow" role="status">{TOO_SLOW}</p>' in html
    assert 'id="figures-card"' not in html and 'id="mini-calendar"' in html
    response = client.get("/dashboard.csv")  # the spreadsheet answers with the page
    assert response.status_code == 200 and TOO_SLOW in response.content.decode()


# --- Labels, glyphs and Rule 8 --------------------------------------------------------------------


def test_every_control_is_labelled_and_every_chip_has_its_glyph() -> None:
    seven_kinds()
    html = page(client_for(Role.BILLING)[0])
    main = html[html.index('id="page-header"') :]
    for field in re.findall(r"<(?:input|select)\b[^>]*>", main):
        if 'type="hidden"' in field:
            continue
        found = re.search(r'id="([^"]+)"', field)
        labelled = "aria-label=" in field or (
            found is not None and f'for="{found.group(1)}"' in html
        )
        assert labelled, field
    chips = re.findall(r'<span class="status-chip status-chip-\w+">(.*?)</span></span>', html, re.S)
    assert chips and all('<svg class="icon icon-' in chip for chip in chips)
    assert 'role="img" aria-label="Collected from January 2026 to November 2026;' in html


def test_the_service_names_no_provider() -> None:
    for path in (
        BACKEND / "ledger" / "services" / "dashboard_v2.py",
        BACKEND / "ledger" / "templates" / "ledger" / "dashboard" / "_activity.html",
        BACKEND / "finance" / "templatetags" / "charts.py",
    ):
        assert "stax" not in path.read_text().lower(), path
    assert str(dashboard_v2.PAID_OUTSIDE) == f"Paid outside {provider_name()}"
