"""The dashboard's Cashflow, Payment status and mini calendar cards and the new grid
(docs/qa/PLAN-012f-b-staging-review.md Batch A, slices A-2 and A-3: D3 to D6; §6).

The shared range (``parse_range``: the four keys, the custom checks, the 36-month cap); the
Cashflow card on payments, costs and a store without a cost row (left out of Expenses and
counted, never zero); the reconciliation with the P&L for each of the four ranges; the
Payment status card's boxes on a seeded month; the mini calendar's dots against the Calendar
page's own read; the two SVG tags; and the page: the order of the cards, the range form, the
mini calendar's links and popovers, and a viewer reading every card.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.template import Context, Template
from django.test import Client

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CalendarEntry,
    CalendarEntryKind,
    LeaseCredit,
    MoneyMethod,
    PaymentMethod,
    RentChargeStatus,
    Store,
)
from ledger.services import calendar, pnl
from ledger.services import dashboard as blocks
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store
from tests.test_operator_views import client_for
from tests.test_pnl import a_cost, a_setup, paid, rent

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
OCT = date(2026, 10, 1)
SEP = date(2026, 9, 1)
AUG = date(2026, 8, 1)
JUN = date(2026, 6, 1)
MAY = date(2026, 5, 1)
JAN = date(2026, 1, 1)
#: The dashboard GET's queries with the default range (73 measured at A-2/A-3, 76 at A5) plus
#: a margin; PLAN-012f-b §10 D20 moved the pin 80 → 81.
MAX_DASHBOARD_QUERIES = 81  # 76 measured on sqlite (A5: 75 before, plus the fees query of D20)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


# --- The range ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "key", "start", "end", "label"),
    [
        ({}, "this", OCT, OCT, "This month"),
        ({"range": "this"}, "this", OCT, OCT, "This month"),
        ({"range": "3m"}, "3m", AUG, OCT, "Last 3 months"),
        ({"range": "12m"}, "12m", date(2025, 11, 1), OCT, "Last 12 months"),
        (
            {"range": "custom", "from": "2026-02", "to": "2026-04"},
            "custom",
            date(2026, 2, 1),
            date(2026, 4, 1),
            "Feb 2026 to Apr 2026",
        ),
        ({"range": "custom", "from": "2026-05", "to": "2026-05"}, "custom", MAY, MAY, "May 2026"),
    ],
)
def test_the_four_keys(
    params: dict[str, str], key: str, start: date, end: date, label: str
) -> None:
    found = blocks.parse_range(params, TODAY)
    assert found == blocks.Range(key, label, start, end)


def test_a_custom_range_of_36_months_is_kept_and_37_refused() -> None:
    kept = blocks.parse_range({"range": "custom", "from": "2023-11", "to": "2026-10"}, TODAY)
    assert kept is not None and len(kept.months) == 36
    assert (
        blocks.parse_range({"range": "custom", "from": "2023-10", "to": "2026-10"}, TODAY) is None
    )


@pytest.mark.parametrize(
    "params",
    [
        {"range": "custom"},
        {"range": "custom", "from": "2026-01"},
        {"range": "custom", "to": "2026-01"},
        {"range": "custom", "from": "2026-05", "to": "2026-04"},  # swapped: refused, not swapped
        {"range": "custom", "from": "2026-13", "to": "2026-14"},
        {"range": "custom", "from": "2026-1", "to": "2026-02"},
        {"range": "custom", "from": "２０２６-01", "to": "2026-02"},
        {"range": "custom", "from": "1999-01", "to": "1999-02"},
        {"range": "week"},
        {"range": "<script>"},
    ],
)
def test_an_unusable_range_is_none(params: dict[str, str]) -> None:
    assert blocks.parse_range(params, TODAY) is None
    assert blocks.RANGE_MESSAGE == (
        "Choose a range of whole months, the earlier month first, at most three years."
    )


def test_the_range_query_writes_months_only_for_custom() -> None:
    assert blocks.Range("3m", "Last 3 months", AUG, OCT).as_query() == {"range": "3m"}
    custom = blocks.Range("custom", "", AUG, OCT).as_query()
    assert custom == {"range": "custom", "from": "2026-08", "to": "2026-10"}


# --- Cashflow (D3, D4) --------------------------------------------------------------------


def seeded_stores() -> tuple[Store, Store]:
    """Store A: landlord 2,500 and other 100 from January, rent 3,500 a month paid on the
    2nd, a setup fee 1,000 (cost 500) paid in October. Store B: a lease and October's rent
    paid, no cost row."""
    store_a = a_store("A")
    lease_a = a_lease(store_a, started=JAN)
    a_cost(store_a, "2500.00", JAN, other_monthly_cost=D("100.00"))
    for month in (JUN, AUG, SEP, OCT):
        charge = rent(lease_a, month, status=RentChargeStatus.PAID, amount_paid=D("3500.00"))
        paid(charge, "3500.00", month.replace(day=2))
    setup = a_setup(lease_a, due=date(2026, 10, 3))
    paid(setup, "1000.00", date(2026, 10, 4))
    store_b = a_store("B")
    lease_b = a_lease(store_b, started=JAN)
    charge = rent(lease_b, OCT, "2000.00", status=RentChargeStatus.PAID, amount_paid=D("2000"))
    paid(charge, "2000.00", date(2026, 10, 3))
    return store_a, store_b


def test_cashflow_leaves_a_store_without_a_cost_out_of_expenses_and_counts_it() -> None:
    seeded_stores()
    card = blocks.cashflow(blocks.Range("this", "This month", OCT, OCT))
    assert card.income == D("6500.00")  # 3,500 + 1,000 + 2,000
    assert card.expenses == D("3100.00")  # 2,500 + 100 + 500: store B is not zero, it is out
    assert card.net == D("3400.00")
    assert card.stores_without_cost == 1
    assert card.without_cost_line == "1 store without a cost entered"
    october = card.months[-1]
    assert (october.month, october.income, october.expenses, october.net) == (
        OCT,
        D("6500.00"),
        D("3100.00"),
        D("3400.00"),
    )


def test_a_month_with_only_stores_without_a_cost_has_unknown_expenses() -> None:
    store = a_store("C")
    lease = a_lease(store, started=JAN)
    charge = rent(lease, OCT, status=RentChargeStatus.PAID, amount_paid=D("3500.00"))
    paid(charge, "3500.00", date(2026, 10, 2))
    card = blocks.cashflow(blocks.Range("this", "This month", OCT, OCT))
    assert card.income == D("3500.00")
    assert card.expenses is None and card.net is None
    assert card.months[-1].expenses is None and card.months[-1].net is None
    assert card.without_cost_line == "1 store without a cost entered"


def test_an_empty_month_is_zero_and_says_nothing_about_costs() -> None:
    card = blocks.cashflow(blocks.Range("this", "This month", OCT, OCT))
    assert (card.income, card.expenses, card.net) == (D(0), D(0), D(0))
    assert card.without_cost_line == ""
    assert len(card.months) == blocks.CASHFLOW_BARS


def test_a_short_range_is_padded_to_twelve_bars_on_the_left() -> None:
    card = blocks.cashflow(blocks.Range("3m", "Last 3 months", AUG, OCT))
    assert [bar.month for bar in card.months][0] == date(2025, 11, 1)
    assert [bar.month for bar in card.months][-1] == OCT
    assert [bar.in_range for bar in card.months] == [False] * 9 + [True] * 3


def test_a_long_range_draws_its_last_twelve_months_and_totals_all_of_them() -> None:
    seeded_stores()
    long = blocks.Range("custom", "", date(2025, 1, 1), OCT)
    card = blocks.cashflow(long)
    assert len(card.months) == 12 and all(bar.in_range for bar in card.months)
    assert card.months[0].month == date(2025, 11, 1)
    total = pnl.total(pnl.rows(pnl.PnlQuery(from_month=long.from_month, to_month=OCT)))
    assert card.income == total.rent_collected + total.fees_collected
    assert card.stores_without_cost == 1


def known_cost_stores() -> None:
    """Two stores with their costs entered, collected and costed across the year."""
    for label, landlord in (("A", "2500.00"), ("D", "1800.00")):
        store = a_store(label)
        lease = a_lease(store, started=date(2025, 6, 1))
        a_cost(store, landlord, date(2025, 6, 1), other_monthly_cost=D("75.00"))
        for month in (date(2025, 12, 1), JUN, AUG, SEP, OCT):
            charge = rent(lease, month, status=RentChargeStatus.PAID, amount_paid=D("3000"))
            paid(charge, "3000.00", month.replace(day=3))
        setup = a_setup(lease, price="400.00", cost="150.00", due=date(2026, 9, 9))
        paid(setup, "400.00", date(2026, 9, 10))


@pytest.mark.parametrize(
    "params",
    [
        {"range": "this"},
        {"range": "3m"},
        {"range": "12m"},
        {"range": "custom", "from": "2026-06", "to": "2026-09"},
    ],
)
def test_the_card_reconciles_with_the_pnl_for_each_range(params: dict[str, str]) -> None:
    known_cost_stores()
    selected = blocks.parse_range(params, TODAY)
    assert selected is not None
    card = blocks.cashflow(selected)
    report = pnl.total(
        pnl.rows(pnl.PnlQuery(from_month=selected.from_month, to_month=selected.to_month))
    )
    landlord, other, fees = report.landlord_rent, report.other_costs, report.fee_costs
    assert landlord is not None and other is not None and fees is not None
    assert card.income == report.rent_collected + report.fees_collected
    assert card.expenses == landlord + other + fees
    assert card.expenses == report.costs
    assert card.net == report.margin_collected
    assert card.stores_without_cost == 0
    bars = sum((bar.income for bar in card.months if bar.in_range), D(0))
    assert bars == card.income


def test_with_a_store_without_a_cost_the_expenses_are_the_pnl_costs_of_the_others() -> None:
    store_a, _ = seeded_stores()
    selected = blocks.Range("3m", "Last 3 months", AUG, OCT)
    card = blocks.cashflow(selected)
    rows = pnl.rows(pnl.PnlQuery(from_month=AUG, to_month=OCT))
    report = pnl.total(rows)
    assert report.landlord_rent is None  # the P&L says unknown; the card leaves it out
    assert card.income == report.rent_collected + report.fees_collected
    assert card.expenses == pnl.total(row for row in rows if row.store_id == store_a.pk).costs
    assert card.stores_without_cost == 1


# --- Payment status (D5) ------------------------------------------------------------------


def status_month() -> None:
    """October: one overdue, one paid, one upcoming, one pending, one waived."""
    a_rent_charge(a_lease(), OCT, RentChargeStatus.UNPAID, "1000.00", day=1)
    paid_charge = a_rent_charge(a_lease(), OCT, RentChargeStatus.PAID, "1500.00", "1500.00", day=3)
    a_payment(paid_charge, "1500.00", date(2026, 10, 3))
    a_rent_charge(a_lease(), OCT, RentChargeStatus.DUE, "1200.00", day=20)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.PENDING, "900.00", day=5)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.WAIVED, "800.00", day=8)


def test_payment_status_counts_a_seeded_month() -> None:
    status_month()
    card = blocks.payment_status(blocks.Range("this", "This month", OCT, OCT), TODAY)
    assert (card.overdue.count, card.overdue.amount) == (1, D("1000.00"))
    assert (card.paid.count, card.paid.amount) == (1, D("1500.00"))
    assert (card.upcoming.count, card.upcoming.amount) == (1, D("1200.00"))
    assert card.pending == 1  # the waived charge is nowhere
    assert card.total == D("3700.00")
    assert [part.key for part in card.parts] == ["overdue", "paid", "upcoming"]


def test_partly_paid_is_overdue_by_its_balance_when_past_due_else_pending() -> None:
    a_rent_charge(a_lease(), OCT, RentChargeStatus.PARTIAL, "1000.00", "400.00", day=2)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.PARTIAL, "1000.00", "250.00", day=25)
    card = blocks.payment_status(blocks.Range("this", "This month", OCT, OCT), TODAY)
    assert (card.overdue.count, card.overdue.amount) == (1, D("600.00"))
    assert card.upcoming.count == 0 and card.pending == 1


def test_a_charge_due_today_is_upcoming_and_outside_the_range_is_not_counted() -> None:
    a_rent_charge(a_lease(), OCT, RentChargeStatus.DUE, "700.00", day=6)
    a_rent_charge(a_lease(), SEP, RentChargeStatus.UNPAID, "999.00", day=5)
    card = blocks.payment_status(blocks.Range("this", "This month", OCT, OCT), TODAY)
    assert (card.upcoming.count, card.upcoming.amount) == (1, D("700.00"))
    assert card.overdue.count == 0
    wider = blocks.payment_status(blocks.Range("3m", "Last 3 months", AUG, OCT), TODAY)
    assert (wider.overdue.count, wider.overdue.amount) == (1, D("999.00"))


def test_a_charge_paid_through_any_path_counts_as_paid() -> None:
    """Fable's MAJOR on A-2: Paid is every charge due in the range whose grid status is paid,
    by its amount paid, whatever paid it and whenever. The card reads the rent charge's
    stored status and amount paid only, so each path is a paid charge with a different
    payment (or none) behind it."""
    early = a_rent_charge(a_lease(), OCT, RentChargeStatus.PAID, "500.00", "500.00", day=1)
    a_payment(early, "500.00", date(2026, 9, 30))  # settled before the range
    credit_lease = a_lease()
    credited = a_rent_charge(credit_lease, OCT, RentChargeStatus.PAID, "300.00", "300.00", day=2)
    credit = LeaseCredit.objects.create(
        lease=credit_lease,
        amount=D("300.00"),
        remaining=D("0.00"),
        method=MoneyMethod.WIRE,
        received_on=date(2026, 9, 20),
        allocation_group=uuid.uuid4(),
        note="Overpaid September",
        reference="Wire 1001",
    )
    applied = a_payment(credited, "300.00", date(2026, 10, 2))
    applied.method, applied.credit = PaymentMethod.CREDIT, credit
    applied.save(update_fields=["method", "credit"])
    # Paid by its installment plan: the debits are payments on the installment one-off
    # charges, none on the rent charge itself.
    a_rent_charge(a_lease(), OCT, RentChargeStatus.PAID, "1200.00", "1200.00", day=3)
    card = blocks.payment_status(blocks.Range("this", "This month", OCT, OCT), TODAY)
    assert (card.paid.count, card.paid.amount) == (3, D("2000.00"))
    assert card.total == D("2000.00")
    # The range is by due date: a charge due in September is in no box of October.
    a_rent_charge(a_lease(), SEP, RentChargeStatus.PAID, "700.00", "700.00", day=1)
    again = blocks.payment_status(blocks.Range("this", "This month", OCT, OCT), TODAY)
    assert again.paid.count == 3


# --- The mini calendar (D6) ---------------------------------------------------------------


def test_the_dots_are_the_days_items_in_returns(today: date) -> None:
    a_lease(debit_day=5)
    a_lease(debit_day=31)
    CalendarEntry.objects.create(kind=CalendarEntryKind.NOTE, on=date(2026, 10, 12), title="Call")
    mini = blocks.mini_calendar(OCT, TODAY)
    first, last = calendar.month_bounds(OCT)
    expected = {item.on for item in calendar.items_in(((first, last),), calendar.Filters())}
    dotted = {cell.day for week in mini.weeks for cell in week if cell.items}
    assert (
        dotted == expected and {date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 31)} <= dotted
    )
    assert (mini.month, mini.previous, mini.next) == (OCT, SEP, date(2026, 11, 1))
    days = [cell for week in mini.weeks for cell in week]
    assert [cell.day for cell in days if cell.today] == [TODAY]
    assert all(cell.in_month == (cell.day.month == 10) for cell in days)
    assert days[0].day.weekday() == 6  # Sunday first, as the Calendar page


def test_the_cal_parameter_falls_back_to_the_current_month() -> None:
    assert blocks.parse_cal("2026-12", TODAY) == date(2026, 12, 1)
    for raw in ("", "2026-13", "nope", "2026-1", "1999-01"):
        assert blocks.parse_cal(raw, TODAY) == OCT


# --- The SVG tags (D3) --------------------------------------------------------------------


def render(source: str, **context: object) -> str:
    return Template("{% load charts %}" + source).render(Context(context))


def months() -> list[blocks.CashflowMonth]:
    expenses = D("50.00")
    found = []
    for index in range(12):
        month = pnl.add_months(date(2025, 11, 1), index)
        known = expenses if index != 3 else None
        found.append(
            blocks.CashflowMonth(
                month=month,
                income=D("100.00") * index,
                expenses=known,
                net=D("100.00") * index - known if known is not None else None,
                in_range=index >= 9,
            )
        )
    return found


def test_the_bars_are_a_pair_per_month_with_a_line_and_an_accessible_name() -> None:
    html = render("{% bars rows %}", rows=months())
    assert html.startswith('<svg class="bars-chart" viewBox="0 0 720.0 208.0" role="img"')
    assert 'aria-label="Income and expenses by month from Nov 2025 to Oct 2026' in html
    assert html.count('fill="#0E8F86"') == 12
    assert html.count('fill="#8FA3BF"') == 11  # the month without its costs has no bar
    assert html.count("<rect") == 23
    assert html.count('<g class="bar-month"') == 12
    # The nine months before the range have their bars faded, never their labels.
    assert html.count('<g class="bar-pair" opacity="0.4">') == 9
    assert html.count("<polyline") == 2  # the unknown month breaks the net line
    assert 'stroke="#0B2545"' in html
    assert "<title>Income Oct 2026: $1,100.00</title>" in html
    assert "<title>Expenses Feb 2026" not in html
    assert ">Oct 2026</text>" in html and "<script" not in html


def test_every_month_known_draws_one_rect_pair_per_month() -> None:
    rows = [
        blocks.CashflowMonth(pnl.add_months(date(2025, 11, 1), i), D(10), D(5), D(5), True)
        for i in range(12)
    ]
    html = render("{% bars rows %}", rows=rows)
    assert html.count("<rect") == 24 and html.count("<polyline") == 1


def test_a_negative_net_reads_below_the_zero_line() -> None:
    rows = [blocks.CashflowMonth(OCT, D(0), D(100), D(-100), True)]
    html = render("{% bars rows %}", rows=rows)
    zero = re.search(r'<line x1="0" y1="([0-9.]+)"', html)
    point = re.search(r'<polyline points="[0-9.]+,([0-9.]+)"', html)
    assert zero is not None and point is not None
    assert float(point.group(1)) > float(zero.group(1))


def labels(html: str) -> list[tuple[str, bool]]:
    """Each month label's text and whether it stays on a narrow chart."""
    return [
        (text, "bar-label-minor" not in classes)
        for classes, text in re.findall(r'<text class="([^"]+)"[^>]*>([^<]*)</text>', html)
    ]


