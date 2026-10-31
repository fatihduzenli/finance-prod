"""The end-of-lease wizard (docs/qa/PLAN-012e-lease-end-transfer-installments.md D4, D5,
D7, D8, D13).

The wizard's state is one :class:`~ledger.models.LeaseEnd` row per lease
(:func:`start`); a draft can be left and resumed and is deleted on Cancel
(:func:`cancel`). Its steps:

1. **Terms** (:func:`set_terms`): the end date (not before the start, not in the
   future), the reason and *Prorate last month*.
2. **Blockers** (:func:`blockers`): a one-off, rent or insurance charge of the lease
   not fully paid with a live provider invoice, a one-off charge with a provider
   schedule, a rent or insurance schedule active or paused at the
   provider (its link is the lease's Stop autopay page), a payment of the lease that is
   pending, an installment plan that is live (active or needing attention) on a rent or
   one-off charge of the lease (PLAN-012e D5; its link is the plan's page: let it
   complete or cancel it), a deposit charge not fully collected and not waived or voided
   (PLAN-012b-b D11; its link is the charge's page: collect it or cancel it). The
   wizard never calls the
   provider: the existing actions clear them, and nothing past this step is offered
   while one is listed. Confirm checks them again.
3. **Statement and deductions** (:func:`ledger.services.statements.final`,
   :func:`save_statement`): typed deductions; the unpaid-balance offset never above the
   open balance. Showing the step writes nothing: the rent charges the statement needs
   (the earlier months' missing debits and the end month's charge as confirm would
   create it) are preview lines with the same totals. When the step is saved with
   nothing blocking (:func:`save_statement`, PLAN-012b-b D9, D10, PLAN-012f-a D) they
   are created through the rent run's helpers, and the draft is marked
   ``statement_saved_at`` (set when the statement step is first saved): from then on it
   is in progress (the store page's banner). A draft that never got there is adopted by a
   transfer (:func:`adopt_for_transfer`); one whose terms were never set is deleted
   after :data:`STALE_DRAFT_DAYS` days by the daily job (:func:`sweep_drafts`).
4. **Settlement** (:func:`set_settlement`): when a refund is due, how (Zelle, wire or
   check, never the payment provider), when, a reference and a note.
5. **Confirm** (:func:`confirm`, an explicit "are you sure"): in one transaction, under
   the lease's and the draft's row locks, the blockers are read again, a draft whose
   statement step was never saved (``statement_saved_at``) is refused before any write
   (:data:`STATEMENT_REQUIRED`, PLAN-012f-a §10: the save created the rent charges the
   statement needs) and the statement computed again, then the last
   month is applied (D3), the lease is ended
   (:func:`ledger.services.leases.end_lease`), the unpaid-balance offset is paid to the
   open charges oldest first as settled manual payments (method ``other``, reference
   "deposit offset"), each deposit is resolved in one row
   (:func:`ledger.services.deposits.resolve_deposit_split`), a credit is refunded on its
   rent charge, each credit held on the lease is paid back by the settlement method
   (:func:`ledger.services.credits.refund_all`, PLAN-012d D6b), the lease-end balance
   becomes a one-off charge (category ``other``,
   manual, due 15 days after the end), the figures are kept and the
   ``lease_end_documents`` job is queued. The store's status is unchanged: the store is
   vacant because no lease of it is in effect (:func:`vacant_since`).

The job (:func:`produce_documents`) renders the PDF, stores it under the store's
documents through :func:`ledger.services.documents.upload` (kind ``final_statement``; the
payment-data checks run on our own bytes) and queues the e-mail to the operator
(:func:`ledger.services.messages.queue`); :func:`send_statement` delivers it with the PDF
attached through :func:`mail.get_sender` (``suppressed`` when mail is off, the catch-all
outside production).

A lease end that is part of a transfer (PLAN-012e D6, :mod:`ledger.services.transfer`)
has the reason ``transfer`` fixed, is confirmed by the transfer's confirm in the same
transaction (:func:`confirm_within`) and is cancelled with the transfer.

A refusal is one sentence and writes nothing. Every step writes one ``audit_log`` row
(entity ``lease_end``).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.urls import reverse
from django.utils import timezone

import mail
from documents_storage import StorageUnavailable
from finance.dates import day_text, month_text
from ledger import merge
from ledger.models import (
    LIVE_PLAN_STATUSES,
    AuditLog,
    ChargeEntityType,
    CollectionMethod,
    CollectionStatus,
    DeductionKind,
    Document,
    DocumentKind,
    InstallmentPlan,
    InsuranceCharge,
    Invoice,
    Lease,
    LeaseEnd,
    LeaseEndDeduction,
    LeaseEndReason,
    LeaseEndStatus,
    LeaseTransfer,
    LeaseTransferStatus,
    LegacyStatus,
    MessageKind,
    MessageStatus,
    MessageTemplate,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    OutboundMessage,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RefundMethod,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
    Store,
)
from ledger.services import (
    autopay_plans,
    credits,
    deposits,
    documents,
    installments,
    leases,
    messages,
    one_off_charges,
    receivables,
    rent_run,
    statements,
)
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from ledger.wording import format_amount, provider_name
from mail.base import SUBJECT_MAX_LENGTH
from payments import jobs, lease_sync
from payments.models import ExternalRef, JobKind

ENTITY = "lease_end"
ENTITY_MESSAGE = "outbound_message"
#: Longest note on the terms or the settlement.
NOTE_MAX_LENGTH = 2000
#: Most deduction lines on one statement.
MAX_DEDUCTIONS = 20
#: The lease-end balance is due this many days after the end date (D5(d)).
DEBT_DUE_DAYS = 15
DEBT_DESCRIPTION = "Lease-end balance"
#: PLAN-012b-b D9: a draft whose terms were never set is deleted after this many days.
STALE_DRAFT_DAYS = 7
OFFSET_REFERENCE = statements.OFFSET_REFERENCE
#: D3 amendment: the note of a rent charge after the end month, waived at confirm.
AFTER_END_NOTE = "After lease end (wizard)"
#: Payment method of each refund method (PLAN-012e-3: a check is recorded as ``check``).
PAYMENT_METHODS: dict[str, str] = {
    RefundMethod.ZELLE: PaymentMethod.ZELLE,
    RefundMethod.WIRE: PaymentMethod.WIRE,
    RefundMethod.CHECK: PaymentMethod.CHECK,
}

# --- Blocker kinds -----------------------------------------------------------------------

LIVE_INVOICE = "live_invoice"
ONE_OFF_SCHEDULE = "one_off_schedule"
AUTOPAY = "autopay"
PENDING_PAYMENT = "pending_payment"
INSTALLMENT_PLAN = "installment_plan"
AUTOPAY_PLAN = "autopay_plan"
DEPOSIT_UNPAID = "deposit_unpaid"

# --- Fixed sentences -----------------------------------------------------------------------

NOT_DRAFT = "This lease end is already confirmed. Nothing was changed."
IN_PROGRESS = "A lease end is in progress for this lease (statement step). Nothing was changed."
ALREADY_ENDED = "This lease has already ended. Nothing was changed."
NOT_CURRENT = "This store has no lease to end."
#: A live autopay plan (PLAN-012f-a D15): it would make debits after the lease ends.
AUTOPAY_PLAN_LIVE = "{item}: a repeating debit is set up; stop it first."
DATE_REQUIRED = leases.END_DATE_REQUIRED
DATE_FUTURE = leases.END_IN_FUTURE
DATE_BEFORE_START = leases.END_BEFORE_START
REASON_REQUIRED = "Choose why the lease ended."
REASON_NOTE_REQUIRED = "Say in a few words why the lease ended."
NOTE_TOO_LONG = f"The note may be at most {NOTE_MAX_LENGTH} characters."
TERMS_REQUIRED = "Set the end date and the reason first."
TERMS_SAVED = "End date and reason saved."
DEDUCTION_KIND = "Choose the kind of each deduction."
DEDUCTION_AMOUNT = "Each deduction needs an amount above zero, with at most two decimals."
DEDUCTION_TOO_LARGE = "A deduction is too large."
DEDUCTION_DESCRIPTION = "An Other deduction needs a description."
DEDUCTION_TOO_LONG = "A description may be at most 200 characters."
TOO_MANY_DEDUCTIONS = f"At most {MAX_DEDUCTIONS} deductions."
OFFSET_ABOVE_OPEN = "The unpaid balance offset cannot be more than the open balance ({open})."
DEDUCTIONS_SAVED = "Deductions saved."
METHOD_INVALID = "Choose Zelle, wire or check."
REFUND_DATE_REQUIRED = "Enter the date the refund is paid."
REFUND_DATE_FUTURE = "The refund date cannot be in the future."
REFERENCE_TOO_LONG = "The reference may be at most 100 characters."
SETTLEMENT_SAVED = "Settlement saved."
SETTLEMENT_REQUIRED = "A refund is due: choose how and when it is paid on the settlement step."
#: PLAN-012f-a §10: confirm needs the statement step saved in this wizard.
STATEMENT_REQUIRED = "Save the statement step before confirming."
CONFIRM_REQUIRED = "Check the box to confirm that the lease ends with these effects."
BLOCKED = "The lease cannot end yet: {count} item(s) are still live. Nothing was changed."
NO_OPERATOR_FOR_DEBT = "The lease has no operator to charge the lease-end balance to."
CHANGED_MEANWHILE = (
    "A charge of this lease changed while confirming. Nothing was changed; try again."
)
CONFIRMED = "The lease has ended. The statement PDF and its e-mail are being prepared."
CANCELLED = "The lease end was cancelled. Nothing was changed."
#: PLAN-012e D6: a lease end that is part of a transfer.
TRANSFER_REASON_FIXED = "This lease ends by a transfer to another operator; the reason is fixed."
TRANSFER_CONFIRMS = (
    "This lease end is part of a transfer: confirm it on the transfer's last step. "
    "Nothing was changed."
)
TRANSFER_CANCELS = (
    "This lease end is part of a transfer: cancel the transfer instead. Nothing was changed."
)
GONE = "The lease end no longer exists."
#: PLAN-012b-b D11.
DEPOSIT_NOT_COLLECTED = "{label} {amount} not collected: collect it, or waive or cancel the charge."
NOT_CONFIRMED = "The lease end is not confirmed."
NO_UPLOADER = "NoUploader"
NO_TEMPLATE = "The final statement e-mail template is missing."
DOCUMENTS_DONE = "Final statement stored; the e-mail to the operator will be sent."
DOCUMENTS_NO_OPERATOR = "Final statement stored; the lease has no operator to e-mail."
STATEMENT_REFUSED = "The final statement was not stored: {reason}"


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    field: str | None = None
    lease_end: LeaseEnd | None = None


@dataclass(frozen=True, slots=True)
class Blocker:
    kind: str
    text: str
    url: str


@dataclass(frozen=True, slots=True)
class DeductionInput:
    kind: str
    description: str
    amount: Decimal


class Refused(Exception):  # noqa: N818 (an outcome, as LeaseRefused)
    """:func:`confirm_within` refused; the caller's transaction must roll back."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class _Abort(Exception):  # noqa: N818 - internal signal, rolls the confirm back
    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def _refused(message: str, field: str | None = None) -> Outcome:
    return Outcome(False, message, field)


