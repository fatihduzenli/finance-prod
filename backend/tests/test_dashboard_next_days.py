"""The mini calendar's "Next 7 days" list (owner 2026-11-01): the items of today and the six days
after (across a month end; the eighth day left out), by day then kind, at most six rows and
"+N more", the store's title linked, the day panel's words, the amounts (a rent charge's, the
rent run's figure when the charge is not made, a re-debit's price; none when waived), the empty
line, "All in Calendar ›", the day panels' data for every day with items, the card's column
ending after its content, the script's Back control, and the queries: a fixed number however
many items there are."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import (
    CalendarEntry,
    CalendarEntryKind,
    ContactEntry,
    ContactKind,
    ContactStatus,
    FeeType,
    InstallmentPlan,
    InsuranceCharge,
    Lease,
    OneOffCategory,
    OneOffChargeStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
    Store,
)
from ledger.services import calendar, rent_run
from ledger.services import dashboard as blocks
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
#: A Wednesday: the window is Oct 28 to Nov 3, the eighth day Nov 4.
TODAY = date(2026, 10, 28)
OCT = date(2026, 10, 1)
SEP = date(2026, 9, 1)
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def a_note(day: date, title: str, *, store: Store | None = None, kind: str = "note") -> None:
    CalendarEntry.objects.create(
        kind=CalendarEntryKind.NOTE if kind == "note" else CalendarEntryKind.REMINDER,
        on=day,
        title=title,
        store=store,
    )


def a_returned_debit(lease: Lease) -> Payment:
    """September's debit, returned: what a re-debit charge comes from."""
    charge = a_rent_charge(lease, SEP, due="1200.00", day=10)
    return a_payment(charge, "1200.00", None, status=PaymentStatus.RETURNED)


def a_world() -> dict[str, Any]:
    """One item of each kind within the window, Oct 28 to Nov 3, and one either side."""
    istanbul = a_store("1991 Istanbul", 1991)
    paris = a_store("2484 Paris", 2484)
    a_lease(istanbul, an_operator("Ops A"), debit_day=30)  # Oct 30, no charge yet: $3,500.00
    paris_ops = an_operator("Ops B")
    paris_lease = a_lease(paris, paris_ops, debit_day=28)
    a_rent_charge(paris_lease, OCT, due="1200.00", day=28)  # Oct 28, made: $1,200.00
    a_note(date(2026, 10, 29), "Ring the bank", kind="reminder")
    insured = a_store("3003 Rome", 3003)
    insured.open_date, insured.needs_open_date = date(2019, 10, 31), False
    insured.save(update_fields=["open_date", "needs_open_date"])
    a_lease(insured, an_operator("Ops D"), debit_day=10)  # Oct 31: the anniversary
    ending = a_lease(a_store("4004 Oslo", 4004), an_operator("Ops C"), debit_day=10)
    ending.ended_on = date(2026, 11, 1)
    ending.save(update_fields=["ended_on"])
    redebit = a_one_off_charge(
        paris_ops,
        "250.00",
        date(2026, 11, 2),
        lease=paris_lease,
        category=OneOffCategory.REDEBIT,
        source_payment=a_returned_debit(paris_lease),
    )
    a_note(date(2026, 11, 3), "Call landlord", store=istanbul)  # the seventh: "+1 more"
    a_note(date(2026, 11, 4), "Day eight")  # outside the window
    a_note(date(2026, 10, 27), "Yesterday")  # outside the window
    return {"istanbul": istanbul, "paris": paris, "redebit": redebit}


def rows_of(upcoming: blocks.NextDays) -> list[tuple[date, str, str, str, Decimal | None]]:
    return [(row.on, row.kind, row.what, row.store, row.amount) for row in upcoming.rows]


def page(client: Client, query: str = "") -> str:
    response = client.get(f"/{query}")
    assert response.status_code == 200
    return response.content.decode()


def mini(html: str) -> str:
    start = html.index('id="mini-calendar"')
    return html[start : html.index("</section>", start)]


