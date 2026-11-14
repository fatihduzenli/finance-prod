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
    operator = new_lease.an_operator_with(
        f"Page Person {number}", email=f"page{number}@example.com"
    )
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
        ".movein-row { display: grid; grid-template-columns: minmax(0, 1fr) 6.5rem 11.5rem 6.5rem "
        "auto;"
    ) in css


def test_the_script_has_no_mirror_and_exposes_the_preview() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "deposit-mirror" not in script
    assert "window.ledgerNewLease = { paymentsPreview: paymentsPreview };" in script


# --- Task 3b (§12, §11 A): the successor's line and the entry's ?lease= -----------------------


def with_successor(admin: Client) -> tuple[Lease, Lease]:
    """A store with a current lease (since 2025) and a prospective successor (Nov 15, 2026)
    of Page Person 437, saved through New lease: (current, successor)."""
    successor = save(admin)
    current = a_lease(successor.store, started=date(2025, 1, 1))
    return current, successor


def store_page(client: Client, lease: Lease) -> str:
    response = client.get(reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}))
    assert response.status_code == 200
    return response.content.decode()


def entry(client: Client, lease: Lease, requested: str | None = None) -> Any:
    path = route("record_payment", lease)
    return client.get(path + (f"?lease={requested}" if requested is not None else ""))


def test_the_store_page_names_the_successor_and_links_its_payment_entry(
    admin: Client, today: date
) -> None:
    current, successor = with_successor(admin)
    client, _user = billing()
    text = store_page(client, current)
    link = f"{route('record_payment', current)}?lease={successor.pk}"
    assert (
        '<p class="card-note" id="header-successor">Upcoming lease: Page Person 437, starts '
        f'Nov 15, 2026 · <a class="card-link" id="successor-payment" href="{link}">'
        "Record a payment</a></p>"
    ) in text
    # The header keeps the current lease.
    assert workspace.store_workspace(current.store, TODAY, Role.BILLING).header.lease == current
    # A viewer sees the line without the link.
    viewer, _user = client_for(Role.VIEWER, "viewer-successor")
    seen = store_page(viewer, current)
    assert (
        '<p class="card-note" id="header-successor">Upcoming lease: Page Person 437, starts '
        "Nov 15, 2026</p>"
    ) in seen
    assert "successor-payment" not in seen


def test_no_successor_line_without_a_successor_or_its_operator(admin: Client, today: date) -> None:
    alone = a_lease(vacant_store(500, "Alone"), started=date(2025, 1, 1))
    assert 'id="header-successor"' not in store_page(admin, alone)
    current, successor = with_successor(admin)
    Lease.objects.filter(pk=successor.pk).update(operator=None)
    assert 'id="header-successor"' not in store_page(admin, current)
    # A store whose only lease is prospective shows no such line (its Status row names it).
    only = save(admin, 501)
    assert 'id="header-successor"' not in store_page(admin, only)


def test_the_entry_with_the_successor_lists_its_open_charges(admin: Client, today: date) -> None:
    current, successor = with_successor(admin)
    client, _user = billing()
    response = entry(client, current, str(successor.pk))
    assert response.status_code == 200
    assert response.context["lease"] == successor
    listed = {row["charge"].charge_id for row in response.context["rows"]}
    assert listed == {item.pk for item in setup_charges(successor)}
    assert "Lease from Nov 15, 2026 · Operator Page Person 437" in response.content.decode()


@pytest.mark.parametrize("which", ["other-store", "ended", "current", "malformed", "empty"])
def test_the_entry_ignores_any_other_lease_parameter(
    admin: Client, today: date, which: str
) -> None:
    current, _successor = with_successor(admin)
    other = save(admin, 502)
    ended = a_lease(current.store, started=date(2024, 1, 1), status="ended")
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2024, 12, 31))
    requested = {
        "other-store": str(other.pk),
        "ended": str(ended.pk),
        "current": str(current.pk),
        "malformed": "not-a-uuid'<script>",
        "empty": "",
    }[which]
    client, _user = billing()
    response = entry(client, current, requested)
    assert response.status_code == 200
    assert response.context["lease"] == current


