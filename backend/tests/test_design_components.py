"""PLAN-012c Batch B (docs/qa/PLAN-012c-design-pass.md D5, D6, §6 B): the component classes on
the main pages, the dashboard as cards, the print style sheet.

Each component is checked where it lives: the page header on every list and detail page,
``td.money`` on the amount cells, ``aria-sort`` where the view sorts (the arrears list), the
status badges with their glyphs (the charge tables and the grid legend), the empty states
with a next step, the confirmation pages, the flash messages as toasts, the pagers and the
wizards' step lists. A style-sheet test checks that app.css has no literal colour, that every
``.badge-<family>`` names its family's tokens and has its icon (PLAN-012f D6: the seven
families of the spec), and that print.css hides the bar and the navigation. The twelve-month
grid at 1,280 px is the e2e suite's (``tests/e2e/test_grid_laptop.py``).
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.messages import constants, get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponse, HttpResponseBase
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import reverse

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from finance.templatetags.components import FAMILIES, action_named, badge_family, with_done
from ledger.models import (
    LeaseEnd,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
)
from tests.auth_helpers import make_user, verified_client
from tests.receivables_helpers import a_lease, a_rent_charge, a_store, an_operator
from tests.test_lease_end import a_store_lease, deductions, draft, settle
from tests.test_transfer import ready as a_ready_transfer

pytestmark = pytest.mark.django_db


BACKEND = Path(settings.BASE_DIR)
TEMPLATES = BACKEND / "ledger" / "templates" / "ledger"
STATIC = BACKEND / "finance" / "static" / "finance"
APP_CSS = STATIC / "app.css"
PRINT_CSS = STATIC / "print.css"
TOKENS_CSS = STATIC / "tokens.css"
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
OCTOBER = date(2026, 10, 1)
#: The icon of each badge family (DESIGN-SPEC §3, PLAN-012f D6).
GLYPHS = {
    "paid": "\u2713",
    "unpaid": "\u25cf",
    "overdue": "\u25cf",
    "pending": "\u25d0",
    "partial": "\u25d1",
    "waived": "\u2013",
    "neutral": "\u25cb",
}
GLYPH = '<span class="badge-icon" aria-hidden="true">'
#: A stored value of each family (PLAN-012f D6): a rent charge "due" is unpaid, one "unpaid"
#: (past due) overdue; "voided" is neutral. A family's own name is not a value for "unpaid".
VALUE_OF = {
    "paid": "paid",
    "unpaid": "due",
    "overdue": "unpaid",
    "pending": "pending",
    "partial": "partial",
    "waived": "waived",
    "neutral": "voided",
}


def client_for(role: Role) -> tuple[Client, Any]:
    user, device = make_user(f"comp-{uuid.uuid4().hex[:8]}", role)
    assert device is not None
    return verified_client(user, device), user


def body(response: HttpResponseBase) -> str:
    assert response.status_code == 200, response
    assert isinstance(response, HttpResponse)
    return response.content.decode()


@pytest.fixture(autouse=True)
def memory_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents_storage, "get_storage", MemoryDocumentStorage)


# --- The page header ---------------------------------------------------------------------------

LIST_PAGES = [
    "/",
    "/stores/",
    "/operators/",
    "/leases/",
    "/payments/",
    "/returns/",
    "/webhooks/",
    "/arrears/",
    "/grid/",
    "/calendar/",
    "/reports/",
    "/reports/pnl/",
    "/reports/aging/",
    "/reports/revenue/",
    "/reports/deposits/",
    "/reports/payments-by-method/",
    "/disputes/",
    "/reconciliation/",
    "/run/",
    "/audit/",
    "/settings/",
    "/search/?q=zzz",
]


#: The page header's title (PLAN-012c D5): the h1 first in .page-header, or, on the dashboard
#: and the workspaces (PLAN-012f D7, D8), first in its title block (after the breadcrumb).
PAGE_HEADER = re.compile(
    r'<div class="page-header[^"]*">\s*(?:<div class="page-title(?:-block)?">\s*'
    r"(?:<nav [^>]*>.*?</nav>\s*)?)?<h1[ >]",
    re.S,
)


@pytest.mark.parametrize("path", LIST_PAGES)
def test_every_list_page_has_its_page_header(path: str) -> None:
    html = body(client_for(Role.ADMIN)[0].get(path))
    assert PAGE_HEADER.search(html), path
    assert html.count("<h1") == 1, path


def test_the_detail_pages_have_their_page_header() -> None:
    lease = a_lease(a_store("2201 HEADER", 2201), an_operator("Header Ops"))
    charge = a_rent_charge(lease, OCTOBER)
    client, _ = client_for(Role.ADMIN)
    for path in (
        f"/stores/{lease.store_id}/",
        f"/operators/{lease.operator_id}/",
        f"/leases/{lease.pk}/",
        f"/charges/{charge.pk}/",
        f"/operators/{lease.operator_id}/statement/",
    ):
        html = body(client.get(path))
        assert PAGE_HEADER.search(html), path


def test_every_staff_template_with_a_title_wraps_it_in_the_page_header() -> None:
    exempt = {
        "reports/statement_public.html",  # the operator's own page, out of scope (§2)
        "invoices/_invoice.html",  # the invoice document itself (print and preview)
    }
    found = 0
    for path in sorted(TEMPLATES.rglob("*.html")):
        relative = path.relative_to(TEMPLATES).as_posix()
        if relative in exempt:
            continue
        text = path.read_text()
        for at in [match.start() for match in re.finditer("<h1", text)]:
            found += 1
            line = text[text.rfind("\n", 0, at) + 1 : text.find("\n", at)]
            # A header partial (the wizards' ledger/lease_ends/_title.html) holds the whole
            # page header on its line: the breadcrumb title block of PLAN-012f D10 included.
            if '<div class="page-header"><h1' in line or PAGE_HEADER.search(line):
                continue
            header = text.rfind('{% block header %}<div class="page-header', 0, at)
            assert header != -1 and "{% endblock %}" not in text[header:at], relative
            assert PAGE_HEADER.search(text[header:at] + "<h1>"), relative
    assert found >= 70


def test_a_list_page_puts_its_primary_action_in_the_header() -> None:
    html = body(client_for(Role.ADMIN)[0].get("/stores/"))
    header = html[html.index('<div class="page-header">') :].split("</h1>", 1)[1]
    header = header.split("</div></div>", 1)[0]
    # PLAN-012f-a D3: "Download spreadsheet" (secondary) then "+ New store" (primary).
    actions = header[header.index('<div class="page-actions">') :]
    assert '<a class="btn" id="export-csv"' in actions
    assert actions.index('id="export-csv"') < actions.index('<a class="btn btn-primary"')
    assert " New store</a>" in actions


# --- The workspace classes ---------------------------------------------------------------------


def test_the_store_workspace_carries_the_reference_classes() -> None:
    lease = a_lease(a_store("2202 WORK", 2202), an_operator("Work Ops"))
    html = body(client_for(Role.BILLING)[0].get(f"/stores/{lease.store_id}/"))
    assert '<section id="workspace-header" class="workspace-header"' in html
    assert 'id="action-bar" class="action-bar"' in html
    # PLAN-012f D6: the tabs are .tab links; the Charges filter is the segmented filter.
    assert 'class="tabs store-tabs" id="store-tabs"' in html
    assert 'class="segmented" id="charge-filters"' in html
    css = APP_CSS.read_text()
    assert '.tab[aria-current="page"], .tab[aria-selected="true"] {' in css


# --- Tables ------------------------------------------------------------------------------------


def test_the_amount_cells_of_the_charge_tables_are_money_cells() -> None:
    lease = a_lease(a_store("2203 MONEY", 2203), an_operator("Money Ops"))
    a_rent_charge(lease, OCTOBER)
    html = body(client_for(Role.BILLING)[0].get(f"/stores/{lease.store_id}/"))
    table = html[html.index('<table id="store-charges" class="role-cards">') :].split(
        "</table>", 1
    )[0]
    # PLAN-012f D8: the charge's amount is the Amount column.
    assert '<td class="money" data-role="amount" data-label="Amount">$3,500.00</td>' in table
    assert (
        '<td class="money" data-role="meta-if-partial" data-label="Balance">$3,500.00</td>' in table
    )
    assert '<th class="money">Balance</th>' in table


def test_no_amount_cell_of_a_template_is_left_aligned() -> None:
    """Every <td> whose content starts with an amount ("$", or "−$") is a money cell."""
    exempt = {
        "reports/statement_public.html",  # out of scope (§2)
        "invoices/_invoice.html",  # the invoice document's own styles
        "grid/page.html",  # the grid's cells and totals have their own layout (11b)
        "operators/_contacts_table.html",  # a promise is a sentence: "$500.00 by 2026-10-01"
    }
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        relative = path.relative_to(TEMPLATES).as_posix()
        if relative in exempt:
            continue
        for match in re.finditer(r"<td\b([^>]*)>(.*?)</td>", path.read_text(), flags=re.S):
            inner = re.sub(r"\{%.*?%\}", "", match.group(2)).strip()
            inner = inner.removeprefix("<s>")
            if re.match(r"^(&minus;)?\$\{\{", inner) and "money" not in match.group(1):
                offenders.append(f"{relative}: {match.group(0)[:80]}")
    assert offenders == []


def test_money_cells_are_right_aligned_in_tabular_figures() -> None:
    css = APP_CSS.read_text()
    rule = css.split("td.money, th.money {", 1)[1].split("}", 1)[0]
    assert "text-align: right;" in rule and "font-variant-numeric: tabular-nums;" in rule


@pytest.mark.parametrize(
    ("query", "column", "direction"),
    [
        ("", "Months behind", "descending"),
        ("?sort=operator&dir=asc", "Operator", "ascending"),
        ("?sort=owed&dir=desc", "Total owed", "descending"),
        ("?sort=last_payment&dir=asc", "Last payment", "ascending"),
    ],
)
def test_the_sorted_column_of_the_arrears_list_carries_aria_sort(
    query: str, column: str, direction: str
) -> None:
    html = body(client_for(Role.VIEWER)[0].get(f"/arrears/{query}"))
    head = html[html.index('<table id="arrears" class="role-cards">') :].split("</thead>", 1)[0]
    sorted_headers = re.findall(
        r'<th class="sortable[^"]*" aria-sort="(\w+)"><a [^>]*>([^<]+)</a>', head
    )
    assert sorted_headers == [(direction, column)]
    assert head.count('class="sortable') == 4  # the four columns the view sorts by
    # The cells keep their header's text as their label: the arrow is drawn by CSS.
    css = APP_CSS.read_text()
    assert 'th[aria-sort="ascending"] a::after { content: " \\25B2"; }' in css
    assert 'th[aria-sort="descending"] a::after { content: " \\25BC"; }' in css


def test_no_other_page_claims_a_sort_it_does_not_do() -> None:
    for path in sorted(TEMPLATES.rglob("*.html")):
        relative = path.relative_to(TEMPLATES).as_posix()
        if relative in ("arrears/page.html", "stores/list.html"):  # PLAN-012f-a D3: Sort
            continue
        assert "aria-sort" not in path.read_text(), relative


def test_the_table_component_styles_the_responsive_regions() -> None:
    css = APP_CSS.read_text()
    assert (
        ".table thead th, .table-cards > table > thead th, .table-scroll > table > thead th {"
        in css
    )
    wide = css.split("@media (min-width: 720px) {", 1)[1].split("\n}\n", 1)[0]
    assert "position: sticky; top: 0;" in wide  # the head row stays in view
    # PLAN-012f D6 (DESIGN-SPEC §3): rows separated by --border-soft, no zebra rows.
    assert "tr:nth-child(even)" not in css
    assert "th, td { padding: 10px 12px; border-bottom: 1px solid var(--border-soft);" in css


# --- Badges ------------------------------------------------------------------------------------


def test_the_charge_table_shows_the_status_as_a_badge() -> None:
    lease = a_lease(a_store("2204 BADGE", 2204), an_operator("Badge Ops"))
    a_rent_charge(lease, OCTOBER, RentChargeStatus.UNPAID)
    a_rent_charge(lease, date(2026, 9, 1), RentChargeStatus.PAID, paid="3500.00")
    html = body(client_for(Role.VIEWER)[0].get(f"/stores/{lease.store_id}/?tab=charges&filter=all"))
    table = html[html.index('<table id="store-charges" class="role-cards">') :].split(
        "</table>", 1
    )[0]
    # PLAN-012f D6: a rent charge "unpaid" (past due) is the overdue family.
    assert f'<span class="badge badge-overdue">{GLYPH}\u25cf</span> Unpaid</span>' in table
    assert f'<span class="badge badge-paid">{GLYPH}\u2713</span> Paid</span>' in table


def test_the_grid_legend_is_a_badge_per_status() -> None:
    html = body(client_for(Role.VIEWER)[0].get("/grid/"))
    legend = html[html.index('<p class="legend muted">') :].split("</p>", 1)[0]
    # PLAN-012e-4 D2: the owner's order, "Unpaid" in its two shades, no "Due" item; PLAN-012f
    # D6: each a badge of its family (due is the unpaid family, unpaid the overdue one).
    for family, label in (
        ("paid", "Paid"),
        ("unpaid", "Unpaid, not yet overdue"),
        ("overdue", "Unpaid, overdue"),
        ("pending", "Pending"),
        ("partial", "Partly paid"),
        ("waived", "Waived"),
    ):
        badge = f'<span class="badge badge-{family}">{GLYPH}{GLYPHS[family]}</span> {label}</span>'
        assert badge in legend, family
    # "Not yet due" holds no charge: its dashed box, as the cells show it.
    assert '<span class="cell-not-due">Not yet due</span>' in legend
    assert "</span> Due</span>" not in legend
    # The grid's own thead stays the first on the page (the month headers).
    assert html.index("<thead") > html.index('<p class="legend muted">')


def test_needs_review_is_an_attention_badge() -> None:
    lease = a_lease(a_store("2205 REVIEW", 2205), an_operator("Review Ops"))
    store = lease.store
    store.needs_review = True
    store.save()
    html = body(client_for(Role.VIEWER)[0].get(f"/stores/{store.pk}/"))
    assert f'<span class="badge badge-unpaid">{GLYPH}\u25cf</span> Needs review</span>' in html
    every = "".join(path.read_text() for path in TEMPLATES.rglob("*.html"))
    # Every "Needs review" comes from the tag (none is laid out by hand).
    assert "Needs review</span>" not in every


@pytest.mark.parametrize(
    ("candidates", "status"),
    [
        (("Paid",), "paid"),
        (("Settled", "settled"), "paid"),
        (("Partially paid",), "partial"),
        (("Partly paid",), "partial"),
        (("Owed",), "unpaid"),
        (("Voided",), "neutral"),
        (("Returned",), "unpaid"),
        (("attention",), "unpaid"),
        (("Not due", "not_due"), "neutral"),
        (("  PENDING  ",), "pending"),
        (("Something else",), ""),
        ((None, "Due"), "unpaid"),
        (("unpaid", "Paid"), "overdue"),  # the value first: a rent charge past due
        (("Unpaid, overdue",), "overdue"),
    ],
)
def test_badge_family_reads_the_value_then_the_label(
    candidates: tuple[object, ...], status: str
) -> None:
    assert badge_family(*candidates) == status


def test_a_badge_label_is_escaped_and_an_unknown_status_is_a_plain_badge() -> None:
    from finance.templatetags.components import status_badge

    assert status_badge("<b>x</b>") == '<span class="badge">&lt;b&gt;x&lt;/b&gt;</span>'
    assert status_badge("Waived", "waived") == (
        f'<span class="badge badge-waived">{GLYPH}\u2013</span> Waived</span>'
    )
    assert status_badge("Not due", "not_due") == (
        f'<span class="badge badge-neutral">{GLYPH}\u25cb</span> Not due</span>'
    )
    assert status_badge("<i>Paid</i>", "paid") == (
        f'<span class="badge badge-paid">{GLYPH}\u2713</span> &lt;i&gt;Paid&lt;/i&gt;</span>'
    )


def test_every_badge_family_names_its_status_tokens_and_has_its_glyph() -> None:
    from finance.templatetags.components import status_badge

    css = APP_CSS.read_text()
    tokens = TOKENS_CSS.read_text()
    # PLAN-012f D1: the spec's block writes several tokens a line.
    defined = set(re.findall(r"(--[a-z0-9-]+):", tokens.split(":root", 1)[1]))
    # PLAN-012f D6: the seven families of the spec, each with its icon.
    assert FAMILIES == ("paid", "unpaid", "overdue", "pending", "partial", "waived", "neutral")
    assert set(FAMILIES) == set(GLYPHS)
    for family in FAMILIES:
        rule = css.split(f".badge-{family} {{", 1)[1].split("}", 1)[0]
        used = re.findall(r"var\((--[a-z0-9-]+)\)", rule)
        assert f"--{family}-bg" in used and f"--{family}-text" in used, family
        assert all(name in defined for name in used), (family, used)
        # The icon is in the markup, hidden from screen readers (the label is the text).
        assert status_badge("x", VALUE_OF[family]).startswith(
            f'<span class="badge badge-{family}">{GLYPH}{GLYPHS[family]}</span> '
        )
    # Every .badge-<name> rule in the sheet is a family or the icon's.
    rules = set(re.findall(r"^\.badge-([a-z-]+) \{", css, re.MULTILINE))
    assert rules == {*FAMILIES, "icon"}


def test_app_css_has_no_literal_colour_and_print_css_neither() -> None:
    for path in (APP_CSS, PRINT_CSS):
        offenders = [
            f"{path.name}:{number}"
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if LITERAL_COLOUR.search(line)
        ]
        assert offenders == []
        assert "url(" not in path.read_text() and "@import" not in path.read_text()


def test_the_new_status_tokens_are_documented_pairs() -> None:
    tokens = TOKENS_CSS.read_text()
    # PLAN-012f D1: voided takes the neutral pair, attention the unpaid one.
    assert "pair: --neutral-text on --neutral-bg" in tokens
    assert "pair: --unpaid-text on --unpaid-bg" in tokens


def test_the_badges_take_the_grid_colour_families() -> None:
    """PLAN-012e-4 D1, D2 in the families of PLAN-012f D6: overdue dark red with white text
    (no outline), unpaid on the soft pair, pending, partly paid, waived and neutral on their own
    pairs, each a documented pair; the grid's cell blocks read the same tokens."""
    css = APP_CSS.read_text()
    tokens = TOKENS_CSS.read_text()
    for family in FAMILIES:
        background, ink = f"--{family}-bg", f"--{family}-text"
        rule = css.split(f".badge-{family} {{", 1)[1].split("}", 1)[0]
        assert rule.strip() == f"background: var({background}); color: var({ink});", family
        assert f"pair: {ink} on {background}\n" in tokens, family
    # The grid's statuses take the same pairs: due the unpaid family, unpaid the overdue one.
    for status, family in (
        ("paid", "paid"),
        ("due", "unpaid"),
        ("unpaid", "overdue"),
        ("pending", "pending"),
        ("partial", "partial"),
        ("waived", "waived"),
    ):
        rule = css.split(f"td.cell-{status} .cell-block {{", 1)[1].split("}", 1)[0]
        assert rule.strip() == f"background: var(--{family}-bg); color: var(--{family}-text);"
    assert "--overdue-text: #FFFFFF;" in tokens
    assert "--status-unpaid-border" not in tokens and "--status-unpaid-border" not in css


