"""The monthly rent run: charges, invoice matching, manual payments (PLAN-007 7b).

Rules kept here:

- **Who is due** (:func:`leases_due`): a lease whose debit day (or second debit day) is
  the given date; a day past the end of a short month falls on its last day. Only
  ``collection_status = active`` leases with a known channel that have started are
  considered; every other lease due that day is listed as skipped with a reason.
- **Charges are idempotent** (D6): ``rent_charges`` is unique on
  ``(lease, period_month, seq)`` and each charge is made by ``get_or_create`` in its own
  transaction, so a second run, or two runs at once, create nothing new. The amount is
  the rent in effect on the due date, halved for a twice-monthly lease with the odd
  cent on the first debit.
- **Matching** (D7): one ``list_invoices`` call per active rent schedule with a provider
  reference. An invoice the provider generated from that schedule, dated inside a
  charge's month, is linked to the charge of that schedule's ``seq``: a local
  ``invoices`` row numbered by :mod:`ledger.services.numbering`, its ``external_refs``
  row, and the provider's status written through :mod:`payments.invoice_sync` (the only
  path to ``provider_status``). A total that differs from ``amount_due`` flags the
  charge and never changes ``amount_due`` (DATA_OWNERSHIP §3). An invoice already
  linked is skipped; one whose date or schedule cannot be read is reported, never
  guessed. Matching is refused, with no provider call, while the breaker is open or the
  failure budget is exhausted.
- **Manual payments** (D8): Zelle, wire or other; amount above zero and at most the
  balance; date not in the future; a note; an optional reference. The payment is
  written first, in its own transaction, and ``amount_paid`` is recomputed from settled
  payments with the status following.
- **Marking the provider invoice** (PLAN-009 D1 to D3): when the charge has a provider
  invoice and the payment covers the charge's whole amount (nothing was paid before),
  the invoice is re-fetched under the charge's row lock; one the provider already shows
  paid is not marked again (``payment.provider_already_paid``, charge flagged);
  otherwise it is marked paid as cash with the memo of :mod:`payments.offline_marking`,
  re-fetched, and its state copied through :mod:`payments.invoice_sync`. A provider
  failure keeps the payment and flags the charge with the failure class and field
  names; :func:`retry_provider_mark` is the manual retry, refused for
  ``RETRY_LOCK_SECONDS`` after the last attempt. A partial payment, or one that differs
  from the invoice total, makes no provider call and flags the charge.
- **Linking an invoice to a charge** (PLAN-008a §2 rules 3, 4): an invoice the
  provider did not generate from a schedule is linked automatically only when exactly
  one open charge of its operator (due, unpaid or partial, balance above zero, no
  invoice yet) has an amount due equal to its total and a period that is the month of
  its date; otherwise staff link it from the charge page. Both set the invoice's
  ``rent_charge`` under row locks on the charge, then the invoice; a charge has at most
  one invoice (a unique partial index on ``invoices.rent_charge``). A manual link whose
  total differs from the balance needs the confirmation and a note, and flags the
  charge.
- **Carried balance** (PLAN-009 D5): the open balances of a lease's earlier charges are
  shown with each due debit and on the charge page; a new charge's amount never
  includes them.

Every change writes ``audit_log``; provider identifiers in it are masked. Money is
``Decimal`` throughout.
"""

from __future__ import annotations

import calendar
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet, Sum
from django.utils import timezone

import payments
from ledger.models import (
    ZERO,
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    Invoice,
    Lease,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
)
from ledger.services import numbering
from ledger.services.leases import (
    ENTITY_SCHEDULE,
    KIND_SCHEDULE,
    PROVIDER_KEY,
    debit_amounts,
    exceeds_provider_limit,
    is_large_debit,
    rent_on,
    schedule_ref,
)
from ledger.validators import validate_no_payment_data
from payments import invoice_sync, offline_marking, operator_sync
from payments.errors import ProviderError
from payments.health import provider_health
from payments.models import ExternalRef
from payments.provider import InvoiceQuery, InvoiceRecord, mask_identifier

ENTITY_CHARGE = "rent_charge"
ENTITY_INVOICE = "invoice"
ENTITY_PAYMENT = "payment"
KIND_INVOICE = "invoice"
CENT = Decimal("0.01")
#: Largest amount ``numeric(12,2)`` holds.
MONEY_MAX = Decimal("9999999999.99")
MANUAL_METHODS: tuple[str, ...] = (PaymentMethod.ZELLE, PaymentMethod.WIRE, PaymentMethod.OTHER)

# --- Fixed sentences ------------------------------------------------------------------

SKIP_PAUSED = "Collection is paused."
SKIP_PROSPECTIVE = "The lease is prospective."
SKIP_ENDED = "The lease has ended."
SKIP_UNKNOWN_CHANNEL = "The payment channel is unknown."
SKIP_NOT_STARTED = "The lease starts after this date."
SKIP_NO_RENT = "No rent is in effect on this date."
_STATUS_SKIPS: dict[str, str] = {
    CollectionStatus.PAUSED: SKIP_PAUSED,
    CollectionStatus.PROSPECTIVE: SKIP_PROSPECTIVE,
    CollectionStatus.ENDED: SKIP_ENDED,
}

