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
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import Client, RequestFactory
from django.urls import reverse

import documents_storage
from accounts.roles import Role
from documents_storage.memory import MemoryDocumentStorage
from finance.templatetags.components import (
    CHIP_GLYPHS,
    FAMILIES,
    PERIODS,
    action_named,
    avatar_initials,
    badge_family,
    parse_period,
    with_done,
)
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
from tests.test_lease_end import a_store_lease, deductions, draft, saved, settle
from tests.test_transfer import ready as a_ready_transfer
from tests.text_helpers import assert_absent

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
    # PLAN-012f-b C-2: the two payment_form.html pages became one add_payment.html.
    # PLAN-012f-b E-4: the eleven confirmation pages lost their own header (their dialog's
    # title names them over the parent page's header).
    assert found >= 58


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
    # PLAN-014 B6: the timeline's empty row (li#timeline-empty) keeps the sentence and the step.
    row = contacts[contacts.index('id="timeline-empty">No entries yet. Add one.') :].split(
        "</li>", 1
    )[0]
    assert ">Log contact</a>" in row
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
    # One primary action (filled, or the danger one) and one secondary .btn. PLAN-012f-b E-4:
    # in a dialog the secondary is "Keep …" back to the parent page, also under a refusal
    # (the branch without the form), so a page with a refusal branch writes it twice.
    primaries = re.findall(r'class="btn (?:btn-primary|btn-danger)"', section)
    assert len(primaries) == 1, primaries
    if "{% dialog " in text:
        keeps = re.findall(
            r'<a class="btn" href="\{\{ back_url \}\}" id="([a-z-]+)">[^<]*Keep ', section
        )
        assert 1 <= len(keeps) <= 2 and len(set(keeps)) == 1, keeps
        assert len(re.findall(r'<a class="btn" href=', section)) == len(keeps)
    else:
        secondary = re.findall(r'<a class="btn" href=|"ledger/lease_ends/_cancel.html"', section)
        assert len(secondary) == 1, secondary


def _confirm_of(html: str) -> str:
    """The confirmation component to the end of the page's content (it holds sections)."""
    return html[html.index('<section class="confirm"') : html.index('id="print-footer"')]


def test_the_lease_end_confirmation_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_lease_end import TODAY

    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    end = saved(draft(a_store_lease()))
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
    # PLAN-012f-b E-4: the level's icon before the text (tests/test_states.py has each level).
    success = html[html.index('<li class="toast success" role="status">') :].split("</li>", 1)[0]
    assert success.startswith(
        '<li class="toast success" role="status"><span class="toast-icon"><svg class="icon '
        'icon-check" width="16"'
    )
    assert success.endswith(
        '</span><span class="toast-text">Saved.</span>'
        '<button type="button" class="toast-dismiss" aria-label="Dismiss this message">&times;'
        "</button>"
    )
    error = html[html.index('<li class="toast error" role="status">') :].split("</li>", 1)[0]
    assert '<span class="toast-text">Refused &lt;b&gt;x&lt;/b&gt;.</span>' in error
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
    content = html[html.index('<div class="page-header dashboard-header">') :]
    cards = re.findall(
        r'<section class="([^"]*\bcard\b[^"]*)"[^>]*?(?:id="([^"]+)")?[^>]*>', content
    )
    assert len(cards) >= 4
    # PLAN-015 Batch 1 re-pin (the owner's mockup; DESIGN-SPEC §7.5): the four figures, the
    # Collections card beside the Operators card, the Recent activity card beside the mini
    # calendar; the Needs attention card, Recent payments, Payment status and the run's cards
    # are gone from the page (the last ones live on Admin › Daily charges).
    order = [
        content.index(f'id="{name}"')
        for name in (
            "figures-card",
            "collections-card",
            "operators-card",
            "activity-card",
            "mini-calendar",
        )
    ]
    assert order == sorted(order)
    for gone in ("attention", "recent-payments", "payment-status", "today-run", "cashflow-card"):
        assert f'id="{gone}"' not in content
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
    assert (
        '<div class="dialog-foot"><a class="btn" href="/operators/'
        f'{operator.pk}/#contact-{entry.pk}" id="delete-contact-keep">Keep the entry</a> '
        '<button class="btn btn-danger" type="submit" id="confirm-delete-contact">Delete</button>'
    ) in section
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


