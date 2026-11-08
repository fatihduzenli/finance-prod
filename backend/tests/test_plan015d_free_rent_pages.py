"""PLAN-015d task 3: free rent on the pages — the New lease Terms row and Summary, the Edit lease
"Free rent" card and its Changes line, the LEASE card row, and the P&L's and Revenue report's
concessions (docs/qa/15/PLAN-015d §3 (2))."""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.forms import LeaseCreateForm
from ledger.models import AuditLog, FreeRentFrom, InvoiceDraft, Lease, RentChargeStatus
from ledger.services import free_rent, invoice_builder, leases, pnl, reports
from tests import test_new_lease_page as new_lease
from tests.test_new_lease_page import create_data, page, post_create, tag_with, vacant_store
from tests.test_operator_views import client_for
from tests.test_pnl import a_lease, a_store, rent

pytestmark = pytest.mark.django_db

D = Decimal
REPO = Path(__file__).resolve().parents[1]
today = new_lease.today
admin = new_lease.admin
MAY = date(2026, 5, 1)
JUNE = date(2026, 6, 1)


# --- New lease ------------------------------------------------------------------------------


def test_the_terms_card_has_the_free_rent_row_and_the_summary_its_hidden_line(
    admin: Client, today: date
) -> None:
    html_text = page(admin, new_lease.create_path(vacant_store()))
    terms = html_text[html_text.index('id="terms-step"') : html_text.index('id="collection-step"')]
    assert 'id="free-rent-row"' in terms
    assert tag_with(terms, 'name="free_rent_days"', 'type="number"', 'min="0"', 'max="366"')
    assert tag_with(terms, 'name="free_rent_from"')
    assert ">Move-in date</option>" in terms and ">1st of next month</option>" in terms
    assert "Free rent (days)" in terms and "Free rent starts" in terms
    assert tag_with(html_text, 'id="summary-free-rent-row"', "hidden")


def test_the_summary_nets_the_first_months_free_rent(today: date) -> None:
    store = vacant_store()
    form = LeaseCreateForm(
        create_data(store, None, started_on="2026-11-15", free_rent_days="10"), today=today
    )
    form.is_valid()
    summary = form.summary()
    # Nov 15–30: 16 of 30 days of $2,000.00 = $1,066.67; 10 of those 16 days free.
    assert summary["first_month"] == "$1,066.67"
    assert summary["free_rent"] == "-$666.67"
    assert summary["total"] == "$400.00"
    later = LeaseCreateForm(
        create_data(store, None, free_rent_days="30", free_rent_from=FreeRentFrom.NEXT_MONTH),
        today=today,
    )
    later.is_valid()
    assert later.summary()["free_rent"] == ""


def test_the_summary_agrees_with_the_concession_the_charge_gets(today: date) -> None:
    lease = a_lease(a_store("Store 9801"), started_on=date(2026, 11, 15), free_rent_days=10)
    leases_rent = D("2000.00")
    from ledger.models import LeaseRentHistory, RentSource

    LeaseRentHistory.objects.create(
        lease=lease, amount=leases_rent, effective_from=lease.started_on, source=RentSource.STAFF
    )
    first = D("1066.67")
    assert free_rent.concession_for(lease, date(2026, 11, 1), first)[0] == (
        free_rent.first_month_concession(
            lease.started_on, 10, FreeRentFrom.MOVE_IN, first, prorate=True
        )
    )


def test_a_lease_saved_with_free_rent_keeps_it_audits_it_and_its_draft_shows_the_discount(
    admin: Client, today: date
) -> None:
    store = vacant_store()
    operator = new_lease.an_operator_with("Free Days Ltd", email="free@example.com")
    response = post_create(
        admin, store, create_data(store, operator, free_rent_days="10", free_rent_from="move_in")
    )
    assert response.status_code == 302
    lease = Lease.objects.get(store=store)
    assert (lease.free_rent_days, lease.free_rent_from) == (10, FreeRentFrom.MOVE_IN)
    audit = AuditLog.objects.get(action=leases.FREE_RENT_SET_ACTION, entity_id=str(lease.pk))
    assert audit.after == {"free_rent_days": 10, "free_rent_from": "move_in"}
    draft = InvoiceDraft.objects.get(lease=lease)
    preview = invoice_builder.preview(draft, today)
    assert [(line.item, line.amount) for line in preview.discounts] == [
        ("Free rent per lease agreement (Nov 15 – Nov 24)", D("666.67"))
    ]
    assert preview.total == D("400.00")