MATCH_ALREADY_LINKED = "Already linked to a charge."
MATCH_OTHER_SCHEDULE = "The invoice does not name this schedule as its source; not linked."
MATCH_NO_DATE = "The invoice date cannot be determined; not linked."
MATCH_NO_TOTAL = "The invoice has no usable total; not linked."
MATCH_NO_CHARGE = "No rent charge exists for {period} debit {seq}; create the charges first."
MATCH_CHARGE_INVOICED = (
    "The charge for {period} debit {seq} already has an invoice; not linked. "
    "The charge is flagged for review."
)
MATCH_NO_OPERATOR = "The lease has no operator; not linked."
MATCH_OTHER_OPERATOR = "The invoice names another operator's customer; not linked."
MATCH_LINKED = "Linked to the charge for {period} debit {seq} as {number}."
MATCH_MISMATCH = (
    "Linked to the charge for {period} debit {seq} as {number}. The invoice total at the payment "
    "provider differs from the amount due; collection on this charge stops until staff resolve "
    "it."
)
MATCH_FAILED = "The payment provider could not list the invoices of a schedule. Matching stopped."
TOTAL_MISMATCH = (
    "The invoice total at the payment provider differs from the amount due. Collection on this "
    "charge stops until staff resolve it; the amount due was not changed."
)
SECOND_INVOICE = (
    "The payment provider generated more than one invoice for this charge. Only the first is "
    "linked; review the others at the payment provider."
)

PAY_METHOD = "Choose Zelle, wire or other."
PAY_NOT_POSITIVE = "The amount must be more than zero."
PAY_NOT_CENTS = "The amount must have at most two decimals."
PAY_OVER_BALANCE = "The amount is more than the balance due. Nothing was recorded."
PAY_FUTURE = "The payment date cannot be in the future."
PAY_NOTE_REQUIRED = "A note is required: say what was received."
PAY_NOTE_PAYMENT_DATA = "This text may not contain card or account numbers."
PAY_WAIVED = "This charge is waived. Nothing was recorded."
PAY_NOTHING_DUE = "Nothing is due on this charge. Nothing was recorded."
PAY_ALREADY = "This payment was already recorded. Nothing was changed."
PAY_RECORDED = "Payment recorded."

#: Audit actions of the provider marking (PLAN-009 D1 to D3).
MARKED_ACTION = "rent_charge.provider_marked"
MARK_FAILED_ACTION = "rent_charge.provider_mark_failed"
NOT_UPDATED_ACTION = "rent_charge.provider_not_updated"
ALREADY_PAID_ACTION = "payment.provider_already_paid"
_CHARGE_MARK_ACTIONS = (MARKED_ACTION, MARK_FAILED_ACTION, NOT_UPDATED_ACTION)
#: Other reasons a charge is flagged; clearing a marking flag keeps the charge flagged
#: when one of them was ever recorded.
LINKED_TOTAL_DIFFERS_ACTION = "rent_charge.linked_total_differs"
_OTHER_FLAG_ACTIONS = (
    LINKED_TOTAL_DIFFERS_ACTION,
    "rent_charge.second_invoice",
    "rent_charge.total_mismatch",
    "rent_charge.disputed",
)
#: Where a marking attempt stopped.
STAGE_CHECK = "check"
STAGE_MARK = "mark"
STAGE_REREAD = "reread"

MARK_DONE = "The invoice at the payment provider is marked paid."
MARK_RETRY_HINT = " The payment is recorded; retry from the charge page."
MARK_NOTHING_TO_RETRY = "Nothing to retry on this charge."

#: Charge statuses an invoice may be linked to automatically (PLAN-008a §2 rule 3).
AUTO_LINK_STATUSES: tuple[str, ...] = (
    RentChargeStatus.DUE,
    RentChargeStatus.UNPAID,
    RentChargeStatus.PARTIAL,
)
LINKED_TOTAL_DIFFERS = "Linked invoice total differs from the balance."
LINK_CHOOSE = "Choose an unlinked invoice of this charge's operator. Nothing was linked."
LINK_CHARGE_HAS_INVOICE = "This charge already has an invoice. Nothing was linked."
LINK_CONFIRM = (
    "The invoice total differs from the balance: tick the confirmation to link it anyway. "
    "Nothing was linked."
)
LINK_NOTE_REQUIRED = (
    "A note is required when the total differs from the balance: say why. Nothing was linked."
)
LINK_SAME = "This invoice is already linked to this charge. Nothing was changed."
LINK_DONE = "Invoice {number} linked to this charge."
LINK_DONE_DIFFERS = (
    "Invoice {number} linked to this charge. Its total differs from the balance; the charge "
    "is flagged for review."
)


# --- Results --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DueDebit:
    lease: Lease
    seq: int
    debit_day: int
    amount: Decimal
    #: Above ``LARGE_DEBIT_WARNING_AMOUNT`` (a placeholder until the provider answers Q25).
    large: bool
    #: Above ``PROVIDER_MAX_TXN_AMOUNT`` when set (PLAN-007c); the charge is created anyway.
    over_limit: bool
    #: This period's charge, when already created.
    charge: RentCharge | None
    #: The same debit's charge of the previous month, if any.
    last_month: RentCharge | None
    #: Open balances of the lease's earlier charges (PLAN-009 D5); not part of ``amount``.
    carried_balance: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class SkippedLease:
    lease: Lease
    reason: str


@dataclass(frozen=True, slots=True)
class RunDay:
    day: date
    due: tuple[DueDebit, ...]
    skipped: tuple[SkippedLease, ...]

    @property
    def period(self) -> date:
        return self.day.replace(day=1)


@dataclass(frozen=True, slots=True)
class ChargeRunReport:
    created: tuple[RentCharge, ...]
    already: tuple[RentCharge, ...]
    skipped: tuple[SkippedLease, ...]


class MarkAction(StrEnum):
    """What happened at the payment provider after a manual payment (PLAN-009 D1 to D3)."""

    NO_INVOICE = "no_invoice"
    NOT_UPDATED = "not_updated"
    MARKED = "marked"
    ALREADY_PAID = "already_paid"
    FAILED = "failed"
    RETRY_TOO_SOON = "retry_too_soon"
    NOTHING_TO_RETRY = "nothing_to_retry"


