"""PLAN-015j task 6 (§11 A): the store page's "Record a payment" pays a Prospective lease's
open charges when the store has no current lease; End lease and the transfer wizard read
exactly what they read before (``lease_end.current_lease``).

Today is 2026-11-03; the lease starts 2026-11-15 (prospective) with a $3,000.00 deposit and a
$500.00 sign fee, as in ``tests/test_plan015j_today_probe.py``."""

from __future__ import annotations

import html
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.test import Client

from accounts.roles import Role
from ledger.models import (
    CollectionStatus,
    InstallmentPlan,
    InvoiceDraft,
    Lease,
    LeaseEnd,
    LeaseTransfer,
    OneOffCharge,
    Payment,
    RentCharge,
)
from ledger.services import installments, lease_end, payments_entry, transfer
from tests import test_new_lease_page as new_lease
from tests.receivables_helpers import a_lease
from tests.test_new_lease_page import create_data, page, post_create, vacant_store
from tests.test_operator_views import client_for
from tests.test_plan015j_today_probe import send_move_in

pytestmark = pytest.mark.django_db

D = Decimal
today = new_lease.today
admin = new_lease.admin
TODAY = new_lease.TODAY  # 2026-11-03
DEP = "setup_security_deposit"


def save(client: Client, **fields: str) -> Lease:
    store = vacant_store()
    operator = new_lease.an_operator_with("Prospect Person", email="prospect@example.com")
    data = create_data(
        store, operator, **{f"{DEP}_price": "3000.00", "setup_sign_fee_price": "500", **fields}
    )
    post_create(client, store, data)
    lease = Lease.objects.get(store=store)
    assert lease.collection_status == CollectionStatus.PROSPECTIVE
    return lease


def billing() -> tuple[Client, Any]:
    return client_for(Role.BILLING, f"billing-{uuid.uuid4().hex[:6]}")


def entry_path(lease: Lease) -> str:
    return f"/stores/{lease.store_id}/payments/new/"


def post_entry(client: Client, lease: Lease, allocations: dict[Any, str], amount: str) -> Any:
    data = {
        "creation_key": str(uuid.uuid4()),
        "method": "wire",
        "amount": amount,
        "paid_on": TODAY.isoformat(),
        "reference": "W-1",
        "note": "Wire received before the start.",
        **{f"alloc_{pk}": value for pk, value in allocations.items()},
    }
    return client.post(entry_path(lease), data)


def listed(lease: Lease) -> list[Any]:
    return [row.charge_id for row in payments_entry.open_charges(lease, TODAY)]


def state(item: OneOffCharge | RentCharge) -> tuple[Any, ...]:
    item.refresh_from_db()
    if isinstance(item, RentCharge):
        return (item.status, item.amount_paid)
    return (item.status, item.amount_paid, item.balance)


# --- payments_entry.current_lease -----------------------------------------------------------


def test_a_store_with_only_a_prospective_lease_pays_that_lease(admin: Client, today: date) -> None:
    lease = save(admin)
    assert payments_entry.current_lease(lease.store, TODAY) == lease
    # End lease and the transfer wizard still see no lease (unchanged).
    assert lease_end.current_lease(lease.store, TODAY) is None