@pytest.mark.parametrize("days", ["-1", "367", "ten"])
def test_free_rent_days_outside_0_to_366_are_refused_and_nothing_is_saved(
    admin: Client, today: date, days: str
) -> None:
    store = vacant_store()
    operator = new_lease.an_operator_with("Refused Days", email="refused@example.com")
    post_create(admin, store, create_data(store, operator, free_rent_days=days))
    assert not Lease.objects.filter(store=store).exists()


def test_an_empty_free_rent_is_none(admin: Client, today: date) -> None:
    store = vacant_store()
    operator = new_lease.an_operator_with("No Free Days", email="none@example.com")
    post_create(admin, store, create_data(store, operator, free_rent_days=""))
    lease = Lease.objects.get(store=store)
    assert (lease.free_rent_days, lease.free_rent_from) == (0, FreeRentFrom.MOVE_IN)
    assert not AuditLog.objects.filter(action=leases.FREE_RENT_SET_ACTION).exists()


def test_the_script_keeps_the_free_rent_line_live() -> None:
    script = (REPO / "ledger/static/ledger/new_lease.js").read_text()
    for needle in ('byId("id_free_rent_days")', '"summary-free-rent-row"', '"next_month"'):
        assert needle in script


# --- Edit lease -----------------------------------------------------------------------------


def an_edited_lease(**fields: object) -> Lease:
    from tests.receivables_helpers import a_lease as active_lease
    from tests.receivables_helpers import an_operator

    lease = active_lease(a_store("Store 9802"), an_operator("Edit Free Rent"))
    if fields:
        Lease.objects.filter(pk=lease.pk).update(**fields)
    return Lease.objects.get(pk=lease.pk)


def test_the_edit_page_has_the_free_rent_card_after_rent_with_its_changes_words(
    admin: Client,
) -> None:
    lease = an_edited_lease(free_rent_days=30)
    html_text = page(admin, f"/leases/{lease.pk}/edit/")
    order = [html_text.index(f'id="{card}"') for card in ("rent-card", "free-rent-card")]
    assert order == sorted(order)
    assert order[1] < html_text.index('id="collection-card"')
    assert tag_with(
        html_text,
        'name="free_rent_days"',
        'data-initial="30"',
        'data-label="Free rent"',
        'data-suffix=" days"',
    )
    assert tag_with(html_text, 'name="free_rent_from"', 'data-initial="move_in"')
    script = (REPO / "ledger/static/ledger/edit_lease.js").read_text()
    assert 'control.type === "number"' in script and '"data-suffix"' in script


def test_an_edit_saves_the_free_rent_and_warns_about_billed_months(admin: Client) -> None:
    lease = an_edited_lease(started_on=date(2026, 1, 1))
    billed = rent(lease, date(2026, 1, 1), "3000.00")
    from ledger.models import RentCharge

    RentCharge.objects.filter(pk=billed.pk).update(
        amount_paid=D("3000.00"), status=RentChargeStatus.PAID
    )
    data = {
        "debit_day": str(lease.debit_day),
        "payment_channel": lease.payment_channel,
        "rent": "",
        "free_rent_days": "45",
        "free_rent_from": "move_in",
    }
    response = admin.post(f"/leases/{lease.pk}/edit/", data, follow=True)
    lease.refresh_from_db()
    assert (lease.free_rent_days, lease.free_rent_from) == (45, "move_in")
    words = [str(message) for message in response.context["messages"]]
    assert "Jan 2026 already billed: it keeps the free rent it was billed with." in words