def _audit(
    actor: User | None,
    action: str,
    entity_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    entity_type: str = ENTITY,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )


def _state(end: LeaseEnd) -> dict[str, Any]:
    return {
        "lease": str(end.lease_id),
        "ended_on": end.ended_on.isoformat() if end.ended_on else None,
        "reason": end.reason,
        "reason_note": end.reason_note,
        "prorate_last_month": end.prorate_last_month,
        "status": end.status,
        "refund_method": end.refund_method,
        "refund_on": end.refund_on.isoformat() if end.refund_on else None,
        "refund_reference": end.refund_reference,
        "settlement_note": end.settlement_note,
    }


# --- Reads ---------------------------------------------------------------------------------


def current_lease(store: Store, today: date | None = None) -> Lease | None:
    """The lease the store page's End lease is for: the store's lease not ended, active
    or paused, started by ``today`` (the later start first)."""
    day = today or timezone.localdate()
    return (
        Lease.objects.filter(
            store=store,
            ended_on__isnull=True,
            collection_status__in=[CollectionStatus.ACTIVE, CollectionStatus.PAUSED],
        )
        .filter(Q(started_on__isnull=True) | Q(started_on__lte=day))
        .select_related("store", "operator")
        .order_by("-started_on", "-created_at")
        .first()
    )


def vacant_since(store: Store, today: date | None = None) -> date | None:
    """The day after the store's last lease ended, when no lease of it is in effect
    ``today`` (the same rule as the attention list's vacant store); ``None`` otherwise."""
    day = today or timezone.localdate()
    found = [lease for lease in store.leases.all() if lease.collection_status != "prospective"]
    in_effect = any(
        lease.collection_status in (CollectionStatus.ACTIVE, CollectionStatus.PAUSED)
        and (lease.started_on is None or lease.started_on <= day)
        and (lease.ended_on is None or lease.ended_on >= day)
        for lease in found
    )
    ended = [lease.ended_on for lease in found if lease.ended_on is not None]
    if in_effect or not ended:
        return None
    return max(ended) + timedelta(days=1)


def draft_transfer(end: LeaseEnd) -> LeaseTransfer | None:
    """The transfer in progress this lease end belongs to (PLAN-012e D6), if any."""
    return LeaseTransfer.objects.filter(lease_end=end, status=LeaseTransferStatus.DRAFT).first()


def terms_set(end: LeaseEnd) -> bool:
    return end.ended_on is not None and bool(end.reason)