# --- PLAN-015 Batch 0: Design v2's components (docs/qa/PLAN-015-design-v2.md §4.1) -------------


def rendered(source: str, **context: Any) -> str:
    return Template("{% load components %}" + source).render(Context(context))


def svg_icon(name: str, size: int) -> str:
    return f'<svg class="icon icon-{name}" width="{size}" height="{size}" viewBox="0 0 24 24"'


def test_kpi2_renders_the_icon_title_sub_figure_badge_and_footer_link() -> None:
    html = rendered(
        '{% kpi2 "calendar" "Due this month" "38 stores · due Nov 1st" "$184,250.00" '
        'badge="↑ 4.2%" badge_tone="paid" link="/grid/" link_text="Open monthly rent" %}'
    )
    assert html.startswith(
        '<div class="kpi2"><div class="kpi2-head"><span class="kpi2-icon" aria-hidden="true">'
        + svg_icon("calendar", 16)
    )
    assert (
        '<div class="kpi2-titles"><p class="kpi2-title">Due this month</p>'
        '<p class="kpi2-sub">38 stores · due Nov 1st</p></div></div>'
    ) in html
    assert (
        '<div class="kpi2-amount"><span class="kpi2-value">$184,250.00</span>'
        '<span class="kpi2-pill kpi2-pill-paid">↑ 4.2%</span></div>'
    ) in html
    assert '<a class="kpi2-link" href="/grid/"><span>Open monthly rent</span>' in html
    assert html.index("Open monthly rent") < html.index(svg_icon("arrow-right", 16))
    assert html.endswith("</a></div>")


def test_kpi2_without_its_optional_parts_and_with_the_red_figure() -> None:
    html = rendered('{% kpi2 "alert" "Overdue" value="$41,650.00" value_tone="unpaid" %}')
    assert '<span class="kpi2-value tone-unpaid">$41,650.00</span>' in html
    assert "kpi2-sub" not in html and "kpi2-pill" not in html and "<a " not in html


# PLAN-015 Batch 1 re-pin: the unpaid tone joins for the Collected card's "↓ x%".
@pytest.mark.parametrize("tone", ["paid", "unpaid", "pending", "neutral"])
def test_kpi2_badge_tones(tone: str) -> None:
    html = rendered("{% kpi2 'banknote' 'Deposits' badge='3 unmatched' badge_tone=t %}", t=tone)
    assert f'<span class="kpi2-pill kpi2-pill-{tone}">3 unmatched</span>' in html


@pytest.mark.parametrize(
    "arguments",
    [
        "value_tone='paid'",
        "value_tone='red'",
        "badge='x' badge_tone='overdue'",
        "badge='x' badge_tone=''",
    ],
)
def test_kpi2_refuses_an_unknown_tone(arguments: str) -> None:
    with pytest.raises(ValueError):
        rendered("{% kpi2 'alert' 'Overdue' " + arguments + " %}")


@pytest.mark.parametrize(
    "link",
    ["https://evil.example.com/", "//evil.example.com/", "javascript:alert(1)", "/\\evil", ""],
)
def test_kpi2_draws_a_link_to_this_site_only(link: str) -> None:
    html = rendered("{% kpi2 'alert' 'Overdue' link=link link_text='See' %}", link=link)
    assert "<a " not in html and 'href="' not in html
    assert_absent(html, "evil")
    assert_absent(html, "javascript")


