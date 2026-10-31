"""The Aging report by store (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md
§3 G1, G3; §5 G): the bucket edges, the rows by store ("No store" for a one-off charge without
a lease), the five filters (state, operator, brand incl. "no brand", kind, deposits on/off and
the note under the total), the drill-down links and the store Charges tab's ``?bucket=``, the
CSV equal to the page, the statement-timeout sentence, and a query count that does not grow
with the number of stores (exact for 500 stores on PostgreSQL, with the two-second bound).

Fable adds the reconciliation tests of G2."""

from __future__ import annotations

import csv
import io
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.roles import Role
from ledger.models import (
    Brand,
    FeeType,
    InstallmentCadence,
    Lease,
    OneOffCategory,
    OneOffCharge,
    Operator,
    PaymentChannel,
    RentCharge,
    RentChargeStatus,
    Store,
    StoreStatus,
)
from ledger.services import installments, receivables
from tests.receivables_helpers import (
    a_lease,
    a_one_off_charge,
    a_rent_charge,
    a_store,
    an_insurance_charge,
    an_operator,
)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

D = Decimal
ZERO = D("0.00")
TODAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
SEP = date(2026, 9, 1)
AGING = "/reports/aging/"
EXPORT = "/reports/aging/export.csv"


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    return TODAY


def viewer() -> Client:
    return client_for(Role.VIEWER)[0]


def page(path: str, params: dict[str, str] | None = None) -> str:
    response = viewer().get(path, params or {})
    assert response.status_code == 200, path
    return response.content.decode()


def records(params: dict[str, str] | None = None) -> list[dict[str, str]]:
    response = viewer().get(EXPORT, params or {})
    assert response.status_code == 200
    body = b"".join(response.streaming_content).decode()  # type: ignore[attr-defined]
    return list(csv.DictReader(io.StringIO(body)))


def a_deposit(lease: Lease, price: str, due: date) -> OneOffCharge:
    assert lease.operator is not None
    fee = FeeType.objects.filter(code="aging-deposit").first() or FeeType.objects.create(
        code="aging-deposit", name="Security deposit", refundable=True
    )
    return a_one_off_charge(
        lease.operator,
        price,
        due,
        lease=lease,
        category=OneOffCategory.SETUP,
        collection="none",
        description="",
        fee_type=fee,
    )


def by_store(report: receivables.AgingReport) -> dict[str, receivables.AgingStoreRow]:
    return {
        row.store_label if row.store_id else f"No store/{row.operator_names[0]}": row
        for row in report.rows
    }


def a_brand(name: str) -> Brand:
    return Brand.objects.create(name=name, sort_order=90)


def build_world() -> dict[str, Any]:
    """Two operators over three stores (GA with a brand, TX without, FL with another brand),
    a lease-less one-off charge, a deposit and an insurance charge."""
    north = an_operator("Nora North")
    south = an_operator("Sam South")
    kiosk, mall = a_brand("Aging Kiosk"), a_brand("Aging Mall")
    atlanta = a_store("Atlanta", 101, state="GA")
    atlanta.brand = kiosk
    atlanta.save()
    austin = a_store("Austin", 202, state="TX")
    miami = a_store("Miami", 303, state="FL")
    miami.brand = mall
    miami.save()
    first = a_lease(atlanta, north, debit_day=5)
    second = a_lease(austin, south, debit_day=1)
    third = a_lease(miami, north, debit_day=1)
    a_rent_charge(first, SEP, RentChargeStatus.UNPAID, "1000.00")  # 31 days
    a_rent_charge(first, date(2026, 10, 1), RentChargeStatus.UNPAID, "1000.00")  # 1 day
    an_insurance_charge(first, 2026, date(2026, 11, 1), "695.00")  # current
    a_rent_charge(second, SEP, RentChargeStatus.UNPAID, "500.00")  # 35 days
    a_one_off_charge(south, "80.00", date(2026, 6, 1), lease=second)  # 127 days
    a_rent_charge(third, date(2026, 7, 1), RentChargeStatus.UNPAID, "700.00")  # 97 days
    a_one_off_charge(north, "40.00", date(2026, 8, 1))  # no lease: 66 days
    deposit = a_deposit(second, "2000.00", SEP)  # 35 days, not in the KPI
    return {
        "north": north,
        "south": south,
        "kiosk": kiosk,
        "mall": mall,
        "atlanta": atlanta,
        "austin": austin,
        "miami": miami,
        "deposit": deposit,
    }