def blockers(lease: Lease, today: date | None = None) -> list[Blocker]:
    """What must be collected or cancelled before the lease ends (D5(b)), each with the
    page that clears it. Local data only: no provider call. A rent autopay moved past a
    split month (PLAN-012b-b D13) leaves its old schedules ended (no blocker); the new
    schedules are live rent schedules and block as any other until autopay is stopped."""
    found: list[Blocker] = []
    charges = (
        OneOffCharge.objects.filter(lease=lease)
        .exclude(status__in=[OneOffChargeStatus.PAID, OneOffChargeStatus.VOIDED])
        .select_related("fee_type")
        .order_by("due_date", "created_at", "id")
    )
    for charge in charges:
        url = reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk})
        live = one_off_charges.live_provider_invoice(charge)
        if live is not None:
            found.append(
                Blocker(
                    LIVE_INVOICE,
                    f"{charge.label}: invoice {live[0].invoice_number} is still open in "
                    f"{provider_name()}. Collect it or cancel it first.",
                    url,
                )
            )
        if one_off_charges.provider_schedule(charge):
            found.append(
                Blocker(
                    ONE_OFF_SCHEDULE,
                    f"{charge.label}: autopay will collect it. Collect it or cancel the "
                    "charge first.",
                    url,
                )
            )
        unpaid = charge.price - charge.amount_paid
        if (
            charge.fee_type is not None
            and charge.fee_type.refundable
            and charge.status in (OneOffChargeStatus.OWED, OneOffChargeStatus.PARTIAL)
            and unpaid > 0
        ):
            found.append(
                Blocker(
                    DEPOSIT_UNPAID,
                    DEPOSIT_NOT_COLLECTED.format(label=charge.label, amount=format_amount(unpaid)),
                    url,
                )
            )
    rents = (
        RentCharge.objects.filter(lease=lease)
        .exclude(status=RentChargeStatus.PAID)
        .order_by("period_month", "seq", "id")
    )
    for rent in rents:
        # A builder invoice's Rent line names the charge too (PLAN-012b-b D5).
        invoice = _live_invoice(rent_run.charge_invoices(rent))
        if invoice is not None:
            what = f"Rent {month_text(rent.period_month)}" + (
                f" (debit {rent.seq})" if rent.seq > 1 else ""
            )
            found.append(
                Blocker(
                    LIVE_INVOICE,
                    f"{what}: invoice {invoice.invoice_number} is still open in "
                    f"{provider_name()}. Collect it or cancel it first.",
                    reverse("ledger:charges:detail", kwargs={"pk": rent.pk}),
                )
            )
    for insurance in InsuranceCharge.objects.filter(lease=lease).order_by("due_on", "id"):
        if _insurance_paid(insurance):
            continue
        invoice = _live_invoice(insurance.invoices.all())
        if invoice is not None:
            found.append(
                Blocker(
                    LIVE_INVOICE,
                    f"Insurance, policy year {insurance.policy_year}: invoice "
                    f"{invoice.invoice_number} is still open in {provider_name()}. Collect it "
                    "or cancel it first.",
                    reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
                )
            )
    stop_url = reverse("ledger:leases:stop_autopay", kwargs={"pk": lease.pk})
    for row, _ref in leases.stoppable_schedules(lease):
        what = "Rent" if row.kind == ScheduleKind.RENT else "Insurance"
        state = "on" if row.state == ScheduleState.ACTIVE else "paused"
        found.append(
            Blocker(
                AUTOPAY,
                f"{what} autopay (debit {row.seq}) is {state} in {provider_name()}. "
                "It must be turned off first.",
                stop_url,
            )
        )
    plans = (
        InstallmentPlan.objects.filter(status__in=LIVE_PLAN_STATUSES)
        .filter(Q(rent_charge__lease=lease) | Q(one_off_charge__lease=lease))
        .select_related("rent_charge", "one_off_charge")
        .order_by("confirmed_at", "id")
    )
    for plan in plans:
        progress = installments.progress(plan)
        found.append(
            Blocker(
                INSTALLMENT_PLAN,
                f"{installments.parent_label(plan.parent)}: an installment plan is live "
                f"({progress.paid_count} of {progress.count} paid). Let it complete or "
                "cancel it first.",
                reverse("ledger:plans:detail", kwargs={"pk": plan.pk}),
            )
        )
    for autopay_plan in autopay_plans.live_plans(lease):
        found.append(
            Blocker(
                AUTOPAY_PLAN,
                AUTOPAY_PLAN_LIVE.format(item=autopay_plan.item_name),
                reverse("ledger:stores:detail", kwargs={"pk": autopay_plan.store_id})
                # The Autopay tab (workspace.tab_url, which imports this module).
                + "?tab=schedules#tab-schedules",
            )
        )
    pending = (
        Payment.objects.filter(status=PaymentStatus.PENDING)
        .filter(
            Q(rent_charge__lease=lease)
            | Q(insurance_charge__lease=lease)
            | Q(one_off_charge__lease=lease)
        )
        .order_by("initiated_on", "created_at", "id")
    )
    for payment in pending:
        started = statements.long_date(payment.initiated_on) if payment.initiated_on else "—"
        found.append(
            Blocker(
                PENDING_PAYMENT,
                f"A payment of {format_amount(payment.amount)} started on {started} is "
                "pending. Wait until it settles or is returned.",
                reverse("ledger:payments:detail", kwargs={"pk": payment.pk}),
            )
        )
    return found


def _live_invoice(invoices: QuerySet[Invoice]) -> Invoice | None:
    """The first of ``invoices`` (by issue date and number) with a current provider
    reference, when it is live (not void, not last seen ``DELETED``): the rule of a
    one-off charge's (:func:`ledger.services.one_off_charges.live_provider_invoice`)."""
    for invoice in invoices.order_by("issue_date", "invoice_number"):
        if ExternalRef.objects.filter(
            entity_type=rent_run.ENTITY_INVOICE,
            entity_id=invoice.pk,
            provider=leases.PROVIDER_KEY,
            external_kind=rent_run.KIND_INVOICE,
            superseded_at__isnull=True,
        ).exists():
            return invoice if one_off_charges.invoice_is_live(invoice) else None
    return None


def _insurance_paid(charge: InsuranceCharge) -> bool:
    """Whether an insurance charge is fully paid: marked paid before the app, or its
    settled payments (less refunds) cover its amount. One without a premium is owed."""
    if charge.legacy_status == LegacyStatus.PAID:
        return True
    if charge.amount is None:
        return False
    paid = Decimal("0.00")
    for payment in charge.payments.filter(status=PaymentStatus.SETTLED):
        amount = Decimal(payment.amount)
        paid += -amount if payment.is_refund else amount
    return paid >= Decimal(charge.amount)


def default_deductions(end: LeaseEnd, today: date | None = None) -> list[DeductionInput]:
    """What the statement step shows before any deduction is saved: the unpaid-balance
    offset, pre-filled with the open balance when there is one and a deposit is held
    (what the deposits do not cover stays open on its charges)."""
    figures = statements.final(end, today=today)
    if figures.open_total <= 0 or figures.deposit_held <= 0:
        return []
    return [DeductionInput(DeductionKind.UNPAID_BALANCE, "", figures.open_total)]


# --- Steps ---------------------------------------------------------------------------------


def start(lease: Lease, actor: User | None, *, reason: str = "") -> LeaseEnd:
    """The lease's draft, made now or found (one per lease). ``ValueError`` for a lease
    that has ended or whose end is confirmed. ``reason`` is set on a new draft (the
    transfer wizard's ``transfer``)."""
    with transaction.atomic():
        locked = Lease.objects.select_for_update(of=("self",)).get(pk=lease.pk)
        found = LeaseEnd.objects.filter(lease=locked).first()
        if found is not None:
            if found.status != LeaseEndStatus.DRAFT:
                raise ValueError(NOT_DRAFT)
            return found
        if locked.ended_on is not None:
            raise ValueError(ALREADY_ENDED)
        end = LeaseEnd.objects.create(lease=locked, created_by=actor, reason=reason)
        _audit(actor, "lease_end.started", end.pk, None, _state(end))
    return end


def in_progress(end: LeaseEnd | None) -> bool:
    """Whether a lease end is in progress (PLAN-012b-b D9): a draft whose statement step
    has been saved (``statement_saved_at``, set on its first save)."""
    return (
        end is not None
        and end.status == LeaseEndStatus.DRAFT
        and end.statement_saved_at is not None
    )


def store_in_progress(store: Store) -> LeaseEnd | None:
    """The store's lease end in progress (:func:`in_progress`), if any: the store page's
    banner."""
    return (
        LeaseEnd.objects.filter(
            lease__store=store,
            status=LeaseEndStatus.DRAFT,
            statement_saved_at__isnull=False,
        )
        .select_related("lease")
        .order_by("created_at", "id")
        .first()
    )