def test_kpi2_escapes_every_text() -> None:
    html = rendered(
        "{% kpi2 'alert' t sub=t value=t badge=t link='/x/' link_text=t %}", t="<b>Ada</b>"
    )
    assert "<b>" not in html and html.count("&lt;b&gt;Ada&lt;/b&gt;") == 5


def test_the_period_control_is_a_get_form_of_three_submit_buttons() -> None:
    html = rendered("{% period_control 'quarter' %}")
    assert html == (
        '<form method="get" class="segmented period-control" aria-label="Period">'
        '<button type="submit" name="period" value="month" aria-pressed="false">This month</button>'
        '<button type="submit" name="period" value="quarter" aria-pressed="true">Quarter</button>'
        '<button type="submit" name="period" value="year" aria-pressed="false">Year</button>'
        "</form>"
    )
    assert "action=" not in html  # the form submits to the page itself, no script needed


@pytest.mark.parametrize(
    "raw", [None, "", "week", "Month", " month", "YEAR", "../etc", "<script>", 3, "month\n"]
)
def test_a_fourth_period_falls_back_to_this_month(raw: object) -> None:
    assert parse_period(raw) == "month"
    html = rendered("{% period_control current %}", current=raw)
    assert re.findall(r'value="(\w+)" aria-pressed="true"', html) == ["month"]
    assert html.count("<button") == 3


@pytest.mark.parametrize("value", ["month", "quarter", "year"])
def test_parse_period_keeps_the_three(value: str) -> None:
    assert parse_period(value) == value
    assert [v for v, _ in PERIODS] == ["month", "quarter", "year"]


def test_the_period_control_can_show_fewer_periods_and_refuses_an_unknown_one() -> None:
    html = rendered("{% period_control 'year' choices %}", choices=["month", "year"])
    assert re.findall(r'value="(\w+)"', html) == ["month", "year"]
    with pytest.raises(ValueError):
        rendered("{% period_control 'month' choices %}", choices=["month", "week"])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Northwind", "NO"),
        ("Dana Whitfield", "DW"),
        ("Harbor Lane Retail", "HL"),
        ("summit corner shops", "SC"),
        ("O'Brien & Sons, LLC", "OS"),
        ("  Pinecrest   Markets  ", "PM"),
        ("Élodie Düzenli", "ED"),
        ("#101 Maple", "MA"),
        ("A", "A"),
        ("", "—"),
        ("   ", "—"),
        ("!!! 42", "—"),
        (None, "—"),
    ],
)
def test_the_avatar_initials(name: str | None, expected: str) -> None:
    assert avatar_initials(name) == expected


def test_the_avatar_tile_is_a_tinted_square_hidden_from_screen_readers() -> None:
    assert (
        rendered("{% avatar_tile 'Dana Whitfield' %}")
        == '<span class="avatar-tile" aria-hidden="true">DW</span>'
    )
    assert (
        rendered("{% avatar_tile 'Dana Whitfield' 36 %}")
        == '<span class="avatar-tile avatar-tile-36" aria-hidden="true">DW</span>'
    )
    assert rendered("{% avatar_tile name %}", name="<b>x</b>") == (
        '<span class="avatar-tile" aria-hidden="true">BX</span>'
    )
    with pytest.raises(ValueError):
        rendered("{% avatar_tile 'Dana' 40 %}")


@pytest.mark.parametrize(
    ("tone", "glyph"),
    [
        ("paid", "check"),
        ("unpaid", "dot"),
        ("overdue", "dot"),
        ("pending", "dot"),
        ("partial", "dot"),
        ("neutral", "circle"),
    ],
)
def test_every_chip_leads_with_its_glyph(tone: str, glyph: str) -> None:
    html = rendered("{% chip tone 'Due Nov 20, 2026' %}", tone=tone)
    assert html.startswith(f'<span class="status-chip status-chip-{tone}">' + svg_icon(glyph, 12))
    assert 'aria-hidden="true"' in html.split("</svg>", 1)[0]  # the words carry the meaning
    assert html.endswith("</svg><span>Due Nov 20, 2026</span></span>")
    assert CHIP_GLYPHS[tone] == glyph


