"""Proration (docs/qa/PLAN-012e-lease-end-transfer-installments.md D1, D2; §6 "Proration"):
the arithmetic and its text, and the rent run's first month of a lease that starts after
the 1st (the charge, its text on the charge page, the grid title and the statement). The
last month (D3) is in ``test_lease_end.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest

from accounts.roles import Role
from ledger.models import AuditLog, Lease, RentCharge
from ledger.services import grid, proration, receivables, rent_run
from tests.test_lease_services import make_lease, make_operator
from tests.test_operator_views import client_for

D = Decimal
OCTOBER = date(2026, 10, 1)
TEXT = "13–31 Oct, 19 of 31 days of $2,000.00 = $1,225.81"


# --- D1: the arithmetic ---------------------------------------------------------------------


def test_the_owners_example_is_rounded_once() -> None:
    part = proration.prorated(D("2000.00"), OCTOBER, 13, 31)
    assert (part.days_used, part.days_in_month, part.amount) == (19, 31, D("1225.81"))
    assert part.text == TEXT and not part.full


def test_the_rule_is_one_rounding_not_a_rounded_daily_rate() -> None:
    """The owner's rule text (a daily rate rounded to the cent, times the days) gives
    $1,225.88; the owner's example answer, $1,225.81, is one rounding of the whole. The
    plan follows the example and asks the owner to confirm (D1)."""
    per_day = (D("2000.00") / 31).quantize(D("0.01"), rounding=ROUND_HALF_UP) * 19
    assert per_day == D("1225.88")
    assert proration.prorated(D("2000.00"), OCTOBER, 13, 31).amount == D("1225.81")


@pytest.mark.parametrize(
    ("month", "first", "last", "rent", "amount", "text"),
    [
        # February of a leap year: 15 of 29 days.
        (
            date(2028, 2, 1),
            15,
            29,
            "2900.00",
            "1500.00",
            "15–29 Feb, 15 of 29 days of $2,900.00 = $1,500.00",
        ),
        # A 30-day month.
        (
            date(2026, 9, 1),
            16,
            30,
            "3000.00",
            "1500.00",
            "16–30 Sep, 15 of 30 days of $3,000.00 = $1,500.00",
        ),
        # A single day; half a cent rounds up (0.025 -> 0.03).
        (date(2026, 9, 1), 1, 1, "0.75", "0.03", "1 Sep, 1 of 30 days of $0.75 = $0.03"),
        # A large rent keeps its thousands separators.
        (
            date(2026, 10, 1),
            1,
            19,
            "12345.67",
            "7566.70",
            "1–19 Oct, 19 of 31 days of $12,345.67 = $7,566.70",
        ),
    ],
)
def test_months_of_every_length(
    month: date, first: int, last: int, rent: str, amount: str, text: str
) -> None:
    part = proration.prorated(D(rent), month, first, last)
    assert (part.amount, part.text) == (D(amount), text)


@pytest.mark.parametrize("month", [date(2026, 2, 1), date(2028, 2, 1), date(2026, 9, 1), OCTOBER])
def test_the_1st_to_the_last_day_is_the_full_month_without_text(month: date) -> None:
    part = proration.prorated(D("2000.00"), month, 1, proration.days_in(month))
    assert (part.amount, part.text, part.full) == (D("2000.00"), "", True)


@pytest.mark.parametrize(
    ("rent", "month", "first", "last", "message"),
    [
        ("2000.00", date(2026, 10, 2), 1, 5, proration.MONTH_NOT_FIRST),
        ("2000.00", OCTOBER, 0, 5, proration.DAYS_OUT_OF_RANGE),
        ("2000.00", OCTOBER, 6, 5, proration.DAYS_OUT_OF_RANGE),
        ("2000.00", date(2026, 9, 1), 1, 31, proration.DAYS_OUT_OF_RANGE),
        ("0.00", OCTOBER, 1, 5, proration.RENT_NOT_POSITIVE),
        ("-1.00", OCTOBER, 1, 5, proration.RENT_NOT_POSITIVE),
    ],
)
def test_bad_input_is_refused(rent: str, month: date, first: int, last: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        proration.prorated(D(rent), month, first, last)


# --- D2: the rent run's first month ----------------------------------------------------------


def a_lease(**overrides: Any) -> Lease:
    values: dict[str, Any] = {"debit_day": 15, "started_on": date(2026, 10, 13)}
    values.update(overrides)
    if "operator" not in values:
        values["operator"] = make_operator(email=None, customer=None)
    return make_lease(D("2000.00"), **values)


def run(day: date) -> list[RentCharge]:
    rent_run.create_charges(day, actor=None)
    return list(RentCharge.objects.order_by("period_month", "seq"))


@pytest.mark.django_db
def test_a_lease_starting_on_the_13th_bills_19_of_31_days_with_the_text() -> None:
    lease = a_lease()
    assert lease.prorate_first_month is True
    [due] = rent_run.leases_due(date(2026, 10, 15)).due
    assert (due.amount, due.proration_text) == (D("1225.81"), TEXT)
    [charge] = run(date(2026, 10, 15))
    assert (charge.amount_due, charge.proration_text) == (D("1225.81"), TEXT)
    created = AuditLog.objects.get(action="rent_charge.created")
    assert created.after is not None and created.after["proration_text"] == TEXT
    [november] = [row for row in run(date(2026, 11, 15)) if row.period_month.month == 11]
    assert (november.amount_due, november.proration_text) == (D("2000.00"), None)


@pytest.mark.django_db
def test_the_switch_off_bills_the_full_first_month() -> None:
    a_lease(prorate_first_month=False)
    [charge] = run(date(2026, 10, 15))
    assert (charge.amount_due, charge.proration_text) == (D("2000.00"), None)


@pytest.mark.django_db
def test_a_lease_starting_on_the_1st_bills_a_full_month() -> None:
    a_lease(started_on=OCTOBER)
    [charge] = run(date(2026, 10, 15))
    assert (charge.amount_due, charge.proration_text) == (D("2000.00"), None)


@pytest.mark.django_db
def test_two_debits_split_the_prorated_month_and_a_debit_before_the_start_is_skipped() -> None:
    lease = a_lease(debit_day=15, second_debit_day=28)
    run(date(2026, 10, 15))
    [first, second] = run(date(2026, 10, 28))
    assert (first.seq, first.amount_due, second.seq, second.amount_due) == (
        1,
        D("612.91"),
        2,
        D("612.90"),
    )
    assert first.proration_text == second.proration_text == TEXT
    other = a_lease(debit_day=5, second_debit_day=20)
    assert run(date(2026, 10, 5)) == [first, second]  # the 5th is before the start
    [late] = [row for row in run(date(2026, 10, 20)) if row.lease_id == other.pk]
    assert (late.seq, late.amount_due) == (2, D("612.90"))
    assert lease.pk != other.pk


@pytest.mark.django_db
def test_a_lease_whose_end_month_has_passed_is_never_charged() -> None:
    lease = a_lease(started_on=date(2026, 1, 1))
    Lease.objects.filter(pk=lease.pk).update(ended_on=date(2026, 9, 30))
    assert run(date(2026, 9, 15))[0].period_month == date(2026, 9, 1)
    plan = rent_run.leases_due(date(2026, 10, 15))
    assert plan.due == () and [item.reason for item in plan.skipped] == [rent_run.SKIP_ENDED]
    assert rent_run.due_counts(date(2026, 10, 15)).due == 0
    assert rent_run.due_counts(date(2026, 9, 15)).due == 1


@pytest.mark.django_db
def test_the_text_is_on_the_charge_page_the_grid_title_and_the_statement() -> None:
    lease = a_lease()
    [charge] = run(date(2026, 10, 15))
    client, _ = client_for(Role.VIEWER)
    page = client.get(f"/charges/{charge.pk}/").content.decode()
    assert f'<dd id="proration-text">{TEXT}</dd>' in page
    cell = grid.build(grid.parse_query({"from": "2026-10", "to": "2026-10"}, OCTOBER), OCTOBER)
    [row] = [item for item in cell.rows if item.store.pk == lease.store_id]
    assert row.cells[0].proration_text == TEXT
    grid_page = client.get("/grid/?from=2026-10&to=2026-10").content.decode()
    assert f'title="{TEXT}"' in grid_page
    [open_line] = receivables.open_balances(date(2026, 10, 20))
    assert open_line.description == f"Rent 2026-10: {TEXT}"


@pytest.mark.django_db
def test_the_lease_form_has_the_switch_on_by_default_and_saves_it_off() -> None:
    client, _ = client_for(Role.ADMIN)
    form = client.get("/leases/new/").content.decode()
    assert "Prorate first month" in form
    assert (
        'name="prorate_first_month"' in form
        and "checked" in form.split('name="prorate_first_month"')[1].split(">")[0]
    )
    lease = a_lease()
    data = {
        "store": str(lease.store_id),
        "operator": str(lease.operator_id),
        "debit_day": "15",
        "second_debit_day": "",
        "payment_channel": lease.payment_channel,
        "started_on": "2026-10-13",
        "collection_status": lease.collection_status,
        "rent": "2000.00",
        "rent_effective_from": "",
    }
    response = client.post(f"/leases/{lease.pk}/edit/", data)
    assert response.status_code == 302
    assert Lease.objects.get(pk=lease.pk).prorate_first_month is False
    entry = AuditLog.objects.filter(action="lease.updated").latest("id")
    assert entry.before is not None and entry.after is not None
    assert (entry.before["prorate_first_month"], entry.after["prorate_first_month"]) == (
        True,
        False,
    )