def test_every_month_is_labelled_and_a_narrow_chart_keeps_every_third_and_both_ends() -> None:
    # Fable's A3-1: on a phone the twelve labels ran past the card; the kept ones fit.
    html = render("{% bars rows today %}", rows=months(), today=date(2026, 10, 25))
    found = labels(html)
    assert [text for text, _ in found] == [
        "Nov 2025", "Dec 2025", "Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026",
        "May 2026", "Jun 2026", "Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026",
    ]  # fmt: skip
    kept = [text for text, major in found if major]
    # The range starts in Aug 2026 (in_range from index 9) and today is in Oct 2026; every
    # third month back from Oct 2026, less Jul 2026 beside Aug 2026.
    assert kept == ["Jan 2026", "Apr 2026", "Aug 2026", "Oct 2026"]
    # A bar's title names its month whether its label is kept or not.
    assert "<title>Income Nov 2025: $0.00</title>" in html


def test_the_range_start_and_today_are_labelled_wherever_they_fall() -> None:
    rows = [
        blocks.CashflowMonth(pnl.add_months(date(2025, 11, 1), i), D(10), D(5), D(5), i >= 4)
        for i in range(12)
    ]
    kept = [text for text, major in labels(render("{% bars rows %}", rows=rows)) if major]
    assert kept == ["Jan 2026", "Mar 2026", "Jul 2026", "Oct 2026"]  # Apr is beside Mar
    html = render("{% bars rows today %}", rows=rows, today=date(2026, 6, 3))
    kept = [text for text, major in labels(html) if major]
    assert kept == ["Jan 2026", "Mar 2026", "Jun 2026", "Oct 2026"]
    # Never two kept labels side by side.
    indexes = [i for i, (_, major) in enumerate(labels(html)) if major]
    assert all(b - a > 1 for a, b in zip(indexes, indexes[1:], strict=False)), indexes


