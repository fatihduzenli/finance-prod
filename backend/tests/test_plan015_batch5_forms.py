"""PLAN-015 Batch 5 (docs/qa/15/PLAN-015-batch-5-forms.md): the form and builder pages take the
New lease page's numbered step cards and its summary card (§3.1)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from payments.fake import FakeProvider
from tests.receivables_helpers import a_lease, a_rent_charge, a_store
from tests.test_add_payment_page import a_plain_operator, invoiced, page_of
from tests.test_operator_views import client_for
from tests.test_plan012fa_c_builder import a_draft, a_world, element, new_store_path, text
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_split_page import a_one_off, an_operator, billing, fake, fixed_now  # noqa: F401
from tests.test_split_page import page as split_page

pytestmark = pytest.mark.django_db

TEMPLATES = Path(__file__).resolve().parents[1] / "ledger" / "templates" / "ledger"
HOW_CARD = '<fieldset class="card form-card collect-card how-card step-card"'

STEP = re.compile(
    r'<div class="step-head"><span class="step-number" aria-hidden="true">(\d)</span>'
    r'<div class="step-title"><(h2|legend) id="([a-z-]+)">([^<]+)</\2>'
    r'(?:<p class="muted step-note"[^>]*>(.*?)</p>)?</div></div>',
    re.S,
)


def get(role: Role, path: str) -> str:
    response = client_for(role)[0].get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


def steps(body: str) -> list[tuple[str, str, str, str]]:
    """(number, tag, title, note text) of each step head of the page's main column."""
    main = body[body.index("<main") :]
    return [
        (number, tag, title, text(note)) for number, tag, _id, title, note in STEP.findall(main)
    ]


def labelled_by_its_title(body: str) -> None:
    """Each step head's title id names its card (or fieldset) through aria-labelledby."""
    main = body[body.index("<main") :]
    for _number, _tag, heading_id, _title, _note in STEP.findall(main):
        assert main.count(f'aria-labelledby="{heading_id}"') == 1, heading_id
        assert main.index(f'aria-labelledby="{heading_id}"') < main.index(f'id="{heading_id}"')


def summary_card(body: str) -> str:
    aside = element(body, '<aside class="form-summary', "aside")
    return element(aside, '<section class="card summary-card"', "section")


def summary_shape(card: str, primary: str, cancel: str, after: str) -> None:
    """The New lease summary's shape: the label first, the primary, Cancel under it, then the
    muted line last."""
    assert card.count('<h2 class="summary-label">Summary</h2>') == 1
    assert card.index('class="summary-label"') < card.index('class="form-actions"')
    assert card.index(f'id="{primary}"') < card.index('<p class="summary-cancel">')
    row = element(card, '<p class="summary-cancel">', "p")
    assert f'id="{cancel}"' in row and '<a class="btn btn-quiet"' in row
    assert card.index('<p class="summary-cancel">') < card.index(after)
    assert "section-label" not in card and "summary-discard" not in card


# --- the builder ---------------------------------------------------------------------------


def test_the_builder_opens_lines_and_how_to_collect_as_numbered_steps() -> None:
    body = get(Role.BILLING, new_store_path(a_world()))
    found = steps(body)
    assert [(number, tag, title) for number, tag, title, _ in found] == [
        ("1", "h2", "Lines"),
        ("2", "legend", "How to collect"),
    ]
    assert found[0][3].startswith("Item: Rent, an open charge of the lease")
    assert found[0][3].endswith("Learn more: The lines of an invoice")
    assert found[1][3] == (
        "One choice; the invoice and its charges are written the same way whichever you pick."
    )
    labelled_by_its_title(body)
    # The legend is the How to collect fieldset's, named by it.
    fieldset = element(body, '<fieldset class="collect-choices" id="collect"', "fieldset")
    assert 'aria-labelledby="collect-title"' in fieldset.split(">", 1)[0]
    assert '<legend id="collect-title">How to collect</legend>' in fieldset
    # One line of help per card: the old help lines moved into the notes.
    lines = element(body, '<section class="card lines-card step-card"', "section")
    assert lines.count("step-note") == 1 and 'class="help"' not in lines


def test_the_builder_summary_has_the_new_lease_shape_for_a_new_and_a_saved_draft() -> None:
    lease = a_world()
    card = summary_card(get(Role.BILLING, new_store_path(lease)))
    summary_shape(card, "collect-actions", "builder-discard", 'id="builder-help"')
    assert "builder-help" in card and 'class="card builder-help"' not in card
    draft = a_draft(lease)
    body = get(Role.BILLING, reverse("ledger:invoices:draft", kwargs={"pk": draft.pk}))
    summary_shape(summary_card(body), "collect-actions", "draft-cancel", 'id="builder-help"')
    assert 'class="card builder-help"' not in body


def test_a_builder_draft_with_no_line_keeps_the_empty_summary_and_the_zero_total() -> None:
    body = get(Role.BILLING, new_store_path(a_world()))
    card = summary_card(body)
    assert '<ul class="summary-lines" id="summary-lines"></ul>' in card
    assert '<span id="lines-total-value">$0.00</span>' in card


