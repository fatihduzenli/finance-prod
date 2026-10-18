"""The home page (docs/qa/PLAN-011-dashboard-reports-mobile.md D1, D10): every figure from
``ledger.services.receivables``, each linking to its list, every role, and a bounded
number of queries."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import (
    CollectionStatus,
    FeeType,
    InvoiceStatus,
    Lease,
    LeaseRentHistory,
    LegacyStatus,
    OneOffCategory,
    OneOffChargeStatus,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    RentSource,
    Store,
    StoreStatus,
)
from ledger.services import receivables, rent_run
from payments import invoice_sync
from payments.models import ReconciliationRun, ReconciliationStatus, ReconciliationTrigger
from tests.auth_helpers import current_code, make_user, password_step, redirect_target, token_step
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_payment,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_invoice,
    an_operator,
    link_provider_invoice,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 6)
SEP = date(2026, 9, 1)
OCT = date(2026, 10, 1)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def build_mix() -> dict[str, object]:
    """One of every kind of open and closed charge, with known amounts."""
    operator = an_operator("Acme Stores")
    lease = a_lease(a_store("2204 FOREST PARK", 2204), operator, debit_day=5)
    a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, "3500.00")  # overdue
    a_rent_charge(lease, OCT, RentChargeStatus.PARTIAL, "3500.00", "1200.00")  # overdue 2300
    paid = a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.PAID, "3500.00", "3500.00")
    a_payment(paid, "3500.00", date(2026, 8, 7))  # last month: not this month
    a_rent_charge(lease, date(2026, 7, 1), RentChargeStatus.WAIVED, "3500.00")  # not owed
    second = a_lease(a_store("3409 LEICESTER", 3409), an_operator("Beta"), debit_day=6)
    pending = a_rent_charge(second, OCT, RentChargeStatus.PENDING, "1850.00", day=6)
    a_payment(pending, "1850.00", None, PaymentStatus.PENDING, initiated_on=TODAY)
    future = a_rent_charge(second, date(2026, 11, 1), RentChargeStatus.DUE, "1850.00", day=6)
    # Insurance: 695 owed, 595 partly paid (295 left), a legacy paid one, one without premium.
    an_insurance_charge(lease, 2025, date(2025, 9, 1))
    partly = an_insurance_charge(lease, 2026, date(2026, 9, 1), "595.00")
    Payment.objects.create(
        insurance_charge=partly,
        kind=PaymentKind.INSURANCE,
        policy_year=2026,
        method=PaymentMethod.ACH,
        amount=D("300.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 10, 2),
    )
    an_insurance_charge(
        second, 2024, date(2024, 9, 1), legacy=True, legacy_status=LegacyStatus.PAID
    )
    an_insurance_charge(second, 2026, date(2026, 9, 1), None)
    # One-off: owed 400 (overdue), a waived and a voided one, and a deposit.
    a_one_off_charge(operator, "400.00", date(2026, 9, 20), lease=lease)
    a_one_off_charge(
        operator,
        "999.00",
        date(2026, 9, 20),
        status=OneOffChargeStatus.WAIVED,
        waived_at=datetime(2026, 9, 21, tzinfo=UTC),
        waive_note="Goodwill.",
    )
    a_one_off_charge(
        operator,
        "888.00",
        date(2026, 9, 20),
        status=OneOffChargeStatus.VOIDED,
        voided_at=datetime(2026, 9, 21, tzinfo=UTC),
        void_note="Duplicate.",
    )
    deposit_type = FeeType.objects.create(code="deposit-x", name="Deposit", refundable=True)
    deposit = a_one_off_charge(
        operator,
        "2000.00",
        date(2026, 9, 1),
        lease=lease,
        category=OneOffCategory.SETUP,
        fee_type=deposit_type,
        description="",
        collection="none",
    )
    # Settled this month: a rent payment, less a refund; the deposit payment left out.
    october = a_rent_charge(
        a_lease(a_store("5300 GIBSONTON", 5300), operator, debit_day=2),
        OCT,
        RentChargeStatus.PAID,
        "3831.25",
        "3831.25",
        day=2,
    )
    original = a_payment(october, "3831.25", date(2026, 10, 3))
    Payment.objects.create(
        rent_charge=october,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("31.25"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 10, 4),
        is_refund=True,
        parent_payment=original,
    )
    Payment.objects.create(
        one_off_charge=deposit,
        kind=PaymentKind.ONE_OFF,
        method=PaymentMethod.WIRE,
        amount=D("2000.00"),
        status=PaymentStatus.SETTLED,
        settled_on=date(2026, 10, 1),
        note="Deposit by wire.",
    )
    return {"operator": operator, "lease": lease, "future": future}


# Open: rent 3500 + 2300 + 1850 + 1850 (November, not overdue); insurance 695 + 295;
# one-off 400. Overdue: everything due before 2026-10-06 except the November charge and
# the pending October one is due on the 6th (not before today).
OUTSTANDING = D("3500.00") + D("2300.00") + D("1850.00") + D("1850.00") + D("695.00")
OUTSTANDING += D("295.00") + D("400.00")
OVERDUE = D("3500.00") + D("2300.00") + D("695.00") + D("295.00") + D("400.00")
PAID_THIS_MONTH = D("3831.25") - D("31.25") + D("300.00")


def test_open_balances_is_one_row_per_open_charge(today: date) -> None:
    build_mix()
    rows = receivables.open_balances(today)
    assert len(rows) == 7
    assert receivables.total(rows) == OUTSTANDING
    kinds = sorted(row.kind for row in rows)
    assert kinds == ["insurance", "insurance", "one_off", "rent", "rent", "rent", "rent"]
    insurance = sorted(row.balance for row in rows if row.kind == "insurance")
    assert insurance == [D("295.00"), D("695.00")]
    assert all(isinstance(row.balance, Decimal) for row in rows)


def test_the_dashboard_figures(today: date) -> None:
    build_mix()
    data = receivables.dashboard(today)
    assert (data.outstanding, data.outstanding_count) == (OUTSTANDING, 7)
    assert (data.overdue, data.overdue_count) == (OVERDUE, 5)
    assert data.paid_this_month == PAID_THIS_MONTH
    assert (data.month_start, data.month_end) == (OCT, date(2026, 10, 31))


def test_paid_this_month_counts_the_last_day_and_not_the_next(today: date) -> None:
    lease = a_lease()
    last = a_rent_charge(lease, OCT, RentChargeStatus.PAID, "100.00", "100.00")
    a_payment(last, "100.00", date(2026, 10, 31))
    after = a_rent_charge(lease, date(2026, 11, 1), RentChargeStatus.PAID, "7.00", "7.00")
    a_payment(after, "7.00", date(2026, 11, 1))
    before = a_rent_charge(lease, SEP, RentChargeStatus.PAID, "9.00", "9.00")
    a_payment(before, "9.00", date(2026, 9, 30))
    assert receivables.dashboard(today).paid_this_month == D("100.00")


def test_the_page_shows_every_figure_with_its_link(today: date) -> None:
    build_mix()
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    assert "$10,890.00" in page  # outstanding
    assert "$7,190.00" in page  # overdue
    assert "$4,100.00" in page  # paid this month
    assert 'href="/arrears/"' in page
    assert "/payments/?date_from=2026-10-01&amp;date_to=2026-10-31&amp;status=settled" in page
    assert 'href="/run/"' in page and 'href="/returns/"' in page
    assert '<p id="last-reconciliation" class="muted">No nightly check has run yet.</p>' in page


def test_invoices_not_sent_need_a_provider_reference(today: date) -> None:
    operator = an_operator("Gamma")
    draft = an_invoice(operator, 1, internal_status=InvoiceStatus.DRAFT)
    link_provider_invoice(draft, "inv-ext-1")
    ready = an_invoice(operator, 2, internal_status=InvoiceStatus.READY)
    link_provider_invoice(ready, "inv-ext-2")
    an_invoice(operator, 3, internal_status=InvoiceStatus.DRAFT)  # local only
    sent = an_invoice(operator, 4, internal_status=InvoiceStatus.SENT)
    link_provider_invoice(sent, "inv-ext-4")
    data = receivables.dashboard(today)
    assert data.not_sent_count == 2
    assert [invoice.invoice_number for invoice in data.not_sent] == [
        "INV-2026-0001",
        "INV-2026-0002",
    ]


def test_failed_attempts_list_attempted_invoices_with_operator_and_store(today: date) -> None:
    operator = an_operator("Delta Retail")
    lease = a_lease(a_store("1444 JACKSONVILLE", 1444), operator)
    charge = a_rent_charge(lease, SEP)
    failed = an_invoice(operator, 7, "3740.00", rent_charge=charge)
    invoice_sync.record_provider_status(failed.pk, "ATTEMPTED")
    other = an_invoice(operator, 8)
    invoice_sync.record_provider_status(other.pk, "SENT")
    data = receivables.dashboard(today)
    assert data.failed_attempts_count == 1
    assert [invoice.pk for invoice in data.failed_attempts] == [failed.pk]
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    assert "Failed payment attempts: 1" in page
    assert "INV-2026-0007" in page and "Delta Retail" in page and "1444 JACKSONVILLE" in page
    assert "$3,740.00" in page
    assert f'href="/charges/{charge.pk}/"' in page
    assert page.index("Failed payment attempts") < page.index("Total outstanding")


def test_the_run_summary_uses_the_run_and_returns_functions(today: date) -> None:
    due_today = a_lease(debit_day=6)
    a_rent_charge(due_today, OCT, RentChargeStatus.PENDING, day=6)
    a_lease(debit_day=6)  # due today, charge not created yet
    a_lease(debit_day=7)  # tomorrow
    returned = a_rent_charge(a_lease(debit_day=2), OCT, RentChargeStatus.UNPAID, day=2)
    payment = a_payment(returned, "3500.00", None, PaymentStatus.RETURNED, initiated_on=OCT)
    assert payment.status == PaymentStatus.RETURNED
    summary = receivables.dashboard(today).run
    assert (summary.due, summary.pending, summary.returned) == (2, 1, 1)
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    assert '<strong id="run-due">2</strong>' in page
    assert '<strong id="run-pending">1</strong>' in page
    assert '<strong id="run-returned">1</strong>' in page


def test_the_last_reconciliation_is_the_latest_run_that_was_not_skipped(today: date) -> None:
    ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.SCHEDULED,
        status=ReconciliationStatus.DONE,
        started_at=datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
    )
    ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.SCHEDULED,
        status=ReconciliationStatus.SKIPPED,
        started_at=datetime(2026, 10, 6, 6, 0, tzinfo=UTC),
    )
    data = receivables.dashboard(today)
    assert data.last_reconciliation is not None
    assert data.last_reconciliation.status == ReconciliationStatus.DONE
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    assert "Done, started 2026-10-05 02:00" in page and 'href="/reconciliation/"' in page


def test_an_empty_database_shows_zeroes(today: date) -> None:
    data = receivables.dashboard(today)
    assert (data.outstanding, data.overdue, data.paid_this_month) == (D("0"), D("0"), D("0"))
    assert (data.outstanding_count, data.not_sent_count, data.failed_attempts_count) == (0, 0, 0)
    assert data.waived_live == []


# --- Roles and routing (D1, D10) -------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING, Role.ADMIN])
def test_every_role_reads_the_dashboard_and_none_may_post(today: date, role: Role) -> None:
    client, _ = client_for(role)
    response = client.get("/")
    assert response.status_code == 200
    assert '<a href="/grid/" id="nav-grid">Monthly rent overview</a>' in response.content.decode()
    assert client.post("/").status_code == 403


def test_the_nav_lists_dashboard_stores_operators_and_the_overview_first() -> None:
    # PLAN-012c D3: Dashboard, Stores, Operators, Monthly rent overview, then the groups;
    # System settings (the Django admin) is listed for an admin only.
    client, _ = client_for(Role.VIEWER)
    page = client.get("/").content.decode()
    nav = page[page.index('<nav aria-label="Main">') : page.index("</nav>")]
    order = [nav.index(f">{label}</a>") for label in ("Dashboard", "Stores", "Operators")]
    order += [nav.index('id="nav-grid"'), nav.index('id="nav-reports"'), nav.index(">Admin<")]
    assert order == sorted(order)
    assert 'href="/admin/"' not in nav
    admin_nav = client_for(Role.ADMIN)[0].get("/").content.decode()
    assert '<a href="/admin/" id="nav-django-admin">System settings</a>' in admin_nav


def test_a_user_without_a_role_or_a_session_cannot_read_it() -> None:
    client, _ = client_for(None)
    assert client.get("/").status_code == 403
    assert Client().get("/").status_code == 401


def test_sign_in_lands_on_the_dashboard(client: Client) -> None:
    _, device = make_user("dana", Role.VIEWER)
    assert device is not None
    password_step(client, "dana")
    response = token_step(client, current_code(device))
    assert response.status_code == 302
    assert redirect_target(response) == "/"
    assert client.get("/").status_code == 200


# --- Bounded queries (D6) ---------------------------------------------------------------------


def _queries(today: date) -> int:
    with CaptureQueriesContext(connection) as captured:
        receivables.dashboard(today)
    return len(captured.captured_queries)


def _more_rows(start: int, count: int) -> None:
    """Charges, invoices and payments on leases not debited today."""
    for n in range(start, start + count):
        operator = an_operator(f"Bulk {n}")
        lease = a_lease(a_store(number=7000 + n), operator, debit_day=20)
        unpaid = a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, day=20)
        paid = a_rent_charge(lease, OCT, RentChargeStatus.PAID, "3500.00", "3500.00", day=2)
        a_payment(paid, "3500.00", date(2026, 10, 3))
        an_insurance_charge(lease, 2026, date(2026, 9, 1))
        a_one_off_charge(operator, "50.00", date(2026, 9, 1))
        invoice = an_invoice(operator, 100 + n, rent_charge=unpaid)
        link_provider_invoice(invoice, f"inv-bulk-{n}")
        invoice_sync.record_provider_status(invoice.pk, "ATTEMPTED")


def test_the_dashboard_query_count_does_not_grow_with_the_data(today: date) -> None:
    build_mix()
    _more_rows(0, 2)
    small = _queries(today)
    _more_rows(2, 20)
    assert _queries(today) == small
    assert small <= 15


def _leases_due_today(count: int) -> None:
    """``count`` active leases debited today (the 6th), each with this month's charge, every
    third one pending; built in bulk so the test stays fast."""
    operator = an_operator("Due today")
    stores = Store.objects.bulk_create(
        Store(
            external_label=f"due-{n}", store_number=9000 + n, state="GA", status=StoreStatus.ACTIVE
        )
        for n in range(count)
    )
    leases = Lease.objects.bulk_create(
        Lease(
            store=store,
            operator=operator,
            debit_day=6,
            collection_status=CollectionStatus.ACTIVE,
            payment_channel=PaymentChannel.PROVIDER_ACH,
            started_on=date(2025, 1, 1),
        )
        for store in stores
    )
    LeaseRentHistory.objects.bulk_create(
        LeaseRentHistory(
            lease=lease,
            amount=D("3500.00"),
            effective_from=date(2025, 1, 1),
            source=RentSource.STAFF,
        )
        for lease in leases
    )
    RentCharge.objects.bulk_create(
        RentCharge(
            lease=lease,
            period_month=OCT,
            seq=1,
            due_on=TODAY,
            amount_due=D("3500.00"),
            amount_paid=D("0.00"),
            status=RentChargeStatus.PENDING if n % 3 == 0 else RentChargeStatus.UNPAID,
        )
        for n, lease in enumerate(leases)
    )


def test_the_run_summary_query_count_holds_with_450_leases_due_today(today: date) -> None:
    """A debit day with 450 leases due: the home page's run summary stays a handful of
    grouped queries (QA review of Batch B: ``leases_due`` ran three per lease)."""
    build_mix()
    small = _queries(today)
    before = receivables.run_summary(today)
    _leases_due_today(450)
    with CaptureQueriesContext(connection) as captured:
        summary = receivables.run_summary(today)
    assert len(captured.captured_queries) == 3
    assert _queries(today) == small
    assert (summary.due, summary.pending) == (before.due + 450, before.pending + 150)


def _counts_from_the_run_page(day: date) -> tuple[int, int]:
    run = rent_run.leases_due(day)
    pending = sum(
        1
        for item in run.due
        if item.charge is not None and item.charge.status == RentChargeStatus.PENDING
    )
    return len(run.due), pending


@pytest.mark.parametrize(
    "day",
    [date(2026, 10, 6), date(2026, 9, 30), date(2026, 2, 28), date(2026, 10, 15)],
    ids=["6th", "30th-of-a-30-day-month", "28th-of-february", "15th"],
)
def test_the_grouped_counts_match_the_run_page(day: date) -> None:
    """Every skip reason and the month-end rule: the dashboard counts what the run page
    lists, on the same rows."""
    period = day.replace(day=1)
    for debit_day in (day.day, 29, 30, 31):
        a_rent_charge(a_lease(debit_day=debit_day), period, RentChargeStatus.PENDING, day=day.day)
        a_lease(debit_day=debit_day)
    twice = a_lease(debit_day=1, second_debit_day=day.day)
    a_rent_charge(twice, period, RentChargeStatus.PENDING, seq=2, day=day.day)
    a_rent_charge(twice, period, RentChargeStatus.PENDING, seq=1, day=1)  # not due today
    a_lease(debit_day=day.day, status=CollectionStatus.PAUSED)
    a_lease(debit_day=day.day, status=CollectionStatus.ENDED)
    a_lease(debit_day=day.day, started=date(2027, 1, 1))
    a_lease(debit_day=day.day, channel=PaymentChannel.UNKNOWN, status=CollectionStatus.PAUSED)
    no_rent = a_lease(debit_day=day.day)
    no_rent.rent_history.update(effective_from=day + timedelta(days=1))
    ended_rent = a_lease(debit_day=day.day, started=date(2025, 1, 1))
    ended_rent.rent_history.update(effective_to=day - timedelta(days=1))
    last_month = a_lease(debit_day=day.day)
    a_rent_charge(last_month, (period - timedelta(days=1)).replace(day=1), RentChargeStatus.PENDING)
    counts = rent_run.due_counts(day)
    assert (counts.due, counts.pending) == _counts_from_the_run_page(day)
    assert counts.due > 0 and counts.pending > 0