def test_a_today_outside_the_drawn_months_adds_no_label() -> None:
    from finance.templatetags.charts import major_months

    rows = months()
    assert major_months(rows, date(2030, 1, 1)) == major_months(rows)
    assert major_months([], date(2026, 10, 1)) == set()


def test_on_a_phone_the_chart_fits_its_card_with_the_kept_labels() -> None:
    from tests.test_plan012f_d_pages import APP_CSS

    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 719.98px) {", 1)[1].split("\n}\n", 1)[0]
    for rule in (
        ".cashflow-chart .bars-chart { min-width: 0; }",
        ".bars-chart .bar-label-minor { display: none; }",
        ".bars-chart .bar-month:last-of-type .bar-label { text-anchor: end; }",
    ):
        assert rule in phone, rule


def test_the_dashboard_passes_today_to_the_chart() -> None:
    template = Path(__file__).resolve().parents[1] / "ledger/templates/ledger/dashboard"
    assert "{% bars cashflow.months data.today %}" in (template / "_cashflow.html").read_text()


@dataclass
class Part:
    key: str
    label: str
    amount: Decimal


def test_the_donut_draws_an_arc_per_part_and_the_total_in_the_middle() -> None:
    parts = [
        Part("overdue", "Overdue", D("100")),
        Part("paid", "Paid", D("300")),
        Part("upcoming", "Upcoming", D("0")),
    ]
    html = render("{% donut parts total %}", parts=parts, total=D("400.00"))
    assert 'stroke="#8A1C14"' in html and 'stroke="#176B33"' in html
    assert 'stroke="#0B2545"' not in html  # a zero part draws nothing
    assert 'stroke-dasharray="25.0 75.0"' in html and 'stroke-dasharray="75.0 25.0"' in html
    assert 'stroke-dashoffset="-25.0"' in html
    assert '<span class="donut-amount">$400.00</span>' in html
    assert 'aria-label="Total $400.00: Overdue $100.00, Paid $300.00"' in html