# --- Empty states ------------------------------------------------------------------------------


def test_an_empty_list_says_so_and_offers_the_next_step() -> None:
    admin, _ = client_for(Role.ADMIN)
    stores = body(admin.get("/stores/"))
    assert re.search(
        r'<td colspan="7" class="muted empty">No stores match\. <a class="btn '
        r'empty-next" href="/stores/new/">New store</a></td>',
        stores,
    ), stores[stores.index("<tbody>") :][:400]
    searched = body(admin.get("/stores/?q=nothing-matches"))
    assert (
        'class="muted empty">No stores match. <a class="empty-next" href="/stores/">Clear</a>'
        in searched
    )
    leases = body(admin.get("/leases/"))
    assert (
        'class="muted empty">No lease yet. '
        '<a class="btn empty-next" href="/leases/new/">New lease</a>' in leases
    )
    # The leases list has no action bar, so its empty state does not point to one.
    assert "from the action bar" not in leases
    # One primary per page: the header's "New store", never a second in the empty state.
    assert stores.count("btn-primary") == 1 and leases.count("btn-primary") == 1
    # A viewer cannot create: the sentence stays, with no next step.
    viewer = body(client_for(Role.VIEWER)[0].get("/stores/"))
    assert 'class="muted empty">No stores match.</td>' in viewer


