"""Adversarial cases for PLAN-012e-4 (the grid's colour families, blank cells outside the lease,
the store column), by the QA owner (plan §4). A store name with markup is escaped in the store
cell; the overdue boundary puts a charge due today in the soft shade and one due yesterday in
the dark shade, both reading "Unpaid"; months before a lease starts or after it ends are blank
(no "Not yet due"); two leases on one store leave no gap; a cell with a charge is never blank;
the legend has no "Due" item; the badges and the cells share the same tokens and no due token
remains."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.test import Client

from accounts.roles import Role
from ledger.models import PaymentChannel, RentChargeStatus
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_grid import TODAY, page
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
WINDOW = "?from=2026-01&to=2026-12"
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def _viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def _row(html: str, label: str) -> str:
    """The grid row naming ``label`` (searched inside the table body, past the filters)."""
    body = html[html.index("<tbody") :]
    start = body.index(label)
    start = body.rfind("<tr", 0, start)
    return body[start : body.index("</tr>", start)]


def _cells(row: str) -> list[tuple[str, str]]:
    """(status class, text) per month cell of a grid row, in order."""
    found = []
    for match in re.finditer(r'<td class="cell(?: cell-([a-z_]+))?"[^>]*>(.*?)</td>', row, re.S):
        status = match.group(1) or ""
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        found.append((status, text))
    return found


# --- the store cell ----------------------------------------------------------------------------


def test_a_store_name_with_markup_is_escaped_in_the_store_cell_and_the_number_leads() -> None:
    store = a_store(f"Istanbul {MARKUP}")
    store.store_number = 1991
    store.save(update_fields=["store_number"])
    a_lease(
        store, an_operator("Bosphorus Co"), started=date(2026, 1, 1), channel=PaymentChannel.WIRE
    )
    html = page(_viewer(), WINDOW)
    assert MARKUP not in html
    row = _row(html, "Bosphorus Co")
    link = re.search(r'<th scope="row" class="store-col"><a [^>]*>([^<]*)</a>', row)
    assert link is not None, row[:300]
    assert link.group(1).startswith("#1991 Istanbul"), link.group(1)
    assert "Bosphorus Co" in row
    assert "wire" not in row.lower() and "Wire" not in row  # the channel left the cell


# --- the overdue boundary -----------------------------------------------------------------------


def test_a_charge_due_today_is_the_soft_shade_and_one_due_yesterday_the_dark_shade() -> None:
    lease = a_lease(
        a_store("2701"),
        an_operator("Boundary Co"),
        started=date(2026, 1, 1),
        channel=PaymentChannel.WIRE,
    )
    a_rent_charge(lease, date(2026, 10, 1), status=RentChargeStatus.DUE, day=TODAY.day)
    a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, day=5)
    cells = _cells(_row(page(_viewer(), WINDOW), "Boundary Co"))
    by_month = {index + 1: cell for index, cell in enumerate(cells)}
    assert by_month[10][0] == "due" and by_month[10][1].startswith("Unpaid"), by_month[10]
    assert by_month[9][0] == "unpaid" and by_month[9][1].startswith("Unpaid"), by_month[9]
    assert "Due $" not in by_month[10][1]


# --- blank cells outside the lease ------------------------------------------------------------


def test_months_outside_the_lease_are_blank_and_future_months_inside_say_not_yet_due() -> None:
    started = a_lease(
        a_store("2702"),
        an_operator("Starter Co"),
        started=date(2026, 6, 1),
        channel=PaymentChannel.WIRE,
    )
    a_rent_charge(started, date(2026, 6, 1), status=RentChargeStatus.PAID, paid="3500.00")
    ended = a_lease(
        a_store("2703"),
        an_operator("Ender Co"),
        started=date(2025, 1, 1),
        channel=PaymentChannel.WIRE,
    )
    ended.ended_on = date(2026, 8, 31)
    ended.save(update_fields=["ended_on"])
    a_rent_charge(ended, date(2026, 8, 1), status=RentChargeStatus.PAID, paid="3500.00")
    html = page(_viewer(), WINDOW)
    start_cells = _cells(_row(html, "Starter Co"))
    assert start_cells[:5] == [("", "")] * 5, start_cells[:5]  # January to May: blank
    assert start_cells[5][0] == "paid"
    assert all(text == "Not yet due" for _, text in start_cells[10:]), start_cells[10:]  # Nov, Dec
    end_cells = _cells(_row(html, "Ender Co"))
    assert end_cells[7][0] == "paid"
    assert end_cells[8:] == [("", "")] * 4, end_cells[8:]  # September to December: blank
    assert "Not yet due" not in [text for _, text in end_cells]


def test_two_leases_on_one_store_leave_no_gap_in_the_hand_over_month() -> None:
    store = a_store("2704")
    first = a_lease(
        store, an_operator("Outgoing Co"), started=date(2025, 1, 1), channel=PaymentChannel.WIRE
    )
    first.ended_on = date(2026, 6, 30)
    first.collection_status = "ended"
    first.save(update_fields=["ended_on", "collection_status"])
    second = a_lease(
        store, an_operator("Incoming Co"), started=date(2026, 7, 1), channel=PaymentChannel.WIRE
    )
    a_rent_charge(first, date(2026, 6, 1), status=RentChargeStatus.PAID, paid="3500.00")
    a_rent_charge(second, date(2026, 7, 1), status=RentChargeStatus.PAID, paid="3500.00")
    html = page(_viewer(), WINDOW)
    cells = _cells(_row(html, "Incoming Co"))  # the row names the current lease's operator
    assert cells[5][0] == "paid" and cells[6][0] == "paid", cells[5:7]
    assert all(text != "Not yet due" for _, text in cells[:7]), cells[:7]


def test_a_cell_with_a_charge_is_never_blank_even_outside_the_lease_dates() -> None:
    lease = a_lease(
        a_store("2705"),
        an_operator("Stray Co"),
        started=date(2026, 3, 1),
        channel=PaymentChannel.WIRE,
    )
    lease.ended_on = date(2026, 5, 31)
    lease.save(update_fields=["ended_on"])
    a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID)
    cells = _cells(_row(page(_viewer(), WINDOW), "Stray Co"))
    assert cells[8][0] == "unpaid" and cells[8][1].startswith("Unpaid")


# --- the legend and the shared tokens -------------------------------------------------------------


def test_the_legend_has_no_due_item_and_two_unpaid_shades() -> None:
    a_lease(
        a_store("2706"),
        an_operator("Legend Co"),
        started=date(2026, 1, 1),
        channel=PaymentChannel.WIRE,
    )
    html = page(_viewer(), WINDOW)
    legend = html[html.index('class="legend') : html.index("</p>", html.index('class="legend'))]
    # Each legend item is a badge span whose first child may be the glyph span.
    items = re.findall(
        r'<span class="badge[^"]*"[^>]*>(.*?)</span>(?=\s*(?:<span class="badge|<span>|$))',
        legend,
        re.S,
    )
    labels = [re.sub(r"<span[^>]*>[^<]*</span>", "", item) for item in items]
    labels = [re.sub(r"<[^>]+>", "", label).strip() for label in labels]
    assert not any(label == "Due" or label.startswith("Due ") for label in labels), labels
    assert sum(1 for label in labels if label.startswith("Unpaid")) == 2, labels


def test_the_badges_and_the_cells_share_their_tokens_and_no_due_token_remains() -> None:
    def read(static_path: str) -> str:
        found = finders.find(static_path)
        assert found, static_path
        return Path(found).read_text(encoding="utf-8")

    tokens = read("finance/tokens.css")
    app = read("finance/app.css")
    grid = (BACKEND / "ledger" / "templates" / "ledger" / "grid" / "page.html").read_text(
        encoding="utf-8"
    )
    for text in (tokens, app, grid):
        assert "--status-due-" not in text and "--status-" not in text  # the 12c names are gone
    for status in ("unpaid", "pending", "partial", "waived", "paid"):
        badge = re.search(rf"\.badge-{status}\s*\{{([^}}]*)\}}", app)
        cell = re.search(rf"\.cell-{status}\s*\{{([^}}]*)\}}", grid)
        assert badge and cell, status
        badge_bg = re.search(r"background:\s*var\((--[a-z-]+)\)", badge.group(1))
        cell_bg = re.search(r"background:\s*var\((--[a-z-]+)\)", cell.group(1))
        assert badge_bg and cell_bg and badge_bg.group(1) == cell_bg.group(1), status
    due_badge = re.search(r"\.badge-due\s*\{([^}]*)\}", app)
    due_cell = re.search(r"\.cell-due\s*\{([^}]*)\}", grid)
    assert due_badge and due_cell
    # PLAN-012f tokens: a charge not yet overdue is the "unpaid" family (light red), the
    # overdue one the "overdue" family; the badge and the cell read the same token.
    badge_bg = re.search(r"background:\s*var\((--[a-z-]+)\)", due_badge.group(1))
    cell_bg = re.search(r"background:\s*var\((--[a-z-]+)\)", due_cell.group(1))
    assert badge_bg and cell_bg and badge_bg.group(1) == cell_bg.group(1) == "--unpaid-bg"
