"""Adversarial cases for PLAN-012d Batch B (manual payment allocation and lease credits), by
the QA owner (plan §6 B, ADR-0013). A charge of another store in the allocation is refused and
nothing is written; a replayed key never pays twice; a credit cannot be applied beyond its
remaining; random allocations never lose or double count a cent; viewer and anonymous are
refused; a card number in the note reaches no row."""

from __future__ import annotations

import random
import uuid
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.db.models import Sum
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    AuditLog,
    Lease,
    LeaseCredit,
    OneOffCharge,
    Payment,
    PaymentChannel,
    PaymentStatus,
    RentCharge,
)
from ledger.services import pnl, receivables
from tests.receivables_helpers import a_lease, a_one_off_charge, a_rent_charge, a_store, an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
ZERO = D("0.00")
TODAY = date(2026, 10, 15)
CARD = "4111 1111 1111 1111"


@pytest.fixture(autouse=True)
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr("django.utils.timezone.localdate", lambda *a, **k: TODAY)
    return TODAY


def _lease() -> Lease:
    lease = a_lease(a_store(), an_operator(), started=date(2026, 8, 1), channel=PaymentChannel.WIRE)
    assert lease.operator is not None
    return lease


def _charges(lease: Lease) -> tuple[RentCharge, OneOffCharge]:
    assert lease.operator is not None
    rent = a_rent_charge(lease, date(2026, 9, 1), due="1000.00")
    other = a_one_off_charge(lease.operator, "250.00", date(2026, 9, 20), lease=lease)
    return rent, other


def _url(lease: Lease) -> str:
    return reverse("ledger:stores:record_payment", kwargs={"pk": lease.store_id})


def _post(amount: str, allocations: Mapping[Any, str], **extra: str) -> dict[str, str]:
    data = {
        "creation_key": str(uuid.uuid4()),
        "method": "zelle",
        "amount": amount,
        "paid_on": TODAY.isoformat(),
        "reference": "ZL-1",
        "note": "Paid by Zelle",
    }
    for pk, value in allocations.items():
        data[f"alloc_{pk}"] = value
    return {**data, **extra}


def _snapshot() -> tuple[int, int, int]:
    return (Payment.objects.count(), LeaseCredit.objects.count(), AuditLog.objects.count())


# --- the allocation stays within the store -----------------------------------------------


def test_a_charge_of_another_store_in_the_allocation_is_refused_and_nothing_is_written() -> None:
    mine, theirs = _lease(), _lease()
    my_rent, _ = _charges(mine)
    their_rent, their_other = _charges(theirs)
    billing, _ = client_for(Role.BILLING)
    before = _snapshot()
    for forged in (
        {my_rent.pk: "500.00", their_rent.pk: "500.00"},
        {their_other.pk: "250.00", my_rent.pk: "750.00"},
        {uuid.uuid4(): "1000.00"},
    ):
        response = billing.post(_url(mine), _post("1000.00", forged))
        assert response.status_code in (200, 302), forged
        assert _snapshot() == before, forged
    their_rent.refresh_from_db()
    assert their_rent.amount_paid == ZERO


def test_a_replayed_key_never_pays_twice() -> None:
    lease = _lease()
    rent, other = _charges(lease)
    billing, _ = client_for(Role.BILLING)
    data = _post("1250.00", {rent.pk: "1000.00", other.pk: "250.00"})
    first = billing.post(_url(lease), data)
    assert first.status_code == 302, first.content[:200]
    after_first = _snapshot()
    second = billing.post(_url(lease), data)
    assert second.status_code == 302
    assert _snapshot() == after_first
    rent.refresh_from_db()
    other.refresh_from_db()
    assert (rent.amount_paid, other.amount_paid) == (D("1000.00"), D("250.00"))
    assert Payment.objects.filter(status=PaymentStatus.SETTLED).count() == 2
    groups = set(Payment.objects.values_list("allocation_group", flat=True))
    assert len(groups) == 1 and None not in groups


# --- credits --------------------------------------------------------------------------------


