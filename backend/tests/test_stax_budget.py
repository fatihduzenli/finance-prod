"""Failure budget (PLAN-005 §6 row 10, D8): 5 per minute or 25 per hour refuse a debit."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from django.test import override_settings

from payments.errors import FailureBudgetExhausted, ProviderUnavailable, ValidationFailed
from payments.models import SyncState
from payments.provider import PayWithStoredMethod
from payments.stax.adapter import StaxProvider
from payments.stax.budget import HOUR_LIMIT, MINUTE_LIMIT, FailureBudget
from payments.stax.state import BUDGET_ROW, MemoryStateStore, SyncStateStore
from payments.stax.transport import Method
from tests.stax_helpers import FakeClock, always, make_rig


@pytest.fixture(autouse=True)
def _worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker", STAX_PAY_INVOICE_ENABLED=True):
        yield


def _budget() -> tuple[FailureBudget, FakeClock]:
    clock = FakeClock()
    return FailureBudget(MemoryStateStore(), clock.as_clock()), clock


def test_limits_are_half_of_the_stax_limits() -> None:
    assert (MINUTE_LIMIT, HOUR_LIMIT) == (5, 25)


def test_four_failures_in_a_minute_allow_a_debit_and_five_refuse_it() -> None:
    budget, clock = _budget()
    for _ in range(4):
        budget.record_http_failure()
        clock.advance(10_000)
    budget.check_can_debit()
    budget.record_http_failure()
    with pytest.raises(FailureBudgetExhausted):
        budget.check_can_debit()


def test_a_payment_failure_counts_like_an_http_failure() -> None:
    budget, _ = _budget()
    for _ in range(4):
        budget.record_http_failure()
    budget.record_payment_failure("txn-returned-1")
    with pytest.raises(FailureBudgetExhausted):
        budget.check_can_debit()
    assert budget.counts() == (5, 5)


def test_payment_failures_alone_exhaust_the_budget() -> None:
    budget, _ = _budget()
    for index in range(5):
        budget.record_payment_failure(f"txn-{index}")
    with pytest.raises(FailureBudgetExhausted):
        budget.check_can_debit()


def test_the_minute_window_expires() -> None:
    budget, clock = _budget()
    for _ in range(5):
        budget.record_http_failure()
    clock.advance(60_000)
    budget.check_can_debit()
    assert budget.counts() == (0, 5)


def test_twenty_five_in_an_hour_refuse_and_the_hour_window_expires() -> None:
    budget, clock = _budget()
    for _ in range(HOUR_LIMIT - 1):
        budget.record_http_failure()
        clock.advance(120_000)  # never more than one in any minute
    budget.check_can_debit()
    budget.record_http_failure()  # the 25th, at 48 minutes
    with pytest.raises(FailureBudgetExhausted):
        budget.check_can_debit()
    clock.advance(3_600_000 - 48 * 60_000)  # the first failure is now exactly one hour old
    budget.check_can_debit()
    assert budget.counts() == (0, 24)


@pytest.mark.parametrize("bad", ["", " ", "txn 1"])
def test_a_payment_failure_needs_an_identifier(bad: str) -> None:
    budget, _ = _budget()
    with pytest.raises(ValidationFailed):
        budget.record_payment_failure(bad)
    assert budget.counts() == (0, 0)


def test_the_transport_records_every_failed_attempt() -> None:
    rig = make_rig(always(503))
    with pytest.raises(ProviderUnavailable):
        rig.transport.send(Method.GET, "/invoice/{id}", object_id="inv-1", idempotent=True)
    assert rig.budget.counts() == (4, 4)


def test_pay_invoice_is_refused_when_the_budget_is_exhausted() -> None:
    rig = make_rig(always(200, {"id": "txn-1"}))
    for _ in range(MINUTE_LIMIT):
        rig.budget.record_payment_failure("txn-returned")
    with pytest.raises(FailureBudgetExhausted):
        StaxProvider(rig.transport).pay_invoice(
            "inv-1", PayWithStoredMethod("pm-1", str(uuid.uuid4()))
        )
    assert rig.recorder.requests == []


def test_budget_does_not_gate_reads() -> None:
    rig = make_rig(always(200, {"id": "inv-1"}))
    for _ in range(MINUTE_LIMIT):
        rig.budget.record_http_failure()
    StaxProvider(rig.transport).get_invoice("inv-1")
    assert len(rig.recorder.requests) == 1


@pytest.mark.django_db
def test_the_budget_is_read_back_from_sync_state() -> None:
    clock = FakeClock()
    budget = FailureBudget(SyncStateStore(), clock.as_clock())
    for _ in range(3):
        budget.record_http_failure()
    budget.record_payment_failure("txn-returned")
    assert SyncState.objects.filter(job_name=BUDGET_ROW).exists()
    clock.advance(30_000)
    restarted = FailureBudget(SyncStateStore(), clock.as_clock())
    assert restarted.counts() == (4, 4)
    restarted.record_http_failure()
    with pytest.raises(FailureBudgetExhausted):
        restarted.check_can_debit()
