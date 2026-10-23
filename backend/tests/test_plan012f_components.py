"""PLAN-012f Batch B (docs/qa/PLAN-012f-approved-design.md D6, §6 B): the approved design's
components (docs/design/DESIGN-SPEC.md §3), each checked on a page that uses it.

The seven badge families with their icon and word; the first cell's title and muted second
line (``ledger/_cell_title.html``); the totals row; the scroll region; the KPI card, the
progress bar (its widths sum to at most 100 % and a zero total draws nothing), the date chip and
the avatar tags; the segmented filter; the tabs' active border; the one-line help rule; the
grid's cell blocks, the dashed "Not yet due" and the tinted current month; the cards, buttons
and inputs on the tokens; and no 12c component class left behind.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.template import Context, Template
from django.test import Client

from accounts.roles import Role
from finance.templatetags.components import (
    FAMILIES,
    GLYPHS,
    avatar,
    date_chip,
    kpi,
    progress,
    progress_widths,
    status_badge,
)
from ledger.models import ContactKind, RentChargeStatus
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_contact_log import entry, promise
from tests.test_grid import TODAY, every_status_store
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND / "finance" / "static" / "finance"
APP_CSS = STATIC / "app.css"
PRINT_CSS = STATIC / "print.css"
TOKENS_CSS = STATIC / "tokens.css"
TEMPLATE_ROOTS = (
    BACKEND / "ledger" / "templates",
    BACKEND / "finance" / "templates",
    BACKEND / "accounts" / "templates",
)
MARKUP = '<script>alert("x")</script>'
ICON = '<span class="badge-icon" aria-hidden="true">'


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def css() -> str:
    return APP_CSS.read_text(encoding="utf-8")


def rule(selector: str) -> str:
    """The declarations of the rule whose selector list is exactly ``selector``."""
    found = re.search(rf"(?:^|\}}|\n){re.escape(selector)} \{{([^}}]*)\}}", css())
    assert found is not None, selector
    return found.group(1)


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.content.decode()


def section(html: str, start: str, end: str) -> str:
    at = html.index(start)
    return html[at : html.index(end, at)]


# --- Status badges: the seven families -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "label", "family", "icon"),
    [
        ("paid", "Paid", "paid", "✓"),
        ("due", "Unpaid", "unpaid", "●"),
        ("owed", "Owed", "unpaid", "●"),
        ("unpaid", "Unpaid", "overdue", "●"),
        ("pending", "Pending", "pending", "◐"),
        ("partial", "Partly paid", "partial", "◑"),
        ("waived", "Waived", "waived", "–"),
        ("voided", "Voided", "neutral", "○"),
        ("not_due", "Not yet due", "neutral", "○"),
        ("returned", "Returned", "unpaid", "●"),
        ("attention", "Closed", "unpaid", "●"),
    ],
)
def test_each_status_is_a_pill_of_its_family_with_its_icon_and_word(
    value: str, label: str, family: str, icon: str
) -> None:
    assert status_badge(label, value) == (
        f'<span class="badge badge-{family}">{ICON}{icon}</span> {label}</span>'
    )


def test_the_seven_families_have_their_pill_rules_on_their_tokens() -> None:
    assert FAMILIES == ("paid", "unpaid", "overdue", "pending", "partial", "waived", "neutral")
    for family in FAMILIES:
        assert rule(f".badge-{family}").strip() == (
            f"background: var(--{family}-bg); color: var(--{family}-text);"
        )
    pill = rule(".badge")
    for declaration in (
        "padding: 3px 9px;",
        "border-radius: var(--radius-pill);",
        "font-size: var(--fs-sm);",
        "font-weight: 600;",
    ):
        assert declaration in pill, declaration
    # Overdue: dark red with white text.
    assert "--overdue-bg: #B42318;" in TOKENS_CSS.read_text() and "--overdue-text: #FFFFFF;" in (
        TOKENS_CSS.read_text()
    )
    assert GLYPHS["overdue"] == GLYPHS["unpaid"] == "●"


def test_the_charges_tab_shows_the_overdue_and_unpaid_families() -> None:
    lease = a_lease(a_store("3301 FAMILY", 3301), an_operator("Family Co"))
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID)
    a_rent_charge(lease, date(2026, 10, 1), RentChargeStatus.DUE)
    html = page(client_for(Role.VIEWER)[0], f"/stores/{lease.store_id}/?tab=charges")
    table = section(html, '<table id="store-charges" class="charges-table">', "</table>")
    assert f'<span class="badge badge-overdue">{ICON}●</span> Unpaid</span>' in table
    assert f'<span class="badge badge-unpaid">{ICON}●</span> Due</span>' in table


# --- Tables: the first cell, the totals row, the scroll region -----------------------------


def test_the_first_cell_is_a_bold_link_with_a_muted_second_line() -> None:
    store = a_store(f"Istanbul {MARKUP}", 3302)
    a_lease(store, an_operator(f"Ops {MARKUP}"), started=date(2026, 1, 1))
    viewer, _ = client_for(Role.VIEWER)
    stores = page(viewer, "/stores/")
    assert MARKUP not in stores
    assert (
        f'<td data-label="Name"><a class="cell-title" href="/stores/{store.pk}/">Istanbul '
        "&lt;script&gt;" in stores
    )
    grid = page(viewer, "/grid/")
    cell = section(grid, f'data-store="{store.pk}"', "</th>")
    assert '<a class="cell-title" href="' in cell
    assert (
        '<span class="cell-subline">Ops &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</span>'
        in (cell)
    )
    assert "font-weight: 600;" in rule(".cell-title")
    subline = rule(".cell-subline")
    assert "color: var(--muted);" in subline and "font-size: var(--fs-sm);" in subline


def test_the_cell_title_include_without_a_link_or_a_subline() -> None:
    template = Template('{% include "ledger/_cell_title.html" with title=title link=link %}')
    out = template.render(Context({"title": MARKUP, "link": ""}))
    assert out == (
        '<span class="cell-title">&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</span>'
    )


def test_the_totals_row_is_on_surface_2_with_a_2px_top_border() -> None:
    html = page(client_for(Role.ADMIN)[0], "/reports/aging/")
    assert '<tr class="totals" id="aging-total">' in html
    totals = rule("tr.totals > th, tr.totals > td")
    assert "background: var(--surface-2);" in totals
    assert "border-top: 2px solid var(--border);" in totals
    grid = page(client_for(Role.VIEWER)[0], "/grid/")
    # PLAN-012f D9: "Collected" over the order of the counts each month's total shows.
    assert (
        '<tr class="totals">\n          <th scope="row" class="store-col">Collected'
        '<span class="cell-subline">paid · unpaid · pending</span></th>'
    ) in grid


def test_a_wide_table_sits_in_the_scroll_region() -> None:
    html = page(client_for(Role.ADMIN)[0], "/audit/")
    assert re.search(r'<div class="table-scroll" tabindex="0">\s*<table id="audit-rows">', html)
    region = rule(".table-scroll")
    assert "overflow-x: auto;" in region
    head = rule(
        ".table thead th, .table-cards > table > thead th, .table-scroll > table > thead th"
    )
    for declaration in (
        "background: var(--surface-2);",
        "color: var(--muted);",
        "font-size: var(--fs-sm);",
        "font-weight: 500;",
    ):
        assert declaration in head, declaration
    assert "border-bottom: 1px solid var(--border-soft);" in rule("th, td")
    assert "font-size: var(--fs-base);" in rule(
        ".table, .table-cards > table, .table-scroll > table"
    )


# --- The KPI card ---------------------------------------------------------------------------


def test_the_kpi_tag_renders_its_three_lines_and_escapes_them() -> None:
    assert kpi("Unpaid balances", "$18,420.00", "11 stores · 3 overdue", "unpaid") == (
        '<div class="kpi"><span class="kpi-label">Unpaid balances</span>'
        '<span class="kpi-value tone-unpaid">$18,420.00</span>'
        '<span class="kpi-note">11 stores · 3 overdue</span></div>'
    )
    plain = kpi(MARKUP, "37")
    assert MARKUP not in plain and '<span class="kpi-value">37</span>' in plain
    assert "kpi-note" not in plain
    # Only the unpaid tone is red; another word is ignored, never put in the class.
    assert '<span class="kpi-value">1</span>' in kpi("x", "1", "", f'paid" onclick="{MARKUP}')
    assert "font-size: var(--fs-kpi);" in rule(".kpi-value")
    assert "font-weight: 700;" in rule(".kpi-value")
    assert rule(".kpi-value.tone-unpaid").strip() == "color: var(--unpaid-text);"
    label = rule(".kpi-label")
    assert "color: var(--muted);" in label and "font-size: var(--fs-sm);" in label


def test_the_dashboard_figures_are_kpi_cards() -> None:
    lease = a_lease(a_store("3303 KPI", 3303), an_operator("Kpi Co"))
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.UNPAID)
    html = page(client_for(Role.VIEWER)[0], "/")
    figures = section(html, 'id="figures-card"', "</section>")
    assert figures.count('<div class="kpi">') == 4
    # PLAN-012f D7: Unpaid balances (red), with its stores and those overdue.
    assert (
        '<span class="kpi-label">Unpaid balances</span><span class="kpi-value tone-unpaid">'
    ) in figures
    assert '<span class="kpi-note">1 store · 1 overdue</span>' in figures
    assert '<a class="kpi-link" id="figure-outstanding" href="/arrears/">' in figures


# --- The progress bar -------------------------------------------------------------------------


def test_the_progress_bar_draws_each_segment_and_its_legend() -> None:
    out = progress([("paid", Decimal("412300")), ("pending", "24750"), ("unpaid", 11000)])
    widths = [Decimal(width) for width in re.findall(r'style="width: ([0-9.]+)%"', out)]
    assert widths == [Decimal("92.0"), Decimal("5.5"), Decimal("2.4")]
    assert sum(widths) <= 100
    assert 'class="progress-segment progress-paid"' in out
    assert re.findall(r'<span class="progress-key progress-([a-z]+)" aria-hidden="true">', out) == [
        "paid",
        "pending",
        "unpaid",
    ]
    assert 'aria-label="Paid $412,300.00, Pending $24,750.00, Unpaid $11,000.00"' in out
    assert "height: 10px;" in rule(".progress-bar")
    key = rule(".progress-key")
    assert "width: 8px;" in key and "height: 8px;" in key


@pytest.mark.parametrize(
    "segments",
    [
        [("paid", 1), ("pending", 1), ("partial", 1)],
        [("paid", "0.01"), ("unpaid", "999999.99")],
        [("paid", 2), ("pending", 2), ("partial", 2), ("unpaid", 2), ("waived", 2), ("neutral", 1)],
        [("paid", "1e30"), ("unpaid", "1")],
    ],
)
def test_the_progress_widths_never_sum_over_100(segments: list[tuple[str, object]]) -> None:
    widths = [width for _, _, width in progress_widths(segments)]
    assert widths and sum(widths) <= 100
    assert all(width >= 0 for width in widths)


@pytest.mark.parametrize(
    "segments",
    [[], [("paid", 0), ("unpaid", 0)], [("paid", -5)], [("paid", "abc"), ("unpaid", None)]],
)
def test_a_zero_total_draws_nothing(segments: list[tuple[str, object]]) -> None:
    assert progress(segments) == ""


def test_a_negative_segment_is_left_out_and_an_unknown_family_refused() -> None:
    assert [family for family, _, _ in progress_widths([("paid", 5), ("unpaid", -5)])] == ["paid"]
    with pytest.raises(ValueError, match="unknown progress family"):
        progress([(MARKUP, 1)])


# --- The date chip and the avatar ---------------------------------------------------------------


def test_the_date_chip_is_the_month_over_the_day() -> None:
    assert date_chip(date(2026, 10, 24)) == (
        '<time class="date-chip" datetime="2026-10-24"><span class="date-chip-month">Oct</span>'
        '<span class="date-chip-day">24</span></time>'
    )
    assert date_chip(None) == "" and date_chip(MARKUP) == ""
    chip = rule(".date-chip")
    assert "width: 44px;" in chip and "background: var(--bg);" in chip
    assert "border-radius: var(--radius-ctl);" in chip
    assert "font-size: var(--fs-xs);" in rule(".date-chip-month")
    day = rule(".date-chip-day")
    assert "font-size: var(--fs-lg);" in day and "font-weight: 700;" in day


def test_the_avatar_is_a_round_letter_in_the_pending_or_neutral_colours() -> None:
    assert (
        avatar("P", "pending") == '<span class="avatar avatar-pending" aria-hidden="true">P</span>'
    )
    assert avatar("call", "neutral") == (
        '<span class="avatar avatar-neutral" aria-hidden="true">C</span>'
    )
    assert avatar("<b>", "neutral") == (
        '<span class="avatar avatar-neutral" aria-hidden="true">&lt;</span>'
    )
    with pytest.raises(ValueError, match="unknown avatar family"):
        avatar("P", "overdue")
    circle = rule(".avatar")
    assert "width: 34px;" in circle and "height: 34px;" in circle
    assert "border-radius: 50%;" in circle
    assert rule(".avatar-pending").strip() == (
        "background: var(--pending-bg); color: var(--pending-text);"
    )
    assert rule(".avatar-neutral").strip() == (
        "background: var(--neutral-bg); color: var(--neutral-text);"
    )


def test_the_contact_tabs_show_each_entry_with_its_avatar() -> None:
    operator = an_operator(f"Avatar {MARKUP}")
    lease = a_lease(a_store("3304 LOG", 3304), operator)
    pledged = promise(operator, lease=lease, by=date(2026, 10, 30), amount="3000.00")
    called = entry(operator, lease=lease, body=f"No answer {MARKUP}")
    billing, _ = client_for(Role.BILLING)
    for path in (f"/stores/{lease.store_id}/?tab=contacts", f"/operators/{operator.pk}/"):
        html = page(billing, path)
        assert MARKUP not in html, path
        log = section(html, 'id="contact-entries"', "</ul>")
        promise_entry = section(log, f'id="contact-{pledged.pk}"', "</li>")
        assert '<span class="avatar avatar-pending" aria-hidden="true">P</span>' in promise_entry
        assert '<strong class="contact-kind">Promise to pay</strong>' in promise_entry
        assert '<div class="contact-actions">' in promise_entry  # Kept, Reschedule, Broken
        call_entry = section(log, f'id="contact-{called.pk}"', "</li>")
        assert called.kind == ContactKind.CALL
        assert '<span class="avatar avatar-neutral" aria-hidden="true">C</span>' in call_entry
        assert '<p class="contact-meta">2026-10-01 · <a href="/stores/' in call_entry
    assert "color: var(--muted);" in rule(".contact-meta")
    assert "font-weight: 700;" in rule(".contact-kind")


# --- The segmented filter and the tabs ----------------------------------------------------------


def test_the_charges_filter_is_a_segmented_control() -> None:
    lease = a_lease(a_store("3305 SEGMENT", 3305), an_operator("Segment Co"))
    html = page(client_for(Role.VIEWER)[0], f"/stores/{lease.store_id}/?tab=charges&filter=paid")
    control = section(html, '<span class="segmented" id="charge-filters"', "</span>")
    assert 'role="group" aria-label="Show charges"' in control
    links = re.findall(
        r'<a href="[^"]*filter=([a-z]+)#tab-charges"( aria-current="page")?>', control
    )
    assert links == [("open", ""), ("paid", ' aria-current="page"'), ("all", "")]
    assert "btn" not in control  # segments, not buttons
    track = rule(".segmented")
    assert "background: var(--bg);" in track and "border-radius: var(--radius-ctl);" in track
    active = rule('.segmented a[aria-current="page"]')
    assert "background: var(--surface);" in active and "box-shadow: 0 0 0 1px var(--border);" in (
        active
    )
    # The grid's status filter stays a select.
    grid = page(client_for(Role.VIEWER)[0], "/grid/")
    assert '<select name="status" id="status">' in grid


def test_the_open_tab_carries_the_teal_bottom_border() -> None:
    lease = a_lease(a_store("3306 TABS", 3306), an_operator("Tabs Co"))
    html = page(client_for(Role.VIEWER)[0], f"/stores/{lease.store_id}/?tab=payments")
    tabs = section(html, '<nav aria-label="Store tabs" class="tabs store-tabs"', "</nav>")
    current = re.findall(r'<a class="tab" id="tab-link-([a-z]+)"[^>]*aria-current="page"', tabs)
    assert current == ["payments"]
    assert 'class="btn"' not in tabs
    active = rule('.tab[aria-current="page"], .tab[aria-selected="true"]')
    assert "border-bottom-color: var(--teal);" in active and "color: var(--text);" in active
    tab = rule(".tab")
    assert "padding: 10px 14px;" in tab and "font-size: var(--fs-base);" in tab
    assert "color: var(--muted);" in tab and "border-bottom: 2px solid transparent;" in tab
    assert "overflow-x: auto;" in rule(".tabs")  # scrolls on a phone


# --- The help line ------------------------------------------------------------------------------


def test_the_help_line_is_one_12px_muted_line_under_its_block() -> None:
    help_rule = rule("p.help")
    assert "color: var(--muted);" in help_rule and "font-size: var(--fs-sm);" in help_rule
    lease = a_lease(a_store("3307 HELP", 3307), an_operator("Help Co"))
    html = page(client_for(Role.ADMIN)[0], f"/stores/{lease.store_id}/")
    card = section(html, 'id="store-cost"', "</section>")
    assert card.count('<p class="help">') == 1


# --- The grid's cell blocks -----------------------------------------------------------------------


def grid_row(html: str, label: str) -> str:
    body = html[html.index("<tbody") :]
    start = body.rfind("<tr", 0, body.index(label))
    return body[start : body.index("</tr>", start)]


def test_each_grid_cell_is_a_block_with_its_amount_over_its_status_line() -> None:
    charges = every_status_store()
    html = page(client_for(Role.VIEWER)[0], "/grid/?from=2026-05&to=2026-12")
    row = grid_row(html, "Halid")
    cells = dict(re.findall(r'<td class="cell cell-([a-z_]+)"[^>]*>(.*?)</td>', row, re.S))
    paid = charges["paid"].pk
    assert cells["paid"] == (
        f'<a class="cell-block" href="/charges/{paid}/" aria-label="2026-05: $3,500.00, Paid">'
        '<span class="cell-amount">$3,500.00</span><span class="cell-status">'
        '<span aria-hidden="true">✓</span> Paid</span></a>'
    )
    # Overdue: "Overdue N d", N the days since its due date (2026-06-05 to 2026-10-06).
    assert '<span aria-hidden="true">●</span> Overdue 123 d</span>' in cells["unpaid"]
    assert 'aria-label="2026-06: $3,500.00, Overdue 123 days"' in cells["unpaid"]
    assert '<span aria-hidden="true">●</span> Unpaid</span>' in cells["due"]
    assert '<span class="cell-amount">$1,200.00 of $3,500.00</span>' in cells["partial"]
    assert "</span> Pending · from 2026-07-05</span>" in cells["pending"]
    assert '<span class="cell-amount">$3,500.00</span>' in cells["waived"]
    # November and December: future months inside the lease, "Not yet due" in its dashed box.
    assert cells["not_due"] == (
        '<span class="cell-block"><span class="cell-status">Not yet due</span></span>'
    )
    dashed = rule("td.cell-not_due .cell-block, .cell-not-due")
    assert "border: 1px dashed var(--input-border);" in dashed and "font-style: italic;" in dashed


def test_a_month_outside_the_lease_is_an_empty_cell() -> None:
    a_lease(a_store("3308 OUTSIDE", 3308), an_operator("Outside Co"), started=date(2026, 9, 1))
    html = page(client_for(Role.VIEWER)[0], "/grid/?from=2026-07&to=2026-09")
    row = grid_row(html, "Outside Co")
    assert '<td class="cell" data-month="2026-07" data-status=""></td>' in row
    assert '<td class="cell" data-month="2026-08" data-status=""></td>' in row


def test_the_current_month_is_tinted() -> None:
    a_lease(a_store("3309 TINT", 3309), an_operator("Tint Co"))
    html = page(client_for(Role.VIEWER)[0], "/grid/?from=2026-08&to=2026-11")
    cols = re.findall(r"<col(?: class=\"([a-z-]+)\")?>", section(html, "<colgroup>", "</colgroup>"))
    assert cols == ["", "", "", "month-current", ""]  # the store column, Aug, Sep, Oct, Nov
    assert (
        '<th scope="col" class="month-col month-current" aria-current="date">2026-10</th>' in html
    )
    assert html.count('class="column-total month-current"') == 1
    assert "background: var(--month-current);" in rule(
        "table.grid col.month-current, table.grid .month-current"
    )
    assert "--month-current: #EEF6F5;" in TOKENS_CSS.read_text()


# --- Cards, buttons, inputs -----------------------------------------------------------------------


def test_the_card_buttons_and_inputs_follow_the_spec() -> None:
    card = rule(".card")
    for declaration in (
        "padding: 18px 20px;",
        "border: 1px solid var(--border);",
        "border-radius: var(--radius-card);",
        "background: var(--surface);",
    ):
        assert declaration in card, declaration
    assert "font-size: var(--fs-lg);" in rule(".card h2") and "font-weight: 600;" in rule(
        ".card h2"
    )
    assert "margin-left: auto;" in rule(".card-link")
    assert "border-radius: var(--radius-pill);" in rule(".count-pill")
    label = rule(".section-label")
    for declaration in (
        "color: var(--muted);",
        "font-size: var(--fs-base);",
        "font-weight: 600;",
        "text-transform: uppercase;",
        "letter-spacing: .04em;",
    ):
        assert declaration in label, declaration
    button = rule(".btn")
    for declaration in (
        "padding: 10px 16px;",
        "border: 1px solid var(--input-border);",
        "border-radius: var(--radius-ctl);",
        "background: var(--surface);",
        "font-weight: 600;",
    ):
        assert declaration in button, declaration
    small = rule(".btn-sm")
    assert "padding: 6px 12px;" in small and "font-size: var(--fs-sm);" in small
    assert rule(".btn-danger").strip() == "color: var(--unpaid-text);"
    field = rule(
        "input[type=text], input[type=email], input[type=search], input[type=number], "
        "input[type=password], input[type=date], input[type=month], input[type=tel], "
        "input[type=url], select, textarea"
    )
    for declaration in (
        "padding: 9px 12px;",
        "border: 1px solid var(--input-border);",
        "border-radius: var(--radius-ctl);",
    ):
        assert declaration in field, declaration
    assert rule("input[type=search]").strip() == "background: var(--surface-2);"
    assert "border-color: var(--link);" in rule(
        "input:focus-visible, select:focus-visible, textarea:focus-visible"
    )


def test_the_lease_menu_is_a_drop_down_button_ending_with_the_arrow() -> None:
    lease = a_lease(a_store("3310 MENU", 3310), an_operator("Menu Co"))
    html = page(client_for(Role.ADMIN)[0], f"/stores/{lease.store_id}/")
    summary = re.search(r'id="lease-menu"><summary class="btn btn-menu">([^<]+)</summary>', html)
    assert summary is not None and summary.group(1).endswith("▾")


# --- No 12c component class left -----------------------------------------------------------------

#: PLAN-012f D10 (Batch D) adds the back-link paragraph of PLAN-012d D5 (``p.crumb``, now the
#: page header's breadcrumb) and the sign-in pages' 12c box (``main.container``, now the
#: two-panel layout of D13).
OLD_COMPONENT = re.compile(
    r"badge-glyph|badge-(?:due|voided|attention)\b|badge not-due|\.not-due\b"
    r'|class="[^"]*\bfigures?\b|\.figures?\b|actions tabs|actions store-tabs|alert-list'
    r"|nth-child\(even\)"
    r'|class="crumb"|\.crumb\b|class="container"|\.container\b'
)


def test_no_12c_component_class_remains() -> None:
    sources = [APP_CSS, PRINT_CSS, BACKEND / "finance" / "templatetags" / "components.py"]
    for root in TEMPLATE_ROOTS:
        sources.extend(sorted(root.rglob("*.html")))
    offenders = [
        f"{path.relative_to(BACKEND)}:{number}: {match.group(0)}"
        for path in sources
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for match in OLD_COMPONENT.finditer(line)
    ]
    assert offenders == []
    # The check sees what it looks for.
    assert OLD_COMPONENT.search('<span class="badge-glyph">') and OLD_COMPONENT.search(
        '<div class="figures">'
    )
    assert not OLD_COMPONENT.search('<div class="kpi-row">')
    assert OLD_COMPONENT.search('<p class="crumb" id="store-crumb">')
    assert OLD_COMPONENT.search('<main class="container">')
    assert not OLD_COMPONENT.search('<nav class="breadcrumb" id="breadcrumb">')