def test_a_credit_cannot_be_applied_beyond_its_remaining() -> None:
    lease = _lease()
    rent, other = _charges(lease)
    billing, _ = client_for(Role.BILLING)
    # $300 received, $250 on the one-off charge, $50 kept as a credit.
    kept = billing.post(_url(lease), _post("300.00", {other.pk: "250.00"}, keep_credit="on"))
    assert kept.status_code == 302, kept.content[:200]
    credit = LeaseCredit.objects.get(lease=lease)
    assert (credit.amount, credit.remaining) == (D("50.00"), D("50.00"))
    before = _snapshot()
    # Applying $60 of a $50 credit is refused and writes nothing.
    data = _post("0.00", {}, **{f"credit_{rent.pk}": "60.00"})
    data["amount"] = ""
    response = billing.post(_url(lease), data)
    assert response.status_code in (200, 302)
    assert _snapshot() == before
    credit.refresh_from_db()
    assert credit.remaining == D("50.00")
    # Two applications in two requests cannot together exceed it either.
    assert billing.post(
        _url(lease), _post("0.00", {}, **{f"credit_{rent.pk}": "50.00"})
    ).status_code in (
        200,
        302,
    )
    credit.refresh_from_db()
    assert credit.remaining >= ZERO
    again = billing.post(_url(lease), _post("0.00", {}, **{f"credit_{rent.pk}": "1.00"}))
    assert again.status_code in (200, 302)
    credit.refresh_from_db()
    assert credit.remaining >= ZERO
    applied = Payment.objects.filter(credit=credit).aggregate(total=Sum("amount"))
    assert (applied["total"] or ZERO) <= D("50.00")


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_random_allocations_never_lose_or_double_count_a_cent(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - test data, not security
    lease = _lease()
    assert lease.operator is not None
    charges: list[RentCharge | OneOffCharge] = [
        a_rent_charge(lease, date(2026, 8, 1), due="900.00"),
        a_rent_charge(lease, date(2026, 9, 1), due="900.00"),
        a_one_off_charge(lease.operator, "300.00", date(2026, 9, 10), lease=lease),
        a_one_off_charge(lease.operator, "120.50", date(2026, 9, 20), lease=lease),
    ]
    billed = sum((D("900.00"), D("900.00"), D("300.00"), D("120.50")), ZERO)
    billing, _ = client_for(Role.BILLING)
    cash = ZERO
    for _ in range(8):
        balances = {}
        for charge in charges:
            charge.refresh_from_db()
            if charge.balance > 0:
                balances[charge.pk] = Decimal(charge.balance)
        if not balances:
            break
        amount = (D(rng.randint(100, 60_000)) / 100).quantize(D("0.01"))
        allocations: dict[Any, str] = {}
        left = amount
        for pk, balance in balances.items():
            if left <= 0:
                break
            share = min(balance, (D(rng.randint(0, int(left * 100))) / 100).quantize(D("0.01")))
            if share > 0:
                allocations[pk] = str(share)
                left -= share
        data = _post(str(amount), allocations, keep_credit="on" if left > 0 else "")
        response = billing.post(_url(lease), data)
        if response.status_code == 302 and "payments" in response["Location"]:
            cash += amount
    direct = sum(
        (
            p.amount
            for p in Payment.objects.filter(status=PaymentStatus.SETTLED).exclude(method="credit")
        ),
        ZERO,
    )
    kept = sum((c.amount for c in LeaseCredit.objects.all()), ZERO)
    assert direct + kept == cash
    remaining = sum((c.remaining for c in LeaseCredit.objects.all()), ZERO)
    applied = sum((p.amount for p in Payment.objects.filter(method="credit")), ZERO)
    assert remaining == kept - applied and remaining >= ZERO
    owed = sum((row.balance for row in receivables.open_balances(TODAY, lease.operator_id)), ZERO)
    assert owed == billed - direct - applied
    query = pnl.parse_query({"from": "2026-08", "to": "2026-10"}, today=TODAY)
    rows = [r for r in pnl.rows(query) if r.store_id == lease.store_id]
    assert sum((r.rent_collected + r.fees_collected for r in rows), ZERO) == direct + applied


# --- roles and guarded text --------------------------------------------------------------------


def test_viewer_and_anonymous_cannot_record_and_a_card_number_reaches_no_row() -> None:
    lease = _lease()
    rent, _ = _charges(lease)
    viewer, _ = client_for(Role.VIEWER)
    assert viewer.get(_url(lease)).status_code == 403
    assert viewer.post(_url(lease), _post("10.00", {rent.pk: "10.00"})).status_code == 403
    assert Client().post(_url(lease), _post("10.00", {rent.pk: "10.00"})).status_code == 401
    billing, _ = client_for(Role.BILLING)
    before = _snapshot()
    response = billing.post(
        _url(lease), _post("10.00", {rent.pk: "10.00"}, note=f"Paid by card {CARD}")
    )
    assert response.status_code in (200, 302)
    assert _snapshot() == before
    response = billing.post(
        _url(lease), _post("10.00", {rent.pk: "10.00"}, reference=CARD.replace(" ", ""))
    )
    assert response.status_code in (200, 302)
    assert _snapshot() == before
    dump = " ".join(str(r.after) for r in AuditLog.objects.all()).replace(" ", "")
    assert "4111111111111111" not in dump
    rent.refresh_from_db()
    assert rent.amount_paid == ZERO
