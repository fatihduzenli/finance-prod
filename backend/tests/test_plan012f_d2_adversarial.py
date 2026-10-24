"""Adversarial cases for the phone's two-line table card (PLAN-012f D-2, generalised by
PLAN-012f-a B3), by the QA owner. The Charges card's table carries the roles the phone layout
hangs on, assigned by ``{% responsive_table %}`` (title, meta, status, amount, meta-if-partial,
actions), in the same order on the store and the operator page (the operator page's Store cell a
meta cell first); the ``partial`` row class marks exactly the rows with both a payment and a
balance; no text is duplicated for the phone; the totals and the empty rows keep their markup;
the old hand-written classes are gone and the role rules live only in the phone media block."""

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

ROLES = ("title", "meta", "status", "amount", "meta-if-partial", "meta-if-partial", "actions")
STATIC = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance"
TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "ledger"
    / "templates"
    / "ledger"
    / "stores"
    / "_tab_charges.html"
)


def _rows(html: str) -> list[str]:
    table = html[html.index('id="store-charges"') :]
    table = table[: table.index("</table>")]
    body = table[table.index("<tbody") :]
    return [
        match.group(0)
        for match in re.finditer(r"<tr[^>]*>.*?</tr>", body, re.S)
        if "totals" not in match.group(0)[:40] and 'class="muted empty"' not in match.group(0)
    ]


def _roles(row: str) -> list[str]:
    found = []
    for td in re.findall(r"<td[^>]*>", row):
        role = re.search(r'data-role="([^"]*)"', td)
        found.append(role.group(1) if role else "")
    return found


def _lease_with_three_charges(number: str, name: str):  # type: ignore[no-untyped-def]
    lease = a_lease(
        a_store(number), an_operator(name), channel=PaymentChannel.WIRE, started=date(2026, 1, 1)
    )
    a_rent_charge(lease, date(2026, 9, 1), status=RentChargeStatus.UNPAID, due="2000.00")
    a_rent_charge(
        lease, date(2026, 8, 1), status=RentChargeStatus.PARTIAL, due="2000.00", paid="500.00"
    )
    a_rent_charge(
        lease, date(2026, 7, 1), status=RentChargeStatus.PAID, due="2000.00", paid="2000.00"
    )
    return lease


def test_the_charge_rows_carry_the_roles_in_order_on_both_workspaces() -> None:
    lease = _lease_with_three_charges("3101", "Cards Co")
    billing, _ = client_for(Role.BILLING)
    store_page = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        {"tab": "charges", "filter": "all"},
    ).content.decode()
    rows = _rows(store_page)
    assert len(rows) == 3, len(rows)
    for row in rows:
        assert tuple(_roles(row)) == ROLES, _roles(row)
    operator_page = billing.get(
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}),
        {"tab": "charges", "filter": "all"},
    ).content.decode()
    for row in _rows(operator_page):
        roles = _roles(row)
        assert roles[0] == "meta" and tuple(roles[1:]) == ROLES, roles


def test_the_partial_class_marks_exactly_the_rows_with_a_payment_and_a_balance() -> None:
    lease = _lease_with_three_charges("3102", "Partial Co")
    a_rent_charge(lease, date(2026, 6, 1), status=RentChargeStatus.WAIVED, due="2000.00")
    billing, _ = client_for(Role.BILLING)
    html = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        {"tab": "charges", "filter": "all"},
    ).content.decode()
    rows = _rows(html)
    assert len(rows) == 4

    def row_classes(row: str) -> list[str]:
        opening = re.match(r"<tr([^>]*)>", row)
        assert opening is not None
        found = re.search(r'class="([^"]*)"', opening.group(1))
        return found.group(1).split() if found else []

    partial_rows = [row for row in rows if "partial" in row_classes(row)]
    assert len(partial_rows) == 1, [row_classes(row) for row in rows]
    assert "$500.00" in partial_rows[0] and "$1,500.00" in partial_rows[0]


def test_no_text_is_duplicated_and_the_totals_and_empty_rows_keep_their_markup() -> None:
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
    assert row.count('class="cell-title"') == 1, row
    assert row.count("cell-subline") <= 1, row
    assert "phone-only" not in row and "laptop-only" not in row
    assert "charge-cell-" not in html and "charges-table" not in html
    assert 'class="totals"' in html
    empty = billing.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        {"tab": "charges", "filter": "paid"},
    ).content.decode()
    assert 'class="muted empty"' in empty and "No paid charges." in empty


def test_the_role_rules_live_only_in_the_phone_block_and_the_old_classes_are_gone() -> None:
    """The first D-2 build's class names collided with the grid's cell-block rules and broke
    the laptop table; the general rule hangs on data-role selectors inside app.css's phone
    media block only, and the hand-written classes exist nowhere."""
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "charge-cell-" not in template and "charges-table" not in template
    assert template.count('data-role="meta-if-partial"') == 2
    for sheet in sorted(STATIC.glob("*.css")):
        text = sheet.read_text(encoding="utf-8")
        assert "charge-cell-" not in text, sheet.name
        if sheet.name != "app.css":
            assert "data-role" not in text, sheet.name
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
        assert "data-role" not in outside, "a data-role rule outside the phone block"
        assert text[start:end].count("data-role") >= 6