@dataclass(frozen=True, slots=True)
class MarkResult:
    action: MarkAction
    #: A fixed sentence for staff that never names the provider; empty for ``NO_INVOICE``.
    message: str = ""
    retry_in: int = 0

    @property
    def ok(self) -> bool:
        return self.action in (MarkAction.NO_INVOICE, MarkAction.MARKED)


class MatchAction(StrEnum):
    LINKED = "linked"
    MISMATCH = "mismatch"
    ALREADY = "already"
    REPORTED = "reported"


@dataclass(frozen=True, slots=True)
class MatchLine:
    action: MatchAction
    #: The provider's invoice ID, masked.
    invoice: str
    message: str


@dataclass(slots=True)
class MatchReport:
    #: Fixed sentences when matching was refused before any provider call.
    refused: tuple[str, ...] = ()
    lines: list[MatchLine] = field(default_factory=list)
    failure: str = ""
    schedules: int = 0

    def count(self, action: MatchAction) -> int:
        return sum(1 for line in self.lines if line.action is action)


@dataclass(frozen=True, slots=True)
class LinkOutcome:
    ok: bool
    #: A fixed sentence for staff that never names the provider.
    message: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class UnlinkedInvoice:
    """One row of the manual link list: the provider ID is shown masked only."""

    invoice: Invoice
    provider_invoice: str


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    ok: bool
    message: str
    field: str | None = None
    payment: Payment | None = None
    #: True when the form's key had already recorded a payment (a resubmission).
    replayed: bool = False
    #: What happened at the payment provider afterwards; ``None`` when nothing was saved.
    provider: MarkResult | None = None


# --- Who is due -----------------------------------------------------------------------


def falls_on(debit_day: int, year: int, month: int) -> int:
    """The calendar day a debit day falls on in a month (29 to 31 move to the last day)."""
    return min(debit_day, calendar.monthrange(year, month)[1])


def leases_due(day: date) -> RunDay:
    """The debits due on ``day`` and the leases due that day that are skipped."""
    last = calendar.monthrange(day.year, day.month)[1]
    days = [day.day] if day.day < last else list(range(day.day, 32))
    leases = (
        Lease.objects.select_related("store", "operator")
        .filter(Q(debit_day__in=days) | Q(second_debit_day__in=days))
        .order_by("store__external_label", "id")
    )
    period = day.replace(day=1)
    previous = (period - timedelta(days=1)).replace(day=1)
    due: list[DueDebit] = []
    skipped: list[SkippedLease] = []
    for lease in leases:
        reason = _skip_reason(lease, day)
        rent = rent_on(lease, day) if not reason else None
        if not reason and rent is None:
            reason = SKIP_NO_RENT
        if reason or rent is None:
            skipped.append(SkippedLease(lease, reason or SKIP_NO_RENT))
            continue
        debit_days = [d for d in (lease.debit_day, lease.second_debit_day) if d is not None]
        amounts = debit_amounts(rent.amount, twice_monthly=len(debit_days) == 2)
        charges = {
            (charge.period_month, charge.seq): charge
            for charge in lease.rent_charges.filter(period_month__in=[period, previous])
        }
        for seq, debit_day in enumerate(debit_days, start=1):
            if falls_on(debit_day, day.year, day.month) != day.day:
                continue
            amount = amounts[seq - 1]
            due.append(
                DueDebit(
                    lease=lease,
                    seq=seq,
                    debit_day=debit_day,
                    amount=amount,
                    large=is_large_debit(amount),
                    over_limit=exceeds_provider_limit(amount),
                    charge=charges.get((period, seq)),
                    last_month=charges.get((previous, seq)),
                    carried_balance=_carried(lease.pk, period, seq),
                )
            )
    return RunDay(day, tuple(due), tuple(skipped))


def carried_balance(charge: RentCharge) -> Decimal:
    """The open balances of the lease's charges before this one (PLAN-009 D5)."""
    return _carried(charge.lease_id, charge.period_month, charge.seq)


def _carried(lease_id: uuid.UUID, period: date, seq: int) -> Decimal:
    """Sum of the balances above zero of the lease's charges before ``(period, seq)``; a
    waived charge is not owed and is left out."""
    total = (
        RentCharge.objects.filter(lease_id=lease_id, balance__gt=0)
        .filter(Q(period_month__lt=period) | Q(period_month=period, seq__lt=seq))
        .exclude(status=RentChargeStatus.WAIVED)
        .aggregate(total=Sum("balance"))["total"]
    )
    return Decimal(total or ZERO).quantize(CENT)


def _skip_reason(lease: Lease, day: date) -> str:
    if lease.collection_status != CollectionStatus.ACTIVE:
        return _STATUS_SKIPS.get(lease.collection_status, SKIP_ENDED)
    if lease.payment_channel == PaymentChannel.UNKNOWN:
        return SKIP_UNKNOWN_CHANNEL
    if lease.started_on is not None and lease.started_on > day:
        return SKIP_NOT_STARTED
    return ""


# --- Charges --------------------------------------------------------------------------


def create_charges(day: date, *, actor: User | None) -> ChargeRunReport:
    """One charge per due debit, idempotently (D6)."""
    plan = leases_due(day)
    created: list[RentCharge] = []
    already: list[RentCharge] = []
    for item in plan.due:
        with transaction.atomic():
            charge, made = RentCharge.objects.get_or_create(
                lease=item.lease,
                period_month=plan.period,
                seq=item.seq,
                defaults={
                    "due_on": day,
                    "amount_due": item.amount,
                    "status": RentChargeStatus.DUE,
                },
            )
            if made:
                _audit(
                    actor, "rent_charge.created", ENTITY_CHARGE, charge.pk, None, _charge(charge)
                )
        (created if made else already).append(charge)
    return ChargeRunReport(tuple(created), tuple(already), plan.skipped)


# --- Matching -------------------------------------------------------------------------


