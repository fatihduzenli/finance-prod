"""The database job queue, the worker, the enqueueing screens, the worker app and the
deployment check (docs/qa/PLAN-010-worker-and-reconciliation.md Batch A: D1 to D4, D13;
§5 test plan).

Nothing here runs the worker between requests (contrast ``tests.job_helpers``): the
queued and running states are what is checked. No test reaches the network.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import signal
import stat
import subprocess
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from accounts.roles import Role
from ledger.models import AuditLog, Invoice, Lease, Operator, RentCharge, SyncStatus
from ledger.services import leases, refunds, rent_run
from ledger.views.jobs import BUSY, REPLAY_REFUSED
from ledger.views.leases import REFRESH_QUEUED
from ledger.views.run import MATCH_QUEUED
from payments import get_provider as real_get_provider
from payments import jobs, operator_sync
from payments.errors import ProviderRejected
from payments.fake import FakeProvider
from payments.health import BREAKER_OPEN
from payments.jobs import runners
from payments.management.commands import worker
from payments.models import (
    ExternalRef,
    Job,
    JobKind,
    JobStatus,
    ProviderSnapshot,
    WebhookEvent,
    WebhookOutcome,
)
from payments.provider import mask_identifier
from payments.stax.breaker import Outcome
from payments.webhooks import receiver
from tests.job_helpers import run_queued_jobs
from tests.test_lease_services import make_lease, method
from tests.test_lease_services import make_operator as lease_operator
from tests.test_manual_payment_marking import invoiced
from tests.test_operator_sync import add_provider_customer, fields, requires_postgresql
from tests.test_operator_views import client_for, form_data
from tests.test_refunds_voids import ach
from tests.test_rent_run import (
    enrolled,
    health,  # noqa: F401 (autouse fixture)
    invoice,
)
from tests.test_webhook_handlers import make_world

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
CARD = "4111111111111111"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


def refuse_provider(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``payments.get_provider`` with one that records and fails every use."""
    used: list[str] = []

    def refused() -> Any:
        used.append("get_provider")
        raise AssertionError("a web view reached the payment provider")

    monkeypatch.setattr("payments.get_provider", refused)
    return used


def new_operator(**overrides: Any) -> Operator:
    values = {"email": f"jobs-{uuid.uuid4().hex[:8]}@example.com", **overrides}
    operator, _ = operator_sync.create_operator(
        fields(**values), creation_key=uuid.uuid4(), actor=None
    )
    return operator


def detail(operator: Operator) -> str:
    return f"/operators/{operator.pk}/"


def only_job() -> Job:
    [job] = Job.objects.all()
    return job


# --- D1: enqueue ------------------------------------------------------------------------


def test_enqueue_stores_a_queued_job_with_identifiers_and_amounts_as_text() -> None:
    operator = new_operator()
    key = uuid.uuid4()
    job = jobs.enqueue(
        JobKind.OPERATOR_LINK,
        operator,
        {"amount": Decimal("12.50"), "key": key, "flag": True, "count": 2},
        None,
    )
    job.refresh_from_db()
    assert (job.kind, job.entity_type, job.entity_id) == ("operator_link", "operator", operator.pk)
    assert job.status == JobStatus.QUEUED
    assert (job.attempts, job.max_attempts) == (0, 1)
    assert job.params == {"amount": "12.50", "key": str(key), "flag": True, "count": 2}
    assert job.requested_by is None and job.replay_key is None


@pytest.mark.parametrize("value", [object(), [1], Decimal("NaN"), b"bytes"], ids=repr)
def test_enqueue_refuses_any_parameter_but_text_numbers_and_identifiers(value: object) -> None:
    with pytest.raises(TypeError):
        jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), {"value": value}, None)
    assert not Job.objects.exists()


def test_enqueue_refuses_a_kind_for_another_entity_and_any_other_model() -> None:
    operator = new_operator()
    with pytest.raises(ValueError, match="does not act"):
        jobs.enqueue(JobKind.AUTOPAY_PAUSE, operator, None, None)
    with pytest.raises(ValueError):
        jobs.enqueue("webhook_event", operator, None, None)
    lease = make_lease()
    with pytest.raises(ValueError, match="operators, leases"):
        jobs.enqueue(JobKind.OPERATOR_SYNC, lease.store, None, None)
    assert not Job.objects.exists()


def test_a_card_number_in_the_parameters_is_refused() -> None:
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        jobs.enqueue(JobKind.OPERATOR_LINK, new_operator(), {"customer_id": CARD}, None)
    assert not Job.objects.exists()


def test_a_replay_key_returns_the_first_job() -> None:
    operator = new_operator()
    key = uuid.uuid4()
    first = jobs.enqueue(JobKind.OPERATOR_SYNC, operator, None, None, key)
    second = jobs.enqueue(JobKind.OPERATOR_SYNC, operator, {"other": "x"}, None, key)
    assert first.pk == second.pk
    assert Job.objects.count() == 1
    assert second.params == {}


def test_a_replay_key_of_another_entity_or_kind_is_refused() -> None:
    first, second = new_operator(), new_operator(email="other@example.com")
    key = uuid.uuid4()
    jobs.enqueue(JobKind.OPERATOR_SYNC, first, None, None, key)
    with pytest.raises(jobs.ReplayKeyUsed):
        jobs.enqueue(JobKind.OPERATOR_SYNC, second, None, None, key)
    with pytest.raises(jobs.ReplayKeyUsed):
        jobs.enqueue(JobKind.OPERATOR_RETRY, first, None, None, key)
    assert Job.objects.count() == 1


# --- D1: claim, run, lost workers ---------------------------------------------------------


def test_claim_takes_the_oldest_queued_job_and_marks_it_running() -> None:
    operator = new_operator()
    older = jobs.enqueue(JobKind.OPERATOR_SYNC, operator, None, None)
    newer = jobs.enqueue(JobKind.OPERATOR_RETRY, operator, None, None)
    Job.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(minutes=1))
    now = timezone.now()

    claimed = jobs.claim_next(now=now)
    assert claimed is not None and claimed.pk == older.pk
    assert (claimed.status, claimed.attempts, claimed.started_at) == (JobStatus.RUNNING, 1, now)
    assert claimed.locked_until == now + timedelta(seconds=jobs.LOCK_SECONDS)
    second = jobs.claim_next(now=now)
    assert second is not None and second.pk == newer.pk
    assert jobs.claim_next(now=now) is None


def test_a_job_whose_worker_died_is_queued_again_once_then_dead() -> None:
    jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)
    start = timezone.now()
    first = jobs.claim_next(now=start)
    assert first is not None
    # Still within its lock: not taken again.
    assert jobs.claim_next(now=start + timedelta(seconds=jobs.LOCK_SECONDS - 1)) is None

    after_lock = start + timedelta(seconds=jobs.LOCK_SECONDS + 1)
    again = jobs.claim_next(now=after_lock)
    assert again is not None and again.pk == first.pk
    assert (again.status, again.attempts) == (JobStatus.RUNNING, 2)

    assert jobs.claim_next(now=after_lock + timedelta(seconds=jobs.LOCK_SECONDS + 1)) is None
    dead = only_job()
    assert (dead.status, dead.result) == (JobStatus.DEAD, jobs.WORKER_LOST)
    assert dead.finished_at is not None
    assert jobs.describe(dead) == jobs.DEAD_TEXT