def test_an_empty_store_tab_offers_its_action_bar_step() -> None:
    lease = a_lease(a_store("2206 EMPTY", 2206), an_operator("Empty Ops"))
    client, _ = client_for(Role.BILLING)
    charges = body(client.get(f"/stores/{lease.store_id}/?tab=charges"))
    row = charges[charges.index('class="muted empty">No open charges.') :].split("</td>", 1)[0]
    assert (
        '<a class="btn empty-next" href="/invoices/new/store/' in row
        and ">Create invoice</a>" in row
    )
    payments = body(client.get(f"/stores/{lease.store_id}/?tab=payments"))
    row = payments[
        payments.index('class="muted empty">No payments yet. Add one from the action bar.') :
    ].split("</td>", 1)[0]
    assert f'href="/stores/{lease.store_id}/payments/new/"' in row
    contacts = body(client.get(f"/stores/{lease.store_id}/?tab=contacts"))
    row = contacts[contacts.index('class="muted empty">No entries yet. Add one.') :].split(
        "</td>", 1
    )[0]
    assert ">Add entry</a>" in row
    # A viewer has no action bar: the empty tab names no step.
    viewer = body(client_for(Role.VIEWER)[0].get(f"/stores/{lease.store_id}/?tab=charges"))
    assert 'class="muted empty">No open charges.</td>' in viewer


