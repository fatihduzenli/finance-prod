"""The operator page as the workspace (docs/qa/PLAN-012d-store-workspace.md D14, §6 E) and the
reports' month selects (the owner's Safari rule, applied to every month picker).

The selector (the operator's only store by default, else the first by label; ``?store=``
picks another of the operator's stores; another operator's store is ignored), the header
card of the selected store with the operator's total, the bar on the operator's lease of
the selected store, the tabs across the operator's stores with a store column, the drafts'
Edit, Send and Delete, the operator-level items kept above the tabs, the roles, the query
budget, and the From and To selects of the P&L and the ranged reports. The adversarial
cases are the QA owner's (test_plan012d_e_adversarial.py)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionStatus,
    InvoiceDraft,
    Lease,
    Operator,
    PaymentChannel,
)
from ledger.services import workspace
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_invoice,
    an_operator,
)
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_store_workspace import a_draft, page

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
OCTOBER = date(2026, 10, 1)
STARTED = date(2026, 9, 1)
TEMPLATES = Path(__file__).resolve().parent.parent / "ledger" / "templates"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


def two_stores(name: str = "Two-store Ops") -> tuple[Operator, Lease, Lease]:
    """An operator with wire leases of stores 1001 and 1002 (created second first, so
    the default is by label, not by age)."""
    operator = an_operator(name)
    second = a_lease(a_store("1002"), operator, started=STARTED, channel=PaymentChannel.WIRE)
    first = a_lease(a_store("1001"), operator, started=STARTED, channel=PaymentChannel.WIRE)
    return operator, first, second


def operator_page(operator: Operator, **query: str) -> str:
    url = reverse("ledger:operators:detail", kwargs={"pk": operator.pk})
    return url + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")


def bar(html: str) -> str:
    found = re.search(r'<nav aria-label="Store actions" id="action-bar".*?</nav>', html, re.S)
    return found.group(0) if found else ""


def section(html: str, element_id: str) -> str:
    found = re.search(rf'<section id="{element_id}".*?</section>', html, re.S)
    assert found is not None, element_id
    return found.group(0)


# --- The selector and the header -----------------------------------------------------------


def test_the_default_store_is_the_first_by_label_and_the_selector_lists_the_operators() -> None:
    operator, first, second = two_stores()
    a_rent_charge(first, OCTOBER, due="1000.00")
    a_rent_charge(second, OCTOBER, due="250.00")
    other = a_lease(a_store("3001"), an_operator("Other"), started=STARTED)
    html = page(client_for(Role.BILLING)[0], operator_page(operator))
    selector = re.search(r'<select name="store" id="store-select">.*?</select>', html, re.S)
    assert selector is not None
    options = selector.group(0)
    assert options.index("1001") < options.index("1002")
    assert f'value="{first.store_id}" selected' in options
    assert str(other.store_id) not in html and "3001" not in options
    header = section(html, "workspace-header")
    assert "1001" in header and "1002" not in header
    assert re.search(r'id="header-open-balance">\$1000\.00<', header)
    assert re.search(r'id="header-operator-balance">\$1250\.00<', header)
    actions = bar(html)
    assert f"lease={first.pk}" in actions and f"lease={second.pk}" not in actions
    assert reverse("ledger:stores:record_payment", kwargs={"pk": first.store_id}) in actions


def test_an_only_store_is_the_default() -> None:
    lease = a_lease(a_store("2477"), an_operator("Solo"), started=STARTED)
    assert lease.operator is not None
    html = page(client_for(Role.BILLING)[0], operator_page(lease.operator))
    assert f'value="{lease.store_id}" selected' in html
    assert f"lease={lease.pk}" in bar(html)


def test_the_store_parameter_switches_the_store_and_the_bar() -> None:
    operator, first, second = two_stores()
    a_rent_charge(first, OCTOBER, due="1000.00")
    a_rent_charge(second, OCTOBER, due="250.00")
    html = page(client_for(Role.BILLING)[0], operator_page(operator, store=str(second.store_id)))
    assert f'value="{second.store_id}" selected' in html
    header = section(html, "workspace-header")
    assert "1002" in header and re.search(r'id="header-open-balance">\$250\.00<', header)
    assert re.search(r'id="header-operator-balance">\$1250\.00<', header)
    actions = bar(html)
    assert f"lease={second.pk}" in actions and f"lease={first.pk}" not in actions
    assert str(first.store_id) not in actions
    # The tab links keep the selection.
    assert f"?store={second.store_id}&amp;tab=invoices#tab-invoices" in html


@pytest.mark.parametrize("raw", ["another", "not-a-uuid", ""])
def test_another_operators_store_or_a_bad_value_falls_back_to_the_default(raw: str) -> None:
    operator, first, _ = two_stores()
    theirs = a_lease(a_store("9009"), an_operator("Other"), started=STARTED)
    a_rent_charge(theirs, OCTOBER, due="4321.00")
    value = str(theirs.store_id) if raw == "another" else raw
    html = page(client_for(Role.ADMIN)[0], operator_page(operator, store=value, tab="history"))
    assert f'value="{first.store_id}" selected' in html
    assert "9009" not in html and "4321.00" not in html and str(theirs.pk) not in html


def test_a_store_now_leased_to_another_operator_shows_no_lease_and_no_bar() -> None:
    operator = an_operator("Outgoing")
    store = a_store("4004")
    a_lease(
        store,
        operator,
        started=date(2025, 1, 1),
        channel=PaymentChannel.WIRE,
        status=CollectionStatus.ENDED,
    )
    Lease.objects.filter(store=store).update(ended_on=date(2026, 8, 31))
    incoming = a_lease(store, an_operator("Incoming"), started=STARTED)
    html = page(client_for(Role.ADMIN)[0], operator_page(operator))
    assert f'value="{store.pk}" selected' in html
    assert "No current lease of this operator" in section(html, "workspace-header")
    assert bar(html) == "" and 'id="no-operator-lease"' in html
    assert str(incoming.pk) not in html and "Incoming" not in html


def test_an_operator_without_a_store_has_tabs_and_no_header() -> None:
    operator = an_operator("Nobody")
    a_one_off_charge(operator, "75.00", OCTOBER)
    html = page(client_for(Role.ADMIN)[0], operator_page(operator))
    assert 'id="no-stores"' in html and 'id="workspace-header"' not in html
    assert "Broken shelf" in section(html, "tab-charges")
    assert "No store." in page(client_for(Role.ADMIN)[0], operator_page(operator, tab="history"))


# --- The tabs across the operator's stores ---------------------------------------------------


def test_the_charges_tab_lists_every_store_of_the_operator_with_a_store_column() -> None:
    operator, first, second = two_stores()
    a_rent_charge(first, OCTOBER, due="1000.00")
    a_rent_charge(second, OCTOBER, due="250.00")
    a_one_off_charge(operator, "75.00", OCTOBER)
    other = a_lease(a_store("9009"), an_operator("Other"), started=STARTED)
    a_rent_charge(other, OCTOBER, due="4321.00")
    html = section(page(client_for(Role.BILLING)[0], operator_page(operator)), "tab-charges")
    assert "<th>Store</th>" in html
    assert "1001" in html and "1002" in html and "Broken shelf" in html
    assert "9009" not in html and "4321.00" not in html
    assert 'id="charges-balance">$1325.00<' in html
    assert f"?operator={operator.pk}" in html
    # The payment forms come back to the operator page.
    assert "next=%2Foperators%2F" in html


def test_the_other_tabs_list_the_operators_rows() -> None:
    operator, first, second = two_stores()
    charge = a_rent_charge(second, OCTOBER, due="1000.00")
    a_payment(charge, "100.00", TODAY)
    an_invoice(operator, 77, "1000.00", rent_charge=charge)
    other_operator = an_operator("Other")
    an_invoice(other_operator, 78, "10.00")
    client = client_for(Role.BILLING)[0]
    payments = section(page(client, operator_page(operator, tab="payments")), "tab-payments")
    assert "1002" in payments and "$100.00" in payments
    assert f"operator={operator.pk}" in payments
    invoices = section(page(client, operator_page(operator, tab="invoices")), "tab-invoices")
    assert "INV-2026-0077" in invoices and "INV-2026-0078" not in invoices
    assert '<td data-label="Store">1002</td>' in invoices
    schedules = page(client, operator_page(operator, tab="schedules"))
    assert 'id="tab-schedules"' in schedules
    contacts = page(client, operator_page(operator, tab="contacts"))
    assert 'href="#contacts"' in section(contacts, "tab-contacts")


def test_documents_and_history_are_the_selected_stores_and_history_is_the_admins() -> None:
    operator, first, second = two_stores()
    AuditLog.objects.create(
        action="operator.updated",
        entity_type="operator",
        entity_id=str(operator.pk),
        after={"marker": "operator-row"},
    )
    AuditLog.objects.create(
        action="store.updated",
        entity_type="store",
        entity_id=str(second.store_id),
        after={"marker": "second-store-row"},
    )
    AuditLog.objects.create(
        action="store.updated",
        entity_type="store",
        entity_id=str(first.store_id),
        after={"marker": "first-store-row"},
    )
    admin = client_for(Role.ADMIN)[0]
    history = section(
        page(admin, operator_page(operator, store=str(second.store_id), tab="history")),
        "tab-history",
    )
    assert "operator-row" in history and "second-store-row" in history
    assert "first-store-row" not in history
    documents = page(admin, operator_page(operator, store=str(second.store_id), tab="documents"))
    assert 'id="documents"' in documents
    billing = page(client_for(Role.BILLING)[0], operator_page(operator, tab="history"))
    assert "tab=history" not in billing and "operator-row" not in billing


# --- The drafts on the Invoices tab (12d-1) -------------------------------------------------


def test_the_invoices_tab_lists_the_operators_drafts_with_edit_send_and_delete() -> None:
    operator, first, second = two_stores()
    on_first = a_draft(first)
    on_second = a_draft(second, item="Key copy")
    client = client_for(Role.BILLING)[0]
    html = section(page(client, operator_page(operator, tab="invoices")), "tab-invoices")
    back = "?next=%2Foperators%2F" + str(operator.pk) + "%2F%3Ftab%3Dinvoices"
    for draft in (on_first, on_second):
        assert draft.number in html
        assert f'href="{reverse("ledger:invoices:draft", kwargs={"pk": draft.pk})}"' in html
        assert reverse("ledger:invoices:send", kwargs={"pk": draft.pk}) + back in html
        assert reverse("ledger:invoices:delete_draft", kwargs={"pk": draft.pk}) + back in html
    viewer = section(
        page(client_for(Role.VIEWER)[0], operator_page(operator, tab="invoices")), "tab-invoices"
    )
    assert on_first.number in viewer and "/delete/" not in viewer and "/send/" not in viewer


def test_delete_from_the_operator_page_returns_to_its_invoices_tab() -> None:
    operator, first, _ = two_stores()
    draft = a_draft(first)
    client = client_for(Role.BILLING)[0]
    back = workspace.operator_tab_url(operator.pk, workspace.TAB_INVOICES)
    url = reverse("ledger:invoices:delete_draft", kwargs={"pk": draft.pk})
    response = client.post(url + "?next=" + back.replace("?", "%3F").replace("=", "%3D"))
    assert (response.status_code, response["Location"]) == (302, back)
    assert not InvoiceDraft.objects.filter(pk=draft.pk).exists()
    assert f"Draft {draft.number} deleted." in page(client, back)
    # A next that leaves the site is ignored.
    other = a_draft(first)
    url = reverse("ledger:invoices:delete_draft", kwargs={"pk": other.pk})
    response = client.post(url + "?next=https://example.com/")
    assert response["Location"] == back + "#tab-invoices"


def test_send_without_a_store_follows_a_local_next_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.services import invoice_builder

    operator, first, _ = two_stores()
    draft = a_draft(first)
    monkeypatch.setattr(
        invoice_builder,
        "send",
        lambda pk, actor: invoice_builder.Outcome(True, "Sent."),
    )
    client = client_for(Role.BILLING)[0]
    back = workspace.operator_tab_url(operator.pk, workspace.TAB_INVOICES)
    url = reverse("ledger:invoices:send", kwargs={"pk": draft.pk})
    quoted = back.replace("?", "%3F").replace("=", "%3D")
    assert client.post(url + "?next=" + quoted)["Location"] == back
    draft_page = reverse("ledger:invoices:draft", kwargs={"pk": draft.pk})
    assert client.post(url + "?next=//example.com/")["Location"] == draft_page


# --- The operator-level items and the roles -----------------------------------------------


def test_the_operator_level_items_stay_above_the_tabs_and_the_old_lists_are_gone() -> None:
    operator, first, _ = two_stores()
    Operator.objects.filter(pk=operator.pk).update(notes="Prefers calls after noon")
    html = page(client_for(Role.BILLING)[0], operator_page(operator))
    workspace_at = html.index('id="workspace"')
    for marker in (
        'id="sms-phone"',
        'id="sms-consent"',
        'id="email-consent"',
        'id="sync-status"',
        'id="collections"',
        'id="handled-personally"',
        'id="contacts"',
        'id="add-contact"',
        "Prefers calls after noon",
        'id="new-invoice"',
    ):
        assert marker in html and html.index(marker) < workspace_at, marker
    for gone in ('id="invoice-drafts-table"', 'id="one-off-charges"', 'id="outstanding"'):
        assert gone not in html, gone
    # The bar's Create invoice has its own id beside the page's New invoice.
    assert html.count('id="new-invoice"') == 1 and 'id="bar-new-invoice"' in bar(html)


def test_a_viewer_sees_no_bar_and_no_history_and_billing_no_admin_actions() -> None:
    operator, first, _ = two_stores()
    viewer = page(client_for(Role.VIEWER)[0], operator_page(operator))
    assert bar(viewer) == "" and "tab=history" not in viewer
    assert 'id="store-select"' in viewer
    billing = bar(page(client_for(Role.BILLING)[0], operator_page(operator)))
    assert "/payments/new/" in billing and "/schedule-payment/" in billing
    assert "/end-lease/" not in billing and "/transfer/" not in billing
    admin = page(client_for(Role.ADMIN)[0], operator_page(operator))
    assert "/end-lease/" in bar(admin) and "/transfer/" in bar(admin)
    assert "tab=history" in admin


# --- The query budget ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["charges", "invoices", "payments", "schedules", "history"])
def test_the_header_and_the_open_tab_stay_within_the_budget_for_three_stores(name: str) -> None:
    operator = an_operator("Three stores")
    leases = [
        a_lease(a_store(f"50{n}"), operator, started=STARTED, channel=PaymentChannel.WIRE)
        for n in range(3)
    ]

    def build() -> int:
        with CaptureQueriesContext(connection) as queries:
            workspace.operator_workspace(operator, TODAY, Role.ADMIN, tab=name)
        return len(queries)

    for lease in leases:
        charge = a_rent_charge(lease, OCTOBER, due="1000.00")
        a_payment(charge, "100.00", TODAY)
        an_invoice(operator, 900 + leases.index(lease), "1000.00", rent_charge=charge)
        a_draft(lease)
    small = build()
    for lease in leases:
        for month in range(1, 9):
            more = a_rent_charge(lease, date(2026, month, 1), due="100.00")
            a_payment(more, "10.00", date(2026, month, 5))
            a_one_off_charge(operator, "20.00", date(2026, month, 3), lease=lease)
        a_draft(lease)
    large = build()
    assert small <= 30 and large <= 30, (small, large)
    assert large == small


def test_the_operator_page_renders_within_a_fixed_number_of_queries() -> None:
    operator = an_operator("Three stores")
    leases = [
        a_lease(a_store(f"60{n}"), operator, started=STARTED, channel=PaymentChannel.WIRE)
        for n in range(3)
    ]
    client = client_for(Role.ADMIN)[0]

    def render() -> int:
        with CaptureQueriesContext(connection) as queries:
            page(client, operator_page(operator))
        return len(queries)

    for lease in leases:
        a_rent_charge(lease, OCTOBER, due="1000.00")
        a_one_off_charge(operator, "20.00", OCTOBER, lease=lease)
    small = render()
    for lease in leases:
        for month in range(1, 9):
            a_rent_charge(lease, date(2026, month, 1), due="100.00")
            a_one_off_charge(operator, "20.00", date(2026, month, 3), lease=lease)
    assert render() == small


# --- The reports' month selects --------------------------------------------------------------


def month_select(html: str, name: str) -> str:
    found = re.search(rf'<select name="{name}" id="{name}">.*?</select>', html, re.S)
    assert found is not None, name
    return found.group(0)


@pytest.mark.parametrize("route", ["pnl", "revenue", "payments_by_method", "deposits", "statement"])
def test_the_reports_from_and_to_are_month_selects_of_49_months(route: str) -> None:
    operator = an_operator("Statement Ops")
    url = (
        reverse("ledger:operators:statement", kwargs={"pk": operator.pk})
        if route == "statement"
        else reverse(f"ledger:reports:{route}")
    )
    html = page(client_for(Role.VIEWER)[0], url + "?from=2026-07&to=2026-09")
    assert 'type="month"' not in html
    start, end = month_select(html, "from"), month_select(html, "to")
    for select in (start, end):
        assert select.count("<option ") == 49 and select.count(" selected") == 1
        assert '<option value="2024-10">October 2024</option>' in select
        assert '<option value="2028-10">October 2028</option>' in select
    assert '<option value="2026-07" selected>July 2026</option>' in start
    assert '<option value="2026-09" selected>September 2026</option>' in end
    assert "from=2026-07" in html and "to=2026-09" in html


def test_a_range_outside_the_window_is_offered_too_and_the_pnl_reads_it() -> None:
    html = page(
        client_for(Role.VIEWER)[0], reverse("ledger:reports:pnl") + "?from=2023-01&to=2023-03"
    )
    start = month_select(html, "from")
    assert start.count("<option ") == 52
    assert '<option value="2023-01" selected>January 2023</option>' in start
    assert '<option value="2023-03" selected>March 2023</option>' in month_select(html, "to")
    export = re.search(r'id="export-csv" href="([^"]+)"', html)
    assert export is not None and "from=2023-01" in export.group(1)
    assert "to=2023-03" in export.group(1)


def test_no_month_input_is_left_in_the_templates() -> None:
    found = [
        str(path.relative_to(TEMPLATES))
        for path in TEMPLATES.rglob("*.html")
        if 'type="month"' in path.read_text()
    ]
    assert found == []
