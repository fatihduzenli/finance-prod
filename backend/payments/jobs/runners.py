"""What each job kind runs (docs/qa/PLAN-010-worker-and-reconciliation.md D3).

Every runner calls the existing service function with its existing signature, as the
web view called it before, with the job's requester as the actor, and returns
``(ok, text)``: whether the job is ``done`` or ``failed``, and the service's fixed
sentence (with the error class and field names when the service names them). Nothing
from a provider response reaches ``text``.

A successful operator sync or link queues a read of the operator's payment methods
(:func:`payments.jobs.refresh_methods`) when the operator has a customer reference, so
the enrolment page usually has a recent list.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal

import payments
from ledger.models import Operator
from ledger.services import leases, refunds, rent_run
from ledger.services.leases import AutopayOutcome, MethodsRefresh
from ledger.services.rent_run import MarkAction, MarkResult, MatchAction, MatchReport
from payments import jobs, operator_sync, reconciliation
from payments.models import (
    Job,
    JobKind,
    ReconciliationRun,
    ReconciliationStatus,
    ReconciliationTrigger,
    WebhookEvent,
    WebhookOutcome,
)
from payments.operator_sync import Action, SyncOutcome
from payments.webhooks import processor

Result = tuple[bool, str]

#: The sentence of a sync that had nothing to send.
NO_CALL = "Nothing to send: the operator needs no sync now."
#: The sentence of a marking that had nothing to do.
NO_INVOICE = "The charge has no payment provider invoice to mark."

_SYNC_FAILED = (Action.FAILED, Action.UPDATE_FAILED, Action.LINK_REFUSED, Action.RETRY_TOO_SOON)
_MARK_FAILED = (MarkAction.FAILED, MarkAction.RETRY_TOO_SOON, MarkAction.NOTHING_TO_RETRY)

#: Most report lines a "Match invoices" job stores; the rest are counted.
MATCH_MAX_LINES = 20
#: Most characters of any one stored report line.
MATCH_LINE_LENGTH = 300
#: The sentence of a replay whose event row no longer exists.
NO_EVENT = "The webhook event no longer exists."


def _with_class(text: str, failure: str, fields: str = "") -> str:
    """The sentence, plus the failure class and field names when it does not name them."""
    if not failure or failure in text:
        return text
    named = f"; fields: {fields}" if fields and fields not in text else ""
    return f"{text} ({failure}{named})"


def _sync(job: Job, outcome: SyncOutcome) -> Result:
    ok = outcome.action not in _SYNC_FAILED
    text = outcome.message or NO_CALL
    if ok:
        _refresh_methods_after(job)
        return ok, text
    fields = outcome.fields
    if not fields and outcome.action is Action.FAILED:
        # A failed create or link stores the rejected key names on the row only.
        stored = Operator.objects.filter(pk=job.entity_id, sync_failure=outcome.failure)
        fields = stored.values_list("sync_failure_fields", flat=True).first() or ""
    return ok, _with_class(text, outcome.failure, fields)


def _refresh_methods_after(job: Job) -> None:
    operator = Operator.objects.filter(pk=job.entity_id).first()
    if operator is not None and operator_sync.current_refs(operator).get(
        operator_sync.KIND_CUSTOMER
    ):
        jobs.refresh_methods(operator, job.requested_by)


def _autopay(outcome: AutopayOutcome) -> Result:
    return outcome.ok, _with_class(outcome.text, outcome.failure)


def _mark(result: MarkResult) -> Result:
    return result.action not in _MARK_FAILED, result.message or NO_INVOICE


def _refund(outcome: refunds.Outcome) -> Result:
    return outcome.ok, outcome.message


def operator_sync_job(job: Job) -> Result:
    return _sync(
        job,
        operator_sync.sync_operator(
            job.entity_id, provider=payments.get_provider(), actor=job.requested_by
        ),
    )


def operator_retry_job(job: Job) -> Result:
    return _sync(
        job,
        operator_sync.sync_operator(
            job.entity_id, provider=payments.get_provider(), actor=job.requested_by, retry=True
        ),
    )


def operator_link_job(job: Job) -> Result:
    return _sync(
        job,
        operator_sync.link_operator(
            job.entity_id,
            customer_external_id=str(job.params["customer_id"]),
            payment_method_external_id=str(job.params["payment_method_id"]),
            provider=payments.get_provider(),
            actor=job.requested_by,
        ),
    )


def autopay_enable_job(job: Job) -> Result:
    return _autopay(
        leases.enable_autopay(
            job.entity_id,
            str(job.params["payment_method_id"]),
            actor=job.requested_by,
            confirmed=job.params.get("confirmed") is True,
        )
    )


def autopay_pause_job(job: Job) -> Result:
    return _autopay(leases.pause_autopay(job.entity_id, actor=job.requested_by))


def autopay_resume_job(job: Job) -> Result:
    return _autopay(leases.resume_autopay(job.entity_id, actor=job.requested_by))


def payment_mark_job(job: Job) -> Result:
    return _mark(
        rent_run.mark_provider_invoice(
            job.entity_id, uuid.UUID(str(job.params["payment"])), actor=job.requested_by
        )
    )


def payment_retry_mark_job(job: Job) -> Result:
    return _mark(rent_run.retry_provider_mark(job.entity_id, actor=job.requested_by))


def _creation_key(job: Job) -> uuid.UUID | None:
    raw = job.params.get("creation_key")
    return uuid.UUID(str(raw)) if raw else None


def payment_refund_job(job: Job) -> Result:
    return _refund(
        refunds.refund_payment(
            job.entity_id,
            amount=Decimal(str(job.params["amount"])),
            confirmed=job.params.get("confirmed") is True,
            actor=job.requested_by,
            creation_key=_creation_key(job),
        )
    )


def payment_void_job(job: Job) -> Result:
    return _refund(
        refunds.void_payment(
            job.entity_id,
            confirmed=job.params.get("confirmed") is True,
            actor=job.requested_by,
            creation_key=_creation_key(job),
        )
    )


def _methods(outcome: MethodsRefresh) -> Result:
    text = outcome.message
    if outcome.fields:
        text = f"{text} Fields the provider rejected: {', '.join(outcome.fields)}."
    return outcome.ok, _with_class(text, outcome.failure)


def operator_methods_refresh_job(job: Job) -> Result:
    return _methods(leases.refresh_payment_methods(job.entity_id))


def _line(text: str) -> str:
    """One stored report line: one line of text, cut to :data:`MATCH_LINE_LENGTH`."""
    flat = " ".join(text.split())
    return flat if len(flat) <= MATCH_LINE_LENGTH else f"{flat[: MATCH_LINE_LENGTH - 3]}..."


def match_report_text(report: MatchReport) -> str:
    """The report as stored lines: the counts, then each line that is not "already
    linked" (invoice IDs are masked by the service), at most :data:`MATCH_MAX_LINES`,
    then the failure sentence."""
    if report.refused:
        return "\n".join(_line(text) for text in report.refused)
    lines = [
        f"Schedules read: {report.schedules}. Linked: {report.count(MatchAction.LINKED)}. "
        f"Linked with a total mismatch: {report.count(MatchAction.MISMATCH)}. "
        f"Already linked: {report.count(MatchAction.ALREADY)}. "
        f"Not linked: {report.count(MatchAction.REPORTED)}."
    ]
    shown = [line for line in report.lines if line.action is not MatchAction.ALREADY]
    lines += [_line(f"Invoice {line.invoice}: {line.message}") for line in shown[:MATCH_MAX_LINES]]
    if len(shown) > MATCH_MAX_LINES:
        lines.append(f"{len(shown) - MATCH_MAX_LINES} more lines not shown.")
    if report.failure:
        lines.append(_line(report.failure))
    return "\n".join(lines)


def run_match_job(job: Job) -> Result:
    report = rent_run.match_schedule_invoices(actor=job.requested_by)
    return not report.refused and not report.failure, match_report_text(report)


def webhook_replay_job(job: Job) -> Result:
    outcome = processor.process_event(job.entity_id, replay=True)
    if outcome is None:
        return False, NO_EVENT
    if outcome in (WebhookOutcome.FAILED, WebhookOutcome.DEAD_LETTER):
        error = (
            WebhookEvent.objects.filter(pk=job.entity_id).values_list("error", flat=True).first()
        )
        failed = f"Replayed. The handler failed again; outcome: {outcome}."
        return False, f"{failed} ({error})" if error else failed
    return True, f"Replayed. Outcome: {outcome}."


def reconcile_text(run: ReconciliationRun) -> str:
    """One sentence: the run's status, and its counts or its error class."""
    if run.status == ReconciliationStatus.SKIPPED:
        return str(run.error)
    if run.status == ReconciliationStatus.FAILED:
        return f"Reconciliation failed at step {run.step_reached or '-'}: {run.error}."
    found = sum(int(step.get("mismatched", 0)) for step in run.counts.values())
    return (
        f"Reconciliation done: {found} mismatches found, {run.requests} provider requests, "
        f"{run.duration_ms or 0} ms."
    )