# --- Buckets ------------------------------------------------------------------------------


def test_the_bucket_keys_are_the_contract() -> None:
    assert [key for key, _ in receivables.AGING_BUCKETS] == [
        "current",
        "1_30",
        "31_60",
        "61_90",
        "over_90",
    ]


@pytest.mark.parametrize(
    ("days", "bucket"),
    [
        (-1, "current"),
        (0, "current"),  # due today
        (1, "1_30"),
        (30, "1_30"),
        (31, "31_60"),
        (60, "31_60"),
        (61, "61_90"),
        (90, "61_90"),
        (91, "over_90"),
    ],
)
def test_a_charge_at_each_edge_lands_in_its_bucket_of_its_store(days: int, bucket: str) -> None:
    lease = a_lease(a_store("Edge", 7), an_operator("Edgar Edge"))
    assert lease.operator is not None
    a_one_off_charge(lease.operator, "10.00", TODAY - timedelta(days=days), lease=lease)
    [row] = receivables.aging(TODAY).rows
    assert row.buckets == {
        key: D("10.00") if key == bucket else ZERO for key, _ in receivables.AGING_BUCKETS
    }
    assert (row.total, row.count, row.store_label) == (D("10.00"), 1, "Edge")


# --- Rows by store ------------------------------------------------------------------------


def test_the_rows_are_by_store_with_the_operator_and_no_store_for_a_lease_less_charge() -> None:
    world = build_world()
    report = receivables.aging(TODAY)
    rows = by_store(report)
    assert list(rows) == ["Atlanta", "Austin", "Miami", "No store/Nora North"]
    atlanta = rows["Atlanta"]
    assert atlanta.operator_names == ["Nora North"] and atlanta.store_number == 101
    assert (atlanta.brand_name, atlanta.store_state) == ("Aging Kiosk", "GA")
    assert atlanta.buckets["current"] == D("695.00")
    assert atlanta.buckets["1_30"] == D("1000.00") and atlanta.buckets["31_60"] == D("1000.00")
    assert (atlanta.total, atlanta.count) == (D("2695.00"), 3)
    assert rows["Austin"].buckets["over_90"] == D("80.00")
    assert rows["Austin"].total == D("580.00")  # the deposit is left out
    assert rows["Miami"].buckets["over_90"] == D("700.00")
    lease_less = rows["No store/Nora North"]
    assert lease_less.store_id is None and lease_less.buckets["61_90"] == D("40.00")
    assert report.as_of == TODAY and not report.deposits_included
    assert report.totals.total == D("4015.00") == receivables.dashboard(TODAY).outstanding
    assert report.totals.count == 7
    assert world["deposit"].pk not in {row.charge_id for row in receivables.open_balances(TODAY)}


def test_a_store_with_two_operators_is_one_row_naming_both() -> None:
    store = a_store("Shared", 55)
    old = a_lease(store, an_operator("Olive Old"))
    new = a_lease(store, an_operator("Ned New"))
    a_rent_charge(old, SEP, RentChargeStatus.UNPAID, "100.00")
    a_rent_charge(new, date(2026, 10, 1), RentChargeStatus.UNPAID, "200.00")
    [row] = receivables.aging(TODAY).rows
    assert row.operator_names == ["Ned New", "Olive Old"] and row.total == D("300.00")
    html = page(AGING)
    assert '<td class="aging-operator" data-label="Operator">Ned New, Olive Old</td>' in html


# --- Filters ------------------------------------------------------------------------------