def test_run_records_done_with_the_service_sentence(fake: FakeProvider) -> None:
    operator = new_operator()
    jobs.enqueue(JobKind.OPERATOR_SYNC, operator, None, None)
    [job, refresh] = run_queued_jobs()
    assert job.status == JobStatus.DONE
    assert job.result == "Created the customer at the payment provider."
    # The successful sync queued the methods read the enrolment page shows.
    assert (refresh.kind, refresh.status) == (JobKind.OPERATOR_METHODS_REFRESH, JobStatus.DONE)
    assert job.finished_at is not None and job.locked_until is None
    assert jobs.describe(job) == f"Done: {job.result}"
    operator.refresh_from_db()
    assert operator.sync_status == SyncStatus.LINKED


def test_run_records_a_refusal_as_failed_with_the_class_and_fields(fake: FakeProvider) -> None:
    operator = new_operator()
    fake.fail_next("find_customers", ProviderRejected(422, "CANARY text", error_fields=("email",)))
    jobs.enqueue(JobKind.OPERATOR_SYNC, operator, None, None)
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert "ProviderRejected" in job.result and "email" in job.result
    assert "CANARY" not in job.result
    assert jobs.describe(job).startswith("Failed: ")


def test_an_exception_in_a_job_is_recorded_as_its_class_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(job: Job) -> tuple[bool, str]:
        raise RuntimeError(f"detail {CARD}")

    monkeypatch.setitem(
        __import__("payments.jobs.runners", fromlist=["RUNNERS"]).RUNNERS,
        JobKind.OPERATOR_SYNC,
        broken,
    )
    jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)
    with caplog.at_level(logging.WARNING):
        [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, "RuntimeError")
    assert CARD not in caplog.text


def test_an_outcome_holding_a_card_number_is_not_stored() -> None:
    jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)
    job = jobs.claim_next()
    assert job is not None
    jobs.finish(job, JobStatus.DONE, f"text {CARD}")
    job.refresh_from_db()
    assert job.result == jobs.RESULT_NOT_STORED


def test_latest_for_is_the_newest_job_of_that_entity_only() -> None:
    first, second = new_operator(), new_operator(email="other@example.com")
    assert jobs.latest_for(first) is None
    old = jobs.enqueue(JobKind.OPERATOR_SYNC, first, None, None)
    Job.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=1))
    new = jobs.enqueue(JobKind.OPERATOR_RETRY, first, None, None)
    jobs.enqueue(JobKind.OPERATOR_SYNC, second, None, None)
    latest = jobs.latest_for(first)
    assert latest is not None and latest.pk == new.pk


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_workers_claiming_at_once_take_one_job_once() -> None:
    jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)
    barrier = threading.Barrier(2)
    taken: list[Job | None] = []

    def claim() -> None:
        try:
            barrier.wait()
            taken.append(jobs.claim_next())
        finally:
            connection.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([job for job in taken if job is not None]) == 1
    assert only_job().attempts == 1


@requires_postgresql
def test_the_database_refuses_a_card_number_in_params_written_around_the_model() -> None:
    from django.db import DataError, IntegrityError, InternalError

    job = jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)
    with pytest.raises((DataError, IntegrityError, InternalError)):
        Job.objects.filter(pk=job.pk).update(params={"note": CARD})


# --- D2: the worker command --------------------------------------------------------------


@pytest.fixture
def as_worker() -> Any:
    with override_settings(STAX_CALLER="worker"):
        yield


def test_the_worker_refuses_to_start_unless_it_is_the_caller() -> None:
    jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)
    for caller in ("none", ""):
        with override_settings(STAX_CALLER=caller), pytest.raises(CommandError, match="worker"):
            call_command("worker", "--once")
    assert only_job().status == JobStatus.QUEUED


def _stored_event() -> WebhookEvent:
    body = f'{{"id": "inv-0001", "delivery": "{uuid.uuid4()}"}}'.encode()
    event_id = receiver.store_event(body, "update_invoice")
    assert event_id is not None
    return WebhookEvent.objects.get(pk=event_id)


@pytest.mark.usefixtures("as_worker")
def test_each_tick_takes_a_job_before_an_event(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        __import__("payments.webhooks.handlers", fromlist=["HANDLERS"]).HANDLERS,
        "update_invoice",
        lambda event_type, object_id: WebhookOutcome.PROCESSED,
    )
    event = _stored_event()
    jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)

    call_command("worker", "--once")
    assert Job.objects.get(kind=JobKind.OPERATOR_SYNC).status == JobStatus.DONE
    event.refresh_from_db()
    assert event.processed_at is None

    # The sync queued a methods read; it is a job, so it too comes before the event.
    call_command("worker", "--once")
    assert Job.objects.get(kind=JobKind.OPERATOR_METHODS_REFRESH).status == JobStatus.DONE
    event.refresh_from_db()
    assert event.processed_at is None

    call_command("worker", "--once")
    event.refresh_from_db()
    assert event.outcome == WebhookOutcome.PROCESSED
    assert worker.tick() is False


def test_the_worker_skips_dead_letters_and_failed_events() -> None:
    dead, failed = _stored_event(), _stored_event()
    WebhookEvent.objects.filter(pk=dead.pk).update(outcome=WebhookOutcome.DEAD_LETTER, attempts=3)
    WebhookEvent.objects.filter(pk=failed.pk).update(outcome=WebhookOutcome.FAILED, attempts=1)
    assert worker.next_event_id() is None


