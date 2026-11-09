"""PLAN-015f §3.3 and §3.4: the insurance row's "Bill now" by
:func:`ledger.services.insurance.can_bill_now` (the lease holds the store on the anniversary,
prospective or not, the anniversary passed or not; due on the anniversary; the daily generator
unchanged), and "Switch to own account" only for a lease sharing another store's lease's
account (the card and the POST alike)."""

from __future__ import annotations

import html
import uuid
from datetime import date

import pytest

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    CollectionStatus,
    InsuranceCharge,
    Lease,
    LeaseBilling,
)
from ledger.services import detail_cards, insurance
from ledger.services import leases as lease_service
from payments.fake import FakeProvider
from payments.models import Job, JobKind
from tests.job_helpers import worker_between_requests  # noqa: F401
from tests.test_insurance_charges import (
    TODAY,
    D,
    lease_on,
    premiums,  # noqa: F401 (autouse fixture)
    store,
)
from tests.test_lease_views import _sharer, _store_page, _switch_path
from tests.test_lease_views import fake as fake  # noqa: F401 (fixture)
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db


# --- can_bill_now: the pure rule ---------------------------------------------------------------


def test_a_lease_holding_the_store_on_the_anniversary_may_be_billed_passed_or_not() -> None:
    lease = lease_on(store(date(2025, 3, 10)), started_on=date(2025, 1, 1))
    assert insurance.can_bill_now(lease, 2026)  # Mar 10, 2026: passed on TODAY (Oct 1)
    assert insurance.can_bill_now(lease, 2027)  # Mar 10, 2027: not reached yet
    # The daily rule still waits for the anniversary.
    assert insurance.owes(lease, 2026, TODAY) and not insurance.owes(lease, 2027, TODAY)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"collection_status": CollectionStatus.PROSPECTIVE, "started_on": date(2026, 3, 1)}, True),
        ({"collection_status": CollectionStatus.PROSPECTIVE, "started_on": None}, True),
        ({"started_on": date(2026, 3, 10)}, True),  # starts on the anniversary itself
        ({"started_on": date(2026, 3, 11)}, False),  # the day after
        (
            {
                "started_on": date(2025, 1, 1),
                "ended_on": date(2026, 3, 9),
                "collection_status": CollectionStatus.ENDED,
            },
            False,
        ),
        (
            {
                "started_on": date(2025, 1, 1),
                "ended_on": date(2026, 3, 10),
                "collection_status": CollectionStatus.ENDED,
            },
            True,  # ends on the anniversary: it held the store that day
        ),
    ],
)
def test_can_bill_now_by_the_lease_dates(fields: dict[str, object], expected: bool) -> None:
    lease = lease_on(store(date(2025, 3, 10)), **fields)
    assert insurance.can_bill_now(lease, 2026) is expected


def test_can_bill_now_refuses_a_store_without_an_open_date_or_closed_by_the_anniversary() -> None:
    assert not insurance.can_bill_now(lease_on(store(None)), 2026)
    closed_on = lease_on(store(date(2025, 3, 10), closed_at=date(2026, 3, 10), label="S2"))
    assert not insurance.can_bill_now(closed_on, 2026)
    closed_after = lease_on(store(date(2025, 3, 10), closed_at=date(2026, 3, 11), label="S3"))
    assert insurance.can_bill_now(closed_after, 2026)


def test_a_29_february_open_date_falls_on_28_february_in_a_common_year() -> None:
    shop = store(date(2024, 2, 29))
    assert insurance.can_bill_now(lease_on(shop, started_on=date(2026, 2, 28)), 2026)
    assert not insurance.can_bill_now(lease_on(shop, started_on=date(2026, 3, 1)), 2026)
    assert insurance.can_bill_now(lease_on(shop, started_on=date(2028, 2, 29)), 2028)


# --- bill_now ----------------------------------------------------------------------------------


def test_a_prospective_lease_holding_the_store_is_billed_due_on_the_anniversary() -> None:
    lease = lease_on(
        store(date(2025, 3, 10)),
        started_on=date(2026, 2, 1),
        collection_status=CollectionStatus.PROSPECTIVE,
    )
    _, user = client_for(Role.BILLING)
    outcome = insurance.bill_now(lease, TODAY, actor=user)
    assert (outcome.ok, outcome.message) == (True, insurance.BILL_CREATED)
    charge = InsuranceCharge.objects.get()
    assert (charge.policy_year, charge.due_on, charge.amount) == (
        2026,
        date(2026, 3, 10),
        D("695.00"),
    )
    [audit] = AuditLog.objects.filter(action=insurance.CREATED_ACTION)
    assert audit.actor_id == user.pk and audit.after["due_on"] == "2026-03-10"
    again = insurance.bill_now(lease, TODAY, actor=user)
    assert (again.ok, again.message) == (False, insurance.BILL_ALREADY)
    assert InsuranceCharge.objects.count() == 1


