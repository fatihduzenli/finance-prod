"""PLAN-015j tasks 3 to 5 and §12, §13: the New lease page's When per Move-in charges row, the
store page's line for a prospective successor, the lease's charges list words, and the two
MAJOR findings of Fable's review of the service half
(docs/qa/15/PLAN-015j-move-in-charge-timing.md §4, §12, §13).

Today is 2026-11-03 (the New lease page tests' ``today``); a lease saved here starts 2026-11-15
and is prospective, with a $3,000.00 deposit and a $500.00 sign fee unless a test says
otherwise."""

from __future__ import annotations

import html
import re
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import InvoiceDraft, Lease, OneOffCharge
from ledger.services import (
    charge_timing,
    invoice_builder,
    lease_end,
    leases,
    start_date,
    workspace,
)
from tests import test_new_lease_page as new_lease
from tests.receivables_helpers import a_lease
from tests.test_new_lease_page import create_data, create_path, page, post_create, vacant_store
from tests.test_operator_views import client_for
from tests.test_plan015j_today_probe import send_move_in

pytestmark = pytest.mark.django_db

D = Decimal
today = new_lease.today
admin = new_lease.admin
TODAY = new_lease.TODAY  # 2026-11-03
START = date(2026, 11, 15)
DEP = "setup_security_deposit"
FEE = "setup_sign_fee"
BACKEND = Path(__file__).resolve().parent.parent
APP_CSS = BACKEND / "finance" / "static" / "finance" / "app.css"
SCRIPT = BACKEND / "ledger" / "static" / "ledger" / "new_lease.js"
PHONE_OPEN = "@media (max-width: 719.98px) {"


def billing() -> tuple[Client, Any]:
    return client_for(Role.BILLING, f"billing-{uuid.uuid4().hex[:6]}")


def route(name: str, lease: Lease) -> str:
    return reverse(f"ledger:stores:{name}", kwargs={"pk": lease.store_id})


def save(client: Client, number: int = 437, **fields: str) -> Lease:
    """New lease of a $3,000.00 deposit and a $500.00 sign fee with ``fields``; saved."""
    store = vacant_store(number, f"Page Store {number}")
    operator = new_lease.an_operator_with(f"Page Person {number}", email="page@example.com")
    data = create_data(
        store, operator, **{f"{DEP}_price": "3000.00", f"{FEE}_price": "500", **fields}
    )
    post_create(client, store, data)
    return Lease.objects.get(store=store)


def setup_charges(lease: Lease) -> list[OneOffCharge]:
    return list(
        OneOffCharge.objects.filter(lease=lease, plan__isnull=True)
        .select_related("lease")
        .order_by("fee_type__code")
    )


# --- §13 MAJOR 1: Schedule a payment and Charge now read the current lease as on main --------