def reconcile_run_job(job: Job) -> Result:
    """The trigger comes from ``params`` (``manual`` when absent): the scheduled job's run
    is ``scheduled`` with no requester, "Run now" keeps who pressed it."""
    trigger = ReconciliationTrigger(job.params.get(jobs.TRIGGER, ReconciliationTrigger.MANUAL))
    run = reconciliation.run_reconciliation(trigger, actor=job.requested_by)
    return run.status == ReconciliationStatus.DONE, reconcile_text(run)


RUNNERS: dict[str, Callable[[Job], Result]] = {
    JobKind.OPERATOR_SYNC: operator_sync_job,
    JobKind.OPERATOR_RETRY: operator_retry_job,
    JobKind.OPERATOR_LINK: operator_link_job,
    JobKind.AUTOPAY_ENABLE: autopay_enable_job,
    JobKind.AUTOPAY_PAUSE: autopay_pause_job,
    JobKind.AUTOPAY_RESUME: autopay_resume_job,
    JobKind.PAYMENT_MARK: payment_mark_job,
    JobKind.PAYMENT_RETRY_MARK: payment_retry_mark_job,
    JobKind.PAYMENT_REFUND: payment_refund_job,
    JobKind.PAYMENT_VOID: payment_void_job,
    JobKind.OPERATOR_METHODS_REFRESH: operator_methods_refresh_job,
    JobKind.RUN_MATCH: run_match_job,
    JobKind.WEBHOOK_REPLAY: webhook_replay_job,
    JobKind.RECONCILE_RUN: reconcile_run_job,
}