def adopt_for_transfer(end: LeaseEnd, actor: User | None) -> LeaseEnd:
    """PLAN-012b-b D9: a draft whose statement step was never saved becomes the transfer's:
    the reason is set to ``transfer``, the terms are kept, audited
    ``lease_end.adopted_by_transfer``. Runs in the caller's transaction (the transfer's
    start, under the store's row lock). ``ValueError`` with one sentence for a draft
    that is in progress or confirmed."""
    row = LeaseEnd.objects.select_for_update(of=("self",)).get(pk=end.pk)
    if row.status != LeaseEndStatus.DRAFT:
        raise ValueError(NOT_DRAFT)
    if row.statement_saved_at is not None:
        raise ValueError(IN_PROGRESS)
    before = _state(row)
    row.reason = LeaseEndReason.TRANSFER
    row.save(update_fields=["reason", "updated_at"])
    _audit(actor, "lease_end.adopted_by_transfer", row.pk, before, _state(row))
    return row


def sweep_drafts(now: datetime | None = None) -> int:
    """PLAN-012b-b D9: delete each draft whose terms were never set, made more than
    :data:`STALE_DRAFT_DAYS` days ago and not part of a transfer, with its deductions;
    audited ``lease_end.swept``. Returns how many were deleted (the daily job)."""
    cutoff = (now or timezone.now()) - timedelta(days=STALE_DRAFT_DAYS)
    candidates = (
        LeaseEnd.objects.filter(status=LeaseEndStatus.DRAFT, created_at__lt=cutoff)
        .filter(Q(ended_on__isnull=True) | Q(reason=""))
        .filter(transfer__isnull=True)
        .values_list("pk", flat=True)
    )
    swept = 0
    for pk in list(candidates):
        with transaction.atomic():
            row = (
                LeaseEnd.objects.select_for_update(of=("self",))
                .filter(pk=pk, status=LeaseEndStatus.DRAFT, created_at__lt=cutoff)
                .first()
            )
            if row is None or terms_set(row):
                continue
            if LeaseTransfer.objects.filter(lease_end=row).exists():
                continue
            state = _state(row)
            row.delete()
            _audit(None, "lease_end.swept", pk, state, None)
            swept += 1
    return swept


def _locked_draft(end_id: uuid.UUID) -> LeaseEnd:
    end = (
        LeaseEnd.objects.select_for_update(of=("self",))
        .select_related("lease", "lease__store", "lease__operator")
        .get(pk=end_id)
    )
    if end.status != LeaseEndStatus.DRAFT:
        raise _Abort(NOT_DRAFT)
    return end


def _note(raw: str, field: str) -> str:
    text = visible_text(raw or "")
    if len(text) > NOTE_MAX_LENGTH:
        raise _Abort(NOTE_TOO_LONG, field)
    try:
        validate_no_payment_data(text)
    except ValidationError:
        raise _Abort(PAYMENT_DATA_MESSAGE, field) from None
    return text


def set_terms(
    end: LeaseEnd,
    ended_on: date | None,
    reason: str,
    prorate: bool,
    *,
    note: str = "",
    actor: User | None,
    today: date | None = None,
) -> Outcome:
    """Step (a): the end date, the reason (``other`` needs a note) and *Prorate last
    month*."""
    day = today or timezone.localdate()
    try:
        with transaction.atomic():
            row = _locked_draft(end.pk)
            lease = row.lease
            if ended_on is None:
                raise _Abort(DATE_REQUIRED, "ended_on")
            if ended_on > day:
                raise _Abort(DATE_FUTURE, "ended_on")
            if lease.started_on is not None and ended_on < lease.started_on:
                raise _Abort(DATE_BEFORE_START.format(start=day_text(lease.started_on)), "ended_on")
            if reason not in LeaseEndReason.values:
                raise _Abort(REASON_REQUIRED, "reason")
            if reason != LeaseEndReason.TRANSFER and draft_transfer(row) is not None:
                raise _Abort(TRANSFER_REASON_FIXED, "reason")
            text = _note(note, "reason_note")
            if reason == LeaseEndReason.OTHER and not text:
                raise _Abort(REASON_NOTE_REQUIRED, "reason_note")
            before = _state(row)
            row.ended_on = ended_on
            row.reason = reason
            row.reason_note = text
            row.prorate_last_month = prorate is True
            row.save(
                update_fields=[
                    "ended_on",
                    "reason",
                    "reason_note",
                    "prorate_last_month",
                    "updated_at",
                ]
            )
            _audit(actor, "lease_end.terms_set", row.pk, before, _state(row))
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    return Outcome(True, TERMS_SAVED, lease_end=row)


def _clean_deduction(item: DeductionInput) -> DeductionInput:
    if item.kind not in DeductionKind.values:
        raise _Abort(DEDUCTION_KIND, "deductions")
    amount = item.amount
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        raise _Abort(DEDUCTION_AMOUNT, "deductions")
    if amount != amount.quantize(rent_run.CENT):
        raise _Abort(DEDUCTION_AMOUNT, "deductions")
    if amount > rent_run.MONEY_MAX:
        raise _Abort(DEDUCTION_TOO_LARGE, "deductions")
    description = visible_text(item.description or "")
    if len(description) > 200:
        raise _Abort(DEDUCTION_TOO_LONG, "deductions")
    try:
        validate_no_payment_data(description)
    except ValidationError:
        raise _Abort(PAYMENT_DATA_MESSAGE, "deductions") from None
    if item.kind == DeductionKind.OTHER and not description:
        raise _Abort(DEDUCTION_DESCRIPTION, "deductions")
    return DeductionInput(item.kind, description, amount)


def _offset_refusal(figures: statements.Final) -> str:
    if figures.offset_total > figures.open_total:
        return OFFSET_ABOVE_OPEN.format(open=format_amount(figures.open_total))
    return ""


def _write_deductions(
    row: LeaseEnd, lines: Sequence[DeductionInput], actor: User | None, today: date | None
) -> None:
    """Replace the locked draft's deductions, refused (:class:`_Abort`) when one is not
    valid or the unpaid-balance offset is above the open balance; audited
    ``lease_end.deductions_set``. Runs in the caller's transaction."""
    if len(lines) > MAX_DEDUCTIONS:
        raise _Abort(TOO_MANY_DEDUCTIONS, "deductions")
    cleaned = [_clean_deduction(item) for item in lines]
    before = [_deduction_state(item) for item in row.deductions.all()]
    row.deductions.all().delete()
    for position, item in enumerate(cleaned, start=1):
        LeaseEndDeduction.objects.create(
            lease_end=row,
            kind=item.kind,
            description=item.description,
            amount=item.amount,
            position=position,
        )
    refusal = _offset_refusal(statements.final(row, today=today))
    if refusal:
        raise _Abort(refusal, "deductions")
    _audit(
        actor,
        "lease_end.deductions_set",
        row.pk,
        {"deductions": before},
        {"deductions": [_deduction_state(item) for item in cleaned]},
    )


def set_deductions(
    end: LeaseEnd,
    lines: Sequence[DeductionInput],
    *,
    actor: User | None,
    today: date | None = None,
) -> Outcome:
    """Replace the deductions only, no rent charge created (the statement step saves
    through :func:`save_statement`). The unpaid-balance offset is never above the open
    balance."""
    try:
        with transaction.atomic():
            row = _locked_draft(end.pk)
            if not terms_set(row):
                raise _Abort(TERMS_REQUIRED)
            _write_deductions(row, lines, actor, today)
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    return Outcome(True, DEDUCTIONS_SAVED, lease_end=row)


