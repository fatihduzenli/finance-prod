"""Provider-side tables (docs/qa/PLAN-004-schema.md §4.2, docs/CLAUDE.md Rule 8).

``ExternalRef`` is the only place a provider identifier of a business entity is stored:
business tables in ``ledger`` carry none, so a second provider needs no migration.
Deposits and webhook events are provider records themselves and keep their provider
identifiers (PLAN-004 decision D12).
"""

from __future__ import annotations

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q

from ledger.models import (
    HALF_CENT,
    GuardedModel,
    MoneyField,
    free_text,
    guarded_char,
    guarded_json,
)

PROVIDER_MAX_LENGTH = 32
EXTERNAL_ID_MAX_LENGTH = 255


class ExternalRef(GuardedModel):
    entity_type = guarded_char(64)
    #: Generic reference, no foreign key; checked by reconciliation (PLAN-004 §7).
    entity_id = models.UUIDField()
    provider = guarded_char(PROVIDER_MAX_LENGTH)
    external_kind = guarded_char(64)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    external_id = models.CharField(max_length=EXTERNAL_ID_MAX_LENGTH)
    created_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "external_refs"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_kind", "external_id"],
                name="external_refs_external_id_unique",
            ),
            models.UniqueConstraint(
                fields=["entity_type", "entity_id", "provider", "external_kind"],
                condition=Q(superseded_at__isnull=True),
                name="external_refs_one_current_ref",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.external_kind} for {self.entity_type} {self.entity_id}"


class ProviderSnapshot(GuardedModel):
    """A trimmed copy of a provider object, as fetched (docs/CLAUDE.md Rule 1)."""

    external_ref = models.ForeignKey(
        ExternalRef, on_delete=models.CASCADE, related_name="snapshots"
    )
    payload = guarded_json()
    fetched_at = models.DateTimeField()

    class Meta:
        db_table = "provider_snapshots"
        indexes = [
            models.Index(
                fields=["external_ref", "fetched_at"], name="provider_snapshots_fetched_idx"
            )
        ]


class Deposit(GuardedModel):
    provider = guarded_char(PROVIDER_MAX_LENGTH)
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    provider_deposit_id = models.CharField(max_length=EXTERNAL_ID_MAX_LENGTH)
    deposit_date = models.DateField()
    gross = MoneyField()
    fees = MoneyField()
    net = MoneyField()
    status = guarded_char(64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "deposits"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_deposit_id"], name="deposits_provider_id_unique"
            ),
            models.CheckConstraint(
                condition=Q(net__gt=F("gross") - F("fees") - HALF_CENT)
                & Q(net__lt=F("gross") - F("fees") + HALF_CENT),
                name="deposits_net_is_gross_minus_fees",
            ),
        ]


class WebhookEventSource(models.TextChoices):
    """Where the receiver found the event name (docs/qa/PLAN-008-webhooks.md D3)."""

    HEADER = "header", "Header"
    BODY = "body", "Body"
    SHAPE = "shape", "Object shape"
    UNKNOWN = "unknown", "Unknown"


class WebhookObjectIdSource(models.TextChoices):
    """Where the receiver found the object ID (docs/qa/PLAN-008a §2 rule 1); empty when
    no plain identifier was found."""

    NONE = "", "None"
    ID = "id", "id"
    MERGE_VARS_INVOICE_ID = "mergeVars.invoice.id", "mergeVars.invoice.id"
    INVOICE_ID = "invoice.id", "invoice.id"
    TRANSACTION_ID = "transaction.id", "transaction.id"
    DATA_ID = "data.id", "data.id"
    BATCH_ID = "batch_id", "batch_id"


class WebhookOutcome(models.TextChoices):
    """Values of ``WebhookEvent.outcome`` (PLAN-008 D3).

    Not attached to the column: an ignored event is stored as ``"ignored: <reason>"``.
    A duplicate delivery creates no row; it increments ``duplicate_count``.
    """

    RECEIVED = "received", "Received"
    PROCESSED = "processed", "Processed"
    IGNORED = "ignored", "Ignored"
    FAILED = "failed", "Failed"
    DEAD_LETTER = "dead_letter", "Dead letter"


