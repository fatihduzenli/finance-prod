"""The dashboard's mini calendar card and the page's query count
(docs/qa/PLAN-012f-b-staging-review.md Batch A, D6; §6).

PLAN-015 Batch 1 removed the Cashflow and Payment status cards with their services and the
``bars`` and ``donut`` tags; their tests went with them (the git history keeps them). What stays:
the mini calendar's dots against the Calendar page's own read, its month parameter, its links on
the page, the dashboard script's rules, the chart keys and the query count. The new page's tests
are tests/test_dashboard_v2.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.template import Context, Template
from django.test import Client

from accounts.roles import Role
from ledger.models import CalendarEntry, CalendarEntryKind, RentChargeStatus, Store
from ledger.services import calendar
from ledger.services import dashboard as blocks
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store
from tests.test_operator_views import client_for
from tests.test_pnl import a_cost, a_setup, paid, rent
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
OCT = date(2026, 10, 1)
SEP = date(2026, 9, 1)
AUG = date(2026, 8, 1)
JUN = date(2026, 6, 1)
JAN = date(2026, 1, 1)
#: The dashboard GET's queries plus a margin. PLAN-015 Batch 1 re-pin: 98 measured on
#: SQLite with the seeded cards' data (was 86 for the PLAN-014 page).
MAX_DASHBOARD_QUERIES = 100


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


# --- Seeds ----------------------------------------------------------------------------------


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


def status_month() -> None:
    """October: one overdue, one paid, one upcoming, one pending, one waived."""
    a_rent_charge(a_lease(), OCT, RentChargeStatus.UNPAID, "1000.00", day=1)
    paid_charge = a_rent_charge(a_lease(), OCT, RentChargeStatus.PAID, "1500.00", "1500.00", day=3)
    a_payment(paid_charge, "1500.00", date(2026, 10, 3))
    a_rent_charge(a_lease(), OCT, RentChargeStatus.DUE, "1200.00", day=20)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.PENDING, "900.00", day=5)
    a_rent_charge(a_lease(), OCT, RentChargeStatus.WAIVED, "800.00", day=8)


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


# --- The chart keys -------------------------------------------------------------------------


def render(source: str, **context: object) -> str:
    return Template("{% load charts %}" + source).render(Context(context))


def test_the_chart_keys_carry_the_colours_and_an_unknown_key_is_a_mistake() -> None:
    # PLAN-015 Batch 1 re-pin: the Collections legend's two keys.
    assert 'fill="var(--chart-income)"' in render('{% chart_key "collected" %}')
    assert 'fill="var(--green)"' in render('{% chart_key "current" %}')
    for gone in ("expenses", "net", "overdue", "nope"):
        with pytest.raises(ValueError):
            render('{% chart_key "' + gone + '" %}')


# --- The page -----------------------------------------------------------------------------


def page(client: Client, **params: str) -> str:
    response = client.get("/", params)
    assert response.status_code == 200
    return response.content.decode()


def test_the_mini_calendar_steps_months_keeps_the_page_state_and_lists_items(today: date) -> None:
    lease = a_lease(debit_day=5)
    CalendarEntry.objects.create(
        kind=CalendarEntryKind.NOTE, on=date(2026, 10, 12), title="Call <b>landlord</b>"
    )
    client, _ = client_for(Role.VIEWER)
    html = page(client, period="year")
    # PLAN-012g C3: the month's name is the link; a dot per kind, coloured by its kind class.
    assert '<a id="mini-month" href="/calendar/?month=2026-10">Oct 2026</a>' in html
    # PLAN-015 Batch 1 re-pin: ‹ › keep the page's other choices (the period here), as they
    # kept the Cashflow range.
    assert 'id="mini-previous" href="?cal=2026-09&amp;period=year"' in html
    assert 'id="mini-next" href="?cal=2026-11&amp;period=year"' in html
    assert html.count('class="mini-dot kind-debit"') == 1  # the 5th
    assert html.count('class="mini-dot kind-note"') == 1  # the 12th
    assert html.count('class="mini-dot ') == 2
    assert 'class="calendar-item kind-debit"' in html and 'class="calendar-item kind-note"' in html
    assert "Call &lt;b&gt;landlord&lt;/b&gt;" in html and "<b>landlord" not in html
    assert 'aria-label="Oct 12, 2026: 1 item"' in html
    assert 'class="mini-day is-today"' in html
    assert 'data-day="2026-10-05"' in html and has_token(html, lease.store.external_label)
    november = page(client, cal="2026-11")
    assert 'href="/calendar/?month=2026-11">Nov 2026</a>' in november


def test_the_dashboard_script_toggles_and_submits_without_markup() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "ledger" / "static" / "ledger" / "dashboard.js"
    ).read_text()
    assert "innerHTML" not in script and "fetch(" not in script
    assert 'classList.add("is-open")' in script and "form.submit()" in script
    # PLAN-015 Batch 1: the Collections tooltip is built with textContent from data-* only.
    assert "textContent" in script and 'getAttribute("data-collected")' in script


def test_the_dashboard_query_count_is_pinned(
    today: date, django_assert_max_num_queries: Any
) -> None:
    """Fable's MINOR on A-2/A-3: the dashboard GET, seeded with the cards' data, stays within
    its measured count plus a small margin (PLAN-015 Batch 1 re-pin: the four KPIs, the
    Collections bars, the operator tiles, the activity feed and the mini calendar)."""
    seeded_stores()
    status_month()
    a_lease(debit_day=5)
    client, _ = client_for(Role.BILLING)
    with django_assert_max_num_queries(MAX_DASHBOARD_QUERIES):
        page(client)
