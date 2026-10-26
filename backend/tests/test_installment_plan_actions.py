"""Pause, Resume and Stop for installment plans on the store's Autopay tab
(docs/qa/PLAN-012f-b D16, slice D-3), and findings D2-1 and D2-2.

Pause releases the next unfired installment's schedule through the release job and pauses
the plan; Resume sets the unpaid debits up again (a passed date moved to the next business
day, audited); Stop is the cancel behind its "Stop this plan?" page. The refusals, the busy
guard, two pauses racing (PostgreSQL), what a paused plan leaves out, the tab's and the
parent card's actions by role and status, the attention item, the migration and the guard
lists; the plan's name on every page (D2-1) and the live preview's own sentences (D2-2).
The worker runs through the queue against a :class:`~payments.fake.FakeProvider`; nothing
reaches a network.
"""

from __future__ import annotations

import importlib
import itertools
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.urls import reverse

from accounts.roles import Role
from ledger.models import (
    LIVE_PLAN_STATUSES,
    AuditLog,
    CollectionMethod,
    InstallmentCadence,
    InstallmentOrigin,
    InstallmentPlan,
    InstallmentPlanStatus,
    Lease,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
)
from ledger.services import attention, installments, invoice_builder, one_off_charges, workspace
from payments import jobs
from payments.fake import FakeProvider
from payments.models import Job, JobKind, JobStatus
from payments.provider import InvoiceRecord
from tests.job_helpers import run_queued_jobs
from tests.receivables_helpers import a_lease, a_store
from tests.test_installments import a_one_off, a_user, calls, pay, plan_for, rows_of
from tests.test_one_off_charges import an_operator
from tests.test_operator_views import client_for
from tests.test_rent_run import health  # noqa: F401 (autouse fixture)

pytestmark = pytest.mark.django_db