@pytest.mark.usefixtures("as_worker")
def test_the_worker_logs_one_line_per_item_with_the_masked_entity(
    fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    operator = new_operator()
    jobs.enqueue(JobKind.OPERATOR_SYNC, operator, None, None)
    with caplog.at_level(logging.INFO, logger="payments.management.commands.worker"):
        call_command("worker", "--once")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Worker item")]
    assert lines == [
        f"Worker item: kind=operator_sync entity={mask_identifier(str(operator.pk))} outcome=done"
    ]
    assert str(operator.pk) not in caplog.text


@pytest.mark.usefixtures("as_worker")
def test_sigterm_finishes_the_current_item_and_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks: list[int] = []
    before = signal.getsignal(signal.SIGTERM)

    def one_item() -> bool:
        ticks.append(1)
        os.kill(os.getpid(), signal.SIGTERM)  # arrives while the item is being processed
        return True

    monkeypatch.setattr(worker, "tick", one_item)
    call_command("worker")  # returns: exit status 0
    assert ticks == [1]
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.usefixtures("as_worker")
def test_a_broken_tick_is_logged_by_class_and_the_loop_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ticks: list[int] = []

    def flaky() -> bool:
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError(f"detail {CARD}")
        os.kill(os.getpid(), signal.SIGTERM)
        return True

    monkeypatch.setattr(worker, "tick", flaky)
    monkeypatch.setattr(worker, "IDLE_SECONDS", 0)
    with caplog.at_level(logging.ERROR):
        call_command("worker")
    assert len(ticks) == 2
    assert "Worker tick failed: RuntimeError" in caplog.text
    assert CARD not in caplog.text


def test_the_worker_runs_jobs_with_the_real_adapter_only_as_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4: under STAX_CALLER=none the adapter refuses; nothing reaches the network."""

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no request may leave in tests")

    monkeypatch.setattr("httpx.Client.send", no_network)
    monkeypatch.setattr("payments._providers", {})
    jobs.enqueue(JobKind.OPERATOR_SYNC, new_operator(), None, None)
    with override_settings(STAX_CALLER="none", STAX_PROVIDER="stax"):
        [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert "ProviderNotPermittedHere" in job.result


# --- D3: the screens queue and show; they never call ----------------------------------------

VIEW_DIRECTORY = BACKEND / "ledger" / "views"
RECEIVER = BACKEND / "payments" / "webhooks" / "receiver.py"


#: Modules whose public functions a view could call to reach the provider indirectly.
SERVICE_MODULES = (
    *sorted((BACKEND / "ledger" / "services").glob("*.py")),
    BACKEND / "payments" / "operator_sync.py",
    BACKEND / "payments" / "invoice_sync.py",
    BACKEND / "payments" / "offline_marking.py",
    *sorted((BACKEND / "payments" / "webhooks").rglob("*.py")),
    # PLAN-010 Batch B: the reconciliation reaches the provider; only the worker runs it.
    *sorted((BACKEND / "payments" / "reconciliation").glob("*.py")),
)
#: Reaches the provider through the handler table, which the scan below does not follow.
DISPATCHERS = frozenset({"process_event"})
#: Functions that make no provider call when this keyword is ``True`` (PLAN-010 D3).
DEFERRING = {"record_manual_payment": "defer_mark"}


def _deferred_calls(tree: ast.AST) -> set[int]:
    """``id`` of each function node of a call that passes its deferring keyword as True."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        keyword = DEFERRING.get(name or "")
        if keyword and any(
            item.arg == keyword
            and isinstance(item.value, ast.Constant)
            and item.value.value is True
            for item in node.keywords
        ):
            allowed.add(id(func))
    return allowed


def _reaches(function: ast.FunctionDef, reaching: set[str]) -> bool:
    arguments = function.args.args + function.args.kwonlyargs
    if any(argument.arg == "provider" for argument in arguments):
        return True
    for node in ast.walk(function):
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if name == "get_provider" or name in reaching:
            return True
    return False


def provider_reaching_functions(paths: tuple[Path, ...] = SERVICE_MODULES) -> frozenset[str]:
    """Public functions of ``paths`` that call ``get_provider``, take a ``provider``, or
    call such a function of the same module (to a fixed point)."""
    found: set[str] = set(DISPATCHERS)
    for path in paths:
        functions = [
            node
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef)
        ]
        reaching: set[str] = set()
        while True:
            more = {f.name for f in functions if f.name not in reaching and _reaches(f, reaching)}
            if not more:
                break
            reaching |= more
        found |= {name for name in reaching if not name.startswith("_")}
    return frozenset(found)