# --- The service --------------------------------------------------------------------------------


def test_the_list_holds_the_next_seven_days_across_the_month_end_six_rows_then_more() -> None:
    a_world()
    upcoming = blocks.mini_calendar(OCT, TODAY).upcoming
    assert (upcoming.start, upcoming.end) == (TODAY, date(2026, 11, 3))
    assert rows_of(upcoming) == [
        (date(2026, 10, 28), calendar.KIND_DEBIT, "Debit day", "#2484 Paris", D("1200.00")),
        (date(2026, 10, 29), calendar.KIND_REMINDER, "Ring the bank", "", None),
        (date(2026, 10, 30), calendar.KIND_DEBIT, "Debit day", "#1991 Istanbul", D("3500.00")),
        (date(2026, 10, 31), calendar.KIND_INSURANCE, "Insurance anniversary", "#3003 Rome", None),
        (date(2026, 11, 1), calendar.KIND_LEASE_END, "Lease ends", "#4004 Oslo", None),
        (date(2026, 11, 2), calendar.KIND_REDEBIT, "Re-debit", "#2484 Paris", D("250.00")),
    ]
    assert upcoming.more == 1  # Nov 3's note; Nov 4 and Oct 27 are not counted
    assert [row.label for row in upcoming.rows[:2]] == ["Oct 28", "Oct 29"]


def test_the_list_follows_today_whatever_month_the_grid_shows() -> None:
    a_world()
    march = blocks.mini_calendar(date(2027, 3, 1), TODAY)
    assert len(march.upcoming.rows) == blocks.NEXT_ROWS and march.upcoming.more == 1
    assert all(cell.day.month == 3 for cell in march.days_with_items)  # the grid keeps its month


def test_the_grid_shows_only_its_month_although_the_read_covers_the_window() -> None:
    a_world()
    october = blocks.mini_calendar(OCT, TODAY)
    days = {cell.day for cell in october.days_with_items}
    assert date(2026, 11, 1) not in days and date(2026, 10, 30) in days
    # Nov 1 to 7 are the grid's last week: no dots there.
    november_cells = [cell for week in october.weeks for cell in week if not cell.in_month]
    assert all(not cell.items for cell in november_cells)


def test_a_day_s_items_are_ordered_by_kind_and_the_rest_counted() -> None:
    store = a_store("1501 Lima", 1501)
    a_note(date(2026, 10, 29), "A note", store=store)
    a_note(date(2026, 10, 29), "A reminder", kind="reminder")
    a_lease(store, an_operator("Lima Ops"), debit_day=29)
    for n in range(7):
        a_note(date(2026, 10, 30), f"Note {n}")
    upcoming = blocks.mini_calendar(OCT, TODAY).upcoming
    assert [row.kind for row in upcoming.rows[:3]] == [
        calendar.KIND_DEBIT,
        calendar.KIND_REMINDER,
        calendar.KIND_NOTE,
    ]
    assert len(upcoming.rows) == blocks.NEXT_ROWS and upcoming.more == 10 - blocks.NEXT_ROWS


def test_nothing_in_the_window_is_an_empty_list() -> None:
    a_note(date(2026, 11, 4), "Day eight")
    upcoming = blocks.mini_calendar(OCT, TODAY).upcoming
    assert upcoming.rows == [] and upcoming.more == 0
    assert upcoming.empty_line == "Nothing in the next 7 days."