D = Decimal
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
TODAY = date(2026, 10, 6)
FIRST = date(2026, 10, 20)

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="row locks: PostgreSQL only"
)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """NOW, one microsecond later on each read: the jobs of one entity keep their order
    (``jobs.latest_for`` sorts by ``created_at``, a tie falls to the random id)."""
    ticks = itertools.count()
    monkeypatch.setattr(
        "django.utils.timezone.now", lambda: NOW + timedelta(microseconds=next(ticks))
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr("payments.get_provider", lambda: provider)
    return provider


@pytest.fixture
def worker() -> Iterator[None]:
    with override_settings(STAX_CALLER="worker"):
        yield


# --- Builders ----------------------------------------------------------------------------


def a_store_lease(fake: FakeProvider) -> Lease:
    return a_lease(a_store("Store 4410"), an_operator(fake), started=date(2026, 1, 1))


def op(lease: Lease) -> Operator:
    assert lease.operator is not None
    return lease.operator


def autopay_plan(
    fake: FakeProvider, *, lease: Lease | None = None, run: bool = True
) -> InstallmentPlan:
    """A 1000.00 autopay charge split in three monthly debits from FIRST; with ``run``
    the worker made the three schedules."""
    operator = op(lease) if lease is not None else an_operator(fake)
    parent = a_one_off(operator, collection=CollectionMethod.AUTOPAY_DUE, due=FIRST, lease=lease)
    plan = plan_for(parent)
    if run:
        run_queued_jobs()
    return plan


def fresh(plan: InstallmentPlan) -> InstallmentPlan:
    return InstallmentPlan.objects.get(pk=plan.pk)


def releases() -> list[Job]:
    return list(
        Job.objects.filter(kind=JobKind.SCHEDULE_DELETE, params__release=True).order_by("id")
    )


def creates_of(row: OneOffCharge) -> list[Job]:
    return list(Job.objects.filter(kind=JobKind.SCHEDULE_CREATE, entity_id=row.pk))


def written() -> tuple[int, int, list[str]]:
    """What a refusal must leave as it was: the audit rows, the jobs, the plans' statuses."""
    return (
        AuditLog.objects.count(),
        Job.objects.count(),
        list(InstallmentPlan.objects.order_by("id").values_list("status", flat=True)),
    )


def messages_of(response: Any) -> list[str]:
    return [str(message) for message in get_messages(response.wsgi_request)]


def autopay_tab(lease: Lease) -> str:
    return workspace.tab_url(lease.store_id, workspace.TAB_SCHEDULES)


# --- Pause ---------------------------------------------------------------------------------


def test_pause_queues_one_release_per_unfired_installment_and_pauses_at_once(
    fake: FakeProvider, worker: None
) -> None:
    plan = autopay_plan(fake)
    rows = rows_of(plan)
    refs = [one_off_charges.provider_schedule(row) for row in rows]
    assert all(refs)
    outcome = installments.pause(plan, a_user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (True, installments.PAUSED)
    # Paused at once; the three releases were queued in the same transaction.
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED
    found = releases()
    assert sorted(job.entity_id for job in found) == sorted(row.pk for row in rows)
    assert all(job.params == {installments.RELEASE_PARAM: True} for job in found)
    paused = AuditLog.objects.get(action=installments.PAUSED_ACTION, entity_id=str(plan.pk))
    assert paused.after is not None and paused.after["released_installments"] == [1, 2, 3]
    assert AuditLog.objects.filter(action=installments.RELEASE_REQUESTED_ACTION).count() == 3
    run_queued_jobs()
    for job in found:
        job.refresh_from_db()
        assert (job.status, job.result) == (JobStatus.DONE, str(installments.PAUSE_RELEASED))
    assert sorted(calls(fake, "delete_scheduled_invoice")) == sorted((ref,) for ref in refs)
    # Released, never voided: the installments stay owed while the plan is paused.
    for row in rows:
        row.refresh_from_db()
        assert row.status == OneOffChargeStatus.OWED
        assert one_off_charges.provider_schedule(row) == ""
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED
    assert installments.debits_out_while_paused(fresh(plan)) == []


def test_pause_leaves_a_paid_installment_alone(fake: FakeProvider, worker: None) -> None:
    plan = autopay_plan(fake)
    first, second, third = rows_of(plan)
    assert pay(first, "333.33").ok
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    assert sorted(job.entity_id for job in releases()) == sorted([second.pk, third.pk])


def test_a_schedule_that_fired_before_its_release_is_recorded_and_shown(
    fake: FakeProvider, worker: None
) -> None:
    plan = autopay_plan(fake)
    first, second, third = rows_of(plan)
    # The provider made the second debit's invoice before the release ran.
    fake.invoices["fake-fired"] = InvoiceRecord(
        external_id="fake-fired",
        raw_status="SENT",
        fetched_at=NOW,
        schedule_id=one_off_charges.provider_schedule(second),
    )
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    run_queued_jobs()
    [fired] = [job for job in releases() if job.entity_id == second.pk]
    assert (fired.status, fired.result) == (JobStatus.FAILED, installments.PAUSE_FIRED)
    assert AuditLog.objects.filter(
        action=installments.PAUSE_FIRED_ACTION, entity_id=str(second.pk)
    ).exists()
    sentence = "One debit had already gone out: Installment 2 of 3, Nov 20, 2026."
    assert installments.debits_out_while_paused(fresh(plan)) == [sentence]
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(reverse("ledger:plans:detail", kwargs={"pk": plan.pk})).content.decode()
    assert sentence in body
    # The other two were released; the plan stays paused.
    assert one_off_charges.provider_schedule(first) == ""
    assert one_off_charges.provider_schedule(third) == ""
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED


def test_pause_of_a_plan_with_no_schedule_pauses_and_releases_nothing(fake: FakeProvider) -> None:
    plan = plan_for(a_one_off(an_operator(fake)))
    outcome = installments.pause(plan, a_user(Role.BILLING))
    assert (outcome.ok, outcome.message) == (True, installments.PAUSED_NO_DEBIT)
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED
    assert not Job.objects.exists()


def test_a_paused_plan_still_carries_the_balance_and_stays_paused_until_completed(
    fake: FakeProvider,
) -> None:
    plan = plan_for(a_one_off(an_operator(fake)))
    assert InstallmentPlanStatus.PAUSED in LIVE_PLAN_STATUSES
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    first, second, third = rows_of(plan)
    assert pay(first, "333.33").ok
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED
    assert pay(second, "333.33").ok and pay(third, "333.34").ok
    assert fresh(plan).status == InstallmentPlanStatus.COMPLETED


# --- Resume --------------------------------------------------------------------------------


def test_resume_sets_every_released_debit_up_again_and_activates(
    fake: FakeProvider, worker: None
) -> None:
    plan = autopay_plan(fake)
    rows = rows_of(plan)
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    run_queued_jobs()
    assert all(one_off_charges.provider_schedule(row) == "" for row in rows)
    before = len(calls(fake, "create_scheduled_invoice"))
    outcome = installments.resume(plan, a_user(Role.BILLING))
    assert outcome.ok, outcome.message
    assert outcome.message == installments.RESUMED.format(date="Oct 20, 2026")
    assert fresh(plan).status == InstallmentPlanStatus.ACTIVE
    # One new schedule_create per installment (Pause released all three); no date moved.
    assert [len(creates_of(row)) for row in rows] == [2, 2, 2]
    assert all(
        [job.params for job in creates_of(row) if job.status == JobStatus.QUEUED] == [{}]
        for row in rows
    )
    run_queued_jobs()
    assert len(calls(fake, "create_scheduled_invoice")) == before + 3
    for row, due in zip(rows, [FIRST, date(2026, 11, 20), date(2026, 12, 20)], strict=True):
        row.refresh_from_db()
        assert row.due_date == due and one_off_charges.provider_schedule(row)
    resumed = AuditLog.objects.get(action=installments.RESUMED_ACTION, entity_id=str(plan.pk))
    assert resumed.after is not None
    assert (resumed.after["queued"], resumed.after["moved"]) == ([1, 2, 3], [])
    assert not AuditLog.objects.filter(action=installments.INSTALLMENT_RESCHEDULED_ACTION).exists()


@pytest.mark.parametrize(
    ("today", "moved_to"),
    [
        # A Wednesday: the Thursday; a Friday or a Saturday: the Monday.
        (date(2026, 10, 21), date(2026, 10, 22)),
        (date(2026, 10, 23), date(2026, 10, 26)),
        (date(2026, 10, 24), date(2026, 10, 26)),
    ],
)
def test_resume_moves_a_passed_debit_to_the_next_business_day_and_audits_it(
    fake: FakeProvider, worker: None, today: date, moved_to: date
) -> None:
    plan = autopay_plan(fake)
    rows = rows_of(plan)
    first, second, third = rows
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    run_queued_jobs()
    before = len(calls(fake, "create_scheduled_invoice"))
    outcome = installments.resume(plan, a_user(Role.BILLING), today=today)
    assert outcome.ok, outcome.message
    for row in rows:
        row.refresh_from_db()
    # Only the passed one moves; the later two keep their dates.
    assert (first.due_date, second.due_date, third.due_date) == (
        moved_to,
        date(2026, 11, 20),
        date(2026, 12, 20),
    )
    assert fresh(plan).first_due == moved_to
    [moved] = AuditLog.objects.filter(action=installments.INSTALLMENT_RESCHEDULED_ACTION)
    assert moved.entity_id == str(first.pk)
    assert moved.after is not None and moved.after["due_date"] == moved_to.isoformat()
    resumed = AuditLog.objects.get(action=installments.RESUMED_ACTION, entity_id=str(plan.pk))
    assert resumed.after is not None
    assert resumed.after["queued"] == [1, 2, 3]
    assert resumed.after["moved"] == [{"installment_no": 1, "due": moved_to.isoformat()}]
    # Every installment gets one new schedule_create, the moved one included.
    for row in rows:
        [create] = [job for job in creates_of(row) if job.status == JobStatus.QUEUED]
        assert create.params == {}
    run_queued_jobs()
    assert all(one_off_charges.provider_schedule(row) for row in rows)
    made = calls(fake, "create_scheduled_invoice")[before:]
    rules = sorted(str(call[0].rule)[:16] for call in made)  # type: ignore[attr-defined]
    assert rules == sorted(
        f"DTSTART={day:%Y%m%d}" for day in (moved_to, date(2026, 11, 20), date(2026, 12, 20))
    )


def test_resume_moves_two_passed_debits_to_two_business_days_in_their_order(
    fake: FakeProvider, worker: None
) -> None:
    plan = autopay_plan(fake)
    rows = rows_of(plan)
    first, second, third = rows
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    run_queued_jobs()
    # A Friday: Oct 20 and Nov 20 passed; the Monday and the Tuesday after, never one day.
    outcome = installments.resume(plan, a_user(Role.BILLING), today=date(2026, 11, 27))
    assert outcome.ok, outcome.message
    for row in rows:
        row.refresh_from_db()
    assert (first.due_date, second.due_date, third.due_date) == (
        date(2026, 11, 30),
        date(2026, 12, 1),
        date(2026, 12, 20),
    )
    assert fresh(plan).first_due == date(2026, 11, 30)
    resumed = AuditLog.objects.get(action=installments.RESUMED_ACTION, entity_id=str(plan.pk))
    assert resumed.after is not None
    assert resumed.after["moved"] == [
        {"installment_no": 1, "due": "2026-11-30"},
        {"installment_no": 2, "due": "2026-12-01"},
    ]
    assert sorted(
        AuditLog.objects.filter(action=installments.INSTALLMENT_RESCHEDULED_ACTION).values_list(
            "entity_id", flat=True
        )
    ) == sorted([str(first.pk), str(second.pk)])
    assert [len(creates_of(row)) for row in rows] == [2, 2, 2]


def test_resume_returns_to_attention_when_a_return_is_still_open(
    fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_for(a_one_off(an_operator(fake)))
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    monkeypatch.setattr(
        one_off_charges,
        "plan_attention_reason",
        lambda found: one_off_charges.INSTALLMENT_RETURNED,
    )
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED
    assert installments.resume(plan, a_user(Role.BILLING)).ok
    assert fresh(plan).status == InstallmentPlanStatus.ATTENTION


# --- Stop (the cancel, relabelled) ---------------------------------------------------------


@pytest.mark.parametrize("paused", [False, True])
def test_stop_is_the_cancel_behind_its_confirmation_page(
    fake: FakeProvider, worker: None, paused: bool
) -> None:
    plan = autopay_plan(fake)
    if paused:
        assert installments.pause(plan, a_user(Role.BILLING)).ok
        run_queued_jobs()
    admin, _ = client_for(Role.ADMIN)
    url = reverse("ledger:plans:cancel", kwargs={"pk": plan.pk})
    page = admin.get(url)
    assert page.status_code == 200
    body = page.content.decode()
    assert "<title>" in body and "Stop this plan?" in body
    assert str(installments.CANCEL_HELP) in body and "Stop the plan" in body
    assert "Cancel the installment plan" not in body
    response = admin.post(url, {"note": "The operator settled elsewhere."}, follow=True)
    assert fresh(plan).status == InstallmentPlanStatus.CANCELLED
    assert any(text.startswith("Plan stopped.") for text in messages_of(response))
    run_queued_jobs()
    assert all(row.status == OneOffChargeStatus.VOIDED for row in rows_of(plan))


# --- Refusals ------------------------------------------------------------------------------


def test_pause_and_resume_refuse_a_plan_in_the_wrong_status_and_write_nothing(
    fake: FakeProvider,
) -> None:
    billing = a_user(Role.BILLING)
    plan = plan_for(a_one_off(an_operator(fake)))
    before = written()
    refused = installments.resume(plan, billing)
    assert (refused.ok, refused.message) == (False, installments.RESUME_NOT_PAUSED)
    assert written() == before
    assert installments.pause(plan, billing).ok
    before = written()
    refused = installments.pause(plan, billing)
    assert (refused.ok, refused.message) == (False, installments.PAUSE_NOT_ACTIVE)
    assert written() == before
    done = plan_for(a_one_off(an_operator(fake, customer="cus-2", bank="pm-2")))
    for row, amount in zip(rows_of(done), ("333.33", "333.33", "333.34"), strict=True):
        assert pay(row, amount).ok
    assert fresh(done).status == InstallmentPlanStatus.COMPLETED
    before = written()
    for action in (installments.pause, installments.resume):
        assert not action(done, billing).ok
    assert written() == before


def test_pause_and_resume_need_billing_in_the_service(fake: FakeProvider) -> None:
    plan = plan_for(a_one_off(an_operator(fake)))
    before = written()
    for actor in (None, a_user(Role.VIEWER)):
        refused = installments.pause(plan, actor)
        assert (refused.ok, refused.message) == (False, installments.PAUSE_BILLING)
        refused = installments.resume(plan, actor)
        assert (refused.ok, refused.message) == (False, installments.PAUSE_BILLING)
    assert written() == before


def test_the_routes_check_the_role_and_the_plan(fake: FakeProvider) -> None:
    lease = a_store_lease(fake)
    plan = plan_for(a_one_off(op(lease), lease=lease))
    viewer, _ = client_for(Role.VIEWER)
    billing, _ = client_for(Role.BILLING)
    before = written()
    for name in ("pause", "resume"):
        url = reverse(f"ledger:plans:{name}", kwargs={"pk": plan.pk})
        assert viewer.post(url).status_code == 403
        assert billing.get(url).status_code == 405
        for missing in ("00000000-0000-0000-0000-000000000000", "not-a-plan"):
            gone = reverse(f"ledger:plans:{name}", kwargs={"pk": missing})
            assert billing.post(gone).status_code == 404
    stop = reverse("ledger:plans:cancel", kwargs={"pk": plan.pk})
    assert billing.get(stop).status_code == 403
    assert billing.post(stop, {"note": "No."}).status_code == 403
    assert written() == before


def test_billing_pauses_and_resumes_from_the_tab_and_lands_back_on_it(fake: FakeProvider) -> None:
    lease = a_store_lease(fake)
    plan = plan_for(a_one_off(op(lease), lease=lease))
    billing, _ = client_for(Role.BILLING)
    response = billing.post(reverse("ledger:plans:pause", kwargs={"pk": plan.pk}))
    assert (response.status_code, response["Location"]) == (302, autopay_tab(lease))
    assert messages_of(response) == [installments.PAUSED_NO_DEBIT]
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED
    again = billing.post(reverse("ledger:plans:pause", kwargs={"pk": plan.pk}))
    # The first message was never shown (no page between): both wait in the session.
    assert messages_of(again)[-1] == installments.PAUSE_NOT_ACTIVE
    response = billing.post(reverse("ledger:plans:resume", kwargs={"pk": plan.pk}))
    assert response["Location"] == autopay_tab(lease)
    assert fresh(plan).status == InstallmentPlanStatus.ACTIVE


# --- The busy guard --------------------------------------------------------------------------


def test_pause_and_resume_wait_for_a_provider_request(fake: FakeProvider, worker: None) -> None:
    plan = autopay_plan(fake, run=False)
    billing = a_user(Role.BILLING)
    before = written()
    # The three schedule_create jobs are still queued.
    refused = installments.pause(plan, billing)
    assert (refused.ok, refused.message) == (False, installments.SHIFT_BUSY)
    assert written() == before
    run_queued_jobs()
    assert installments.pause(plan, billing).ok
    found = releases()
    assert len(found) == 3
    # The three releases are still queued; then two have run and one still waits.
    for waiting in (3, 1):
        before = written()
        refused = installments.resume(plan, billing)
        assert (refused.ok, refused.message) == (False, installments.SHIFT_BUSY)
        assert written() == before
        assert [job.status for job in releases()].count(JobStatus.QUEUED) == waiting
        if waiting == 3:
            for job in found[:2]:
                jobs.run(job)
    run_queued_jobs()
    assert installments.resume(plan, billing).ok
    # Resume's three schedule_create jobs are queued: Pause waits for them in turn.
    before = written()
    refused = installments.pause(plan, billing)
    assert (refused.ok, refused.message) == (False, installments.SHIFT_BUSY)
    assert written() == before
    run_queued_jobs()
    assert installments.pause(plan, billing).ok


def test_pause_waits_for_the_parents_release(fake: FakeProvider, worker: None) -> None:
    operator = an_operator(fake)
    parent = a_one_off(operator, collection=CollectionMethod.AUTOPAY_DUE, due=FIRST)
    jobs.enqueue(JobKind.SCHEDULE_CREATE, parent, None, None)
    run_queued_jobs()
    assert one_off_charges.provider_schedule(parent)
    plan = plan_for(parent)
    assert installments.release_pending(plan)
    before = written()
    refused = installments.pause(plan, a_user(Role.BILLING))
    assert (refused.ok, refused.message) == (False, installments.SHIFT_BUSY)
    assert written() == before


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_two_pauses_at_the_same_moment_leave_one_release(fake: FakeProvider, worker: None) -> None:
    plan = autopay_plan(fake)
    billing = a_user(Role.BILLING)
    barrier = threading.Barrier(2)
    outcomes: list[installments.Outcome] = []
    errors: list[BaseException] = []

    def pause() -> None:
        try:
            barrier.wait(timeout=10)
            outcomes.append(installments.pause(plan, billing))
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=pause) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert sorted(outcome.ok for outcome in outcomes) == [False, True]
    assert len(releases()) == 1
    assert AuditLog.objects.filter(action=installments.PAUSED_ACTION).count() == 1


# --- What a paused plan leaves out ----------------------------------------------------------


def test_a_paused_plans_reschedule_moves_the_dates_and_sets_nothing_up(
    fake: FakeProvider, worker: None
) -> None:
    plan = autopay_plan(fake)
    rows = rows_of(plan)
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    run_queued_jobs()
    outcome = installments.reschedule(
        plan, date(2026, 12, 1), InstallmentCadence.MONTHLY, a_user(Role.BILLING)
    )
    assert outcome.ok, outcome.message
    # D3-1: Pause released every schedule, so a reschedule while paused moves the dates and
    # sets nothing up: no new schedule_create, no provider update, no schedule on any row.
    assert [len(creates_of(row)) for row in rows] == [1, 1, 1]
    run_queued_jobs()
    for row, due in zip(rows, [date(2026, 12, 1), date(2027, 1, 1), date(2027, 2, 1)], strict=True):
        row.refresh_from_db()
        assert (row.due_date, one_off_charges.provider_schedule(row)) == (due, "")
    assert calls(fake, "update_scheduled_invoice") == []
    assert fresh(plan).status == InstallmentPlanStatus.PAUSED


def test_a_paused_plans_debits_are_not_listed_as_collected_by_autopay(
    fake: FakeProvider, worker: None
) -> None:
    lease = a_store_lease(fake)
    plan = autopay_plan(fake, lease=lease)
    owner = lease.store
    shown = workspace.schedules_tab(owner, TODAY, NOW, Role.BILLING)["autopay_charges"]
    assert {row.pk for row in rows_of(plan)} <= {row.pk for row in shown}
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    shown = workspace.schedules_tab(owner, TODAY, NOW, Role.BILLING)["autopay_charges"]
    assert not {row.pk for row in rows_of(plan)} & {row.pk for row in shown}


# --- The Autopay tab and the parent's card --------------------------------------------------


def _ids(plan: InstallmentPlan) -> dict[str, str]:
    return {
        name: f'id="installment-plan-{name}-{plan.pk}"'
        for name in ("pause", "resume", "reschedule", "stop")
    }


@pytest.mark.parametrize(
    ("role", "paused", "shown"),
    [
        (Role.VIEWER, False, set()),
        (Role.VIEWER, True, set()),
        (Role.BILLING, False, {"pause", "reschedule"}),
        (Role.BILLING, True, {"resume", "reschedule"}),
        (Role.ADMIN, False, {"pause", "reschedule", "stop"}),
        (Role.ADMIN, True, {"resume", "reschedule", "stop"}),
    ],
)
def test_the_tab_and_the_parent_card_show_the_actions_by_role_and_status(
    fake: FakeProvider, role: Role, paused: bool, shown: set[str]
) -> None:
    lease = a_store_lease(fake)
    parent = a_one_off(op(lease), lease=lease)
    plan = plan_for(parent)
    if paused:
        assert installments.pause(plan, a_user(Role.BILLING)).ok
    client, _ = client_for(role)
    pages = [
        client.get(autopay_tab(lease)).content.decode(),
        client.get(
            reverse("ledger:one_off_charges:detail", kwargs={"pk": parent.pk})
        ).content.decode(),
    ]
    for body in pages:
        assert {name for name, marker in _ids(plan).items() if marker in body} == shown
        assert ("Paused" in body) is paused
    tab = pages[0]
    row = tab[tab.index(f'id="store-plan-{plan.pk}"') :].split("</tr>", 1)[0]
    assert (">Paused</td>" in row) is paused
    if "stop" in shown:
        assert reverse("ledger:plans:cancel", kwargs={"pk": plan.pk}) in row


def test_a_stopped_plan_leaves_the_tab_and_offers_nothing(fake: FakeProvider) -> None:
    lease = a_store_lease(fake)
    plan = plan_for(a_one_off(op(lease), lease=lease))
    assert installments.cancel(plan, "Settled.", a_user(Role.ADMIN)).ok
    admin, _ = client_for(Role.ADMIN)
    body = admin.get(autopay_tab(lease)).content.decode()
    assert f'id="store-plan-{plan.pk}"' not in body
    assert not any(marker in body for marker in _ids(plan).values())
    assert installments.plan_actions(fresh(plan), billing=True, admin=True) == {
        "can_pause": False,
        "can_resume": False,
        "can_reschedule": False,
        "can_stop": False,
    }


# --- Needs review ----------------------------------------------------------------------------


@pytest.mark.parametrize(("days", "listed"), [(30, False), (31, True), (90, True)])
def test_a_plan_paused_over_30_days_lands_in_needs_review(
    fake: FakeProvider, days: int, listed: bool
) -> None:
    lease = a_store_lease(fake)
    plan = plan_for(a_one_off(op(lease), lease=lease))
    assert installments.pause(plan, a_user(Role.BILLING)).ok
    AuditLog.objects.filter(action=installments.PAUSED_ACTION).update(at=NOW - timedelta(days=days))
    found = [
        item
        for item in attention.attention(TODAY).items
        if item.kind == attention.INSTALLMENT_PLAN_PAUSED
    ]
    if not listed:
        assert found == []
        return
    [item] = found
    assert item.severity == attention.LOW
    assert item.text == (
        f"Installment plan paused for {days} days: Store 4410, Arrears July to September 2026"
    )
    assert item.why == f"Installment plan paused for {days} days"
    assert item.url == reverse("ledger:plans:detail", kwargs={"pk": plan.pk})
    assert item.store_id == lease.store_id
    by_operator = attention.attention(TODAY, operator=lease.operator_id).items
    assert any(row.kind == attention.INSTALLMENT_PLAN_PAUSED for row in by_operator)
    assert installments.resume(plan, a_user(Role.BILLING)).ok
    assert not [
        row
        for row in attention.attention(TODAY).items
        if row.kind == attention.INSTALLMENT_PLAN_PAUSED
    ]


# --- The migration and the guard lists -------------------------------------------------------


def _migrate(target: tuple[str, str]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate([target])


def _status_check() -> str:
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            cursor.execute("SELECT sql FROM sqlite_master WHERE name = 'installment_plans'")
        else:
            cursor.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'installment_plans_status_valid'"
            )
        return str(cursor.fetchone()[0])


@pytest.mark.django_db(transaction=True)
def test_migration_0055_adds_paused_and_reverses() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        _migrate(("ledger", "0054_installment_settlement"))
        assert "'paused'" not in _status_check()
        _migrate(("ledger", "0055_installment_plan_paused"))
        assert "'paused'" in _status_check() and "'attention'" in _status_check()
        _migrate(("ledger", "0054_installment_settlement"))
        assert "'paused'" not in _status_check()
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaves)
    assert "'paused'" in _status_check()


def test_migration_0055_guards_no_column_and_is_on_both_guard_lists() -> None:
    module = importlib.import_module("ledger.migrations.0055_installment_plan_paused")
    assert module.GUARDED_COLUMNS == {}
    here = Path(__file__).resolve().parent
    for name in ("test_card_number_guard.py", "test_migrations_roundtrip.py"):
        text = (here / name).read_text(encoding="utf-8")
        assert '"ledger.migrations.0055_installment_plan_paused",' in text, name


# --- D2-1: the plan's name on every page -----------------------------------------------------


def _settlement(fake: FakeProvider) -> tuple[Lease, OneOffCharge, InstallmentPlan]:
    lease = a_store_lease(fake)
    parent = a_one_off(op(lease), "900.00", lease=lease)
    lines = installments.settlement_lines(D("900.00"), 3, FIRST, InstallmentCadence.MONTHLY)
    outcome = installments.create_plan(
        parent,
        3,
        FIRST,
        InstallmentCadence.MONTHLY,
        lines,
        a_user(Role.BILLING),
        collection=CollectionMethod.AUTOPAY_DUE,
        origin=InstallmentOrigin.SETTLEMENT,
    )
    assert outcome.ok and outcome.plan is not None, outcome.message
    return lease, parent, outcome.plan


def test_a_settlement_plan_is_named_so_on_the_charges_tab_its_pages_and_tables(
    fake: FakeProvider,
) -> None:
    lease, parent, plan = _settlement(fake)
    viewer, _ = client_for(Role.VIEWER)
    charges = viewer.get(workspace.tab_url(lease.store_id, workspace.TAB_CHARGES)).content.decode()
    assert "On settlement plan" in charges and "On installment plan" not in charges
    detail = viewer.get(reverse("ledger:plans:detail", kwargs={"pk": plan.pk})).content.decode()
    assert "Settlement plan, Arrears July to September 2026" in detail
    parent_page = viewer.get(
        reverse("ledger:one_off_charges:detail", kwargs={"pk": parent.pk})
    ).content.decode()
    for body in (detail, parent_page):
        for number in (1, 2, 3):
            assert f">Settlement plan · {number} of 3</a>" in body
        assert ">1 of 3</a>" not in body
    assert installments.plan_name(InstallmentOrigin.SPLIT) == "Installment plan"
    assert installments.on_plan_badge(InstallmentOrigin.SPLIT) == "On installment plan"


def test_a_split_plans_rows_read_installment_k_of_n(fake: FakeProvider) -> None:
    plan = plan_for(a_one_off(an_operator(fake)))
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get(reverse("ledger:plans:detail", kwargs={"pk": plan.pk})).content.decode()
    assert ">Installment 2 of 3</a>" in body and "Installment plan, " in body
    assert 'id="plan-cancel">Stop plan</a>' not in body  # a viewer: no action


# --- D2-2: the live preview's own sentences --------------------------------------------------


@pytest.mark.parametrize(
    ("params", "sentence"),
    [
        ({"count": "1"}, "Choose 2 to 12 payments."),
        ({"count": "13"}, "Choose 2 to 12 payments."),
        ({"first": "2026-10-05"}, "Choose a first debit date from today on."),
        ({"first": ""}, "Choose a first debit date from today on."),
        ({"first": "2026-02-30"}, "Choose a first debit date from today on."),
        ({"cadence": "biweekly"}, "Choose monthly, weekly or yearly."),
        ({"first": "2027-12-01"}, "Choose a first debit date at most 365 days from today."),
        (
            {"total": "0.05", "count": "12"},
            "The total cannot be split into that many debits of at least one cent.",
        ),
        ({"total": "0"}, invoice_builder.SETTLEMENT_TOTAL),
    ],
)
def test_the_preview_answers_its_own_short_sentences(params: dict[str, str], sentence: str) -> None:
    from urllib.parse import urlencode

    billing, _ = client_for(Role.BILLING)
    query = {"total": "1000.00", "count": "3", "first": "2026-12-01", "cadence": "monthly"}
    query.update(params)
    url = reverse("ledger:invoices:settlement_preview") + "?" + urlencode(query)
    response = billing.get(url)
    assert response.status_code == 400
    assert response.json() == {"ok": False, "message": sentence}
    assert "Nothing was scheduled" not in sentence
    # The form keeps the full sentences.
    assert invoice_builder.SETTLEMENT_COUNT.endswith("Nothing was scheduled.")