class WebhookEvent(GuardedModel):
    received_at = models.DateTimeField(auto_now_add=True)
    event_type = guarded_char(100)
    event_source = models.CharField(
        max_length=16, choices=WebhookEventSource.choices, default=WebhookEventSource.UNKNOWN
    )
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    provider_object_id = models.CharField(max_length=EXTERNAL_ID_MAX_LENGTH)
    #: The body path the object ID was read from (PLAN-008a §2 rule 1).
    object_id_source = models.CharField(
        max_length=32,
        choices=WebhookObjectIdSource.choices,
        default=WebhookObjectIdSource.NONE,
        blank=True,
    )
    #: An identifier, not free text: outside the card-number guard (PLAN-004 §4.6 point 1).
    dedupe_hash = models.CharField(max_length=64, unique=True)
    #: The body as received, redacted per Rule 1 (``payments.redaction.redact``); null
    #: when the body was not a JSON value or still matched the guard after redaction.
    #: ``Decimal`` numbers are stored as strings, never as binary floating point.
    payload = guarded_json(null=True, blank=True, encoder=DjangoJSONEncoder)
    processed_at = models.DateTimeField(null=True, blank=True)
    #: ``processed: unlinked (n candidates)`` (PLAN-008a §2 rule 3) needs more than 32.
    outcome = guarded_char(64, blank=True)
    error = free_text()
    #: Failed processing attempts; the third moves the event to dead letter.
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    #: Byte-identical redeliveries received after the first (PLAN-008 D7).
    duplicate_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "webhook_events"
        constraints = [
            models.CheckConstraint(
                condition=Q(event_source__in=WebhookEventSource.values),
                name="webhook_events_event_source_valid",
            ),
            models.CheckConstraint(
                condition=Q(object_id_source__in=WebhookObjectIdSource.values),
                name="webhook_events_object_id_source_valid",
            ),
        ]