def test_the_amounts_follow_the_charge_the_rent_run_and_the_waive() -> None:
    # A twice-monthly lease: two halves, the odd cent on the first debit, as the rent run.
    a_lease(a_store("5005 Kyiv", 5005), debit_day=28, second_debit_day=30, rent="3000.01")
    # A lease starting this month after the 1st: the rent run's prorated figure.
    started = a_lease(a_store("6006 Baku", 6006), debit_day=31, started=date(2026, 10, 15))
    # A waived charge: no amount.
    waived = a_lease(a_store("7007 Riga", 7007), debit_day=29)
    a_rent_charge(waived, OCT, RentChargeStatus.WAIVED, "900.00", day=29)
    # A store-less bank debit of an operator: the whole title, no store.
    a_one_off_charge(
        an_operator("Solo Ops"),
        "75.50",
        date(2026, 11, 1),
        category=OneOffCategory.REDEBIT,
        source_payment=a_returned_debit(a_lease(a_store("8008 Lyon", 8008), debit_day=10)),
    )
    rows = {(row.on, row.store): row for row in blocks.mini_calendar(OCT, TODAY).upcoming.rows}
    assert rows[date(2026, 10, 28), "#5005 Kyiv"].amount == D("1500.01")
    assert rows[date(2026, 10, 28), "#5005 Kyiv"].what == "Debit day, debit 1"
    assert rows[date(2026, 10, 30), "#5005 Kyiv"].amount == D("1500.00")
    expected = rent_run.first_debit_amount(Lease.objects.get(pk=started.pk), OCT)
    assert expected is not None and expected < D("3500.00")
    assert rows[date(2026, 10, 31), "#6006 Baku"].amount == expected
    assert rows[date(2026, 10, 29), "#7007 Riga"].amount is None
    solo = rows[date(2026, 11, 1), ""]
    assert (solo.what, solo.amount, solo.store_url) == ("Re-debit: Solo Ops", D("75.50"), "")


def test_the_list_reads_a_fixed_number_of_queries_however_many_items() -> None:
    a_world()

    def measured() -> int:
        with CaptureQueriesContext(connection) as captured:
            blocks.mini_calendar(OCT, TODAY)
        return len(captured.captured_queries)

    # The Calendar's three; the promises, the one-off charges and the insurance charges
    # (three); then the stores, the rent charges, the rent in effect and the re-debits behind
    # the six rows shown: ten.
    first = measured()
    assert first == 3 + 3 + 4
    for n in range(12):
        a_note(date(2026, 11, 3), f"More {n}", store=a_store(f"9{n:03d} Extra", 9000 + n))
    # Twelve more items (and stores) in the window: the same reads, never one per row.
    assert measured() == first


def test_promises_installments_and_due_dates_read_a_fixed_number_of_queries() -> None:
    money_world()

    def measured() -> int:
        with CaptureQueriesContext(connection) as captured:
            blocks.mini_calendar(OCT, TODAY)
        return len(captured.captured_queries)

    # The Calendar's three; the promises, the one-off charges, the insurance charges and
    # their re-debits (four); the rows' stores (one): eight.
    first = measured()
    assert first == 3 + 4 + 1
    money_world(copies=5, start=1)  # five times the promises, installments and due dates
    # Never more (the six rows are now store-less promises: no store read).
    assert measured() <= first


# --- Promises, installments and due dates (Fable's review of the owner's list) ------------------


def a_promise(lease: Lease | None, operator: Any, day: date, amount: str, **fields: Any) -> None:
    ContactEntry.objects.create(
        operator=operator,
        lease=lease,
        kind=ContactKind.PROMISE,
        happened_on=date(2026, 10, 20),
        promised_date=day,
        promised_amount=D(amount),
        body="Friday.",
        **fields,
    )


def a_plan(lease: Lease, status: str = "active") -> InstallmentPlan:
    assert lease.operator is not None
    parent = a_one_off_charge(
        lease.operator, "1200.00", date(2026, 10, 30), lease=lease, description="Shelving"
    )
    extra: dict[str, Any] = {}
    if status == "cancelled":
        extra = {"cancelled_at": datetime(2026, 10, 1, tzinfo=UTC), "note": "Stopped."}
    return InstallmentPlan.objects.create(
        one_off_charge=parent,
        operator=lease.operator,
        lease=lease,
        count=3,
        first_due=date(2026, 10, 3),
        cadence="monthly",
        collection="manual",
        status=status,
        confirmed_at=datetime(2026, 9, 20, tzinfo=UTC),
        **extra,
    )


