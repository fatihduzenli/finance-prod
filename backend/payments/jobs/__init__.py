"""The database job queue (docs/qa/PLAN-010-worker-and-reconciliation.md D1 to D3).

Web views never call the payment provider. A view that needs it writes its local
change first, as before, then calls :func:`enqueue`; the ``worker`` management command
takes one job at a time with :func:`claim_next` and runs it with :func:`run`, which
calls the existing service function (:mod:`payments.jobs.runners`) and records the
outcome. The page of the entity shows :func:`latest_for` through :func:`describe`.

Delivery is at least once: a job whose worker stopped mid-run (``running`` past
``locked_until``) is queued again once and then marked ``dead``. Every service a job
runs is idempotent (row locks, creation keys, external reference checks), so a second
run changes nothing that the first one completed. A job never retries by itself after
it finished; the manual retry creates a new job.

``params`` holds identifiers and amounts only (amounts as strings, never a float);
``result`` holds the service's fixed sentence, or an error's class and field names.

Besides the staff actions, three reads run here so that no page reaches the provider:
the operator's payment-method list for the enrolment page (``operator_methods_refresh``,
queued by the page's button and after every successful link or sync, see
:func:`refresh_methods`), "Match invoices" on the rent run (``run_match``, entity
:data:`RENT_RUN`) and the webhook event replay (``webhook_replay``). The automatic
methods refresh is not the page state of its operator (:data:`BACKGROUND_KINDS`).
"Run now" on the reconciliation page queues ``reconcile_run`` (entity
:data:`RECONCILIATION`), which the worker runs through
:func:`payments.reconciliation.run_reconciliation`. The scheduled Container Apps job
queues the same kind with ``params.trigger`` ``scheduled`` and no requester
(:func:`queue_scheduled_reconciliation`), so the worker stays the only process that
calls the provider (PLAN-010 D5).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.utils import timezone

from payments.models import Job, JobEntity, JobKind, JobStatus, ReconciliationTrigger

#: How long a claimed job may run before it is considered lost (D1).
LOCK_SECONDS = 600
#: The result stored when a job's worker stopped mid-run.
WORKER_LOST = "WorkerLost"
#: What the page shows for a dead job.
DEAD_TEXT = "Stopped: the worker stopped twice while running this request. Nothing more is tried."
#: The result stored when the outcome could not be stored as it was.
RESULT_NOT_STORED = "ResultNotStored"

#: The entity each kind acts on; :func:`enqueue` refuses any other.
ENTITY_OF_KIND: dict[str, str] = {
    JobKind.OPERATOR_SYNC: JobEntity.OPERATOR,
    JobKind.OPERATOR_RETRY: JobEntity.OPERATOR,
    JobKind.OPERATOR_LINK: JobEntity.OPERATOR,
    JobKind.AUTOPAY_ENABLE: JobEntity.LEASE,
    JobKind.AUTOPAY_PAUSE: JobEntity.LEASE,
    JobKind.AUTOPAY_RESUME: JobEntity.LEASE,
    JobKind.PAYMENT_MARK: JobEntity.RENT_CHARGE,
    JobKind.PAYMENT_RETRY_MARK: JobEntity.RENT_CHARGE,
    JobKind.PAYMENT_REFUND: JobEntity.PAYMENT,
    JobKind.PAYMENT_VOID: JobEntity.PAYMENT,
    JobKind.OPERATOR_METHODS_REFRESH: JobEntity.OPERATOR,
    JobKind.RUN_MATCH: JobEntity.RENT_RUN,
    JobKind.WEBHOOK_REPLAY: JobEntity.WEBHOOK_EVENT,
    JobKind.RECONCILE_RUN: JobEntity.RECONCILIATION,
    JobKind.SETUP_PAYMENT_MARK: JobEntity.SETUP_CHARGE,
    JobKind.SETUP_PAYMENT_RETRY_MARK: JobEntity.SETUP_CHARGE,
}

#: ``app_label.ModelName`` of each entity type.
_ENTITY_OF_MODEL: dict[str, str] = {
    "ledger.Operator": JobEntity.OPERATOR,
    "ledger.Lease": JobEntity.LEASE,
    "ledger.RentCharge": JobEntity.RENT_CHARGE,
    "ledger.SetupCharge": JobEntity.SETUP_CHARGE,
    "ledger.Payment": JobEntity.PAYMENT,
    "payments.WebhookEvent": JobEntity.WEBHOOK_EVENT,
}


@dataclass(frozen=True, slots=True)
class Target:
    """A job entity that is not a table row; ``pk`` is a fixed identifier."""

    entity_type: str
    pk: uuid.UUID


#: The rent run: one entity for every "Match invoices" job, whatever the day.
RENT_RUN = Target(JobEntity.RENT_RUN, uuid.UUID(int=0))
#: The reconciliation: one entity for every "Run now" job (PLAN-010 D10).
RECONCILIATION = Target(JobEntity.RECONCILIATION, uuid.UUID(int=0))
#: The ``params`` key of a ``reconcile_run`` job naming how it started (scheduled or
#: manual); a job without it is manual.
TRIGGER = "trigger"

#: Kinds queued by the worker itself, not by a staff action on the entity's page: they
#: are left out of :func:`latest_for` unless asked for by kind.
BACKGROUND_KINDS: frozenset[str] = frozenset({JobKind.OPERATOR_METHODS_REFRESH})

Entity = models.Model | Target

ACTIVE: tuple[str, ...] = (JobStatus.QUEUED, JobStatus.RUNNING)

logger = logging.getLogger(__name__)


class ReplayKeyUsed(Exception):  # noqa: N818 (reads as a state, like the provider errors)
    """The replay key already belongs to a job of another kind or entity."""


def entity_type_of(entity: Entity) -> str:
    """The job entity type of a model instance or a :class:`Target`; ``ValueError`` for
    any other model."""
    if isinstance(entity, Target):
        if entity.entity_type not in JobEntity.values:
            raise ValueError("Unknown job entity type")
        return entity.entity_type
    label = f"{entity._meta.app_label}.{entity._meta.object_name}"
    try:
        return _ENTITY_OF_MODEL[label]
    except KeyError:
        raise ValueError(
            "Jobs act on operators, leases, rent charges, setup charges, payments, "
            "webhook events, the rent run and the reconciliation only"
        ) from None


def _clean_params(params: Mapping[str, object] | None) -> dict[str, str | bool | int]:
    """Identifiers and amounts only: text, booleans, integers; Decimal and UUID as text."""
    cleaned: dict[str, str | bool | int] = {}
    for name, value in (params or {}).items():
        if isinstance(value, bool | int | str):
            cleaned[str(name)] = value
        elif isinstance(value, Decimal) and value.is_finite():
            cleaned[str(name)] = str(value)
        elif isinstance(value, uuid.UUID):
            cleaned[str(name)] = str(value)
        else:
            raise TypeError(f"Job parameter {name!s} has an unsupported type")
    return cleaned


def enqueue(
    kind: str,
    entity: Entity,
    params: Mapping[str, object] | None,
    requested_by: User | AnonymousUser | None,
    replay_key: uuid.UUID | None = None,
) -> Job:
    """Queue one job for ``entity``; with ``replay_key``, return the job it already made.

    Raises :class:`ReplayKeyUsed` when the key belongs to a job of another kind or
    entity, and ``ValueError`` when ``kind`` does not act on this entity.
    """
    job_kind = JobKind(kind)
    entity_type = entity_type_of(entity)
    if ENTITY_OF_KIND[job_kind] != entity_type:
        raise ValueError("This job kind does not act on this entity")
    if replay_key is not None:
        existing = _replayed(replay_key, job_kind, entity_type, entity.pk)
        if existing is not None:
            return existing
    job = Job(
        kind=job_kind,
        entity_type=entity_type,
        entity_id=entity.pk,
        params=_clean_params(params),
        requested_by=requested_by if isinstance(requested_by, User) else None,
        replay_key=replay_key,
    )
    try:
        with transaction.atomic():
            job.save()
    except IntegrityError:
        if replay_key is None:
            raise
        existing = _replayed(replay_key, job_kind, entity_type, entity.pk)
        if existing is None:
            raise
        return existing
    return job


def _replayed(replay_key: uuid.UUID, kind: str, entity_type: str, entity_id: object) -> Job | None:
    existing = Job.objects.filter(replay_key=replay_key).first()
    if existing is None:
        return None
    if (existing.kind, existing.entity_type, existing.entity_id) != (kind, entity_type, entity_id):
        raise ReplayKeyUsed
    return existing


def _skip_locked() -> bool:
    return connection.vendor == "postgresql"


def requeue_lost(*, now: datetime | None = None) -> int:
    """Queue again, once, each ``running`` job past ``locked_until``; then mark it dead."""
    moment = now or timezone.now()
    changed = 0
    with transaction.atomic():
        lost = list(
            Job.objects.select_for_update(skip_locked=_skip_locked())
            .filter(status=JobStatus.RUNNING, locked_until__lt=moment)
            .order_by("created_at", "id")
        )
        for job in lost:
            if job.attempts <= job.max_attempts:
                job.status = JobStatus.QUEUED
            else:
                job.status = JobStatus.DEAD
                job.finished_at = moment
            job.locked_until = None
            job.result = WORKER_LOST
            job.save(update_fields=["status", "finished_at", "locked_until", "result"])
            logger.warning("Job %s lost its worker: %s", job.kind, job.status)
            changed += 1
    return changed


def claim_next(*, now: datetime | None = None) -> Job | None:
    """Take the oldest queued job and mark it ``running``; ``None`` when there is none.

    PostgreSQL: ``SELECT ... FOR UPDATE SKIP LOCKED``, so two workers never take one job.
    sqlite (local only) has no row locks; the conditional update below still lets only
    one caller move the row from ``queued`` to ``running``.
    """
    moment = now or timezone.now()
    requeue_lost(now=moment)
    with transaction.atomic():
        job = (
            Job.objects.select_for_update(skip_locked=_skip_locked())
            .filter(status=JobStatus.QUEUED)
            .order_by("created_at", "id")
            .first()
        )
        if job is None:
            return None
        locked_until = moment + timedelta(seconds=LOCK_SECONDS)
        taken = Job.objects.filter(pk=job.pk, status=JobStatus.QUEUED).update(
            status=JobStatus.RUNNING,
            attempts=job.attempts + 1,
            started_at=moment,
            finished_at=None,
            locked_until=locked_until,
        )
        if taken != 1:
            return None
    job.refresh_from_db()
    return job


def run(job: Job) -> Job:
    """Run a claimed job through its service and record the outcome; never raises
    for a failure of the service."""
    from payments.jobs.runners import RUNNERS
    from payments.webhooks.processor import describe_error

    try:
        ok, text = RUNNERS[job.kind](job)
    except Exception as error:  # any failure is recorded as class and field names
        status, result = JobStatus.FAILED, describe_error(error)
        logger.warning("Job %s raised %s", job.kind, result)
    else:
        status, result = (JobStatus.DONE if ok else JobStatus.FAILED), text
    return finish(job, status, result)


def finish(job: Job, status: str, result: str, *, now: datetime | None = None) -> Job:
    """Store the outcome of a running job."""
    job.status = status
    job.result = result
    job.finished_at = now or timezone.now()
    job.locked_until = None
    try:
        job.check_payment_data()
    except ValidationError:
        job.result = RESULT_NOT_STORED
    job.save(update_fields=["status", "result", "finished_at", "locked_until"])
    return job


def latest_for(entity: Entity, kinds: Iterable[str] | None = None) -> Job | None:
    """The newest job of this entity, of ``kinds`` or else of any staff-action kind
    (not :data:`BACKGROUND_KINDS`); for the entity's page."""
    found = Job.objects.filter(entity_type=entity_type_of(entity), entity_id=entity.pk)
    if kinds is None:
        found = found.exclude(kind__in=BACKGROUND_KINDS)
    else:
        found = found.filter(kind__in=list(kinds))
    return found.order_by("-created_at", "-id").first()


