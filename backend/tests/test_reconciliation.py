"""The nightly reconciliation (docs/qa/PLAN-010-worker-and-reconciliation.md Batch B: D5 to
D12; §5 test plan; Prompt 10 item 5).

``payments.get_provider`` is a :class:`~payments.fake.FakeProvider` whose records the tests
write, except in the 500-invoice test, which runs the real Stax adapter, transport and
process-wide limiter against an ``httpx.MockTransport`` on a fake clock. No test reaches
the network.
"""

from __future__ import annotations

import ast
import io
import re
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

import payments
from accounts.roles import Role
from finance.env_guard import EnvGuardError, validate
from ledger.forms import ResolveMismatchForm
from ledger.models import AuditLog, Invoice, Payment, PaymentStatus
from ledger.services import returns
from payments import alerts, invoice_sync, jobs
from payments.errors import ProviderUnavailable
from payments.fake import FakeProvider
from payments.management.commands.reconcile import QUEUED
from payments.models import (
    ExternalRef,
    Job,
    JobKind,
    JobStatus,
    MismatchKind,
    ReconciliationMismatch,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
    SyncState,
)
from payments.provider import (
    AchRejection,
    DateRange,
    DepositDetail,
    DepositRecord,
    TransactionQuery,
    mask_identifier,
)
from payments.reconciliation import (
    ALREADY_RUNNING,
    CURSOR_ROW,
    lock,
    mismatches,
    reconcile_cursor,
    run_reconciliation,
    schedule,
    steps,
)
from payments.reconciliation.schedule import ALREADY_STARTED, NOT_DUE, should_run_scheduled
from payments.stax.adapter import StaxProvider
from payments.stax.breaker import CircuitBreaker
from payments.stax.budget import FailureBudget
from payments.stax.limiter import SECOND_MS, reset_limiter
from payments.stax.state import MemoryStateStore
from payments.stax.transport import StaxTransport
from payments.sync_writer import provider_write
from tests.job_helpers import run_queued_jobs
from tests.stax_helpers import FakeClock, Recorder
from tests.test_jobs import provider_reaching_functions
from tests.test_lease_services import make_operator
from tests.test_operator_sync import requires_postgresql
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)
from tests.test_webhook_handlers import (
    INVOICE,
    TXN,
    deliver,
    make_world,
    payment_for,
    provider_txn,
    settle,
)

pytestmark = pytest.mark.django_db

REPO = Path(__file__).resolve().parents[2]
#: A weekday at 02:30 New York (EDT) on which nothing else happens.
NOW = datetime(2026, 10, 6, 6, 30, tzinfo=UTC)
NEW_YORK = {"RECONCILE_TIMEZONE": "America/New_York", "RECONCILE_LOCAL_TIME": time(2, 0)}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    """Every ``sentry_sdk.capture_message`` the alerts module makes."""
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        "sentry_sdk.capture_message", lambda *args, **kwargs: events.append((args, kwargs))
    )
    return events


def mismatch_kinds() -> list[str]:
    return sorted(ReconciliationMismatch.objects.values_list("kind", flat=True))


def cursor_text() -> str:
    row = SyncState.objects.filter(job_name=CURSOR_ROW).first()
    return row.cursor if row is not None else ""


def pending_payment(fake: FakeProvider, initiated_on: date, txn: str = TXN) -> Payment:
    """An ACH payment made by its create event, pending, initiated on ``initiated_on``."""
    provider_txn(fake, txn)
    assert deliver("create_transaction", id=txn) == "processed"
    payment = payment_for(txn)
    Payment.objects.filter(pk=payment.pk).update(initiated_on=initiated_on)
    payment.refresh_from_db()
    return payment


# --- Prompt 10 item 5: the four required tests ------------------------------------------------


@pytest.mark.parametrize("ref_too", [False, True], ids=["row-deleted", "row-and-ref-deleted"])
def test_a_deleted_local_payment_is_restored_from_the_transaction_list(
    fake: FakeProvider, ref_too: bool
) -> None:
    make_world(fake)
    lost = pending_payment(fake, NOW.date())
    if ref_too:
        ExternalRef.objects.filter(entity_id=lost.pk).delete()
    Payment.objects.filter(pk=lost.pk).delete()

    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)

    assert run.status == ReconciliationStatus.DONE
    restored = payment_for(TXN)
    assert restored.pk != lost.pk
    assert restored.status == PaymentStatus.PENDING
    assert restored.amount == fake.transactions[TXN].total
    [found] = ReconciliationMismatch.objects.all()
    assert found.kind == MismatchKind.PAYMENT_RESTORED
    assert (found.entity_type, found.entity_id) == ("payment", restored.pk)
    assert found.resolved_at is None
    assert TXN not in found.detail and mask_identifier(TXN) in found.detail
    assert run.counts["b"]["restored"] == 1
    # The next run finds the payment and records nothing new.
    again = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(hours=1))
    assert again.counts["b"] == {"checked": 1, "matched": 1, "pages": 1}
    assert ReconciliationMismatch.objects.count() == 1


def test_a_wrong_local_invoice_status_is_overwritten_and_logged(fake: FakeProvider) -> None:
    world = make_world(fake)
    invoice_sync.record_provider_status(world.invoice.pk, "PAID")
    assert fake.invoices[INVOICE].raw_status == "SENT"

    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)

    world.invoice.refresh_from_db()
    assert world.invoice.provider_status == "SENT"
    [found] = ReconciliationMismatch.objects.all()
    assert found.kind == MismatchKind.INVOICE_STATUS_DIFFERS
    assert found.entity_id == world.invoice.pk
    assert found.resolved_at == NOW
    assert found.resolution_note == mismatches.OVERWRITTEN
    assert "PAID" in found.detail and "SENT" in found.detail
    assert INVOICE not in found.detail
    assert run.counts["a"]["mismatched"] == 1
    assert AuditLog.objects.filter(
        action="invoice.provider_state_changed", entity_id=str(world.invoice.pk)
    ).exists()