def _no_provider_reach(path: Path, reaching: frozenset[str] = frozenset()) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deferred = _deferred_calls(tree)
    for node in ast.walk(tree):
        line = f"{path.name}:{getattr(node, 'lineno', 0)}"
        named = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if named in reaching and id(node) not in deferred:
            offenders.append(f"{line} names {named}, which reaches the provider")
        if isinstance(node, ast.ImportFrom):
            offenders += [
                f"{line} imports {alias.name}, which reaches the provider"
                for alias in node.names
                if alias.name in reaching
            ]
        if isinstance(node, ast.ImportFrom) and node.module == "payments":
            if any(alias.name == "get_provider" for alias in node.names):
                offenders.append(f"{line} imports get_provider")
        elif isinstance(node, ast.Import):
            if any(alias.name == "payments" for alias in node.names):
                offenders.append(f"{line} imports the payments package (get_provider)")
        elif isinstance(node, ast.Attribute):
            if node.attr == "get_provider":
                offenders.append(f"{line} uses get_provider")
            if isinstance(node.value, ast.Name) and node.value.id == "provider":
                offenders.append(f"{line} uses provider.{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "get_provider":
            offenders.append(f"{line} names get_provider")
    return offenders


def test_no_view_and_not_the_receiver_reaches_the_provider() -> None:
    files = sorted(VIEW_DIRECTORY.glob("*.py")) + [RECEIVER]
    assert VIEW_DIRECTORY / "operators.py" in files and len(files) > 5
    reaching = provider_reaching_functions()
    offenders = [offender for path in files for offender in _no_provider_reach(path, reaching)]
    assert offenders == []


def test_the_service_scan_finds_the_reads_the_pages_used_to_make() -> None:
    """Batch A follow-up: the three views that reached the provider through a service."""
    reaching = provider_reaching_functions()
    assert {
        "refresh_payment_methods",
        "match_schedule_invoices",
        "process_event",
        "enable_autopay",
        "mark_provider_invoice",
        "refund_payment",
        "sync_operator",
    } <= reaching
    assert "payment_method_choices" not in reaching
    assert not {"autopay_blockers", "debit_plan", "leases_due", "create_charges"} & reaching


def test_the_scan_covers_the_reconciliation_view_and_finds_the_run() -> None:
    """PLAN-010 Batch B: the reconciliation page is scanned like every view, and the
    reconciliation's entry point counts as reaching the provider, so the page may only
    queue it."""
    assert VIEW_DIRECTORY / "reconciliation.py" in sorted(VIEW_DIRECTORY.glob("*.py"))
    reaching = provider_reaching_functions()
    assert "run_reconciliation" in reaching
    assert not {"resolve", "record", "should_run_scheduled", "local"} & reaching
    assert _no_provider_reach(VIEW_DIRECTORY / "reconciliation.py", reaching) == []


def test_the_static_check_finds_an_indirect_provider_call(tmp_path: Path) -> None:
    service = tmp_path / "service.py"
    service.write_text(
        "import payments\n"
        "def _read(x):\n    return payments.get_provider().list_payment_methods(x)\n"
        "def methods(x):\n    return _read(x)\n"
        "def local(x):\n    return x\n"
    )
    reaching = provider_reaching_functions((service,))
    assert reaching == DISPATCHERS | {"methods"}
    view = tmp_path / "view.py"
    view.write_text(
        "from ledger.services import leases as service\n"
        "from payments.webhooks.processor import process_event\n"
        "service.methods(1)\nservice.local(1)\nprocess_event(1)\n"
    )
    assert len(_no_provider_reach(view, reaching)) == 3
    marking = tmp_path / "marking.py"
    marking.write_text(
        "rent_run.record_manual_payment(1, defer_mark=True)\n"
        "rent_run.record_manual_payment(1)\n"
        "rent_run.record_manual_payment(1, defer_mark=flag)\n"
    )
    assert len(_no_provider_reach(marking, frozenset({"record_manual_payment"}))) == 2


def test_the_static_check_finds_a_provider_call(tmp_path: Path) -> None:
    sample = tmp_path / "view.py"
    sample.write_text(
        "import payments\nprovider = payments.get_provider()\nprovider.find_customers(1)\n"
        "from payments import get_provider\n"
    )
    assert len(_no_provider_reach(sample)) == 4


Post = tuple[str, Client, str, dict[str, Any], str]


def _operator_and_lease_posts(fake: FakeProvider) -> list[Post]:
    billing, _ = client_for(Role.BILLING)
    admin, _ = client_for(Role.ADMIN)
    linked = new_operator(email="linked@example.com")
    jobs.enqueue(JobKind.OPERATOR_SYNC, linked, None, None)
    run_queued_jobs()
    failed = new_operator(email="failed@example.com")
    Operator.objects.filter(pk=failed.pk).update(
        sync_status=SyncStatus.FAILED,
        sync_attempted_at=timezone.now() - timedelta(minutes=5),
    )
    manual = new_operator(email=None, display_name="Manual")
    lease = make_lease()
    # One lease each: a lease's second request waits while its first is queued (BUSY).
    paused = make_lease(operator=lease_operator(email="paused@example.com", customer="cus-p"))
    resumed = make_lease(operator=lease_operator(email="resumed@example.com", customer="cus-r"))
    return [
        (
            JobKind.OPERATOR_SYNC,
            billing,
            "/operators/new/",
            form_data(email="created@example.com"),
            "",
        ),
        (
            JobKind.OPERATOR_SYNC,
            billing,
            f"{detail(linked)}edit/",
            form_data(email="linked@example.com", phone="+1 415 555 0199"),
            str(linked.pk),
        ),
        (JobKind.OPERATOR_RETRY, billing, f"{detail(failed)}retry-sync/", {}, str(failed.pk)),
        (
            JobKind.OPERATOR_LINK,
            billing,
            f"{detail(manual)}link/",
            {"customer_id": "cus_manual_0001", "payment_method_id": "pm_bank-0001"},
            str(manual.pk),
        ),
        (
            JobKind.AUTOPAY_ENABLE,
            admin,
            f"/leases/{lease.pk}/enrol/",
            {"payment_method_id": "pm-bank-1"},
            str(lease.pk),
        ),
        (JobKind.AUTOPAY_PAUSE, admin, f"/leases/{paused.pk}/pause/", {}, str(paused.pk)),
        (JobKind.AUTOPAY_RESUME, admin, f"/leases/{resumed.pk}/resume/", {}, str(resumed.pk)),
    ]


def _charge_posts(fake: FakeProvider) -> list[Post]:
    billing, _ = client_for(Role.BILLING)
    charge = invoiced(fake)
    return [
        # First: the retry button refuses while any job of the charge is queued.
        (
            JobKind.PAYMENT_RETRY_MARK,
            billing,
            f"/charges/{charge.pk}/retry-mark/",
            {},
            str(charge.pk),
        ),
        (
            JobKind.PAYMENT_MARK,
            billing,
            f"/charges/{charge.pk}/payments/new/",
            {
                "creation_key": str(uuid.uuid4()),
                "method": "wire",
                "amount": str(charge.balance),
                "paid_on": timezone.localdate().isoformat(),
                "reference": "",
                "note": "Wire received.",
            },
            str(charge.pk),
        ),
    ]


def _payment_posts(fake: FakeProvider) -> list[Post]:
    admin, _ = client_for(Role.ADMIN)
    make_world(fake)
    refundable = ach(fake, "1500.00")
    voidable = ach(fake, "200.00", "txn-void-0001", settled=False)
    return [
        (
            JobKind.PAYMENT_REFUND,
            admin,
            f"/payments/{refundable.pk}/refund/",
            {"creation_key": str(uuid.uuid4()), "amount": "100.00", "confirm": "on"},
            str(refundable.pk),
        ),
        (
            JobKind.PAYMENT_VOID,
            admin,
            f"/payments/{voidable.pk}/void/",
            {"creation_key": str(uuid.uuid4()), "confirm": "on"},
            str(voidable.pk),
        ),
    ]


def _read_posts(fake: FakeProvider) -> list[Post]:
    """Batch A follow-up: the three screens that read the provider before."""
    admin, _ = client_for(Role.ADMIN)
    billing, _ = client_for(Role.BILLING)
    lease = make_lease()
    assert lease.operator is not None
    event = _stored_event()
    return [
        (
            JobKind.OPERATOR_METHODS_REFRESH,
            admin,
            f"/leases/{lease.pk}/enrol/",
            {"refresh_methods": "1"},
            str(lease.operator.pk),
        ),
        (JobKind.RUN_MATCH, billing, "/run/match/", {}, str(jobs.RENT_RUN.pk)),
        (JobKind.WEBHOOK_REPLAY, billing, f"/webhooks/{event.pk}/replay/", {}, str(event.pk)),
    ]


@pytest.mark.parametrize(
    "build",
    [_operator_and_lease_posts, _charge_posts, _payment_posts, _read_posts],
    ids=["operators-and-leases", "charges", "payments", "reads"],
)
def test_every_enqueueing_view_queues_its_job_and_never_calls_the_provider(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    posts = build(fake)
    fake.calls.clear()
    used = refuse_provider(monkeypatch)
    before = set(Job.objects.values_list("pk", flat=True))
    for kind, client, path, data, entity in posts:
        response = client.post(path, data)
        assert response.status_code == 302, path
        new = Job.objects.exclude(pk__in=before)
        assert [(job.kind, job.status) for job in new] == [(kind, JobStatus.QUEUED)], path
        if entity:
            assert str(new.get().entity_id) == entity, path
        before |= {job.pk for job in new}
    assert used == []
    assert fake.calls == []


@pytest.mark.usefixtures("as_worker")
def test_the_page_shows_queued_then_running_then_done_and_refreshes_only_meanwhile(
    fake: FakeProvider,
) -> None:
    billing, user = client_for(Role.BILLING)
    response = billing.post("/operators/new/", form_data(), follow=True)
    page = response.content.decode()
    operator = Operator.objects.get()
    assert '<meta http-equiv="refresh" content="5">' in page
    assert 'id="job-state">Queued<' in page
    assert fake.calls == []
    job = only_job()
    assert job.requested_by == user

    assert jobs.claim_next() is not None
    page = billing.get(detail(operator)).content.decode()
    assert 'id="job-state">Running<' in page and 'http-equiv="refresh"' in page

    jobs.run(Job.objects.get())
    page = billing.get(detail(operator)).content.decode()
    assert 'id="job-state">Done: Created the customer at the payment provider.<' in page
    assert 'http-equiv="refresh"' not in page


def test_a_page_without_a_job_shows_no_state_and_does_not_refresh(fake: FakeProvider) -> None:
    billing, _ = client_for(Role.BILLING)
    operator = new_operator(email=None)
    page = billing.get(detail(operator)).content.decode()
    assert 'id="job-status"' not in page and 'http-equiv="refresh"' not in page


def test_the_retry_button_waits_for_a_queued_job_and_a_second_post_queues_nothing(
    fake: FakeProvider,
) -> None:
    billing, _ = client_for(Role.BILLING)
    operator = new_operator()
    Operator.objects.filter(pk=operator.pk).update(
        sync_status=SyncStatus.FAILED,
        sync_failure="ProviderUnavailable",
        sync_attempted_at=timezone.now() - timedelta(minutes=5),
    )
    assert "Retry sync" in billing.get(detail(operator)).content.decode()
    billing.post(f"{detail(operator)}retry-sync/")
    page = billing.get(detail(operator)).content.decode()
    assert "Retry sync" not in page
    second = billing.post(f"{detail(operator)}retry-sync/", follow=True)
    assert BUSY in second.content.decode()
    assert Job.objects.count() == 1
    assert fake.calls == []


@pytest.mark.parametrize(
    ("kind", "action", "data"),
    [
        (JobKind.AUTOPAY_ENABLE, "enrol", {"payment_method_id": "pm-bank-1"}),
        (JobKind.AUTOPAY_PAUSE, "pause", {}),
        (JobKind.AUTOPAY_RESUME, "resume", {}),
    ],
    ids=["enable", "pause", "resume"],
)
def test_a_double_click_on_a_lease_action_queues_one_job_and_says_busy(
    fake: FakeProvider, kind: str, action: str, data: dict[str, str]
) -> None:
    """Batch A review finding 1: a second enable, pause or resume while the lease's
    latest request is queued or running queues nothing and shows the BUSY warning."""
    admin, _ = client_for(Role.ADMIN)
    lease = make_lease()
    first = admin.post(f"/leases/{lease.pk}/{action}/", data)
    assert first.status_code == 302
    second = admin.post(f"/leases/{lease.pk}/{action}/", data, follow=True)
    assert BUSY in second.content.decode()
    assert [(job.kind, job.status) for job in Job.objects.all()] == [(kind, JobStatus.QUEUED)]
    # Running counts as busy too; once it finished, the next request is queued.
    Job.objects.update(status=JobStatus.RUNNING)
    assert BUSY in admin.post(f"/leases/{lease.pk}/{action}/", data, follow=True).content.decode()
    Job.objects.update(status=JobStatus.DONE)
    admin.post(f"/leases/{lease.pk}/{action}/", data)
    assert Job.objects.count() == 2
    assert fake.calls == []


def test_a_queued_pause_also_blocks_a_resume_of_the_same_lease(fake: FakeProvider) -> None:
    admin, _ = client_for(Role.ADMIN)
    lease = make_lease()
    admin.post(f"/leases/{lease.pk}/pause/")
    response = admin.post(f"/leases/{lease.pk}/resume/", follow=True)
    assert BUSY in response.content.decode()
    assert list(Job.objects.values_list("kind", flat=True)) == [JobKind.AUTOPAY_PAUSE]


def test_a_viewer_cannot_queue_anything(fake: FakeProvider) -> None:
    viewer, _ = client_for(Role.VIEWER)
    operator = new_operator()
    assert viewer.post(f"{detail(operator)}retry-sync/").status_code == 403
    assert viewer.post(f"{detail(operator)}link/", {"customer_id": "c"}).status_code == 403
    assert not Job.objects.exists()


def test_the_page_never_shows_the_identifiers_a_link_job_carries(fake: FakeProvider) -> None:
    billing, _ = client_for(Role.BILLING)
    operator = new_operator(email=None)
    data = {"customer_id": "cus_secretish_0001", "payment_method_id": "pm_secretish_0002"}
    billing.post(f"{detail(operator)}link/", data)
    assert only_job().params == data
    for _ in range(2):
        page = billing.get(detail(operator)).content.decode()
        assert "cus_secretish_0001" not in page and "pm_secretish_0002" not in page
        run_queued_jobs()


def test_a_double_submitted_refund_form_queues_one_job_and_refunds_once(
    fake: FakeProvider,
) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    fake.calls.clear()
    admin, _ = client_for(Role.ADMIN)
    data = {"creation_key": str(uuid.uuid4()), "amount": "100.00", "confirm": "on"}
    admin.post(f"/payments/{payment.pk}/refund/", data)
    again = admin.post(f"/payments/{payment.pk}/refund/", data, follow=True)
    assert refunds.ALREADY_SUBMITTED in again.content.decode()
    assert Job.objects.count() == 1
    run_queued_jobs()
    run_queued_jobs()
    assert [name for name, _ in fake.calls].count("refund_transaction") == 1


def test_an_unconfirmed_or_excessive_refund_is_refused_before_anything_is_queued(
    fake: FakeProvider,
) -> None:
    make_world(fake)
    payment = ach(fake, "1500.00")
    admin, _ = client_for(Role.ADMIN)
    path = f"/payments/{payment.pk}/refund/"
    for data in (
        {"creation_key": str(uuid.uuid4()), "amount": "100.00"},
        {"creation_key": str(uuid.uuid4()), "amount": "1500.01", "confirm": "on"},
    ):
        response = admin.post(path, data)
        assert (response.status_code, response["Location"]) == (302, path)
    assert not Job.objects.exists()


def test_a_replay_key_used_for_another_record_is_refused_with_a_message(
    fake: FakeProvider,
) -> None:
    make_world(fake)
    first = ach(fake, "1500.00")
    second = ach(fake, "300.00", "txn-other-0001")
    admin, _ = client_for(Role.ADMIN)
    key = uuid.uuid4()
    jobs.enqueue(JobKind.PAYMENT_REFUND, first, {"amount": "1.00"}, None, key)
    response = admin.post(
        f"/payments/{second.pk}/void/", {"creation_key": str(key), "confirm": "on"}, follow=True
    )
    # The key is known: the form says so, and nothing is queued for the second payment.
    assert refunds.ALREADY_SUBMITTED in response.content.decode()
    assert Job.objects.count() == 1


def test_submit_refuses_a_replay_key_of_another_record_with_a_message() -> None:
    from django.contrib.auth.models import AnonymousUser
    from django.contrib.messages import get_messages
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    from ledger.views.jobs import submit

    first, second = new_operator(), new_operator(email="other@example.com")
    key = uuid.uuid4()
    jobs.enqueue(JobKind.OPERATOR_SYNC, first, None, None, key)
    request = RequestFactory().post("/")
    request.user = AnonymousUser()
    request.session = SessionStore()
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    assert submit(request, JobKind.OPERATOR_SYNC, second, replay_key=key) is None
    assert [str(message) for message in get_messages(request)] == [REPLAY_REFUSED]
    assert Job.objects.count() == 1


def test_a_resubmitted_manual_payment_queues_one_marking(fake: FakeProvider) -> None:
    charge = invoiced(fake)
    billing, _ = client_for(Role.BILLING)
    data = {
        "creation_key": str(uuid.uuid4()),
        "method": "wire",
        "amount": str(charge.balance),
        "paid_on": timezone.localdate().isoformat(),
        "reference": "",
        "note": "Wire received.",
    }
    path = f"/charges/{charge.pk}/payments/new/"
    billing.post(path, data)
    billing.post(path, data)
    job = only_job()
    assert job.kind == JobKind.PAYMENT_MARK
    assert job.params == {"payment": str(charge.payments.get().pk)}
    assert job.replay_key == uuid.UUID(data["creation_key"])
    assert fake.calls == []
    [done] = run_queued_jobs()
    assert (done.status, done.result) == (JobStatus.DONE, rent_run.MARK_DONE)


# --- Batch A follow-up: the methods list, "Match invoices" and the replay -----------------


def enrol_page(lease: Lease) -> str:
    return f"/leases/{lease.pk}/enrol/"


def test_the_three_pages_read_nothing_from_the_provider_whatever_their_state(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, _ = client_for(Role.ADMIN)
    lease = make_lease()
    assert lease.operator is not None
    event = _stored_event()
    fake.add_payment_method("cus-1", method())
    pages = (enrol_page(lease), "/run/", f"/webhooks/{event.pk}/")
    used = refuse_provider(monkeypatch)
    for path in pages:  # nothing read yet, nothing queued
        assert admin.get(path).status_code == 200, path
    admin.post(enrol_page(lease), {"refresh_methods": "1"})
    admin.post("/run/match/")
    admin.post(f"/webhooks/{event.pk}/replay/")
    for path in pages:  # each with a queued job
        page = admin.get(path).content.decode()
        assert 'id="job-state">Queued<' in page and 'http-equiv="refresh"' in page, path
    monkeypatch.setattr("payments.get_provider", lambda: fake)
    run_queued_jobs()
    used = refuse_provider(monkeypatch)
    for path in pages:  # each with a finished job and, for enrolment, a snapshot
        page = admin.get(path).content.decode()
        assert 'id="job-state">Done: ' in page and 'http-equiv="refresh"' not in page, path
    assert used == []


def test_the_enrolment_page_without_a_snapshot_offers_the_refresh_then_lists_the_methods(
    fake: FakeProvider,
) -> None:
    admin, user = client_for(Role.ADMIN)
    lease = make_lease()
    fake.add_payment_method("cus-1", method())
    page = admin.get(enrol_page(lease)).content.decode()
    assert leases.METHODS_NOT_READ.replace("'", "&#x27;") in page
    assert 'id="refresh-methods"' in page and 'id="payment-methods"' not in page
    assert 'type="submit" id="enable-autopay"' not in page and 'id="no-bank-method"' not in page

    response = admin.post(enrol_page(lease), {"refresh_methods": "1"}, follow=True)
    page = response.content.decode()
    assert response.redirect_chain[-1][0] == enrol_page(lease)
    assert REFRESH_QUEUED.replace("'", "&#x27;") in page
    assert 'id="refresh-methods" disabled' in page and 'http-equiv="refresh"' in page
    job = only_job()
    assert (job.kind, job.requested_by, job.params) == (
        JobKind.OPERATOR_METHODS_REFRESH,
        user,
        {},
    )
    assert fake.calls == []

    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.DONE, leases.METHODS_READ.format(count=1))
    fake.calls.clear()
    page = admin.get(enrol_page(lease)).content.decode()
    assert "Methods as of " in page and 'type="submit" id="enable-autopay"' in page
    assert 'id="refresh-methods"' in page and 'id="refresh-methods" disabled' not in page
    assert fake.calls == []


def test_a_second_refresh_while_one_waits_queues_nothing(fake: FakeProvider) -> None:
    admin, _ = client_for(Role.ADMIN)
    lease = make_lease()
    admin.post(enrol_page(lease), {"refresh_methods": "1"})
    response = admin.post(enrol_page(lease), {"refresh_methods": "1"}, follow=True)
    assert BUSY in response.content.decode()
    assert Job.objects.count() == 1


def test_a_refresh_for_an_operator_without_a_customer_is_refused_before_queueing(
    fake: FakeProvider,
) -> None:
    admin, _ = client_for(Role.ADMIN)
    lease = make_lease(operator=lease_operator(customer=None))
    response = admin.post(enrol_page(lease), {"refresh_methods": "1"}, follow=True)
    assert leases.NO_PROVIDER_CUSTOMER in response.content.decode()
    assert not Job.objects.exists()


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BILLING])
def test_only_an_admin_may_refresh_the_methods(fake: FakeProvider, role: Role) -> None:
    client, _ = client_for(role)
    lease = make_lease()
    assert client.post(enrol_page(lease), {"refresh_methods": "1"}).status_code == 403
    assert client.get(enrol_page(lease)).status_code == 403
    assert not Job.objects.exists()