def amounts_of(report: receivables.AgingReport) -> dict[str, list[Decimal]]:
    return {label: [*row.amounts, row.total] for label, row in by_store(report).items()}


def test_state_operator_and_brand_choose_rows_and_never_change_their_amounts() -> None:
    world = build_world()
    every = amounts_of(receivables.aging(TODAY))
    cases: dict[str, tuple[dict[str, str], list[str]]] = {
        "state": ({"state": "tx"}, ["Austin"]),
        "operator": (
            {"operator": str(world["north"].pk)},
            ["Atlanta", "Miami", "No store/Nora North"],
        ),
        "brand": ({"brand": str(world["mall"].pk)}, ["Miami"]),
        "no brand": ({"brand": "none"}, ["Austin"]),
        "two filters": (
            {"operator": str(world["north"].pk), "brand": str(world["kiosk"].pk)},
            ["Atlanta"],
        ),
    }
    for name, (params, expected) in cases.items():
        selected = receivables.parse_aging_filter(params)
        assert not selected.messages, name
        report = receivables.aging(TODAY, selected)
        found = amounts_of(report)
        assert list(found) == expected, name
        assert all(found[label] == every[label] for label in found), name
        assert report.totals.total == sum((every[label][-1] for label in expected), ZERO), name


def test_the_kind_filter_counts_only_that_kind() -> None:
    build_world()
    for kind, total in (
        ("rent", D("3200.00")),
        ("insurance", D("695.00")),
        ("one_off", D("120.00")),
        ("all", D("4015.00")),
    ):
        report = receivables.aging(TODAY, receivables.parse_aging_filter({"kind": kind}))
        assert report.totals.total == total, kind
    insurance = receivables.aging(TODAY, receivables.parse_aging_filter({"kind": "insurance"}))
    assert [row.store_label for row in insurance.rows] == ["Atlanta"]


def test_include_deposits_adds_the_deposits_still_owed_and_says_so() -> None:
    world = build_world()
    selected = receivables.parse_aging_filter({"deposits": "1"})
    report = receivables.aging(TODAY, selected)
    austin = by_store(report)["Austin"]
    assert austin.total == D("2580.00") and austin.buckets["31_60"] == D("2500.00")
    assert austin.deposits == D("2000.00") and austin.count == 3
    assert report.deposits_included and report.totals.deposits == D("2000.00")
    assert report.totals.total == receivables.dashboard(TODAY).outstanding + D("2000.00")
    on = page(AGING, {"deposits": "1"})
    assert 'id="aging-grand-total">$6,015.00<' in on
    note = re.search(r'<p class="muted" id="aging-deposits-note">([^<]*)</p>', on)
    assert note is not None
    assert "includes $2,000.00 of deposits still owed" in note.group(1)
    assert "Unpaid balances figure leaves deposits out" in note.group(1)
    off = page(AGING)
    assert 'id="aging-grand-total">$4,015.00<' in off
    assert "Deposits still owed are left out" in off
    # A deposit is a one-off charge: kept with the one-off kind, not with rent.
    one_off = receivables.aging(
        TODAY, receivables.parse_aging_filter({"kind": "one_off", "deposits": "1"})
    )
    assert one_off.totals.total == D("2120.00") and one_off.deposits_included
    rent = receivables.aging(
        TODAY, receivables.parse_aging_filter({"kind": "rent", "deposits": "1"})
    )
    assert rent.totals.total == D("3200.00") and not rent.deposits_included
    # A waived or voided deposit is not owed.
    for fields in (
        {"status": "waived", "waived_at": NOW, "waive_note": "Goodwill"},
        {"status": "voided", "voided_at": NOW, "void_note": "Cancelled"},
    ):
        OneOffCharge.objects.filter(pk=world["deposit"].pk).update(
            status="owed", waived_at=None, waive_note="", voided_at=None, void_note=""
        )
        OneOffCharge.objects.filter(pk=world["deposit"].pk).update(**fields)
        assert receivables.aging(TODAY, selected).totals.deposits == ZERO, fields


