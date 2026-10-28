"""PLAN-012f-a Batch C, slice C2: the wizard frame of End lease and Transfer (D8;
DESIGN-SPEC §4 Transfer wizard, transfer-wizard-laptop.png).

The header's "Step N of M · nothing changes until you confirm" on every step of both wizards;
the step card's list (done, current and later steps, and which are links), its context lines
and the cancel form with its note; the footer's Back target per step; the statement step's
tiles (prorated and full month, the deposit's collected day known or not, the deductions red
above zero) and its SETTLEMENT box (the signs, the green refund or the red owed row); the
deduction rows without script (all eight shown, the script's controls hidden) and the script's
hooks; the phone's markup order and rules.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from finance.dates import day_text
from ledger.models import LeaseEnd, LeaseTransfer, Payment
from ledger.services import stores as store_service
from tests.test_lease_end import (
    OCTOBER,
    TODAY,
    a_store_lease,
    deductions,
    deposit,
    draft,
    figures,
    october,
    saved,
    settle,
)
from tests.test_operator_views import client_for
from tests.test_plan012f_d_pages import APP_CSS, phone_block
from tests.test_transfer import a_clean_lease, begin
from tests.test_transfer import ready as a_ready_transfer

pytestmark = pytest.mark.django_db

PROGRESS = "nothing changes until you confirm"
LEASE_END_STEPS = ("terms", "blockers", "statement", "settlement", "confirm")


@pytest.fixture(autouse=True)
def pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)


@pytest.fixture
def admin() -> tuple[Client, User]:
    return client_for(Role.ADMIN)


def text_of(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def header_of(html: str) -> str:
    start = html.index('id="page-header"')
    return html[start : html.index("</header>", start)]


def steps_of(html: str) -> list[tuple[str, str]]:
    block = html[
        html.index('id="wizard-steps"') : html.index("</ol>", html.index('id="wizard-steps"'))
    ]
    return re.findall(r"<li([^>]*)>(.*?)</li>", block, re.S)


def aside_of(html: str) -> str:
    start = html.index('<aside class="card wizard-steps-card"')
    return html[start : html.index("</aside>", start)]


def back_of(html: str) -> str:
    found = re.search(r'<a class="btn" href="([^"]+)" id="wizard-back">← Back</a>', html)
    assert found is not None, "no Back link"
    return found.group(1)


def a_ready_end() -> LeaseEnd:
    """A plain lease end with every step reachable: October paid, a deposit held, one
    deduction saved and the refund settled."""
    end = draft(a_clean_lease())
    assert deductions(end, ("cleaning", "Floors", "150.00")).ok
    assert settle(end).ok
    return saved(end)


def lease_end_paths(end: LeaseEnd) -> list[str]:
    return [reverse(f"ledger:lease_ends:{name}", kwargs={"pk": end.pk}) for name in LEASE_END_STEPS]


def transfer_paths(row: LeaseTransfer) -> list[str]:
    end = row.lease_end
    assert end is not None
    return [
        reverse("ledger:transfers:operator", kwargs={"pk": row.pk}),
        reverse("ledger:lease_ends:terms", kwargs={"pk": end.pk}),
        reverse("ledger:transfers:blockers", kwargs={"pk": row.pk}),
        reverse("ledger:lease_ends:statement", kwargs={"pk": end.pk}),
        reverse("ledger:lease_ends:settlement", kwargs={"pk": end.pk}),
        reverse("ledger:transfers:incoming", kwargs={"pk": row.pk}),
        reverse("ledger:transfers:confirm", kwargs={"pk": row.pk}),
    ]


def page(client: Client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.content.decode()


# --- The header --------------------------------------------------------------------------


def test_every_lease_end_step_counts_itself_in_the_header(admin: Any) -> None:
    client, _ = admin
    end = a_ready_end()
    store = end.lease.store
    for number, path in enumerate(lease_end_paths(end), 1):
        header = header_of(page(client, path))
        assert f"Step {number} of 5 · {PROGRESS}" in header, path
        assert f"<h1>End lease {store_service.title(store)}</h1>" in header, path
        assert 'aria-current="page">End lease</span>' in header


def test_every_transfer_step_counts_itself_in_the_header(admin: Any) -> None:
    client, user = admin
    row = a_ready_transfer(user)
    title = store_service.title(row.store)
    for number, path in enumerate(transfer_paths(row), 1):
        header = header_of(page(client, path))
        assert f"Step {number} of 7 · {PROGRESS}" in header, path
        assert f"<h1>Transfer {title} to another operator</h1>" in header, path
        assert 'aria-current="page">Transfer</span>' in header


# --- The step card -----------------------------------------------------------------------


def test_the_step_list_marks_done_current_and_later_steps(admin: Any) -> None:
    client, _ = admin
    end = a_ready_end()
    paths = lease_end_paths(end)
    items = steps_of(page(client, paths[2]))  # the statement step
    assert len(items) == 5
    for index, (attributes, inner) in enumerate(items):
        if index < 2:
            assert 'class="step-done"' in attributes and "(done)" in inner
            assert f'<a href="{paths[index]}">' in inner
        elif index == 2:
            assert 'aria-current="step"' in attributes and "<strong>" in inner
            assert "<a " not in inner and "step-done" not in attributes
        else:
            assert "step-done" not in attributes and "aria-current" not in attributes
            assert f'<a href="{paths[index]}">' in inner  # the terms are set: they open


def test_a_later_step_that_cannot_open_is_not_a_link(admin: Any) -> None:
    client, _ = admin
    row = begin(a_clean_lease())  # the incoming lease is not set: the confirm cannot open
    paths = transfer_paths(row)
    items = steps_of(page(client, paths[3]))
    assert len(items) == 7
    incoming, confirm = items[5][1], items[6][1]
    assert f'<a href="{paths[5]}">' in incoming
    assert "<a " not in confirm and "<span>Confirm</span>" in confirm


def test_the_step_card_holds_the_context_lines_and_the_cancel(admin: Any) -> None:
    client, user = admin
    row = a_ready_transfer(user)
    end = row.lease_end
    assert end is not None and end.ended_on is not None
    aside = aside_of(page(client, transfer_paths(row)[3]))
    lines = text_of(aside[aside.index('id="wizard-context"') :])
    outgoing = row.outgoing_lease.operator
    assert outgoing is not None and row.incoming_operator is not None
    assert f"{outgoing.display_name} → to {row.incoming_operator.display_name}" in lines
    assert "Lease ends Oct 19, 2026" in lines
    cancel = re.search(r'<form[^>]*id="transfer-cancel"[^>]*>(.*?)</form>', aside, re.S)
    assert cancel is not None
    assert 'method="post"' in cancel.group(0) and "js-single-submit" in cancel.group(0)
    assert reverse("ledger:transfers:cancel", kwargs={"pk": row.pk}) in cancel.group(0)
    assert "csrfmiddlewaretoken" in cancel.group(1)
    assert '<button class="link-action danger" type="submit">Cancel the transfer</button>' in (
        cancel.group(1)
    )
    assert "Deletes this transfer and its lease end draft; nothing else changes." in cancel.group(1)
    # A plain lease end: the lease line and its own cancel.
    plain = a_ready_end()
    aside = aside_of(page(client, lease_end_paths(plain)[0]))
    assert "Lease of <strong>" in aside and "Lease ends Oct 19, 2026" in text_of(aside)
    cancel = re.search(r'<form[^>]*id="lease-end-cancel"[^>]*>(.*?)</form>', aside, re.S)
    assert cancel is not None and "Cancel the lease end</button>" in cancel.group(1)
    assert "Deletes this draft; nothing else changes." in cancel.group(1)


# --- The footer --------------------------------------------------------------------------


def test_back_leads_to_the_step_before_or_the_store(admin: Any) -> None:
    client, user = admin
    end = a_ready_end()
    paths = lease_end_paths(end)
    store = reverse("ledger:stores:detail", kwargs={"pk": end.lease.store_id})
    for index, path in enumerate(paths):
        assert back_of(page(client, path)) == (paths[index - 1] if index else store), path
    row = a_ready_transfer(user)
    paths = transfer_paths(row)
    store = reverse("ledger:stores:detail", kwargs={"pk": row.store_id})
    for index, path in enumerate(paths):
        assert back_of(page(client, path)) == (paths[index - 1] if index else store), path


def test_the_primary_submits_its_steps_form_from_the_footer(admin: Any) -> None:
    client, _ = admin
    end = a_ready_end()
    paths = lease_end_paths(end)
    footers = {
        0: '<button class="btn btn-primary" type="submit" form="lease-end-terms">',
        2: '<button class="btn btn-primary" type="submit" form="lease-end-deductions">',
        3: '<button class="btn btn-primary" type="submit" form="lease-end-settlement">',
        4: 'form="lease-end-confirm" id="confirm-lease-end">End the lease</button>',
    }
    for index, button in footers.items():
        html = page(client, paths[index])
        footer = html[html.index('id="wizard-footer"') :].split("</div>", 1)[0]
        assert button in footer, paths[index]
    blockers = page(client, paths[1])
    assert 'id="continue">Continue →</a>' in blockers.split('id="wizard-footer"', 1)[1]


# --- The statement tiles -----------------------------------------------------------------


def tiles_of(html: str) -> list[str]:
    block = html[html.index('id="statement-tiles"') :]
    return re.findall(r'<div class="kpi">(.*?)</div>', block, re.S)[:3]


def test_the_tiles_read_a_prorated_month_a_collected_deposit_and_one_deduction(admin: Any) -> None:
    client, _ = admin
    end = a_ready_end()  # ended Oct 19, prorated; the deposit paid
    found = figures(end)
    html = page(client, lease_end_paths(end)[2])
    opened, held, deducted = tiles_of(html)
    assert f"${found.open_total:,.2f}" in opened
    assert '<span class="kpi-note">rent 1–19 Oct, 19 of 31 days</span>' in opened
    assert f"${found.deposit_held:,.2f}" in held and found.deposit_held > 0
    collected = (
        Payment.objects.filter(one_off_charge_id=found.deposits[0].charge_id, is_refund=False)
        .get()
        .settled_on
    )
    assert collected is not None
    assert f'<span class="kpi-note">collected {day_text(collected)}</span>' in held
    # PLAN-013a C7: the value is also table_total.js's output (it follows the typed rows).
    assert (
        '<span class="kpi-value tone-unpaid js-total-out" data-total-for="deduction-rows" '
        'data-tone-above-zero="tone-unpaid">$150.00</span>'
    ) in deducted
    assert '<span class="kpi-note">1 line</span>' in deducted


def test_the_tiles_of_a_full_month_without_a_deposit_or_deduction(admin: Any) -> None:
    client, _ = admin
    lease = a_store_lease()
    october(lease, paid="2000.00")
    end = draft(lease, prorate=False)
    html = page(client, lease_end_paths(end)[2])
    opened, held, deducted = tiles_of(html)
    assert "kpi-note" not in opened and "kpi-note" not in held
    assert '<span class="kpi-value">$0.00</span>' in held
    assert '<span class="kpi-value js-total-out" data-total-for="deduction-rows" ' in deducted
    assert 'data-tone-above-zero="tone-unpaid">$0.00</span>' in deducted
    assert "kpi-value tone-unpaid" not in deducted
    assert '<span class="kpi-note">0 lines</span>' in deducted


# --- The SETTLEMENT box ------------------------------------------------------------------


def box_of(html: str) -> str:
    return html[
        html.index('id="settlement-box"') : html.index("</dl>", html.index('id="settlement-box"'))
    ]


def test_the_settlement_box_ends_in_the_green_refund_row(admin: Any) -> None:
    client, _ = admin
    end = a_ready_end()
    found = figures(end)
    assert found.refund_due > 0 and found.amount_owed == 0
    html = page(client, lease_end_paths(end)[2])
    box = box_of(html)
    assert f'<dd id="settle-held">${found.deposit_held:,.2f}</dd>' in box
    assert f'<dd id="settle-applied">− ${found.offset_applied:,.2f}</dd>' in box
    kept = found.withheld - found.offset_applied
    assert f'<dd id="settle-kept">− ${kept:,.2f}</dd>' in box and kept == 150
    paid_back = found.credit + found.lease_credit
    assert (f'<dd id="settle-credit">+ ${paid_back:,.2f}</dd>' in box) is (paid_back > 0)
    refund = re.search(r'<div class="([^"]*)" id="settle-refund">(.*?)</div>', box, re.S)
    assert refund is not None and "settle-refund" in refund.group(1)
    assert f"Refund due to {found.operator}" in refund.group(2)
    assert f"${found.refund_due:,.2f}" in refund.group(2)
    assert 'id="settle-owed"' not in box
    assert "nothing goes through" in html.split('id="settlement-help"', 1)[1][:200]


def test_the_settlement_box_ends_in_the_red_owed_row(admin: Any) -> None:
    client, _ = admin
    lease = a_store_lease(started_on=OCTOBER)
    october(lease)  # nothing paid, no deposit: the operator owes
    end = draft(lease)
    found = figures(end)
    assert found.amount_owed > 0 and found.refund_due == 0
    box = box_of(page(client, lease_end_paths(end)[2]))
    owed = re.search(r'<div class="([^"]*)" id="settle-owed">(.*?)</div>', box, re.S)
    assert owed is not None and "settle-owed" in owed.group(1)
    assert f"Still owed by {found.operator}" in owed.group(2)
    assert f"${found.amount_owed:,.2f}" in owed.group(2)
    assert 'id="settle-refund"' not in box


def test_a_refund_while_a_balance_stays_owed_shows_both_final_rows(admin: Any) -> None:
    client, _ = admin
    lease = a_store_lease()  # months before October missing: owed
    october(lease)
    deposit(lease, "500.00")  # held, and no deduction saved: refunded
    end = draft(lease)
    found = figures(end)
    assert found.amount_owed > 0 and found.refund_due > 0
    box = box_of(page(client, lease_end_paths(end)[2]))
    assert 'id="settle-refund"' in box and 'id="settle-owed"' in box
    assert box.index('id="settle-refund"') < box.index('id="settle-owed"')


# --- The deduction rows ------------------------------------------------------------------


def test_without_script_every_deduction_row_shows_and_the_scripts_controls_are_hidden(
    admin: Any,
) -> None:
    client, _ = admin
    end = a_ready_end()
    html = page(client, lease_end_paths(end)[2])
    table = html[
        html.index('id="deduction-rows"') : html.index(
            "</table>", html.index('id="deduction-rows"')
        )
    ]
    rows = re.findall(r"<tr class=\"deduction-row\"[^>]*>(.*?)</tr>", table, re.S)
    assert len(rows) == 8
    assert not re.search(r'<tr class="deduction-row"[^>]*hidden', table)
    assert "<th>Reason</th><th>Note</th>" in table and '<th class="money">Amount</th>' in table
    for number, row in enumerate(rows, 1):
        assert f'aria-label="Clear deduction {number}"' in row
        assert re.search(r'<button class="btn btn-sm deduction-clear" type="button" hidden', row)
    assert 'value="150.00"' in rows[0] and 'value="Floors"' in rows[0]
    assert '<button class="btn add-deduction" type="button" id="add-deduction" hidden>' in html
    assert "+ Add deduction</button>" in html
    assert "ledger/deduction_rows.js" in html and "ledger/table_total.js" in html


def test_the_deduction_script_hides_the_empty_rows_after_the_last_filled_one() -> None:
    script = Path(__file__).resolve().parents[1] / "ledger/static/ledger/deduction_rows.js"
    text = script.read_text(encoding="utf-8")
    for hook in (
        '"deduction-rows"',
        '"add-deduction"',
        "tr.deduction-row",
        "button.deduction-clear",
    ):
        assert hook in text, hook
    assert "row.hidden = index > last" in text  # the first row always shows
    assert "clear.hidden = false" in text and 'new Event("input")' in text
    css = APP_CSS.read_text(encoding="utf-8")
    assert "#deduction-rows tr.deduction-row[hidden], .add-deduction[hidden]" in css


# --- The phone ---------------------------------------------------------------------------


def test_on_a_phone_the_step_line_comes_first_and_the_step_card_last(admin: Any) -> None:
    client, user = admin
    row = a_ready_transfer(user)
    html = page(client, transfer_paths(row)[3])
    content = html[html.index("<main") :]
    order = [
        content.index('id="wizard-step-line"'),
        content.index('<section class="card wizard-card"'),
        content.index('id="wizard-footer"'),
        content.index('<aside class="card wizard-steps-card"'),
    ]
    assert order == sorted(order)
    assert 'Step 4 of 7 <span class="steps-name">· Statement and deductions</span>' in content
    block = phone_block(APP_CSS.read_text(encoding="utf-8"))
    assert ".wizard { display: block; }" in block
    assert (
        ".wizard-footer { position: sticky; bottom: calc(90px + env(safe-area-inset-bottom));"
        in block
    )
    assert ".steps { display: none; }" in block
    laptop = APP_CSS.read_text(encoding="utf-8").split("@media", 1)[0]
    assert ".wizard { display: grid; grid-template-columns: 260px minmax(0, 1fr);" in laptop