def match_schedule_invoices(*, actor: User | None) -> MatchReport:
    """Link provider invoices generated by active rent schedules to their charges (D7)."""
    health = provider_health()
    if not health.ok:
        return MatchReport(refused=health.messages)
    report = MatchReport()
    rows = (
        CollectionSchedule.objects.filter(kind=ScheduleKind.RENT, state=ScheduleState.ACTIVE)
        .select_related("lease", "lease__operator")
        .order_by("lease__store__external_label", "lease_id", "seq")
    )
    linked = [(row, ref) for row in rows if (ref := schedule_ref(row))]
    if not linked:
        return report
    provider = payments.get_provider()
    for row, ref in linked:
        try:
            records = provider.list_invoices(InvoiceQuery(schedule_external_id=ref))
        except ProviderError as error:
            report.failure = MATCH_FAILED
            _audit(
                actor,
                "rent_run.match_failed",
                "collection_schedule",
                row.pk,
                None,
                {
                    "schedule": mask_identifier(ref),
                    "failure": operator_sync.failure_name(error),
                },
            )
            return report
        report.schedules += 1
        for record in records:
            report.lines.append(_match_one(row, ref, record, actor))
    return report


def match_invoice(
    record: InvoiceRecord, operator_id: uuid.UUID, *, actor: User | None
) -> MatchLine | None:
    """Link one provider invoice by the matching rules above, when the schedule it names
    is an active rent schedule of ours; ``None`` when it names none (PLAN-008 D4).

    ``operator_id`` is the operator whose customer the invoice names: an invoice is never
    linked to a schedule of another operator's lease. The caller has already fetched the
    invoice; nothing here calls the provider.
    """
    ref = invoice_sync.schedule_id(record)
    if not ref:
        return None
    schedule_ids = ExternalRef.objects.filter(
        entity_type=ENTITY_SCHEDULE,
        provider=PROVIDER_KEY,
        external_kind=KIND_SCHEDULE,
        external_id=ref,
        superseded_at__isnull=True,
    ).values("entity_id")
    row = (
        CollectionSchedule.objects.filter(
            pk__in=schedule_ids, kind=ScheduleKind.RENT, state=ScheduleState.ACTIVE
        )
        .select_related("lease", "lease__operator")
        .first()
    )
    if row is None:
        return None
    if row.lease.operator_id != operator_id:
        return MatchLine(
            MatchAction.REPORTED, mask_identifier(record.external_id), MATCH_OTHER_OPERATOR
        )
    return _match_one(row, ref, record, actor)


def _match_one(
    row: CollectionSchedule, ref: str, record: InvoiceRecord, actor: User | None
) -> MatchLine:
    masked = mask_identifier(record.external_id)

    def line(action: MatchAction, message: str) -> MatchLine:
        return MatchLine(action, masked, message)

    if _invoice_linked(record.external_id):
        return line(MatchAction.ALREADY, MATCH_ALREADY_LINKED)
    if invoice_sync.schedule_id(record) != ref:
        return line(MatchAction.REPORTED, MATCH_OTHER_SCHEDULE)
    issued = invoice_sync.invoice_date(record)
    if issued is None:
        return line(MatchAction.REPORTED, MATCH_NO_DATE)
    total = record.total
    if total is None or not ZERO <= total <= MONEY_MAX or total != total.quantize(CENT):
        return line(MatchAction.REPORTED, MATCH_NO_TOTAL)
    period = issued.replace(day=1)
    words = {"period": f"{period:%Y-%m}", "seq": row.seq}
    try:
        with transaction.atomic():
            charge = (
                RentCharge.objects.select_for_update()
                .filter(lease_id=row.lease_id, period_month=period, seq=row.seq)
                .first()
            )
            if charge is None:
                return line(MatchAction.REPORTED, MATCH_NO_CHARGE.format(**words))
            if _invoice_linked(record.external_id):
                return line(MatchAction.ALREADY, MATCH_ALREADY_LINKED)
            if charge.invoices.exists():
                if not charge.needs_review:  # flagged once; later runs only report it
                    _flag(charge, actor, "rent_charge.second_invoice", {"invoice": masked})
                return line(MatchAction.REPORTED, MATCH_CHARGE_INVOICED.format(**words))
            operator = row.lease.operator
            if operator is None:
                return line(MatchAction.REPORTED, MATCH_NO_OPERATOR)
            due = invoice_sync.due_date(record)
            invoice = Invoice.objects.create(
                invoice_number=numbering.next_invoice_number(issued.year),
                operator=operator,
                rent_charge=charge,
                issue_date=issued,
                due_date=due if due is not None and due >= issued else issued,
                subtotal=total,
                tax=ZERO,
                total=total,
                allow_card=False,
                allow_ach=True,
                creation_key=uuid.uuid4(),
            )
            ExternalRef.objects.create(
                entity_type=ENTITY_INVOICE,
                entity_id=invoice.pk,
                provider=PROVIDER_KEY,
                external_kind=KIND_INVOICE,
                external_id=record.external_id,
            )
            invoice_sync.record_provider_status(invoice.pk, record.raw_status)
            words["number"] = invoice.invoice_number
            _audit(
                actor,
                "invoice.matched",
                ENTITY_INVOICE,
                invoice.pk,
                None,
                {
                    "invoice_number": invoice.invoice_number,
                    "rent_charge": str(charge.pk),
                    "period": words["period"],
                    "seq": row.seq,
                    "total": str(total),
                    "provider_invoice": masked,
                    "schedule": mask_identifier(ref),
                },
            )
            if total != charge.amount_due:
                _flag(
                    charge,
                    actor,
                    "rent_charge.total_mismatch",
                    {
                        "amount_due": str(charge.amount_due),
                        "provider_total": str(total),
                        "invoice_number": invoice.invoice_number,
                    },
                )
                return line(MatchAction.MISMATCH, MATCH_MISMATCH.format(**words))
    except IntegrityError:
        # Another run linked the same provider invoice first; everything here rolled back.
        return line(MatchAction.ALREADY, MATCH_ALREADY_LINKED)
    return line(MatchAction.LINKED, MATCH_LINKED.format(**words))