def test_a_store_with_a_current_lease_and_a_prospective_successor_pays_the_current(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    current = a_lease(lease.store, started=date(2025, 1, 1))
    assert payments_entry.current_lease(lease.store, TODAY) == current
    assert lease_end.current_lease(lease.store, TODAY) == current


def test_the_earliest_upcoming_prospective_lease_not_ended_is_paid(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    later = a_lease(lease.store, started=date(2027, 1, 1), status=CollectionStatus.PROSPECTIVE)
    ended = a_lease(lease.store, started=date(2026, 11, 10), status=CollectionStatus.PROSPECTIVE)
    Lease.objects.filter(pk=ended.pk).update(ended_on=date(2026, 11, 12))
    assert payments_entry.current_lease(lease.store, TODAY) == lease
    assert later.started_on == date(2027, 1, 1)  # a later start is not chosen


def test_a_store_with_no_lease_still_refuses(today: date) -> None:
    store = vacant_store(440, "Empty Store")
    assert payments_entry.current_lease(store, TODAY) is None
    outcome = payments_entry.record_entry(
        store,
        payments_entry.EntryInput(
            creation_key=uuid.uuid4(),
            method="wire",
            amount=D("10.00"),
            paid_on=TODAY,
            reference="",
            note="Wire.",
            allocations={},
        ),
        actor=None,
        today=TODAY,
    )
    assert (outcome.ok, outcome.message) == (False, payments_entry.NO_LEASE)


# --- The store page lists and records ---------------------------------------------------------


def _record_in_two(client: Client, lease: Lease, item: OneOffCharge | RentCharge) -> None:
    total = D(item.balance if isinstance(item, OneOffCharge) else item.amount_due)
    for amount in ("200.00", str(total - D("200.00"))):
        response = post_entry(client, lease, {item.pk: amount}, amount)
        assert response.status_code == 302
        assert response["Location"].endswith("?tab=payments#tab-payments"), response["Location"]


def test_the_entry_pays_a_dated_deposit_in_part_then_in_full(admin: Client, today: date) -> None:
    lease = save(admin, **{f"{DEP}_when": "on_date", f"{DEP}_on": "2026-12-01"})
    deposit = OneOffCharge.objects.get(lease=lease, fee_type__refundable=True)
    client, _ = billing()
    text = page(client, entry_path(lease))
    assert f'name="alloc_{deposit.pk}"' in text
    assert deposit.pk in listed(lease)
    response = post_entry(client, lease, {deposit.pk: "200.00"}, "200.00")
    assert response.status_code == 302
    assert state(deposit) == ("partial", D("200.00"), D("2800.00"))
    post_entry(client, lease, {deposit.pk: "2800.00"}, "2800.00")
    assert state(deposit) == ("paid", D("3000.00"), D("0.00"))
    assert Payment.objects.filter(one_off_charge=deposit).count() == 2
    lease.refresh_from_db()
    assert lease.collection_status == CollectionStatus.PROSPECTIVE


def test_the_entry_pays_an_installment_of_the_deposit_plan(admin: Client, today: date) -> None:
    lease = save(
        admin,
        **{
            f"{DEP}_when": "installments",
            f"{DEP}_count": "3",
            f"{DEP}_first_due": "2026-11-10",
            f"{DEP}_cadence": "monthly",
        },
    )
    plan = InstallmentPlan.objects.get(lease=lease)
    first = installments.installments_of(plan)[0]
    assert first.pk in listed(lease)
    client, _ = billing()
    _record_in_two(client, lease, first)
    assert state(first) == ("paid", D("1000.00"), D("0.00"))


def test_the_entry_pays_a_charge_on_the_unsent_move_in_draft(admin: Client, today: date) -> None:
    lease = save(admin)
    fee = OneOffCharge.objects.get(lease=lease, fee_type__code="sign_fee")
    assert InvoiceDraft.objects.get(lease=lease).status == "draft"
    assert fee.pk in listed(lease)
    client, _ = billing()
    _record_in_two(client, lease, fee)
    assert state(fee) == ("paid", D("500.00"), D("0.00"))


def test_the_entry_pays_the_rent_charge_of_a_sent_first_invoice(admin: Client, today: date) -> None:
    lease = save(admin)
    send_move_in(admin, lease)
    [rent] = RentCharge.objects.filter(lease=lease)
    assert rent.pk in listed(lease)
    client, _ = billing()
    response = post_entry(client, lease, {rent.pk: "500.00"}, "500.00")
    assert response.status_code == 302
    rent.refresh_from_db()
    assert rent.amount_paid == D("500.00") and rent.status == "partial"


def test_a_viewer_is_still_refused(admin: Client, today: date) -> None:
    lease = save(admin)
    viewer = client_for(Role.VIEWER, "viewer-prospect")[0]
    assert viewer.get(entry_path(lease)).status_code == 403


# --- Lease end and transfer exactly as before -----------------------------------------------


def test_end_lease_is_not_offered_and_refused_for_a_store_whose_only_lease_is_prospective(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    store_page = page(admin, f"/stores/{lease.store_id}/")
    assert 'id="store-end-lease"' not in store_page
    assert 'id="store-transfer"' not in store_page
    response = admin.post(f"/stores/{lease.store_id}/end-lease/")
    assert response.status_code == 302
    assert html.escape(lease_end.NOT_CURRENT) in page(admin, response["Location"])
    assert not LeaseEnd.objects.exists()


def test_the_transfer_wizard_refuses_to_start_for_a_store_whose_only_lease_is_prospective(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    with pytest.raises(ValueError, match=transfer.NOT_CURRENT):
        transfer.start(lease.store, None, today=TODAY)
    assert not LeaseTransfer.objects.exists() and not LeaseEnd.objects.exists()


def test_the_transfer_wizard_starts_from_the_current_lease_not_the_successor(
    admin: Client, today: date
) -> None:
    lease = save(admin)
    current = a_lease(lease.store, started=date(2025, 1, 1))
    started = transfer.start(lease.store, None, today=TODAY)
    assert started.outgoing_lease_id == current.pk