def test_a_chip_may_name_another_glyph_of_the_set_only_and_a_tone_it_knows() -> None:
    html = rendered("{% chip 'neutral' 'Vacant · 23 days' 'dot' %}")
    assert svg_icon("dot", 12) in html
    for source in (
        "{% chip 'neutral' 'x' 'trash' %}",
        "{% chip 'waived' 'x' %}",
        "{% chip '' 'x' %}",
        "{% chip 'Paid' 'x' %}",
    ):
        with pytest.raises(ValueError):
            rendered(source)
    # A value from the view is escaped (a template literal is the template's own text).
    assert "&lt;script&gt;" in rendered("{% chip 'paid' text %}", text="<script>")


def test_the_card_head_puts_the_label_left_and_the_link_right() -> None:
    html = render_to_string(
        "finance/components/card_head.html",
        {"label": "Operators", "link": "/operators/", "link_text": "All operators ›"},
    )
    assert html.endswith(
        '<div class="card-head"><p class="section-label">Operators</p>'
        '<a class="card-link" href="/operators/">All operators ›</a></div>'
    )
    bare = render_to_string("finance/components/card_head.html", {"label": "Collections"})
    assert bare.endswith('<div class="card-head"><p class="section-label">Collections</p></div>')


def test_the_new_components_take_their_colours_from_documented_pairs() -> None:
    css = APP_CSS.read_text()

    def rule(selector: str) -> str:
        return css.split(f"\n{selector} {{", 1)[1].split("}", 1)[0]

    for tone in CHIP_GLYPHS:
        assert f"background: var(--{tone}-bg); color: var(--{tone}-text);" in rule(
            f".status-chip-{tone}"
        ), tone
    for tone in ("paid", "unpaid", "pending", "neutral"):
        assert f"background: var(--{tone}-bg); color: var(--{tone}-text);" in rule(
            f".kpi2-pill-{tone}"
        ), tone
    for selector in (".kpi2-icon", ".avatar-tile"):
        assert "background: var(--month-current); color: var(--link);" in rule(selector)
    assert "color: var(--unpaid-text)" in rule(".kpi2-value.tone-unpaid")
    tokens = TOKENS_CSS.read_text()
    assert "pair: --link on --month-current" in tokens
    assert "pair[dark]: --link on --month-current over --surface" in tokens
    assert "pair[dark]: --link on --month-current over --surface-2" in tokens


def test_every_dark_pair_meets_wcag_aa_with_the_dark_values() -> None:
    """The ``pair[dark]:`` lines (PLAN-012g; PLAN-015 adds the avatar tile on the sidebar's
    card) measured with the dark block's values over the light ones, the owner's one accepted
    pair (the primary button, 3.97) excepted as in the light check."""
    from tests.test_design_accessibility import (
        ACCEPTED_UNDER_AA,
        PAIR,
        Pair,
        definitions,
        pair_ratio,
    )
    from tests.test_plan012g_a_tokens import block_after, declarations, strip_comments

    tokens = TOKENS_CSS.read_text()
    dark = definitions(tokens)
    dark.update(declarations(block_after(strip_comments(tokens), r':root\[data-theme="dark"\]')))
    dark_lines = [
        Pair(fg, bg, kind, over)
        for fg, bg, over, kind in PAIR.findall(
            "\n".join(
                line.replace("pair[dark]:", "pair:")
                for line in tokens.splitlines()
                if "pair[dark]:" in line
            )
        )
    ]
    assert len(dark_lines) >= 40
    failures = []
    for pair in dark_lines:
        ratio = pair_ratio(pair, dark)
        low, high = ACCEPTED_UNDER_AA.get(str(pair), (pair.minimum, float("inf")))
        if not low <= ratio <= high:
            failures.append(f"{pair}: {ratio:.2f}")
    assert failures == []