def test_every_empty_table_row_is_an_empty_state() -> None:
    for path in sorted(TEMPLATES.rglob("*.html")):
        relative = path.relative_to(TEMPLATES).as_posix()
        if relative == "reports/statement_public.html":
            continue
        for match in re.finditer(r'<tr><td colspan="[^>]*" class="([^"]*)">', path.read_text()):
            assert "empty" in match.group(1).split(), relative


def test_action_named_finds_an_action_by_key_or_none() -> None:
    class Action:
        def __init__(self, key: str) -> None:
            self.key = key

    found = Action("create_invoice")
    assert action_named([Action("x"), found], "create_invoice") is found
    assert action_named([Action("x")], "create_invoice") is None
    assert action_named(None, "create_invoice") is None


# --- Confirmation pages --------------------------------------------------------------------------

CONFIRM_TEMPLATES = (
    "invoices/charge_now.html",
    "one_off_charges/charge_now.html",
    "lease_ends/confirm.html",
    "transfers/confirm.html",
    "payments/refund.html",
    "payments/void.html",
    "leases/stop_autopay.html",
    "installments/cancel.html",
    "invoices/cancel.html",
    "one_off_charges/cancel.html",
    "one_off_charges/waive.html",
    "contacts/delete.html",
)
#: The pages that end or remove something: their one primary action is the danger button.
DANGER_CONFIRMS = (
    "installments/cancel.html",
    "invoices/cancel.html",
    "one_off_charges/cancel.html",
    "one_off_charges/waive.html",
    "contacts/delete.html",
    "lease_ends/confirm.html",
    "transfers/confirm.html",
    "leases/stop_autopay.html",
)


