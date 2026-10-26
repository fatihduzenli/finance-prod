"""The profit and loss page in the report family's frame (PLAN-012f-b Batch E, slice E-1;
DESIGN-SPEC §4 Profit and loss, profit-and-loss-laptop.png).

The header (breadcrumb, Print, Download spreadsheet), the filter card (no State), the four
figures equal to the totals, the table card (title, count line, held pills, two-row header,
amounts without "$" and with a true minus, "unknown", the Flags pills, the total, the footer
link), the empty landlord rent of a store created without one (the page and the CSV's "known"
columns), and the phone's two-line cards.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from accounts.roles import Role
from finance.templatetags.money import money
from ledger.models import Store
from tests.test_operator_views import client_for
from tests.test_pnl import (
    AUGUST,
    EXPORT,
    JULY,
    PAGE,
    a_cost,
    a_lease,
    a_store,
    an_operator,
    csv_rows,
    get,
    known_store,
    paid,
    rent,
)
from tests.test_store_views import form_data

pytestmark = pytest.mark.django_db

D = Decimal
JULY_ONLY = {"from": "2026-07", "to": "2026-07"}
NEW_STORE = "/stores/new/"


def page_of(params: dict[str, str] | None = None) -> str:
    response = get(PAGE, params or JULY_ONLY)
    assert response.status_code == 200
    return str(response.content.decode())


def between(page: str, start: str, end: str) -> str:
    """The text of ``page`` after the first ``start``, up to the next ``end``."""
    assert start in page, start
    return page.split(start, 1)[1].split(end, 1)[0]


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


# --- The header and the filter card -----------------------------------------------------------


def test_the_header_has_the_breadcrumb_print_and_the_spreadsheet() -> None:
    page = page_of()
    header = between(page, '<div class="page-header">', 'id="pnl-filters"')
    assert '<nav class="breadcrumb" aria-label="Breadcrumb" id="breadcrumb">' in header
    assert '<a href="/reports/">Reports</a>' in header
    assert (
        '<span aria-current="page">Profit and loss</span></nav><h1>Profit and loss</h1>' in header
    )
    assert '<button class="btn" type="button" id="pnl-print" onclick="window.print()">' in header
    assert re.search(
        r'<a class="btn" id="export-csv" href="/reports/pnl/export\.csv\?[^"]*">'
        r"Download spreadsheet</a>",
        header,
    )
    assert "btn-primary" not in header


def test_the_filter_card_has_the_months_the_grouping_the_rows_box_and_a_navy_show() -> None:
    form = between(
        page_of(), '<form method="get" action="/reports/pnl/" id="pnl-filters"', "</form>"
    )
    for name in ("from", "to", "group"):
        assert f'<select name="{name}" id="{name}">' in form, name
    assert re.findall(r'<option value="([a-z]+)"', between(form, 'id="group"', "</select>")) == [
        "store",
        "operator",
        "month",
    ]
    assert 'name="state"' not in form and ">State<" not in form
    assert 'type="checkbox" name="detail" value="1" id="detail"' in form
    assert '<button class="btn btn-dark pnl-show" type="submit">Show</button>' in form
    # The old row of links is gone: the spreadsheet is in the header, the rows are the box.
    assert 'id="toggle-detail"' not in page_of()


# --- The four figures -------------------------------------------------------------------------


def kpi(page: str, element_id: str) -> str:
    return between(page, f'<div class="kpi" id="{element_id}">', "</div>")


def test_the_four_figures_equal_the_totals() -> None:
    _, _, charge, _ = known_store()
    paid(charge, "2000.00", date(2026, 7, 5))
    response = get(PAGE, JULY_ONLY)
    total, kpis = response.context["total"], response.context["kpis"]
    assert kpis["billed"] == total.rent_billed + total.fees_billed == D("4500.00")
    assert kpis["collected"] == total.rent_collected + total.fees_collected == D("2000.00")
    assert kpis["unknown_stores"] == 0
    page = response.content.decode()
    billed = text_of(kpi(page, "kpi-billed"))
    assert billed == "Billed $4,500.00 rent $3,500.00 · fees $1,000.00"
    assert '<span class="kpi-value tone-paid">$2,000.00</span>' in kpi(page, "kpi-collected")
    costs = text_of(kpi(page, "kpi-costs"))
    assert costs.startswith(f"Costs {format_dollars(total.costs)} landlord $2,500.00")
    assert costs.endswith("fee costs $500.00")
    margin = kpi(page, "kpi-margin")
    assert total.margin_collected == D("2000.00") - total.costs
    assert format_dollars(total.margin_collected) in margin
    assert 'id="kpi-unknown"' not in margin


def format_dollars(amount: Decimal | None) -> str:
    assert amount is not None
    return money(amount)


def test_a_negative_margin_is_red_and_a_positive_one_green() -> None:
    _, _, charge, sign = known_store()
    assert '<span class="kpi-value tone-unpaid">-$' in kpi(page_of(), "kpi-margin")
    paid(charge, "3500.00", date(2026, 7, 5))
    paid(sign, "1000.00", date(2026, 7, 12))
    assert '<span class="kpi-value tone-paid">$' in kpi(page_of(), "kpi-margin")


def test_the_collected_card_names_a_withheld_deposit_only_when_there_is_one() -> None:
    known_store()
    assert "withheld deposit" not in kpi(page_of(), "kpi-collected")


def test_an_unknown_cost_makes_costs_and_margin_unknown_and_counts_the_stores() -> None:
    known_store()
    rent(a_lease(a_store("Store 2476"), an_operator("Transfer Test")), JULY)
    rent(a_lease(a_store("Store 2477"), an_operator("Halid")), JULY)
    response = get(PAGE, JULY_ONLY)
    assert response.context["kpis"]["unknown_stores"] == 2
    page = response.content.decode()
    assert '<span class="kpi-value"><span class="unknown">unknown</span></span>' in kpi(
        page, "kpi-costs"
    )
    margin = kpi(page, "kpi-margin")
    assert '<span class="kpi-value"><span class="unknown">unknown</span></span>' in margin
    assert (
        '<span class="kpi-note" id="kpi-unknown">2 stores unknown · '
        '<a href="#pnl-explained">why</a></span>'
    ) in margin
    assert 'id="pnl-explained"' in page
    assert "never counted as zero" in between(page, 'id="pnl-explained">', "</p>")


# --- The table card ---------------------------------------------------------------------------


def test_the_card_header_has_the_title_the_count_line_and_the_held_pills() -> None:
    known_store()
    page = page_of()
    assert '<h2 id="pnl-title">Jul 2026 · by store</h2>' in page
    count = text_of(between(page, '<span class="list-counts" id="pnl-count">', "</span>"))
    assert count == "1 store · 1 row · amounts in US dollars"
    held = between(page, '<span class="pnl-held">', "</div>")
    assert '<span class="badge" id="deposits-held">Deposits held at Jul 31, 2026: ' in held
    assert '<span id="deposits-held-amount">$0.00</span>' in held
    assert '<span id="credits-held-amount">$0.00</span>' in held
    ranged = page_of({"from": "2026-07", "to": "2026-08", "group": "operator"})
    assert '<h2 id="pnl-title">Jul 2026 to Aug 2026 · by operator</h2>' in ranged


def test_the_header_has_two_rows_with_the_column_groups() -> None:
    known_store()
    head = between(page_of(), '<table id="pnl"', "</thead>")
    first, second = re.findall(r"<tr>(.*?)</tr>", head, re.S)
    assert re.findall(r"<th[^>]*>([^<]*)</th>", first) == [
        "Store",
        "Rent",
        "Fees",
        "Costs",
        "Margin",
        "Flags",
    ]
    assert '<th colspan="3" scope="colgroup" class="grp-head grp-start">Costs</th>' in first
    assert re.findall(r"<th[^>]*>([^<]*)</th>", second) == [
        "Billed",
        "Collected",
        "Billed",
        "Collected",
        "Landlord",
        "Other",
        "Fee costs",
        "Billed",
        "Collected",
    ]


def group_row(page: str) -> str:
    return between(page, '<tr class="pnl-group">', "</tr>")


def test_a_row_shows_the_store_bold_with_its_operator_and_amounts_without_the_dollar() -> None:
    known_store()
    row = group_row(page_of())
    assert (
        '<span class="cell-title">Store 1796</span><span class="cell-subline">'
        "Northwind Stores</span>"
    ) in row
    cells = re.findall(r'<td class="money[^"]*"[^>]*>(.*?)</td>', row, re.S)
    assert [text_of(cell) for cell in cells] == [
        "3,500.00",
        "0.00",
        "1,000.00",
        "0.00",
        "2,500.00",
        text_of(cells[5]),
        "500.00",
        text_of(cells[7]),
        text_of(cells[8]),
    ]
    assert all("$" not in cell for cell in cells)
    assert '<td class="money grp-start margin-billed" data-label="Margin billed"' in row
    assert '<td class="money margin-collected" data-label="Margin collected"' in row


def test_a_negative_amount_has_a_true_minus_in_the_unpaid_colour() -> None:
    known_store()
    row = group_row(page_of())
    collected = between(row, 'class="money margin-collected"', "</td>")
    assert re.search(r'<span class="negative">−[0-9,]+\.[0-9]{2}</span>', collected)
    assert "-" not in text_of(collected.split(">", 1)[1])


def test_the_flags_column_shows_the_unknown_and_vacant_pills() -> None:
    store = a_store("Store 1999")
    a_cost(store)
    rent(a_lease(store, ended_on=date(2026, 7, 31)), JULY)
    rent(a_lease(a_store("Store 2476"), an_operator("Transfer Test")), AUGUST)
    page = page_of({"from": "2026-08", "to": "2026-08"})
    rows = re.findall(r'<tr class="pnl-group">(.*?)</tr>', page, re.S)
    assert len(rows) == 2
    vacant = next(row for row in rows if "Store 1999" in row)
    unknown = next(row for row in rows if "Store 2476" in row)
    assert '<span class="cell-subline">—</span>' in vacant
    assert '<span class="badge cost-only-flag">Vacant</span>' in vacant
    assert "Unknown:" not in vacant
    assert '<span class="badge unknown-flag">Unknown: landlord rent, other costs</span>' in unknown
    total = between(page, '<tr class="totals" id="pnl-total">', "</tr>")
    assert '<span class="badge">Unknown: 1 store</span>' in total
    assert text_of(between(total, 'data-label="Landlord rent"', "</td>")).endswith("unknown")


def test_a_store_part_vacant_in_the_range_keeps_its_cost_only_count() -> None:
    store = a_store()
    a_cost(store)
    rent(a_lease(store, ended_on=date(2026, 7, 31)), JULY)
    row = group_row(page_of({"from": "2026-07", "to": "2026-08"}))
    assert '<span class="badge cost-only-flag">1 cost-only</span>' in row


def test_the_total_row_is_bold_on_the_second_surface_and_the_footer_links_the_help() -> None:
    known_store()
    page = page_of()
    assert '<tr class="totals" id="pnl-total">' in between(page, "<tfoot>", "</tfoot>")
    foot = between(page, '<p class="muted pnl-foot" id="pnl-foot">', "</p>")
    assert "Deposits are money held for operators, not revenue" in foot
    assert '<a href="/help/store-costs/" id="pnl-help">How this report is built</a>' in foot
    assert get("/help/store-costs/").status_code == 200


def test_the_labels_are_escaped() -> None:
    store = a_store("<script>alert(1)</script>")
    rent(a_lease(store, an_operator("<b>Bold</b>")), JULY)
    page = page_of()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;b&gt;Bold&lt;/b&gt;" in page


# --- The empty landlord rent (Batch B made it optional) ---------------------------------------


def test_a_store_created_without_a_landlord_rent_is_unknown_on_the_page_and_in_the_csv() -> None:
    admin, _ = client_for(Role.ADMIN)
    created = admin.post(NEW_STORE, form_data(landlord_rent="", open_date="2026-01-01"))
    assert created.status_code == 302
    store = Store.objects.get()
    rent(a_lease(store, an_operator("Acme")), JULY, "1200.00")
    response = admin.get(PAGE, JULY_ONLY)
    [group] = response.context["groups"]
    assert group.landlord_rent is None and group.margin_billed is None
    assert response.context["kpis"]["unknown_stores"] == 1
    row = group_row(response.content.decode())
    for label in ("Landlord rent", "Margin billed", "Margin collected"):
        cell = between(row, f'data-label="{label}"', "</td>")
        assert cell.endswith('<span class="unknown">unknown</span>'), label
    assert "Unknown: landlord rent" in row
    assert "1,200.00" in between(row, 'data-label="Rent billed"', "</td>")
    for level, detail in (("store", ""), ("detail", "1")):
        records = csv_rows(admin.get(EXPORT, {**JULY_ONLY, "detail": detail}))
        record = next(item for item in records if item["Level"] == level)
        assert (record["Landlord rent (USD)"], record["Landlord rent known"]) == ("", "no")
        assert (record["Margin billed (USD)"], record["Margin billed known"]) == ("", "no")
        assert (record["Margin collected (USD)"], record["Margin collected known"]) == ("", "no")
        assert record["Unknown inputs"].startswith("landlord rent")
        assert record["Rent billed (USD)"] == "1200.00"


def test_a_landlord_rent_of_zero_is_known_and_not_flagged() -> None:
    store = a_store()
    a_cost(store, landlord="0.00", other_monthly_cost=D("0.00"))
    rent(a_lease(store), JULY, "1200.00")
    row = group_row(page_of())
    assert "Unknown:" not in row and '<span class="unknown">' not in row
    assert text_of(between(row, 'data-label="Margin billed"', "</td>")).endswith("1,200.00")


# --- The phone --------------------------------------------------------------------------------


def test_the_phone_draws_the_rows_as_two_line_cards() -> None:
    known_store()
    page = page_of({**JULY_ONLY, "detail": "1"})
    assert re.search(
        r'<div class="table-cards">\s*<table id="pnl" class="pnl-table role-cards">', page
    )
    assert '<div class="table-scroll"' not in page
    for row in (group_row(page), between(page, '<tr class="pnl-row">', "</tr>")):
        roles = re.findall(r'<td[^>]*data-role="([a-z-]+)"', row)
        assert roles[0] == "title" and roles.count("title") == 1
        assert roles.count("amount") == 1 and roles[-1] == "meta-badge"
        assert set(roles[1:-1]) == {"meta", "amount"}
        labels = re.findall(r'<td[^>]*data-label="([^"]+)"', row)
        assert labels[1:4] == ["Rent billed", "Rent collected", "Fees billed"]
    total = between(page, '<tr class="totals" id="pnl-total">', "</tr>")
    assert len(re.findall(r'data-label="', total)) == 10


def test_the_style_sheet_carries_the_report_family_rules() -> None:
    from django.contrib.staticfiles import finders

    path = finders.find("finance/app.css")
    assert path is not None
    with open(path, encoding="utf-8") as sheet:
        css = sheet.read()
    assert ".pnl-table .grp-start { border-left: 1px solid var(--border-soft); }" in css
    assert ".pnl-table .negative { color: var(--unpaid-text); }" in css
    assert ".kpi-value.tone-paid { color: var(--paid-text); }" in css
    phone = css[css.index("@media (max-width: 719.98px)") :]
    assert ".table-cards > table.pnl-table.role-cards tr.totals { flex-wrap: wrap;" in phone