def an_installment(plan: InstallmentPlan, number: int, day: date, **fields: Any) -> None:
    assert plan.operator is not None
    a_one_off_charge(
        plan.operator,
        "400.00",
        day,
        lease=plan.lease,
        category=OneOffCategory.INSTALLMENT,
        description=f"Installment {number} of 3: Shelving",
        plan=plan,
        installment_no=number,
        **fields,
    )


def money_world(copies: int = 1, start: int = 0) -> dict[str, Any]:
    """Each new kind in the window (Oct 28 to Nov 3) and on Nov 4, with the states left out:
    a kept and a broken promise, a deleted one; a paid installment, one of a cancelled plan;
    a deposit, a paid and a waived one-off charge, a plan's parent; a paid insurance charge."""
    found: dict[str, Any] = {}
    for n in range(start, start + copies):
        operator = an_operator(f"Promise Ops {n}")
        lease = a_lease(a_store(f"{5100 + n} Lagos", 5100 + n), operator, debit_day=15)
        a_promise(lease, operator, date(2026, 11, 2), "3500.00")
        a_promise(None, an_operator(f"Loose Ops {n}"), date(2026, 10, 29), "90.00")
        a_promise(lease, operator, date(2026, 11, 4), "1.00")  # day eight
        a_promise(lease, operator, date(2026, 10, 30), "2.00", status=ContactStatus.BROKEN)
        a_promise(lease, operator, date(2026, 10, 30), "3.00", status=ContactStatus.KEPT)
        plan = a_plan(lease)
        an_installment(plan, 2, date(2026, 11, 3), status=OneOffChargeStatus.PARTIAL, paid="150.00")
        an_installment(plan, 1, date(2026, 10, 29), status=OneOffChargeStatus.PAID, paid="400.00")
        an_installment(plan, 3, date(2026, 11, 4))  # day eight
        stopped = a_plan(a_lease(a_store(f"{5200 + n} Accra", 5200 + n), debit_day=15))
        stopped.status, stopped.cancelled_at, stopped.note = (
            "cancelled",
            datetime(2026, 10, 1, tzinfo=UTC),
            "Stopped.",
        )
        stopped.save(update_fields=["status", "cancelled_at", "note"])
        an_installment(stopped, 1, date(2026, 10, 31))  # a cancelled plan's: left out
        a_one_off_charge(operator, "80.00", date(2026, 10, 31), lease=lease, description="Key")
        a_one_off_charge(
            operator,
            "60.00",
            date(2026, 10, 31),
            lease=lease,
            description="Paid one",
            status=OneOffChargeStatus.PAID,
            paid="60.00",
        )
        a_one_off_charge(
            operator,
            "70.00",
            date(2026, 10, 31),
            lease=lease,
            description="Waived one",
            status=OneOffChargeStatus.WAIVED,
            waived_at=datetime(2026, 10, 20, tzinfo=UTC),
            waive_note="Goodwill.",
        )
        deposit_type = FeeType.objects.create(
            code=f"deposit-{n}", name="Security deposit", refundable=True
        )
        a_one_off_charge(
            operator,
            "500.00",
            date(2026, 10, 31),
            lease=lease,
            category=OneOffCategory.SETUP,
            fee_type=deposit_type,
            description="",
        )
        insurance = InsuranceCharge.objects.create(
            lease=lease, policy_year=2026, due_on=date(2026, 11, 1), amount=D("695.00")
        )
        paid_insurance = InsuranceCharge.objects.create(
            lease=lease, policy_year=2025, due_on=date(2026, 11, 1), amount=D("100.00")
        )
        Payment.objects.create(
            insurance_charge=paid_insurance,
            kind=PaymentKind.INSURANCE,
            policy_year=2026,
            method=PaymentMethod.ACH,
            amount=D("100.00"),
            status=PaymentStatus.SETTLED,
            initiated_on=date(2026, 10, 1),
            settled_on=date(2026, 10, 3),
        )
        Payment.objects.create(
            insurance_charge=insurance,
            kind=PaymentKind.INSURANCE,
            policy_year=2026,
            method=PaymentMethod.ACH,
            amount=D("95.00"),
            status=PaymentStatus.SETTLED,
            initiated_on=date(2026, 10, 1),
            settled_on=date(2026, 10, 3),
        )
        if not found:
            found = {"lease": lease, "operator": operator, "plan": plan}
    return found