def charge_review_message(charge: RentCharge) -> str:
    """Why a flagged charge needs review, or ``""`` when it is not flagged."""
    if not charge.needs_review:
        return ""
    parts = []
    if charge.invoices.exclude(total=charge.amount_due).exists():
        parts.append(TOTAL_MISMATCH)
    if AuditLog.objects.filter(
        entity_type=ENTITY_CHARGE, entity_id=str(charge.pk), action=LINKED_TOTAL_DIFFERS_ACTION
    ).exists():
        parts.append(LINKED_TOTAL_DIFFERS)
    if charge.review_message:
        parts.append(f"{offline_marking.shown(charge.review_message)}.")
    return " ".join(parts) or SECOND_INVOICE


def _invoice_linked(external_id: str) -> bool:
    return ExternalRef.objects.filter(
        provider=PROVIDER_KEY, external_kind=KIND_INVOICE, external_id=external_id
    ).exists()


def _flag(charge: RentCharge, actor: User | None, action: str, context: dict[str, Any]) -> None:
    before = _charge(charge)
    charge.needs_review = True
    charge.save(update_fields=["needs_review", "updated_at"])
    _audit(actor, action, ENTITY_CHARGE, charge.pk, before, {**_charge(charge), **context})


# --- Linking an invoice to a charge (PLAN-008a §2 rules 3, 4) ---------------------------


def _unlinked(queryset: QuerySet[Invoice]) -> QuerySet[Invoice]:
    return queryset.filter(
        rent_charge__isnull=True, insurance_charge__isnull=True, charge_adjustment__isnull=True
    )


def _provider_ref(invoice_id: uuid.UUID) -> str:
    return (
        ExternalRef.objects.filter(
            entity_type=ENTITY_INVOICE,
            entity_id=invoice_id,
            provider=PROVIDER_KEY,
            external_kind=KIND_INVOICE,
            superseded_at__isnull=True,
        )
        .values_list("external_id", flat=True)
        .first()
        or ""
    )


def _operator_of(charge: RentCharge) -> uuid.UUID | None:
    return Lease.objects.only("operator_id").get(pk=charge.lease_id).operator_id


def unlinked_invoices(charge: RentCharge) -> list[UnlinkedInvoice]:
    """The local invoices of the charge's operator that have no charge, newest first."""
    operator_id = _operator_of(charge)
    if operator_id is None:
        return []
    rows = _unlinked(Invoice.objects.filter(operator_id=operator_id)).order_by(
        "-issue_date", "-invoice_number"
    )
    return [UnlinkedInvoice(row, mask_identifier(_provider_ref(row.pk))) for row in rows]


def _auto_candidates(operator_id: uuid.UUID, total: Decimal, issued: date) -> QuerySet[RentCharge]:
    return RentCharge.objects.filter(
        lease__operator_id=operator_id,
        status__in=AUTO_LINK_STATUSES,
        balance__gt=0,
        amount_due=total,
        period_month=issued.replace(day=1),
        invoices__isnull=True,
    )


def _attach(charge: RentCharge, invoice: Invoice) -> None:
    """Set the invoice's charge. The caller holds the locks on both rows; a charge that
    already has an invoice is refused by the unique index with an IntegrityError."""
    Invoice.objects.filter(pk=invoice.pk, rent_charge__isnull=True).update(
        rent_charge=charge, updated_at=timezone.now()
    )


def auto_link_invoice(
    invoice_id: uuid.UUID, total: Decimal | None, issued: date | None
) -> tuple[bool, int]:
    """Link an unlinked invoice to the one open charge it fits (PLAN-008a §2 rule 3).

    ``total`` and ``issued`` are the re-fetched invoice's total and date; the caller has
    checked that the invoice names no schedule. Returns whether the invoice is now
    linked and the number of candidate charges found; nothing is written unless there
    is exactly one.
    """
    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return False, 0
    if invoice.rent_charge_id is not None:
        return True, 1
    if total is None or issued is None:
        return False, 0
    candidates = _auto_candidates(invoice.operator_id, total, issued)
    found = list(candidates.values_list("pk", flat=True)[:2])
    if len(found) != 1:
        return False, candidates.count() if found else 0
    try:
        with transaction.atomic():
            charge = RentCharge.objects.select_for_update().filter(pk=found[0]).first()
            locked = _unlinked(Invoice.objects.select_for_update().filter(pk=invoice_id)).first()
            if (
                charge is None
                or locked is None
                or not candidates.filter(pk=charge.pk).exists()
                or charge.invoices.exists()
            ):
                return _linked(invoice_id), 1
            _attach(charge, locked)
            _audit(
                None,
                "invoice.auto_linked",
                ENTITY_INVOICE,
                locked.pk,
                {"rent_charge": None},
                {
                    "invoice_number": locked.invoice_number,
                    "rent_charge": str(charge.pk),
                    "period": f"{charge.period_month:%Y-%m}",
                    "seq": charge.seq,
                    "total": str(total),
                    "provider_invoice": mask_identifier(_provider_ref(locked.pk)),
                },
            )
    except IntegrityError:
        # Another invoice was linked to that charge first; ours rolled back.
        return False, 1
    return True, 1


def _linked(invoice_id: uuid.UUID) -> bool:
    return Invoice.objects.filter(pk=invoice_id, rent_charge__isnull=False).exists()