def _create_statement_charges(lease: Lease, row: LeaseEnd, actor: User | None) -> list[RentCharge]:
    """The rent charges the statement needs (PLAN-012b-b D10, PLAN-012f-a D), created under
    the caller's lease and draft row locks: the missing debits of the months before the end
    month by the rent run's catch-up (:func:`ledger.services.rent_run.catch_up_lease`), and
    each charge of the end month that the last month's rule needs
    (:func:`ledger.services.statements.last_month`) by
    :func:`ledger.services.rent_run.create_month`, audited with the lease end. Charges that
    exist are never changed here (confirm reduces them). Returns the charges created."""
    if row.ended_on is None:
        return []
    month = row.ended_on.replace(day=1)
    made = list(rent_run.catch_up_lease(lease, month - timedelta(days=1), actor))
    last = statements.last_month(row)
    if last is not None:
        for change in last.changes:
            if change.charge_id is not None:
                continue
            charge, created = rent_run.create_month(
                lease,
                last.month,
                actor,
                due_on=change.due_on,
                seq=change.seq,
                amount=change.after,
                proration_text=last.text,
                context={"lease_end": str(row.pk)},
            )
            if created:
                made.append(charge)
    return made


def save_statement(
    end: LeaseEnd,
    lines: Sequence[DeductionInput],
    *,
    actor: User | None,
    today: date | None = None,
) -> Outcome:
    """Step (c) saved, "Save and continue" (PLAN-012b-b D9, D10; PLAN-012f-a D).

    In one transaction, under the lease's and the draft's row locks, when the draft's
    terms are set, the lease has not ended and nothing blocks (:func:`blockers`): the
    missing debits of the months before the end month are created by the rent run's
    catch-up (:func:`ledger.services.rent_run.catch_up_lease`), each charge of the end
    month that confirm would create (:func:`ledger.services.statements.last_month`,
    prorated by its rule) by :func:`ledger.services.rent_run.create_month`, audited with
    the lease end, and the deductions are replaced (the unpaid-balance offset never above
    the open balance). Charges that exist are never changed here (confirm reduces them).
    The first time, ``statement_saved_at`` is set (``lease_end.statement_saved``, with
    the charges created). A refusal is one sentence and writes nothing. The statement
    page shows the same figures before the save (preview lines). Local data only: no
    provider call."""
    day = today or timezone.localdate()
    try:
        with transaction.atomic():
            lease = Lease.objects.select_for_update(of=("self",)).get(pk=end.lease_id)
            row = _locked_draft(end.pk)
            if row.ended_on is None or not terms_set(row):
                raise _Abort(TERMS_REQUIRED)
            if lease.ended_on is not None:
                raise _Abort(ALREADY_ENDED)
            found = blockers(lease, day)
            if found:
                raise _Abort(BLOCKED.format(count=len(found)))
            made = _create_statement_charges(lease, row, actor)
            _write_deductions(row, lines, actor, day)
            if row.statement_saved_at is None:
                row.statement_saved_at = timezone.now()
                row.save(update_fields=["statement_saved_at", "updated_at"])
                _audit(
                    actor,
                    "lease_end.statement_saved",
                    row.pk,
                    {"statement_saved_at": None},
                    {
                        "statement_saved_at": row.statement_saved_at.isoformat(),
                        "charges": [str(charge.pk) for charge in made],
                    },
                )
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    return Outcome(True, DEDUCTIONS_SAVED, lease_end=row)


def _deduction_state(item: DeductionInput | LeaseEndDeduction) -> dict[str, str]:
    return {"kind": item.kind, "description": item.description, "amount": str(item.amount)}


def set_settlement(
    end: LeaseEnd,
    method: str,
    on: date | None,
    reference: str,
    note: str,
    *,
    actor: User | None,
    today: date | None = None,
) -> Outcome:
    """Step (d): how a refund is paid back (Zelle, wire or check; empty when nothing is
    refunded), when, a reference and a note. A card or account number in the reference
    or the note is refused here, before any statement exists."""
    day = today or timezone.localdate()
    try:
        with transaction.atomic():
            row = _locked_draft(end.pk)
            if not terms_set(row):
                raise _Abort(TERMS_REQUIRED)
            method = (method or "").strip()
            if method and method not in RefundMethod.values:
                raise _Abort(METHOD_INVALID, "method")
            if method and on is None:
                raise _Abort(REFUND_DATE_REQUIRED, "refund_on")
            if on is not None and on > day:
                raise _Abort(REFUND_DATE_FUTURE, "refund_on")
            text_reference = visible_text(reference or "")
            if len(text_reference) > 100:
                raise _Abort(REFERENCE_TOO_LONG, "reference")
            try:
                validate_no_payment_data(text_reference)
            except ValidationError:
                raise _Abort(PAYMENT_DATA_MESSAGE, "reference") from None
            text_note = _note(note, "note")
            before = _state(row)
            row.refund_method = method
            row.refund_on = on if method else None
            row.refund_reference = text_reference
            row.settlement_note = text_note
            row.save(
                update_fields=[
                    "refund_method",
                    "refund_on",
                    "refund_reference",
                    "settlement_note",
                    "updated_at",
                ]
            )
            _audit(actor, "lease_end.settlement_set", row.pk, before, _state(row))
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    return Outcome(True, SETTLEMENT_SAVED, lease_end=row)


def cancel(end: LeaseEnd, actor: User | None) -> Outcome:
    """Delete the draft and its deductions; nothing else is changed."""
    try:
        with transaction.atomic():
            row = _locked_draft(end.pk)
            if draft_transfer(row) is not None:
                raise _Abort(TRANSFER_CANCELS)
            state = _state(row)
            pk = row.pk
            row.delete()
            _audit(actor, "lease_end.cancelled", pk, state, None)
    except _Abort as abort:
        return _refused(abort.message)
    return Outcome(True, CANCELLED)


# --- Confirm -------------------------------------------------------------------------------


def _payment_method(end: LeaseEnd) -> str:
    return PAYMENT_METHODS.get(end.refund_method, "")


def _settlement_text(end: LeaseEnd, what: str) -> str:
    label = end.lease.store.external_label
    text = f"{what} at the end of the lease of store {label}."
    if end.refund_method == RefundMethod.CHECK:
        text += " Paid by check."
    if end.settlement_note:
        text += f" {end.settlement_note}"
    return text


def _apply_last_month(
    end: LeaseEnd, last: statements.LastMonth, on: date, actor: User | None
) -> tuple[list[str], list[Payment]]:
    """D3: reduce the month's charges and refund a credit (the missing charges were created
    by the statement step's save, :func:`_create_statement_charges`). Returns the charges
    written and the credit refunds."""
    written: list[str] = []
    refunds: list[Payment] = []
    for change in last.changes:
        if change.charge_id is None:
            # PLAN-012f-a §10: never reached; the statement step's save created the end
            # month's missing charges (_create_statement_charges) and confirm refuses a draft
            # never saved. A missing charge here means the month changed: refuse rather than
            # create it.
            raise _Abort(CHANGED_MEANWHILE)
        charge = RentCharge.objects.select_for_update().get(pk=change.charge_id)
        if (charge.amount_due, charge.amount_paid) != (change.before, change.paid):
            raise _Abort(CHANGED_MEANWHILE)
        before = rent_run.charge_state(charge)
        refund_payment = None
        if change.refund > 0:
            refund_payment = Payment(
                rent_charge=charge,
                kind=PaymentKind.RENT,
                method=_payment_method(end),
                amount=change.refund,
                status=PaymentStatus.SETTLED,
                is_refund=True,
                parent_payment_id=change.parent_payment_id,
                initiated_on=on,
                settled_on=on,
                reference=end.refund_reference,
                note=_settlement_text(end, "Rent paid for days after the last day, paid back"),
                recorded_by=actor,
            )
            refund_payment.save()
            refunds.append(refund_payment)
        charge.amount_due = change.after
        charge.proration_text = last.text or None
        charge.save(update_fields=["amount_due", "proration_text", "updated_at"])
        rent_run.recompute_charge(charge)
        _audit(
            actor,
            "rent_charge.prorated",
            charge.pk,
            before,
            {
                **rent_run.charge_state(charge),
                "lease_end": str(end.pk),
                "refund_payment": str(refund_payment.pk) if refund_payment else None,
            },
            entity_type=rent_run.ENTITY_CHARGE,
        )
        written.append(str(charge.pk))
    return written, refunds


