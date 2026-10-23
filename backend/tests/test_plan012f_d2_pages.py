"""PLAN-012f D-2 (docs/qa/PLAN-012f-approved-design.md D-2; DESIGN-SPEC §6): the markup the
phone's charge cards hang on.

The Charges card's table is ``charges-table``; each charge row's cells carry the fixed classes
in order (the operator page with ``charge-cell-store`` first) beside their existing ones; the row is
``partial`` exactly when it has a payment and a balance; the due cell holds the ISO date only;
the totals and the empty rows keep their markup; the phone's rules live in the 719.98 px block
only, so the laptop is unchanged.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import Lease, PaymentChannel, RentChargeStatus
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_responsive import APP_CSS

pytestmark = pytest.mark.django_db

STORE_CELLS = [
    "charge-cell-title",
    "charge-cell-due charge-due",
    "charge-cell-status",
    "charge-cell-amount money",
    "charge-cell-paid money",
    "charge-cell-balance money",
    "charge-cell-actions",
]


def _lease(label: str) -> Lease:
    return a_lease(
        a_store(label),
        an_operator(f"{label} Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 1),
    )


def _charges(client: Client, name: str, pk: object, **query: str) -> str:
    html = client.get(
        reverse(name, kwargs={"pk": pk}), {"tab": "charges", **query}
    ).content.decode()
    start = html.index('<table id="store-charges" class="charges-table">')
    return html[start : html.index("</table>", start)]


def _charge_rows(table: str) -> list[str]:
    return re.findall(r'<tr class="store-charge[^"]*">.*?</tr>', table, re.S)


def _row_classes(row: str) -> list[str]:
    opening = re.match(r'<tr class="([^"]*)">', row)
    assert opening is not None, row[:80]
    return opening.group(1).split()


def _cell_classes(row: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r'<td class="([^"]*)"', row)]


def test_the_cells_carry_the_fixed_classes_in_order_on_the_store_page() -> None:
    lease = _lease("3201 MARKUP")
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, due="2000.00")
    a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.PARTIAL, "2000.00", "500.00")
    billing, _ = client_for(Role.BILLING)
    table = _charges(billing, "ledger:stores:detail", lease.store_id, filter="all")
    rows = _charge_rows(table)
    assert len(rows) == 2
    for row in rows:
        assert _cell_classes(row) == STORE_CELLS, _cell_classes(row)
        assert "charge-cell-store" not in row


def test_the_operator_page_puts_the_store_cell_first() -> None:
    lease = _lease("3202 MARKUP")
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, due="2000.00")
    billing, _ = client_for(Role.BILLING)
    table = _charges(billing, "ledger:operators:detail", lease.operator_id, filter="all")
    rows = _charge_rows(table)
    assert len(rows) == 1
    assert _cell_classes(rows[0]) == ["charge-cell-store", *STORE_CELLS], _cell_classes(rows[0])
    assert ">3202 MARKUP</a>" in rows[0]


@pytest.mark.parametrize(
    ("status", "paid", "partial"),
    [
        (RentChargeStatus.UNPAID, "0.00", False),
        (RentChargeStatus.PARTIAL, "500.00", True),
        (RentChargeStatus.PARTIAL, "1999.99", True),
        (RentChargeStatus.PAID, "2000.00", False),
        (RentChargeStatus.WAIVED, "0.00", False),
    ],
)
def test_partial_marks_exactly_a_row_with_a_payment_and_a_balance(
    status: str, paid: str, partial: bool
) -> None:
    lease = _lease(f"3203 {status} {paid}")
    a_rent_charge(lease, date(2026, 9, 1), status, "2000.00", paid)
    billing, _ = client_for(Role.BILLING)
    table = _charges(billing, "ledger:stores:detail", lease.store_id, filter="all")
    rows = _charge_rows(table)
    assert len(rows) == 1
    assert ("partial" in _row_classes(rows[0])) is partial, rows[0][:120]


def test_the_due_cell_holds_the_iso_date_only() -> None:
    lease = _lease("3204 DUE")
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, due="2000.00")
    billing, _ = client_for(Role.BILLING)
    table = _charges(billing, "ledger:stores:detail", lease.store_id)
    due = re.findall(r'<td class="charge-cell-due charge-due"[^>]*>(.*?)</td>', table, re.S)
    assert len(due) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", due[0]), due
    # The "Due" word is drawn by the phone's CSS: the row has no copy of it.
    assert "Due " not in _charge_rows(table)[0]


def test_the_totals_and_the_empty_rows_keep_their_markup() -> None:
    lease = _lease("3205 ROWS")
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID, due="2000.00")
    billing, _ = client_for(Role.BILLING)
    table = _charges(billing, "ledger:stores:detail", lease.store_id)
    assert (
        '<tr class="totals"><th scope="row" colspan="5">Open balance</th>'
        '<td class="money balance-open" data-label="Balance">'
        '<span id="charges-balance">$2,000.00</span></td><td data-label="Actions"></td></tr>'
    ) in table
    empty = _charges(billing, "ledger:stores:detail", lease.store_id, filter="paid")
    assert '<tr><td colspan="7" class="muted empty">No paid charges.' in empty
    assert _charge_rows(empty) == []
    operator_empty = _charges(billing, "ledger:operators:detail", lease.operator_id, filter="paid")
    assert '<tr><td colspan="8" class="muted empty">No paid charges.' in operator_empty
    assert 'colspan="6">Balance of the charges shown</th>' in operator_empty


def test_the_charge_card_rules_live_in_the_phone_block_only() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.index("@media (max-width: 719.98px) {")
    end = css.index("\n}\n", phone)
    rules = [m.start() for m in re.finditer(r"table\.charges-table", css)]
    assert rules, "no charges-table rule"
    assert all(phone < at < end for at in rules), rules