@pytest.mark.parametrize("relative", CONFIRM_TEMPLATES)
def test_each_confirmation_template_is_a_confirm_component(relative: str) -> None:
    text = (TEMPLATES / relative).read_text()
    section = text[text.index('<section class="confirm"') : text.index("</section>")]
    assert "What will happen" in section
    assert 'class="confirm-what"' in section
    # One primary action (filled, or the danger one) and one secondary .btn.
    primaries = re.findall(r'class="btn (?:btn-primary|btn-danger)"', section)
    assert len(primaries) == 1, primaries
    secondary = re.findall(r'<a class="btn" href=|"ledger/lease_ends/_cancel.html"', section)
    assert len(secondary) == 1, secondary


def _confirm_of(html: str) -> str:
    """The confirmation component to the end of the page's content (it holds sections)."""
    return html[html.index('<section class="confirm"') : html.index('id="print-footer"')]


def test_the_lease_end_confirmation_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_lease_end import TODAY

    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    end = draft(a_store_lease())
    assert deductions(end).ok
    settle(end)
    client, _ = client_for(Role.ADMIN)
    html = body(client.get(reverse("ledger:lease_ends:confirm", kwargs={"pk": end.pk})))
    section = _confirm_of(html)
    assert '<h2 class="confirm-question" id="confirm-question">Are you sure?</h2>' in section
    assert "<h3>What will happen</h3>" in section and 'id="effects"' in section
    assert 'id="confirm-lease-end"' in section and 'id="lease-end-cancel"' in section
    assert LeaseEnd.objects.get(pk=end.pk).status == "draft"