def _apply_after_end(
    end: LeaseEnd, later: Sequence[statements.AfterEnd], on: date, actor: User | None
) -> tuple[list[str], list[Payment]]:
    """D3 amendment: waive each rent charge of a month after the end month, its paid
    part refunded first. Returns the charges waived and the credit refunds."""
    written: list[str] = []
    refunds: list[Payment] = []
    for item in later:
        charge = RentCharge.objects.select_for_update().get(pk=item.charge_id)
        if (charge.amount_due, charge.amount_paid, charge.status) != (
            item.amount_due,
            item.paid,
            item.status,
        ):
            raise _Abort(CHANGED_MEANWHILE)
        before = rent_run.charge_state(charge)
        made: list[Payment] = []
        for refund in item.refunds:
            payment = Payment(
                rent_charge=charge,
                kind=PaymentKind.RENT,
                method=_payment_method(end),
                amount=refund.amount,
                status=PaymentStatus.SETTLED,
                is_refund=True,
                parent_payment_id=refund.parent_payment_id,
                initiated_on=on,
                settled_on=on,
                reference=end.refund_reference,
                note=_settlement_text(end, "Rent paid for a month after the end date, paid back"),
                recorded_by=actor,
            )
            payment.save()
            made.append(payment)
        if made:
            rent_run.recompute_charge(charge)
        charge.status = RentChargeStatus.WAIVED
        charge.review_message = AFTER_END_NOTE
        charge.save(update_fields=["status", "review_message", "updated_at"])
        _audit(
            actor,
            "rent_charge.waived_after_end",
            charge.pk,
            before,
            {
                **rent_run.charge_state(charge),
                "review_message": AFTER_END_NOTE,
                "lease_end": str(end.pk),
                "refund_payments": [str(payment.pk) for payment in made],
            },
            entity_type=rent_run.ENTITY_CHARGE,
        )
        written.append(str(charge.pk))
        refunds.extend(made)
    return written, refunds


def _apply_offsets(
    end: LeaseEnd, amount: Decimal, on: date, actor: User | None, today: date
) -> list[str]:
    """Pay ``amount`` of the deposit to the lease's open charges, oldest due first, as
    settled manual payments (method ``other``, reference "deposit offset"). A payment on
    a charge with a provider invoice queues the usual marking job for the worker."""
    lease = end.lease
    remaining = amount
    written: list[str] = []
    note = _settlement_text(end, "Deposit offset against this charge")
    rows = [
        row
        for row in receivables.open_balances(today, operator=lease.operator_id)
        if row.lease_id == lease.pk and row.balance > 0
    ]
    for row in rows:
        if remaining <= 0:
            break
        pay = min(remaining, row.balance)
        if row.kind == receivables.KIND_RENT:
            rent = rent_run.record_manual_payment(
                row.charge_id,
                method=PaymentMethod.OTHER,
                amount=pay,
                paid_on=on,
                note=note,
                actor=actor,
                today=today,
                reference=OFFSET_REFERENCE,
                defer_mark=True,
            )
            if not rent.ok or rent.payment is None:
                raise _Abort(CHANGED_MEANWHILE)
            if rent.mark_due:
                charge = RentCharge.objects.get(pk=row.charge_id)
                jobs.enqueue(JobKind.PAYMENT_MARK, charge, {"payment": rent.payment.pk}, actor)
            written.append(str(rent.payment.pk))
        elif row.kind == receivables.KIND_ONE_OFF:
            one_off = one_off_charges.record_payment(
                row.charge_id,
                method=PaymentMethod.OTHER,
                amount=pay,
                paid_on=on,
                note=note,
                actor=actor,
                today=today,
                reference=OFFSET_REFERENCE,
            )
            if not one_off.ok or one_off.payment is None:
                raise _Abort(CHANGED_MEANWHILE)
            if one_off.mark_due:
                jobs.enqueue(
                    JobKind.ONE_OFF_PAYMENT_MARK,
                    OneOffCharge.objects.get(pk=row.charge_id),
                    {"payment": one_off.payment.pk},
                    actor,
                )
            written.append(str(one_off.payment.pk))
        else:
            insurance = InsuranceCharge.objects.select_for_update().get(pk=row.charge_id)
            payment = Payment.objects.create(
                insurance_charge=insurance,
                kind=PaymentKind.INSURANCE,
                policy_year=insurance.policy_year,
                method=PaymentMethod.OTHER,
                amount=pay,
                status=PaymentStatus.SETTLED,
                initiated_on=on,
                settled_on=on,
                reference=OFFSET_REFERENCE,
                note=note,
                recorded_by=actor,
            )
            _audit(
                actor,
                "insurance_charge.payment_recorded",
                insurance.pk,
                None,
                {"payment": str(payment.pk), "amount": str(pay), "method": payment.method},
                entity_type="insurance_charge",
            )
            written.append(str(payment.pk))
        remaining -= pay
    if remaining > 0:
        raise _Abort(CHANGED_MEANWHILE)
    return written


def _resolve_deposits(
    end: LeaseEnd, figures: statements.Final, on: date, actor: User | None, today: date
) -> tuple[list[str], Payment | None]:
    """One resolution per deposit held: the withheld total (the offset part first) is
    taken from the deposits in order, the rest of each is refunded."""
    withheld_left = figures.withheld
    offset_left = figures.offset_applied
    written: list[str] = []
    refund_payment: Payment | None = None
    for line in figures.deposits:
        if line.held <= 0:
            # A deposit still owed (PLAN-012b-b D11) blocks the confirm; nothing is held.
            continue
        withheld = min(line.held, withheld_left)
        offset = min(withheld, offset_left)
        refunded = line.held - withheld
        charge = OneOffCharge.objects.get(pk=line.charge_id)
        outcome = deposits.resolve_deposit_split(
            charge,
            refunded,
            withheld,
            offset=offset,
            method=_payment_method(end) if refunded > 0 else "",
            resolved_on=on,
            reference=end.refund_reference if refunded > 0 else "",
            note=_settlement_text(end, "Deposit settled"),
            actor=actor,
            today=today,
        )
        if not outcome.ok or outcome.resolution is None:
            raise _Abort(outcome.message, outcome.field)
        written.append(str(outcome.resolution.pk))
        if refunded > 0 and refund_payment is None:
            refund_payment = (
                Payment.objects.filter(one_off_charge=charge, kind=PaymentKind.REFUND_DEPOSIT)
                .order_by("-created_at")
                .first()
            )
        withheld_left -= withheld
        offset_left -= offset
    return written, refund_payment


def _refund_credits(
    end: LeaseEnd, figures: statements.Final, on: date, actor: User | None
) -> list[Payment]:
    """PLAN-012d D6b: pay back each credit held on the lease by the settlement method; what
    is held must be what the statement lists (the lease's row lock keeps it so)."""
    if credits.balance(end.lease) != figures.lease_credit:
        raise _Abort(CHANGED_MEANWHILE)
    return credits.refund_all(
        end.lease,
        method=_payment_method(end),
        on=on,
        reference=end.refund_reference,
        note=_settlement_text(end, "Credit held on the lease, paid back"),
        actor=actor,
    )