def test_the_filters_component_shows_chips_for_accepted_values_only() -> None:
    world = build_world()
    html = page(
        AGING,
        {
            "state": "GA",
            "operator": str(world["north"].pk),
            "brand": "none",
            "kind": "rent",
            "deposits": "1",
        },
    )
    chips = re.findall(r'aria-label="Remove ([^"]+)"', html)
    assert chips == [
        "State: Georgia",
        "Operator: Nora North",
        "Brand: No brand",
        "Charges: Rent",
        "Deposits: Included",
    ]
    assert 'id="filters-toggle"' in html and "Filters · 5" in html
    assert 'id="filters-clear" href="/reports/aging/"' in html
    bad = page(
        AGING,
        {"state": "ZZ", "operator": "nope", "brand": "nope", "kind": "cash", "deposits": "yes"},
    )
    assert re.findall(r'aria-label="Remove ([^"]+)"', bad) == []
    for sentence in (
        receivables.BAD_STATE,
        receivables.BAD_OPERATOR,
        receivables.BAD_BRAND,
        receivables.BAD_KIND,
    ):
        assert sentence in bad
    assert 'id="aging-grand-total">$4,015.00<' in bad
    # An operator that is not one: a well-formed id, no chip, no row.
    unknown = page(AGING, {"operator": "00000000-0000-0000-0000-000000000000"})
    assert "Remove Operator" not in unknown and "Nothing is owed." in unknown


def test_the_filter_round_trips_its_query() -> None:
    selected = receivables.parse_aging_filter(
        {"state": " fl ", "brand": "NONE", "kind": "all", "deposits": "1"}
    )
    assert selected.as_query() == {"state": "FL", "deposits": "1"}
    assert selected.messages == (receivables.BAD_BRAND,)  # the value is "none", in lower case
    assert receivables.parse_aging_filter({}).as_query() == {}


# --- Drill-down ---------------------------------------------------------------------------


def test_the_store_and_each_bucket_link_to_the_charges_tab() -> None:
    world = build_world()
    html = page(AGING)
    store = world["atlanta"].pk
    base = f"/stores/{store}/?tab=charges&amp;status=open"
    assert f'<a href="{base}#tab-charges">#101 Atlanta</a>' in html
    for bucket, amount in (("current", "$695.00"), ("1_30", "$1,000.00"), ("31_60", "$1,000.00")):
        assert f'<a href="{base}&amp;bucket={bucket}#tab-charges">{amount}</a>' in html, bucket
    assert f"{base}&amp;bucket=over_90" not in html  # nothing owed there: no link
    row = re.search(
        r'<tr class="aging-row">\s*<td class="aging-store" data-label="Store">No store</td>', html
    )
    assert row is not None  # the lease-less row has no store to open


def test_the_charges_tab_highlights_the_rows_of_the_bucket() -> None:
    world = build_world()
    store: Store = world["atlanta"]
    aging = by_store(receivables.aging(TODAY))["Atlanta"]
    client = viewer()
    for bucket, _ in receivables.AGING_BUCKETS:
        response = client.get(
            f"/stores/{store.pk}/", {"tab": "charges", "status": "open", "bucket": bucket}
        )
        context = response.context
        highlighted = [row for row in context["charge_rows"] if row.in_bucket]
        assert sum((row.balance for row in highlighted), ZERO) == aging.buckets[bucket], bucket
        assert context["bucket_total"] == aging.buckets[bucket]
        assert context["bucket_count"] == len(highlighted)
        html = response.content.decode()
        assert html.count(" in-bucket") == len(highlighted), bucket
        assert 'id="charges-bucket"' in html
        assert context["charge_total"] == aging.total
    plain = client.get(f"/stores/{store.pk}/", {"tab": "charges", "bucket": "sometime"})
    assert not any(row.in_bucket for row in plain.context["charge_rows"])
    assert 'id="charges-bucket"' not in plain.content.decode()