def test_promises_installments_and_due_dates_are_listed_with_their_amounts() -> None:
    world = money_world()
    upcoming = blocks.mini_calendar(OCT, TODAY).upcoming
    assert rows_of(upcoming) == [
        (date(2026, 10, 29), blocks.KIND_PROMISE, "Promise to pay", "Loose Ops 0", D("90.00")),
        # A cancelled plan's parent is owed again; the live plan's parent is not listed.
        (date(2026, 10, 30), blocks.KIND_DUE, "Shelving", "#5200 Accra", D("1200.00")),
        (date(2026, 10, 31), blocks.KIND_DUE, "Key", "#5100 Lagos", D("80.00")),
        (
            date(2026, 11, 1),
            blocks.KIND_DUE,
            "Insurance, policy year 2026",
            "#5100 Lagos",
            D("600.00"),
        ),
        (date(2026, 11, 2), blocks.KIND_PROMISE, "Promise to pay", "#5100 Lagos", D("3500.00")),
        (
            date(2026, 11, 3),
            blocks.KIND_INSTALLMENT,
            "Installment 2 of 3",
            "#5100 Lagos",
            D("250.00"),
        ),
    ]
    assert upcoming.more == 0  # the kept, broken, paid, waived, deposit, parent, day 8: out
    links = {row.kind: row.url for row in upcoming.rows}
    assert links[blocks.KIND_PROMISE] == f"/operators/{world['operator'].pk}/#contacts"
    assert links[blocks.KIND_INSTALLMENT].startswith("/one-off-charges/")
    assert upcoming.rows[0].store_url == ""  # a promise without a lease names its operator


def test_a_deleted_promise_and_a_paused_plan_s_installment() -> None:
    operator = an_operator("Paused Ops")
    lease = a_lease(a_store("5300 Cairo", 5300), operator, debit_day=15)
    a_promise(
        lease,
        operator,
        date(2026, 10, 30),
        "5.00",
        deleted_at=datetime(2026, 10, 21, tzinfo=UTC),
        delete_reason="Typo.",
        deleted_by=User.objects.create_user("deleter"),
    )
    plan = a_plan(lease, status="paused")  # a paused plan's installments stay owed
    an_installment(plan, 1, date(2026, 10, 30))
    rows = blocks.mini_calendar(OCT, TODAY).upcoming.rows
    assert [(row.kind, row.amount) for row in rows] == [(blocks.KIND_INSTALLMENT, D("400.00"))]


def test_the_new_kinds_have_dots_a_legend_and_the_day_s_panel_lists_them() -> None:
    money_world()
    html = mini(page(client_for(Role.VIEWER)[0]))
    start = html.index('data-day="2026-10-31"')
    cell = html[start : html.index("</div>", start)]
    assert 'class="mini-dot kind-due"' in cell
    assert "Key: #5100 Lagos" in cell  # the day panel's words
    assert "Waived one" not in cell and "Security deposit" not in cell
    legend = html[html.index('id="mini-legend"') :]
    for label in ("Promise to pay", "Due date"):
        assert label in legend
    october = blocks.mini_calendar(OCT, TODAY)
    kinds = {item.kind for cell in october.days_with_items for item in cell.items}
    assert {blocks.KIND_PROMISE, blocks.KIND_DUE} <= kinds
    # Nov 2 and 3 are November's: the grid's November shows the promise and the installment.
    november = blocks.mini_calendar(date(2026, 11, 1), TODAY)
    by_day = {cell.day: {item.kind for item in cell.items} for cell in november.days_with_items}
    assert blocks.KIND_PROMISE in by_day[date(2026, 11, 2)]
    assert blocks.KIND_INSTALLMENT in by_day[date(2026, 11, 3)]
    assert blocks.KIND_INSTALLMENT in by_day[date(2026, 11, 4)]  # the month, past the window
    css = (BACKEND / "finance" / "static" / "finance" / "app.css").read_text()
    for kind in ("promise", "installment", "due"):
        assert f".mini-calendar .kind-{kind} {{ --dot-colour: var(--" in css


