"""PLAN-012f-a Batch C, slice C1: the invoice builder and the form family (D5;
docs/design/DESIGN-SPEC.md §4 Create invoice, create-invoice-laptop.png).

The builder: the header (the breadcrumb, the title with its muted context line, the right-hand
"nothing reaches the provider" line); the Lines card (Item, Description, Qty, Unit amount,
Total and the trash; a Rent line's selects under its Item box; "+ Add line" beside its help);
How to collect as one tile per choice of the form's options (the chosen one marked, each
choice's fields inside its own tile, every field in the markup for a page without script); the
SUMMARY card (the lines and their amounts, the total, the memo and the due date, one button per
choice with its id, Discard); the phone order (markup order). The forms: each page's cards and
their headings, the context line in the header, the one primary in the summary card. The
``line_total`` filter. Nothing reaches a network.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.template import Context, Template
from django.urls import reverse

from accounts.roles import Role
from finance.templatetags.builder import line_total
from ledger.forms import InvoiceLineForm, InvoiceLineFormSet
from ledger.models import InvoiceDraft, Lease, PaymentChannel
from ledger.services import invoice_builder
from ledger.services.invoice_builder import LineInput
from ledger.views.invoices import _lines_total
from payments.fake import FakeProvider
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
APP_CSS = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance" / "app.css"
COLUMNS = ["Item", "Description", "Qty", "Unit amount", "Total", "Remove"]


@pytest.fixture(autouse=True)
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def a_world() -> Lease:
    return a_lease(
        a_store("1991 Istanbul", 1991),
        an_operator("Fatih Co"),
        channel=PaymentChannel.WIRE,
        started=date(2026, 1, 13),
    )


def a_draft(lease: Lease) -> InvoiceDraft:
    assert lease.operator is not None
    lines = [
        LineInput("Arrears September", "", None, D("100.00")),
        LineInput("Sign repair", "North wall", D("3"), D("25.00")),
    ]
    actor = client_for(Role.BILLING)[1]
    outcome = invoice_builder.create_draft(lease.operator, lease, "", None, lines, actor)
    assert outcome.ok and outcome.draft is not None, outcome.message
    return outcome.draft


def get(role: Role, path: str) -> str:
    response = client_for(role)[0].get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def header(body: str) -> str:
    start = body.index('id="page-header"')
    return body[start : body.index("</header>", start)]


def element(body: str, opening: str, tag: str) -> str:
    """The element that ``opening`` starts, up to its matching close tag (nesting counted)."""
    start = body.index(opening)
    depth, at = 0, start
    pattern = re.compile(rf"<(/?){tag}\b[^>]*>")
    for match in pattern.finditer(body, start):
        depth += -1 if match.group(1) else 1
        at = match.end()
        if depth == 0:
            break
    return body[start:at]


def text(markup: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", markup).split())


def new_store_path(lease: Lease, query: str = "") -> str:
    path = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})
    return path + f"?store={lease.store_id}&lease={lease.pk}{query}"


# --- the header ----------------------------------------------------------------------------


def test_the_builder_header_has_the_crumb_the_title_its_context_and_the_draft_line() -> None:
    lease = a_world()
    head = header(get(Role.BILLING, new_store_path(lease)))
    crumb = element(head, '<nav class="breadcrumb"', "nav")
    assert text(crumb) == "Stores › #1991 › New invoice"
    assert '<h1 class="store-title">New invoice</h1><p class="muted" id="known-fields">' in head
    assert "Store 1991 Istanbul · Lease from Jan 13, 2026 · Operator Fatih Co" in head
    note = element(head, '<p class="builder-note"', "p")
    assert text(note) == "Draft · nothing reaches Stax until you choose how to collect"
    # The draft's own page: its number, the same context line and the same note.
    draft = a_draft(lease)
    head = header(get(Role.BILLING, reverse("ledger:invoices:draft", kwargs={"pk": draft.pk})))
    assert f"Invoice draft {draft.number}" in text(head)
    assert 'id="known-fields"' in head and 'id="builder-note"' in head
    assert 'id="draft-preview"' in head and 'id="draft-cancel"' not in head


def test_the_operator_builder_names_the_operator_as_its_context() -> None:
    lease = a_world()
    assert lease.operator is not None
    path = reverse("ledger:invoices:new_operator", kwargs={"pk": lease.operator.pk})
    head = header(get(Role.BILLING, path))
    assert '<p class="muted" id="known-fields">Fatih Co</p>' in head


# --- the Lines card ------------------------------------------------------------------------


def test_the_lines_card_has_its_columns_the_rent_selects_and_the_add_line_button() -> None:
    lease = a_world()
    body = get(Role.BILLING, new_store_path(lease))
    card = element(body, '<section class="card lines-card" id="draft-card">', "section")
    assert "<h2>Lines</h2>" in card
    head = element(card, "<thead>", "thead")
    assert [text(cell) for cell in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)] == COLUMNS
    row = element(card, '<tr class="draft-line"', "tr")
    item = element(row, '<td class="line-item"', "td")
    # The Rent line's lease and month selects and the Save box sit under the Item box.
    assert 'name="lines-0-item"' in item and '<div class="rent-field">' in item
    assert 'name="lines-0-rent_lease"' in item and 'name="lines-0-rent_month"' in item
    assert 'class="save-item"' in item and 'name="lines-0-save_item"' in item
    trash = element(row, '<button class="btn line-trash', "button")
    assert 'aria-label="Remove line"' in trash and 'class="icon icon-trash"' in trash
    add = element(card, '<div class="add-line-row">', "div")
    assert 'id="add-line"' in add and "Add line" in text(add) and 'id="lines-help"' in add
    assert 'class="icon icon-plus"' in add
    # The memo and the due date left the Lines card for the SUMMARY card.
    assert 'name="memo"' not in card and 'name="due_date"' not in card


def test_each_line_shows_its_total_and_the_summary_lists_the_lines() -> None:
    draft = a_draft(a_world())
    body = get(Role.BILLING, reverse("ledger:invoices:draft", kwargs={"pk": draft.pk}))
    totals = re.findall(r'<td class="money line-total"[^>]*>(.*?)</td>', body, re.S)
    # The two saved lines, then the blank row the page adds.
    assert totals[:2] == ["$100.00", "$75.00"]
    summary = element(body, '<aside class="form-summary builder-side"', "aside")
    lines = element(summary, '<ul class="summary-lines" id="summary-lines">', "ul")
    assert re.findall(r"<li>(.*?)</li>", lines, re.S) == [
        '<span class="summary-name">Arrears September</span> '
        '<span class="summary-amount">$100.00</span>',
        '<span class="summary-name">Sign repair</span> <span class="summary-amount">$75.00</span>',
    ]
    assert (
        '<p class="summary-total" id="lines-total"><span>Total</span> '
        '<span id="lines-total-value">$175.00</span></p>'
    ) in summary
    assert 'name="memo"' in summary and 'name="due_date"' in summary


def test_the_summary_holds_one_button_per_choice_and_discard() -> None:
    lease = a_world()
    body = get(Role.ADMIN, new_store_path(lease))
    summary = element(body, '<aside class="form-summary builder-side"', "aside")
    actions = element(summary, '<div class="form-actions" id="collect-actions">', "div")
    buttons = re.findall(r"<button ([^>]*)>", actions)
    ids = [re.search(r'id="([^"]+)"', attributes).group(1) for attributes in buttons]  # type: ignore[union-attr]
    radios = re.findall(r'<input type="radio" name="collect" value="([^"]+)"', body)
    named = {
        "draft": "save-draft",
        "email": "send-draft",
        "charge_now": "draft-charge-now",
        "schedule": "schedule-draft",
        "record": "record-draft",
        "autopay": "autopay-draft",  # PLAN-012f-a Batch E: the sixth choice
    }
    assert ids == [named[value] for value in radios]
    for attributes, value in zip(buttons, radios, strict=True):
        assert 'class="btn btn-primary"' in attributes
        assert f'name="submit_collect" value="{value}"' in attributes
        assert f'data-shown-when="collect-{value}"' in attributes
    discard = element(summary, '<a class="btn summary-discard"', "a")
    back = f"/stores/{lease.store_id}/"
    assert discard.startswith(f'<a class="btn summary-discard" href="{back}')
    assert text(discard) == "Discard"
    help_card = element(summary, '<section class="card builder-help"', "section")
    assert "The primary button changes with the choice" in help_card
    # A draft's Discard opens its delete page (a confirmation; the delete itself is a POST).
    draft = a_draft(lease)
    body = get(Role.BILLING, reverse("ledger:invoices:draft", kwargs={"pk": draft.pk}))
    cancel = reverse("ledger:invoices:cancel", kwargs={"pk": draft.pk})
    summary = element(body, '<aside class="form-summary builder-side"', "aside")
    assert f'<a class="btn summary-discard" href="{cancel}" id="draft-cancel">Discard</a>' in (
        summary
    )
    assert body.count('id="draft-cancel"') == 1


# --- How to collect ------------------------------------------------------------------------


TILE = r'<div class="collect-tile(?: is-chosen)?" id="tile-[^"]+">'


def tiles(body: str) -> list[str]:
    card = element(body, '<section class="card collect-card"', "section")
    return [element(card, opening, "div") for opening in re.findall(TILE, card)]


def test_one_tile_per_choice_each_with_its_radio_name_and_sentence() -> None:
    lease = a_world()
    for role, count in ((Role.BILLING, 5), (Role.ADMIN, 6)):
        body = get(role, new_store_path(lease))
        found = tiles(body)
        radios = re.findall(r'type="radio" name="collect"', body)
        assert len(found) == len(radios), role
        # Charge now is an admin's choice only while the pay-invoice switch is on.
        # Six choices for an admin (Charge now included), five for billing (PLAN-012f-a E).
        assert len(found) == (count if 'id="collect-charge_now"' in body else 5), role
        for tile in found:
            assert tile.count('type="radio" name="collect"') == 1
            assert tile.count('<span class="collect-name">') == 1
            assert tile.count('<span class="collect-note">') == 1
    assert "<legend>How to collect</legend>" in body
    assert body.count('id="collect-help"') == 1


def test_the_chosen_tile_is_marked_and_holds_its_fields() -> None:
    lease = a_world()
    body = get(Role.BILLING, new_store_path(lease, "&collect=record"))
    marked = [
        tile for tile in tiles(body) if tile.startswith('<div class="collect-tile is-chosen"')
    ]
    assert len(marked) == 1 and 'id="collect-record" checked' in marked[0]
    record = next(tile for tile in tiles(body) if 'id="tile-record"' in tile)
    for name in ("method", "paid_on", "reference", "payment_note"):
        assert f'name="{name}"' in record, name
    assert 'id="collect-record-fields"' in record
    others = [tile for tile in tiles(body) if 'id="tile-record"' not in tile]
    assert all('name="method"' not in tile for tile in others)
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".collect-tile:has(input[type=radio]:checked) {" in css
    # Without script every field is in the page: no wrapper starts hidden.
    fields = element(record, '<div class="collect-fields" id="collect-record-fields">', "div")
    assert re.search(r'<div class="field[^"]*"[^>]*\bhidden\b', fields) is None
    assert 'data-shown-when="collect-record"' in fields


def test_the_schedule_tile_holds_the_debit_date_when_schedule_is_offered() -> None:
    lease = a_lease(a_store("1992"), an_operator("Debit Co"), started=date(2026, 1, 1))
    body = get(Role.BILLING, new_store_path(lease))
    schedule = [tile for tile in tiles(body) if 'id="tile-schedule"' in tile]
    assert len(schedule) == 1  # an active lease: Schedule for a date is offered
    # Without a lease there is no Schedule tile: one line under the tiles says why.
    assert lease.operator is not None
    path = reverse("ledger:invoices:new_operator", kwargs={"pk": lease.operator.pk})
    body = get(Role.BILLING, path)
    assert not [tile for tile in tiles(body) if 'id="tile-schedule"' in tile]
    assert 'id="collect-schedule-note"' in body and 'name="schedule_on"' not in body
    assert 'id="collect-schedule-fields"' in schedule[0] and 'name="schedule_on"' in schedule[0]
    assert 'name="confirm_large_debit"' in schedule[0]


def test_a_hostile_choice_opens_save_draft_and_marks_no_other_tile() -> None:
    lease = a_world()
    body = get(Role.BILLING, new_store_path(lease, '&collect="><b>x</b>'))
    marked = [tile for tile in tiles(body) if 'is-chosen"' in tile.split(">", 1)[0]]
    assert len(marked) == 1 and 'id="collect-draft" checked' in marked[0]
    assert "<b>x</b>" not in body


# --- the phone order -----------------------------------------------------------------------


def test_the_builder_reads_lines_then_how_to_collect_then_the_summary() -> None:
    body = get(Role.BILLING, new_store_path(a_world()))
    form = element(body, '<form method="post" id="draft-form"', "form")
    order = [
        form.index('id="draft-card"'),
        form.index('id="collect-card"'),
        form.index('<aside class="form-summary builder-side"'),
        form.index('id="collect-actions"'),
    ]
    assert order == sorted(order)
    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 768px) {", 1)[1].split("\n}\n", 1)[0]
    assert ".form-layout { grid-template-columns: minmax(0, 1fr);" in phone
    assert ".form-summary .form-actions { position: fixed;" in phone
    assert ".collect-tiles { grid-template-columns: minmax(0, 1fr); }" in phone


# --- the form family -----------------------------------------------------------------------


def headings(body: str) -> list[str]:
    form = body[body.index("<main") :]
    return [
        text(found)
        for found in re.findall(
            r'<(?:section|fieldset)\b[^>]*class="card form-card[^"]*"[^>]*>'
            r"\s*<(?:h2|legend)>(.*?)</",
            form,
            re.S,
        )
    ]


def summary_of(body: str) -> str:
    return element(body, '<aside class="form-summary"', "aside")


def test_each_form_page_has_its_cards_and_its_primary_in_the_summary() -> None:
    lease = a_world()
    assert lease.operator is not None
    charge = a_rent_charge(lease, date(2026, 10, 1), due="2000.00")
    one_off = a_one_off_charge(lease.operator, "250.00", date(2026, 10, 20), lease=lease)
    pages = [
        # PLAN-012f-b Batch B: New / Edit store and operator are dialogs, their Save in the
        # dialog's foot (tests/test_form_modals.py).
        (
            reverse("ledger:leases:create"),
            ["Lease", "Debits", "Setup checklist", "Documents"],
            "save-lease",
        ),
        (reverse("ledger:leases:edit", kwargs={"pk": lease.pk}), ["Lease", "Debits"], "save-lease"),
        (
            reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}),
            ["Payment", "Allocation"],
            "save-payment",
        ),
        # PLAN-012f-b C-2: Add payment's "How is this being paid?" card (the Note card has
        # its field's label, no heading).
        (
            reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk}),
            ["How is this being paid?"],
            "save-payment",
        ),
        (
            reverse("ledger:one_off_charges:payment", kwargs={"pk": one_off.pk}),
            ["How is this being paid?"],
            "save-payment",
        ),
        (reverse("ledger:calendar:add"), ["Entry"], "save-calendar-entry"),
    ]
    for path, expected, button in pages:
        body = get(Role.ADMIN, path)
        assert headings(body) == expected, (path, headings(body))
        summary = summary_of(body)
        assert summary.count("btn-primary") == 1, path
        assert f'id="{button}"' in summary and body.count(f'id="{button}"') == 1, path
        main = body[body.index("<main") :]
        form = element(main, '<form method="post"', "form")
        assert summary in form, path  # the button submits its form


def test_the_lease_form_keeps_its_fields_in_lease_and_debits() -> None:
    body = get(Role.ADMIN, reverse("ledger:leases:create"))
    lease_card = element(body, '<section class="card form-card" id="lease-card">', "section")
    debits = element(body, '<section class="card form-card" id="debits-card">', "section")
    for name in ("debit_day", "second_debit_day", "payment_channel"):
        assert f'name="{name}"' in debits and f'name="{name}"' not in lease_card, name
    for name in ("store", "operator", "started_on", "collection_status", "rent"):
        assert f'name="{name}"' in lease_card and f'name="{name}"' not in debits, name
    assert 'id="twice-monthly-field"' in debits
    # The setup checklist in the Lines style; its total in the summary card.
    assert 'id="setup-checklist"' in element(
        body, '<section class="card form-card lines-card" id="setup-card">', "section"
    )
    summary = summary_of(body)
    assert 'id="setup-total"' in summary and 'data-total-for="setup-checklist"' in summary


def test_the_payment_form_has_the_allocation_in_lines_style_and_the_unallocated_line_aside() -> (
    None
):
    lease = a_world()
    a_rent_charge(lease, date(2026, 10, 1), due="2000.00")
    body = get(Role.BILLING, reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}))
    allocation = element(
        body, '<section class="card form-card lines-card" id="allocation-card">', "section"
    )
    assert 'id="allocation-lines"' in allocation and 'id="keep-credit-field"' in allocation
    summary = summary_of(body)
    assert 'id="unallocated-line"' in summary and 'id="unallocated"' in summary
    head = header(body)
    assert 'id="known-fields"' in head and "Store 1991 Istanbul" in head


def test_the_context_line_is_in_the_header_where_the_form_has_context() -> None:
    lease = a_world()
    query = f"?store={lease.store_id}&lease={lease.pk}"
    path = reverse("ledger:leases:edit", kwargs={"pk": lease.pk}) + query
    body = get(Role.ADMIN, path)
    head = header(body)
    assert "Store 1991 Istanbul · Lease from Jan 13, 2026 · Operator Fatih Co" in head, path
    assert body.count('id="known-fields"') == 1, path
    # PLAN-012f-b C-2: Add payment's header carries its own muted line (charge · store ·
    # operator · due), opened from the store page or not.
    charge = a_rent_charge(lease, date(2026, 10, 1))
    path = reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk}) + query
    head = header(get(Role.ADMIN, path))
    assert "Rent Oct 2026, debit 1 · #1991 Istanbul · Fatih Co · due " in head, path


# --- the line_total filter -----------------------------------------------------------------


def a_row(**values: str) -> InvoiceLineForm:
    """One bound row of the builder's formset."""
    data = {
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        **{f"lines-0-{name}": value for name, value in values.items()},
    }
    form: InvoiceLineForm = InvoiceLineFormSet(data, prefix="lines").forms[0]
    return form