def link_invoice(
    charge_id: uuid.UUID,
    invoice_id: uuid.UUID,
    *,
    confirm: bool,
    note: str,
    actor: User | None,
) -> LinkOutcome:
    """Staff link an unlinked invoice of the charge's operator to the charge (rule 4).

    Every rule is checked under the row locks, charge first, then invoice. A total that
    differs from the charge's balance needs ``confirm`` and a note, and flags the charge.
    """
    text = (note or "").strip()
    if text:
        try:
            validate_no_payment_data(text)
        except ValidationError:
            return LinkOutcome(False, PAY_NOTE_PAYMENT_DATA, "note")
    try:
        with transaction.atomic():
            charge = RentCharge.objects.select_for_update().get(pk=charge_id)
            operator_id = _operator_of(charge)
            invoice = (
                Invoice.objects.select_for_update()
                .filter(pk=invoice_id, operator_id=operator_id)
                .first()
                if operator_id is not None
                else None
            )
            if invoice is None:
                return LinkOutcome(False, LINK_CHOOSE, "invoice")
            if invoice.rent_charge_id == charge.pk:
                return LinkOutcome(True, LINK_SAME)
            if (
                invoice.rent_charge_id is not None
                or invoice.insurance_charge_id is not None
                or invoice.charge_adjustment_id is not None
            ):
                return LinkOutcome(False, LINK_CHOOSE, "invoice")
            if charge.invoices.exists():
                return LinkOutcome(False, LINK_CHARGE_HAS_INVOICE)
            differs = invoice.total != charge.balance
            if differs and not confirm:
                return LinkOutcome(False, LINK_CONFIRM, "confirm")
            if differs and not text:
                return LinkOutcome(False, LINK_NOTE_REQUIRED, "note")
            _attach(charge, invoice)
            context = {
                "invoice_number": invoice.invoice_number,
                "invoice_total": str(invoice.total),
                "charge_balance": str(charge.balance),
            }
            _audit(
                actor,
                "invoice.linked",
                ENTITY_INVOICE,
                invoice.pk,
                {"rent_charge": None},
                {
                    **context,
                    "rent_charge": str(charge.pk),
                    "amount_due": str(charge.amount_due),
                    "provider_invoice": mask_identifier(_provider_ref(invoice.pk)),
                    "total_differs": differs,
                    "note": text,
                },
            )
            if differs:
                _flag(charge, actor, LINKED_TOTAL_DIFFERS_ACTION, context)
            template = LINK_DONE_DIFFERS if differs else LINK_DONE
    except IntegrityError:
        # Another link to this charge committed first; ours rolled back.
        return LinkOutcome(False, LINK_CHARGE_HAS_INVOICE)
    return LinkOutcome(True, template.format(number=invoice.invoice_number))


# --- Manual payments ------------------------------------------------------------------


def record_manual_payment(
    charge_id: uuid.UUID,
    *,
    method: str,
    amount: Decimal,
    paid_on: date,
    note: str,
    actor: User | None,
    creation_key: uuid.UUID | None = None,
    today: date | None = None,
    reference: str = "",
) -> PaymentOutcome:
    """Record a Zelle, wire or other payment against a rent charge (D8), then update the
    provider invoice as the module docstring says (PLAN-009 D1 to D3).

    ``creation_key`` is the one-time key of the form; it is kept in the audit row and
    looked up under the charge's row lock, so a resubmitted form records nothing and
    calls nothing.
    """
    text = (note or "").strip()
    reference = (reference or "").strip()
    if method not in MANUAL_METHODS:
        return PaymentOutcome(False, PAY_METHOD, "method")
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        return PaymentOutcome(False, PAY_NOT_POSITIVE, "amount")
    if amount != amount.quantize(CENT):
        return PaymentOutcome(False, PAY_NOT_CENTS, "amount")
    if paid_on > (today or timezone.localdate()):
        return PaymentOutcome(False, PAY_FUTURE, "paid_on")
    if not text:
        return PaymentOutcome(False, PAY_NOTE_REQUIRED, "note")
    with transaction.atomic():
        charge = RentCharge.objects.select_for_update().get(pk=charge_id)
        if creation_key is not None and _payment_for_key(charge, creation_key):
            return PaymentOutcome(True, PAY_ALREADY, replayed=True)
        if charge.status == RentChargeStatus.WAIVED:
            return PaymentOutcome(False, PAY_WAIVED)
        balance = charge.amount_due - _settled(charge)
        if balance <= 0:
            return PaymentOutcome(False, PAY_NOTHING_DUE)
        if amount > balance:
            return PaymentOutcome(False, PAY_OVER_BALANCE, "amount")
        whole = balance == charge.amount_due and amount == balance
        before = _charge(charge)
        payment = Payment(
            rent_charge=charge,
            kind=PaymentKind.RENT,
            method=method,
            amount=amount,
            status=PaymentStatus.SETTLED,
            initiated_on=paid_on,
            settled_on=paid_on,
            reference=reference,
            note=text,
            recorded_by=actor,
        )
        try:
            payment.save()
        except ValidationError:
            # The card-number guard: the transaction is rolled back with nothing saved.
            transaction.set_rollback(True)
            return PaymentOutcome(False, PAY_NOTE_PAYMENT_DATA, "note")
        charge.amount_paid = _settled(charge)
        charge.status = (
            RentChargeStatus.PAID
            if charge.amount_paid >= charge.amount_due
            else RentChargeStatus.PARTIAL
        )
        charge.save(update_fields=["amount_paid", "status", "updated_at"])
        _audit(
            actor,
            "rent_charge.payment_recorded",
            ENTITY_CHARGE,
            charge.pk,
            before,
            {
                **_charge(charge),
                "payment": str(payment.pk),
                "method": method,
                "amount": str(amount),
                "paid_on": paid_on.isoformat(),
                "creation_key": str(creation_key) if creation_key is not None else None,
            },
        )
        linked = _provider_invoice(charge)
        if linked is not None and not (whole and linked[0].total == amount):
            reason = offline_marking.PARTIAL_AMOUNT if not whole else offline_marking.TOTAL_DIFFERS
            result = _not_updated(charge, payment, linked, reason, actor)
            return PaymentOutcome(True, PAY_RECORDED, payment=payment, provider=result)
    if linked is None:
        return PaymentOutcome(
            True, PAY_RECORDED, payment=payment, provider=MarkResult(MarkAction.NO_INVOICE)
        )
    result = _mark(charge.pk, payment.pk, actor=actor, retry=False)
    return PaymentOutcome(True, PAY_RECORDED, payment=payment, provider=result)