def test_a_successful_link_or_sync_queues_a_methods_read_and_a_failed_one_does_not(
    fake: FakeProvider,
) -> None:
    add_provider_customer(fake, "cus_link_0001", "link@example.com")
    manual = new_operator(email=None, display_name="Manual")
    jobs.enqueue(
        JobKind.OPERATOR_LINK,
        manual,
        {"customer_id": "cus_link_0001", "payment_method_id": "pm_bank-0001"},
        None,
    )
    failing = new_operator(email="fails@example.com")
    fake.fail_next("find_customers", ProviderRejected(422, "x", error_fields=("email",)))
    jobs.enqueue(JobKind.OPERATOR_SYNC, failing, None, None)
    ran = run_queued_jobs()
    assert [(job.kind, job.status) for job in ran] == [
        (JobKind.OPERATOR_LINK, JobStatus.DONE),
        (JobKind.OPERATOR_SYNC, JobStatus.FAILED),
        (JobKind.OPERATOR_METHODS_REFRESH, JobStatus.DONE),
    ]
    assert ran[2].entity_id == manual.pk
    assert leases.payment_method_choices(make_lease(operator=manual)).fetched_at is not None


def test_the_automatic_read_is_not_the_operator_page_state(fake: FakeProvider) -> None:
    billing, _ = client_for(Role.BILLING)
    operator = new_operator()
    jobs.enqueue(JobKind.OPERATOR_SYNC, operator, None, None)
    jobs.run(jobs.claim_next())  # type: ignore[arg-type]
    assert Job.objects.filter(kind=JobKind.OPERATOR_METHODS_REFRESH, status="queued").exists()
    page = billing.get(detail(operator)).content.decode()
    assert 'id="job-state">Done: Created the customer at the payment provider.<' in page
    assert 'http-equiv="refresh"' not in page
    latest = jobs.latest_for(operator, [JobKind.OPERATOR_METHODS_REFRESH])
    assert latest is not None and latest.status == JobStatus.QUEUED


