"""The month grid (docs/qa/PLAN-011-dashboard-reports-mobile.md D2, D10; SCOPE Flow 3 and 4):
rows, cells, colours, the note marker, the click-through, each filter, the range and its
cap, "not due" before the lease start, the CSV in the sheet's column order, roles and a
bounded number of queries."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import ChargeNote, PaymentChannel, PaymentStatus, RentCharge, RentChargeStatus
from ledger.services import grid
from ledger.services.pnl import CAPPED, SWAPPED, UNREADABLE
from tests.receivables_helpers import (
    a_lease,
    a_payment,
    a_rent_charge,
    a_store,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 6)
JUL, AUG, SEP, OCT = (date(2026, m, 1) for m in (7, 8, 9, 10))


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


@pytest.fixture
def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def page(client: Client, query: str = "") -> str:
    response = client.get(f"/grid/{query}")
    assert response.status_code == 200
    return response.content.decode()


def built(query: str = "", **params: str) -> grid.Grid:
    values = dict(part.split("=", 1) for part in query.split("&") if part)
    values.update(params)
    return grid.build(grid.parse_query(values, TODAY), TODAY)


def row_of(result: grid.Grid, label: str) -> grid.GridRow:
    [found] = [row for row in result.rows if row.store.external_label == label]
    return found


def cell(result: grid.Grid, label: str, month: date) -> grid.Cell:
    return next(item for item in row_of(result, label).cells if item.month == month)


def every_status_store() -> dict[str, RentCharge]:
    lease = a_lease(a_store("2922 TUCSON", 2922, "AZ"), an_operator("Halid"), debit_day=5)
    charges = {
        "paid": a_rent_charge(lease, date(2026, 5, 1), RentChargeStatus.PAID, "3500.00", "3500.00"),
        "unpaid": a_rent_charge(lease, date(2026, 6, 1), RentChargeStatus.UNPAID),
        "pending": a_rent_charge(lease, JUL, RentChargeStatus.PENDING),
        "partial": a_rent_charge(lease, AUG, RentChargeStatus.PARTIAL, "3500.00", "1200.00"),
        "due": a_rent_charge(lease, SEP, RentChargeStatus.DUE),
        "waived": a_rent_charge(lease, OCT, RentChargeStatus.WAIVED),
    }
    a_payment(
        charges["pending"], "3500.00", None, PaymentStatus.PENDING, initiated_on=date(2026, 7, 5)
    )
    return charges


# --- Cells, colours, marker, click-through --------------------------------------------------

#: PLAN-012c D1: each status's fill token (unpaid is a navy outline on paper).
COLOURS = {
    "paid": "var(--status-paid-bg)",
    "unpaid": "var(--status-unpaid-bg)",
    "pending": "var(--status-pending-bg)",
    "partial": "var(--status-partial-bg)",
    "due": "var(--status-due-bg)",
    "waived": "var(--status-waived-bg)",
}


def test_every_status_has_its_colour_text_and_link(today: date, viewer: Client) -> None:
    charges = every_status_store()
    html = page(viewer, "?from=2026-05&to=2026-10")
    for status, colour in COLOURS.items():
        assert f".cell-{status}{{background:{colour};" in html, status
        charge = charges[status]
        assert f'<td class="cell cell-{status}" data-month=' in html, status
        assert f'href="/charges/{charge.pk}/"' in html, status
    result = built(**{"from": "2026-05", "to": "2026-10"})
    texts = [item.text for item in row_of(result, "2922 TUCSON").cells]
    assert texts == [
        "Paid $3,500.00",
        "Unpaid $3,500.00",
        "Pending $3,500.00 (from 2026-07-05)",
        "Partly paid $1,200.00 of $3,500.00",
        "Due $3,500.00",
        "Waived",
    ]


def test_the_note_marker_shows_only_on_a_charge_with_a_note(today: date, viewer: Client) -> None:
    charges = every_status_store()
    ChargeNote.objects.create(rent_charge=charges["unpaid"], body="Short payment, see June.")
    result = built(**{"from": "2026-05", "to": "2026-10"})
    marked = [item.month for item in row_of(result, "2922 TUCSON").cells if item.has_note]
    assert marked == [date(2026, 6, 1)]
    html = page(viewer, "?from=2026-05&to=2026-10")
    assert html.count('<span class="note-marker" title="Has a note">●</span>') == 1
    assert 'aria-label="2026-06: Unpaid $3,500.00, has a note"' in html


def test_not_due_before_the_lease_started_and_blank_after(today: date) -> None:
    lease = a_lease(a_store("1083 JACKSONVILLE", 1083), started=date(2026, 8, 15))
    a_rent_charge(lease, SEP, RentChargeStatus.PAID, "3740.00", "3740.00")
    result = built(**{"from": "2026-06", "to": "2026-10"})
    statuses = [item.status for item in row_of(result, "1083 JACKSONVILLE").cells]
    assert statuses == ["not_due", "not_due", "", "paid", ""]  # August: started mid-month
    assert [item.text for item in row_of(result, "1083 JACKSONVILLE").cells][:2] == [
        "Not yet due",
        "Not yet due",
    ]


def test_a_store_without_a_lease_is_a_row_of_blank_cells(today: date) -> None:
    a_store("PLAZA")
    row = row_of(built(), "PLAZA")
    assert row.operator is None and row.channel == ""
    assert {item.text for item in row.cells} == {""}


def test_a_month_without_any_charge_is_blank_in_every_row(today: date) -> None:
    lease = a_lease(a_store("1", 1))
    a_rent_charge(lease, AUG, RentChargeStatus.PAID, "10.00", "10.00")
    result = built(**{"from": "2026-08", "to": "2026-09"})
    assert cell(result, "1", SEP).text == ""
    assert result.columns[1].collected == 0 and result.columns[1].counts == {}


def test_two_charges_of_one_month_make_one_cell_with_their_sums(today: date) -> None:
    """Twice-monthly lease (owner, Q28: the grid shows the sum)."""
    lease = a_lease(a_store("3409 LEICESTER", 3409), debit_day=1, second_debit_day=15)
    a_rent_charge(lease, AUG, RentChargeStatus.PAID, "1850.00", "1850.00", seq=1, day=1)
    a_rent_charge(lease, AUG, RentChargeStatus.UNPAID, "1850.00", seq=2, day=15)
    a_rent_charge(lease, SEP, RentChargeStatus.PAID, "1850.00", "1850.00", seq=1, day=1)
    a_rent_charge(lease, SEP, RentChargeStatus.PAID, "1850.00", "1850.00", seq=2, day=15)
    result = built(**{"from": "2026-08", "to": "2026-09"})
    assert cell(result, "3409 LEICESTER", AUG).text == "Partly paid $1,850.00 of $3,700.00"
    assert cell(result, "3409 LEICESTER", SEP).text == "Paid $3,700.00"


@pytest.mark.parametrize(
    ("statuses", "paid", "expected"),
    [
        (["paid", "paid"], "2", "paid"),
        (["waived", "waived"], "0", "waived"),
        (["paid", "waived"], "1", "paid"),
        (["pending", "unpaid"], "0", "pending"),
        (["unpaid", "unpaid"], "0", "unpaid"),
        (["unpaid", "due"], "0", "unpaid"),
        (["due", "due"], "0", "due"),
        (["partial"], "1", "partial"),
    ],
)
def test_the_combined_status(statuses: list[str], paid: str, expected: str) -> None:
    assert grid.combined_status(statuses, Decimal(paid)) == expected


def test_rows_are_in_store_number_order_with_unnumbered_stores_last(today: date) -> None:
    for label, number in (("MALL", None), ("2475", 2475), ("880", 880), ("ARBOR", None)):
        a_store(label, number)
    assert [row.store.external_label for row in built().rows] == ["880", "2475", "ARBOR", "MALL"]


def test_the_row_shows_the_current_leases_operator_and_channel(today: date) -> None:
    store = a_store("1580", 1580)
    old = a_lease(store, an_operator("Old Op"), started=date(2025, 3, 1))
    old.collection_status = "ended"
    old.ended_on = date(2026, 5, 31)
    old.save()
    a_lease(store, an_operator("Aga"), started=date(2026, 6, 1), channel=PaymentChannel.WIRE)
    row = row_of(built(), "1580")
    assert row.operator is not None and row.operator.display_name == "Aga"
    assert row.channel == PaymentChannel.WIRE


def test_column_totals_count_cells_and_sum_what_was_collected(today: date) -> None:
    every_status_store()
    other = a_lease(a_store("1444", 1444))
    a_rent_charge(other, AUG, RentChargeStatus.PAID, "3740.00", "3740.00")
    result = built(**{"from": "2026-08", "to": "2026-08"})
    [august] = result.columns
    assert (august.count("partial"), august.count("paid"), august.count("unpaid")) == (1, 1, 0)
    assert august.collected == Decimal("4940.00")


# --- Filters -------------------------------------------------------------------------------


def three_stores() -> None:
    acme = an_operator("Acme")
    a_rent_charge(
        a_lease(a_store("100", 100, "GA"), acme), AUG, RentChargeStatus.PAID, "1.00", "1.00"
    )
    a_rent_charge(
        a_lease(a_store("200", 200, "fl"), an_operator("Beta"), channel=PaymentChannel.WIRE),
        AUG,
        RentChargeStatus.UNPAID,
    )
    a_rent_charge(a_lease(a_store("300", 300, "GA"), acme), SEP, RentChargeStatus.PENDING)


def labels(result: grid.Grid) -> list[str]:
    return [row.store.external_label for row in result.rows]


def test_each_filter(today: date) -> None:
    three_stores()
    acme = str(row_of(built(), "100").operator.pk)  # type: ignore[union-attr]
    assert labels(built(operator=acme)) == ["100", "300"]
    assert labels(built(state="FL")) == ["200"]
    assert labels(built(state=" ga ")) == ["100", "300"]
    assert labels(built(channel=PaymentChannel.WIRE)) == ["200"]
    assert labels(built(status="pending")) == ["300"]
    assert labels(built(status="paid", operator=acme)) == ["100"]
    assert labels(built(status="not_due")) == []


def test_the_status_filter_keeps_a_row_with_at_least_one_matching_cell(today: date) -> None:
    lease = a_lease(a_store("400", 400))
    a_rent_charge(lease, AUG, RentChargeStatus.PAID, "1.00", "1.00")
    a_rent_charge(lease, SEP, RentChargeStatus.UNPAID)
    result = built(status="unpaid")
    assert labels(result) == ["400"]
    assert [item.status for item in result.rows[0].cells if item.status] == ["paid", "unpaid"]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"operator": "not-a-uuid"}, grid.BAD_OPERATOR),
        ({"channel": "card"}, grid.BAD_CHANNEL),
        ({"status": "' OR 1=1 --"}, grid.BAD_STATUS),
    ],
)
def test_a_bad_filter_is_dropped_with_a_sentence(
    today: date, viewer: Client, params: dict[str, str], message: str
) -> None:
    three_stores()
    query = grid.parse_query(params, TODAY)
    assert message in query.messages
    assert labels(grid.build(query, TODAY)) == ["100", "200", "300"]
    from urllib.parse import urlencode

    assert message in page(viewer, f"?{urlencode(params)}").replace("&#x27;", "'")


def test_sql_like_state_text_matches_nothing(today: date) -> None:
    three_stores()
    assert labels(built(state="GA' OR '1'='1")) == []


# --- Range ------------------------------------------------------------------------------


def test_the_default_range_is_ten_months_before_this_one_this_one_and_the_next(
    today: date,
) -> None:
    # PLAN-012d D12: 10 past months, the current and the next.
    query = grid.parse_query({}, TODAY)
    assert (query.from_month, query.to_month) == (date(2025, 12, 1), date(2026, 11, 1))
    assert len(query.months) == 12 and query.messages == ()
    assert grid.default_range(TODAY) == (date(2025, 12, 1), date(2026, 11, 1))
    # Only "to" given: the 11 months before it and that month.
    only_to = grid.parse_query({"to": "2026-06"}, TODAY)
    assert (only_to.from_month, only_to.to_month) == (date(2025, 7, 1), date(2026, 6, 1))


def test_the_range_is_capped_at_24_months(today: date, viewer: Client) -> None:
    query = grid.parse_query({"from": "2024-01", "to": "2026-12"}, TODAY)
    assert len(query.months) == 24
    assert (query.from_month, query.to_month) == (date(2025, 1, 1), date(2026, 12, 1))
    assert CAPPED.format(limit=24, start="2025-01", end="2026-12") in query.messages
    html = page(viewer, "?from=2024-01&to=2026-12")
    assert html.count('class="month-col"') == 24
    assert grid.parse_query({"from": "2025-01", "to": "2026-12"}, TODAY).messages == ()


def test_the_month_pickers_are_selects_of_the_current_month_and_24_either_side(
    today: date, viewer: Client
) -> None:
    # PLAN-012d D12 (owner, 8b and 9a): plain selects, not HTML month inputs.
    months = grid.picker_months(grid.parse_query({}, TODAY), TODAY)
    assert len(months) == 49 and (months[0], months[-1]) == (date(2024, 10, 1), date(2028, 10, 1))
    assert months == sorted(set(months))
    # A range shown outside the window is added, so it can be submitted again.
    old = grid.picker_months(grid.parse_query({"from": "2021-06", "to": "2021-07"}, TODAY), TODAY)
    assert old[:3] == [date(2021, 6, 1), date(2021, 7, 1), date(2024, 10, 1)] and len(old) == 51
    html = page(viewer, "")
    assert 'type="month"' not in html
    for name in ("from", "to"):
        select = html.split(f'<select name="{name}" id="{name}">')[1].split("</select>")[0]
        assert select.count("<option ") == 49 and select.count(" selected") == 1
    assert '<option value="2026-10">October 2026</option>' in html
    assert '<option value="2025-12" selected>December 2025</option>' in html
    assert '<option value="2026-11" selected>November 2026</option>' in html


def test_a_bad_or_reversed_range_answers_with_a_sentence(today: date, viewer: Client) -> None:
    bad = grid.parse_query({"from": "2026-13"}, TODAY)
    assert UNREADABLE.format(start="2025-12", end="2026-11") in bad.messages
    swapped = grid.parse_query({"from": "2026-09", "to": "2026-01"}, TODAY)
    assert (swapped.from_month, swapped.to_month) == (date(2026, 1, 1), SEP)
    assert SWAPPED.format(start="2026-01", end="2026-09") in swapped.messages
    assert "The months could not be read" in page(viewer, "?from=garbage&to=%00")


# --- CSV ---------------------------------------------------------------------------------


def read_csv(client: Client, query: str = "") -> list[list[str]]:
    response = client.get(f"/grid/export.csv{query}")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith('attachment; filename="grid-')
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    return list(csv.reader(io.StringIO(body)))


def test_the_csv_is_in_the_sheets_column_order(today: date, viewer: Client) -> None:
    every_status_store()
    rows = read_csv(viewer, "?from=2026-05&to=2026-10")
    assert rows[0] == [
        "Store number",
        "Store",
        "State",
        "Operator",
        "Channel",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
        "2026-10",
    ]
    assert rows[1] == [
        "2922",
        "2922 TUCSON",
        "AZ",
        "Halid",
        "Bank debit",
        "Paid $3,500.00",
        "Unpaid $3,500.00",
        "Pending $3,500.00 (from 2026-07-05)",
        "Partly paid $1,200.00 of $3,500.00",
        "Due $3,500.00",
        "Waived",
    ]


def test_the_csv_applies_the_filters_and_writes_not_due_and_blanks(
    today: date, viewer: Client
) -> None:
    three_stores()
    late = a_lease(a_store("500", 500, "FL"), started=date(2026, 9, 1))
    a_rent_charge(late, SEP, RentChargeStatus.PAID, "9.99", "9.99")
    rows = read_csv(viewer, "?from=2026-07&to=2026-09&state=FL")
    assert [row[1] for row in rows[1:]] == ["200", "500"]
    assert rows[2][5:] == ["Not yet due", "Not yet due", "Paid $9.99"]
    assert rows[1][5:] == ["", "Unpaid $3,500.00", ""]


def test_csv_text_cells_are_formula_safe(today: date, viewer: Client) -> None:
    lease = a_lease(a_store('=HYPERLINK("http://example.com")', 9), an_operator("+Plus"))
    a_rent_charge(lease, AUG, RentChargeStatus.PAID, "1.00", "1.00")
    rows = read_csv(viewer, "?from=2026-08&to=2026-08")
    assert rows[1][1] == '\'=HYPERLINK("http://example.com")'
    assert rows[1][3] == "'+Plus"


# --- Roles and queries (D6, D10) ----------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_reads_the_grid_and_its_export(today: date, role: Role) -> None:
    client, _ = client_for(role)
    assert client.get("/grid/").status_code == 200
    assert client.get("/grid/export.csv").status_code == 200
    assert client.post("/grid/").status_code == 403
    assert client.post("/grid/export.csv").status_code == 403


def test_without_a_role_or_a_session_the_grid_is_refused() -> None:
    assert client_for(None)[0].get("/grid/").status_code == 403
    assert Client().get("/grid/export.csv").status_code == 401


def test_the_page_is_keyboard_reachable_with_sticky_header_and_first_column(
    today: date, viewer: Client
) -> None:
    every_status_store()
    html = page(viewer, "?from=2026-05&to=2026-10")
    assert 'class="grid-scroll" tabindex="0"' in html
    assert "table.grid thead th{position:sticky;top:0;" in html
    assert "table.grid th.store-col{position:sticky;left:0;" in html
    assert '<th scope="row" class="store-col">' in html


def _stores(start: int, count: int) -> None:
    for n in range(start, start + count):
        lease = a_lease(a_store(number=n), an_operator(f"Op {n}"))
        for month in (JUL, AUG, SEP):
            charge = a_rent_charge(lease, month, RentChargeStatus.PENDING)
            a_payment(charge, "3500.00", None, PaymentStatus.PENDING, initiated_on=month)
        ChargeNote.objects.create(rent_charge=charge, body="Note.")


def _queries() -> int:
    with CaptureQueriesContext(connection) as captured:
        built(**{"from": "2026-01", "to": "2026-12"})
    return len(captured.captured_queries)


def test_the_grid_query_count_does_not_grow_with_stores_or_months(today: date) -> None:
    _stores(0, 2)
    small = _queries()
    _stores(2, 30)
    assert _queries() == small
    assert small <= 5