def test_an_insurance_charge_due_on_the_anniversary_is_the_anniversary_s_row() -> None:
    operator = an_operator("Insured Ops")
    store = a_store("5400 Lima", 5400)
    store.open_date, store.needs_open_date = date(2019, 10, 30), False
    store.save(update_fields=["open_date", "needs_open_date"])
    lease = a_lease(store, operator, debit_day=15)
    InsuranceCharge.objects.create(
        lease=lease, policy_year=2026, due_on=date(2026, 10, 30), amount=D("695.00")
    )
    # The same lease's other charge, due on another day: a due date.
    InsuranceCharge.objects.create(
        lease=lease, policy_year=2025, due_on=date(2026, 11, 2), amount=D("120.00")
    )
    october = blocks.mini_calendar(OCT, TODAY)
    assert rows_of(october.upcoming) == [
        (
            date(2026, 10, 30),
            calendar.KIND_INSURANCE,
            "Insurance anniversary",
            "#5400 Lima",
            D("695.00"),
        ),
        (
            date(2026, 11, 2),
            blocks.KIND_DUE,
            "Insurance, policy year 2025",
            "#5400 Lima",
            D("120.00"),
        ),
    ]
    [day] = [cell for cell in october.days_with_items if cell.day == date(2026, 10, 30)]
    assert [item.kind for item in day.items] == [calendar.KIND_INSURANCE] and day.kinds == [
        calendar.KIND_INSURANCE
    ]
    html = mini(page(client_for(Role.VIEWER)[0]))
    start = html.index('data-day="2026-10-30"')
    cell = html[start : html.index("</div>", start)]
    assert cell.count('<li class="calendar-item') == 1 and "kind-due" not in cell


def test_an_anniversary_whose_charge_is_paid_keeps_no_amount() -> None:
    store = a_store("5500 Quito", 5500)
    store.open_date, store.needs_open_date = date(2019, 10, 30), False
    store.save(update_fields=["open_date", "needs_open_date"])
    lease = a_lease(store, an_operator("Paid Ops"), debit_day=15)
    charge = InsuranceCharge.objects.create(
        lease=lease, policy_year=2026, due_on=date(2026, 10, 30), amount=D("100.00")
    )
    Payment.objects.create(
        insurance_charge=charge,
        kind=PaymentKind.INSURANCE,
        policy_year=2026,
        method=PaymentMethod.ACH,
        amount=D("100.00"),
        status=PaymentStatus.SETTLED,
        initiated_on=date(2026, 10, 1),
        settled_on=date(2026, 10, 3),
    )
    rows = blocks.mini_calendar(OCT, TODAY).upcoming.rows
    assert [(row.kind, row.what, row.amount) for row in rows] == [
        (calendar.KIND_INSURANCE, "Insurance anniversary", None)
    ]


# --- The page -------------------------------------------------------------------------------------


