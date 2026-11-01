"""Annual insurance charges (PLAN-007 §6 row 8; the owner's algorithm, ADR-0005), and the LEASE
card's "Bill now" on the insurance row (PLAN-014 I-b: once, audited with the actor, refused for
a viewer, a GET, an archived operator, a store without an open date and a lease that does not
owe the year)."""

from __future__ import annotations

import html
from datetime import date
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.utils import timezone

from accounts.roles import Role
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
from payments import lease_sync
from tests.receivables_helpers import an_operator
from tests.test_operator_views import client_for

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


# --- PLAN-014 I-b: the LEASE card's "Bill now" on the insurance row ----------------------


def _billable_lease(open_date: date = date(2025, 3, 10)) -> Lease:
    return lease_on(store(open_date), operator=an_operator("Bill Now Co"))


def test_bill_now_creates_the_current_years_charge_once_audited_with_the_actor() -> None:
    lease = _billable_lease()
    _, user = client_for(Role.BILLING)
    first = insurance.bill_now(lease, TODAY, actor=user)
    assert first.ok and first.message == insurance.BILL_CREATED and first.charge is not None
    assert years(lease) == [(2026, date(2026, 3, 10), D("695.00"), False)]
    [entry] = AuditLog.objects.filter(action="insurance_charge.created")
    assert entry.actor_id == user.pk and entry.entity_id == str(first.charge.pk)
    assert entry.after["policy_year"] == 2026 and entry.after["amount"] == "695.00"
    again = insurance.bill_now(lease, TODAY, actor=user)
    assert (again.ok, again.message, again.charge) == (False, insurance.BILL_ALREADY, None)
    assert InsuranceCharge.objects.count() == 1
    assert AuditLog.objects.filter(action="insurance_charge.created").count() == 1


def test_bill_now_before_this_years_anniversary_bills_last_policy_year() -> None:
    lease = _billable_lease(date(2025, 11, 20))
    outcome = insurance.bill_now(lease, TODAY, actor=None)
    assert outcome.ok and years(lease) == [(2025, date(2025, 11, 20), D("595.00"), False)]


def test_bill_now_without_a_premium_creates_a_charge_that_waits_for_it() -> None:
    InsurancePremium.objects.filter(policy_year=2026).delete()
    lease = _billable_lease()
    outcome = insurance.bill_now(lease, TODAY, actor=None)
    assert (outcome.ok, outcome.message) == (True, insurance.BILL_NEEDS_PREMIUM)
    assert years(lease) == [(2026, date(2026, 3, 10), None, True)]


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("no open date", "BILL_NO_OPEN_DATE"),
        ("started after the anniversary", "BILL_NOT_OWED"),
        ("ended before the anniversary", "BILL_NOT_OWED"),
        ("store closed before the anniversary", "BILL_NOT_OWED"),
        ("prospective", "BILL_NOT_OWED"),
    ],
)
def test_bill_now_refuses_with_one_sentence_and_writes_nothing(case: str, message: str) -> None:
    operator = an_operator("Refused Co")
    if case == "no open date":
        lease = lease_on(store(None), operator=operator)
    elif case == "started after the anniversary":
        lease = lease_on(store(date(2025, 3, 10)), operator=operator, started_on=date(2026, 4, 1))
    elif case == "ended before the anniversary":
        lease = lease_on(
            store(date(2025, 3, 10)),
            operator=operator,
            started_on=date(2025, 3, 1),
            ended_on=date(2026, 3, 1),
            collection_status=CollectionStatus.ENDED,
        )
    elif case == "store closed before the anniversary":
        lease = lease_on(store(date(2025, 3, 10), closed_at=date(2026, 3, 1)), operator=operator)
    else:
        lease = lease_on(
            store(date(2025, 3, 10)),
            operator=operator,
            collection_status=CollectionStatus.PROSPECTIVE,
        )
    outcome = insurance.bill_now(lease, TODAY, actor=None)
    assert (outcome.ok, outcome.message) == (False, getattr(insurance, message))
    assert outcome.message.endswith(" Nothing was billed.")
    assert not InsuranceCharge.objects.exists() and not AuditLog.objects.exists()


def _bill_url(lease: Lease) -> str:
    return f"/leases/{lease.pk}/insurance/bill/"


def test_the_route_bills_for_billing_and_returns_to_the_store_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    lease = _billable_lease()
    client, user = client_for(Role.BILLING)
    response = client.post(_bill_url(lease), follow=True)
    assert response.redirect_chain[0] == (f"/stores/{lease.store_id}/", 302)
    assert html.escape(insurance.BILL_CREATED) in response.content.decode()
    [entry] = AuditLog.objects.filter(action="insurance_charge.created")
    assert entry.actor_id == user.pk
    second = client.post(_bill_url(lease), follow=True)
    assert "already billed" in second.content.decode()
    assert InsuranceCharge.objects.count() == 1


def test_the_route_refuses_a_viewer_a_get_and_an_archived_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    lease = _billable_lease()
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(_bill_url(lease)).status_code in (302, 403)
    billing, _ = client_for(Role.BILLING)
    assert billing.get(_bill_url(lease)).status_code == 405
    assert not InsuranceCharge.objects.exists()
    assert lease.operator is not None
    lease.operator.archived_at = timezone.now()
    lease.operator.save()
    response = billing.post(_bill_url(lease), follow=True)
    assert lease_sync.MESSAGES[lease_sync.ARCHIVED] in response.content.decode()
    assert not InsuranceCharge.objects.exists()
    assert not AuditLog.objects.filter(action="insurance_charge.created").exists()


def test_the_route_refuses_a_store_without_an_open_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *args, **kwargs: TODAY)
    lease = lease_on(store(None), operator=an_operator("No Date Co"))
    client, _ = client_for(Role.BILLING)
    response = client.post(_bill_url(lease), follow=True)
    assert insurance.BILL_NO_OPEN_DATE in response.content.decode()
    assert not InsuranceCharge.objects.exists()
