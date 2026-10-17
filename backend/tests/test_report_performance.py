"""Performance on volume (docs/qa/PLAN-011-dashboard-reports-mobile.md D6): with 10,000
invoices, 12,000 rent charges and their payments over 500 stores and 24 months, each of
the dashboard, the grid (500 stores x 24 months), arrears, aging, revenue, payments by
method, deposits and one statement answers in under 2 s on PostgreSQL, with a bounded
number of queries. The rows are written in bulk and the planner's statistics refreshed
(``maintenance.analyze``), as after an import. PostgreSQL only: the bound is a property
of the production engine; the query bounds are checked on both engines elsewhere.

Run with ``-s`` to see the measured times."""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import (
    CollectionStatus,
    InsuranceCharge,
    Invoice,
    InvoiceStatus,
    Lease,
    LeaseRentHistory,
    OneOffCharge,
    Operator,
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
from ledger.services import maintenance
from ledger.services.pnl import add_months
from payments.models import Deposit
from payments.sync_writer import provider_write
from tests.test_operator_views import client_for

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(connection.vendor != "postgresql", reason="timed on PostgreSQL only"),
]

D = Decimal
TODAY = date(2026, 10, 6)
FIRST_MONTH = date(2024, 11, 1)
MONTHS = 24
STORES = 500
OPERATORS = 250
INVOICES = 10_000
LIMIT_SECONDS = 2.0
STATES = ("GA", "FL", "TX", "AL", "SC", "NC", "TN")


def _status(store: int, month: int) -> str:
    """Mostly paid; the last months of some stores unpaid, partial or pending."""
    if month < MONTHS - 3:
        return RentChargeStatus.PAID
    return (
        RentChargeStatus.PAID,
        RentChargeStatus.UNPAID,
        RentChargeStatus.PARTIAL,
        RentChargeStatus.PENDING,
        RentChargeStatus.DUE,
    )[(store + month) % 5]


def build_volume() -> Operator:
    operators = Operator.objects.bulk_create(
        [Operator(display_name=f"Volume Operator {n:03d}") for n in range(OPERATORS)]
    )
    stores = Store.objects.bulk_create(
        [
            Store(
                external_label=f"Volume Store {n:03d}",
                store_number=10_000 + n,
                state=STATES[n % len(STATES)],
                status=StoreStatus.ACTIVE,
            )
            for n in range(STORES)
        ]
    )
    leases = Lease.objects.bulk_create(
        [
            Lease(
                store=store,
                operator=operators[n % OPERATORS],
                debit_day=1 + n % 28,
                collection_status=CollectionStatus.ACTIVE,
                payment_channel=PaymentChannel.PROVIDER_ACH if n % 10 else PaymentChannel.WIRE,
                started_on=FIRST_MONTH,
            )
            for n, store in enumerate(stores)
        ]
    )
    LeaseRentHistory.objects.bulk_create(
        [
            LeaseRentHistory(
                lease=lease,
                amount=D(2000 + n * 7 % 3000),
                effective_from=FIRST_MONTH,
                source=RentSource.STAFF,
            )
            for n, lease in enumerate(leases)
        ]
    )
    charges = []
    for n, lease in enumerate(leases):
        amount = D(2000 + n * 7 % 3000) + D(n % 100) / D(100)
        for m in range(MONTHS):
            month = add_months(FIRST_MONTH, m)
            status = _status(n, m)
            paid = {
                str(RentChargeStatus.PAID): amount,
                str(RentChargeStatus.PARTIAL): amount / 2,
            }.get(status, D("0.00"))
            charges.append(
                RentCharge(
                    lease=lease,
                    period_month=month,
                    due_on=month.replace(day=lease.debit_day or 1),
                    amount_due=amount,
                    amount_paid=paid.quantize(D("0.01")),
                    status=status,
                )
            )
    charges = RentCharge.objects.bulk_create(charges, batch_size=2_000)
    operator_of = {lease.pk: operators[n % OPERATORS] for n, lease in enumerate(leases)}
    Invoice.objects.bulk_create(
        [
            Invoice(
                invoice_number=f"INV-2026-{n + 1:05d}",
                operator=operator_of[charge.lease_id],
                rent_charge=charge,
                issue_date=charge.due_on,
                due_date=charge.due_on,
                subtotal=charge.amount_due,
                total=charge.amount_due,
                internal_status=InvoiceStatus.SENT,
            )
            for n, charge in enumerate(charges[:INVOICES])
        ],
        batch_size=2_000,
    )
    with provider_write():  # as the sync layer records what the provider returned
        Invoice.objects.update(provider_status="PAID")
        Invoice.objects.filter(invoice_number__endswith="00").update(provider_status="ATTEMPTED")
    payments = []
    for charge in charges:
        if charge.status in (RentChargeStatus.PAID, RentChargeStatus.PARTIAL):
            payments.append(
                Payment(
                    rent_charge=charge,
                    kind=PaymentKind.RENT,
                    method=PaymentMethod.ACH,
                    amount=charge.amount_paid,
                    status=PaymentStatus.SETTLED,
                    initiated_on=charge.due_on,
                    settled_on=charge.due_on.replace(day=min(charge.due_on.day + 2, 28)),
                )
            )
        elif charge.status == RentChargeStatus.PENDING:
            payments.append(
                Payment(
                    rent_charge=charge,
                    kind=PaymentKind.RENT,
                    method=PaymentMethod.ACH,
                    amount=charge.amount_due,
                    status=PaymentStatus.PENDING,
                    initiated_on=charge.due_on,
                )
            )
    Payment.objects.bulk_create(payments, batch_size=2_000)
    InsuranceCharge.objects.bulk_create(
        [
            InsuranceCharge(
                lease=lease,
                policy_year=2026,
                due_on=date(2026, 1 + n % 12, 1),
                amount=D("695.00"),
                needs_premium=False,
            )
            for n, lease in enumerate(leases)
        ]
    )
    OneOffCharge.objects.bulk_create(
        [
            OneOffCharge(
                operator_id=lease.operator_id,
                lease=lease,
                category="damage",
                collection="manual",
                description="Volume damage",
                price=D("125.00"),
                due_date=date(2026, 1 + n % 9, 15),
            )
            for n, lease in enumerate(leases)
        ]
    )
    Deposit.objects.bulk_create(
        [
            Deposit(
                provider="stax_pay",
                provider_deposit_id=f"volume-deposit-{n:05d}",
                deposit_date=add_months(FIRST_MONTH, n % MONTHS).replace(day=1 + n % 28),
                gross=D("50000.00"),
                fees=D("125.00"),
                net=D("49875.00"),
                status="settled",
            )
            for n in range(MONTHS * 22)
        ]
    )
    maintenance.analyze(
        Operator,
        Store,
        Lease,
        LeaseRentHistory,
        RentCharge,
        Invoice,
        Payment,
        InsuranceCharge,
        OneOffCharge,
        Deposit,
    )
    return operators[0]