@pytest.mark.parametrize("letter", ["a", "b", "c", "d", "e"])
def test_a_failure_in_any_step_leaves_the_cursor_and_records_the_step(
    fake: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[Any],
    letter: str,
) -> None:
    """Prompt 10 item 5 names step (c); §5 asks for every step (parametrised)."""
    working = steps.business_days_between
    make_world(fake)
    pending_payment(fake, NOW.date())
    first = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert first.status == ReconciliationStatus.DONE
    assert cursor_text() == NOW.isoformat()
    fake.deposits.append(DepositRecord(external_id="batch-0001", raw_status="", fetched_at=NOW))
    failing = {
        "a": "get_invoice",
        "b": "list_transactions",
        "c": "get_deposit",
        "d": "list_ach_rejections",
    }
    if letter in failing:
        fake.fail_next(failing[letter], ProviderUnavailable(503))
    else:

        def broken(start: date, end: date) -> int:
            raise RuntimeError

        monkeypatch.setattr(steps, "business_days_between", broken)

    later = NOW + timedelta(days=1)
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=later)

    assert run.status == ReconciliationStatus.FAILED
    assert run.step_reached == letter
    assert run.error == ("ProviderUnavailable" if letter in failing else "RuntimeError")
    assert cursor_text() == NOW.isoformat()
    assert reconcile_cursor() == NOW
    assert sorted(run.counts) == [step for step in "abcde" if step < letter]
    [failed_event] = [
        event for event in sentry_events if event[1]["fingerprint"] == ["reconciliation-failed"]
    ]
    assert failed_event[1]["level"] == "fatal"
    assert failed_event[1]["extras"] == {"step_reached": letter, "error": run.error}
    # The next run redoes the window and then advances the cursor.
    monkeypatch.setattr(steps, "business_days_between", working)
    healed = run_reconciliation(ReconciliationTrigger.MANUAL, now=later + timedelta(hours=1))
    assert healed.status == ReconciliationStatus.DONE
    assert reconcile_cursor() == later + timedelta(hours=1)