def test_schedule_a_payment_sends_a_store_with_only_a_prospective_lease_to_charges(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    assert lease_end.current_lease(lease.store, TODAY) is None
    client, _user = billing()
    response = client.get(route("schedule_payment", lease))
    assert response.status_code == 302
    assert response["Location"] == workspace.tab_url(lease.store_id, workspace.TAB_CHARGES)


def test_charge_now_opens_the_builder_without_a_prospective_lease(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    response = admin.get(route("charge_now_pick", lease))
    assert response.status_code == 302
    location = response["Location"]
    new_invoice = reverse("ledger:invoices:new_store", kwargs={"pk": lease.store_id})
    assert location.startswith(f"{new_invoice}?")
    assert f"store={lease.store_id}" in location
    assert "lease=" not in location
    assert location.endswith("collect=charge_now")


def test_schedule_and_charge_now_keep_the_current_lease(admin: Client, today: date) -> None:
    lease = save(admin)
    current = a_lease(lease.store, started=date(2025, 1, 1))
    client, _user = billing()
    schedule = client.get(route("schedule_payment", lease))
    assert f"lease={current.pk}" in schedule["Location"]
    charge_now = admin.get(route("charge_now_pick", lease))
    assert f"lease={current.pk}" in charge_now["Location"]


# --- §13 MAJOR 2: a charge riding an invoice has no "due" words, whatever its due date ------


def test_activate_now_leaves_the_first_invoice_charges_without_words(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    send_move_in(admin, lease)
    leases.activate(lease.pk, actor=billing()[1], today=TODAY)
    lease.refresh_from_db()
    assert lease.started_on == TODAY
    charges = setup_charges(lease)
    assert [item.due_date for item in charges] == [START, START]  # not the start any more
    assert [charge_timing.lease_words(item) for item in charges] == ["", ""]


def test_change_start_date_leaves_the_draft_charges_without_words(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    start_date.apply_change(
        lease.pk,
        date(2026, 11, 20),
        reason="Keys handed over later.",
        choice=None,
        move_deposit=False,
        actor=billing()[1],
        today=TODAY,
    )
    lease.refresh_from_db()
    assert lease.started_on == date(2026, 11, 20)
    charges = setup_charges(lease)
    draft = InvoiceDraft.objects.get(lease=lease)
    on_draft = {line.existing_charge_id for line in invoice_builder.lines_of(draft)}
    assert {item.pk for item in charges} <= on_draft
    assert [charge_timing.lease_words(item) for item in charges] == ["", ""]


def test_an_older_due_date_on_no_invoice_reads_due(admin: Client, today: date) -> None:
    """§9.2 as narrowed by §13: an owed setup charge on no invoice nor draft whose stored due
    date is not the start reads "due <date>"."""
    lease = save(admin)
    InvoiceDraft.objects.filter(lease=lease).delete()
    fee = OneOffCharge.objects.get(lease=lease, fee_type__code="sign_fee")
    OneOffCharge.objects.filter(pk=fee.pk).update(due_date=date(2026, 11, 20))
    fee.refresh_from_db()
    assert charge_timing.lease_words(fee) == "due Nov 20, 2026"
    deposit = OneOffCharge.objects.get(lease=lease, fee_type__code="security_deposit")
    assert charge_timing.lease_words(deposit) == ""  # due on the start: no words


# --- Task 3: the page --------------------------------------------------------------------------


def row_of(text: str, code: str) -> str:
    """The Move-in charges row ``setup-<code>`` of the page, through its When line."""
    start = text.index(f'<div class="movein-row" id="setup-{code}"')
    return text[start : text.index("</div>\n      </div>", start)]


def phone_block(css: str) -> str:
    """The body of app.css's one phone block (``@media (max-width: 719.98px)``)."""
    start = css.index(PHONE_OPEN) + len(PHONE_OPEN)
    depth, at = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(css[at], 0)
        at += 1
    return css[start : at - 1]


def test_each_row_has_name_price_when_usual_and_more_in_that_order(
    admin: Client, today: date
) -> None:
    text = page(admin, create_path(vacant_store()))
    for code, name in (("security_deposit", "Security deposit"), ("sign_fee", "Sign fee")):
        row = row_of(text, code)
        order = [
            row.index('class="movein-name"'),
            row.index('class="movein-price"'),
            row.index('class="movein-when-select"'),
            row.index('class="movein-usual'),
            row.index('class="movein-more"'),
            row.index(f'id="setup-{code}-when"'),
        ]
        assert order == sorted(order)
        select = re.search(r'<select name="setup_' + code + r'_when"[^>]*>', row)
        assert select is not None
        # No visible label; the accessible name; rendered enabled (the script disables it).
        assert f'aria-label="When: {name}"' in select.group(0)
        assert "disabled" not in select.group(0)
        assert f'for="id_setup_{code}_when"' not in row
        when_line = row[row.index(f'id="setup-{code}-when"') :]
        assert 'name="setup_' + code + '_when"' not in when_line


def test_the_sub_fields_show_for_their_choice_while_the_row_is_priced(
    admin: Client, today: date
) -> None:
    row = row_of(page(admin, create_path(vacant_store())), "sign_fee")
    priced = "id_setup_sign_fee_price"
    assert f'data-shown-when="id_setup_sign_fee_when=on_date&amp;{priced}"' in row
    for part in ("count", "first_due", "cadence"):
        found = re.search(r'<(?:input|select)[^>]*name="setup_sign_fee_' + part + '"[^>]*>', row)
        assert found is not None
        assert f'data-shown-when="id_setup_sign_fee_when=installments&amp;{priced}"' in (
            found.group(0)
        )
    # The payments sentence is always in the row, empty and hidden until it has words.
    assert (
        '<p class="muted movein-preview" aria-live="polite" hidden id="setup-sign_fee-preview"></p>'
    ) in row
    # §9.5: the hint under "Due on a date" on a non-deposit row only.
    hint = html.escape(charge_timing.NOT_ON_INVOICE)
    assert hint in row
    deposit = row_of(page(admin, create_path(vacant_store(438, "Other"))), "security_deposit")
    assert hint not in deposit


def test_the_summary_list_off_the_first_invoice_is_always_there(admin: Client, today: date) -> None:
    text = page(admin, create_path(vacant_store()))
    assert (
        '<ul class="lease-summary-timed" id="summary-timed" '
        'aria-label="Not on the first invoice" hidden></ul>'
    ) in text


def test_the_step_note_and_no_deposit_mirror(admin: Client, today: date) -> None:
    text = page(admin, create_path(vacant_store()))
    assert "One-time charges. Leave a price empty to skip it; choose when each is collected." in (
        text
    )
    assert "billed with the first invoice" not in text
    assert "deposit-mirror" not in text and "movein-mirror" not in text


def test_the_styles_the_select_and_its_phone_line() -> None:
    css = APP_CSS.read_text(encoding="utf-8")
    assert css.count(PHONE_OPEN) == 1
    block = phone_block(css)
    assert ".movein-when-select { grid-column: 1 / -1; }" in block
    assert ".movein-when .field { flex: 1 1 100%; }" in block
    outside = css.replace(block, "")
    assert "data-role" not in outside
    assert ".movein-when-select { grid-column: 1 / -1; }" not in outside
    # The old controls' styles are gone.
    for gone in ("movein-mirror", "deposit-when", "deposit-plan-row"):
        assert gone not in css
    # Laptop: name, price, When, usual, More in one row.
    assert (
        ".movein-row { display: grid; grid-template-columns: minmax(0, 1fr) 8.5rem 12.5rem 7rem "
        "auto;"
    ) in css


def test_the_script_has_no_mirror_and_exposes_the_preview() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "deposit-mirror" not in script
    assert "window.ledgerNewLease = { paymentsPreview: paymentsPreview };" in script
