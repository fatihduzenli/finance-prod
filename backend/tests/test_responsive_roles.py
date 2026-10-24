"""The cells' roles (docs/qa/PLAN-012f-a-design-follow-up.md §3.2 B3, §4; DESIGN-SPEC §6).

In cards mode ``{% responsive_table %}`` gives every body ``<td>`` a ``data-role``: ``title``
for the cell holding a ``cell-title`` (else the row's first cell; the cells before it are
``meta``), ``amount`` for its first ``money`` cell (else the cell under an "Amount", "Balance",
"Total" or "Price" header, in that order), ``status`` for the first cell holding a badge,
``actions`` for a cell holding a button, form or link-button and for every cell of such a
column, ``meta`` for the rest (``meta-badge`` when it holds badges only). A summary table, whose
title would be a money cell, is all ``meta``. A role written in the template is kept; a cell
spanning columns gets none; a table with a field in its body gets none at all; scroll mode
gives none. The phone's CSS draws the roles.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

import pytest

from accounts.roles import Role
from finance.templatetags.responsive import responsive
from ledger.models import PaymentChannel, RentChargeStatus
from tests.receivables_helpers import a_lease, a_payment, a_rent_charge, a_store, an_operator
from tests.test_responsive import client_for, render


def _roles(markup: str) -> list[str | None]:
    """The ``data-role`` of each body ``<td>`` in order (``None`` for a cell without)."""
    body = markup[markup.index("<tbody") :]
    roles: list[str | None] = []
    for cell in re.finditer(r"<td\b([^>]*)>", body):
        role = re.search(r'data-role="([^"]*)"', cell.group(1))
        roles.append(role.group(1) if role else None)
    return roles


def _table(head: str, *rows: str) -> str:
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


# --- The tag on synthetic tables ------------------------------------------------------------


def test_the_first_cell_is_the_title_the_money_cell_the_amount_the_rest_meta() -> None:
    out = responsive(
        _table(
            "<th>Store</th><th>City</th><th class='money'>Rent</th><th>Opened</th>",
            '<tr><td>A</td><td>Austin</td><td class="money">$1.00</td><td>2026</td></tr>',
        )
    )
    assert _roles(out) == ["title", "meta", "amount", "meta"]
    assert '<td class="money" data-role="amount" data-label="Rent">$1.00</td>' in out
    assert out.startswith('<div class="table-cards"><table class="role-cards">')


def test_only_the_first_money_cell_is_the_amount() -> None:
    out = responsive(
        _table(
            "<th>Who</th><th>Billed</th><th>Paid</th>",
            '<tr><td>A</td><td class="money">$2.00</td><td class="cell money">$1.00</td></tr>',
        )
    )
    assert _roles(out) == ["title", "amount", "meta"]


def test_a_summary_table_whose_title_would_be_money_is_all_meta() -> None:
    out = responsive(
        _table(
            "<th>Settled</th><th>Pending</th><th>Note</th>",
            '<tr><td class="money">$1.00</td><td class="money">$2.00</td><td>x</td></tr>',
        )
    )
    assert _roles(out) == ["meta", "meta", "meta"]
    assert '<td class="money" data-role="meta" data-label="Settled">$1.00</td>' in out


def test_one_summary_row_makes_the_whole_table_meta() -> None:
    out = responsive(
        _table(
            "<th>Who</th><th class='money'>Amount</th>",
            '<tr><td>a</td><td class="money">$1</td></tr>',
            '<tr><td class="money">$9</td><td class="money">$1</td></tr>',
        )
    )
    assert _roles(out) == ["meta"] * 4


def test_a_money_first_cell_beside_a_cell_title_is_not_a_summary() -> None:
    out = responsive(
        _table(
            "<th>Billed</th><th>Charge</th><th>Paid</th>",
            '<tr><td class="money">$2</td><td><a class="cell-title" href="/c/">Rent</a></td>'
            '<td class="money">$1</td></tr>',
        )
    )
    assert _roles(out) == ["meta", "title", "amount"]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["Price", "Total", "Balance", "Amount"], ["title", "meta", "meta", "meta", "amount"]),
        (["Price", "Total", "Balance", "Note"], ["title", "meta", "meta", "amount", "meta"]),
        (["Price", "Total", "Note", "Note"], ["title", "meta", "amount", "meta", "meta"]),
        (["Price", "Note", "Note", "Note"], ["title", "amount", "meta", "meta", "meta"]),
        (["amount", "Amounts", "Sum", "Note"], ["title", "meta", "meta", "meta", "meta"]),
    ],
)
def test_without_a_money_cell_the_header_names_the_amount_in_preference_order(
    headers: list[str], expected: list[str]
) -> None:
    head = "<th>Name</th>" + "".join(f"<th>{name}</th>" for name in headers)
    row = "<tr><td>n</td>" + "".join(f"<td>{i}</td>" for i in range(len(headers))) + "</tr>"
    assert _roles(responsive(_table(head, row))) == expected


def test_a_header_named_amount_on_the_first_cell_leaves_it_the_title() -> None:
    out = responsive(_table("<th>Amount</th><th>Note</th>", "<tr><td>$1</td><td>x</td></tr>"))
    assert _roles(out) == ["title", "meta"]


def test_the_first_badge_cell_is_the_status() -> None:
    out = responsive(
        _table(
            "<th>Name</th><th>State</th><th>Note</th>",
            '<tr><td>n <span class="badge">New</span></td>'
            '<td><span class="badge badge-paid">Paid</span></td>'
            '<td><span class="badge note-indicator">Note</span></td></tr>',
        )
    )
    # The title's own badge leaves it the title; a second badge cell is a badge-only meta.
    assert _roles(out) == ["title", "status", "meta-badge"]


def test_a_cell_with_a_button_a_form_or_a_link_button_is_actions() -> None:
    out = responsive(
        _table(
            "<th>Name</th><th>A</th><th>B</th><th>C</th><th>D</th>",
            "<tr><td>n</td>"
            '<td><button type="button">Go</button></td>'
            '<td><form method="post" action="/x/"><input type="hidden" name="csrfmiddlewaretoken"'
            ' value="t"><button class="link-action">Stop</button></form></td>'
            '<td><a class="btn btn-sm" href="/y/">Pay</a></td>'
            '<td><a href="/z/">Just a link</a></td></tr>',
        )
    )
    assert _roles(out) == ["title", "actions", "actions", "actions", "meta"]
    # A hidden input (the form's token) does not make the table a form.
    assert 'class="role-cards"' in out


def test_the_cell_holding_a_cell_title_is_the_title_and_the_cells_before_it_meta() -> None:
    out = responsive(
        _table(
            "<th>Store</th><th>Charge</th><th>Due</th><th class='money'>Amount</th>",
            '<tr><td><a href="/s/">3202</a></td>'
            '<td><a class="cell-title" href="/c/">Rent</a><span class="cell-subline">x</span></td>'
            '<td>2026-09-01</td><td class="money">$1</td></tr>',
        )
    )
    assert _roles(out) == ["meta", "title", "meta", "amount"]


def test_a_cell_title_span_without_a_link_is_the_title_too() -> None:
    out = responsive(
        _table(
            "<th>Store</th><th>Charge</th>",
            '<tr><td>s</td><td><span class="cell-title">Rent</span></td></tr>',
        )
    )
    assert _roles(out) == ["meta", "title"]


def test_a_cell_title_word_inside_another_class_is_not_a_title() -> None:
    out = responsive(
        _table(
            "<th>Store</th><th>Charge</th>",
            '<tr><td>s</td><td><span class="cell-titles">Rent</span></td></tr>',
        )
    )
    assert _roles(out) == ["title", "meta"]


def test_a_column_with_actions_in_any_row_is_actions_in_every_row() -> None:
    out = responsive(
        _table(
            "<th>Name</th><th>Note</th><th><span class='visually-hidden'>Actions</span></th>",
            '<tr><td>a</td><td>x</td><td><a class="btn btn-sm" href="/p/">Pay</a></td></tr>',
            "<tr><td>b</td><td>y</td><td></td></tr>",
            '<tr><td colspan="3">No more.</td></tr>',
        )
    )
    assert _roles(out) == ["title", "meta", "actions", "title", "meta", "actions", None]
    # The empty one is empty still: the phone hides it (td:empty).
    assert '<td data-role="actions" data-label="Actions"></td>' in out


def test_a_column_without_actions_in_any_row_stays_meta() -> None:
    out = responsive(
        _table(
            "<th>Name</th><th>Do</th>",
            "<tr><td>a</td><td></td></tr>",
            "<tr><td>b</td><td></td></tr>",
        )
    )
    assert _roles(out) == ["title", "meta", "title", "meta"]


@pytest.mark.parametrize(
    ("cell", "role"),
    [
        ('<span class="badge note-indicator" title="Note">Note</span>', "meta-badge"),
        (
            '<span class="badge badge-paid"><span class="badge-icon" aria-hidden="true">✓</span>'
            " Paid</span>",
            "meta-badge",
        ),
        (' <span class="badge">A</span> <span class="badge">B</span> ', "meta-badge"),
        ('Held <span class="badge">A</span>', "meta"),
        ('<span class="badge">A</span><br>', "meta"),
        ('<span class="other">A</span>', "meta"),
        ('<a class="badge" href="/x/">A</a>', "meta"),
        ("", "meta"),
    ],
)
def test_a_meta_cell_holding_badges_only_is_a_meta_badge(cell: str, role: str) -> None:
    out = responsive(
        _table(
            "<th>Name</th><th>Status</th><th>Note</th>",
            f'<tr><td>n</td><td><span class="badge">S</span></td><td>{cell}</td></tr>',
        )
    )
    assert _roles(out) == ["title", "status", role]


def test_a_role_written_in_the_template_is_kept_and_not_given_twice() -> None:
    out = responsive(
        _table(
            "<th>Name</th><th>Store</th><th class='money'>Amount</th><th>Paid</th>",
            '<tr><td data-role="meta">n</td><td data-role="title">s</td>'
            '<td class="money">$1</td><td class="money" data-role="meta-if-partial">$0</td></tr>',
        )
    )
    assert _roles(out) == ["meta", "title", "amount", "meta-if-partial"]
    assert out.count("data-role=") == 4
    explicit_amount = responsive(
        _table(
            "<th>Name</th><th>Paid</th><th>Due</th>",
            '<tr><td>n</td><td class="money">$0</td><td data-role="amount">$1</td></tr>',
        )
    )
    assert _roles(explicit_amount) == ["title", "meta", "amount"]


@pytest.mark.parametrize(
    "field",
    [
        '<input type="text" name="q">',
        '<input name="amount">',
        '<input type="checkbox" name="pick">',
        '<select name="s"><option>1</option></select>',
        '<textarea name="t"></textarea>',
        '<INPUT TYPE="number">',
    ],
)
def test_a_table_with_a_field_in_its_body_gets_no_roles_at_all(field: str) -> None:
    out = responsive(
        _table(
            "<th>Line</th><th class='money'>Amount</th><th>Field</th>",
            '<tr><td>a</td><td class="money">$1</td><td>—</td></tr>',
            f'<tr><td>b</td><td class="money">$2</td><td>{field}</td></tr>',
        )
    )
    assert "data-role" not in out
    assert "role-cards" not in out
    # The labels are kept as they are: the label-over-value layout.
    assert out.count('data-label="Line"') == 2


def test_a_hidden_input_alone_is_not_a_field() -> None:
    out = responsive(
        _table(
            "<th>Line</th><th>Do</th>",
            '<tr><td>a</td><td><input type="hidden" name="x"></td></tr>',
        )
    )
    assert _roles(out) == ["title", "meta"]


def test_a_cell_spanning_columns_gets_no_role() -> None:
    out = responsive(
        _table(
            "<th>Name</th><th class='money'>Amount</th><th>Note</th>",
            '<tr><td colspan="3" class="muted empty">No rows.</td></tr>',
            '<tr><td colspan="2">Sum</td><td class="money">$3</td></tr>',
        )
    )
    assert '<td colspan="3" class="muted empty">No rows.</td>' in out
    # The spanning cell is not the row's title; the next cell still gets its role.
    assert _roles(out) == [None, None, "amount"]
    # The table is still a role table: its totals and empty rows keep the cards' layout.
    assert 'class="role-cards"' in out


def test_a_row_header_is_not_the_title() -> None:
    out = responsive(
        _table(
            "<th>Month</th><th>Note</th><th class='money'>Amount</th>",
            '<tr><th scope="row">Oct</th><td>x</td><td class="money">$1</td></tr>',
        )
    )
    assert _roles(out) == ["meta", "amount"]


def test_the_footer_and_the_head_get_no_roles() -> None:
    out = responsive(
        "<table><thead><tr><th>Name</th><th class='money'>Amount</th></tr></thead>"
        '<tbody><tr><td>a</td><td class="money">$1</td></tr></tbody>'
        '<tfoot><tr class="totals"><th>Total</th><td class="money">$1</td></tr></tfoot></table>'
    )
    foot = out[out.index("<tfoot>") :]
    assert "data-role" not in foot
    assert "data-role" not in out[: out.index("<tbody>")]
    assert '<td class="money" data-label="Amount">$1</td></tr></tfoot>' in out


def test_the_role_class_joins_the_tables_own_classes() -> None:
    out = responsive(
        '<table id="t" class="table attention-table"><thead><tr><th>Name</th></tr></thead>'
        "<tbody><tr><td>a</td></tr></tbody></table>"
    )
    assert '<table id="t" class="table attention-table role-cards">' in out
    assert out.count("role-cards") == 1


def test_scroll_mode_gives_no_roles() -> None:
    out = render(
        '{% responsive_table "scroll" %}<table id="r"><thead><tr><th>Month</th>'
        '<th class="money">Amount</th></tr></thead><tbody><tr><td>Oct</td>'
        '<td class="money">$1</td></tr></tbody></table>{% endresponsive_table %}'
    )
    assert "data-role" not in out
    assert "role-cards" not in out
    assert '<td data-label="Month">Oct</td>' in out


def test_a_table_without_a_head_gets_no_roles() -> None:
    table = "<table><tbody><tr><td>1</td></tr></tbody></table>"
    assert responsive(table) == table


def test_the_roles_come_from_the_markup_never_from_a_cell_value() -> None:
    hostile = '<span class="badge">x</span><button>y</button> data-role="title"'
    out = render(
        "{% responsive_table %}<table><thead><tr><th>Name</th><th>Note</th><th>Other</th></tr>"
        "</thead><tbody><tr><td>n</td><td>{{ v }}</td><td>{{ v }}</td></tr></tbody></table>"
        "{% endresponsive_table %}",
        v=hostile,
    )
    # The escaped value holds no badge, button or role: both cells are meta.
    assert _roles(out) == ["title", "meta", "meta"]
    assert "<button>" not in out


def test_the_tag_keeps_its_arguments() -> None:
    out = render(
        '{% responsive_table "cards" %}<table><thead><tr><th>A</th></tr></thead>'
        "<tbody><tr><td>a</td></tr></tbody></table>{% endresponsive_table %}"
    )
    assert '<td data-role="title" data-label="A">a</td>' in out


# --- The pages ---------------------------------------------------------------------------------


def _section(page: str, opening: str) -> str:
    start = page.index(opening)
    return page[start : page.index("</table>", start)]


@pytest.mark.django_db
def test_the_stores_list_carries_roles() -> None:
    store = a_store(f"4101 ROLES {uuid.uuid4().hex[:4]}", 4101)
    page = client_for(Role.VIEWER).get("/stores/").content.decode()
    table = _section(page, '<table class="role-cards">')
    row = next(
        r
        for r in re.findall(r"<tr>.*?</tr>", table[table.index("<tbody>") :], re.S)
        if store.external_label in r
    )
    # Name, Store number, City, State, Open date, Status (a plain text status: meta).
    assert _roles(f"<tbody>{row}") == ["title", "meta", "meta", "meta", "meta", "meta"]


@pytest.mark.django_db
def test_the_payments_list_carries_roles() -> None:
    lease = a_lease(a_store("4102 ROLES", 4102), an_operator("Roles Co"))
    charge = a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.PAID, "100.00", "100.00")
    a_payment(charge, "100.00", date(2026, 9, 2))
    page = client_for(Role.VIEWER).get("/payments/").content.decode()
    table = _section(page, '<table id="payments" class="role-cards">')
    roles = _roles(table)
    # Paid on, Initiated, Operator, Store, Charge, Method, Amount, Status, Reference,
    # Transaction, Note.
    assert roles == ["title", *["meta"] * 5, "amount", "status", "meta", "meta", "meta"]
    # The totals table above it: five money cells, a summary: each "Label value".
    totals = _section(page, '<table id="payment-totals" class="role-cards">')
    assert _roles(totals) == ["meta"] * 5


@pytest.mark.django_db
def test_the_charge_pages_payments_table_carries_roles() -> None:
    lease = a_lease(
        a_store("4103 ROLES", 4103), an_operator("Roles Three"), channel=PaymentChannel.WIRE
    )
    charge = a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.PARTIAL, "100.00", "40.00")
    a_payment(charge, "40.00", date(2026, 9, 2))
    page = client_for(Role.VIEWER).get(f"/charges/{charge.pk}/").content.decode()
    table = _section(page, '<table id="payments" class="role-cards">')
    # Received, Method, Amount, Status, Reference, Note, Recorded by.
    assert _roles(table) == ["title", "meta", "amount", "status", "meta", "meta", "meta"]
