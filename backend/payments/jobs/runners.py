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
from datetime import date
from decimal import Decimal

import payments
from ledger.models import OneOffCharge, Operator
from ledger.services import (
    calendar,
    disputes,
    installments,
    lease_end,
    leases,
    messages,
    one_off_charges,
    refunds,
    rent_run,
)
from ledger.services.leases import AutopayOutcome, MethodsRefresh
from ledger.services.rent_run import MarkAction, MarkResult, MatchAction, MatchReport
from ledger.wording import provider_sentence, with_provider
from payments import jobs, one_off_invoices, operator_sync, reconciliation
from payments.models import (
    Job,
    JobEntity,
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
NO_INVOICE = provider_sentence("The charge has no {provider} invoice to mark.")

_SYNC_FAILED = (Action.FAILED, Action.UPDATE_FAILED, Action.LINK_REFUSED, Action.RETRY_TOO_SOON)
_MARK_FAILED = (MarkAction.FAILED, MarkAction.RETRY_TOO_SOON, MarkAction.NOTHING_TO_RETRY)

#: Most report lines a "Match invoices" job stores; the rest are counted.
MATCH_MAX_LINES = 20
#: Most characters of any one stored report line.
MATCH_LINE_LENGTH = 300
#: The sentence of a replay whose event row no longer exists.
NO_EVENT = provider_sentence("The message from {provider} no longer exists.")


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
    if outcome.action is Action.UPDATE_FAILED:
        # The message names the failure in words; the class and the rejected key names are
        # on the operator row and in the audit row, never shown to staff (copy list §4.1).
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
    return result.action not in _MARK_FAILED, result.message or str(NO_INVOICE)


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


def autopay_stop_job(job: Job) -> Result:
    """PLAN-012e D5 amendment: delete the lease's provider schedules."""
    return _autopay(leases.stop_autopay(job.entity_id, actor=job.requested_by))


def payment_mark_job(job: Job) -> Result:
    return _mark(
        rent_run.mark_provider_invoice(
            job.entity_id, uuid.UUID(str(job.params["payment"])), actor=job.requested_by
        )
    )


def one_off_payment_mark_job(job: Job) -> Result:
    """PLAN-010b D6: the one-off charge's invoice, marked by the rent rules."""
    return _mark(
        one_off_charges.mark_provider_invoice(
            job.entity_id, uuid.UUID(str(job.params["payment"])), actor=job.requested_by
        )
    )


def payment_retry_mark_job(job: Job) -> Result:
    return _mark(rent_run.retry_provider_mark(job.entity_id, actor=job.requested_by))


def one_off_payment_retry_mark_job(job: Job) -> Result:
    """PLAN-010b Batch B: the manual retry of a one-off charge's failed marking."""
    return _mark(one_off_charges.retry_provider_mark(job.entity_id, actor=job.requested_by))


def invoice_create_job(job: Job) -> Result:
    """PLAN-010d D3: the one-off charge's provider invoice (and its e-mail). PLAN-012 D6:
    with :data:`ledger.services.one_off_charges.CHARGE_NOW_PARAM`, a successful create
    queues the ``invoice_pay`` job (the single attempt of 10d, its confirmation carried
    over). PLAN-012e D11: with :data:`ledger.services.installments.RESCHEDULE_PARAM`, the
    provider invoice of a moved installment gets its new due date."""
    if job.entity_type == JobEntity.RENT_CHARGE:
        # PLAN-012b-b D5: an invoice builder invoice of one Rent line alone.
        return one_off_invoices.create_rent_invoice(job.entity_id, job.requested_by)
    if job.params.get(installments.RESCHEDULE_PARAM) is True:
        return one_off_invoices.move_invoice(job.entity_id, job.requested_by)
    ok, text = one_off_invoices.create_provider_invoice(job.entity_id, job.requested_by)
    if ok and job.params.get(one_off_charges.CHARGE_NOW_PARAM) is True:
        charge = OneOffCharge.objects.get(pk=job.entity_id)
        # A create run twice (a lost worker) queues the attempt once.
        if jobs.latest_for(charge, [JobKind.INVOICE_PAY]) is None:
            jobs.enqueue(
                JobKind.INVOICE_PAY,
                charge,
                {"confirmed": job.params.get("confirmed") is True},
                job.requested_by,
            )
    return ok, text


def invoice_send_job(job: Job) -> Result:
    """PLAN-010d D3: the e-mail of the one-off charge's provider invoice, alone (of an
    invoice of rent alone on a rent charge, PLAN-012b-b D5)."""
    if job.entity_type == JobEntity.RENT_CHARGE:
        return one_off_invoices.send_rent(job.entity_id, job.requested_by)
    return one_off_invoices.send(job.entity_id, job.requested_by)


def invoice_delete_job(job: Job) -> Result:
    """PLAN-010d D6: the cancel of an invoiced one-off charge; with the waived flag
    (PLAN-011 D0), the delete of a waived charge's live invoice; with
    :data:`ledger.services.installments.RELEASE_PARAM` (PLAN-012b-a D3), the release of a
    split parent's invoice, the charge kept (a rent charge's too, PLAN-012b-b D13: the
    only kind of job a rent charge has here)."""
    if job.entity_type == JobEntity.RENT_CHARGE:
        return one_off_invoices.release_invoice(job.entity_id, job.requested_by, job.entity_type)
    if job.params.get(one_off_charges.WAIVED_INVOICE_PARAM) is True:
        return one_off_invoices.delete_waived(job.entity_id, job.requested_by)
    if job.params.get(installments.RELEASE_PARAM) is True:
        return one_off_invoices.release_invoice(job.entity_id, job.requested_by)
    return one_off_invoices.delete(job.entity_id, job.requested_by)


def schedule_create_job(job: Job) -> Result:
    """PLAN-010d D2: the one-occurrence schedule of an ``autopay_due`` charge. PLAN-012e
    D11: with :data:`ledger.services.installments.RESCHEDULE_PARAM`, the schedule of a
    moved installment gets its new date."""
    if job.params.get(installments.RESCHEDULE_PARAM) is True:
        return one_off_invoices.move_schedule(job.entity_id, job.requested_by)
    return one_off_invoices.schedule(job.entity_id, job.requested_by)


def schedule_delete_job(job: Job) -> Result:
    """PLAN-010d D6: the cancel of an ``autopay_due`` charge whose schedule has not fired;
    with :data:`ledger.services.installments.RELEASE_PARAM` (PLAN-012b-a D3), the release
    of a split parent's schedule, the charge kept; on a rent charge (PLAN-012b-b D13, the
    only kind of job a rent charge has here), the move of its lease's rent autopay past
    the month."""
    if job.params.get(installments.RELEASE_PARAM) is True or (
        job.entity_type == JobEntity.RENT_CHARGE
    ):
        return one_off_invoices.release_schedule(job.entity_id, job.requested_by, job.entity_type)
    return one_off_invoices.delete_schedule(job.entity_id, job.requested_by)


def invoice_pay_job(job: Job) -> Result:
    """PLAN-010d D4: "Charge now", one pay-an-invoice attempt."""
    return one_off_invoices.pay(
        job.entity_id, job.requested_by, confirmed=job.params.get("confirmed") is True
    )


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
        rejected = with_provider("Fields {provider} rejected")
        text = f"{text} {rejected}: {', '.join(outcome.fields)}."
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
        f"Autopay invoices read: {report.schedules}. "
        f"Attached: {report.count(MatchAction.LINKED)}. "
        f"Attached, but the amounts differ: {report.count(MatchAction.MISMATCH)}. "
        f"Already attached: {report.count(MatchAction.ALREADY)}. "
        f"Not attached: {report.count(MatchAction.REPORTED)}."
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
        return False, str(NO_EVENT)
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
    return with_provider(
        f"Nightly check done: {found} findings, {run.requests} {{provider}} requests, "
        f"{run.duration_ms or 0} ms."
    )


def reconcile_run_job(job: Job) -> Result:
    """The trigger comes from ``params`` (``manual`` when absent): the scheduled job's run
    is ``scheduled`` with no requester, "Run now" keeps who pressed it."""
    trigger = ReconciliationTrigger(job.params.get(jobs.TRIGGER, ReconciliationTrigger.MANUAL))
    run = reconciliation.run_reconciliation(trigger, actor=job.requested_by)
    return run.status == ReconciliationStatus.DONE, reconcile_text(run)


def message_send_job(job: Job) -> Result:
    """PLAN-011b D4, PLAN-012e D8: one e-mail (a collection e-mail or a final
    statement), rendered and sent."""
    outcome = messages.send(job.entity_id)
    return outcome.ok, outcome.message


def calendar_email_job(job: Job) -> Result:
    """PLAN-011b D8: one morning e-mail (a calendar entry or a promise to pay)."""
    day = date.fromisoformat(str(job.params[calendar.MORNING_DAY]))
    return calendar.send_morning_email(job.entity_type, job.entity_id, day)


def disputes_sync_job(job: Job) -> Result:
    """PLAN-012 D8: read the provider's disputes."""
    outcome = disputes.sync()
    return outcome.ok, outcome.message


def dispute_evidence_upload_job(job: Job) -> Result:
    """PLAN-012 D8: send one evidence file."""
    return disputes.upload_evidence(job.entity_id, job.requested_by)


def lease_end_documents_job(job: Job) -> Result:
    """PLAN-012e §4: the final statement PDF of a confirmed lease end, stored under the
    store's documents, and its e-mail queued."""
    return lease_end.produce_documents(job.entity_id, job.requested_by)


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
    JobKind.ONE_OFF_PAYMENT_MARK: one_off_payment_mark_job,
    JobKind.ONE_OFF_PAYMENT_RETRY_MARK: one_off_payment_retry_mark_job,
    JobKind.INVOICE_CREATE: invoice_create_job,
    JobKind.INVOICE_SEND: invoice_send_job,
    JobKind.INVOICE_DELETE: invoice_delete_job,
    JobKind.SCHEDULE_CREATE: schedule_create_job,
    JobKind.SCHEDULE_DELETE: schedule_delete_job,
    JobKind.INVOICE_PAY: invoice_pay_job,
    JobKind.MESSAGE_SEND: message_send_job,
    JobKind.CALENDAR_EMAIL: calendar_email_job,
    JobKind.DISPUTES_SYNC: disputes_sync_job,
    JobKind.DISPUTE_EVIDENCE_UPLOAD: dispute_evidence_upload_job,
    JobKind.LEASE_END_DOCUMENTS: lease_end_documents_job,
    JobKind.AUTOPAY_STOP: autopay_stop_job,
}