def _stax_reply(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.startswith("/invoice/"):
        external_id = path.rsplit("/", 1)[1]
        return httpx.Response(
            200, json={"id": external_id, "status": "SENT", "total": "100.00", "customer_id": "c"}
        )
    if path in ("/transaction", "/query/deposit", "/query/statement/v3/ach-rejects"):
        return httpx.Response(200, json=[])
    raise AssertionError(f"unexpected request {request.method} {path}")


def _max_in_window(departures: list[int], width: int) -> int:
    ordered = sorted(departures)
    return max(sum(1 for other in ordered if start <= other < start + width) for start in ordered)


@pytest.fixture
def five_hundred_invoices() -> list[str]:
    operator = make_operator(email="bulk@example.com", customer="cus-bulk")
    invoices = [
        Invoice(
            invoice_number=f"INV-2026-{index:04d}",
            operator=operator,
            issue_date=date(2026, 10, 1),
            due_date=date(2026, 10, 15),
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
        )
        for index in range(1, 501)
    ]
    Invoice.objects.bulk_create(invoices)
    with provider_write():
        Invoice.objects.update(provider_status="SENT")
    ExternalRef.objects.bulk_create(
        [
            ExternalRef(
                entity_type="invoice",
                entity_id=invoice.pk,
                provider="stax_pay",
                external_kind="invoice",
                external_id=f"inv-bulk-{index:04d}",
            )
            for index, invoice in enumerate(invoices, start=1)
        ]
    )
    return [f"inv-bulk-{index:04d}" for index in range(1, 501)]


def test_500_invoices_with_the_real_limiter_never_depart_more_than_two_a_second(
    monkeypatch: pytest.MonkeyPatch, five_hundred_invoices: list[str]
) -> None:
    clock = FakeClock()
    limiter = reset_limiter(clock.as_clock())
    recorder = Recorder(_stax_reply, clock)
    transport = StaxTransport(
        http_transport=recorder.transport(),
        breaker=CircuitBreaker(
            MemoryStateStore(), clock.as_clock(), on_open=lambda opened, reopen: None
        ),
        budget=FailureBudget(MemoryStateStore(), clock.as_clock()),
        clock=clock.as_clock(),
    )
    provider = StaxProvider(transport)
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    try:
        with override_settings(STAX_CALLER="worker", STAX_PROVIDER="stax"):
            run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    finally:
        transport.close()
        reset_limiter()

    assert run.status == ReconciliationStatus.DONE, run.error
    assert run.counts["a"] == {"checked": 500, "matched": 500}
    # 500 invoices plus one page each of transactions, deposits and rejections.
    assert len(recorder.arrivals) == 503
    assert run.requests == 503
    assert limiter.departures == 503
    assert _max_in_window(recorder.arrivals, SECOND_MS) <= 2
    assert max(Counter(at // SECOND_MS for at in recorder.arrivals).values()) <= 2
    # The limiter really paced the run: 503 requests at 100 a minute take over 5 minutes.
    assert max(recorder.arrivals) >= 5 * 60_000
    assert ReconciliationMismatch.objects.count() == 0


def test_the_window_is_the_lookback_first_then_the_cursor_minus_the_overlap(
    fake: FakeProvider,
) -> None:
    def starts() -> list[date]:
        """The start of each transaction and rejection query."""
        found: list[date] = []
        for name, args in fake.calls:
            if name not in ("list_transactions", "list_ach_rejections"):
                continue
            query = args[0]
            if isinstance(query, TransactionQuery) and query.date_range is not None:
                found.append(query.date_range.start)
            elif isinstance(query, DateRange):
                found.append(query.start)
        return found

    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert set(starts()) == {(NOW - timedelta(days=35)).date()}
    fake.calls.clear()
    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(days=1))
    assert set(starts()) == {(NOW - timedelta(hours=48)).date()}
    fake.calls.clear()
    # A cursor older than a year is clamped: the provider's ranges span a year at most.
    SyncState.objects.filter(job_name=CURSOR_ROW).update(
        cursor=(NOW - timedelta(days=900)).isoformat()
    )
    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(days=2))
    assert set(starts()) == {(NOW + timedelta(days=2) - timedelta(days=364)).date()}


def test_a_run_that_would_spend_over_half_the_hourly_budget_is_refused_before_any_call(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, sentry_events: list[Any]
) -> None:
    make_world(fake)
    monkeypatch.setattr("payments.reconciliation.BUDGET_REQUESTS", 3)
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert run.status == ReconciliationStatus.FAILED
    assert run.error == "BudgetExceeded"
    assert run.step_reached == ""
    assert fake.calls == []
    assert cursor_text() == ""


# --- D5: the local-time gate -------------------------------------------------------------------


def fire(monkeypatch: pytest.MonkeyPatch, moment: datetime, *, work: bool = True) -> str:
    """One firing of the scheduled job at ``moment``: ``reconcile --scheduled`` with
    ``STAX_CALLER=none``, as the job container runs it; then, with ``work``, the worker
    (``STAX_CALLER=worker``) runs what was queued, at the same moment. The command's
    output."""
    monkeypatch.setattr("django.utils.timezone.now", lambda: moment)
    out = io.StringIO()
    provider = payments.get_provider()
    assert isinstance(provider, FakeProvider)
    calls_before = len(provider.calls)
    with override_settings(STAX_CALLER="none"):
        call_command("reconcile", "--scheduled", stdout=out)
    assert len(provider.calls) == calls_before  # the firing calls no provider
    if work:
        with override_settings(STAX_CALLER="worker"):
            run_queued_jobs()
    return out.getvalue().strip()


def started_runs() -> list[ReconciliationRun]:
    return list(ReconciliationRun.objects.exclude(status=ReconciliationStatus.SKIPPED))


@pytest.mark.parametrize(
    ("day", "runs_at"),
    [
        (date(2026, 3, 8), 7),  # clocks go forward at 02:00: 06:00 UTC is 01:00 EST
        (date(2026, 11, 1), 7),  # clocks go back at 02:00: 06:00 UTC is 01:00 EST
        (date(2026, 7, 15), 6),  # summer: 06:00 UTC is 02:00 EDT
        (date(2026, 1, 15), 7),  # winter: 07:00 UTC is 02:00 EST
    ],
    ids=["dst-starts", "dst-ends", "summer", "winter"],
)
def test_both_utc_firings_of_a_day_make_exactly_one_run(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, day: date, runs_at: int
) -> None:
    with override_settings(**NEW_YORK):
        first = fire(monkeypatch, datetime.combine(day, time(6, 0), tzinfo=UTC))
        second = fire(monkeypatch, datetime.combine(day, time(7, 0), tzinfo=UTC))
    queued_at = 6 if first == QUEUED else 7
    assert queued_at == runs_at
    other = second if first == QUEUED else first
    assert other.startswith(f"Status: skipped. {NOT_DUE}")
    [job] = Job.objects.all()
    assert (job.kind, job.status, job.params, job.requested_by) == (
        JobKind.RECONCILE_RUN,
        JobStatus.DONE,
        {"trigger": "scheduled"},
        None,
    )
    [run] = started_runs()
    assert run.status == ReconciliationStatus.DONE
    assert (run.trigger, run.requested_by) == (ReconciliationTrigger.SCHEDULED, None)
    assert run.started_at.hour == runs_at
    [skip] = ReconciliationRun.objects.filter(status=ReconciliationStatus.SKIPPED)
    assert skip.error.startswith(NOT_DUE)


def test_consecutive_days_across_both_changes_each_run_once(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    with override_settings(**NEW_YORK):
        for start in (date(2026, 3, 6), date(2026, 10, 30)):
            for offset in range(4):
                day = start + timedelta(days=offset)
                for hour in (6, 7):
                    fire(monkeypatch, datetime.combine(day, time(hour, 0), tzinfo=UTC))
    started = started_runs()
    assert len(started) == 8
    assert Job.objects.count() == 8
    assert {run.trigger for run in started} == {ReconciliationTrigger.SCHEDULED}
    local_days = sorted(schedule.local(run.started_at).date() for run in started)
    assert len(set(local_days)) == 8


def test_a_failed_scheduled_run_still_counts_for_its_day_and_run_now_ignores_the_gate(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    morning = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    fake.fail_next("list_transactions", ProviderUnavailable(503))
    with override_settings(**NEW_YORK):
        assert fire(monkeypatch, morning) == QUEUED
        [failed] = started_runs()
        assert failed.status == ReconciliationStatus.FAILED
        assert Job.objects.get().status == JobStatus.FAILED
        assert fire(monkeypatch, morning + timedelta(hours=1)) == (
            f"Status: skipped. {ALREADY_STARTED}"
        )
        assert Job.objects.count() == 1
        noon = morning + timedelta(hours=10)
        assert should_run_scheduled(noon)[0] is False
        manual = run_reconciliation(ReconciliationTrigger.MANUAL, now=noon)
    assert manual.status == ReconciliationStatus.DONE


@pytest.mark.parametrize("state", ["queued", "running"])
def test_the_second_firing_while_the_job_still_waits_queues_nothing(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """D5 as amended: the job container only queues; the second UTC firing finds the first
    one's job still queued (or running) and neither queues another nor counts as a run.
    It writes a ``skipped`` row, which the page leaves out of its run list."""
    morning = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)  # 02:00 EDT
    with override_settings(**NEW_YORK):
        assert fire(monkeypatch, morning, work=False) == QUEUED
        [job] = Job.objects.all()
        assert (job.status, job.params, job.requested_by) == (
            "queued",
            {"trigger": "scheduled"},
            None,
        )
        if state == "running":
            monkeypatch.setattr("django.utils.timezone.now", lambda: morning)
            claimed = jobs.claim_next()
            assert claimed is not None and claimed.pk == job.pk
        assert fire(monkeypatch, morning + timedelta(hours=1), work=False) == (
            f"Status: skipped. {ALREADY_STARTED}"
        )
    assert Job.objects.count() == 1
    assert started_runs() == []
    assert fake.calls == []
    billing, _ = client_for(Role.BILLING)
    page = billing.get("/reconciliation/").content.decode()
    assert NOT_DUE not in page
    assert "No runs yet." in page
    assert "Queued" in page or "Running" in page
    with override_settings(STAX_CALLER="worker"):
        if state == "running":
            done = jobs.run(Job.objects.get())
        else:
            [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE
    [run] = started_runs()
    assert (run.trigger, run.requested_by) == (ReconciliationTrigger.SCHEDULED, None)
    page = billing.get("/reconciliation/").content.decode()
    assert '<td data-label="How">Scheduled</td>' in page
    assert NOT_DUE not in page


def test_a_waiting_run_now_job_is_not_the_days_scheduled_run(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    morning = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    monkeypatch.setattr("django.utils.timezone.now", lambda: morning)
    admin, user = client_for(Role.ADMIN)
    admin.post("/reconciliation/run/")
    with override_settings(**NEW_YORK):
        assert fire(monkeypatch, morning, work=False) == QUEUED
    triggers = sorted(job.params.get("trigger", "manual") for job in Job.objects.all())
    assert triggers == ["manual", "scheduled"]
    with override_settings(STAX_CALLER="worker"):
        run_queued_jobs()
    runs = {run.trigger: run.requested_by for run in started_runs()}
    assert runs == {ReconciliationTrigger.MANUAL: user, ReconciliationTrigger.SCHEDULED: None}


def test_a_reconcile_job_with_an_unknown_trigger_fails_without_a_call(
    fake: FakeProvider,
) -> None:
    jobs.enqueue(JobKind.RECONCILE_RUN, jobs.RECONCILIATION, {"trigger": "nightly"}, None)
    with override_settings(STAX_CALLER="worker"):
        [done] = run_queued_jobs()
    assert done.status == JobStatus.FAILED
    assert "ValueError" in done.result
    assert ReconciliationRun.objects.count() == 0
    assert fake.calls == []


# --- D6: the lock ------------------------------------------------------------------------------


class BlockingProvider(FakeProvider):
    """Stops inside step (b) until released, so a second start meets a held lock."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = threading.Event()
        self.release = threading.Event()

    def list_transactions(self, query: Any) -> Any:
        self.inside.set()
        assert self.release.wait(timeout=30)
        return super().list_transactions(query)


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_a_second_start_while_one_runs_is_skipped_on_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = BlockingProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    results: list[ReconciliationRun] = []
    errors: list[BaseException] = []

    def first() -> None:
        try:
            results.append(run_reconciliation(ReconciliationTrigger.SCHEDULED))
        except BaseException as error:  # surfaced below
            errors.append(error)
        finally:
            connection.close()

    def second() -> None:
        try:
            results.append(run_reconciliation(ReconciliationTrigger.MANUAL))
        except BaseException as error:  # surfaced below
            errors.append(error)
        finally:
            connection.close()

    running = threading.Thread(target=first)
    running.start()
    assert provider.inside.wait(timeout=30)
    late = threading.Thread(target=second)
    late.start()
    late.join(timeout=30)
    provider.release.set()
    running.join(timeout=30)

    assert errors == []
    by_trigger = {run.trigger: run for run in results}
    assert by_trigger[ReconciliationTrigger.MANUAL].status == ReconciliationStatus.SKIPPED
    assert by_trigger[ReconciliationTrigger.MANUAL].error == ALREADY_RUNNING
    assert by_trigger[ReconciliationTrigger.SCHEDULED].status == ReconciliationStatus.DONE
    # Released: the next start takes the lock.
    assert run_reconciliation(ReconciliationTrigger.MANUAL).status == ReconciliationStatus.DONE


def test_a_held_lock_skips_the_run_and_a_stopped_run_is_marked_abandoned(
    fake: FakeProvider,
) -> None:
    if connection.vendor == "postgresql":
        pytest.skip("an advisory lock is re-entrant within one session; see the thread test")
    with lock.held() as acquired:
        assert acquired
        run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
        assert (run.status, run.error) == (ReconciliationStatus.SKIPPED, ALREADY_RUNNING)
        assert fake.calls == []
    stopped = ReconciliationRun.objects.create(
        trigger=ReconciliationTrigger.SCHEDULED,
        status=ReconciliationStatus.RUNNING,
        started_at=NOW - timedelta(hours=3),
    )
    assert run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW).status == "done"
    stopped.refresh_from_db()
    assert (stopped.status, stopped.error) == (ReconciliationStatus.FAILED, "Abandoned")


# --- D7: steps (c), (d), (e) -------------------------------------------------------------------


def test_deposits_flag_rows_without_a_settled_payment_and_settled_payments_without_a_deposit(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_world(fake)
    payment = pending_payment(fake, date(2026, 9, 28))
    settle(fake)
    assert deliver("update_transaction_settled", id=TXN) == "processed"
    Payment.objects.filter(pk=payment.pk).update(settled_on=date(2026, 9, 30))
    fake.deposits.append(DepositRecord(external_id="batch-0001", raw_status="", fetched_at=NOW))

    def detail(date_range: Any, batch_id: str) -> DepositDetail:
        return DepositDetail(
            external_id=batch_id,
            raw_status="",
            fetched_at=NOW,
            items=({"transaction_id": "txn-unknown-0009"}, {"note": "fee row"}),
        )

    monkeypatch.setattr(fake, "get_deposit", detail)
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert run.status == ReconciliationStatus.DONE
    assert run.counts["c"]["unreadable"] == 1
    assert mismatch_kinds() == [
        MismatchKind.DEPOSIT_UNMATCHED,
        MismatchKind.SETTLED_PAYMENT_UNDEPOSITED,
    ]
    unmatched = ReconciliationMismatch.objects.get(kind=MismatchKind.DEPOSIT_UNMATCHED)
    assert unmatched.entity_type == mismatches.ENTITY_PROVIDER_TRANSACTION
    assert unmatched.entity_id == mismatches.provider_entity_id("transaction", "txn-unknown-0009")
    assert "txn-unknown-0009" not in unmatched.detail
    undeposited = ReconciliationMismatch.objects.get(kind=MismatchKind.SETTLED_PAYMENT_UNDEPOSITED)
    assert undeposited.entity_id == payment.pk
    # Found again by the next run: seen, not recorded twice.
    again = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(hours=1))
    assert again.counts["c"]["seen"] == 2
    assert ReconciliationMismatch.objects.count() == 2


def test_a_rejection_no_webhook_reported_returns_the_payment_and_is_logged(
    fake: FakeProvider,
) -> None:
    world = make_world(fake)
    payment = pending_payment(fake, NOW.date())
    fake.ach_rejections.append(
        AchRejection(external_id=TXN, raw_status="", fetched_at=NOW, amount=Decimal("1500.00"))
    )
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.RETURNED
    assert payment in list(returns.open_returns())
    world.charge.refresh_from_db()
    assert world.charge.needs_review
    [found] = ReconciliationMismatch.objects.all()
    assert (found.kind, found.entity_id) == (MismatchKind.RETURN_MISSED_BY_WEBHOOK, payment.pk)
    assert run.counts["d"]["restored"] == 1
    # Already returned: the next run only matches it.
    again = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(hours=1))
    assert again.counts["d"] == {"checked": 1, "matched": 1}


def test_a_rejection_naming_no_local_payment_makes_nothing(fake: FakeProvider) -> None:
    fake.ach_rejections.append(
        AchRejection(external_id="txn-x-0001", raw_status="", fetched_at=NOW)
    )
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert run.counts["d"] == {"checked": 1, "unmatched": 1}
    assert Payment.objects.count() == 0


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 10, 2), date(2026, 10, 5), 1),  # Friday to Monday
        (date(2026, 10, 2), date(2026, 10, 9), 5),  # Friday to Friday
        (date(2026, 10, 3), date(2026, 10, 5), 1),  # Saturday to Monday
        (date(2026, 10, 3), date(2026, 10, 4), 0),  # Saturday to Sunday
        (date(2026, 10, 1), date(2026, 10, 12), 7),  # Thursday to the Monday after next
        (date(2026, 10, 5), date(2026, 10, 5), 0),
        (date(2026, 10, 9), date(2026, 10, 5), 0),
        (date(2026, 1, 1), date(2026, 12, 31), 260),
    ],
)
def test_business_days_skip_weekends(start: date, end: date, expected: int) -> None:
    assert steps.business_days_between(start, end) == expected


def test_a_pending_payment_is_flagged_after_the_fifth_business_day(fake: FakeProvider) -> None:
    make_world(fake)
    # Tuesday 2026-10-06: five business days after Tuesday 2026-09-29, six after 09-28.
    payment = pending_payment(fake, date(2026, 9, 29))
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert run.counts["e"] == {"checked": 1, "matched": 1}
    Payment.objects.filter(pk=payment.pk).update(initiated_on=date(2026, 9, 28))
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(minutes=5))
    assert run.counts["e"] == {"checked": 1, "mismatched": 1}
    [found] = ReconciliationMismatch.objects.all()
    assert (found.kind, found.entity_id) == (MismatchKind.PENDING_TOO_OLD, payment.pk)
    assert "6 business days" in found.detail


# --- D8: alerts carry counts only ---------------------------------------------------------------


def test_the_mismatch_event_is_fatal_with_counts_by_kind_only_and_once_per_finding(
    fake: FakeProvider, sentry_events: list[tuple[tuple[Any, ...], dict[str, Any]]]
) -> None:
    make_world(fake)
    payment = pending_payment(fake, date(2026, 9, 1))
    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    [(args, kwargs)] = sentry_events
    assert args == (alerts.MISMATCH_MESSAGE,)
    assert kwargs == {
        "level": "fatal",
        "fingerprint": ["reconciliation-mismatch"],
        "extras": {"new_mismatches_by_kind": {"pending_too_old": 1}},
    }
    shown = repr((args, kwargs))
    for value in (str(payment.pk), TXN, mask_identifier(TXN), str(payment.amount), INVOICE):
        assert value not in shown
    # Seen again by the next run: no new mismatch, no event.
    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW + timedelta(hours=1))
    assert len(sentry_events) == 1


def synced_world(fake: FakeProvider) -> Any:
    """The webhook handlers' world, with the local invoice status already the provider's."""
    world = make_world(fake)
    invoice_sync.record_provider_status(world.invoice.pk, fake.invoices[INVOICE].raw_status)
    return world


def test_a_clean_run_sends_no_event(
    fake: FakeProvider, sentry_events: list[tuple[tuple[Any, ...], dict[str, Any]]]
) -> None:
    synced_world(fake)
    run = run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    assert run.status == ReconciliationStatus.DONE
    assert sentry_events == []


# --- The commands ------------------------------------------------------------------------------


def test_reconcile_refuses_unless_it_is_the_caller(fake: FakeProvider) -> None:
    for caller in ("none", ""):
        with override_settings(STAX_CALLER=caller), pytest.raises(CommandError, match="worker"):
            call_command("reconcile")
    assert ReconciliationRun.objects.count() == 0
    assert fake.calls == []


def test_reconcile_prints_counts_only_and_fails_loudly(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced_world(fake)
    out = io.StringIO()
    with override_settings(STAX_CALLER="worker"):
        call_command("reconcile", stdout=out)
    text = out.getvalue()
    assert text.startswith("Status: done.")
    assert "Step a: checked=1, matched=1." in text
    assert INVOICE not in text and "Northwind" not in text
    fake.fail_next("list_transactions", ProviderUnavailable(503))
    with override_settings(STAX_CALLER="worker"), pytest.raises(CommandError, match="step b"):
        call_command("reconcile", stdout=io.StringIO())


def test_reconcile_scheduled_outside_the_window_records_a_skip(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    noon = datetime(2026, 7, 15, 16, 0, tzinfo=UTC)
    monkeypatch.setattr("payments.reconciliation.schedule.timezone.now", lambda: noon)
    out = io.StringIO()
    with override_settings(STAX_CALLER="none", **NEW_YORK):
        call_command("reconcile", "--scheduled", stdout=out)
    assert "Not due" in out.getvalue()
    [run] = ReconciliationRun.objects.all()
    assert (run.trigger, run.status) == ("scheduled", "skipped")
    assert Job.objects.count() == 0
    assert fake.calls == []


@pytest.mark.parametrize("caller", ["none", "", "worker"])
def test_reconcile_scheduled_when_due_queues_one_job_under_any_caller(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    out = io.StringIO()
    with override_settings(STAX_CALLER=caller, **NEW_YORK):
        call_command("reconcile", "--scheduled", stdout=out)
    assert out.getvalue().strip() == QUEUED
    [job] = Job.objects.all()
    assert (job.kind, job.entity_type, job.entity_id) == (
        JobKind.RECONCILE_RUN,
        "reconciliation",
        jobs.RECONCILIATION.pk,
    )
    assert (job.status, job.params, job.requested_by) == ("queued", {"trigger": "scheduled"}, None)
    assert ReconciliationRun.objects.count() == 0
    assert fake.calls == []


def test_the_scheduled_path_of_the_command_names_nothing_that_reaches_the_provider() -> None:
    path = REPO / "backend/payments/management/commands/reconcile.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    [queue] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_queue_if_due"
    ]
    reaching = provider_reaching_functions() | {"get_provider", "run_reconciliation"}
    named = {
        node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        for node in ast.walk(queue)
    }
    assert named & reaching == set()


def test_age_pending_payment_moves_the_date_back_and_audits(fake: FakeProvider) -> None:
    make_world(fake)
    payment = pending_payment(fake, date(2026, 10, 1))
    out = io.StringIO()
    call_command("age_pending_payment", "--payment", str(payment.pk), "--days", "10", stdout=out)
    assert out.getvalue().strip() == "2026-09-21"
    payment.refresh_from_db()
    assert payment.initiated_on == date(2026, 9, 21)
    entry = AuditLog.objects.get(action="payment.aged_for_test")
    assert entry.before == {"initiated_on": "2026-10-01"}


def test_age_pending_payment_refuses_production_and_anything_but_pending(
    fake: FakeProvider,
) -> None:
    make_world(fake)
    payment = pending_payment(fake, date(2026, 10, 1))
    with override_settings(APP_ENV="production"), pytest.raises(CommandError, match="production"):
        call_command("age_pending_payment", "--payment", str(payment.pk), "--days", "3")
    for days in ("0", "-1", "3651"):
        with pytest.raises(CommandError, match="--days"):
            call_command("age_pending_payment", "--payment", str(payment.pk), "--days", days)
    with pytest.raises(CommandError, match="payment ID"):
        call_command("age_pending_payment", "--payment", "not-an-id", "--days", "3")
    with pytest.raises(CommandError, match="No payment"):
        call_command("age_pending_payment", "--payment", str(uuid.uuid4()), "--days", "3")
    settle(fake)
    assert deliver("update_transaction_settled", id=TXN) == "processed"
    with pytest.raises(CommandError, match="Only a pending payment.*settled.*Nothing was changed"):
        call_command("age_pending_payment", "--payment", str(payment.pk), "--days", "3")
    payment.refresh_from_db()
    assert payment.initiated_on == date(2026, 10, 1)
    assert not AuditLog.objects.filter(action="payment.aged_for_test").exists()


# --- D10: the page --------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.VIEWER, None])
def test_below_billing_cannot_see_the_page_or_act(fake: FakeProvider, role: Role | None) -> None:
    client, _ = client_for(role)
    assert client.get("/reconciliation/").status_code == 403
    assert client.post("/reconciliation/run/").status_code == 403
    assert Job.objects.count() == 0


def test_the_page_shows_counts_and_masked_identifiers_only(fake: FakeProvider) -> None:
    make_world(fake)
    payment = pending_payment(fake, date(2026, 9, 1))
    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    billing, _ = client_for(Role.BILLING)
    page = billing.get("/reconciliation/").content.decode()
    assert "Pending too long" in page
    assert mask_identifier(str(payment.pk)) in page
    assert str(payment.pk) not in page
    assert TXN not in page and INVOICE not in page
    # 06:30 UTC is 02:30 in New York (EDT).
    assert "2026-10-06 02:30 EDT" in page
    assert 'id="run-now"' not in page  # Run now is for admins
    uuids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", page)
    mismatch_ids = {str(pk) for pk in ReconciliationMismatch.objects.values_list("pk", flat=True)}
    assert set(uuids) <= mismatch_ids  # only the form targets, never a record's ID


def test_billing_resolves_with_a_note_and_the_resolution_is_audited(fake: FakeProvider) -> None:
    make_world(fake)
    pending_payment(fake, date(2026, 9, 1))
    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    found = ReconciliationMismatch.objects.get()
    billing, user = client_for(Role.BILLING)
    url = f"/reconciliation/mismatches/{found.pk}/resolve/"
    refused = billing.post(url, {"note": "  "}, follow=True)
    assert "A note is required" in refused.content.decode()
    card = billing.post(url, {"note": "Card 4111111111111111"}, follow=True)
    assert card.status_code == 200
    found.refresh_from_db()
    assert found.resolved_at is None
    done = billing.post(url, {"note": "Checked with the bank."}, follow=True)
    assert mismatches.RESOLVED in done.content.decode()
    found.refresh_from_db()
    assert (found.resolved_by, found.resolution_note) == (user, "Checked with the bank.")
    assert AuditLog.objects.filter(
        action="reconciliation_mismatch.resolved", entity_id=str(found.pk), actor=user
    ).exists()
    again = billing.post(url, {"note": "Twice."}, follow=True)
    assert mismatches.ALREADY_RESOLVED in again.content.decode()
    assert billing.post("/reconciliation/mismatches/not-an-id/resolve/").status_code == 404


#: Notes with nothing visible in them: invisible format characters (the card guard's own
#: list), Unicode whitespace, and the no-break space, alone or mixed.
INVISIBLE_NOTES = (
    "\u200b",
    "\u200c\u200d",
    "\u2060\ufeff",
    "\u00a0",
    " \u200b\u00a0\t\n\u3000",
)


@pytest.mark.parametrize("note", INVISIBLE_NOTES)
def test_the_resolve_form_refuses_a_note_with_nothing_visible(note: str) -> None:
    form = ResolveMismatchForm({"note": note})
    assert not form.is_valid()
    assert form.errors["note"] == [mismatches.NOTE_REQUIRED]


def test_the_resolve_form_keeps_the_note_without_invisible_characters() -> None:
    form = ResolveMismatchForm({"note": "\u00a0\u200bChecked\u2060 with the bank.\ufeff "})
    assert form.is_valid()
    assert form.cleaned_data["note"] == "Checked with the bank."


def test_the_resolve_service_refuses_a_note_with_nothing_visible(fake: FakeProvider) -> None:
    make_world(fake)
    pending_payment(fake, date(2026, 9, 1))
    run_reconciliation(ReconciliationTrigger.MANUAL, now=NOW)
    found = ReconciliationMismatch.objects.get()
    _, user = client_for(Role.BILLING)
    for note in INVISIBLE_NOTES:
        outcome = mismatches.resolve(found.pk, note=note, actor=user, now=NOW)
        assert outcome == mismatches.ResolveOutcome(False, mismatches.NOTE_REQUIRED), repr(note)
    found.refresh_from_db()
    assert (found.resolved_at, found.resolution_note) == (None, "")
    assert not AuditLog.objects.filter(action="reconciliation_mismatch.resolved").exists()

    done = mismatches.resolve(found.pk, note="\u200b Checked\u00a0\ufeff", actor=user, now=NOW)
    assert done.ok
    found.refresh_from_db()
    assert found.resolution_note == "Checked"
    [audit] = AuditLog.objects.filter(action="reconciliation_mismatch.resolved")
    assert audit.after["resolution_note"] == "Checked"


def test_run_now_is_admin_only_queued_once_and_run_by_the_worker(fake: FakeProvider) -> None:
    billing, _ = client_for(Role.BILLING)
    assert billing.post("/reconciliation/run/").status_code == 403
    admin, user = client_for(Role.ADMIN)
    assert 'id="run-now"' in admin.get("/reconciliation/").content.decode()
    admin.post("/reconciliation/run/")
    second = admin.post("/reconciliation/run/", follow=True)
    assert "already waiting" in second.content.decode()
    [job] = Job.objects.all()
    assert (job.kind, job.entity_type, job.entity_id) == (
        JobKind.RECONCILE_RUN,
        "reconciliation",
        jobs.RECONCILIATION.pk,
    )
    page = admin.get("/reconciliation/").content.decode()
    assert 'http-equiv="refresh"' in page and "Queued" in page
    assert fake.calls == []
    with override_settings(STAX_CALLER="worker"):
        [done] = run_queued_jobs()
    assert done.status == JobStatus.DONE
    assert done.result.startswith("Reconciliation done:")
    [run] = ReconciliationRun.objects.all()
    assert (run.trigger, run.requested_by) == (ReconciliationTrigger.MANUAL, user)
    page = admin.get("/reconciliation/").content.decode()
    assert 'http-equiv="refresh"' not in page
    assert 'id="last-status">Done' in page


def test_the_nav_links_the_page(fake: FakeProvider) -> None:
    billing, _ = client_for(Role.BILLING)
    page = billing.get("/webhooks/").content.decode()
    assert 'href="/reconciliation/">Reconciliation</a>' in page


# --- D12: configuration ------------------------------------------------------------------------

LOCAL_ENV = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}


def test_the_reconciliation_settings_default_as_the_plan_says() -> None:
    config = validate(LOCAL_ENV)
    assert (
        config.pending_payment_max_business_days,
        config.reconcile_lookback_days,
        config.reconcile_overlap_hours,
        config.reconcile_deposit_days,
        config.reconcile_local_time,
        config.reconcile_timezone,
    ) == (5, 35, 48, 14, time(2, 0), "America/New_York")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PENDING_PAYMENT_MAX_BUSINESS_DAYS", "99999"),
        ("PENDING_PAYMENT_MAX_BUSINESS_DAYS", "five"),
        ("RECONCILE_LOOKBACK_DAYS", "365"),
        ("RECONCILE_OVERLAP_HOURS", "-1"),
        ("RECONCILE_DEPOSIT_DAYS", "1.5"),
        ("RECONCILE_LOCAL_TIME", "2:00"),
        ("RECONCILE_LOCAL_TIME", "24:00"),
        ("RECONCILE_TIMEZONE", "Mars/Olympus"),
        ("RECONCILE_TIMEZONE", "../../etc/passwd"),
    ],
)
def test_an_invalid_reconciliation_setting_aborts_without_echoing_it(name: str, value: str) -> None:
    with pytest.raises(EnvGuardError) as raised:
        validate({**LOCAL_ENV, name: value})
    assert name in str(raised.value)
    assert value not in str(raised.value)


def test_valid_reconciliation_settings_are_read() -> None:
    config = validate(
        {
            **LOCAL_ENV,
            "PENDING_PAYMENT_MAX_BUSINESS_DAYS": "7",
            "RECONCILE_LOCAL_TIME": "23:30",
            "RECONCILE_TIMEZONE": "America/Chicago",
        }
    )
    assert config.pending_payment_max_business_days == 7
    assert config.reconcile_local_time == time(23, 30)
    assert config.reconcile_timezone == "America/Chicago"


def test_the_env_example_documents_the_names_with_empty_values() -> None:
    example = (REPO / ".env.example").read_text().splitlines()
    for name in (
        "PENDING_PAYMENT_MAX_BUSINESS_DAYS",
        "RECONCILE_LOOKBACK_DAYS",
        "RECONCILE_OVERLAP_HOURS",
        "RECONCILE_DEPOSIT_DAYS",
        "RECONCILE_LOCAL_TIME",
        "RECONCILE_TIMEZONE",
    ):
        assert f"{name}=" in example


def test_a_window_that_crosses_midnight_runs_once(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    late = {"RECONCILE_TIMEZONE": "UTC", "RECONCILE_LOCAL_TIME": time(23, 30)}
    with override_settings(**late):
        assert should_run_scheduled(datetime(2026, 7, 15, 23, 0, tzinfo=UTC))[0] is False
        assert fire(monkeypatch, datetime(2026, 7, 15, 23, 45, tzinfo=UTC)) == QUEUED
        assert [run.status for run in started_runs()] == ["done"]
        assert fire(monkeypatch, datetime(2026, 7, 16, 0, 45, tzinfo=UTC)).startswith(
            "Status: skipped."
        )
        assert should_run_scheduled(datetime(2026, 7, 16, 1, 30, tzinfo=UTC))[0] is False
    assert len(started_runs()) == 1


# --- Infrastructure -----------------------------------------------------------------------------


def test_the_scheduled_job_queues_reconcile_at_both_utc_hours_as_no_caller() -> None:
    module = (REPO / "infra/modules/containerAppJobReconcile.bicep").read_text()
    assert "resource job 'Microsoft.App/jobs@2024-03-01'" in module
    assert "triggerType: 'Schedule'" in module
    assert "param cronExpression string = '0 6,7 * * *'" in module
    assert "param replicaTimeout int = 300\n" in module
    for line in ("replicaRetryLimit: 0", "parallelism: 1", "replicaCompletionCount: 1"):
        assert line in module
    # D5 as amended: the job only queues the run; it never calls the provider.
    assert "'exec python backend/manage.py reconcile --scheduled'" in module
    assert "provider_environment_check" not in module
    assert "name: 'STAX_CALLER'\n    value: 'none'\n" in module
    assert "value: 'worker'" not in module
    worker = (REPO / "infra/modules/containerAppWorker.bicep").read_text()
    # The mail settings (PLAN-011b D9) are the worker's, the web app's and the daily
    # job's; the reconciliation sends no e-mail.
    mail_secrets = {"gmail-service-account-json"}
    mail_env = {"MAIL_ENABLED", "GMAIL_SENDER", "MAIL_CATCH_ALL", "COMPANY_NAME"}
    mail_env |= {"GMAIL_SERVICE_ACCOUNT_JSON"}
    for secret in re.findall(r"name: '([a-z-]+)'\n    keyVaultUrl", worker):
        if secret not in mail_secrets:
            assert f"name: '{secret}'\n    keyVaultUrl" in module
    for name in re.findall(r"name: '([A-Z_]+)'\n", worker):
        if name not in mail_env:
            assert f"name: '{name}'\n" in module
    for name in (*mail_secrets, *mail_env):
        assert name not in module


def test_main_wires_the_job_with_the_web_image_and_names_it() -> None:
    main = (REPO / "infra/main.bicep").read_text()
    head = "module reconcileJob 'modules/containerAppJobReconcile.bicep'"
    assert head in main
    block = main.split(head)[1].split("\n}\n")[0]
    assert "image: webImage" in block and "identityResourceId: identity.outputs" in block
    assert "param reconcileJobName string = '${resourceNamePrefix}-reconcile'" in main
    assert "output reconcileJobName string = reconcileJob.outputs.jobName" in main


def test_the_worker_gets_a_sixty_second_grace_period() -> None:
    worker = (REPO / "infra/modules/containerAppWorker.bicep").read_text()
    assert "      terminationGracePeriodSeconds: 60\n" in worker


def test_the_dockerfile_names_the_worker_as_the_caller() -> None:
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "Step 10" not in dockerfile
    assert "STAX_CALLER=none" in dockerfile
    assert "worker container app" in dockerfile


@pytest.fixture(autouse=True)
def _new_york() -> Iterator[None]:
    with override_settings(**NEW_YORK):
        yield