def test_the_transfer_confirmation_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_lease_end import TODAY  # the transfer tests use the same day

    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    client, user = client_for(Role.ADMIN)
    row = a_ready_transfer(user)
    html = body(client.get(reverse("ledger:transfers:confirm", kwargs={"pk": row.pk})))
    section = _confirm_of(html)
    assert "Are you sure?" in section and "<h3>What will happen</h3>" in section
    assert 'id="effects"' in section and 'id="incoming-effects"' in section
    assert 'id="transfer-cancel"' in section


def _manual_payment() -> Payment:
    lease = a_lease(a_store("2207 PAY", 2207), an_operator("Pay Ops"))
    charge = a_rent_charge(lease, OCTOBER, RentChargeStatus.PAID, paid="3500.00")
    return Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.WIRE,
        amount=charge.amount_due,
        status=PaymentStatus.SETTLED,
        initiated_on=OCTOBER,
        settled_on=OCTOBER,
        note="Wire received.",
    )


@pytest.mark.parametrize("name", ["refund", "void"])
def test_the_refund_and_void_pages_are_confirmations(name: str) -> None:
    payment = _manual_payment()
    client, _ = client_for(Role.ADMIN)
    html = body(client.get(reverse(f"ledger:payments:{name}", kwargs={"pk": payment.pk})))
    section = _confirm_of(html)
    assert '<h2 id="confirm-what-heading">What will happen</h2>' in section
    # A wire payment cannot go back through the processor: the refusal is in the component.
    assert f'id="{name}-refusal"' in section


def test_the_stop_autopay_page_is_a_confirmation() -> None:
    lease = a_lease(a_store("2208 STOP", 2208), an_operator("Stop Ops"))
    client, _ = client_for(Role.ADMIN)
    html = body(client.get(reverse("ledger:leases:stop_autopay", kwargs={"pk": lease.pk})))
    section = _confirm_of(html)
    assert "What will happen" in section and 'id="stop-warning"' in section
    assert 'id="nothing-to-stop"' in section


# --- Flash messages as toasts --------------------------------------------------------------------


def _with_messages(*items: tuple[int, str]) -> str:
    user, _ = make_user(f"comp-{uuid.uuid4().hex[:8]}", Role.VIEWER)
    request = RequestFactory().get("/")
    request.user = user
    request.session = SessionStore()
    storage = FallbackStorage(request)
    request._messages = storage  # type: ignore[attr-defined]
    for level, text in items:
        storage.add(level, text)
    return render_to_string("finance/base.html", {"messages": get_messages(request)}, request)


def test_a_flash_message_is_a_dismissable_toast_in_a_polite_live_region() -> None:
    html = _with_messages((constants.SUCCESS, "Saved."), (constants.ERROR, "Refused <b>x</b>."))
    assert '<ul class="toasts" id="toasts" aria-live="polite">' in html
    assert (
        '<li class="toast success" role="status"><span class="toast-text">Saved.</span>'
        '<button type="button" class="toast-dismiss" aria-label="Dismiss this message">&times;'
        "</button></li>"
    ) in html
    assert (
        '<li class="toast error" role="status"><span class="toast-text">'
        "Refused &lt;b&gt;x&lt;/b&gt;.</span>" in html
    )
    assert 'document.querySelectorAll(".toast-dismiss")' in html


def test_without_a_message_there_is_no_toast_list_and_no_script() -> None:
    html = _with_messages()
    assert 'class="toasts"' not in html and ".toast-dismiss" not in html


def test_the_toast_fades_after_eight_seconds_but_keeps_its_text_and_spares_refusals() -> None:
    css = APP_CSS.read_text()
    assert (
        ".toast:not(.warning):not(.error) { animation: toast-fade 600ms ease-in 8s forwards; }"
        in css
    )
    fade = css.split("@keyframes toast-fade {", 1)[1].split("} }", 1)[0]
    # Faded by opacity only: never removed from the page or the accessibility tree.
    assert fade.strip() == "to { opacity: 0;"
    toast_rules = "".join(re.findall(r"[^{}]*\.toast[^{}]*\{[^{}]*\}", css))
    assert "display: none" not in toast_rules and "visibility" not in toast_rules
    assert (
        "@media (prefers-reduced-motion: reduce) { .toast { animation: none !important; } }" in css
    )


# --- Pagers --------------------------------------------------------------------------------------


#: The list family's paged lists (PLAN-012f-a D3): their footer is ledger/_list_footer.html.
LIST_FAMILY = (
    "stores/list.html",
    "operators/list.html",
    "leases/list.html",
    "payments/list.html",
    "returns/list.html",
)


@pytest.mark.parametrize(
    "relative",
    [
        "stores/list.html",
        "operators/list.html",
        "leases/list.html",
        "payments/list.html",
        "returns/list.html",
        "webhooks/list.html",
        "reconciliation/page.html",
        "operators/contacts.html",
        "audit/page.html",
    ],
)
def test_every_paged_list_uses_the_pager(relative: str) -> None:
    text = (TEMPLATES / relative).read_text()
    if relative in LIST_FAMILY:  # PLAN-012f-a D3: the family's footer, inside the card
        assert '{% include "ledger/_list_footer.html" with page=page' in text
        text = (TEMPLATES / "_list_footer.html").read_text()
        assert '<div class="pager list-footer" role="navigation" aria-label="Pages">' in text
        assert '<span class="pager-count">' in text
        return
    assert '<div class="pager' in text and 'role="navigation" aria-label="Pages"' in text
    assert '<span class="pager-count">Page ' in text