def test_an_empty_donut_is_the_ring_and_zero() -> None:
    html = render("{% donut parts total %}", parts=[], total=D(0))
    assert html.count("<circle") == 1 and "$0.00" in html


# --- The page (A-3) -----------------------------------------------------------------------


def page(client: Client, **params: str) -> str:
    response = client.get("/", params)
    assert response.status_code == 200
    return response.content.decode()


def test_the_cards_are_in_the_owners_order(today: date) -> None:
    client, _ = client_for(Role.BILLING)
    html = page(client)
    order = [
        'id="figures-card"',
        'id="cashflow-card"',
        'id="attention"',
        'id="payment-status"',
        'id="recent-payments"',
        'id="mini-calendar"',
        'id="collection-card"',
        'id="coming-up"',
        'id="today-run"',
        'id="failed-attempts"',
        'id="not-sent-card"',
        'id="last-reconciliation-card"',
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert html.count('class="dashboard-row-thirds"') == 2
    assert 'src="/static/ledger/dashboard.js"' in html


def test_a_viewer_reads_every_card_and_nothing_is_written(today: date) -> None:
    status_month()
    client, _ = client_for(Role.VIEWER)
    before = AuditLog.objects.count()
    html = page(client, range="3m")
    assert AuditLog.objects.count() == before
    assert 'id="cashflow-income">$1,500.00<' in html  # the one settled payment
    # The five leases have no cost row: left out of Expenses and counted.
    assert 'id="cashflow-without-cost">5 stores without a cost entered<' in html
    assert 'id="cashflow-expenses">—<' in html and 'id="cashflow-net">—<' in html
    assert 'role="img"' in html and 'class="bars-chart"' in html
    assert '<span class="donut-amount">' in html
    assert 'id="payment-status-pending">+ 1 pending<' in html
    assert 'id="status-overdue"' in html and 'id="status-paid"' in html
    assert 'id="status-upcoming"' in html
    form = html[
        html.index('id="cashflow-range"') - 80 : html.index(
            "</form>", html.index('id="cashflow-range"')
        )
    ]
    assert 'method="get"' in form and "csrfmiddlewaretoken" not in form
    assert '<option value="3m" selected>Last 3 months</option>' in form


def test_an_unusable_range_says_so_once_and_computes_neither_card(today: date) -> None:
    client, _ = client_for(Role.VIEWER)
    html = page(client, range="custom", **{"from": "2026-09", "to": "2026-05"})
    assert html.count(blocks.RANGE_MESSAGE) == 1
    assert 'class="bars-chart"' not in html and 'class="donut"' not in html
    assert 'id="payment-status-none"' in html
    assert 'class="range-form is-custom"' in html
    assert '<option value="2026-09" selected>' in html  # the months asked for stay chosen


def test_the_mini_calendar_steps_months_keeps_the_range_and_lists_items(today: date) -> None:
    lease = a_lease(debit_day=5)
    CalendarEntry.objects.create(
        kind=CalendarEntryKind.NOTE, on=date(2026, 10, 12), title="Call <b>landlord</b>"
    )
    client, _ = client_for(Role.VIEWER)
    html = page(client, range="12m")
    assert 'href="/calendar/?month=2026-10">Calendar</a>' in html
    assert 'id="mini-previous" href="?cal=2026-09&amp;range=12m"' in html
    assert 'id="mini-next" href="?cal=2026-11&amp;range=12m"' in html
    assert 'id="mini-month">Oct 2026<' in html
    assert html.count('class="mini-dot"') == 2  # the 5th (debit day) and the 12th (note)
    assert 'class="calendar-item kind-debit"' in html and 'class="calendar-item kind-note"' in html
    assert "Call &lt;b&gt;landlord&lt;/b&gt;" in html and "<b>landlord" not in html
    assert 'aria-label="Oct 12, 2026: 1 item"' in html
    assert 'class="mini-day is-today"' in html
    assert 'data-day="2026-10-05"' in html and lease.store.external_label in html
    november = page(client, cal="2026-11")
    assert 'id="mini-month">Nov 2026<' in november
    assert '<input type="hidden" name="cal" value="2026-11">' in november


def test_the_dashboard_script_toggles_and_submits_without_markup() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "ledger" / "static" / "ledger" / "dashboard.js"
    ).read_text()
    assert "innerHTML" not in script and "fetch(" not in script
    assert 'classList.add("is-open")' in script and "form.submit()" in script


def test_the_chart_keys_carry_the_colours_and_an_unknown_key_is_a_mistake() -> None:
    assert 'fill="#8FA3BF"' in render('{% chart_key "expenses" %}')
    assert 'fill="#8A1C14"' in render('{% chart_key "overdue" %}')
    assert '<line x1="0" y1="4" x2="12" y2="4" stroke="#0B2545"' in render('{% chart_key "net" %}')
    with pytest.raises(ValueError):
        render('{% chart_key "nope" %}')


def test_the_dashboard_query_count_is_pinned(
    today: date, django_assert_max_num_queries: Any
) -> None:
    """Fable's MINOR on A-2/A-3: the dashboard GET with the default range, seeded with the
    cards' data, stays within its measured count plus a small margin (the new cards add
    the P&L's fourteen queries, one for Payment status and the Calendar's three)."""
    seeded_stores()
    status_month()
    a_lease(debit_day=5)
    client, _ = client_for(Role.BILLING)
    with django_assert_max_num_queries(MAX_DASHBOARD_QUERIES):
        page(client)