def test_no_provider_literal_in_the_changed_templates() -> None:
    for name in (
        "invoices/_draft_form.html",
        "invoices/_collect.html",
        "invoices/_summary.html",
        "payments/_how.html",
        "payments/_add_payment_summary.html",
        "payments/add_payment.html",
        "stores/record_payment.html",
        "installments/split.html",
    ):
        assert "Stax" not in (TEMPLATES / name).read_text(encoding="utf-8"), name


# --- Add payment ---------------------------------------------------------------------------


def test_add_payment_opens_how_and_note_as_numbered_steps(fake: FakeProvider) -> None:  # noqa: F811
    body = get(Role.ADMIN, page_of(invoiced(fake)))
    assert steps(body) == [
        (
            "1",
            "legend",
            "How is this being paid?",
            "Pick one. Nothing is charged until you press the button at the right.",
        ),
        ("2", "h2", "Note", "What was received or why it is being charged now."),
    ]
    labelled_by_its_title(body)
    how = element(body, HOW_CARD, "fieldset")
    assert '<legend id="how-title">How is this being paid?</legend>' in how
    assert 'class="help"' not in how.split('<div class="collect-tiles', 1)[0]


def test_add_payment_summary_rows_and_shape(fake: FakeProvider) -> None:  # noqa: F811
    card = summary_card(get(Role.ADMIN, page_of(invoiced(fake, "75.00"))))
    summary_shape(card, "save-payment", "cancel-payment", 'id="cancel-payment"')
    rows = re.findall(r"<dt>([^<]+)</dt><dd([^>]*)>([^<]*)</dd>", card)
    assert rows[:3] == [
        ("Charge", "", "$75.00"),
        ("Paid", "", "$0.00"),
        ("Balance due", ' class="tone-unpaid"', "$75.00"),
    ]
    assert [label for label, _, _ in rows[3:]] == ["This payment"]
    assert '<div class="lease-summary-total" id="tile-this-payment">' in card
    # The After this payment line stays under the rows, before the primary.
    assert card.index('id="tile-this-payment"') < card.index('id="after-payment"')
    assert card.index('id="after-payment"') < card.index('id="save-payment"')
    assert "kpi" not in card


def test_add_payment_with_no_bank_greys_the_debit_card_and_chooses_the_record_card() -> None:
    lease = a_lease(a_store("1991 Istanbul", 1991), a_plain_operator("Fatih Co"))
    charge = a_rent_charge(lease, date(2026, 10, 1), due="2000.00")
    body = get(Role.ADMIN, reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk}))
    how = element(body, HOW_CARD, "fieldset")
    assert '<div class="collect-tile is-unavailable" id="tile-debit">' in how
    assert 'id="how-debit" disabled' in how and 'id="how-record" checked' in how
    rows = re.findall(r"<dt>([^<]+)</dt><dd([^>]*)>([^<]*)</dd>", summary_card(body))
    assert rows[2] == ("Balance due", ' class="tone-unpaid"', "$2,000.00")
    assert rows[3] == ("This payment", "", "—")


# --- Add payment with allocation (the store's) ---------------------------------------------


def test_the_store_add_payment_has_payment_and_allocation_steps_and_the_summary_shape() -> None:
    lease = a_world()
    body = get(Role.BILLING, reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id}))
    found = steps(body)
    assert [(number, tag, title) for number, tag, title, _ in found] == [
        ("1", "h2", "Payment"),
        ("2", "h2", "Allocation"),
    ]
    assert found[0][3].startswith("A payment received by Zelle, wire, check")
    labelled_by_its_title(body)
    aside = element(body, '<aside class="form-summary"', "aside")
    assert aside.startswith('<aside class="form-summary" aria-label="Summary">')
    card = summary_card(body)
    summary_shape(card, "save-payment", "cancel-payment", 'id="unallocated-help"')
    assert (
        '<div class="lease-summary-total" id="unallocated-line"><dt>Unallocated</dt>'
        '<dd id="unallocated">$0.00</dd></div>'
    ) in card
    cancel = element(card, '<a class="btn btn-quiet" id="cancel-payment"', "a")
    assert f'href="/stores/{lease.store_id}/?tab=payments#tab-payments"' in cancel


# --- Split into installments ---------------------------------------------------------------


def test_the_split_page_numbers_plan_and_installments_and_has_the_summary_shape(
    fake: FakeProvider,  # noqa: F811
    billing: Client,  # noqa: F811
) -> None:
    parent = a_one_off(an_operator(fake))
    body = split_page(billing, parent, count="3", first_due="2026-11-20", cadence="monthly")
    assert [(number, tag, title) for number, tag, title, _ in steps(body)] == [
        ("1", "h2", "Plan"),
        ("2", "h2", "Installments"),
    ]
    labelled_by_its_title(body)
    card = summary_card(body)
    summary_shape(card, "split-confirm", "split-cancel", 'id="split-nsf"')
    assert "kpi" not in card and "summary-tiles" not in card


def test_no_kpi_tag_and_no_summary_tiles_remain_on_the_form_pages() -> None:
    for name in (
        "payments/_add_payment_summary.html",
        "installments/split.html",
        "invoices/_summary.html",
        "stores/record_payment.html",
    ):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "{% kpi " not in source and "summary-tiles" not in source, name