def test_a_paged_list_renders_its_pager() -> None:
    for number in range(60):
        a_store(f"P{number:03d}", 9000 + number)
    html = body(client_for(Role.VIEWER)[0].get("/stores/"))
    pager = html[html.index('<div class="pager list-footer"') :].split("</div>", 1)[0]
    assert '<span class="pager-count">1–25 of 60</span>' in pager
    assert 'rel="next" href="?page=2">Next ›</a>' in pager
    assert '<span class="pager-page" aria-current="page">1</span>' in pager


# --- Wizard steps --------------------------------------------------------------------------------


def test_the_lease_end_wizard_marks_the_current_step_and_the_done_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_lease_end import TODAY

    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    end = draft(a_store_lease())
    client, _ = client_for(Role.ADMIN)
    html = body(client.get(reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk})))
    steps = html[html.index('<ol class="wizard-steps steps" id="wizard-steps"') :].split(
        "</ol>", 1
    )[0]
    items = re.findall(r"<li([^>]*)>(.*?)</li>", steps, flags=re.S)
    current = [
        index for index, (attributes, _) in enumerate(items) if 'aria-current="step"' in attributes
    ]
    assert len(current) == 1
    for index, (attributes, inner) in enumerate(items):
        done = index < current[0]
        assert ('class="step-done"' in attributes) is done, (index, attributes)
        assert ("(done)" in inner) is done
        if index != current[0]:
            assert "<a href=" in inner, inner  # the terms are set: every other step opens
    assert "<strong>" in items[current[0]][1]


def test_the_transfer_wizard_uses_the_same_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_lease_end import TODAY  # the transfer tests use the same day

    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    client, user = client_for(Role.ADMIN)
    row = a_ready_transfer(user)
    html = body(client.get(reverse("ledger:transfers:incoming", kwargs={"pk": row.pk})))
    steps = html[html.index('id="wizard-steps"') :].split("</ol>", 1)[0]
    assert steps.count('aria-current="step"') == 1
    assert 'class="step-done"' in steps  # the operator and the lease-end steps come first


def test_with_done_marks_the_steps_before_the_current_one() -> None:
    steps = [("A", "/a", False), ("B", "", True), ("C", "/c", False)]
    assert with_done(steps) == [
        ("A", "/a", False, True),
        ("B", "", True, False),
        ("C", "/c", False, False),
    ]
    # No current step: nothing is known to be done.
    assert [item[3] for item in with_done([("A", "/a", False), ("B", "/b", False)])] == [
        False,
        False,
    ]


# --- The dashboard as cards ----------------------------------------------------------------------


def test_the_dashboard_is_cards_with_the_figures_then_the_attention_list() -> None:
    html = body(client_for(Role.VIEWER)[0].get("/"))
    content = html[html.index('<div class="page-header"><div class="page-title"><h1>Dashboard') :]
    cards = re.findall(
        r'<section class="([^"]*\bcard\b[^"]*)"[^>]*?(?:id="([^"]+)")?[^>]*>', content
    )
    assert len(cards) >= 5
    first = content.index('class="card')
    # PLAN-012f D7: the four figures, then the attention card, the collection and coming-up
    # cards, the recent payments, then the run, the failed attempts and the other lists.
    assert content.index('id="figures-card"') < content.index('id="attention-none"')
    assert content.index('id="attention-none"') >= first  # the first card is the attention card
    assert content.index('id="attention-none"') < content.index('id="today-run"')
    order = [
        content.index(f'id="{name}"')
        for name in (
            "figures-card",
            "attention",
            "collection-card",
            "coming-up",
            "recent-payments",
            "today-run",
            "failed-attempts",
            "not-sent-card",
            "last-reconciliation-card",
        )
    ]
    assert order == sorted(order)
    assert "<style>" not in content  # the cards' styles are app.css's


# --- Print ---------------------------------------------------------------------------------------


def test_the_print_style_sheet_is_linked_on_the_statement_and_the_pnl() -> None:
    lease = a_lease(a_store("2209 PRINT", 2209), an_operator("Print Ops"))
    client, _ = client_for(Role.ADMIN)
    link = '<link rel="stylesheet" href="/static/finance/print.css" media="print">'
    for path in (f"/operators/{lease.operator_id}/statement/", "/reports/pnl/"):
        html = body(client.get(path))
        assert link in html, path
        assert html.index('href="/static/finance/app.css"') < html.index(link)
        assert (
            '<div class="print-only print-header" id="print-header">'
            '<span class="print-company">Monvera</span>' in html
        )
        assert re.search(
            r'<div class="print-only print-footer" id="print-footer">'
            # DESIGN-SPEC §5: the footer is read by a person, "Printed Oct 24, 2026".
            r"Printed [A-Z][a-z]{2} \d{1,2}, \d{4}</div>",
            html,
        )