@pytest.mark.parametrize(
    ("quantity", "unit", "expected"),
    [
        ("", "25.00", "25.00"),
        ("3", "25.00", "75.00"),
        ("0.5", "0.01", "0.01"),  # half a cent rounds half up
        ("2", "abc", "0.00"),
        ("-1", "10.00", "0.00"),
        ("2", "", "0.00"),
        ("NaN", "10.00", "0.00"),
        ("1e400", "10.00", "0.00"),
    ],
)
def test_the_line_total_is_the_quantity_times_the_unit_amount(
    quantity: str, unit: str, expected: str
) -> None:
    assert line_total(a_row(item="Sign", quantity=quantity, unit_amount=unit)) == D(expected)


def test_the_filter_renders_through_money_and_a_removed_row_shows_no_amount() -> None:
    rendered = Template("{% load builder money %}{{ row|line_total|money }}").render(
        Context({"row": a_row(item="Sign", quantity="2", unit_amount="1234.5")})
    )
    assert rendered == "$2,469.00"
    lease = a_world()
    path = new_store_path(lease)
    client = client_for(Role.BILLING)[0]
    rows = [
        {"item": "Removed", "unit_amount": "500.00", "DELETE": "on"},
        {"item": "Sign repair", "unit_amount": "-1"},  # refused: the form is held
    ]
    data = {
        "lines-TOTAL_FORMS": str(len(rows)),
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        "lease": str(lease.pk),
        "collect": "draft",
        **{f"lines-{i}-{k}": v for i, row in enumerate(rows) for k, v in row.items()},
    }
    client.post(path, data)
    body = client.get(path).content.decode()
    hidden = element(body, '<tr class="draft-line" hidden>', "tr")
    assert re.search(r'<td class="money line-total"[^>]*></td>', hidden), hidden
    assert "$500.00" not in element(body, '<ul class="summary-lines"', "ul")


def test_the_views_total_is_the_sum_of_the_filters_line_totals() -> None:
    """The service's rule is the one source: the view's total under the lines equals the sum
    of the filter's per-row totals, half cents and an unparsable row included."""
    rows = [
        {"item": "A", "quantity": "0.5", "unit_amount": "0.01"},
        {"item": "B", "quantity": "3", "unit_amount": "33.335"},
        {"item": "C", "quantity": "", "unit_amount": "1234.5"},
        {"item": "D", "quantity": "2", "unit_amount": "abc"},
    ]
    data = {
        "lines-TOTAL_FORMS": str(len(rows)),
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        **{f"lines-{i}-{k}": v for i, row in enumerate(rows) for k, v in row.items()},
    }
    formset = InvoiceLineFormSet(data, prefix="lines")
    per_line = [line_total(form) for form in formset.forms]
    assert per_line == [D("0.01"), D("100.01"), D("1234.50"), D("0.00")]
    assert _lines_total(formset) == sum(per_line) == D("1334.52")
    assert per_line[1] == invoice_builder.shown_line_total("3", "33.335")
