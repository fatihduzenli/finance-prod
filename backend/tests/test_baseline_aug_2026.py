"""The August 2026 baseline (docs/qa/PLAN-011-dashboard-reports-mobile.md D8): the grid, the
arrears list, the dashboard and the run summary reproduce the sheet's column sums,
392 collected for $1,470,001.57, 47 unpaid, 13 pending (docs/COLUMN_MAPPING.md §3)."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from accounts.roles import Role
from ledger.services import grid, receivables
from tests.fixtures.baseline_aug_2026 import (
    AS_OF,
    COLLECTED_COUNT,
    COLLECTED_TOTAL,
    PENDING_COUNT,
    PENDING_DEBIT_DAY,
    UNPAID_COUNT,
    UNPAID_OPERATOR_COUNT,
    Baseline,
    build_baseline,
    collected_amounts,
    pending_amounts,
    unpaid_amounts,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def baseline(monkeypatch: pytest.MonkeyPatch) -> Baseline:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: AS_OF)
    return build_baseline()


def test_the_amounts_follow_the_rule_and_are_not_uniform() -> None:
    collected = collected_amounts()
    assert len(collected) == COLLECTED_COUNT
    assert sum(collected, D("0.00")) == COLLECTED_TOTAL
    assert collected[-1] == D("9652.92")
    assert len(set(collected)) > 300
    assert all(amount == amount.quantize(D("0.01")) and amount > 0 for amount in collected)
    assert len(unpaid_amounts()) == UNPAID_COUNT and len(pending_amounts()) == PENDING_COUNT


def test_the_grid_august_column(baseline: Baseline) -> None:
    result = grid.build(grid.parse_query({"from": "2026-08", "to": "2026-08"}, AS_OF), AS_OF)
    [august] = result.columns
    assert (august.count("paid"), august.count("unpaid"), august.count("pending")) == (
        COLLECTED_COUNT,
        UNPAID_COUNT,
        PENDING_COUNT,
    )
    assert august.collected == COLLECTED_TOTAL
    assert len(result.rows) == COLLECTED_COUNT + UNPAID_COUNT + PENDING_COUNT


def test_the_grid_page_and_csv_show_the_same_column(baseline: Baseline) -> None:
    client, _ = client_for(Role.VIEWER)
    html = client.get("/grid/?from=2026-08&to=2026-08").content.decode()
    assert html.count('class="cell cell-paid"') == COLLECTED_COUNT
    assert html.count('class="cell cell-unpaid"') == UNPAID_COUNT
    assert html.count('class="cell cell-pending"') == PENDING_COUNT
    assert "$1,470,001.57" in html
    response = client.get("/grid/export.csv?from=2026-08&to=2026-08")
    rows = list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode())))  # type: ignore[attr-defined]
    cells = [row[5] for row in rows[1:]]
    paid = [cell for cell in cells if cell.startswith("paid ")]
    assert len(paid) == COLLECTED_COUNT
    assert sum((D(cell[5:].replace(",", "")) for cell in paid), D("0.00")) == COLLECTED_TOTAL
    assert sum(1 for cell in cells if cell.startswith("unpaid ")) == UNPAID_COUNT
    pending = [cell for cell in cells if cell.startswith("pending ")]
    assert len(pending) == PENDING_COUNT
    assert all(cell.endswith("(from 2026-08-09)") for cell in pending)


def test_the_arrears_count_and_total(baseline: Baseline) -> None:
    result = receivables.arrears(AS_OF)
    assert result.count == UNPAID_OPERATOR_COUNT == len(baseline.unpaid_operator_ids)
    assert {row.operator_id for row in result.rows} == baseline.unpaid_operator_ids
    assert result.total_owed == baseline.unpaid_total == D("180274.93")
    assert {row.bucket for row in result.rows} == {receivables.BUCKET_ONE}
    client, _ = client_for(Role.VIEWER)
    html = client.get("/arrears/").content.decode()
    assert f'<th id="arrears-count">{UNPAID_OPERATOR_COUNT}</th>' in html
    assert '<th id="arrears-owed">$180,274.93</th>' in html


def test_the_dashboard_figures(baseline: Baseline) -> None:
    data = receivables.dashboard(AS_OF)
    open_total = baseline.unpaid_total + baseline.pending_total
    assert (data.outstanding, data.outstanding_count) == (open_total, UNPAID_COUNT + PENDING_COUNT)
    assert (data.overdue, data.overdue_count) == (open_total, UNPAID_COUNT + PENDING_COUNT)
    assert data.paid_this_month == COLLECTED_TOTAL
    client, _ = client_for(Role.VIEWER)
    html = client.get("/").content.decode()
    assert "$1,470,001.57" in html and "$228,116.93" in html


def test_the_run_summary_on_the_pending_day(baseline: Baseline) -> None:
    summary = receivables.run_summary(date(2026, 8, PENDING_DEBIT_DAY))
    assert summary.due == baseline.due_by_day[PENDING_DEBIT_DAY]
    assert summary.pending == PENDING_COUNT
    assert summary.returned == 0
    for day in (1, 25):
        assert receivables.run_summary(date(2026, 8, day)).due == baseline.due_by_day[day]