def test_print_css_hides_the_shell_and_breaks_the_pages() -> None:
    css = PRINT_CSS.read_text()
    assert "@media print {" in css
    hidden = css.split("{ display: none !important; }", 1)[0]
    # PLAN-012f D3, D4: the sidebar, the phone bars and their toggles.
    for selector in (
        "#sidebar",
        "#phone-bar",
        "#tab-bar",
        ".sheet-toggle",
        ".toasts",
        ".action-bar",
    ):
        assert selector in hidden, selector
    assert (
        "main, main *, #page-header * { color: var(--text) !important; "
        "background: transparent !important; }" in css
    )
    assert "th, td { border: 1px solid var(--text) !important;" in css
    # The statement keeps its month heading rows but forces no page break (plan D6: store
    # groups only, on the P&L).
    assert "tr.print-month { break-inside: avoid;" in css
    assert not re.search(r"print-month[^{]*\{[^}]*break-before", css)
    assert ".pnl-detail tr.pnl-group { break-before: page;" in css
    assert ".print-only { display: block; }" in css
    # Never on screen.
    assert ".print-only { display: none; }" in APP_CSS.read_text()


def test_the_statement_has_a_month_row_per_month_and_the_pnl_its_detail_class() -> None:
    text = (TEMPLATES / "reports" / "statement.html").read_text()
    # The ISO month is the grouping key only, never shown; the heading reads "Oct 2026"
    # (DESIGN-SPEC §5).
    assert (
        '{% ifchanged line.on|date:"Y-m" %}<tr class="print-month">'
        '<th colspan="7" scope="colgroup">{{ line.on|month|default:"No date" }}</th>' in text
    )
    pnl = body(client_for(Role.ADMIN)[0].get("/reports/pnl/?detail=1"))
    assert '<div class="pnl-print pnl-detail">' in pnl
    assert '<div class="pnl-print">' in body(client_for(Role.ADMIN)[0].get("/reports/pnl/"))


@pytest.mark.parametrize("relative", DANGER_CONFIRMS)
def test_a_confirmation_that_ends_or_removes_something_asks_with_the_danger_button(
    relative: str,
) -> None:
    text = (TEMPLATES / relative).read_text()
    section = text[text.index('<section class="confirm"') : text.index("</section>")]
    assert 'class="btn btn-danger"' in section and 'class="btn btn-primary"' not in section


def test_the_cancel_and_delete_pages_render_as_confirmations() -> None:
    from ledger.models import ContactEntry

    operator = an_operator("Delete Ops")
    lease = a_lease(a_store("2210 DELETE", 2210), operator)
    client, user = client_for(Role.ADMIN)
    entry = ContactEntry.objects.create(
        operator=operator,
        lease=lease,
        kind="call",
        body="Called.",
        happened_on=OCTOBER,
        author=user,
    )
    html = body(client.get(reverse("ledger:contacts:delete", kwargs={"pk": entry.pk})))
    section = _confirm_of(html)
    assert '<h2 class="confirm-question" id="confirm-question">Are you sure?</h2>' in section
    assert "<h3>What will happen</h3>" in section
    assert 'id="confirm-delete-contact">Delete</button> <a class="btn" href=' in section
    assert '<div class="field">' in section


# --- Forms: every labelled control is a .field ---------------------------------------------------

#: Templates whose single controls go through stores/_form_field.html (at least this many).
FIELD_TEMPLATES = {
    "installments/cancel.html": 1,
    "installments/split.html": 1,
    "installments/reschedule.html": 1,
    "invoices/cancel.html": 1,
    "one_off_charges/cancel.html": 1,
    "one_off_charges/waive.html": 1,
    "one_off_charges/link_invoice.html": 1,
    "charges/link_invoice.html": 1,
    "contacts/correct.html": 1,
    "contacts/delete.html": 1,
    "transfers/confirm.html": 1,
    "documents/_section.html": 2,
}


@pytest.mark.parametrize(("relative", "count"), sorted(FIELD_TEMPLATES.items()))
def test_the_hand_laid_forms_use_the_field_component(relative: str, count: int) -> None:
    text = (TEMPLATES / relative).read_text()
    assert text.count('{% include "ledger/stores/_form_field.html"') >= count
    # No single control's errors are printed above it any more.
    assert not re.search(r"\{\{ [a-z_]+\.(note|reason|body)\.errors \}\}", text), relative


def test_the_field_puts_the_error_under_the_control_and_marks_it() -> None:
    from django import forms

    class NoteForm(forms.Form):
        note = forms.CharField(label="Note", help_text="Say why.")

    form = NoteForm(data={"note": ""})
    assert not form.is_valid()
    html = render_to_string(
        "ledger/stores/_form_field.html", {"field": form["note"], "label": "Why", "mark": True}
    )
    assert html.index('<div class="field has-error">') < html.index("<label")
    assert html.index("<label") < html.index("<input") < html.index('class="help muted"')
    assert html.index('class="help muted"') < html.index('class="errorlist')
    assert (
        '>Why <span class="required">*<span class="visually-hidden"> required</span></span></label>'
        in html
    )


def test_the_refund_amount_is_a_field_with_its_error_under_it() -> None:
    text = (TEMPLATES / "payments" / "refund.html").read_text()
    block = text[
        text.index('<div class="field{% if form.amount.errors %} has-error{% endif %}">') :
    ]
    block = block.split("</div>", 1)[0]
    assert block.index("<input") < block.index('class="help muted"') < block.index("field-error")
