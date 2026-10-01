"""Invoice numbers ``INV-YYYY-NNNN`` (PLAN-007 B2; owner's format, DATA_OWNERSHIP §6)."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from django.db import transaction

from ledger.models import Invoice, Operator
from ledger.services import numbering
from payments.models import SyncState
from tests.test_lease_services import _race
from tests.test_operator_sync import requires_postgresql

pytestmark = pytest.mark.django_db


def take(year: int = 2026) -> str:
    with transaction.atomic():
        return numbering.next_invoice_number(year)


def test_numbers_are_consecutive_per_year_and_restart_each_year() -> None:
    assert [take(2026), take(2026), take(2027), take(2026)] == [
        "INV-2026-0001",
        "INV-2026-0002",
        "INV-2027-0001",
        "INV-2026-0003",
    ]
    assert SyncState.objects.get(job_name="invoice_number:2026").cursor == "3"


def test_a_rolled_back_transaction_returns_its_number() -> None:
    take()
    with pytest.raises(RuntimeError, match="invoice failed"), transaction.atomic():
        assert numbering.next_invoice_number(2026) == "INV-2026-0002"
        raise RuntimeError("invoice failed")
    assert take() == "INV-2026-0002"  # gap-free


def test_a_number_already_on_an_invoice_is_passed_over_never_reused() -> None:
    operator = Operator.objects.create(display_name="Numbering Ltd")
    for number in ("INV-2026-0001", "INV-2026-0002"):
        Invoice.objects.create(
            invoice_number=number,
            operator=operator,
            issue_date="2026-10-01",
            due_date="2026-10-01",
            subtotal=Decimal("1.00"),
            total=Decimal("1.00"),
        )
    assert take() == "INV-2026-0003"
    assert take() == "INV-2026-0004"


def test_the_sequence_grows_past_9999() -> None:
    SyncState.objects.create(job_name="invoice_number:2026", cursor="9999")
    assert take() == "INV-2026-10000"


@pytest.mark.parametrize("year", [1999, 2101])
def test_a_year_out_of_range_is_refused(year: int) -> None:
    with pytest.raises(ValueError, match="out of range"), transaction.atomic():
        numbering.next_invoice_number(year)


@pytest.mark.django_db(transaction=True)
def test_outside_a_transaction_is_refused() -> None:
    with pytest.raises(RuntimeError, match="inside a transaction"):
        numbering.next_invoice_number(2026)
    assert not SyncState.objects.exists()


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_concurrent_takers_never_share_a_number() -> None:
    lock = threading.Lock()
    taken: list[str] = []

    def worker() -> None:
        for _ in range(5):
            number = take()
            with lock:
                taken.append(number)

    _race(worker, worker, worker)
    assert sorted(taken) == [f"INV-2026-{n:04d}" for n in range(1, 16)]