def test_bill_now_never_dates_the_charge_before_the_anniversary() -> None:
    # The day the store opened in 2026 is the year's due date, however late the press.
    lease = lease_on(store(date(2025, 9, 30)), started_on=date(2025, 9, 1))
    outcome = insurance.bill_now(lease, TODAY, actor=None)
    assert outcome.ok and outcome.charge is not None
    assert outcome.charge.due_on == date(2026, 9, 30) <= TODAY


def test_a_lease_ending_before_the_anniversary_is_not_billable_and_the_post_is_refused() -> None:
    lease = lease_on(
        store(date(2025, 3, 10)),
        started_on=date(2025, 1, 1),
        ended_on=date(2026, 3, 1),
        collection_status=CollectionStatus.ENDED,
    )
    rows = detail_cards.lease_collections(lease, TODAY)
    assert rows.insurance is not None and rows.insurance.text == "not billed"
    assert rows.insurance_billable is False
    billing, _ = client_for(Role.BILLING)
    response = billing.post(f"/leases/{lease.pk}/insurance/bill/", follow=True)
    assert html.escape(insurance.BILL_NOT_OWED) in response.content.decode()
    assert not InsuranceCharge.objects.exists()


def test_the_daily_generator_still_skips_a_prospective_lease() -> None:
    lease = lease_on(
        store(date(2025, 3, 10)),
        started_on=date(2025, 1, 1),
        collection_status=CollectionStatus.PROSPECTIVE,
    )
    report = insurance.generate_insurance_charges(TODAY)
    assert report.created == 0 and not InsuranceCharge.objects.filter(lease=lease).exists()
    assert detail_cards.lease_collections(lease, TODAY).insurance_billable is True


def test_a_viewer_post_is_forbidden_and_bills_nothing() -> None:
    lease = lease_on(store(date(2025, 3, 10)), started_on=date(2025, 1, 1))
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.post(f"/leases/{lease.pk}/insurance/bill/").status_code == 403
    assert not InsuranceCharge.objects.exists()


# --- Switch to own account: only for another store's account ------------------------------------


def same_store(holder: Lease, sharer: Lease) -> None:
    """Make ``holder`` an earlier, ended lease of ``sharer``'s store (a renewal)."""
    Lease.objects.filter(pk=holder.pk).update(
        store=sharer.store,
        ended_on=date(2026, 1, 31),
        collection_status=CollectionStatus.ENDED,
    )


@pytest.mark.usefixtures("worker_between_requests")
def test_a_sharer_of_its_own_stores_lease_gets_no_switch_and_the_post_is_refused(
    fake: FakeProvider,  # noqa: F811
) -> None:
    holder, sharer = _sharer(fake)
    billing, _ = client_for(Role.BILLING, "billing-same-store")
    assert 'id="switch-own-account"' in _store_page(billing, sharer)
    same_store(holder, sharer)
    sharer = Lease.objects.get(pk=sharer.pk)
    assert 'id="switch-own-account"' not in _store_page(billing, sharer)
    account = detail_cards.lease_account(sharer)
    assert account.shares is not None and account.switchable is False
    assert lease_service.switch_refusal(sharer) == lease_service.SWITCH_SAME_STORE
    jobs_before = Job.objects.filter(kind=JobKind.LEASE_ACCOUNT_SWITCH).count()
    response = billing.post(_switch_path(sharer), follow=True)
    message = lease_service.SWITCH_MESSAGES[lease_service.SWITCH_SAME_STORE]
    assert html.escape(message) in response.content.decode()
    assert Job.objects.filter(kind=JobKind.LEASE_ACCOUNT_SWITCH).count() == jobs_before
    assert LeaseBilling.objects.get(lease=sharer).shares_account_of_id == holder.pk


def test_the_worker_refuses_a_same_store_switch_too(fake: FakeProvider) -> None:  # noqa: F811
    holder, sharer = _sharer(fake)
    same_store(holder, sharer)
    outcome = lease_service.switch_to_own_account(sharer.pk, move_key=uuid.uuid4(), actor=None)
    assert (outcome.ok, outcome.reason) == (False, lease_service.SWITCH_SAME_STORE)
    assert LeaseBilling.objects.get(lease=sharer).shares_account_of_id == holder.pk
    assert not [name for name, _ in fake.calls if name == "list_payment_methods"]