class SyncState(GuardedModel):
    job_name = guarded_char(100, unique=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    cursor = free_text()

    class Meta:
        db_table = "sync_state"


class JobKind(models.TextChoices):
    """What a job does; a closed list (docs/qa/PLAN-010 D1). Each maps to one service."""

    OPERATOR_SYNC = "operator_sync", "Operator sync"
    OPERATOR_RETRY = "operator_retry", "Operator sync retry"
    OPERATOR_LINK = "operator_link", "Operator link"
    AUTOPAY_ENABLE = "autopay_enable", "Enable autopay"
    AUTOPAY_PAUSE = "autopay_pause", "Pause autopay"
    AUTOPAY_RESUME = "autopay_resume", "Resume autopay"
    PAYMENT_MARK = "payment_mark", "Mark the provider invoice paid"
    PAYMENT_RETRY_MARK = "payment_retry_mark", "Retry marking the provider invoice"
    PAYMENT_REFUND = "payment_refund", "Refund"
    PAYMENT_VOID = "payment_void", "Void"
    #: PLAN-010 Batch A follow-up: the reads the enrolment, run and event pages needed.
    OPERATOR_METHODS_REFRESH = "operator_methods_refresh", "Refresh payment methods"
    RUN_MATCH = "run_match", "Match invoices"
    WEBHOOK_REPLAY = "webhook_replay", "Replay webhook event"
    #: PLAN-010 Batch B: "Run now" on the reconciliation page.
    RECONCILE_RUN = "reconcile_run", "Run reconciliation"


class JobEntity(models.TextChoices):
    """The kind of row a job acts on; its page shows the job's state."""

    OPERATOR = "operator", "Operator"
    LEASE = "lease", "Lease"
    RENT_CHARGE = "rent_charge", "Rent charge"
    PAYMENT = "payment", "Payment"
    #: Not a row: the rent run, one fixed entity ID (``payments.jobs.RENT_RUN``).
    RENT_RUN = "rent_run", "Rent run"
    WEBHOOK_EVENT = "webhook_event", "Webhook event"
    #: Not a row: the reconciliation, one fixed entity ID (``payments.jobs.RECONCILIATION``).
    RECONCILIATION = "reconciliation", "Reconciliation"


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    #: Its worker stopped mid-run twice (PLAN-010 D1); never run again.
    DEAD = "dead", "Dead"


class Job(GuardedModel):
    """One provider-reaching action, created by the web and run by the worker (PLAN-010 D1).

    ``params`` holds identifiers and amounts only (amounts as strings). ``result`` holds
    the outcome sentence, or an error's class and field names; never provider text.
    """

    kind = models.CharField(max_length=32, choices=JobKind.choices)
    entity_type = models.CharField(max_length=32, choices=JobEntity.choices)
    #: Generic reference, no foreign key, as ``external_refs.entity_id``.
    entity_id = models.UUIDField()
    params = guarded_json(default=dict, blank=True, encoder=DjangoJSONEncoder)
    status = models.CharField(max_length=16, choices=JobStatus.choices, default=JobStatus.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    #: Jobs never retry by themselves; the manual retry creates a new job.
    max_attempts = models.PositiveSmallIntegerField(default=1)
    result = free_text()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    #: A ``running`` job past this time lost its worker.
    locked_until = models.DateTimeField(null=True, blank=True)
    #: The form's one-time key: a second submission finds the first job.
    replay_key = models.UUIDField(null=True, blank=True, unique=True)

    class Meta:
        db_table = "jobs"
        indexes = [
            models.Index(fields=["status", "created_at"], name="jobs_status_created_idx"),
            models.Index(fields=["entity_type", "entity_id", "created_at"], name="jobs_entity_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(kind__in=JobKind.values), name="jobs_kind_valid"),
            models.CheckConstraint(
                condition=Q(entity_type__in=JobEntity.values), name="jobs_entity_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(status__in=JobStatus.values), name="jobs_status_valid"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.status}"


class ReconciliationTrigger(models.TextChoices):
    """How a reconciliation run started (docs/qa/PLAN-010 D5, D10)."""

    SCHEDULED = "scheduled", "Scheduled"
    MANUAL = "manual", "Run now"


class ReconciliationStatus(models.TextChoices):
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    #: Another run held the lock, or the scheduled run was not due (D5, D6).
    SKIPPED = "skipped", "Skipped"


class ReconciliationStep(models.TextChoices):
    """The steps of a run, in order (D7); ``""`` before the first."""

    NONE = "", "None"
    INVOICES = "a", "Invoices"
    TRANSACTIONS = "b", "Transactions"
    DEPOSITS = "c", "Deposits"
    REJECTIONS = "d", "ACH rejections"
    PENDING = "e", "Pending payments"
    REPORT = "f", "Report"


class MismatchKind(models.TextChoices):
    """What a reconciliation found (D7); a closed list."""

    INVOICE_STATUS_DIFFERS = "invoice_status_differs", "Invoice status differed"
    PAYMENT_RESTORED = "payment_restored", "Payment restored"
    DEPOSIT_UNMATCHED = "deposit_unmatched", "Deposit row unmatched"
    SETTLED_PAYMENT_UNDEPOSITED = "settled_payment_undeposited", "Settled payment not deposited"
    RETURN_MISSED_BY_WEBHOOK = "return_missed_by_webhook", "Return missed by webhook"
    PENDING_TOO_OLD = "pending_too_old", "Pending too long"


class ReconciliationRun(GuardedModel):
    """One reconciliation run and its report (D9, D10).

    ``counts`` holds, per step letter, the step's counters (``checked``, ``matched``,
    ``mismatched``, ``restored`` and step-specific ones): integers only. ``error`` holds
    an exception's class name or a fixed reason, never a message or a value.
    """

    trigger = models.CharField(max_length=16, choices=ReconciliationTrigger.choices)
    status = models.CharField(
        max_length=16,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.RUNNING,
    )
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    step_reached = models.CharField(
        max_length=1, choices=ReconciliationStep.choices, default="", blank=True
    )
    counts = guarded_json(default=dict, blank=True)
    #: Requests that left through the rate limiter during the run.
    requests = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error = free_text()
    #: Who pressed "Run now"; null for the scheduled job.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "reconciliation_runs"
        indexes = [
            models.Index(fields=["trigger", "started_at"], name="recon_runs_started_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(trigger__in=ReconciliationTrigger.values),
                name="reconciliation_runs_trigger_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=ReconciliationStatus.values),
                name="reconciliation_runs_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(step_reached__in=ReconciliationStep.values),
                name="reconciliation_runs_step_reached_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.trigger} {self.status}"


class ReconciliationMismatch(GuardedModel):
    """A difference a run found (D10).

    ``entity_id`` is our row's UUID, or, for a provider object we hold no row for, a
    UUID derived from the provider's identifier
    (:func:`payments.reconciliation.mismatches.provider_entity_id`), so no provider
    identifier is stored here (docs/CLAUDE.md Rule 8); pages show it masked. ``detail``
    names identifiers masked only. At most one unresolved row per ``(kind, entity_type,
    entity_id)``: a later run that finds it again counts it as seen.
    """

    run = models.ForeignKey(ReconciliationRun, on_delete=models.PROTECT, related_name="mismatches")
    kind = models.CharField(max_length=32, choices=MismatchKind.choices)
    entity_type = guarded_char(32)
    #: Generic reference, no foreign key, as ``external_refs.entity_id``.
    entity_id = models.UUIDField()
    detail = free_text()
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    resolution_note = free_text()

    class Meta:
        db_table = "reconciliation_mismatches"
        indexes = [
            models.Index(fields=["resolved_at", "created_at"], name="recon_mismatches_list_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=MismatchKind.values),
                name="reconciliation_mismatches_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(resolved_at__isnull=True) | Q(resolution_note__regex=r"\S"),
                name="reconciliation_mismatches_resolved_has_note",
            ),
            models.UniqueConstraint(
                fields=["kind", "entity_type", "entity_id"],
                condition=Q(resolved_at__isnull=True),
                name="reconciliation_mismatches_one_open",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.entity_type}"
