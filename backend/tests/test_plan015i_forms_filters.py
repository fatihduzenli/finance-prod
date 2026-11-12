"""PLAN-015i (docs/qa/15/PLAN-015i-forms-filters-residuals.md): the single-card form pages open
with the unnumbered step head (§3.1)."""

from __future__ import annotations

import re

import pytest
from django.template.loader import render_to_string

from tests.test_plan012f_d_pages import World, client_of, path_of
from tests.test_plan012f_d_pages import world as world  # noqa: F401 (fixture)
from tests.test_plan012fa_c_builder import element, text

pytestmark = pytest.mark.django_db

STEP_HEAD = "ledger/leases/_step_head.html"

#: The twelve single-card form pages: the route, the card's title id, its title and the help
#: sentence moved into the head's note (``None``: the page's help stays where it was, or it
#: has none, and no note line is drawn).
FORMS: list[tuple[str, str, str, str | None]] = [
    ("ledger:stores:set_cost", "store-cost-card-title", "Landlord rent", None),
    (
        "ledger:stores:correct_cost",
        "cost-correct-card-title",
        "Landlord rent",
        "Saving writes a new row with these values and dates and keeps this one in the history, "
        "struck through, with your note. The dates may not overlap another current row of this "
        "store.",
    ),
    ("ledger:contacts:edit", "contact-edit-card-title", "Contact", None),
    (
        "ledger:contacts:correct",
        "contact-correct-card-title",
        "Correction",
        "The entry stays as it was recorded; the correction is listed under it with your name "
        "and the time. Never write card or account numbers.",
    ),
    ("ledger:contacts:outcome", "contact-outcome-card-title", "Outcome", None),
    ("ledger:leases:enrol", "enrol-card-title", "Autopay", None),
    ("ledger:charges:link_invoice", "link-invoice-card-title", "Invoice", None),
    ("ledger:one_off_charges:link", "link-invoice-card-title", "Invoice", None),
    ("ledger:plans:reschedule", "reschedule-card-title", "New dates", None),
    ("ledger:leases:resolve_deposit", "deposit-card-title", "Deposit", None),
    (
        "ledger:calendar:edit",
        "calendar-card-title",
        "Entry",
        "Calendar entries are shared with all staff and appear in every staff member's calendar "
        "feed. Never write card or account numbers.",
    ),
    ("ledger:one_off_charges:edit", "charge-card-title", "Charge", None),
]


def form_card(body: str, heading_id: str) -> str:
    opening = re.search(
        rf'<section class="card form-card step-card"(?: id="[a-z-]+")? '
        rf'aria-labelledby="{heading_id}">',
        body,
    )
    assert opening is not None, heading_id
    return element(body, opening.group(0), "section")


@pytest.mark.parametrize(("name", "heading_id", "title", "note"), FORMS)
def test_each_single_card_form_opens_with_the_unnumbered_step_head(
    world: World,  # noqa: F811
    name: str,
    heading_id: str,
    title: str,
    note: str | None,
) -> None:
    response = client_of(world, name, {}).get(path_of(world, name))
    assert response.status_code == 200, name
    body = response.content.decode()
    card = form_card(body, heading_id)
    first = card.split(">", 1)[1].lstrip()
    head = (
        f'<div class="step-head"><div class="step-title"><h2 id="{heading_id}">{title}</h2>'
        + (f'<p class="muted step-note">{note}</p>' if note else "")
        + "</div></div>"
    )
    assert first.startswith(head), (name, first[:300])
    # One heading per card, named by the section; no number, no empty note line.
    assert card.count("<h2") == 1 and body.count(f'id="{heading_id}"') == 1
    assert body.count(f'aria-labelledby="{heading_id}"') == 1
    assert "step-number" not in card and "step-action" not in card
    assert card.count("step-note") == (1 if note else 0)
    if note:
        # Moved, never duplicated.
        assert text(body).count(text(note)) == 1, name


def test_the_step_head_without_a_note_draws_no_muted_line() -> None:
    plain = render_to_string(STEP_HEAD, {"heading_id": "x-title", "title": "Title", "note": ""})
    assert plain == (
        '<div class="step-head"><div class="step-title"><h2 id="x-title">Title</h2></div></div>\n'
    )
    assert "step-note" not in render_to_string(STEP_HEAD, {"heading_id": "y", "title": "T"})


def test_the_step_head_with_a_note_is_unchanged() -> None:
    # Byte-identical to the head before PLAN-015i (New lease and Edit lease use it).
    numbered = render_to_string(
        STEP_HEAD,
        {"number": 2, "heading_id": "terms-title", "title": "Terms", "note": "Rent, start."},
    )
    assert numbered == (
        '<div class="step-head"><span class="step-number" aria-hidden="true">2</span>'
        '<div class="step-title"><h2 id="terms-title">Terms</h2>'
        '<p class="muted step-note">Rent, start.</p></div></div>\n'
    )
    escaped = render_to_string(
        STEP_HEAD, {"heading_id": "h", "title": "T", "note": "<b>x</b>", "action": ""}
    )
    assert '<p class="muted step-note">&lt;b&gt;x&lt;/b&gt;</p>' in escaped
