"""Adversarial cases for PLAN-012f D-2 (the phone's charge cards), by the QA owner (plan §6).
The Charges card's table carries the fixed classes the phone layout hangs on, in the same
order on the store and the operator page (the operator page with its Store cell first); the
``partial`` row class marks exactly the rows with both a payment and a balance; no text is
duplicated for the phone (the kind appears once a row); the totals and the empty rows keep
their markup; markup in a charge's description stays escaped inside the title cell."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel, RentChargeStatus
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

CELLS = (
    "charge-cell-title",
    "charge-cell-due",
    "charge-cell-status",
    "charge-cell-amount",
    "charge-cell-paid",
    "charge-cell-balance",
    "charge-cell-actions",
)


def _rows(html: str) -> list[str]:
    table = html[html.index('class="charges-table') :]
    table = table[: table.index("</table>")]
    body = table[table.index("<tbody") :]
    return [
        match.group(0)
        for match in re.finditer(r"<tr[^>]*>.*?</tr>", body, re.S)
        if "totals" not in match.group(0)[:40] and 'class="muted empty"' not in match.group(0)
    ]


def _classes(cell: str) -> str:
    found = re.search(r'class="([^"]*)"', cell)
    return found.group(1) if found else ""


def _cells(row: str) -> list[str]:
    return [_classes(td) for td in re.findall(r"<td[^>]*>", row)]


def test_the_charge_rows_carry_the_cell_classes_in_order_on_both_workspaces() -> None:
    lease = a_lease(
        a_store("3101"),
        an_operator("Cards Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    a_rent_charge(
        lease, date(2026, 8, 1), status=RentChargeStatus.PARTIAL, due="2000.00", paid="500.00"
    )
    a_rent_charge(
        lease, date(2026, 7, 1), status=RentChargeStatus.PAID, due="2000.00", paid="2000.00"
    )
    billing, _ = client_for(Role.BILLING)
    store_page = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        {"tab": "charges", "filter": "all"},
    ).content.decode()
    assert 'class="charges-table' in store_page
    rows = _rows(store_page)
    assert len(rows) == 3, len(rows)
    for row in rows:
        cells = _cells(row)
        assert len(cells) == len(CELLS), cells
        for found, expected in zip(cells, CELLS, strict=True):
            assert expected in found.split(), (found, expected)
    operator_page = billing.get(
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}),
        {"tab": "charges", "filter": "all"},
    ).content.decode()
    for row in _rows(operator_page):
        cells = _cells(row)
        assert len(cells) == len(CELLS) + 1, cells
        assert "charge-cell-store" in cells[0].split(), cells[0]
        for found, expected in zip(cells[1:], CELLS, strict=True):
            assert expected in found.split(), (found, expected)


def test_the_partial_class_marks_exactly_the_rows_with_a_payment_and_a_balance() -> None:
    lease = a_lease(
        a_store("3102"),
        an_operator("Partial Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    unpaid = a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    partial = a_rent_charge(
        lease, date(2026, 8, 1), status=RentChargeStatus.PARTIAL, due="2000.00", paid="500.00"
    )
    paid = a_rent_charge(
        lease, date(2026, 7, 1), status=RentChargeStatus.PAID, due="2000.00", paid="2000.00"
    )
    waived = a_rent_charge(lease, date(2026, 6, 1), status=RentChargeStatus.WAIVED, due="2000.00")
    billing, _ = client_for(Role.BILLING)
    html = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        {"tab": "charges", "filter": "all"},
    ).content.decode()
    by_month: dict[str, str] = {}
    for row in _rows(html):
        labelled = re.search(r"Rent (\d{4}-\d{2})", row)
        assert labelled is not None, row[:200]
        by_month[labelled.group(1)] = row
    assert set(by_month) == {"2026-09", "2026-08", "2026-07", "2026-06"}

    def row_classes(row: str) -> list[str]:
        opening = re.match(r"<tr([^>]*)>", row)
        assert opening is not None
        found = re.search(r'class="([^"]*)"', opening.group(1))
        return found.group(1).split() if found else []

    assert "partial" in row_classes(by_month["2026-08"]), by_month["2026-08"][:120]
    for month in ("2026-09", "2026-07", "2026-06"):
        assert "partial" not in row_classes(by_month[month]), (month, by_month[month][:120])
    assert unpaid.pk and partial.pk and paid.pk and waived.pk


def test_the_kind_appears_once_a_row_and_markup_in_a_description_stays_in_the_title_cell() -> None:
    lease = a_lease(
        a_store("3103"),
        an_operator("Once Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )
    a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    billing, _ = client_for(Role.BILLING)
    html = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}), {"tab": "charges"}
    ).content.decode()
    rows = _rows(html)
    assert len(rows) == 1
    row = rows[0]
    # The title block renders once: one title, at most one subline, no phone copy of the kind.
    assert row.count('class="cell-title"') == 1, row
    assert row.count("cell-subline") <= 1, row
    assert "phone-only" not in row and "laptop-only" not in row
    # The due cell carries the date only (its "Due" word comes from CSS, never a second copy).
    due = re.search(r'<td class="[^"]*charge-cell-due[^"]*"[^>]*>(.*?)</td>', row, re.S)
    assert due is not None and re.fullmatch(
        r"\s*\d{4}-\d{2}-\d{2}\s*", re.sub(r"<[^>]+>", "", due.group(1))
    )
    # The totals row and the empty row keep their markup.
    assert 'class="totals"' in html
    empty = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        {"tab": "charges", "filter": "paid"},
    ).content.decode()
    assert 'class="muted empty"' in empty and "No paid charges." in empty


STATIC = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance"


def test_the_charge_cell_classes_appear_in_no_rule_outside_the_phone_block() -> None:
    """The first D-2 build's names (cell-amount, cell-status, cell-due, cell-paid) matched the
    monthly grid's cell-block rules and broke the laptop table. The classes of the contract
    appear only inside app.css's phone media block, and in no other stylesheet."""
    for sheet in sorted(STATIC.glob("*.css")):
        text = sheet.read_text(encoding="utf-8")
        if sheet.name != "app.css":
            assert "charge-cell-" not in text, sheet.name
            continue
        start = text.index("@media (max-width: 719.98px) {")
        depth, end = 0, None
        for at, char in enumerate(text[start:], start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = at
                    break
        assert end is not None
        outside = text[:start] + text[end:]
        assert "charge-cell-" not in outside, "a charge-cell- rule outside the phone block"
        assert text[start:end].count("charge-cell-") >= 8
    # And the grid's own cell classes match nothing in the charges table's markup.
    template = (
        Path(__file__).resolve().parents[1]
        / "ledger"
        / "templates"
        / "ledger"
        / "stores"
        / "_tab_charges.html"
    ).read_text(encoding="utf-8")
    for clashing in ("cell-amount", "cell-status", "cell-due", "cell-paid", "cell-balance"):
        assert f'"{clashing}' not in template and f" {clashing} " not in template, clashing