def test_a_payment_recorded_for_the_successor_lands_on_its_charge_only(
    admin: Client, today: date
) -> None:
    current, successor = with_successor(admin)
    fee = OneOffCharge.objects.get(lease=successor, fee_type__code="sign_fee")
    before = {
        item.pk: (item.amount_paid, item.status) for item in OneOffCharge.objects.exclude(pk=fee.pk)
    }
    client, _user = billing()
    path = f"{route('record_payment', current)}?lease={successor.pk}"
    response = client.post(
        path,
        {
            "creation_key": str(uuid.uuid4()),
            "method": "wire",
            "amount": "500.00",
            "paid_on": TODAY.isoformat(),
            "reference": "W-2",
            "note": "Wire for the sign fee.",
            f"alloc_{fee.pk}": "500.00",
        },
    )
    assert response.status_code == 302
    fee.refresh_from_db()
    assert (fee.amount_paid, fee.balance) == (D("500.00"), D("0.00"))
    after = {
        item.pk: (item.amount_paid, item.status) for item in OneOffCharge.objects.exclude(pk=fee.pk)
    }
    assert after == before


def test_a_store_whose_only_lease_is_prospective_offers_add_payment_to_billing(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    keys = [
        action.key for action in workspace.store_workspace(lease.store, TODAY, Role.BILLING).actions
    ]
    assert "record_payment" in keys
    viewer = workspace.store_workspace(lease.store, TODAY, Role.VIEWER).actions
    assert viewer == []
    client, _user = billing()
    assert 'id="record-payment"' in store_page(client, lease)
    # A vacant store with no lease at all offers none.
    empty = vacant_store(503, "Empty")
    keys = [a.key for a in workspace.store_workspace(empty, TODAY, Role.ADMIN).actions]
    assert "record_payment" not in keys


# --- Task 4: the charges list's words ---------------------------------------------------------


def charges_page(client: Client, lease: Lease) -> str:
    response = client.get(
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}) + "?tab=charges&filter=all"
    )
    assert response.status_code == 200
    return response.content.decode()


def test_the_charges_list_shows_each_setup_charges_words(admin: Client, today: date) -> None:
    timing: dict[str, Any] = {
        f"{FEE}_when": "on_date",
        f"{FEE}_on": "2026-12-15",
        f"{DEP}_when": "installments",
        f"{DEP}_count": "3",
        f"{DEP}_first_due": "2026-12-01",
    }
    lease = save(admin, **timing)
    text = charges_page(admin, lease)
    assert '· <span class="charge-timing">due Dec 15, 2026</span>' in text
    assert '· <span class="charge-timing">0 of 3 installments paid</span>' in text
    rows = workspace.charges_tab(lease.store, "all", Role.BILLING, today=TODAY)["charge_rows"]
    words = {row.description: row.timing for row in rows if row.kind == "one_off"}
    fee = OneOffCharge.objects.get(lease=lease, fee_type__code="sign_fee")
    deposit = OneOffCharge.objects.get(lease=lease, fee_type__code="security_deposit")
    assert words[fee.label] == charge_timing.lease_words(fee) == "due Dec 15, 2026"
    assert words[deposit.label] == charge_timing.lease_words(deposit) == "0 of 3 installments paid"
    # The installments themselves carry no words.
    assert sum(1 for row in rows if row.kind == "one_off" and row.timing) == 2


def test_a_charge_on_the_first_invoice_shows_no_words(admin: Client, today: date) -> None:
    lease = save(admin)
    assert 'class="charge-timing"' not in charges_page(admin, lease)


def lease_with_planned_charges(admin: Client, number: int, count: int) -> Lease:
    """A New lease of ``count`` charges of their own fee types, each "In installments" (2
    from Dec 1, 2026)."""
    from ledger.models import FeeType

    fields: dict[str, Any] = {f"{DEP}_price": "", f"{FEE}_price": ""}
    for index in range(count):
        code = f"qc_{number}_{index}"
        FeeType.objects.create(
            code=code, name=f"Fee {index}", default_price=D("100"), show_in_setup=True
        )
        fields.update(
            {
                f"setup_{code}_price": "100.00",
                f"setup_{code}_when": "installments",
                f"setup_{code}_count": "2",
                f"setup_{code}_first_due": "2026-12-01",
            }
        )
    return save(admin, number, **fields)


@pytest.mark.parametrize("count", [1, 8])
def test_the_charges_list_reads_the_words_in_a_constant_number_of_queries(
    admin: Client, today: date, count: int
) -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    small = lease_with_planned_charges(admin, 600, 1)
    lease = lease_with_planned_charges(admin, 601, count)
    with CaptureQueriesContext(connection) as baseline:
        workspace.charges_tab(small.store, "all", Role.BILLING, today=TODAY)
    with CaptureQueriesContext(connection) as captured:
        found = workspace.charges_tab(lease.store, "all", Role.BILLING, today=TODAY)
    assert len(captured) == len(baseline)
    words = [row.timing for row in found["charge_rows"] if row.timing]
    assert words == ["0 of 2 installments paid"] * count
