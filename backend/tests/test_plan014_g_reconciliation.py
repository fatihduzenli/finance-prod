"""PLAN-014 G2 (Fable): the Aging report reconciles. On a world with every kind — a partly paid
rent charge, a derived insurance charge, an installment plan (its parent listed, its rows
counted), a re-debit carrying part of a returned rent debit, a deposit, a waived rent charge,
a voided one-off charge and a lease-less one-off charge — the grand total without deposits
equals the dashboard's Unpaid figure and the sum of the stores' open balances; each store row
equals its Charges tab's open total; each bucket equals the sum of the rows the tab highlights
for it; the CSV's totals equal the page's; a row-choosing filter never changes a row's amounts;
deposits on add exactly the deposits still owed; the edges of the buckets on the day."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from accounts.roles import Role
from ledger.models import (
    Brand,
    InstallmentCadence,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentChargeStatus,
    Store,
)
from ledger.services import installments, receivables, workspace
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_aging import EXPORT, a_deposit
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
ZERO = D("0.00")
TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    monkeypatch.setattr("django.utils.timezone.now", lambda: datetime(2026, 10, 6, 16, tzinfo=UTC))
    return TODAY


def a_world() -> dict[str, Any]:
    """Three stores, two operators, every kind of open and closed charge."""
    north, south = an_operator("Nora North"), an_operator("Sam South")
    brand = Brand.objects.create(name="Recon Brand", sort_order=95)
    atlanta = a_store("Atlanta", 101, state="GA")
    atlanta.brand = brand
    atlanta.save()
    austin = a_store("Austin", 202, state="TX")
    miami = a_store("Miami", 303, state="FL")
    first = a_lease(atlanta, north, debit_day=5, channel=PaymentChannel.WIRE)  # a plan by hand
    second = a_lease(austin, south, debit_day=1)
    third = a_lease(miami, north, debit_day=1)
    # Atlanta: a partly paid rent (600 open, 31 days), a current insurance charge, a plan on an
    # August rent charge (its rows count, its parent does not), a waived rent, a voided one-off.
    a_rent_charge(first, date(2026, 9, 1), RentChargeStatus.UNPAID, "1000.00", "400.00")
    an_insurance_charge(first, 2026, date(2026, 11, 1), "695.00")
    parent = a_rent_charge(first, date(2026, 8, 1), RentChargeStatus.UNPAID, "900.00")
    cadence = InstallmentCadence.values[0]
    lines, refused = installments.preview(parent, 3, TODAY, cadence, today=TODAY)
    assert refused is None
    made = installments.create_plan(parent, 3, TODAY, cadence, lines, None, today=TODAY)
    assert made.ok, made.message
    a_rent_charge(first, date(2026, 7, 1), RentChargeStatus.WAIVED, "1000.00")
    a_one_off_charge(
        north,
        "250.00",
        date(2026, 6, 1),
        lease=first,
        status=OneOffChargeStatus.VOIDED,
        voided_at=timezone.now(),
        void_note="Entered twice.",
    )
    # Austin: a returned rent debit whose re-debit carries part of it (the original stays
    # open for the rest), a deposit still owed (not in the KPI), an old one-off charge.
    returned_rent = a_rent_charge(second, date(2026, 8, 1), RentChargeStatus.UNPAID, "500.00")
    payment = Payment.objects.create(
        rent_charge=returned_rent,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D("500.00"),
        status=PaymentStatus.RETURNED,
        initiated_on=date(2026, 8, 1),
        returned_at=timezone.now() - timedelta(days=40),
        return_code="R01",
        return_reason="",
    )
    a_one_off_charge(
        south,
        "500.00",
        date(2026, 9, 20),
        lease=second,
        category=OneOffCategory.REDEBIT,
        description="Re-debit of Rent August 2026",
        source_payment=payment,
    )
    deposit = a_deposit(second, "2000.00", date(2026, 9, 1))
    a_one_off_charge(south, "80.00", date(2026, 6, 1), lease=second)
    # Miami: a rent 97 days late; and a lease-less one-off charge of Nora's ("No store").
    a_rent_charge(third, date(2026, 7, 1), RentChargeStatus.UNPAID, "700.00")
    a_one_off_charge(north, "40.00", date(2026, 8, 1))
    return {
        "stores": [atlanta, austin, miami],
        "leases": {atlanta.pk: [first], austin.pk: [second], miami.pk: [third]},
        "north": north,
        "south": south,
        "brand": brand,
        "deposit": deposit,
    }


def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def rows_by_store(report: receivables.AgingReport) -> dict[str, receivables.AgingStoreRow]:
    return {
        (row.store_label if row.store_id else f"No store/{row.operator_names[0]}"): row
        for row in report.rows
    }


def csv_rows(params: dict[str, str] | None = None) -> list[dict[str, str]]:
    response: Any = viewer().get(EXPORT, params or {})
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode())))


def money(text: str) -> Decimal:
    return D(text.replace("$", "").replace(",", "") or "0")


# --- The grand total ------------------------------------------------------------------------------


def test_the_grand_total_is_the_dashboards_unpaid_figure_and_the_sum_of_the_store_balances() -> (
    None
):
    world = a_world()
    report = receivables.aging(TODAY)
    assert report.totals.total == receivables.dashboard(TODAY).outstanding
    stores: list[Store] = world["stores"]
    by_store = rows_by_store(report)
    store_sum = ZERO
    for store in stores:
        balances = workspace.store_balances(store, world["leases"][store.pk], TODAY)
        assert by_store[store.external_label].total == balances.total, store.external_label
        store_sum += balances.total
    no_store = by_store["No store/Nora North"]
    assert no_store.total == D("40.00")
    assert report.totals.total == store_sum + no_store.total
    # Nothing closed or double counted: the waived rent, the voided one-off and the plan's
    # parent are not in it; the plan's rows, the re-debit and the carried original are once.
    assert report.totals.total == sum((row.total for row in report.rows), ZERO)
    assert world["deposit"].price not in {row.total for row in report.rows}


def test_each_store_row_is_its_charges_tabs_open_total_and_each_bucket_its_highlighted_rows() -> (
    None
):
    world = a_world()
    report = receivables.aging(TODAY)
    by_store = rows_by_store(report)
    client = viewer()
    for store in world["stores"]:
        row = by_store[store.external_label]
        tab = client.get(f"/stores/{store.pk}/", {"tab": "charges", "status": "open"})
        assert tab.status_code == 200
        assert tab.context["charge_total"] == row.total, store.external_label
        for bucket, _ in receivables.AGING_BUCKETS:
            drill = client.get(
                f"/stores/{store.pk}/", {"tab": "charges", "status": "open", "bucket": bucket}
            )
            highlighted = [r for r in drill.context["charge_rows"] if r.in_bucket]
            assert sum((r.balance for r in highlighted), ZERO) == row.buckets[bucket], (
                store.external_label,
                bucket,
            )
        assert sum(row.buckets.values(), ZERO) == row.total


def test_the_csv_totals_equal_the_page_under_every_filter() -> None:
    world = a_world()
    filters: list[dict[str, str]] = [
        {},
        {"state": "GA"},
        {"operator": str(world["north"].pk)},
        {"brand": str(world["brand"].pk)},
        {"brand": "none"},
        {"kind": "rent"},
        {"deposits": "1"},
    ]
    for params in filters:
        report = receivables.aging(TODAY, receivables.parse_aging_filter(params))
        lines = csv_rows(params)
        total_line = next(line for line in lines if line["Store"] == "Total")
        assert money(total_line["Total (USD)"]) == report.totals.total, params
        for bucket, label in receivables.AGING_BUCKETS:
            assert money(total_line[f"{label} (USD)"]) == report.totals.buckets[bucket], (
                params,
                bucket,
            )
        assert len([line for line in lines if line["Store"] != "Total"]) == len(report.rows)


def test_a_row_choosing_filter_never_changes_a_rows_amounts() -> None:
    world = a_world()
    plain = rows_by_store(receivables.aging(TODAY))
    for params in (
        {"state": "GA"},
        {"state": "TX"},
        {"operator": str(world["north"].pk)},
        {"operator": str(world["south"].pk)},
        {"brand": str(world["brand"].pk)},
        {"brand": "none"},
    ):
        filtered = receivables.aging(TODAY, receivables.parse_aging_filter(params))
        assert filtered.rows, params
        for row in filtered.rows:
            key = row.store_label if row.store_id else f"No store/{row.operator_names[0]}"
            assert row.total == plain[key].total, (params, key)
            assert row.buckets == plain[key].buckets, (params, key)
            assert row.count == plain[key].count, (params, key)


def test_deposits_on_add_exactly_the_deposits_still_owed() -> None:
    world = a_world()
    off = receivables.aging(TODAY)
    on = receivables.aging(TODAY, receivables.parse_aging_filter({"deposits": "1"}))
    assert on.totals.total == off.totals.total + world["deposit"].price
    austin_off = rows_by_store(off)["Austin"]
    austin_on = rows_by_store(on)["Austin"]
    assert austin_on.total - austin_off.total == world["deposit"].price
    assert austin_on.buckets["31_60"] - austin_off.buckets["31_60"] == world["deposit"].price
    page: Any = viewer().get("/reports/aging/", {"deposits": "1"})
    assert "deposits still owed" in page.content.decode()


def test_the_bucket_edges_on_the_day() -> None:
    operator = an_operator("Edge Case")
    lease = a_lease(a_store("Edges", 404), operator, debit_day=1)
    expected = {
        "current": ["2026-10-06", "2026-10-07"],
        "1_30": ["2026-10-05", "2026-09-06"],
        "31_60": ["2026-09-05", "2026-08-07"],
        "61_90": ["2026-08-06", "2026-07-08"],
        "over_90": ["2026-07-07", "2026-01-01"],
    }
    amount = D("10.00")
    for days in expected.values():
        for text in days:
            due = date.fromisoformat(text)
            a_one_off_charge(operator, "10.00", due, lease=lease, description=f"Due {text}")
    [row] = receivables.aging(TODAY).rows
    for bucket, days in expected.items():
        assert row.buckets[bucket] == amount * len(days), bucket
    assert row.total == amount * 10


def test_a_lease_less_one_off_is_in_no_store_and_in_no_store_row_only(
    settings: Any,
) -> None:
    world = a_world()
    report = receivables.aging(TODAY)
    by_store = rows_by_store(report)
    lease_less = OneOffCharge.objects.get(lease__isnull=True, operator=world["north"])
    assert by_store["No store/Nora North"].total == lease_less.balance
    for store in world["stores"]:
        tab = viewer().get(f"/stores/{store.pk}/", {"tab": "charges", "status": "open"})
        assert lease_less.pk not in {r.charge_id for r in tab.context["charge_rows"]}
    assert isinstance(world["leases"][world["stores"][0].pk][0], Lease)