def refresh_methods(operator: models.Model, requested_by: User | AnonymousUser | None) -> Job:
    """Queue a read of the operator's payment methods, unless one is already waiting."""
    waiting = latest_for(operator, [JobKind.OPERATOR_METHODS_REFRESH])
    if waiting is not None and is_active(waiting):
        return waiting
    return enqueue(JobKind.OPERATOR_METHODS_REFRESH, operator, None, requested_by)


def is_active(job: Job | None) -> bool:
    return job is not None and job.status in ACTIVE


def describe(job: Job) -> str:
    """What the page shows: "Queued", "Running", "Done: <outcome>", "Failed: <outcome>"."""
    if job.status == JobStatus.QUEUED:
        return "Queued"
    if job.status == JobStatus.RUNNING:
        return "Running"
    if job.status == JobStatus.DEAD:
        return DEAD_TEXT
    label = "Done" if job.status == JobStatus.DONE else "Failed"
    return f"{label}: {job.result}" if job.result else label


def queue_scheduled_reconciliation() -> Job:
    """Queue the nightly ``reconcile_run`` job for the worker: trigger ``scheduled``, no
    requester (PLAN-010 D5)."""
    return enqueue(
        JobKind.RECONCILE_RUN,
        RECONCILIATION,
        {TRIGGER: ReconciliationTrigger.SCHEDULED.value},
        None,
    )


def scheduled_reconciliations_waiting(since: datetime) -> list[datetime]:
    """When each queued or running scheduled ``reconcile_run`` job created at or after
    ``since`` was created."""
    return list(
        Job.objects.filter(
            kind=JobKind.RECONCILE_RUN,
            status__in=ACTIVE,
            created_at__gte=since,
            params__trigger=ReconciliationTrigger.SCHEDULED.value,
        ).values_list("created_at", flat=True)
    )