def _debt_charge(end: LeaseEnd, amount: Decimal, actor: User | None, today: date) -> OneOffCharge:
    """The lease-end balance: a manual one-off charge, nothing at the payment provider."""
    lease = end.lease
    if lease.operator is None or end.ended_on is None:
        raise _Abort(NO_OPERATOR_FOR_DEBT)
    item = one_off_charges.ChargeInput(
        fee_type=None,
        price=amount,
        cost=None,
        vendor="",
        due_date=end.ended_on + timedelta(days=DEBT_DUE_DAYS),
        note=f"Deductions above the deposit held at the end of the lease of store "
        f"{lease.store.external_label}.",
        category=OneOffCategory.OTHER,
        collection=CollectionMethod.MANUAL,
        description=DEBT_DESCRIPTION,
    )
    outcome = one_off_charges.create_charge(
        lease.operator, lease, item, actor, today=today, with_invoice=False
    )
    if not outcome.ok or outcome.charge is None:
        raise _Abort(outcome.message, outcome.field)
    return outcome.charge


def _end_reason(end: LeaseEnd) -> str:
    label = str(LeaseEndReason(end.reason).label)
    return f"{label}: {end.reason_note}" if end.reason_note else label


def _summary(figures: statements.Final) -> dict[str, Any]:
    return {
        "open_total": str(figures.open_total),
        "deposit_held": str(figures.deposit_held),
        "deductions": str(figures.deductions_total),
        "offset_applied": str(figures.offset_applied),
        "withheld": str(figures.withheld),
        "credit": str(figures.credit),
        "lease_credit": str(figures.lease_credit),
        "refund_due": str(figures.refund_due),
        "debt": str(figures.debt),
        "amount_owed": str(figures.amount_owed),
        "proration_text": figures.last_month.text if figures.last_month else "",
    }


def confirm(
    end: LeaseEnd, actor: User | None, *, confirmed: bool, today: date | None = None
) -> Outcome:
    """Step (e): end the lease with every effect of the statement, in one transaction
    (see the module docstring). Refused, with nothing written, unless ``confirmed`` is
    exactly ``True``, the draft's terms are set, nothing blocks, the statement step was
    saved (``field="statement"`` otherwise), the settlement is set
    when a refund is due and the lease end is not part of a transfer (the transfer
    confirms it, :func:`ledger.services.transfer.confirm`)."""
    if confirmed is not True:
        return _refused(CONFIRM_REQUIRED, "confirmed")
    day = today or timezone.localdate()
    try:
        with transaction.atomic():
            row = confirm_within(end, actor, day)
    except Refused as refusal:
        return _refused(refusal.message, refusal.field)
    return Outcome(True, CONFIRMED, lease_end=row)


def confirm_within(
    end: LeaseEnd, actor: User | None, today: date, *, transfer: LeaseTransfer | None = None
) -> LeaseEnd:
    """The confirm's writes, inside the caller's transaction; :class:`Refused` when a rule
    fails (the caller rolls back). ``transfer`` is the transfer confirming its own lease
    end; any other caller is refused while a transfer holds the draft."""
    try:
        return _confirm(end, actor, today, transfer)
    except _Abort as abort:
        raise Refused(abort.message, abort.field) from None
    except leases.LeaseRefused as refusal:
        raise Refused(str(refusal), refusal.field) from None
    except LeaseEnd.DoesNotExist:
        raise Refused(GONE) from None


def _confirm(
    end: LeaseEnd, actor: User | None, day: date, transfer: LeaseTransfer | None
) -> LeaseEnd:
    lease = Lease.objects.select_for_update(of=("self",)).get(pk=end.lease_id)
    row = _locked_draft(end.pk)
    held_by = draft_transfer(row)
    if held_by is not None and (transfer is None or held_by.pk != transfer.pk):
        raise _Abort(TRANSFER_CONFIRMS)
    if not terms_set(row) or row.ended_on is None:
        raise _Abort(TERMS_REQUIRED)
    if lease.ended_on is not None:
        raise _Abort(ALREADY_ENDED)
    found = blockers(lease, day)
    if found:
        raise _Abort(BLOCKED.format(count=len(found)))
    # PLAN-012f-a §10: the statement step's save created the charges the statement needs;
    # a draft never saved is refused before any write.
    if row.statement_saved_at is None:
        raise _Abort(STATEMENT_REQUIRED, "statement")
    figures = statements.final(row, today=day)
    refusal = _offset_refusal(figures)
    if refusal:
        raise _Abort(refusal)
    if figures.refund_due > 0 and (not row.refund_method or row.refund_on is None):
        raise _Abort(SETTLEMENT_REQUIRED)
    if figures.debt > 0 and lease.operator_id is None:
        raise _Abort(NO_OPERATOR_FOR_DEBT)
    on = row.refund_on or day
    written: dict[str, Any] = {}
    credit_refunds: list[Payment] = []
    if figures.last_month is not None and figures.last_month.changes:
        written["rent_charges"], credit_refunds = _apply_last_month(
            row, figures.last_month, on, actor
        )
    if figures.after_end:
        written["waived_after_end"], later_refunds = _apply_after_end(
            row, figures.after_end, on, actor
        )
        credit_refunds += later_refunds
    leases.end_lease(
        lease.pk,
        row.ended_on,
        _end_reason(row),
        actor=actor,
        today=day,
        reason=row.reason,
    )
    row.lease.refresh_from_db()
    if figures.offset_applied > 0:
        written["offset_payments"] = _apply_offsets(row, figures.offset_applied, on, actor, day)
    resolutions, refund_payment = _resolve_deposits(row, figures, on, actor, day)
    written["deposit_resolutions"] = resolutions
    if figures.lease_credit > 0:
        credit_refunds += _refund_credits(row, figures, on, actor)
        written["credit_refunds"] = [str(payment.pk) for payment in credit_refunds]
    debt = _debt_charge(row, figures.debt, actor, day) if figures.debt > 0 else None
    if refund_payment is None and credit_refunds:
        refund_payment = credit_refunds[0]
    before = _state(row)
    row.status = LeaseEndStatus.CONFIRMED
    row.confirmed_at = timezone.now()
    row.statement_json = figures.to_json()
    row.refund_payment = refund_payment
    row.debt_charge = debt
    row.save(
        update_fields=[
            "status",
            "confirmed_at",
            "statement_json",
            "refund_payment",
            "debt_charge",
            "updated_at",
        ]
    )
    _audit(
        actor,
        "lease_end.confirmed",
        row.pk,
        before,
        {
            **_state(row),
            **_summary(figures),
            **written,
            "debt_charge": str(debt.pk) if debt is not None else None,
            "refund_payment": str(refund_payment.pk) if refund_payment else None,
            **({"transfer": str(transfer.pk)} if transfer is not None else {}),
        },
    )
    jobs.enqueue(JobKind.LEASE_END_DOCUMENTS, row, None, actor)
    return row


# --- The documents job and the e-mail -----------------------------------------------------


def _adopt_stored(store: Store, data: bytes) -> Document | None:
    """The live document of the store with these bytes (a retried job's own upload)."""
    digest = hashlib.sha256(data).hexdigest()
    return Document.objects.filter(
        store=store,
        sha256=digest,
        deleted_at__isnull=True,
        kind=DocumentKind.FINAL_STATEMENT,
    ).first()


