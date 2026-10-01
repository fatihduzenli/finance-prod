"""Annual insurance charges (PLAN-007 §6 row 8; the owner's algorithm, ADR-0005)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from ledger.models import (
    AuditLog,
    CollectionStatus,
    InsuranceCharge,
    InsurancePremium,
    Lease,
    PaymentChannel,
    Store,
    StoreStatus,
)
from ledger.services import insurance

pytestmark = pytest.mark.django_db

D = Decimal
TODAY = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def premiums() -> None:
    InsurancePremium.objects.create(policy_year=2025, amount=D("595.00"))
    InsurancePremium.objects.create(policy_year=2026, amount=D("695.00"))


def store(open_date: date | None, closed_at: date | None = None, label: str = "S1") -> Store:
    return Store.objects.create(
        external_label=label, open_date=open_date, closed_at=closed_at, status=StoreStatus.ACTIVE
    )


def lease_on(shop: Store, **overrides: Any) -> Lease:
    values: dict[str, Any] = {
        "store": shop,
        "collection_status": CollectionStatus.PAUSED,
        "payment_channel": PaymentChannel.WIRE,
        "started_on": None,
    }
    values.update(overrides)
    return Lease.objects.create(**values)


def years(lease: Lease) -> list[tuple[int, date, Decimal | None, bool]]:
    return [
        (c.policy_year, c.due_on, c.amount, c.needs_premium)
        for c in InsuranceCharge.objects.filter(lease=lease).order_by("policy_year")
    ]


def test_each_year_from_the_open_year_is_charged_on_its_anniversary() -> None:
    lease = lease_on(store(date(2025, 3, 10)))
    report = insurance.generate_insurance_charges(TODAY)
    assert years(lease) == [
        (2025, date(2025, 3, 10), D("595.00"), False),
        (2026, date(2026, 3, 10), D("695.00"), False),
    ]
    assert (report.created, report.existing) == (2, 0)
    assert AuditLog.objects.filter(action="insurance_charge.created").count() == 2


def test_a_second_run_creates_nothing() -> None:
    lease_on(store(date(2025, 3, 10)))
    insurance.generate_insurance_charges(TODAY)
    again = insurance.generate_insurance_charges(TODAY)
    assert (again.created, again.existing) == (0, 2)
    assert InsuranceCharge.objects.count() == 2


def test_an_anniversary_after_today_is_not_yet_charged_and_today_is() -> None:
    later = lease_on(store(date(2025, 10, 2), label="later"))
    today = lease_on(store(date(2025, 10, 1), label="today"))
    insurance.generate_insurance_charges(TODAY)
    assert [y for y, *_ in years(later)] == [2025]
    assert [y for y, *_ in years(today)] == [2025, 2026]


def test_29_february_falls_on_28_february_in_a_common_year() -> None:
    assert insurance.anniversary(date(2024, 2, 29), 2025) == date(2025, 2, 28)
    assert insurance.anniversary(date(2024, 2, 29), 2028) == date(2028, 2, 29)
    InsurancePremium.objects.create(policy_year=2024, amount=D("535.00"))
    lease = lease_on(store(date(2024, 2, 29)))
    insurance.generate_insurance_charges(TODAY)
    assert [due for _, due, *_ in years(lease)] == [
        date(2024, 2, 29),
        date(2025, 2, 28),
        date(2026, 2, 28),
    ]


def test_a_future_open_date_creates_nothing() -> None:
    lease = lease_on(store(date(2027, 1, 1)))
    insurance.generate_insurance_charges(TODAY)
    assert years(lease) == []


@pytest.mark.parametrize(
    ("closed_at", "expected"),
    [
        (date(2026, 3, 9), [2025]),  # one day before the anniversary: no charge
        (date(2026, 3, 10), [2025]),  # on the anniversary: not before it, no charge
        (date(2026, 3, 11), [2025, 2026]),  # after it: owed
    ],
)
def test_a_store_closed_before_its_anniversary_owes_nothing_for_that_year(
    closed_at: date, expected: list[int]
) -> None:
    lease = lease_on(store(date(2025, 3, 10), closed_at=closed_at))
    insurance.generate_insurance_charges(TODAY)
    assert [y for y, *_ in years(lease)] == expected


def test_a_missing_premium_creates_the_charge_without_an_amount() -> None:
    InsurancePremium.objects.filter(policy_year=2026).delete()
    lease = lease_on(store(date(2025, 3, 10)))
    report = insurance.generate_insurance_charges(TODAY)
    assert years(lease)[-1] == (2026, date(2026, 3, 10), None, True)
    assert report.needs_premium == 1


def test_a_store_without_an_open_date_gets_no_charge() -> None:
    lease = lease_on(store(None))
    report = insurance.generate_insurance_charges(TODAY)
    assert years(lease) == [] and report.needs_open_date == 1


def test_the_lease_holding_the_store_on_the_anniversary_owes_the_year() -> None:
    shop = store(date(2025, 3, 10))
    old = lease_on(
        shop,
        started_on=date(2025, 1, 1),
        ended_on=date(2025, 12, 31),
        collection_status=CollectionStatus.ENDED,
    )
    new = lease_on(shop, started_on=date(2026, 1, 1))
    insurance.generate_insurance_charges(TODAY)
    assert [y for y, *_ in years(old)] == [2025]
    assert [y for y, *_ in years(new)] == [2026]


def test_a_prospective_lease_owes_nothing() -> None:
    lease = lease_on(store(date(2025, 3, 10)), collection_status=CollectionStatus.PROSPECTIVE)
    insurance.generate_insurance_charges(TODAY)
    assert years(lease) == []


def test_the_due_soon_list_covers_today_to_seven_days_ahead_and_writes_nothing() -> None:
    today = lease_on(store(date(2025, 10, 1), label="A today"))
    in_seven = lease_on(store(date(2024, 10, 8), label="B seven"))
    lease_on(store(date(2025, 10, 9), label="C eight"))
    lease_on(store(date(2025, 9, 30), label="D yesterday"))
    closed = store(date(2025, 10, 3), closed_at=date(2026, 10, 3), label="E closed")
    lease_on(closed)
    due = insurance.insurance_due_soon(TODAY)
    assert [(item.lease.pk, item.policy_year, item.due_on, item.amount) for item in due] == [
        (today.pk, 2026, date(2026, 10, 1), D("695.00")),
        (in_seven.pk, 2026, date(2026, 10, 8), D("695.00")),
    ]
    assert all(item.charge is None for item in due)
    assert not InsuranceCharge.objects.exists()


def test_the_due_soon_list_crosses_the_year_end_and_shows_a_missing_premium() -> None:
    lease = lease_on(store(date(2025, 1, 2)))
    [item] = insurance.insurance_due_soon(date(2026, 12, 28))
    assert (item.lease.pk, item.policy_year, item.due_on, item.amount) == (
        lease.pk,
        2027,
        date(2027, 1, 2),
        None,
    )


def test_the_due_soon_list_shows_a_charge_already_created() -> None:
    lease = lease_on(store(date(2025, 10, 1)))
    insurance.generate_insurance_charges(TODAY)
    [item] = insurance.insurance_due_soon(TODAY)
    assert item.charge is not None and item.charge.lease_id == lease.pk


def test_the_command_is_idempotent_and_prints_the_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    lease_on(store(date(2025, 3, 10)))
    lease_on(store(None, label="no date"))
    first, second = StringIO(), StringIO()
    call_command("generate_insurance_charges", stdout=first)
    call_command("generate_insurance_charges", stdout=second)
    assert first.getvalue().strip() == (
        "created 2, already existed 0, missing premium 0, skipped (store has no open date) 1"
    )
    assert second.getvalue().startswith("created 0, already existed 2,")
    assert InsuranceCharge.objects.count() == 2