def retry_provider_mark(charge_id: uuid.UUID, *, actor: User | None) -> MarkResult:
    """Retry the last failed marking of the charge's provider invoice (PLAN-009 D2)."""
    return _mark(charge_id, None, actor=actor, retry=True)


def provider_retry_wait(charge: RentCharge, now: datetime | None = None) -> int | None:
    """``None`` when the charge has no failed marking to retry; otherwise the whole
    seconds until a retry is allowed (0: now)."""
    last = _last_mark_event(charge)
    if last is None or last.action != MARK_FAILED_ACTION:
        return None
    return _retry_wait(last, now)


def _retry_wait(event: AuditLog, now: datetime | None = None) -> int:
    elapsed = ((now or timezone.now()) - event.at).total_seconds()
    lock = operator_sync.RETRY_LOCK_SECONDS
    if elapsed >= lock:
        return 0
    return min(lock, max(1, math.ceil(lock - elapsed)))


def _last_mark_event(charge: RentCharge) -> AuditLog | None:
    return (
        AuditLog.objects.filter(
            Q(
                entity_type=ENTITY_CHARGE,
                entity_id=str(charge.pk),
                action__in=_CHARGE_MARK_ACTIONS,
            )
            | Q(action=ALREADY_PAID_ACTION, after__rent_charge=str(charge.pk))
        )
        .order_by("-id")
        .first()
    )


def _provider_invoice(charge: RentCharge) -> tuple[Invoice, str] | None:
    """The charge's first invoice with a current provider reference, and that reference."""
    for invoice in charge.invoices.order_by("issue_date", "invoice_number"):
        ref = (
            ExternalRef.objects.filter(
                entity_type=ENTITY_INVOICE,
                entity_id=invoice.pk,
                provider=PROVIDER_KEY,
                external_kind=KIND_INVOICE,
                superseded_at__isnull=True,
            )
            .values_list("external_id", flat=True)
            .first()
        )
        if ref:
            return invoice, ref
    return None


def _mark_context(payment: Payment, linked: tuple[Invoice, str]) -> dict[str, Any]:
    invoice, ref = linked
    return {
        "payment": str(payment.pk),
        "invoice_number": invoice.invoice_number,
        "provider_invoice": mask_identifier(ref),
    }


def _flag_review(charge: RentCharge, text: str) -> None:
    charge.needs_review = True
    charge.review_message = text
    charge.save(update_fields=["needs_review", "review_message", "updated_at"])


def _clear_review(charge: RentCharge) -> None:
    """Clear a marking flag; the charge stays flagged for any other recorded reason."""
    if not charge.review_message:
        return
    charge.review_message = ""
    charge.needs_review = (
        charge.invoices.exclude(total=charge.amount_due).exists()
        or AuditLog.objects.filter(
            entity_type=ENTITY_CHARGE, entity_id=str(charge.pk), action__in=_OTHER_FLAG_ACTIONS
        ).exists()
    )
    charge.save(update_fields=["needs_review", "review_message", "updated_at"])


def _not_updated(
    charge: RentCharge,
    payment: Payment,
    linked: tuple[Invoice, str],
    reason: str,
    actor: User | None,
) -> MarkResult:
    """No provider call (D3): flag the charge; an earlier, different flag text is kept."""
    before = {**_charge(charge), "review_message": charge.review_message}
    if not charge.review_message:
        _flag_review(charge, reason)
    elif not charge.needs_review:
        charge.needs_review = True
        charge.save(update_fields=["needs_review", "updated_at"])
    _audit(
        actor,
        NOT_UPDATED_ACTION,
        ENTITY_CHARGE,
        charge.pk,
        before,
        {
            **_charge(charge),
            "review_message": charge.review_message,
            **_mark_context(payment, linked),
            "reason": reason,
        },
    )
    return MarkResult(MarkAction.NOT_UPDATED, offline_marking.shown(reason))