def produce_documents(end_id: uuid.UUID, actor: User | None) -> tuple[bool, str]:
    """The ``lease_end_documents`` job: render the PDF from the confirmed figures, store
    it under the store's documents, and queue its e-mail to the operator (once)."""
    end = (
        LeaseEnd.objects.select_related("lease__store", "lease__operator", "document")
        .filter(pk=end_id)
        .first()
    )
    if end is None:
        return False, GONE
    if end.status != LeaseEndStatus.CONFIRMED:
        return False, NOT_CONFIRMED
    store = end.lease.store
    figures = statements.from_json(end.statement_json)
    document = end.document
    if document is None:
        if actor is None:
            return False, STATEMENT_REFUSED.format(reason=NO_UPLOADER)
        data = statements.render_pdf(figures)
        upload = SimpleUploadedFile(
            statements.pdf_filename(figures), data, content_type="application/pdf"
        )
        try:
            stored = documents.upload(
                store,
                DocumentKind.FINAL_STATEMENT,
                upload,
                "Final statement of the lease that ended "
                f"{statements.long_date(figures.ended_on)}.",
                actor,
            )
        except PermissionDenied:
            return False, STATEMENT_REFUSED.format(reason="PermissionDenied")
        document = stored.document
        if not stored.ok and stored.code == documents.DUPLICATE:
            document = _adopt_stored(store, data)
        if document is None:
            _audit(actor, "lease_end.statement_refused", end.pk, None, {"reason": stored.code})
            return False, STATEMENT_REFUSED.format(reason=stored.message)
        LeaseEnd.objects.filter(pk=end.pk).update(document=document)
        _audit(
            actor,
            "lease_end.statement_stored",
            end.pk,
            None,
            {"document": str(document.pk), "store": str(store.pk)},
        )
    operator = end.lease.operator
    if operator is None:
        return True, DOCUMENTS_NO_OPERATOR
    template = MessageTemplate.objects.filter(code=merge.FINAL_STATEMENT_CODE).first()
    if template is None:
        return False, NO_TEMPLATE
    queued = OutboundMessage.objects.filter(
        kind=MessageKind.FINAL_STATEMENT,
        charge_entity_type=ChargeEntityType.LEASE_END,
        charge_entity_id=end.pk,
        manual=False,
    ).exists()
    if not queued:
        try:
            with transaction.atomic():
                message = messages.queue(
                    MessageKind.FINAL_STATEMENT,
                    operator,
                    template,
                    messages.MessageContext(ChargeEntityType.LEASE_END, end.pk),
                    lease=end.lease,
                    document=document,
                    actor=actor,
                )
                _audit(
                    actor,
                    "lease_end.statement_queued",
                    end.pk,
                    None,
                    {"message": str(message.pk), "document": str(document.pk)},
                )
        except IntegrityError:
            pass  # a concurrent run queued it first
    return True, DOCUMENTS_DONE


def merge_values(figures: statements.Final) -> dict[str, str]:
    return {
        "operator": figures.operator,
        "store": figures.store,
        "company": figures.company,
        "refund_due": format_amount(figures.refund_due),
        "amount_owed": format_amount(figures.amount_owed),
        "end_date": statements.long_date(figures.ended_on),
    }


def _finish(message: OutboundMessage, status: str, *, error: str = "", receipt: Any = None) -> None:
    message.status = status
    message.error = error
    if receipt is not None:
        message.mail_message_id = receipt.provider_message_id
        if status != MessageStatus.SUPPRESSED:
            message.sent_at = receipt.sent_at
    message.save()
    action = {
        MessageStatus.SENT: "lease_end.statement_sent",
        MessageStatus.CATCH_ALL: "lease_end.statement_sent",
        MessageStatus.SUPPRESSED: "lease_end.statement_suppressed",
    }.get(MessageStatus(status), "lease_end.statement_failed")
    _audit(
        None,
        action,
        message.pk,
        None,
        {
            "lease_end": str(message.charge_entity_id),
            "document": str(message.document_id) if message.document_id else None,
            "status": status,
            "error": error,
        },
        entity_type=ENTITY_MESSAGE,
    )


def send_statement(message_id: uuid.UUID) -> messages.Sent:
    """Deliver one queued final statement e-mail with its PDF attached."""
    from ledger.services.collections import NOT_QUEUED, html_part, one_line

    with transaction.atomic():
        message = (
            OutboundMessage.objects.select_for_update(of=("self",))
            .select_related("operator", "lease", "template", "document", "sent_by")
            .filter(pk=message_id)
            .first()
        )
        if message is None:
            return messages.Sent(False, GONE)
        if message.status != MessageStatus.QUEUED:
            return messages.Sent(
                True, NOT_QUEUED.format(status=message.get_status_display().lower())
            )
        end = LeaseEnd.objects.filter(pk=message.charge_entity_id).first()
        operator = message.operator
        reason = ""
        if end is None or end.status != LeaseEndStatus.CONFIRMED or message.document is None:
            reason = "StatementMissing"
        elif operator.archived_at is not None:
            reason = "OperatorArchived"
        elif not (message.lease is not None and lease_sync.billing_of(message.lease).email):
            # PLAN-014 C5: the statement goes to the ended lease's billing e-mail.
            reason = "NoAddress"
        if reason or end is None or message.document is None:
            _finish(message, MessageStatus.SUPPRESSED, error=reason or "StatementMissing")
            return messages.Sent(True, f"Not sent ({reason or 'StatementMissing'}).")
        figures = statements.from_json(end.statement_json)
        values = merge_values(figures)
        try:
            subject = one_line(merge.render(message.template.subject, values))
            body = merge.render(message.template.body, values)
        except merge.TemplateRefused:
            _finish(message, MessageStatus.FAILED, error="TemplateRefused")
            return messages.Sent(False, "Failed: TemplateRefused.")
        address = lease_sync.billing_of(message.lease).email if message.lease is not None else ""
        message.to_address = address
        message.subject = subject[:SUBJECT_MAX_LENGTH]
        message.body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        try:
            message.check_payment_data()
        except ValidationError:
            message.subject = ""
            _finish(message, MessageStatus.FAILED, error="PaymentDataRefused")
            return messages.Sent(False, "Failed: PaymentDataRefused.")
        try:
            sender = mail.get_sender()
        except mail.MailUnavailable as error:
            _finish(message, MessageStatus.FAILED, error=error.cause)
            return messages.Sent(False, f"Failed: {error.cause}.")
        reader = message.sent_by
        if reader is None:
            _finish(message, MessageStatus.FAILED, error="NoReader")
            return messages.Sent(False, "Failed: NoReader.")
        try:
            data = documents.read_all(message.document, reader)
        except (StorageUnavailable, PermissionDenied) as error:
            name = type(error).__name__
            _finish(message, MessageStatus.FAILED, error=name)
            return messages.Sent(False, f"Failed: {name}.")
        try:
            outgoing = mail.Message(
                to=address,
                subject=message.subject,
                text=body,
                html=html_part(body),
                attachments=(
                    mail.Attachment(
                        statements.pdf_filename(figures), message.document.content_type, data
                    ),
                ),
            )
            receipt = sender.send(outgoing)
        except mail.MailUnavailable as error:
            _finish(message, MessageStatus.FAILED, error=error.cause)
            return messages.Sent(False, f"Failed: {error.cause}.")
        except ValueError:
            _finish(message, MessageStatus.FAILED, error="MessageRefused")
            return messages.Sent(False, "Failed: MessageRefused.")
        status = MessageStatus(receipt.disposition)
        _finish(message, status, receipt=receipt)
        texts = {
            MessageStatus.SENT: "Sent.",
            MessageStatus.CATCH_ALL: "Sent to the test inbox.",
            MessageStatus.SUPPRESSED: "Not sent: mail is off.",
        }
        return messages.Sent(True, texts[status])


def statement_log(end: LeaseEnd) -> list[OutboundMessage]:
    return list(
        OutboundMessage.objects.filter(
            kind=MessageKind.FINAL_STATEMENT,
            charge_entity_type=ChargeEntityType.LEASE_END,
            charge_entity_id=end.pk,
        ).order_by("-queued_at")
    )