def test_status_open_lists_only_open_charges_and_the_total_is_the_aging_row() -> None:
    """``?status=open`` is the Charges filter; a deposit and a charge on a live installment
    plan are listed but not in the open balance, which is the store's Aging row."""
    operator = an_operator("Ivy Installment")
    lease = a_lease(a_store("Plan", 9), operator, channel=PaymentChannel.WIRE)
    a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, "1000.00")
    paid = a_rent_charge(lease, date(2026, 7, 1), RentChargeStatus.PAID, "1000.00", "1000.00")
    parent = a_rent_charge(lease, date(2026, 8, 1), RentChargeStatus.UNPAID, "900.00")
    cadence = InstallmentCadence.values[0]
    lines, refused = installments.preview(parent, 3, TODAY, cadence, today=TODAY)
    assert refused is None
    made = installments.create_plan(parent, 3, TODAY, cadence, lines, None, today=TODAY)
    assert made.ok
    a_deposit(lease, "2000.00", SEP)
    response = viewer().get(f"/stores/{lease.store_id}/", {"tab": "charges", "status": "open"})
    rows = response.context["charge_rows"]
    assert response.context["charge_filter"] == "open"
    assert paid.pk not in {row.charge_id for row in rows}
    [aging] = receivables.aging(TODAY).rows
    assert response.context["charge_total"] == aging.total == D("1900.00")
    html = response.content.decode()
    assert '<span id="charges-balance">$1,900.00</span>' in html
    assert 'id="charges-uncounted"' in html


# --- The page, the CSV, the guard ---------------------------------------------------------


def test_the_page_is_titled_aging_and_the_header_has_no_money_filter_bug() -> None:
    build_world()
    html = page(AGING)
    assert "· Aging</title>" in html and "<h1>Aging</h1>" in html
    head = re.search(r"<thead>(.*?)</thead>", html, re.S)
    assert head is not None
    headers = re.findall(r"<th[^>]*>([^<]*)</th>", head.group(1))
    assert headers == [
        "Store",
        "Operator",
        "Current",
        "1 to 30 days",
        "31 to 60 days",
        "61 to 90 days",
        "Over 90 days",
        "Total",
        "Open charges",
    ]
    assert f"As of {TODAY:%b} {TODAY.day}, {TODAY.year}" in html
    menu = page("/reports/")
    assert 'id="report-aging">Aging</a>' in menu


@pytest.mark.parametrize(
    "params",
    [{}, {"state": "GA"}, {"brand": "none"}, {"kind": "rent", "deposits": "1"}, {"deposits": "1"}],
)
def test_the_csv_equals_the_page(params: dict[str, str]) -> None:
    build_world()
    report = receivables.aging(TODAY, receivables.parse_aging_filter(params))
    found = records(params)
    assert len(found) == len(report.rows) + 1
    labels = [label for _, label in receivables.AGING_BUCKETS]
    for record, row in zip(found, report.rows, strict=False):
        assert record["Store"] == row.store_label
        assert record["Operator"] == "; ".join(row.operator_names)
        assert [D(record[f"{label} (USD)"]) for label in labels] == row.amounts
        assert D(record["Total (USD)"]) == row.total and int(record["Open charges"]) == row.count
    total = found[-1]
    assert total["Store"] == "Total"
    assert [D(total[f"{label} (USD)"]) for label in labels] == report.totals.amounts
    assert D(total["Total (USD)"]) == report.totals.total
    html = page(AGING, params)
    assert f'id="aging-grand-total">${report.totals.total:,}<' in html


def test_the_export_link_carries_the_filters() -> None:
    world = build_world()
    html = page(AGING, {"state": "GA", "operator": str(world["north"].pk)})
    assert f'href="/reports/aging/export.csv?state=GA&amp;operator={world["north"].pk}"' in html
    austin = records({"state": "TX"})
    assert [record["Store"] for record in austin] == ["Austin", "Total"]
    assert (austin[0]["Store number"], austin[0]["State"], austin[0]["Brand"]) == ("202", "TX", "")