def test_every_page_answers_under_two_seconds_on_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    operator = build_volume()
    assert RentCharge.objects.count() == STORES * MONTHS == 12_000
    assert Invoice.objects.count() == INVOICES
    span = {"from": f"{FIRST_MONTH:%Y-%m}", "to": f"{TODAY:%Y-%m}"}
    # The run summary reuses rent_run.leases_due, which reads 3 rows per lease due today
    # (rent in effect, charges, carried balance): bounded by the leases due on one day,
    # not by the data. Reported to QA; rent_run is outside this batch.
    due_today = Lease.objects.filter(debit_day=TODAY.day).count()
    pages: dict[str, tuple[str, dict[str, str], int]] = {
        "dashboard": ("/", {}, 25 + 3 * due_today),
        "grid (500 x 24)": ("/grid/", span, 20),
        # PLAN-012d D16: the words filter keeps the grid's query count.
        "grid filtered": ("/grid/", {**span, "q": "store 1"}, 20),
        "arrears": ("/arrears/", {}, 20),
        "aging": ("/reports/aging/", {}, 20),
        "revenue": ("/reports/revenue/", span, 30),
        "payments by method": ("/reports/payments-by-method/", span, 20),
        "deposits": ("/reports/deposits/", span, 20),
        "statement": (f"/operators/{operator.pk}/statement/", span, 20),
    }
    client, _ = client_for(Role.VIEWER)
    client.get("/reports/")  # warm the session and the template loaders
    measured: dict[str, tuple[float, int]] = {}
    for name, (path, params, _) in pages.items():
        with CaptureQueriesContext(connection) as captured:
            started = time.perf_counter()
            response = client.get(path, params)
            elapsed = time.perf_counter() - started
        assert response.status_code == 200, name
        assert "too long" not in response.content.decode(), name
        measured[name] = (elapsed, len(captured.captured_queries))
    for name, (elapsed, queries) in measured.items():
        print(f"PLAN-011 D6 {name}: {elapsed:.3f} s, {queries} queries")  # noqa: T201
    for name, (_, _, query_bound) in pages.items():
        assert measured[name][1] <= query_bound, (name, measured[name])
    slow = {name: value for name, value in measured.items() if value[0] >= LIMIT_SECONDS}
    assert not slow, slow


def test_the_company_settings_add_one_query_to_a_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLAN-012c D2: the shell's company settings cost one query a page, the row read once
    (no create, no savepoint) and no logo lookup while the row names none. Each total is
    this file's figure before the shell read the row, plus one."""
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    operator = build_volume()
    span = {"from": f"{FIRST_MONTH:%Y-%m}", "to": f"{TODAY:%Y-%m}"}
    pages: dict[str, tuple[str, dict[str, str], int]] = {
        "grid (500 x 24)": ("/grid/", span, 18 + 1),
        "revenue": ("/reports/revenue/", span, 29 + 1),
        "arrears": ("/arrears/", {}, 18 + 1),
        "statement": (f"/operators/{operator.pk}/statement/", span, 19 + 1),
    }
    client, _ = client_for(Role.VIEWER)
    client.get("/reports/")  # warm the session and the template loaders
    measured: dict[str, int] = {}
    for name, (path, params, _) in pages.items():
        with CaptureQueriesContext(connection) as captured:
            response = client.get(path, params)
        assert response.status_code == 200, name
        measured[name] = len(captured.captured_queries)
        print(f"PLAN-012c D2 {name}: {measured[name]} queries")  # noqa: T201
    assert measured == {name: expected for name, (_, _, expected) in pages.items()}
