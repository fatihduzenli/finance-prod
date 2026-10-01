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
- **Manual payments** (D8): Zelle, wire or other, recorded here only (no provider
  call); amount above zero and at most the balance; date not in the future; a note.
  ``amount_paid`` is recomputed from settled payments and the status follows.

Every change writes ``audit_log``; provider identifiers in it are masked. Money is
``Decimal`` throughout.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
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
    PROVIDER_KEY,
    debit_amounts,
    is_large_debit,
    rent_on,
    schedule_ref,
)
from payments import invoice_sync, operator_sync
from payments.errors import ProviderError
from payments.health import provider_health
from payments.models import ExternalRef
from payments.provider import InvoiceQuery, InvoiceRecord, mask_identifier

ENTITY_CHARGE = "rent_charge"
ENTITY_INVOICE = "invoice"
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


# --- Results --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DueDebit:
    lease: Lease
    seq: int
    debit_day: int
    amount: Decimal
    #: Above ``LARGE_DEBIT_WARNING_AMOUNT`` (a placeholder until the provider answers Q25).
    large: bool
    #: This period's charge, when already created.
    charge: RentCharge | None
    #: The same debit's charge of the previous month, if any.
    last_month: RentCharge | None


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
class PaymentOutcome:
    ok: bool
    message: str
    field: str | None = None
    payment: Payment | None = None
    #: True when the form's key had already recorded a payment (a resubmission).
    replayed: bool = False


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
                    charge=charges.get((period, seq)),
                    last_month=charges.get((previous, seq)),
                )
            )
    return RunDay(day, tuple(due), tuple(skipped))


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
    if charge.invoices.exclude(total=charge.amount_due).exists():
        return TOTAL_MISMATCH
    return SECOND_INVOICE


def _invoice_linked(external_id: str) -> bool:
    return ExternalRef.objects.filter(
        provider=PROVIDER_KEY, external_kind=KIND_INVOICE, external_id=external_id
    ).exists()


def _flag(charge: RentCharge, actor: User | None, action: str, context: dict[str, Any]) -> None:
    before = _charge(charge)
    charge.needs_review = True
    charge.save(update_fields=["needs_review", "updated_at"])
    _audit(actor, action, ENTITY_CHARGE, charge.pk, before, {**_charge(charge), **context})


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
) -> PaymentOutcome:
    """Record a Zelle, wire or other payment against a rent charge (D8). No provider call.

    ``creation_key`` is the one-time key of the form; it is kept in the audit row and
    looked up under the charge's row lock, so a resubmitted form records nothing.
    """
    text = (note or "").strip()
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
        before = _charge(charge)
        payment = Payment(
            rent_charge=charge,
            kind=PaymentKind.RENT,
            method=method,
            amount=amount,
            status=PaymentStatus.SETTLED,
            initiated_on=paid_on,
            settled_on=paid_on,
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
    return PaymentOutcome(True, "Payment recorded.", payment=payment)


def _payment_for_key(charge: RentCharge, creation_key: uuid.UUID) -> bool:
    return AuditLog.objects.filter(
        action="rent_charge.payment_recorded",
        entity_type=ENTITY_CHARGE,
        entity_id=str(charge.pk),
        after__creation_key=str(creation_key),
    ).exists()


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
