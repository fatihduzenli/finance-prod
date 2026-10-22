"""The invoice builder's lines (docs/qa/PLAN-012d-store-workspace.md D10; §6 C, the
implementer's cases; the adversarial ones are the QA owner's file).

One Item box per line on one list (Rent, the lease's existing open charges, the
catalogue items); the month select and the rent-amount endpoint; existing-charge lines
attached on send; removed lines ignored and errors numbered by the visible line; one line
on a new draft; the trash button; the width rule; the draft link of a refusal. Nothing
reaches a network.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.db import transaction
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionMethod,
    FeeType,
    Invoice,
    InvoiceDraft,
    InvoiceDraftLine,
    InvoiceLineItem,
    InvoiceStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
)
from ledger.services import invoice_builder, rent_run
from ledger.services import one_off_charges as one_off_service
from payments.fake import FakeProvider
from tests import test_invoice_builder as builder_tests
from tests.test_invoice_builder import (
    FIRST_MONTH,
    FIRST_TEXT,
    MEMO,
    NOVEMBER,
    OCTOBER,
    TODAY,
    a_draft,
    a_rent_lease,
    a_world,
    fresh,
    line,
    page_data,
    sent,
    user,
)
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_setup_charges import deposit_type, item

pytestmark = pytest.mark.django_db

D = Decimal
REPO = Path(__file__).resolve().parents[1]
# The builder file's fixtures, re-exported so pytest finds them here.
fixed_now = builder_tests.fixed_now
fake = builder_tests.fake
worker = builder_tests.worker


def html(client: Any, url: str) -> str:
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code)
    return str(response.content.decode())


def a_deposit(lease: Lease, price: str = "500.00") -> OneOffCharge:
    """The setup checklist's deposit on ``lease``: a setup charge, nothing at the
    provider (collection none)."""
    outcome = one_off_service.add_charge(
        lease.pk, item(deposit_type(), price, due_date=TODAY), actor=None
    )
    assert outcome.ok and outcome.charge is not None, outcome.message
    return outcome.charge


def existing(charge: OneOffCharge, unit: str | None = None) -> invoice_builder.LineInput:
    return invoice_builder.LineInput(
        "Existing open charge",
        "",
        None,
        D(unit) if unit is not None else None,
        existing_charge=charge.pk,
    )


def rent_amount(client: Any, lease: Lease, month: str, **params: str) -> Any:
    query = "&".join(f"{name}={value}" for name, value in {"lease": lease.pk, **params}.items())
    return client.get(f"{reverse('ledger:invoices:rent_amount')}?{query}&month={month}")


# --- One combo per line -------------------------------------------------------------------------


def test_a_new_draft_opens_with_one_line_and_the_list_starts_with_rent(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    deposit = a_deposit(lease)
    billing, _ = client_for(Role.BILLING)
    page = html(billing, f"/invoices/new/store/{lease.store_id}/")
    assert page.count('class="draft-line"') == 1
    assert 'id="id_lines-0-kind"' not in page
    options = page[page.index('<datalist id="draft-items">') : page.index("</datalist>")]
    label = "Existing open charge: Security deposit ($500.00)"
    assert options.index('value="Rent"') < options.index(label) < options.index("Sign fee")
    assert f'data-charge="{deposit.pk}" data-balance="500.00"' in options
    # Add line and the trash button are the script's; the Remove box stays without it.
    assert 'id="add-line"' in page and 'aria-label="Remove line"' in page
    assert 'name="lines-0-DELETE"' in page
    assert 'id="lines-total"' in page and 'id="lines-total-value">$0.00<' in page


def test_the_month_select_offers_twelve_months_either_side_by_name(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    draft = a_draft(operator, lease, [line()])
    billing, _ = client_for(Role.BILLING)
    page = html(billing, f"/invoices/drafts/{draft.pk}/")
    select = page[page.index('name="lines-0-rent_month"') :]
    select = select[: select.index("</select>")]
    assert '<option value="2026-10">October 2026</option>' in select
    assert '<option value="2025-10">October 2025</option>' in select
    assert '<option value="2027-10">October 2027</option>' in select
    assert "2025-09" not in select and "2027-11" not in select
    assert select.count("<option") == 25 + 1  # the months and "Choose the month"
    assert 'type="month"' not in page
    # The script reveals the lease and the month only for Rent, and reads the amount.
    script = (REPO / "ledger" / "static" / "ledger" / "invoice_lines.js").read_text()
    assert 'kind === "rent"' in script and "td.rent-field" in script
    assert "fetch(url" in script and 'data-rent-amount-url="/invoices/rent-amount/?draft=' in page


def test_a_rent_line_posted_from_the_item_box_takes_the_typed_amount(fake: FakeProvider) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    draft = a_draft(operator, lease, [line()])
    billing, _ = client_for(Role.BILLING)
    data = page_data(
        [
            {"item": "Sign repair", "unit_amount": "100.00"},
            # Filled by the endpoint, then edited by hand: the amount stays editable.
            {"item": "rent ", "rent_lease": str(lease.pk), "rent_month": "2026-10"},
        ],
        lease=str(lease.pk),
    )
    data["lines-1-unit_amount"] = "500.00"
    billing.post(f"/invoices/drafts/{draft.pk}/edit/", data)
    _, rent = invoice_builder.lines_of(fresh(draft))
    assert (rent.rent_lease_id, rent.rent_month, rent.unit_amount) == (lease.pk, OCTOBER, D("500"))
    # Re-opened, the line shows Rent and its month selected.
    page = html(billing, f"/invoices/drafts/{draft.pk}/")
    assert 'name="lines-1-item" value="Rent"' in page
    assert '<option value="2026-10" selected>October 2026</option>' in page


def test_the_rent_columns_start_collapsible_and_open_when_a_saved_line_is_rent(
    fake: FakeProvider,
) -> None:
    """PLAN-012e-3: the two Rent columns carry ``rent-col`` (the script collapses them
    while no line is Rent); the table starts with ``has-rent`` only when a saved line is
    Rent."""
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    draft = a_draft(operator, lease, [line()])
    billing, _ = client_for(Role.BILLING)
    page = html(billing, f"/invoices/drafts/{draft.pk}/")
    table = page[page.index('<table id="draft-lines"') : page.index("</table>")]
    head = table[: table.index("</thead>")]
    assert '<th class="rent-col">Rent: lease</th><th class="rent-col">Rent: month</th>' in head
    # Two Rent cells on every row (the saved line and the blank one the page adds).
    rows = table.count('class="draft-line"')
    assert rows >= 1 and table.count('<td class="rent-field rent-col"') == 2 * rows
    assert "has-rent" not in table[: table.index(">")]
    data = page_data(
        [
            {"item": "Sign repair", "unit_amount": "100.00"},
            {"item": "Rent", "rent_lease": str(lease.pk), "rent_month": "2026-10"},
        ],
        lease=str(lease.pk),
    )
    data["lines-1-unit_amount"] = "500.00"
    billing.post(f"/invoices/drafts/{draft.pk}/edit/", data)
    page = html(billing, f"/invoices/drafts/{draft.pk}/")
    opening = page[page.index('<table id="draft-lines"') :]
    assert 'class="has-rent"' in opening[: opening.index(">")]
    script = (REPO / "ledger" / "static" / "ledger" / "invoice_lines.js").read_text()
    assert '"has-rent"' in script and ".rent-col" in script and "cell.hidden" in script


# --- The rent-amount endpoint ------------------------------------------------------------------


def test_the_endpoint_gives_the_prorated_first_month_and_the_run_charges_amount(
    fake: FakeProvider,
) -> None:
    operator = an_operator(fake)
    lease = a_rent_lease(operator)
    billing, _ = client_for(Role.BILLING)
    prorated = rent_amount(billing, lease, "2026-10", operator=str(operator.pk))
    assert prorated.status_code == 200 and prorated["Content-Type"] == "application/json"
    assert prorated.json() == {"amount": str(FIRST_MONTH), "text": FIRST_TEXT, "message": ""}
    # The rent run's charge of the month, at its own amount.
    made, _ = rent_run.create_month(lease, NOVEMBER, None, amount=D("975.50"))
    assert rent_amount(billing, lease, "2026-11").json()["amount"] == "975.50"
    # A month that cannot be billed says why, with no amount.
    before = rent_amount(billing, lease, "2026-09").json()
    assert before["amount"] is None and "cannot be before it" in before["message"]
    assert not before["message"].startswith("Line ")
    # A draft binds the lease to its operator.
    draft = a_draft(operator, lease, [line()])
    assert rent_amount(billing, lease, "2026-10", draft=str(draft.pk)).status_code == 200
    other = an_operator(fake, customer="cus-lines-2", bank="pm-lines-2")
    elsewhere = a_draft(other, None, [line()])
    assert rent_amount(billing, lease, "2026-10", draft=str(elsewhere.pk)).status_code == 404
    for month in ("2026-1", "2026-00", "oct", ""):
        assert rent_amount(billing, lease, month).status_code == 400, month
    assert str(made.pk) not in prorated.content.decode()


# --- Existing open charges ----------------------------------------------------------------------


def test_the_checklists_deposit_joins_the_invoice_and_is_attached_on_send(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    deposit = a_deposit(lease)
    billing, _ = client_for(Role.BILLING)
    label = "Existing open charge: Security deposit ($500.00)"
    # Typed (or chosen without the script): found by the box's text.
    response = billing.post(
        f"/invoices/new/store/{lease.store_id}/",
        page_data(
            [{"item": label, "unit_amount": ""}, {"item": "Sign repair", "unit_amount": "100"}],
            lease=str(lease.pk),
            memo=MEMO,
        ),
    )
    draft = InvoiceDraft.objects.get()
    assert response["Location"].startswith(f"/invoices/drafts/{draft.pk}/")
    first, second = invoice_builder.lines_of(draft)
    assert (first.existing_charge_id, first.item, first.unit_amount, first.fee_type_id) == (
        deposit.pk,
        "Security deposit",
        D("500.00"),
        None,
    )
    assert second.existing_charge_id is None
    page = html(billing, f"/invoices/drafts/{draft.pk}/")
    assert f'name="lines-0-item" value="{label}"' in page
    assert f'name="lines-0-existing_charge" value="{deposit.pk}"' in page
    charges_before = OneOffCharge.objects.count()
    invoice = sent(draft).invoice
    assert invoice is not None
    # Attached, not created: one new charge (the typed line) and the deposit's own row.
    assert OneOffCharge.objects.count() == charges_before + 1
    assert invoice.one_off_charge_id == deposit.pk and invoice.total == D("600.00")
    lines = list(InvoiceLineItem.objects.filter(invoice=invoice).order_by("position"))
    assert [(row.one_off_charge_id, row.line_total) for row in lines][0] == (
        deposit.pk,
        D("500.00"),
    )
    deposit = fresh(deposit)
    assert (deposit.collection, deposit.price, deposit.category) == (
        CollectionMethod.EMAIL,
        D("500.00"),
        OneOffCategory.SETUP,
    )
    audit = AuditLog.objects.get(action=invoice_builder.ATTACHED_ACTION)
    assert audit.entity_id == str(deposit.pk) and audit.before["collection"] == "none"
    assert audit.after["invoice_number"] == invoice.invoice_number
    # Billed now: no longer offered.
    assert invoice_builder.open_charges(lease) == []


def test_an_existing_charge_below_its_balance_is_refused_and_never_repriced(
    fake: FakeProvider,
) -> None:
    """D10 amended (2026-10-16): the line bills the charge at its balance; a lower amount
    is refused with the sentence, nothing written; the exact balance is billed unchanged."""
    operator, lease = a_world(fake)
    deposit = a_deposit(lease)
    refused = invoice_builder.create_draft(
        operator, lease, MEMO, None, [existing(deposit, "499.99")], user(Role.BILLING), today=TODAY
    )
    assert not refused.ok and refused.message == (
        "Line 1: Security deposit is billed at its balance, $500.00. To change the amount, "
        "edit the charge first."
    )
    assert refused.message == invoice_builder.LINE_EXISTING_BELOW.format(
        line=1, charge="Security deposit", balance="$500.00"
    )
    assert not InvoiceDraft.objects.exists()
    # Through the page: the same sentence, the draft not made.
    billing, _ = client_for(Role.BILLING)
    path = f"/invoices/new/store/{lease.store_id}/"
    label = "Existing open charge: Security deposit ($500.00)"
    billing.post(path, page_data([{"item": label, "unit_amount": "250.00"}], lease=str(lease.pk)))
    assert "is billed at its balance, $500.00. To change the amount, edit the charge first." in (
        html(billing, path)
    )
    assert not InvoiceDraft.objects.exists()
    # Exactly the balance: billed, the charge's price unchanged.
    draft = a_draft(operator, lease, [existing(deposit, "500.00")])
    invoice = sent(draft).invoice
    assert invoice is not None and invoice.total == D("500.00")
    assert fresh(deposit).price == D("500.00")


def test_a_saved_existing_line_whose_charge_was_lowered_is_refused_on_send(
    fake: FakeProvider,
) -> None:
    """The charge edited after the draft was saved: the send refuses the old amount and
    changes nothing; the charge keeps its new price."""
    operator, lease = a_world(fake)
    deposit = a_deposit(lease)
    draft = a_draft(operator, lease, [existing(deposit)])
    OneOffCharge.objects.filter(pk=deposit.pk).update(price=D("400.00"))
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING), today=TODAY)
    assert not outcome.ok and "can be at most its balance, $400.00" in outcome.message
    assert not Invoice.objects.exists() and fresh(deposit).price == D("400.00")
    # Changed between the check and the row lock: refused as changed meanwhile, unpriced.
    clean = invoice_builder._clean_existing_line(1, existing(deposit), lease, operator)
    OneOffCharge.objects.filter(pk=deposit.pk).update(price=D("350.00"))
    with pytest.raises(invoice_builder._Refused) as refusal, transaction.atomic():
        invoice_builder._attach_existing(clean, 1, CollectionMethod.EMAIL, lease)
    assert refusal.value.outcome.message == invoice_builder.LINE_EXISTING_CHANGED.format(
        line=1, charge="Security deposit", balance="$350.00"
    )
    deposit = fresh(deposit)
    assert (deposit.price, deposit.collection) == (D("350.00"), CollectionMethod.NONE)


def test_the_existing_lines_amount_is_read_only_on_the_page() -> None:
    script = (REPO / "ledger" / "static" / "ledger" / "invoice_lines.js").read_text()
    assert "unit.value = chosen.dataset.balance;\n            unit.readOnly = true;" in script
    assert "unit.readOnly = false;" in script and "input.readOnly = false;" in script
    assert "unit.max" not in script
    # PLAN-012e-2 D10: the sentence moved from the draft form to the help page it links.
    form = (REPO / "ledger" / "templates" / "ledger" / "invoices" / "_draft_form.html").read_text()
    assert "slug='invoice-lines'" in form
    help_page = REPO / "finance" / "templates" / "finance" / "help" / "invoice-lines.html"
    assert "the amount is its balance and cannot be changed here" in help_page.read_text()


def test_a_charge_on_a_live_invoice_or_a_plan_is_neither_offered_nor_attached(
    fake: FakeProvider,
) -> None:
    from ledger.models import InstallmentPlan

    operator, lease = a_world(fake)
    invoiced = OneOffCharge.objects.create(
        operator=operator,
        lease=lease,
        category=OneOffCategory.DAMAGE,
        collection=CollectionMethod.MANUAL,
        description="Broken shelf",
        price=D("150.00"),
        due_date=TODAY,
        status=OneOffChargeStatus.OWED,
    )
    Invoice.objects.create(
        invoice_number="INV-2026-9001",
        operator=operator,
        one_off_charge=invoiced,
        issue_date=TODAY,
        due_date=TODAY,
        memo="Broken shelf",
        subtotal=D("150.00"),
        tax=D("0"),
        fees=D("0"),
        surcharge=D("0"),
        total=D("150.00"),
        internal_status=InvoiceStatus.DRAFT,
        allow_card=False,
        allow_ach=True,
        legacy=False,
    )
    planned = OneOffCharge.objects.create(
        operator=operator,
        lease=lease,
        category=OneOffCategory.ARREARS,
        collection=CollectionMethod.MANUAL,
        description="Arrears",
        price=D("900.00"),
        due_date=TODAY,
        status=OneOffChargeStatus.OWED,
    )
    InstallmentPlan.objects.create(
        one_off_charge=planned,
        operator=operator,
        count=3,
        first_due=TODAY,
        cadence="monthly",
        collection="manual",
        confirmed_at=builder_tests.NOW,
    )
    open_one = a_deposit(lease)
    assert invoice_builder.open_charges(lease) == [open_one]
    for charge in (invoiced, planned):
        refused = invoice_builder.create_draft(
            operator, lease, MEMO, None, [existing(charge)], user(Role.BILLING), today=TODAY
        )
        assert not refused.ok and "is no longer open" in refused.message, charge.description
    # Open when saved, billed elsewhere before the send: refused, nothing written.
    draft = a_draft(operator, lease, [existing(open_one)])
    OneOffCharge.objects.filter(pk=open_one.pk).update(amount_paid=D("1.00"), status="partial")
    outcome = invoice_builder.send(draft.pk, user(Role.BILLING))
    assert not outcome.ok and "is no longer open" in outcome.message
    assert not Invoice.objects.exclude(invoice_number="INV-2026-9001").exists()


def test_the_same_existing_charge_on_another_draft_is_refused_with_its_link(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    deposit = a_deposit(lease)
    first = a_draft(operator, lease, [existing(deposit)])
    twice = invoice_builder.create_draft(
        operator,
        lease,
        MEMO,
        None,
        [existing(deposit), existing(deposit)],
        user(Role.BILLING),
        today=TODAY,
    )
    assert not twice.ok and twice.message.startswith("Line 2: Security deposit is already on")
    billing, _ = client_for(Role.BILLING)
    second = a_draft(operator, lease, [line()])
    label = "Existing open charge: Security deposit ($500.00)"
    billing.post(
        f"/invoices/drafts/{second.pk}/edit/",
        page_data([{"item": label}], lease=str(lease.pk)),
    )
    page = html(billing, f"/invoices/drafts/{second.pk}/")
    link = f'<a href="/invoices/drafts/{first.pk}/" class="draft-link">{first.number}</a>.'
    assert f"Line 1: Security deposit is already on draft {link}" in page


def test_an_existing_charge_line_with_a_rent_month_or_a_quantity_is_refused(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    deposit = a_deposit(lease)
    for bad in (
        invoice_builder.LineInput(
            "", "", None, None, existing_charge=deposit.pk, rent_lease=lease.pk, rent_month=OCTOBER
        ),
        invoice_builder.LineInput("", "", D("2"), None, existing_charge=deposit.pk),
    ):
        outcome = invoice_builder.create_draft(
            operator, lease, MEMO, None, [bad], user(Role.BILLING), today=TODAY
        )
        assert not outcome.ok, bad
    # An operator-level draft has no lease's charges.
    outcome = invoice_builder.create_draft(
        operator, None, MEMO, None, [existing(deposit)], user(Role.BILLING), today=TODAY
    )
    assert not outcome.ok and outcome.message == invoice_builder.LINE_EXISTING_OTHER.format(line=1)
    assert not InvoiceDraftLine.objects.exists()


# --- Removed lines and visible numbering ---------------------------------------------------------


def test_removed_lines_are_ignored_and_errors_name_the_visible_line(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    billing, _ = client_for(Role.BILLING)
    path = f"/invoices/new/store/{lease.store_id}/"
    rows = [
        {"item": "Removed", "unit_amount": "abc", "DELETE": "on"},
        {"item": "Sign repair", "unit_amount": "100.00"},
        {"item": "Poster", "unit_amount": "0.00"},
    ]
    billing.post(path, page_data(rows, lease=str(lease.pk)))
    page = html(billing, path)
    # The service's "Line 2" (its second line) is the second visible line too.
    assert "Line 2: the unit amount must be more than zero" in page
    # A form error on the third row, after a removed first row, is the visible line 2.
    rows[2] = {"item": "Rent", "unit_amount": ""}
    billing.post(path, page_data(rows, lease=str(lease.pk)))
    page = html(billing, path)
    assert "Line 2: Choose the lease and the month of the rent." in page
    assert "Line 3:" not in page
    # The held form keeps the removed row hidden and numbers the others.
    assert page.count('<tr class="draft-line" hidden>') == 1
    assert 'class="line-number" data-label="Line">2</td>' in page
    assert 'class="line-number" data-label="Line">3</td>' not in page
    # With a blank row between, a service refusal still names the visible line.
    rows = [
        {"item": "Sign repair", "unit_amount": "100.00"},
        {"item": "", "unit_amount": ""},
        {"item": "Poster", "unit_amount": "0.00"},
    ]
    billing.post(path, page_data(rows, lease=str(lease.pk)))
    assert "Line 3: the unit amount must be more than zero" in html(billing, path)
    assert not InvoiceDraft.objects.exists()


def test_the_trash_button_removes_a_line_from_the_draft(fake: FakeProvider) -> None:
    operator, lease = a_world(fake)
    draft = a_draft(operator, lease, [line("Sign repair"), line("Poster", "20.00")])
    billing, _ = client_for(Role.BILLING)
    page = html(billing, f"/invoices/drafts/{draft.pk}/")
    assert page.count('aria-label="Remove line"') == 3  # two lines and the blank one
    assert 'id="lines-total-value">$120.00<' in page
    script = (REPO / "ledger" / "static" / "ledger" / "invoice_lines.js").read_text()
    assert "remove.checked = true;" in script and "row.hidden = true;" in script
    data = page_data(
        [
            {"item": "Sign repair", "unit_amount": "100.00", "DELETE": "on"},
            {"item": "Poster", "unit_amount": "20.00"},
        ],
        lease=str(lease.pk),
    )
    billing.post(f"/invoices/drafts/{draft.pk}/edit/", data)
    assert [row.item for row in invoice_builder.lines_of(fresh(draft))] == ["Poster"]


# --- Styles ------------------------------------------------------------------------------------


def test_the_item_box_takes_its_cells_full_width() -> None:
    css = (REPO / "finance" / "static" / "finance" / "app.css").read_text()
    assert "td.line-item input { width: 100%;" in css
    assert ".line-trash[hidden]" in css


# --- The catalogue flag of a saved item ---------------------------------------------------------


def test_an_item_saved_from_the_builder_is_hidden_from_the_setup_checklist(
    fake: FakeProvider,
) -> None:
    operator, lease = a_world(fake)
    a_draft(operator, lease, [line("Window film", "75.00", save_item=True)])
    saved = FeeType.objects.get(name="Window film")
    assert saved.show_in_setup is False and saved.active
    assert saved in invoice_builder.offered_fee_types()
    assert saved not in one_off_service.checklist_types()
    audit = AuditLog.objects.get(action=invoice_builder.FEE_TYPE_CREATED_ACTION)
    assert audit.after["show_in_setup"] is False
    admin, _ = client_for(Role.ADMIN)
    assert 'id="setup-window-film"' not in html(admin, "/leases/new/")


def test_the_flag_is_listed_and_edited_in_the_admin_and_audited() -> None:
    from tests.auth_helpers import make_user, verified_client

    fee_type = FeeType.objects.create(code="window-film", name="Window film", sort_order=500)
    user_row, device = make_user("admin-flag", Role.ADMIN, superuser=True)
    assert device is not None
    client = verified_client(user_row, device)
    assert "show_in_setup" in client.get("/admin/ledger/feetype/").content.decode()
    response = client.post(
        f"/admin/ledger/feetype/{fee_type.pk}/change/",
        {
            "code": "window-film",
            "name": "Window film",
            "default_cost": "",
            "default_price": "",
            "active": "on",
            "show_in_setup": "on",
            "sort_order": "500",
        },
    )
    assert response.status_code == 302
    assert FeeType.objects.get(pk=fee_type.pk).show_in_setup is True
    audit = AuditLog.objects.get(action="fee_type.changed_in_admin")
    assert (audit.before["show_in_setup"], audit.after["show_in_setup"]) == (False, True)