def _mark(
    charge_id: uuid.UUID, payment_id: uuid.UUID | None, *, actor: User | None, retry: bool
) -> MarkResult:
    """Re-fetch, mark and re-fetch the charge's provider invoice under its row lock (D1).

    With ``retry``, the payment is the one named by the last failed attempt, and the call
    is refused while that attempt is less than ``RETRY_LOCK_SECONDS`` old. When the last
    attempt marked the invoice but could not re-read it, an invoice now shown paid
    confirms that mark and is not treated as paid elsewhere.
    """
    with transaction.atomic():
        charge = RentCharge.objects.select_for_update().get(pk=charge_id)
        previous_stage = ""
        if retry:
            last = _last_mark_event(charge)
            if last is None or last.action != MARK_FAILED_ACTION or not last.after:
                return MarkResult(MarkAction.NOTHING_TO_RETRY, MARK_NOTHING_TO_RETRY)
            wait = _retry_wait(last)
            if wait:
                return MarkResult(
                    MarkAction.RETRY_TOO_SOON, operator_sync.retry_wait_message(wait), wait
                )
            payment_id = uuid.UUID(str(last.after["payment"]))
            previous_stage = str(last.after.get("stage", ""))
        linked = _provider_invoice(charge)
        if linked is None or payment_id is None:
            return MarkResult(MarkAction.NOTHING_TO_RETRY if retry else MarkAction.NO_INVOICE)
        invoice, ref = linked
        payment = Payment.objects.get(pk=payment_id, rent_charge=charge)
        context = _mark_context(payment, linked)
        before = {**_charge(charge), "review_message": charge.review_message}
        provider = payments.get_provider()
        stage = STAGE_CHECK
        try:
            record = provider.get_invoice(ref)
            if invoice_sync.shows_paid(record) and previous_stage != STAGE_REREAD:
                changes = invoice_sync.record_provider_state(invoice.pk, record)
                _flag_review(charge, offline_marking.ALREADY_PAID)
                _audit(
                    actor,
                    ALREADY_PAID_ACTION,
                    ENTITY_PAYMENT,
                    payment.pk,
                    before,
                    {
                        **_charge(charge),
                        "review_message": charge.review_message,
                        "rent_charge": str(charge.pk),
                        **context,
                        "provider_changes": changes,
                    },
                )
                return MarkResult(
                    MarkAction.ALREADY_PAID, offline_marking.shown(offline_marking.ALREADY_PAID)
                )
            if not invoice_sync.shows_paid(record):
                stage = STAGE_MARK
                provider.mark_invoice_paid_cash_or_check(
                    ref,
                    offline_marking.offline_payment(
                        method=payment.method,
                        amount=payment.amount,
                        paid_on=payment.settled_on or payment.initiated_on or timezone.localdate(),
                        note=payment.note,
                        reference=payment.reference,
                    ),
                )
                stage = STAGE_REREAD
                record = provider.get_invoice(ref)
        except ProviderError as error:
            failure = operator_sync.failure_name(error)
            fields = operator_sync.failure_fields(error)
            template = (
                offline_marking.REREAD_FAILED
                if stage == STAGE_REREAD
                else offline_marking.MARK_FAILED
            )
            text = offline_marking.failure_text(template, failure, fields)
            _flag_review(charge, text)
            _audit(
                actor,
                MARK_FAILED_ACTION,
                ENTITY_CHARGE,
                charge.pk,
                before,
                {
                    **_charge(charge),
                    "review_message": charge.review_message,
                    **context,
                    "stage": stage,
                    "failure": failure,
                    "fields": fields,
                },
            )
            return MarkResult(
                MarkAction.FAILED, offline_marking.shown(text) + "." + MARK_RETRY_HINT
            )
        changes = invoice_sync.record_provider_state(invoice.pk, record)
        _clear_review(charge)
        _audit(
            actor,
            MARKED_ACTION,
            ENTITY_CHARGE,
            charge.pk,
            before,
            {
                **_charge(charge),
                "review_message": charge.review_message,
                **context,
                "confirmed_earlier_mark": stage == STAGE_CHECK,
                "provider_changes": changes,
            },
        )
        return MarkResult(MarkAction.MARKED, MARK_DONE)


def _payment_for_key(charge: RentCharge, creation_key: uuid.UUID) -> bool:
    return AuditLog.objects.filter(
        action="rent_charge.payment_recorded",
        entity_type=ENTITY_CHARGE,
        entity_id=str(charge.pk),
        after__creation_key=str(creation_key),
    ).exists()


def recompute_charge(charge: RentCharge) -> None:
    """Recompute a charge's paid amount and status from its payments (PLAN-008 D5, D12).

    Paid in full: ``paid``; partly: ``partial``; nothing settled but a payment pending:
    ``pending``; nothing settled after a payment was returned or reopened: ``unpaid``;
    otherwise the status stays (``due``). A waived charge is left as it is. The caller
    holds the row lock and writes the audit row.
    """
    if charge.status == RentChargeStatus.WAIVED:
        return
    paid = _settled(charge)
    payments = Payment.objects.filter(rent_charge=charge, is_refund=False)
    if paid >= charge.amount_due and paid > 0:
        status = RentChargeStatus.PAID
    elif paid > 0:
        status = RentChargeStatus.PARTIAL
    elif payments.filter(status=PaymentStatus.PENDING).exists():
        status = RentChargeStatus.PENDING
    elif (
        charge.status
        in (
            RentChargeStatus.PENDING,
            RentChargeStatus.PAID,
            RentChargeStatus.PARTIAL,
        )
        or payments.filter(status=PaymentStatus.RETURNED).exists()
    ):
        status = RentChargeStatus.UNPAID
    else:
        status = RentChargeStatus(charge.status)
    if (charge.amount_paid, charge.status) != (paid, status):
        charge.amount_paid = paid
        charge.status = status
        charge.save(update_fields=["amount_paid", "status", "updated_at"])


def charge_state(charge: RentCharge) -> dict[str, Any]:
    """The audited fields of a charge."""
    return _charge(charge)


def _settled(charge: RentCharge) -> Decimal:
    """Settled payments minus settled refunds, to the cent."""
    rows = Payment.objects.filter(rent_charge=charge, status=PaymentStatus.SETTLED)
    paid = rows.filter(is_refund=False).aggregate(total=Sum("amount"))["total"] or ZERO
    refunded = rows.filter(is_refund=True).aggregate(total=Sum("amount"))["total"] or ZERO
    return (Decimal(paid) - Decimal(refunded)).quantize(CENT)


# --- Helpers --------------------------------------------------------------------------


def _charge(charge: RentCharge) -> dict[str, Any]:
    return {
        "lease": str(charge.lease_id),
        "period": f"{charge.period_month:%Y-%m}",
        "seq": charge.seq,
        "due_on": charge.due_on.isoformat(),
        "amount_due": str(charge.amount_due),
        "amount_paid": str(charge.amount_paid),
        "status": charge.status,
        "needs_review": charge.needs_review,
    }


def _audit(
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )
