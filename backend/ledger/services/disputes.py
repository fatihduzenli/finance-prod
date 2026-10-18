"""The dispute list and its evidence (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md
D8, D9).

Read only towards the money: nothing here changes a charge or a payment. The dispute
webhooks keep flagging the disputed payment's charge for review
(:mod:`payments.webhooks.handlers.dispute`); this module keeps the provider's list.

- :func:`sync` (the worker's ``disputes_sync`` job): reads the provider's disputes created
  in the last :data:`SYNC_DAYS` days, page after page (until a page brings nothing new,
  at most :data:`MAX_PAGES`), and upserts one :class:`~ledger.models.Dispute` per
  provider identifier (held in ``external_refs``, kind ``dispute``). Status and reason
  are kept verbatim; the payment is linked when we hold the disputed transaction, and the
  operator and store are derived from it. The time of the last successful read is the
  ``sync_state`` row :data:`SYNC_JOB`; a failed read changes no row, so the page keeps the
  last fetch with its time. Audited ``dispute.synced`` with counts, or
  ``dispute.sync_failed`` with the error's class name.
- :func:`refresh` (billing; refused while a sync is queued or running), the daily tick
  (:func:`queue_daily`, once a day) and the dispute webhooks (:func:`queue_sync`) queue
  that job; nothing here calls the provider from a web request.
- :func:`add_evidence` (admin): refuses, storing nothing, a name that is empty, too long
  or holds payment data, a dispute without a known operator or past its respond-by
  time, a file of :data:`~payments.provider.EVIDENCE_MAX_BYTES` bytes or more, a type
  other than PDF, JPEG, PNG or TIFF, and an image without the notice acknowledged
  (:data:`NOTICE`). Then the file is stored through
  :func:`ledger.services.documents.upload` on the dispute's operator, kind
  ``dispute_evidence``, where the 10c checks run unchanged (the PDF text through the
  payment-data guard), and a :class:`~ledger.models.DisputeEvidence` row (``queued``)
  and one ``dispute_evidence_upload`` job are written together.
- :func:`upload_evidence` (the worker): reads the stored bytes back through the documents
  storage (checked against the stored size and SHA-256) and sends them; on success the
  row is ``sent`` with the provider's file identifier (``external_refs``, kind
  ``dispute_file``) and a ``disputes_sync`` is queued to re-read the dispute's status;
  on failure it is ``failed`` with the error's class name, and the job's result says
  why (the provider's sentence when the respond-by date has passed). A job never runs
  twice by itself; :func:`retry_evidence` (admin) queues a new one for a failed row.

Every write is audited (entity ``dispute`` or ``dispute_evidence``). Provider identifiers
appear masked only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone

import payments
from accounts.roles import Role, meets, role_of
from documents_storage import StorageUnavailable
from ledger.models import (
    EVIDENCE_NAME_MAX_LENGTH,
    AuditLog,
    Dispute,
    DisputeEvidence,
    DisputeEvidenceStatus,
    DocumentKind,
    Payment,
    SyncFailure,
)
from ledger.services import documents, returns
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from ledger.wording import provider_sentence
from payments import jobs
from payments.errors import ProviderError, ProviderRejected
from payments.models import Job, JobKind, SyncState
from payments.provider import (
    EVIDENCE_EXTENSIONS,
    EVIDENCE_MAX_BYTES,
    DateRange,
    DisputeQuery,
    DisputeRecord,
    PaymentProvider,
    mask_identifier,
)
from payments.webhooks.handlers.common import (
    ENTITY_PAYMENT,
    KIND_TRANSACTION,
    add_ref,
    current_ref,
    entity_for,
    json_summary,
)

ENTITY_DISPUTE = "dispute"
KIND_DISPUTE = "dispute"
ENTITY_EVIDENCE = "dispute_evidence"
KIND_DISPUTE_FILE = "dispute_file"
#: The ``sync_state`` row holding the time of the last successful read.
SYNC_JOB = "disputes_sync"
#: How far back the list is read, by creation date.
SYNC_DAYS = 90
#: Most pages read in one sync.
MAX_PAGES = 50
#: The ``params`` key of a ``disputes_sync`` job naming what queued it.
TRIGGER = "trigger"
TRIGGER_MANUAL = "manual"
TRIGGER_DAILY = "daily"
TRIGGER_WEBHOOK = "webhook"
TRIGGER_EVIDENCE = "evidence"
#: Days left under which the page highlights a dispute (D8).
HIGHLIGHT_DAYS = 7
#: The masked transaction identifier in ``raw_summary``.
SUMMARY_TRANSACTION = "transaction"
#: The documented, non-identifier fields ``raw_summary`` keeps whatever the adapter sends
#: (Rule 1, Rule 8); the same list the provider mapping keeps. Every other key is dropped.
SUMMARY_KEYS: tuple[str, ...] = (
    "reason",
    "amount",
    "status",
    "respond_by",
    "created_at",
    "updated_at",
)

ACTION_SYNCED = "dispute.synced"
ACTION_SYNC_FAILED = "dispute.sync_failed"
ACTION_REFRESH = "dispute.refresh_requested"
ACTION_EVIDENCE_ADDED = "dispute.evidence_added"
ACTION_EVIDENCE_REFUSED = "dispute.evidence_refused"
ACTION_EVIDENCE_SENT = "dispute.evidence_sent"
ACTION_EVIDENCE_FAILED = "dispute.evidence_failed"
ACTION_EVIDENCE_RETRY = "dispute.evidence_retry"

#: The notice an image needs acknowledged (D8): an image cannot be checked for numbers.
NOTICE = "This is a receipt, lease page or correspondence, not a bank form"

REFRESH_QUEUED = provider_sentence("Updating from {provider}; the list refreshes when it answers.")
BUSY = provider_sentence("An update from {provider} is already in progress.")
EVIDENCE_QUEUED = provider_sentence("Evidence stored. It is being sent to {provider}.")
NOT_BILLING = "Only billing staff may refresh the dispute list."
NOT_ADMIN = "Only an admin may add or resend dispute evidence."
NAME_REQUIRED = "Give the evidence a name."
NAME_TOO_LONG = f"The name may be at most {EVIDENCE_NAME_MAX_LENGTH} characters."
NO_OPERATOR = (
    "This dispute is not linked to one of our operators, so the evidence has nowhere to be "
    "kept. Nothing was saved."
)
PAST_RESPOND_BY = provider_sentence(
    "The respond-by date of this dispute has passed: {provider} no longer takes evidence. "
    "Nothing was saved."
)
TOO_LARGE = "Dispute evidence must be smaller than 10 MB. Nothing was saved."
NOTICE_REQUIRED = (
    "An image cannot be checked for account numbers. Check the box to confirm it is a "
    "receipt, lease page or correspondence, not a bank form. Nothing was saved."
)
NOT_FAILED = "Only evidence that failed to send can be sent again."
NO_PROVIDER_DISPUTE = provider_sentence("This dispute is not in {provider}.")
SENT = provider_sentence("Evidence sent to {provider}.")
ALREADY_SENT = "This evidence was already sent."
REFUSED = provider_sentence("{provider} refused the evidence: {error}.")
FAILED = "The evidence could not be sent ({error}). Use Send again to retry."
READ_FAILED = "The stored file could not be read back ({error}). Use Send again to retry."
SYNC_DONE = (
    "Disputes read: {read}. New: {created}. Changed: {changed}. Linked to a payment: {linked}."
)
SYNC_MORE = " More disputes than fit in one update; the rest come next time."
SYNC_FAILED = "The disputes could not be read ({error}). The list shows the last read."

Actor = User | AnonymousUser | None


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    forbidden: bool = False
    evidence: DisputeEvidence | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    ok: bool
    message: str
    read: int = 0
    created: int = 0
    changed: int = 0
    linked: int = 0


def _user(actor: Actor) -> User | None:
    return actor if isinstance(actor, User) else None


def _meets(actor: Actor, role: Role) -> bool:
    user = _user(actor)
    return user is not None and meets(role_of(user), role)


def _audit(
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: object,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=_user(actor),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )


# --- Reading the list -------------------------------------------------------------------


def last_synced() -> datetime | None:
    """When the list was last read successfully, or ``None``."""
    return (
        SyncState.objects.filter(job_name=SYNC_JOB)
        .values_list("last_success_at", flat=True)
        .first()
    )


def ordered() -> QuerySet[Dispute]:
    """Every dispute by respond-by time, the ones without one last."""
    return Dispute.objects.select_related("operator", "store", "payment").order_by(
        F("respond_by").asc(nulls_last=True), "provider_created_at", "id"
    )


def days_left(dispute: Dispute, today: date) -> int | None:
    """Whole days from ``today`` to the respond-by date (New York); negative once past."""
    if dispute.respond_by is None:
        return None
    return (timezone.localdate(dispute.respond_by) - today).days


def is_past(dispute: Dispute, now: datetime | None = None) -> bool:
    """Whether the respond-by time has passed: the provider takes no more evidence."""
    return dispute.respond_by is not None and dispute.respond_by <= (now or timezone.now())


def masked_transaction(dispute: Dispute) -> str:
    value = dispute.raw_summary.get(SUMMARY_TRANSACTION) if dispute.raw_summary else None
    return value if isinstance(value, str) else ""


# --- Sync (the worker) -------------------------------------------------------------------


def _read_all(provider: PaymentProvider, today: date) -> tuple[list[DisputeRecord], bool]:
    """Every dispute of the window, page after page; and whether pages were left."""
    window = DateRange(today - timedelta(days=SYNC_DAYS), today)
    found: dict[str, DisputeRecord] = {}
    for page in range(1, MAX_PAGES + 1):
        fresh = [
            record
            for record in provider.list_disputes(DisputeQuery(window, page=page))
            if record.external_id not in found
        ]
        if not fresh:
            return list(found.values()), False
        for record in fresh:
            found[record.external_id] = record
    return list(found.values()), True


def _payment_for(record: DisputeRecord) -> Payment | None:
    ref = entity_for(KIND_TRANSACTION, record.transaction_external_id)
    if ref is None or ref.entity_type != ENTITY_PAYMENT:
        return None
    return (
        Payment.objects.select_related(
            "invoice__operator",
            "rent_charge__lease__store",
            "rent_charge__lease__operator",
            "insurance_charge__lease__store",
            "insurance_charge__lease__operator",
            "one_off_charge__lease__store",
            "one_off_charge__lease__operator",
            "one_off_charge__operator",
        )
        .filter(pk=ref.entity_id)
        .first()
    )


def _summary(record: DisputeRecord) -> dict[str, Any]:
    raw = record.raw_summary or {}
    summary = json_summary({key: raw[key] for key in SUMMARY_KEYS if key in raw})
    if record.transaction_external_id:
        summary[SUMMARY_TRANSACTION] = mask_identifier(record.transaction_external_id)
    return summary


def _fields(record: DisputeRecord) -> dict[str, Any]:
    payment = _payment_for(record)
    place = returns.row(payment) if payment is not None else None
    return {
        "payment": payment,
        "operator": place.operator if place is not None else None,
        "store": place.store if place is not None else None,
        "reason": record.reason[:64],
        "amount": record.amount.quantize(Decimal("0.01")) if record.amount is not None else None,
        "status": record.raw_status[:64],
        "respond_by": record.respond_by,
        "provider_created_at": record.created_at,
        "changed_at": record.updated_at,
        "raw_summary": _summary(record),
    }


def _state(dispute: Dispute) -> dict[str, Any]:
    return {
        "status": dispute.status,
        "reason": dispute.reason,
        "amount": str(dispute.amount) if dispute.amount is not None else None,
        "respond_by": dispute.respond_by.isoformat() if dispute.respond_by else None,
        "payment": str(dispute.payment_id) if dispute.payment_id else None,
        "operator": str(dispute.operator_id) if dispute.operator_id else None,
        "store": str(dispute.store_id) if dispute.store_id else None,
    }


def _upsert(record: DisputeRecord, fetched_at: datetime) -> tuple[bool, bool, bool]:
    """Write one record; (created, changed, linked to our payment). The caller holds
    the transaction."""
    fields = _fields(record)
    linked = fields["payment"] is not None
    ref = entity_for(KIND_DISPUTE, record.external_id)
    if ref is None:
        dispute = Dispute(fetched_at=fetched_at, **fields)
        dispute.check_payment_data()
        dispute.save(force_insert=True)
        add_ref(ENTITY_DISPUTE, dispute.pk, KIND_DISPUTE, record.external_id)
        return True, False, linked
    dispute = Dispute.objects.select_for_update().get(pk=ref.entity_id)
    before = _state(dispute)
    for name, value in fields.items():
        setattr(dispute, name, value)
    dispute.fetched_at = fetched_at
    dispute.check_payment_data()
    dispute.save()
    return False, _state(dispute) != before, linked


def sync(*, provider: PaymentProvider | None = None, now: datetime | None = None) -> SyncResult:
    """Read the provider's disputes and upsert them (the ``disputes_sync`` job)."""
    moment = now or timezone.now()
    try:
        records, more = _read_all(provider or payments.get_provider(), timezone.localdate(moment))
    except ProviderError as error:
        name = type(error).__name__
        _audit(None, ACTION_SYNC_FAILED, ENTITY_DISPUTE, uuid.UUID(int=0), None, {"error": name})
        return SyncResult(False, SYNC_FAILED.format(error=name))
    created = changed = linked = 0
    with transaction.atomic():
        for record in records:
            new, moved, ours = _upsert(record, moment)
            created += new
            changed += moved
            linked += ours
        SyncState.objects.update_or_create(job_name=SYNC_JOB, defaults={"last_success_at": moment})
        counts = {
            "read": len(records),
            "created": created,
            "changed": changed,
            "linked": linked,
            "more_pages": more,
        }
        _audit(None, ACTION_SYNCED, ENTITY_DISPUTE, uuid.UUID(int=0), None, counts)
    message = SYNC_DONE.format(read=len(records), created=created, changed=changed, linked=linked)
    return SyncResult(
        True,
        message + (SYNC_MORE if more else ""),
        read=len(records),
        created=created,
        changed=changed,
        linked=linked,
    )


# --- Queueing the sync ----------------------------------------------------------------


def sync_job() -> Job | None:
    """The latest ``disputes_sync`` job, for the page."""
    return jobs.latest_for(jobs.DISPUTES, [JobKind.DISPUTES_SYNC])


def queue_sync(actor: Actor, trigger: str) -> Job | None:
    """Queue a ``disputes_sync`` job unless one is already queued or running; the new job,
    or ``None``."""
    if jobs.is_active(sync_job()):
        return None
    return jobs.enqueue(JobKind.DISPUTES_SYNC, jobs.DISPUTES, {TRIGGER: trigger}, actor)


def refresh(actor: Actor) -> Outcome:
    """**Refresh** on the page (billing)."""
    if not _meets(actor, Role.BILLING):
        return Outcome(False, NOT_BILLING, forbidden=True)
    job = queue_sync(actor, TRIGGER_MANUAL)
    if job is None:
        return Outcome(False, BUSY)
    _audit(actor, ACTION_REFRESH, ENTITY_DISPUTE, uuid.UUID(int=0), None, {"job": str(job.pk)})
    return Outcome(True, REFRESH_QUEUED)


def queue_daily(today: date) -> bool:
    """The daily tick's sync: queued once a day (a second run the same day, or a sync
    already waiting, queues nothing)."""
    start = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    already = Job.objects.filter(
        kind=JobKind.DISPUTES_SYNC, created_at__gte=start, params__trigger=TRIGGER_DAILY
    ).exists()
    return not already and queue_sync(None, TRIGGER_DAILY) is not None


# --- Evidence ------------------------------------------------------------------------------


def _refused(actor: Actor, dispute: Dispute, code: str, message: str) -> Outcome:
    _audit(actor, ACTION_EVIDENCE_REFUSED, ENTITY_DISPUTE, dispute.pk, None, {"reason": code})
    return Outcome(False, message)


def _name_refusal(name: str) -> str:
    if not name:
        return NAME_REQUIRED
    if len(name) > EVIDENCE_NAME_MAX_LENGTH:
        return NAME_TOO_LONG
    try:
        validate_no_payment_data(name)
    except ValidationError:
        return PAYMENT_DATA_MESSAGE
    return ""


def add_evidence(
    dispute: Dispute,
    uploaded_file: UploadedFile[bytes],
    name: str,
    actor: Actor,
    *,
    acknowledged: bool = False,
    now: datetime | None = None,
) -> Outcome:
    """Store one evidence file and queue its upload (admin); see the module docstring."""
    user = _user(actor)
    if user is None or not meets(role_of(user), Role.ADMIN):
        return Outcome(False, NOT_ADMIN, forbidden=True)
    text = visible_text(name or "")
    refusal = _name_refusal(text)
    if refusal:
        return _refused(actor, dispute, "name", refusal)
    if dispute.operator is None:
        return _refused(actor, dispute, "no_operator", NO_OPERATOR)
    if is_past(dispute, now):
        return _refused(actor, dispute, "past_respond_by", PAST_RESPOND_BY)
    if int(uploaded_file.size or 0) >= EVIDENCE_MAX_BYTES:
        return _refused(actor, dispute, "too_large", TOO_LARGE)
    uploaded_file.seek(0)
    data = uploaded_file.read(EVIDENCE_MAX_BYTES)
    if len(data) >= EVIDENCE_MAX_BYTES:
        return _refused(actor, dispute, "too_large", TOO_LARGE)
    sniffed = documents.sniff(data, documents.sanitise_filename(str(uploaded_file.name or "")))
    if isinstance(sniffed, documents.Refusal) and sniffed.code == documents.TYPE_NOT_ACCEPTED:
        return _refused(
            actor,
            dispute,
            documents.EVIDENCE_TYPE_NOT_ACCEPTED,
            documents.MESSAGES[documents.EVIDENCE_TYPE_NOT_ACCEPTED],
        )
    if isinstance(sniffed, documents.Sniffed):
        if sniffed.kind not in documents.EVIDENCE_TYPES:
            return _refused(
                actor,
                dispute,
                documents.EVIDENCE_TYPE_NOT_ACCEPTED,
                documents.MESSAGES[documents.EVIDENCE_TYPE_NOT_ACCEPTED],
            )
        if sniffed.kind in documents.IMAGE_TYPES and acknowledged is not True:
            return _refused(actor, dispute, "notice_required", NOTICE_REQUIRED)
    del data
    # Any other refusal (an unreadable type, payment data in a PDF, a duplicate, the
    # storage) is the documents upload's own, audited there.
    stored = documents.upload(
        dispute.operator, DocumentKind.DISPUTE_EVIDENCE, uploaded_file, "", user
    )
    if not stored.ok or stored.document is None:
        return Outcome(False, stored.message)
    if stored.document.content_type not in EVIDENCE_EXTENSIONS:  # pragma: no cover
        raise AssertionError("The documents upload stored a type evidence may not be")
    with transaction.atomic():
        evidence = DisputeEvidence(
            dispute=dispute, document=stored.document, name=text, requested_by=user
        )
        evidence.check_payment_data()
        evidence.save(force_insert=True)
        job = jobs.enqueue(JobKind.DISPUTE_EVIDENCE_UPLOAD, evidence, None, user)
        _audit(
            actor,
            ACTION_EVIDENCE_ADDED,
            ENTITY_EVIDENCE,
            evidence.pk,
            None,
            {
                "dispute": str(dispute.pk),
                "document": str(stored.document.pk),
                "content_type": stored.document.content_type,
                "size_bytes": stored.document.size_bytes,
                "image_notice": acknowledged is True,
                "job": str(job.pk),
            },
        )
    return Outcome(True, EVIDENCE_QUEUED, evidence=evidence)


def retry_evidence(
    evidence: DisputeEvidence, actor: Actor, *, now: datetime | None = None
) -> Outcome:
    """**Send again** for evidence that failed (admin): a new job, as every retry."""
    user = _user(actor)
    if user is None or not meets(role_of(user), Role.ADMIN):
        return Outcome(False, NOT_ADMIN, forbidden=True)
    with transaction.atomic():
        row = (
            DisputeEvidence.objects.select_for_update()
            .select_related("dispute")
            .get(pk=evidence.pk)
        )
        if row.status != DisputeEvidenceStatus.FAILED:
            return Outcome(False, NOT_FAILED)
        if is_past(row.dispute, now):
            return Outcome(False, PAST_RESPOND_BY)
        row.status = DisputeEvidenceStatus.QUEUED
        row.error = ""
        row.save(update_fields=["status", "error", "updated_at"])
        job = jobs.enqueue(JobKind.DISPUTE_EVIDENCE_UPLOAD, row, None, user)
        _audit(
            actor,
            ACTION_EVIDENCE_RETRY,
            ENTITY_EVIDENCE,
            row.pk,
            {"status": DisputeEvidenceStatus.FAILED},
            {"status": DisputeEvidenceStatus.QUEUED, "job": str(job.pk)},
        )
    return Outcome(True, EVIDENCE_QUEUED, evidence=row)


def _fail(row: DisputeEvidence, actor: Actor, error: str, text: str) -> tuple[bool, str]:
    row.status = DisputeEvidenceStatus.FAILED
    row.error = error
    row.save(update_fields=["status", "error", "updated_at"])
    _audit(
        actor,
        ACTION_EVIDENCE_FAILED,
        ENTITY_EVIDENCE,
        row.pk,
        {"status": DisputeEvidenceStatus.QUEUED},
        {"status": DisputeEvidenceStatus.FAILED, "error": error},
    )
    return False, text


def upload_evidence(
    evidence_id: uuid.UUID, actor: Actor, *, provider: PaymentProvider | None = None
) -> tuple[bool, str]:
    """Send one queued evidence file (the ``dispute_evidence_upload`` job)."""
    with transaction.atomic():
        row = (
            DisputeEvidence.objects.select_for_update()
            .select_related("dispute", "document")
            .get(pk=evidence_id)
        )
        if row.status == DisputeEvidenceStatus.SENT:
            return True, ALREADY_SENT
        dispute_ref = current_ref(ENTITY_DISPUTE, row.dispute_id, KIND_DISPUTE)
        if not dispute_ref:
            return _fail(row, actor, "NoProviderDispute", NO_PROVIDER_DISPUTE)
        reader = _user(actor) or row.requested_by
        if reader is None:
            return _fail(row, actor, "NoReader", READ_FAILED.format(error="NoReader"))
        try:
            data = documents.read_all(row.document, reader)
        except (StorageUnavailable, PermissionDenied) as error:
            name = type(error).__name__
            return _fail(row, actor, name, READ_FAILED.format(error=name))
        try:
            receipt = (provider or payments.get_provider()).upload_dispute_evidence(
                dispute_ref, row.name, data, row.document.content_type
            )
        except ProviderRejected as error:
            name = type(error).__name__
            return _fail(
                row,
                actor,
                name,
                error.reason or REFUSED.format(error=dict(SyncFailure.choices).get(name, name)),
            )
        except ProviderError as error:
            name = type(error).__name__
            return _fail(row, actor, name, FAILED.format(error=name))
        row.status = DisputeEvidenceStatus.SENT
        row.sent_at = receipt.fetched_at
        row.error = ""
        row.save(update_fields=["status", "sent_at", "error", "updated_at"])
        if receipt.external_id and not current_ref(ENTITY_EVIDENCE, row.pk, KIND_DISPUTE_FILE):
            add_ref(ENTITY_EVIDENCE, row.pk, KIND_DISPUTE_FILE, receipt.external_id)
        _audit(
            actor,
            ACTION_EVIDENCE_SENT,
            ENTITY_EVIDENCE,
            row.pk,
            {"status": DisputeEvidenceStatus.QUEUED},
            {
                "status": DisputeEvidenceStatus.SENT,
                "provider_file": mask_identifier(receipt.external_id),
            },
        )
        # The dispute's status is re-read from the provider's list.
        queue_sync(actor, TRIGGER_EVIDENCE)
    return True, SENT


def evidence_job(evidence: DisputeEvidence) -> Job | None:
    """The latest upload job of one evidence row, for the page."""
    return jobs.latest_for(evidence, [JobKind.DISPUTE_EVIDENCE_UPLOAD])
