"""The arrears list (docs/qa/PLAN-011-dashboard-reports-mobile.md D3, D10; SCOPE Flow 3 and 4):
one row per operator behind, months behind bucketed 1, 2, 3+, total owed over rent,
insurance and one-off balances, last payment, channels and stores; filters, sorting,
totals, CSV, roles and a bounded number of queries."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import OneOffChargeStatus, PaymentChannel, PaymentStatus, RentChargeStatus
from ledger.services import receivables
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
JUL, AUG, SEP, OCT = (date(2026, m, 1) for m in (7, 8, 9, 10))


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def report(**params: str) -> receivables.ArrearsReport:
    return receivables.arrears(TODAY, receivables.parse_arrears_filter(params))


def by_name(result: receivables.ArrearsReport) -> dict[str, receivables.ArrearsRow]:
    return {row.operator_name: row for row in result.rows}


def build() -> dict[str, object]:
    """Three months behind, two, one, other charges only, pending only, and paid up."""
    three = an_operator("Three Months")
    lease = a_lease(a_store("1001", 1001, "GA"), three, debit_day=5)
    for month in (JUL, AUG):
        a_rent_charge(lease, month, RentChargeStatus.UNPAID, "1000.00")
    a_rent_charge(lease, SEP, RentChargeStatus.PARTIAL, "1000.00", "400.00")
    a_rent_charge(lease, OCT, RentChargeStatus.UNPAID, "1000.00", day=20)  # not yet due
    paid = a_rent_charge(lease, date(2026, 6, 1), RentChargeStatus.PAID, "1000.00", "1000.00")
    a_payment(paid, "1000.00", date(2026, 6, 8))
    an_insurance_charge(lease, 2026, date(2026, 3, 1))  # 695 overdue
    a_one_off_charge(three, "250.00", date(2026, 9, 1), lease=lease)

    two = an_operator("Two Months")
    wire = a_lease(
        a_store("2002", 2002, "FL"),
        two,
        channel=PaymentChannel.WIRE,
        debit_day=1,
        second_debit_day=15,
    )
    # A twice-monthly lease: two unpaid charges of August are one month behind.
    a_rent_charge(wire, AUG, RentChargeStatus.UNPAID, "500.00", seq=1, day=1)
    a_rent_charge(wire, AUG, RentChargeStatus.UNPAID, "500.00", seq=2, day=15)
    a_rent_charge(wire, SEP, RentChargeStatus.UNPAID, "500.00", seq=1, day=1)
    ach = a_lease(a_store("2003", 2003, "GA"), two)
    last = a_rent_charge(ach, SEP, RentChargeStatus.PAID, "10.00", "10.00")
    a_payment(last, "10.00", date(2026, 9, 9))

    one = an_operator("One Month")
    a_rent_charge(a_lease(a_store("3003", 3003, "TX"), one), SEP, RentChargeStatus.UNPAID, "800.00")

    other = an_operator("Other Only")
    a_one_off_charge(other, "120.00", date(2026, 9, 30))
    a_one_off_charge(
        other,
        "999.00",
        date(2026, 9, 30),
        status=OneOffChargeStatus.WAIVED,
        waived_at=datetime(2026, 10, 1, tzinfo=UTC),
        waive_note="Goodwill.",
    )

    pending = an_operator("Pending Only")
    charge = a_rent_charge(a_lease(a_store("4004", 4004), pending), SEP, RentChargeStatus.PENDING)
    a_payment(charge, "3500.00", None, PaymentStatus.PENDING, initiated_on=date(2026, 9, 5))

    upcoming = an_operator("Not Yet Due")
    a_one_off_charge(upcoming, "75.00", date(2026, 10, 30))
    return {"three": three, "two": two, "one": one, "other": other}


def test_months_behind_buckets_and_totals(today: date) -> None:
    build()
    rows = by_name(report())
    assert set(rows) == {"Three Months", "Two Months", "One Month", "Other Only"}
    three, two, one, other = (
        rows["Three Months"],
        rows["Two Months"],
        rows["One Month"],
        rows["Other Only"],
    )
    assert (three.months_behind, three.bucket) == (3, "3+")
    # Owed: 1000 + 1000 + 600 + 1000 (October, not yet due) + 695 + 250.
    assert three.total_owed == D("4545.00")
    assert three.overdue == D("3545.00")
    assert (two.months_behind, two.bucket, two.total_owed) == (2, "2", D("1500.00"))
    assert (one.months_behind, one.bucket, one.total_owed) == (1, "1", D("800.00"))
    assert (other.months_behind, other.bucket, other.total_owed) == (0, "0", D("120.00"))


def test_last_payment_channels_and_stores(today: date) -> None:
    build()
    rows = by_name(report())
    assert rows["Three Months"].last_payment == date(2026, 6, 8)
    assert rows["Two Months"].last_payment == date(2026, 9, 9)
    assert rows["One Month"].last_payment is None
    assert rows["Two Months"].channel_labels == ["Wire"]  # the paid-up ACH store owes nothing
    assert rows["Two Months"].store_labels == ["2002"]
    assert rows["Other Only"].store_labels == [] and rows["Other Only"].channel_labels == []


def test_bucket_totals_and_overall(today: date) -> None:
    build()
    result = report()
    assert [(bucket, count, owed) for bucket, _, count, owed in result.bucket_totals] == [
        ("1", 1, D("800.00")),
        ("2", 1, D("1500.00")),
        ("3+", 1, D("4545.00")),
        ("0", 1, D("120.00")),
    ]
    assert (result.count, result.total_owed) == (4, D("6965.00"))


def test_filters_by_operator_state_and_bucket(today: date) -> None:
    made = build()
    two = made["two"]
    assert [row.operator_name for row in report(operator=str(two.pk)).rows] == ["Two Months"]  # type: ignore[attr-defined]
    assert sorted(row.operator_name for row in report(state="ga").rows) == ["Three Months"]
    assert [row.operator_name for row in report(state="FL").rows] == ["Two Months"]
    assert [row.operator_name for row in report(bucket="3+").rows] == ["Three Months"]
    assert [row.operator_name for row in report(bucket="0").rows] == ["Other Only"]
    filtered = report(bucket="1")
    assert (filtered.count, filtered.total_owed) == (1, D("800.00"))


def test_sorting(today: date) -> None:
    build()
    names = [row.operator_name for row in report().rows]
    assert names == ["Three Months", "Two Months", "One Month", "Other Only"]
    assert [row.operator_name for row in report(sort="owed", dir="asc").rows] == [
        "Other Only",
        "One Month",
        "Two Months",
        "Three Months",
    ]
    assert [row.operator_name for row in report(sort="operator", dir="asc").rows] == [
        "One Month",
        "Other Only",
        "Three Months",
        "Two Months",
    ]
    assert report(sort="last_payment").rows[0].operator_name == "Two Months"


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"operator": "x"}, receivables.BAD_OPERATOR),
        ({"bucket": "4"}, receivables.BAD_BUCKET),
        ({"sort": "balance; DROP TABLE payments"}, receivables.BAD_SORT),
    ],
)
def test_a_bad_filter_answers_200_with_a_sentence(
    today: date, params: dict[str, str], message: str
) -> None:
    build()
    assert message in receivables.parse_arrears_filter(params).messages
    client, _ = client_for(Role.VIEWER)
    response = client.get("/arrears/", params)
    assert response.status_code == 200
    assert message in response.content.decode()
    assert response.content.decode().count('class="arrears-row"') == 4


def test_the_page_shows_rows_totals_and_sort_links(today: date) -> None:
    build()
    client, _ = client_for(Role.VIEWER)
    html = client.get("/arrears/").content.decode()
    assert '<th id="arrears-count">4</th>' in html
    assert '<th id="arrears-owed" class="money">$6,965.00</th>' in html
    assert "$4,545.00" in html and "3 or more months" in html
    assert 'href="?sort=owed&amp;dir=desc"' in html
    assert 'href="?sort=months&amp;dir=asc"' in html  # the current sort flips


def test_the_csv_has_readable_headers_and_the_filtered_rows(today: date) -> None:
    build()
    client, _ = client_for(Role.VIEWER)
    response = client.get("/arrears/export.csv?bucket=2")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    rows = list(csv.reader(io.StringIO(body)))
    assert rows == [
        list(receivables.ARREARS_CSV_HEADER),
        ["Two Months", "2", "2 months", "1500.00", "1500.00", "2026-09-09", "Wire", "2002"],
    ]


def test_csv_text_is_formula_safe(today: date) -> None:
    operator = an_operator("=cmd|' /C calc'!A0")
    a_rent_charge(a_lease(a_store("@SUM(1)", 1), operator), SEP, RentChargeStatus.UNPAID)
    client, _ = client_for(Role.VIEWER)
    body = b"".join(client.get("/arrears/export.csv").streaming_content).decode()  # type: ignore[attr-defined]
    [_, row] = list(csv.reader(io.StringIO(body)))
    assert row[0] == "'=cmd|' /C calc'!A0" and row[7] == "'@SUM(1)"


def test_nobody_behind_shows_an_empty_list(today: date) -> None:
    client, _ = client_for(Role.VIEWER)
    html = client.get("/arrears/").content.decode()
    assert "No operator is behind." in html and '<th id="arrears-count">0</th>' in html


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_reads_arrears_and_none_may_post(today: date, role: Role) -> None:
    client, _ = client_for(role)
    assert client.get("/arrears/").status_code == 200
    assert client.get("/arrears/export.csv").status_code == 200
    assert client.post("/arrears/").status_code == 403
    assert client.post("/arrears/export.csv").status_code == 403


def test_without_a_role_or_a_session_arrears_is_refused() -> None:
    assert client_for(None)[0].get("/arrears/").status_code == 403
    assert Client().get("/arrears/").status_code == 401


def _operators(start: int, count: int) -> None:
    for n in range(start, start + count):
        operator = an_operator(f"Bulk {n}")
        lease = a_lease(a_store(number=8000 + n), operator)
        for month in (JUL, AUG):
            a_rent_charge(lease, month, RentChargeStatus.UNPAID)
        paid = a_rent_charge(lease, SEP, RentChargeStatus.PAID, "3500.00", "3500.00")
        a_payment(paid, "3500.00", date(2026, 9, 7))
        an_insurance_charge(lease, 2026, date(2026, 3, 1))
        a_one_off_charge(operator, "10.00", date(2026, 9, 1), lease=lease)


def test_the_arrears_query_count_does_not_grow_with_the_data(today: date) -> None:
    def count() -> int:
        with CaptureQueriesContext(connection) as captured:
            report()
        return len(captured.captured_queries)

    _operators(0, 2)
    small = count()
    _operators(2, 25)
    assert count() == small
    assert small <= 5