def test_the_card_lists_the_rows_with_store_links_amounts_more_and_the_calendar_link() -> None:
    world = a_world()
    html = mini(page(client_for(Role.VIEWER)[0]))
    upcoming = html[html.index('id="mini-upcoming"') :]
    assert '<h3 id="mini-upcoming-heading">Next 7 days</h3>' in upcoming
    link = '<a class="card-link" id="mini-upcoming-all" href="/calendar/">All in Calendar ›</a>'
    assert link in upcoming
    listed = upcoming[upcoming.index('id="mini-upcoming-list"') : upcoming.index("</ul>")]
    assert listed.count('<li class="mini-upcoming-row') == blocks.NEXT_ROWS
    assert f'<a href="/stores/{world["istanbul"].pk}/">#1991 Istanbul</a>' in listed
    assert f'<a href="/stores/{world["paris"].pk}/">#2484 Paris</a>' in listed
    assert '<span class="mini-upcoming-amount">$1,200.00</span>' in listed
    assert '<span class="mini-upcoming-amount">$3,500.00</span>' in listed
    assert '<span class="mini-upcoming-amount">$250.00</span>' in listed
    assert '<span class="mini-upcoming-on">Oct 30</span>' in listed
    assert f'href="/one-off-charges/{world["redebit"].pk}/">Re-debit</a>' in listed
    assert '<span class="mini-upcoming-store">—</span>' in listed  # the reminder has no store
    assert '<p class="mini-upcoming-more" id="mini-upcoming-more">+1 more</p>' in upcoming
    assert "Day eight" not in listed and "mini-upcoming-none" not in upcoming
    # The day panel sits in the list's place, a polite live region, hidden until a tap.
    assert '<div class="mini-panel" id="mini-panel" aria-live="polite" hidden></div>' in upcoming
    # No id is used twice in the card (the month's › is id="mini-next").
    ids = re.findall(r' id="([^"]+)"', html)
    assert len(ids) == len(set(ids)) and 'id="mini-next" href="?cal=2026-11"' in html


def test_every_day_with_items_carries_its_list_for_the_tap() -> None:
    a_world()
    html = mini(page(client_for(Role.VIEWER)[0]))
    october = blocks.mini_calendar(OCT, TODAY)
    days = october.days_with_items
    assert days
    for cell in days:
        start = html.index(f'data-day="{cell.day:%Y-%m-%d}"')
        end = html.index("</div>", start)
        assert '<ul class="mini-popover">' in html[start:end], cell.day
        for item in cell.items:
            assert item.url in html[start:end]
    assert html.count('<ul class="mini-popover">') == len(days)


def test_an_empty_window_says_so_in_one_muted_line() -> None:
    html = mini(page(client_for(Role.VIEWER)[0]))
    line = '<p class="muted mini-upcoming-none" id="mini-upcoming-none">'
    assert f"{line}Nothing in the next 7 days.</p>" in html
    assert 'id="mini-upcoming-list"' not in html and 'id="mini-upcoming-more"' not in html
    assert 'id="mini-upcoming-all" href="/calendar/"' in html


def test_markup_in_an_entry_title_is_escaped_in_the_list() -> None:
    a_note(TODAY, 'Call <script>alert("x")</script>')
    html = mini(page(client_for(Role.VIEWER)[0]))
    listed = html[html.index('id="mini-upcoming-list"') :]
    assert "Call &lt;script&gt;" in listed and "<script>alert" not in html


def test_the_mini_calendar_s_column_ends_after_its_card() -> None:
    css = (BACKEND / "finance" / "static" / "finance" / "app.css").read_text()
    assert ".dashboard-side-fit { align-self: start; }" in css
    html = page(client_for(Role.VIEWER)[0])
    column = html.index('<div class="dashboard-side dashboard-side-fit">')
    # PLAN-015 Batch 1 re-pin: the column beside the Recent activity card, the page's last row.
    assert html.index('id="activity-card"') < column < html.index('id="mini-calendar"')
    assert html.count("dashboard-side-fit") == 1
    rules = css[css.index(".mini-upcoming {") : css.index(".mini-days-list {")]
    assert "#" not in rules.replace("#mini", "")  # tokens only: no literal colour


def test_the_script_puts_the_tapped_day_in_the_list_s_place_with_a_back_control() -> None:
    script = (BACKEND / "ledger" / "static" / "ledger" / "dashboard.js").read_text()
    assert 'getElementById("mini-upcoming-body")' in script
    assert "upcoming.hidden = true" in script and "upcoming.hidden = false" in script
    assert 'back.textContent = "Back to next 7 days"' in script
    assert "innerHTML" not in script