def test_a_cancelled_aging_answers_200_with_the_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.views import reports as report_views

    def too_slow(compute: Any) -> Any:
        raise report_views.ReportTooSlow

    monkeypatch.setattr(report_views, "bounded", too_slow)
    for target in (AGING, EXPORT):
        response = viewer().get(target, {"state": "GA", "deposits": "1"})
        assert response.status_code == 200 and not response.streaming, target
        html = response.content.decode()
        assert report_views.TOO_SLOW in html and 'id="aging-too-slow"' in html, target
        assert 'id="aging"' not in html and "State: Georgia" in html, target


# --- Queries ------------------------------------------------------------------------------


def _stores(count: int, start: int, operators: list[Operator]) -> None:
    for n in range(start, start + count):
        lease = a_lease(a_store(f"Q{n:04d}", n), operators[n % len(operators)])
        a_rent_charge(lease, SEP, RentChargeStatus.UNPAID, "100.00")
        an_insurance_charge(lease, 2026, date(2026, 11, 1))
        a_one_off_charge(operators[n % len(operators)], "10.00", SEP, lease=lease)
        a_deposit(lease, "50.00", SEP)


def _page_queries(params: dict[str, str]) -> int:
    client = viewer()
    client.get("/reports/")  # warm the session
    with CaptureQueriesContext(connection) as captured:
        response = client.get(AGING, params)
    assert response.status_code == 200
    return len(captured.captured_queries)


@pytest.mark.parametrize("params", [{}, {"deposits": "1", "brand": "none", "state": "GA"}])
def test_the_page_query_count_does_not_grow_with_the_stores(params: dict[str, str]) -> None:
    operators = [an_operator(f"Count {n}") for n in range(3)]
    _stores(2, 0, operators)
    small = _page_queries(params)
    _stores(12, 2, operators)
    assert _page_queries(params) == small


#: The page's queries on PostgreSQL: the shell (session, user, role, company settings,
#: theme and the like), open_balances (4), the stores' number and brand (1), the operators
#: and brands of the filters (2); one more with deposits.
POSTGRESQL_BUDGET = 15


@pytest.mark.skipif(connection.vendor != "postgresql", reason="timed on PostgreSQL only")
def test_500_stores_on_postgresql_answer_in_a_fixed_count_under_two_seconds() -> None:
    operators = Operator.objects.bulk_create(
        [Operator(display_name=f"Bulk Operator {n:03d}") for n in range(250)]
    )
    stores = Store.objects.bulk_create(
        [
            Store(
                external_label=f"Bulk {n:03d}",
                store_number=n,
                state=("GA", "TX", "FL")[n % 3],
                status=StoreStatus.ACTIVE,
            )
            for n in range(500)
        ]
    )
    leases = Lease.objects.bulk_create(
        [
            Lease(
                store=store, operator=operators[n % 250], debit_day=1, started_on=date(2024, 1, 1)
            )
            for n, store in enumerate(stores)
        ]
    )
    RentCharge.objects.bulk_create(
        [
            RentCharge(
                lease=lease,
                period_month=month,
                seq=1,
                due_on=month,
                amount_due=D("100.00"),
                amount_paid=ZERO,
                status=RentChargeStatus.UNPAID,
            )
            for lease in leases
            for month in (date(2026, 6, 1), SEP, date(2026, 10, 1))
        ]
    )
    for params, budget in (({}, POSTGRESQL_BUDGET), ({"deposits": "1"}, POSTGRESQL_BUDGET + 1)):
        client = viewer()
        client.get("/reports/")
        with CaptureQueriesContext(connection) as captured:
            started = time.perf_counter()
            response = client.get(AGING, params)
            elapsed = time.perf_counter() - started
        assert response.status_code == 200 and "too long" not in response.content.decode()
        print(f"PLAN-014 G aging {params}: {elapsed:.3f} s, {len(captured)} queries")  # noqa: T201
        assert len(captured) == budget, [query["sql"][:80] for query in captured.captured_queries]
        assert elapsed < 2.0
    assert len(receivables.aging(TODAY).rows) == 500