def test_viewers_cannot_edit_a_leases_free_rent() -> None:
    lease = an_edited_lease()
    viewer = client_for(Role.VIEWER, "viewer-free-rent")[0]
    viewer.post(f"/leases/{lease.pk}/edit/", {"free_rent_days": "30"})
    lease.refresh_from_db()
    assert lease.free_rent_days == 0


# --- the LEASE card -------------------------------------------------------------------------


def test_the_lease_card_shows_the_free_rent_row_only_with_free_days(admin: Client) -> None:
    lease = an_edited_lease(free_rent_days=30, started_on=date(2026, 1, 10))
    html_text = page(admin, f"/stores/{lease.store_id}/")
    assert '<dt>Free rent</dt><dd id="header-free-rent">30 days, Jan 10 – Feb 8</dd>' in html_text
    Lease.objects.filter(pk=lease.pk).update(free_rent_days=0)
    assert 'id="header-free-rent"' not in page(admin, f"/stores/{lease.store_id}/")


# --- P&L and Revenue ---------------------------------------------------------------------------


def _total(month_from: date, month_to: date) -> pnl.Figures:
    return pnl.total(pnl.rows(pnl.PnlQuery(month_from, month_to)))


def test_pnl_shows_billed_concessions_and_net_and_the_billed_margin_is_net() -> None:
    lease = a_lease(a_store("Store 9803"))
    rent(lease, MAY, "3000.00", concession=D("1000.00"))
    rent(lease, JUNE, "3000.00")
    total = _total(MAY, JUNE)
    assert (total.rent_billed, total.concessions, total.net_rent) == (
        D("6000.00"),
        D("1000.00"),
        D("5000.00"),
    )


def test_pnl_billed_concessions_and_net_reconcile_with_the_revenue_report() -> None:
    lease = a_lease(a_store("Store 9804"))
    rent(lease, MAY, "3000.00", concession=D("1000.00"))
    rent(lease, JUNE, "2500.00", concession=D("2500.00"), status=RentChargeStatus.PAID)
    rent(lease, JUNE, "1200.00", seq=2, concession=D("100.00"), status=RentChargeStatus.VOID)
    revenue = reports.revenue_by_month(reports.ReportRange(MAY, JUNE))
    total = _total(MAY, JUNE)
    assert revenue.total.rent_billed == total.rent_billed == D("5500.00")
    assert revenue.total.concessions == total.concessions == D("3500.00")
    assert revenue.total.rent_billed - revenue.total.concessions == total.net_rent
    assert revenue.total.total_billed == total.net_rent + total.fees_billed


def test_the_csvs_carry_the_concessions_columns(admin: Client) -> None:
    lease = a_lease(a_store("Store 9805"))
    rent(lease, MAY, "3000.00", concession=D("1000.00"))
    query = {"from": "2026-05", "to": "2026-05"}
    response = admin.get("/reports/pnl/export.csv", query)
    body = b"".join(response.streaming_content).decode().lstrip("\ufeff")
    pnl_rows = list(csv.reader(io.StringIO(body)))
    header = pnl_rows[0]
    assert header[4:8] == [
        "Rent billed (USD)",
        "Concessions (USD)",
        "Net rent (USD)",
        "Rent collected (USD)",
    ]
    assert pnl_rows[-1][4:7] == ["3000.00", "1000.00", "2000.00"]
    assert reports.REVENUE_CSV_HEADER[1:4] == (
        "Rent billed (USD)",
        "Concessions (USD)",
        "Rent collected (USD)",
    )


def test_the_pages_show_the_concessions_columns(admin: Client) -> None:
    lease = a_lease(a_store("Store 9806"))
    rent(lease, MAY, "3000.00", concession=D("1000.00"))
    query = "?from=2026-05&to=2026-05"
    pnl_page = page(admin, "/reports/pnl/" + query)
    assert re.search(
        r'<th scope="col" class="money">Concessions</th><th scope="col" class="money">Net</th>',
        pnl_page,
    )
    assert 'data-label="Concessions"' in pnl_page and 'data-label="Net rent"' in pnl_page
    revenue_page = page(admin, "/reports/revenue/" + query)
    assert '<th class="money">Concessions</th>' in revenue_page
    assert "$1,000.00" in revenue_page