def test_refresh_methods_returns_the_waiting_read() -> None:
    operator = new_operator()
    first = jobs.refresh_methods(operator, None)
    assert jobs.refresh_methods(operator, None).pk == first.pk
    jobs.finish(first, JobStatus.DONE, "done")
    assert jobs.refresh_methods(operator, None).pk != first.pk


def test_match_is_queued_once_and_its_report_is_shown_masked_and_cut(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing, user = client_for(Role.BILLING)
    response = billing.post("/run/match/", follow=True)
    assert MATCH_QUEUED in response.content.decode()
    second = billing.post("/run/match/", follow=True)
    assert BUSY in second.content.decode()
    job = only_job()
    assert (job.kind, job.entity_type, job.entity_id, job.requested_by) == (
        JobKind.RUN_MATCH,
        "rent_run",
        jobs.RENT_RUN.pk,
        user,
    )
    assert 'id="match-invoices" disabled' in billing.get("/run/").content.decode()

    many = rent_run.MatchReport(schedules=3)
    many.lines += [
        rent_run.MatchLine(
            rent_run.MatchAction.REPORTED, mask_identifier(f"inv-secret-{n:04d}"), "Not linked."
        )
        for n in range(runners.MATCH_MAX_LINES + 5)
    ]
    many.lines.append(rent_run.MatchLine(rent_run.MatchAction.ALREADY, "x", "Already."))
    monkeypatch.setattr(rent_run, "match_schedule_invoices", lambda actor: many)
    [job] = run_queued_jobs()
    assert job.status == JobStatus.DONE
    lines = job.result.splitlines()
    assert lines[0].startswith("Schedules read: 3. ") and "Already linked: 1." in lines[0]
    assert len(lines) == 1 + runners.MATCH_MAX_LINES + 1
    assert lines[-1] == "5 more lines not shown."
    page = billing.get("/run/").content.decode()
    assert 'id="match-report"' in page and "inv-secret" not in page
    assert page.count("Not linked.</li>") == runners.MATCH_MAX_LINES
    assert 'id="match-invoices" disabled' not in page


def test_the_match_report_text_names_a_refusal_or_a_failure_and_cuts_long_lines() -> None:
    assert runners.match_report_text(rent_run.MatchReport(refused=("Paused.",))) == "Paused."
    failed = rent_run.MatchReport(schedules=1, failure=rent_run.MATCH_FAILED)
    failed.lines.append(rent_run.MatchLine(rent_run.MatchAction.REPORTED, "i…1", "x" * 1000))
    lines = runners.match_report_text(failed).splitlines()
    assert lines[-1] == rent_run.MATCH_FAILED
    assert len(lines[1]) == runners.MATCH_LINE_LENGTH and lines[1].endswith("...")


def test_a_refused_or_failed_match_job_is_failed(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    for report in (
        rent_run.MatchReport(refused=("Paused.",)),
        rent_run.MatchReport(failure=rent_run.MATCH_FAILED),
    ):
        monkeypatch.setattr(rent_run, "match_schedule_invoices", lambda actor, r=report: r)
        jobs.enqueue(JobKind.RUN_MATCH, jobs.RENT_RUN, None, None)
        [job] = run_queued_jobs()
        assert job.status == JobStatus.FAILED


def test_with_the_breaker_open_match_queues_nothing_and_says_why(
    fake: FakeProvider,
    health: Any,  # noqa: F811 (the autouse fixture, asked for by name)
) -> None:
    breaker, _ = health
    for _ in range(3):
        breaker.record(Outcome.CLIENT_ERROR)
    billing, _ = client_for(Role.BILLING)
    response = billing.post("/run/match/", follow=True)
    assert BREAKER_OPEN in response.content.decode()
    assert not Job.objects.exists()


def test_a_replay_is_queued_once_and_shows_its_outcome(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        __import__("payments.webhooks.handlers", fromlist=["HANDLERS"]).HANDLERS,
        "update_invoice",
        lambda event_type, object_id: WebhookOutcome.PROCESSED,
    )
    billing, _ = client_for(Role.BILLING)
    event = _stored_event()
    billing.post(f"/webhooks/{event.pk}/replay/")
    second = billing.post(f"/webhooks/{event.pk}/replay/", follow=True)
    assert BUSY in second.content.decode()
    page = second.content.decode()
    assert 'id="replay" disabled' in page
    [job] = run_queued_jobs()
    assert (job.kind, job.status, job.result) == (
        JobKind.WEBHOOK_REPLAY,
        JobStatus.DONE,
        "Replayed. Outcome: processed.",
    )
    event.refresh_from_db()
    assert event.outcome == WebhookOutcome.PROCESSED


def test_a_replay_of_a_deleted_event_fails_with_a_sentence() -> None:
    event = _stored_event()
    jobs.enqueue(JobKind.WEBHOOK_REPLAY, event, None, None)
    WebhookEvent.objects.filter(pk=event.pk).delete()
    [job] = run_queued_jobs()
    assert (job.status, job.result) == (JobStatus.FAILED, runners.NO_EVENT)


def test_the_worker_event_pass_still_skips_an_event_a_failed_replay_attempted(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(event_type: str, object_id: str) -> str:
        raise ProviderRejected(422, "CANARY", error_fields=("id",))

    monkeypatch.setitem(
        __import__("payments.webhooks.handlers", fromlist=["HANDLERS"]).HANDLERS,
        "update_invoice",
        failing,
    )
    event = _stored_event()
    jobs.enqueue(JobKind.WEBHOOK_REPLAY, event, None, None)
    [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert job.result == (
        "Replayed. The handler failed again; outcome: failed. (ProviderRejected (fields: id))"
    )
    assert "CANARY" not in job.result
    assert worker.next_event_id() is None


def test_enqueue_refuses_the_new_kinds_on_another_entity() -> None:
    operator = new_operator()
    event = _stored_event()
    for kind, entity in (
        (JobKind.RUN_MATCH, operator),
        (JobKind.WEBHOOK_REPLAY, jobs.RENT_RUN),
        (JobKind.OPERATOR_METHODS_REFRESH, event),
    ):
        with pytest.raises(ValueError, match="does not act on this entity"):
            jobs.enqueue(kind, entity, None, None)
    with pytest.raises(ValueError, match="Unknown job entity type"):
        jobs.enqueue(JobKind.RUN_MATCH, jobs.Target("nothing", uuid.uuid4()), None, None)
    assert not Job.objects.exists()
    assert set(jobs.ENTITY_OF_KIND) == set(JobKind.values) == set(runners.RUNNERS)


# --- The three reads under STAX_CALLER=none: refused, nothing half written ------------------


@contextmanager
def as_none_with_the_real_adapter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The real adapter in a process that may not call; no request may leave."""

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no request may leave in tests")

    monkeypatch.setattr("httpx.Client.send", no_network)
    monkeypatch.setattr("payments._providers", {})
    monkeypatch.setattr("payments.get_provider", real_get_provider)
    with override_settings(STAX_CALLER="none", STAX_PROVIDER="stax"):
        yield


def _state() -> dict[str, list[Any]]:
    """Every row the three reads could write, except the job itself and the audit log."""
    return {
        "snapshots": sorted(
            (str(row.pk), str(row.payload), row.fetched_at.isoformat())
            for row in ProviderSnapshot.objects.all()
        ),
        "refs": sorted(
            (str(pk), external_id, str(superseded_at))
            for pk, external_id, superseded_at in ExternalRef.objects.values_list(
                "pk", "external_id", "superseded_at"
            )
        ),
        "invoices": sorted(
            (str(pk), str(charge), status)
            for pk, charge, status in Invoice.objects.values_list(
                "pk", "rent_charge_id", "provider_status"
            )
        ),
        "charges": sorted(
            (str(pk), status, review)
            for pk, status, review in RentCharge.objects.values_list("pk", "status", "needs_review")
        ),
        "jobs": sorted(str(pk) for pk in Job.objects.values_list("pk", flat=True)),
    }


def test_a_refused_methods_read_keeps_the_previous_snapshot(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = make_lease()
    assert lease.operator is not None
    fake.add_payment_method("cus-1", method())
    assert leases.refresh_payment_methods(lease.operator.pk).ok
    queued = jobs.enqueue(JobKind.OPERATOR_METHODS_REFRESH, lease.operator, None, None)
    before = _state()
    with as_none_with_the_real_adapter(monkeypatch):
        [job] = run_queued_jobs()
    assert job.pk == queued.pk
    assert job.status == JobStatus.FAILED and "ProviderNotPermittedHere" in job.result
    assert _state() == before
    assert leases.payment_method_choices(lease).fetched_at is not None


def test_a_refused_match_links_nothing_and_records_the_failure(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, [ref] = enrolled(fake)
    rent_run.create_charges(timezone.localdate(), actor=None)
    invoice(fake, ref)
    jobs.enqueue(JobKind.RUN_MATCH, jobs.RENT_RUN, None, None)
    before = _state()
    with as_none_with_the_real_adapter(monkeypatch):
        [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert job.result.splitlines()[-1] == rent_run.MATCH_FAILED
    assert _state() == before
    [audit] = AuditLog.objects.filter(action="rent_run.match_failed")
    assert audit.after["failure"] == "ProviderNotPermittedHere"


def test_a_refused_replay_rolls_its_handler_back_and_counts_one_attempt(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_world(fake)
    event = _stored_event()
    jobs.enqueue(JobKind.WEBHOOK_REPLAY, event, None, None)
    before = _state()
    with as_none_with_the_real_adapter(monkeypatch):
        [job] = run_queued_jobs()
    assert job.status == JobStatus.FAILED
    assert job.result.endswith("(ProviderNotPermittedHere)")
    assert _state() == before
    event.refresh_from_db()
    assert (event.processed_at, event.attempts, event.error) == (
        None,
        1,
        "ProviderNotPermittedHere",
    )
    assert event.outcome == WebhookOutcome.FAILED
    # The worker's own pass does not take it again; it waits for a replay.
    assert worker.next_event_id() is None


# --- D4: the worker app and the web app ----------------------------------------------------


def test_the_worker_app_is_one_replica_without_ingress_running_the_worker() -> None:
    module = (REPO / "infra/modules/containerAppWorker.bicep").read_text()
    assert (
        "'python backend/manage.py provider_environment_check && "
        "exec python backend/manage.py worker'"
    ) in module
    assert "deploy_migrate" not in module
    assert "ingress" not in module.replace("No ingress", "").replace("no ingress", "")
    assert "minReplicas: 1\n        maxReplicas: 1\n" in module
    assert "name: 'STAX_CALLER'\n    value: 'worker'\n" in module
    web = (REPO / "infra/modules/containerAppWeb.bicep").read_text()
    for secret in re.findall(r"name: '([a-z-]+)'\n    keyVaultUrl", web):
        assert f"name: '{secret}'\n    keyVaultUrl" in module
    # The document store is the web app's alone (PLAN-010c D1): the worker and the job
    # never touch a file, so they carry none of its settings.
    web_only = {"DOCUMENTS_STORAGE_ACCOUNT_URL", "DOCUMENTS_CONTAINER", "AZURE_CLIENT_ID"}
    names = set(re.findall(r"name: '([A-Z_]+)'\n", web))
    assert web_only <= names
    for name in names - web_only:
        assert f"name: '{name}'\n" in module
    for name in web_only:
        assert name not in module


def test_main_wires_the_worker_with_the_web_image_and_staging_runs_the_web_as_none() -> None:
    main = (REPO / "infra/main.bicep").read_text()
    assert "module worker 'modules/containerAppWorker.bicep'" in main
    block = main.split("module worker 'modules/containerAppWorker.bicep'")[1].split("\n}\n")[0]
    assert "image: webImage" in block and "identityResourceId: identity.outputs" in block
    assert "param workerAppName string = '${resourceNamePrefix}-worker'" in main
    assert "output workerAppName string" in main
    params = (REPO / "infra/parameters/staging.bicepparam").read_text()
    assert re.search(r"^param staxCaller = 'none'$", params, re.MULTILINE)
    assert not re.search(r"^param webMaxReplicas", params, re.MULTILINE)


# --- D13: the deployment job ---------------------------------------------------------------

CI = REPO / ".github/workflows/ci.yml"


def _step(name: str) -> str:
    text = CI.read_text()
    start = text.index(f"- name: {name}")
    end = text.find("\n      - name:", start + 1)
    return text[start : end if end != -1 else len(text)]


def _run_block(step: str) -> str:
    lines = step.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == "run: |") + 1
    return "\n".join(line[10:] for line in lines[start:]) + "\n"


def test_both_updates_tolerate_an_error_and_the_check_comes_before_the_smoke_test() -> None:
    text = CI.read_text()
    web = _step("Roll the web container app onto the new image")
    workers = _step("Roll the worker container app onto the new image")
    reconcile = _step("Roll the reconcile job onto the new image")
    for step in (web, workers, reconcile):
        assert '|| echo "update returned an error; verifying the revision instead"' in step
        assert '--image "$ACR_NAME.azurecr.io/finance-web:$IMAGE_TAG"' in step
    assert "vars.WORKER_APP_NAME || 'finance-staging-worker'" in workers
    assert "az containerapp job update" in reconcile
    assert "vars.RECONCILE_JOB_NAME || 'finance-staging-reconcile'" in reconcile
    order = [
        text.index("- name: Roll the web container app"),
        text.index("- name: Roll the worker container app"),
        text.index("- name: Roll the reconcile job"),
        text.index("- name: Verify the web and worker revisions are live on this commit"),
        text.index("- name: Smoke test the health endpoint"),
    ]
    assert order == sorted(order)
    verify = _step("Verify the web and worker revisions are live on this commit")
    assert "IMAGE_TAG: ${{ github.sha }}" in verify
    assert "vars.RECONCILE_JOB_NAME || 'finance-staging-reconcile'" in verify
    for word in ("az containerapp revision list", "Healthy", "Running", "600"):
        assert word in verify
    assert "properties.template.containers[0].image" in verify
    # Read-only queries only: the revision list and the job's own description.
    assert not re.search(
        r"\baz (?!containerapp revision list|containerapp job show)", _run_block(verify)
    )


def _run_verification(
    tmp_path: Path, web: str, workers: str, job_tag: str = "0" * 40
) -> subprocess.CompletedProcess[str]:
    """The verification script with ``az``, ``date`` and ``sleep`` stubbed: ``az`` prints
    the count given per app and the job's image with ``job_tag``, the clock jumps 100 s
    per reading, sleep returns at once."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "az": f'case "$*" in *web-app*) echo {web};; *worker-app*) echo {workers};; '
        f"*reconcile-job*) echo registry.azurecr.io/finance-web:{job_tag};; esac\n",
        "date": f'n=$(cat "{tmp_path}/clock" 2>/dev/null || echo 0)\n'
        f'echo $((n + 100)) > "{tmp_path}/clock"\necho $((n + 1000))\n',
        "sleep": "exit 0\n",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/bash\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    script = _run_block(_step("Verify the web and worker revisions are live on this commit"))
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "WEB_APP_NAME": "web-app",
        "WORKER_APP_NAME": "worker-app",
        "RECONCILE_JOB_NAME": "reconcile-job",
        "RESOURCE_GROUP": "group",
        "IMAGE_TAG": "0" * 40,
    }
    return subprocess.run(  # noqa: S603 (a fixed interpreter and the repository's own script)
        ["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=60
    )


def test_the_verification_passes_when_both_revisions_are_live(tmp_path: Path) -> None:
    result = _run_verification(tmp_path, "1", "1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "web and worker run this commit" in result.stdout


@pytest.mark.parametrize(
    ("web", "workers", "job_tag"),
    [("1", "0", "0" * 40), ("0", "1", "0" * 40), ("0", "0", "0" * 40), ("1", "1", "1" * 40)],
)
def test_the_verification_fails_after_ten_minutes_when_a_revision_is_not_live(
    tmp_path: Path, web: str, workers: str, job_tag: str
) -> None:
    result = _run_verification(tmp_path, web, workers, job_tag)
    assert result.returncode == 1
    assert "not live after 10 minutes" in result.stderr
