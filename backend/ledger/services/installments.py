"""Installment plans (docs/qa/PLAN-012e-lease-end-transfer-installments.md D9 to D12).

A plan splits the open balance of one charge, a rent charge or a one-off charge (its
**parent**), into 2 to 12 installments. Rules kept here, not in the views:

- **Amounts and dates.** :func:`split` gives equal shares rounded down to the cent with
  the remainder on the last (the shares sum exactly; a share below one cent is refused);
  :func:`schedule_dates` gives the dates from the first one and the cadence (``weekly``,
  ``biweekly``, ``monthly``: the first date's day of the month, clamped to the month's
  last day). The preview (:func:`preview`) shows both; staff may edit each date and
  amount before confirming as long as the amounts still sum to the parent's balance,
  each is at least one cent, the dates are strictly ascending and the first is today or
  later (:func:`lines_refusal`).
- **Who may be split** (:func:`split_refusal`, local data only): the parent is open (not
  waived or voided) with a balance above zero; it has no plan that is not cancelled; no
  payment on it is pending (a rent charge whose month debit started is ``pending``);
  nothing of it is live at the payment provider that the split cannot release (a
  multi-line invoice (:data:`LIVE_INVOICE_SHARED`), a one-off charge's schedule that
  generated its invoice, a provider request still waiting, or a rent charge's live
  invoice while the lease's rent autopay would also debit it); it is not a deposit, a
  re-debit, an NSF fee or an installment; its operator is known and not archived; the
  collection's provider prerequisites hold. The parent's own live single-charge invoice
  (one-off or rent), a one-off charge's schedule that generated no invoice, or, for a
  rent charge due today or later, the lease's active rent autopay that would debit it,
  is **released** by the confirm (PLAN-012b-a D2, PLAN-012b-b D13,
  :func:`release_kind`; the split page says so, :func:`release_notice`): the rent
  autopay is moved past the month (:func:`ledger.services.leases.move_autopay`).
- **Collection** (:func:`collection_of`): as the parent's. A one-off charge's own
  (``autopay_due``, ``email``, ``manual``; ``charge_now`` is collected by ``email``,
  setup's ``none`` by ``manual``); a rent charge's from its lease's channel (the payment
  provider's ACH: ``autopay_due``; any other: ``manual``). ``autopay_due`` needs the
  operator's provider customer and linked bank method, every amount within the confirmed
  provider limit and the large-debit confirmation above the warning threshold;
  ``email`` needs the customer.
- **Confirm** (:func:`create_plan`): in one transaction under the parent's row lock (two
  concurrent confirms leave one plan: the second finds the first, and a partial unique
  index backs it), the plan and one ``installment`` one-off charge per line ("Installment
  n of N: <parent>"), each collected as the plan says: ``autopay_due`` queues
  ``schedule_create`` (one one-occurrence schedule each, as PLAN-010d D2); ``email`` makes
  a local invoice and queues ``invoice_create``; ``manual`` makes nothing at the payment
  provider. Audited: the plan, each installment and the parent. A parent whose provider
  object is released (PLAN-012b-a D3) gets no installment job yet: its own
  ``invoice_delete`` or ``schedule_delete`` job is queued with :data:`RELEASE_PARAM`
  (audited ``<entity>.release_requested``, the identifiers masked); the worker deletes
  the object at the provider without voiding the charge, or moves the rent autopay
  (:func:`payments.one_off_invoices.release_invoice`, ``release_schedule``) and then,
  in the same transaction, :func:`after_release` queues one job per installment, only
  while the plan is live, nothing was paid on the parent meanwhile (else the plan is
  ``attention``, PLAN-012b-a D4) and no installment job exists yet; audited
  :data:`RELEASED_ACTION` once. The plan page says the release waits while that job is
  queued, running or failed (:func:`release_pending`); the charge page's Retry repeats it
  (a rent charge's: :func:`retry_release`).
- **While the plan is live** (``active`` or ``attention``) the parent is left out of
  every open balance (:mod:`ledger.services.receivables`, hence aging, arrears, the
  dashboard and the attention list) and its installments are owed instead; a payment on
  an installment pays that installment only, and a manual payment on the parent is
  refused. When every installment is paid the plan is ``completed`` and the parent is
  paid through :func:`ledger.services.one_off_charges.sync_plan` (``covered_by_plan``,
  no payment row). A returned installment debit puts the plan in ``attention`` until
  the return is resolved (the returns queue, or ``nsf.reschedule`` with the NSF fee).
- **Reschedule** (:func:`reschedule`, billing): new dates from a new first date (and
  cadence) for the installments not yet paid that have no payment at all and whose
  schedule has not generated its invoice; refused while a provider request of one of
  them waits. Each date is changed here, an e-mailed installment's local invoice too;
  the provider objects follow through the existing jobs with the
  :data:`RESCHEDULE_PARAM` flag: ``schedule_create`` updates the schedule's rule (or
  creates it when it was never made), ``invoice_create`` updates the provider invoice's
  due date (or creates it when it was never made).
- **Cancel** (:func:`cancel`, admin, a note): refused while an installment not yet paid
  has a payment pending, a part paid, a re-debit in progress or a provider request
  waiting. The plan becomes ``cancelled``; every installment not paid is cancelled
  through the one-off cancel path (:func:`ledger.services.one_off_charges.cancel_charge`:
  the worker deletes its invoice or schedule and voids it); the parent is an open charge
  again for what the installments did not collect (its paid amount includes what they
  were paid, audited).

Money is ``Decimal`` throughout; nothing here calls the payment provider.
"""

from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role, meets, role_of
from ledger import wording
from ledger.models import (
    DESCRIPTION_MAX_LENGTH,
    INSTALLMENTS_MAX,
    INSTALLMENTS_MIN,
    LIVE_PLAN_STATUSES,
    ZERO,
    AuditLog,
    CollectionMethod,
    InstallmentCadence,
    InstallmentCollection,
    InstallmentPlan,
    InstallmentPlanStatus,
    Invoice,
    InvoiceStatus,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentChannel,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    ScheduleState,
)
from ledger.services import leases, numbering, one_off_charges, rent_run
from ledger.services.one_off_charges import ENTITY_PLAN, plan_state
from ledger.validators import validate_no_payment_data, visible_text
from ledger.wording import format_amount, provider_sentence
from payments import jobs
from payments.models import Job, JobEntity, JobKind, JobStatus
from payments.provider import mask_identifier

CENT = Decimal("0.01")
#: The ``params`` flag of a ``schedule_create`` or ``invoice_create`` job that moves an
#: existing provider object to the installment's new date.
RESCHEDULE_PARAM = "reschedule"
#: The ``params`` flag of an ``invoice_delete`` or ``schedule_delete`` job that releases a
#: split parent's provider object (PLAN-012b-a D3): deleted there, the charge kept owed.
RELEASE_PARAM = one_off_charges.RELEASE_PARAM
#: How an installment's description begins.
DESCRIPTION_TEMPLATE = "Installment {number} of {count}: {label}"

CREATED_ACTION = "installment_plan.created"
RESCHEDULED_ACTION = "installment_plan.rescheduled"
CANCELLED_ACTION = "installment_plan.cancelled"
INSTALLMENT_RESCHEDULED_ACTION = "one_off_charge.installment_rescheduled"
SPLIT_ACTION = "split_into_installments"
RELEASE_REQUESTED_ACTION = "one_off_charge.release_requested"
RENT_RELEASE_REQUESTED_ACTION = "rent_charge.release_requested"
RELEASED_ACTION = "installment_plan.released"

# --- Fixed sentences ------------------------------------------------------------------

COUNT_RANGE = f"Choose between {INSTALLMENTS_MIN} and {INSTALLMENTS_MAX} installments."
CHOOSE_CADENCE = "Choose weekly, every two weeks or monthly."
FIRST_REQUIRED = "Enter the first installment's date."
FIRST_IN_PAST = "The first installment's date must be today or later."
CENT_TOO_SMALL = "The balance cannot be split into that many installments of at least one cent."
LINES_COUNT = "Give a date and an amount for every installment."
LINE_AMOUNT = "Each installment's amount must be at least $0.01, in whole cents."
LINES_SUM = "The installments must add up to the balance, {balance}; they add up to {total}."
LINES_ORDER = "The installments' dates must be in order, each after the one before."
NOTHING_DUE = "Nothing is owed on this charge."
CLOSED = "This charge is waived or voided."
HAS_PLAN = "This charge already has an installment plan."
NOT_SPLITTABLE = "A re-debit, an NSF fee or an installment is not split into installments."
DEPOSIT = "A deposit is not split into installments."
PENDING = "A payment on this charge is pending: wait until it settles or is returned."
LIVE_SCHEDULE = "Autopay will collect this charge: collect it or cancel the charge first."
LIVE_INVOICE_SHARED = (
    "Invoice {number} has other lines; it cannot be released for one of them. Collect it or "
    "cancel it first."
)
SCHEDULE_FIRED = "Autopay already made this month's invoice; collect or cancel it first."
#: What the split page says when the parent's provider object is released (D2).
RELEASE_NOTICE_INVOICE = provider_sentence(
    "Invoice {number} will be cancelled in {provider} (the charge stays open here) and each "
    "installment is collected on its own."
)
RELEASE_NOTICE_SCHEDULE = (
    "The autopay of this charge will be cancelled (the charge stays open here) and each "
    "installment is collected on its own."
)
RELEASE_NOTICE_AUTOPAY = (
    "Rent autopay of this lease will skip this month's debit: its autopay is moved to next "
    "month's debit (the charge stays open here), and each installment is collected on its "
    "own."
)
#: What the plan page says while the release job waits or failed (D3).
RELEASE_PENDING_INVOICE = provider_sentence("Waiting for {provider} to cancel invoice {number}.")
RELEASE_PENDING_SCHEDULE = provider_sentence(
    "Waiting for {provider} to cancel the autopay of this charge."
)
RELEASE_PENDING_AUTOPAY = provider_sentence(
    "Waiting for {provider} to move the lease's rent autopay past this month."
)
#: The release job's outcomes (D3).
RELEASED = provider_sentence("Cancelled in {provider}; the {count} installments are being set up.")
RELEASED_PLAN_CLOSED = provider_sentence(
    "Cancelled in {provider}. The plan is cancelled or completed: nothing more to do."
)
RELEASED_PARENT_PAID = provider_sentence(
    "Cancelled in {provider}. A payment on the charge landed first: the plan needs attention "
    "and no installment was set up."
)
RELEASE_ALREADY = provider_sentence("Already cancelled in {provider}. Nothing was changed.")
RELEASE_NO_PLAN = "This charge has no installment plan. Nothing was released."
#: A rent charge with a live invoice that the lease's rent autopay would debit too: one
#: release cannot free both (D13).
AUTOPAY_WILL_DEBIT = (
    "Rent autopay will debit this charge on its due date: stop or pause autopay first."
)
REQUEST_WAITING = provider_sentence(
    "A change is still in progress in {provider}: try again in a minute."
)
NO_OPERATOR = "This charge has no operator."
OPERATOR_ARCHIVED = "The operator is archived."
NO_CUSTOMER = provider_sentence(
    "The operator is not connected to {provider}: installments collected by {collection} are "
    "not possible."
)
NO_BANK_METHOD = "The operator has no linked bank method: autopay installments are not possible."
CREATED = "Installment plan created: {count} installments."
NOT_LIVE = "This plan is completed or cancelled. Nothing was changed."
NOTHING_TO_SHIFT = "No installment can be moved: each is paid, has a payment or was debited."
SHIFT_BUSY = provider_sentence(
    "A change is still in progress in {provider}: try again in a minute. Nothing was changed."
)
RESCHEDULED = "{count} installments moved; the first is now due {date}."
NOTE_REQUIRED = "A note is required: say why the plan is cancelled."
NOTE_PAYMENT_DATA = "This text may not contain card or account numbers."
CANCEL_ADMIN = "Only an admin can cancel an installment plan."
CANCEL_PENDING = (
    "A payment on an installment is pending: wait until it settles or is returned. Nothing "
    "was changed."
)
CANCEL_PARTIAL = "An installment is partly paid: collect the rest of it first. Nothing was changed."
CANCEL_REDEBIT = (
    "A returned installment is being collected again: wait for the re-debit or cancel it "
    "first. Nothing was changed."
)
CANCELLED = provider_sentence(
    "Plan cancelled. The unpaid installments are cancelled in {provider}; the charge is open "
    "again for {balance}."
)

#: The help text of each page (D10, D11).
SPLIT_HELP = (
    "Split what is still owed on this charge into equal installments, the last one "
    "carrying the rounding cent. Each installment is a charge of its own, collected the "
    "way this charge is; while the plan runs, this charge is paid through them."
)
PREVIEW_HELP = (
    "Check each installment before confirming. You may change any date or amount, as long "
    "as the amounts still add up to the balance and the dates stay in order."
)
PLAN_HELP = (
    "Each installment is collected on its own date. A returned installment puts the plan "
    "on the attention list until it is dealt with on the Returned payments page. Reschedule moves "
    "the installments not yet paid; Cancel (admin) stops the plan and leaves the charge "
    "open for what was not paid."
)
RESCHEDULE_HELP = provider_sentence(
    "Only installments with no payment move; paid ones keep their dates. Autopay and "
    "e-mailed invoices are updated in {provider} automatically."
)
CANCEL_HELP = provider_sentence(
    "The unpaid installments are cancelled (their invoices and autopay are cancelled in "
    "{provider}) and the charge becomes one open charge for what they did not collect."
)


# --- Values -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line:
    """One installment as previewed or edited: its due date and amount."""

    due: date
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    field: str | None = None
    plan: InstallmentPlan | None = None


@dataclass(frozen=True, slots=True)
class Progress:
    """Where a plan stands (D9): installments paid of the plan's count, and money paid
    of what the installments bill."""

    plan: InstallmentPlan
    paid_count: int
    count: int
    paid: Decimal
    total: Decimal
    installments: list[OneOffCharge] = field(default_factory=list)

    @property
    def text(self) -> str:
        return (
            f"Installment plan: {self.paid_count} of {self.count} paid, "
            f"{format_amount(self.paid)} of {format_amount(self.total)}"
        )

    @property
    def short_text(self) -> str:
        """The month grid's cell text: ``Plan 1 of 3 paid, $333.33 of $1,000.00``."""
        return (
            f"Plan {self.paid_count} of {self.count} paid, "
            f"{format_amount(self.paid)} of {format_amount(self.total)}"
        )


Parent = RentCharge | OneOffCharge


# --- Arithmetic (D9) ---------------------------------------------------------------------


def split(total: Decimal, count: int) -> list[Decimal]:
    """``count`` equal shares of ``total`` rounded down to the cent, the remainder on the
    last; they sum exactly. Raises ``ValueError`` when ``count`` is outside 2 to 12 or a
    share would be below one cent."""
    if not INSTALLMENTS_MIN <= count <= INSTALLMENTS_MAX:
        raise ValueError(COUNT_RANGE)
    share = (total / count).quantize(CENT, rounding=ROUND_DOWN)
    if share < CENT:
        raise ValueError(CENT_TOO_SMALL)
    return [share] * (count - 1) + [total - share * (count - 1)]


def _add_months(first: date, months: int) -> date:
    month_index = first.month - 1 + months
    year, month = first.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(first.day, calendar.monthrange(year, month)[1]))


def schedule_dates(first: date, cadence: str, count: int) -> list[date]:
    """The ``count`` dates from ``first``: every 7 days, every 14 days, or monthly on the
    first date's day of the month, clamped to the month's last day (the 31st gives 28 or
    29 February, then 31 March). Raises ``ValueError`` on an unknown cadence."""
    if cadence == InstallmentCadence.WEEKLY:
        return [first + timedelta(days=7 * index) for index in range(count)]
    if cadence == InstallmentCadence.BIWEEKLY:
        return [first + timedelta(days=14 * index) for index in range(count)]
    if cadence == InstallmentCadence.MONTHLY:
        return [_add_months(first, index) for index in range(count)]
    raise ValueError(CHOOSE_CADENCE)


# --- Reads -------------------------------------------------------------------------------


def parent_label(parent: Parent) -> str:
    """What staff call the parent: ``Rent 2026-10`` (``(debit 2)``) or the one-off label."""
    if isinstance(parent, RentCharge):
        return f"Rent {parent.period_month:%Y-%m}" + (
            f" (debit {parent.seq})" if parent.seq > 1 else ""
        )
    return parent.label


def parent_url(parent: Parent) -> str:
    if isinstance(parent, RentCharge):
        return reverse("ledger:charges:detail", kwargs={"pk": parent.pk})
    return reverse("ledger:one_off_charges:detail", kwargs={"pk": parent.pk})


def balance_of(parent: Parent) -> Decimal:
    return Decimal(parent.balance)


def operator_of(parent: Parent) -> Operator | None:
    if isinstance(parent, RentCharge):
        return parent.lease.operator
    if parent.operator is not None:
        return parent.operator
    return parent.lease.operator if parent.lease is not None else None


def _plans_of(parent: Parent) -> list[InstallmentPlan]:
    return list(parent.installment_plans.order_by("-confirmed_at", "-id"))


def plan_of(parent: Parent) -> InstallmentPlan | None:
    """The parent's plan that is not cancelled, else its latest cancelled one, else
    ``None``: what the parent's page shows."""
    plans = _plans_of(parent)
    for plan in plans:
        if plan.status != InstallmentPlanStatus.CANCELLED:
            return plan
    return plans[0] if plans else None


def collection_of(parent: Parent) -> str:
    """How the installments of ``parent`` are collected: as the parent is (D9)."""
    if isinstance(parent, RentCharge):
        if parent.lease.payment_channel == PaymentChannel.PROVIDER_ACH:
            return InstallmentCollection.AUTOPAY_DUE
        return InstallmentCollection.MANUAL
    if parent.collection == CollectionMethod.AUTOPAY_DUE:
        return InstallmentCollection.AUTOPAY_DUE
    if parent.collection in (CollectionMethod.EMAIL, CollectionMethod.CHARGE_NOW):
        return InstallmentCollection.EMAIL
    return InstallmentCollection.MANUAL


def installments_of(plan: InstallmentPlan) -> list[OneOffCharge]:
    return list(
        OneOffCharge.objects.filter(plan=plan).order_by("installment_no", "created_at", "id")
    )


def _progress(plan: InstallmentPlan, rows: list[OneOffCharge]) -> Progress:
    return Progress(
        plan=plan,
        paid_count=sum(1 for row in rows if row.status == OneOffChargeStatus.PAID),
        count=plan.count,
        paid=sum((Decimal(row.amount_paid) for row in rows), ZERO),
        total=sum((Decimal(row.price) for row in rows), ZERO),
        installments=rows,
    )


def progress(plan: InstallmentPlan) -> Progress:
    """How far the plan is (D9): ``n of N paid, $X of $Y`` (one query)."""
    return _progress(plan, installments_of(plan))


def live_rent_progress(start: date, end: date) -> dict[uuid.UUID, Progress]:
    """Per rent charge of the months ``start`` to ``end`` with a live plan, its progress
    (two queries; the month grid, D12)."""
    plans = {
        plan.pk: plan
        for plan in InstallmentPlan.objects.filter(
            rent_charge__period_month__gte=start,
            rent_charge__period_month__lte=end,
            status__in=LIVE_PLAN_STATUSES,
        )
    }
    rows: dict[uuid.UUID, list[OneOffCharge]] = {pk: [] for pk in plans}
    for row in OneOffCharge.objects.filter(plan__in=list(plans)).order_by("installment_no"):
        if row.plan_id is not None:
            rows[row.plan_id].append(row)
    return {
        plan.rent_charge_id: _progress(plan, rows[pk])
        for pk, plan in plans.items()
        if plan.rent_charge_id is not None
    }


# --- Who may be split (D9, D10) -------------------------------------------------------------


def _live_invoice(charge: Parent) -> Invoice | None:
    """The charge's invoice with a provider reference, when it is live there."""
    linked = rent_run.provider_invoice(charge)
    if linked is not None and one_off_charges.invoice_is_live(linked[0]):
        return linked[0]
    return None


def _autopay_will_debit(charge: RentCharge, today: date) -> bool:
    """Whether the lease's active rent autopay would debit this charge: it is due today
    or later and a rent schedule of the lease is active at the provider (D13)."""
    return charge.due_on >= today and leases.autopay_on(charge.lease)


def _rent_refusal(charge: RentCharge, today: date) -> str:
    if charge.status == RentChargeStatus.WAIVED:
        return CLOSED
    if charge.status == RentChargeStatus.PENDING:
        return PENDING
    invoice = _live_invoice(charge)
    if invoice is not None:
        if invoice.rent_charge_id != charge.pk or one_off_charges.invoice_line_count(invoice) > 1:
            # A Rent line of a multi-line invoice: never released for one line (D13).
            return LIVE_INVOICE_SHARED.format(number=invoice.invoice_number)
        if _autopay_will_debit(charge, today):
            return AUTOPAY_WILL_DEBIT
    return ""


def _one_off_refusal(charge: OneOffCharge) -> str:
    if charge.status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED):
        return CLOSED
    if charge.category in (
        OneOffCategory.REDEBIT,
        OneOffCategory.NSF_FEE,
        OneOffCategory.INSTALLMENT,
    ):
        return NOT_SPLITTABLE
    if charge.fee_type is not None and charge.fee_type.refundable:
        return DEPOSIT
    billed_on = one_off_charges.billed_with(charge) or charge
    if billed_on.pk == charge.pk and _schedule_fired(charge):
        return SCHEDULE_FIRED
    invoice = _live_invoice(billed_on)
    if invoice is not None:
        if billed_on.pk != charge.pk or one_off_charges.invoice_line_count(invoice) > 1:
            # A line of a multi-line invoice: never released for one line (D2).
            return LIVE_INVOICE_SHARED.format(number=invoice.invoice_number)
        return ""
    if billed_on.pk != charge.pk and one_off_charges.provider_schedule(billed_on):
        return LIVE_SCHEDULE
    return ""


def _schedule_fired(charge: OneOffCharge) -> bool:
    """An ``autopay_due`` charge whose provider schedule already generated its invoice."""
    return bool(one_off_charges.provider_schedule(charge)) and (
        one_off_charges.invoices_of(charge).exists()
    )


def release_kind(parent: Parent, today: date | None = None) -> str:
    """The job that releases the parent's own provider object when it is split
    (PLAN-012b-a D2, D3; PLAN-012b-b D13): ``invoice_delete`` for the parent's live
    single-charge invoice; ``schedule_delete`` for a one-off parent's schedule that
    generated no invoice, or for a rent parent the lease's active rent autopay would
    debit (moved past the month); ``""`` when nothing is live. Valid once
    :func:`split_refusal` is ``""``."""
    if isinstance(parent, RentCharge):
        if _live_invoice(parent) is not None:
            return JobKind.INVOICE_DELETE
        if _autopay_will_debit(parent, today or timezone.localdate()):
            return JobKind.SCHEDULE_DELETE
        return ""
    if one_off_charges.billed_with(parent) is not None:
        return ""
    if _live_invoice(parent) is not None:
        return JobKind.INVOICE_DELETE
    if one_off_charges.provider_schedule(parent) and not _schedule_fired(parent):
        return JobKind.SCHEDULE_DELETE
    return ""


def release_notice(parent: Parent) -> str:
    """What the split page says of the provider object the confirm releases (D2), or
    ``""`` when nothing is live."""
    kind = release_kind(parent)
    if kind == JobKind.INVOICE_DELETE:
        invoice = _live_invoice(parent)
        return RELEASE_NOTICE_INVOICE.format(number=invoice.invoice_number if invoice else "")
    if kind == JobKind.SCHEDULE_DELETE:
        return RELEASE_NOTICE_AUTOPAY if isinstance(parent, RentCharge) else RELEASE_NOTICE_SCHEDULE
    return ""


def release_job(parent: Parent) -> Job | None:
    """The parent's latest job when it is a release (D3, D13), else ``None``."""
    job = jobs.latest_for(parent)
    if (
        job is None
        or job.kind not in (JobKind.INVOICE_DELETE, JobKind.SCHEDULE_DELETE)
        or job.params.get(RELEASE_PARAM) is not True
    ):
        return None
    return job


def release_pending(plan: InstallmentPlan) -> str:
    """What the plan page says while the release of its parent's provider object is
    queued, running or failed (D3), or ``""``."""
    if plan.status not in LIVE_PLAN_STATUSES:
        return ""
    parent = plan.parent
    job = release_job(parent)
    if job is None or not (jobs.is_active(job) or job.status in (JobStatus.FAILED, JobStatus.DEAD)):
        return ""
    if job.kind == JobKind.SCHEDULE_DELETE:
        return (
            RELEASE_PENDING_AUTOPAY if isinstance(parent, RentCharge) else RELEASE_PENDING_SCHEDULE
        )
    invoice = _live_invoice(parent) or rent_run.charge_invoices(parent).first()
    return RELEASE_PENDING_INVOICE.format(number=invoice.invoice_number if invoice else "")


def retry_release(parent: RentCharge) -> str | None:
    """The job kind a rent charge's Retry queues (D13): its latest job's, when that is a
    failed release while the charge's plan is live; ``None`` otherwise."""
    job = release_job(parent)
    if job is None or job.status not in (JobStatus.FAILED, JobStatus.DEAD):
        return None
    if not parent.installment_plans.filter(status__in=LIVE_PLAN_STATUSES).exists():
        return None
    return str(job.kind)


def split_refusal(parent: Parent, today: date | None = None) -> str:
    """Why ``parent`` cannot be split now, or ``""`` (see the module docstring). Local
    data only."""
    day = today or timezone.localdate()
    operator = operator_of(parent)
    if operator is None:
        return NO_OPERATOR
    if operator.archived_at is not None:
        return OPERATOR_ARCHIVED
    if parent.installment_plans.exclude(status=InstallmentPlanStatus.CANCELLED).exists():
        return HAS_PLAN
    if isinstance(parent, RentCharge):
        reason = _rent_refusal(parent, day)
        pending = Payment.objects.filter(rent_charge=parent, status=PaymentStatus.PENDING)
    else:
        reason = _one_off_refusal(parent)
        pending = Payment.objects.filter(one_off_charge=parent, status=PaymentStatus.PENDING)
    if reason:
        return reason
    if balance_of(parent) <= 0:
        return NOTHING_DUE
    if pending.exists():
        return PENDING
    if jobs.is_active(jobs.latest_for(parent)):
        return REQUEST_WAITING
    return _collection_refusal(collection_of(parent), operator)


def _collection_refusal(collection: str, operator: Operator) -> str:
    if collection == InstallmentCollection.MANUAL:
        return ""
    if not leases.customer_ref(operator):
        label = wording.label(InstallmentCollection, collection)
        return NO_CUSTOMER.format(collection=str(label).lower())
    if collection == InstallmentCollection.AUTOPAY_DUE and not leases.linked_payment_method(
        operator
    ):
        return NO_BANK_METHOD
    return ""


# --- Preview and lines (D10) -----------------------------------------------------------------


def preview(
    parent: Parent, count: int, first_due: date | None, cadence: str, today: date | None = None
) -> tuple[list[Line], Outcome | None]:
    """The proposed installments of ``parent``, or the first rule the request breaks."""
    day = today or timezone.localdate()
    if not INSTALLMENTS_MIN <= count <= INSTALLMENTS_MAX:
        return [], Outcome(False, COUNT_RANGE, "count")
    if cadence not in InstallmentCadence.values:
        return [], Outcome(False, CHOOSE_CADENCE, "cadence")
    if first_due is None:
        return [], Outcome(False, FIRST_REQUIRED, "first_due")
    if first_due < day:
        return [], Outcome(False, FIRST_IN_PAST, "first_due")
    try:
        amounts = split(balance_of(parent), count)
    except ValueError as error:
        return [], Outcome(False, str(error), "count")
    dates = schedule_dates(first_due, cadence, count)
    return [Line(due, amount) for due, amount in zip(dates, amounts, strict=True)], None


def lines_refusal(lines: list[Line], balance: Decimal, count: int, today: date) -> Outcome | None:
    """The first rule the edited lines break (D10), or ``None``."""
    if len(lines) != count:
        return Outcome(False, LINES_COUNT, "lines")
    for line in lines:
        amount = line.amount
        if (
            not isinstance(amount, Decimal)
            or not amount.is_finite()
            or amount < CENT
            or amount > one_off_charges.AMOUNT_MAX
        ):
            return Outcome(False, LINE_AMOUNT, "lines")
        try:
            whole_cents = amount == amount.quantize(CENT)
        except InvalidOperation:
            whole_cents = False
        if not whole_cents:
            return Outcome(False, LINE_AMOUNT, "lines")
    total = sum((line.amount for line in lines), ZERO)
    if total != balance:
        message = LINES_SUM.format(balance=format_amount(balance), total=format_amount(total))
        return Outcome(False, message, "lines")
    if lines[0].due < today:
        return Outcome(False, FIRST_IN_PAST, "lines")
    if any(later.due <= earlier.due for earlier, later in zip(lines, lines[1:], strict=False)):
        return Outcome(False, LINES_ORDER, "lines")
    return None


def _debit_refusal(collection: str, lines: list[Line], confirmed: bool) -> Outcome | None:
    if collection != InstallmentCollection.AUTOPAY_DUE:
        return None
    amounts = [line.amount for line in lines]
    limit = leases.provider_limit_refusal(amounts)
    if limit:
        return Outcome(False, limit, "lines")
    confirmation = leases.large_debit_confirmation(amounts)
    if confirmation and confirmed is not True:
        return Outcome(False, confirmation, "confirm")
    return None


# --- Confirm (D9, D10) ------------------------------------------------------------------------


def _lock(parent: Parent) -> Parent:
    if isinstance(parent, RentCharge):
        # No join: FOR UPDATE cannot lock the nullable side of an outer join.
        return RentCharge.objects.select_for_update().get(pk=parent.pk)
    return OneOffCharge.objects.select_for_update().get(pk=parent.pk)


def _parent_state(parent: Parent) -> tuple[str, dict[str, object]]:
    if isinstance(parent, RentCharge):
        return rent_run.ENTITY_CHARGE, rent_run.charge_state(parent)
    return one_off_charges.ENTITY_ONE_OFF_CHARGE, one_off_charges.charge_state(parent)


def _audit(
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )


def _make_installment(
    plan: InstallmentPlan,
    number: int,
    line: Line,
    label: str,
    actor: User | None,
    today: date,
    *,
    queue: bool,
) -> OneOffCharge:
    description = DESCRIPTION_TEMPLATE.format(number=number, count=plan.count, label=label)
    collection = CollectionMethod(plan.collection)
    charge = OneOffCharge.objects.create(
        operator=plan.operator,
        lease=plan.lease,
        category=OneOffCategory.INSTALLMENT,
        collection=collection,
        description=description[:DESCRIPTION_MAX_LENGTH],
        price=line.amount,
        due_date=line.due,
        note=f"Installment {number} of {plan.count} of the plan made {today.isoformat()}.",
        status=OneOffChargeStatus.OWED,
        plan=plan,
        installment_no=number,
    )
    invoice = None
    if collection == CollectionMethod.EMAIL:
        invoice = Invoice.objects.create(
            invoice_number=numbering.next_invoice_number(today.year),
            operator=plan.operator,
            one_off_charge=charge,
            issue_date=today,
            due_date=line.due,
            memo=charge.description,
            subtotal=line.amount,
            tax=ZERO,
            fees=ZERO,
            surcharge=ZERO,
            total=line.amount,
            internal_status=InvoiceStatus.DRAFT,
            allow_card=False,
            allow_ach=True,
            legacy=False,
        )
    _audit(
        actor,
        one_off_charges.CREATED_ACTION,
        one_off_charges.ENTITY_ONE_OFF_CHARGE,
        charge.pk,
        None,
        {
            **one_off_charges.charge_state(charge),
            "note": charge.note,
            "plan": str(plan.pk),
            "installment_no": number,
            "invoice_number": invoice.invoice_number if invoice is not None else None,
        },
    )
    if queue:
        _queue_provider_job(charge, actor)
    return charge


def _queue_provider_job(installment: OneOffCharge, actor: User | None) -> bool:
    """Queue the installment's provider object as its collection says (D9): a schedule
    for ``autopay_due``, an invoice for ``email``, nothing for ``manual``."""
    if installment.collection == CollectionMethod.AUTOPAY_DUE:
        jobs.enqueue(JobKind.SCHEDULE_CREATE, installment, None, actor)
        return True
    if installment.collection == CollectionMethod.EMAIL:
        jobs.enqueue(JobKind.INVOICE_CREATE, installment, None, actor)
        return True
    return False


def create_plan(
    parent: Parent,
    count: int,
    first_due: date,
    cadence: str,
    lines: list[Line],
    actor: User | None,
    *,
    confirmed: bool = False,
    today: date | None = None,
) -> Outcome:
    """Create the plan and its installments (D9, D10; see the module docstring). Every
    rule is checked again under the parent's row lock; a refusal writes nothing."""
    day = today or timezone.localdate()
    if not INSTALLMENTS_MIN <= count <= INSTALLMENTS_MAX:
        return Outcome(False, COUNT_RANGE, "count")
    if cadence not in InstallmentCadence.values:
        return Outcome(False, CHOOSE_CADENCE, "cadence")
    if first_due < day:
        return Outcome(False, FIRST_IN_PAST, "first_due")
    try:
        with transaction.atomic():
            locked = _lock(parent)
            reason = split_refusal(locked, day)
            if reason:
                return Outcome(False, reason)
            refused = lines_refusal(lines, balance_of(locked), count, day)
            collection = collection_of(locked)
            refused = refused or _debit_refusal(collection, lines, confirmed)
            if refused is not None:
                return refused
            operator = operator_of(locked)
            if operator is None:
                return Outcome(False, NO_OPERATOR)
            plan = InstallmentPlan.objects.create(
                rent_charge=locked if isinstance(locked, RentCharge) else None,
                one_off_charge=locked if isinstance(locked, OneOffCharge) else None,
                operator=operator,
                lease=locked.lease,
                count=count,
                first_due=lines[0].due,
                cadence=cadence,
                collection=collection,
                status=InstallmentPlanStatus.ACTIVE,
                created_by=actor,
                confirmed_at=timezone.now(),
            )
            _audit(
                actor,
                CREATED_ACTION,
                ENTITY_PLAN,
                plan.pk,
                None,
                {
                    **plan_state(plan),
                    "balance": str(balance_of(locked)),
                    "lines": [
                        {"due": line.due.isoformat(), "amount": str(line.amount)} for line in lines
                    ],
                },
            )
            label = parent_label(locked)
            # The parent's live provider object is released first; the release job
            # queues the installments' own objects after it (PLAN-012b-a D3).
            release = release_kind(locked, day)
            for number, line in enumerate(lines, start=1):
                _make_installment(plan, number, line, label, actor, day, queue=not release)
            if release:
                _request_release(locked, plan, release, actor)
            entity, state = _parent_state(locked)
            _audit(
                actor,
                f"{entity}.{SPLIT_ACTION}",
                entity,
                locked.pk,
                state,
                {**state, "plan": str(plan.pk), "count": count},
            )
    except IntegrityError:
        # A concurrent confirm on the same parent committed first.
        return Outcome(False, HAS_PLAN)
    return Outcome(True, CREATED.format(count=count), plan=plan)


def _request_release(parent: Parent, plan: InstallmentPlan, kind: str, actor: User | None) -> None:
    """Queue the release of the parent's provider object (D3, D13), audited on the
    parent with the provider identifiers masked."""
    target: dict[str, object]
    if kind == JobKind.INVOICE_DELETE:
        linked = rent_run.provider_invoice(parent)
        target = {
            "invoice_number": linked[0].invoice_number if linked else None,
            "provider_invoice": mask_identifier(linked[1]) if linked else None,
        }
    elif isinstance(parent, RentCharge):
        target = {
            "schedules": [
                mask_identifier(ref)
                for row in leases.live_schedules(parent.lease)
                if row.state == ScheduleState.ACTIVE and (ref := leases.schedule_ref(row))
            ]
        }
    else:
        target = {"schedule": mask_identifier(one_off_charges.provider_schedule(parent))}
    entity, state = _parent_state(parent)
    action = (
        RENT_RELEASE_REQUESTED_ACTION
        if isinstance(parent, RentCharge)
        else RELEASE_REQUESTED_ACTION
    )
    _audit(
        actor,
        action,
        entity,
        parent.pk,
        state,
        {**state, "plan": str(plan.pk), "job_kind": kind, **target},
    )
    jobs.enqueue(kind, parent, {RELEASE_PARAM: True}, actor)


def after_release(
    parent: Parent, actor: User | None, context: dict[str, object]
) -> tuple[bool, str]:
    """The plan's side of a release (D3; the worker only, in the release's transaction,
    under the parent's row lock): the plan's lock, then, when the plan is still live and
    nothing was paid on the parent meanwhile, one provider job per installment, only if
    none was queued for the plan yet; audited :data:`RELEASED_ACTION` once. A plan no
    longer live gets nothing; a payment on the parent puts the plan in ``attention``
    (D4) and queues nothing."""
    found = plan_of(parent)
    if found is None:
        return False, RELEASE_NO_PLAN
    plan = InstallmentPlan.objects.select_for_update().get(pk=found.pk)
    if plan.status not in LIVE_PLAN_STATUSES:
        return True, RELEASED_PLAN_CLOSED
    one_off_charges.apply_plan_status(plan, actor=actor)
    if one_off_charges.parent_paid_on_plan(plan) > 0:
        return True, RELEASED_PARENT_PAID
    rows = installments_of(plan)
    queued = 0
    asked = Job.objects.filter(
        entity_type=JobEntity.ONE_OFF_CHARGE, entity_id__in=[row.pk for row in rows]
    )
    if not asked.exists():
        for row in rows:
            if row.status == OneOffChargeStatus.OWED and _queue_provider_job(row, actor):
                queued += 1
    audited = AuditLog.objects.filter(
        action=RELEASED_ACTION, entity_type=ENTITY_PLAN, entity_id=str(plan.pk)
    )
    if not queued and audited.exists():
        return True, RELEASE_ALREADY
    state = plan_state(plan)
    _audit(
        actor, RELEASED_ACTION, ENTITY_PLAN, plan.pk, state, {**state, **context, "queued": queued}
    )
    return True, RELEASED.format(count=queued)


# --- Reschedule (D11) -------------------------------------------------------------------------


def _shiftable(installment: OneOffCharge) -> bool:
    """Not paid, no payment of any kind, and (autopay) no invoice generated yet."""
    if installment.status != OneOffChargeStatus.OWED or installment.amount_paid > 0:
        return False
    if Payment.objects.filter(one_off_charge=installment).exists():
        return False
    return not (
        installment.collection == CollectionMethod.AUTOPAY_DUE
        and one_off_charges.invoices_of(installment).exists()
    )


def _lock_plan(plan_id: uuid.UUID) -> tuple[InstallmentPlan, list[OneOffCharge]]:
    """The plan's installments, then its parent, then the plan, under their row locks
    (the order of a payment on an installment: the installment first)."""
    rows = list(
        OneOffCharge.objects.select_for_update()
        .filter(plan_id=plan_id)
        .order_by("installment_no", "created_at", "id")
    )
    # The parent before the plan, as a payment on the parent takes them (PLAN-012b-a D4).
    one_off_charges.lock_plan_parent(plan_id)
    plan = InstallmentPlan.objects.select_for_update().get(pk=plan_id)
    return plan, rows


def reschedule(
    plan: InstallmentPlan,
    first_due: date | None,
    cadence: str,
    actor: User | None,
    *,
    today: date | None = None,
) -> Outcome:
    """Move the installments not yet paid (D11; see the module docstring)."""
    day = today or timezone.localdate()
    if cadence not in InstallmentCadence.values:
        return Outcome(False, CHOOSE_CADENCE, "cadence")
    if first_due is None:
        return Outcome(False, FIRST_REQUIRED, "first_due")
    if first_due < day:
        return Outcome(False, FIRST_IN_PAST, "first_due")
    with transaction.atomic():
        locked, rows = _lock_plan(plan.pk)
        if locked.status not in LIVE_PLAN_STATUSES:
            return Outcome(False, NOT_LIVE)
        moving = [row for row in rows if _shiftable(row)]
        if not moving:
            return Outcome(False, NOTHING_TO_SHIFT)
        if any(jobs.is_active(jobs.latest_for(row)) for row in moving):
            return Outcome(False, SHIFT_BUSY)
        before = plan_state(locked)
        dates = schedule_dates(first_due, cadence, len(moving))
        for row, due in zip(moving, dates, strict=True):
            _move(row, due, actor)
        locked.first_due = min(row.due_date for row in rows)
        locked.cadence = cadence
        locked.save(update_fields=["first_due", "cadence", "updated_at"])
        _audit(
            actor,
            RESCHEDULED_ACTION,
            ENTITY_PLAN,
            locked.pk,
            before,
            {
                **plan_state(locked),
                "moved": [
                    {"installment_no": row.installment_no, "due": due.isoformat()}
                    for row, due in zip(moving, dates, strict=True)
                ],
            },
        )
    return Outcome(
        True, RESCHEDULED.format(count=len(moving), date=first_due.isoformat()), plan=locked
    )


def _move(row: OneOffCharge, due: date, actor: User | None) -> None:
    """One installment's new date, its local invoice's, and the job that moves its
    provider object (none for a manual one, or when no provider object exists yet)."""
    before = one_off_charges.charge_state(row)
    row.due_date = due
    row.save(update_fields=["due_date", "updated_at"])
    invoices = Invoice.objects.filter(one_off_charge=row).exclude(
        internal_status=InvoiceStatus.VOID
    )
    invoices.update(due_date=due, updated_at=timezone.now())
    job_kind, moves = "", False
    if row.collection == CollectionMethod.AUTOPAY_DUE:
        # A schedule that was never made (its create failed) is made for the new date.
        job_kind, moves = JobKind.SCHEDULE_CREATE, bool(one_off_charges.provider_schedule(row))
    elif row.collection == CollectionMethod.EMAIL and rent_run.provider_invoice(row) is not None:
        job_kind, moves = JobKind.INVOICE_CREATE, True
    _audit(
        actor,
        INSTALLMENT_RESCHEDULED_ACTION,
        one_off_charges.ENTITY_ONE_OFF_CHARGE,
        row.pk,
        before,
        {**one_off_charges.charge_state(row), "job_kind": job_kind or None},
    )
    if job_kind:
        jobs.enqueue(job_kind, row, {RESCHEDULE_PARAM: True} if moves else None, actor)


# --- Cancel (D11) -----------------------------------------------------------------------------


def _open_redebit(installment: OneOffCharge) -> bool:
    return (
        OneOffCharge.objects.filter(
            category=OneOffCategory.REDEBIT, source_payment__one_off_charge=installment
        )
        .exclude(
            status__in=[
                OneOffChargeStatus.PAID,
                OneOffChargeStatus.WAIVED,
                OneOffChargeStatus.VOIDED,
            ]
        )
        .exists()
    )


def _cancel_refusal(rows: list[OneOffCharge]) -> str:
    unpaid = [
        row
        for row in rows
        if row.status not in (OneOffChargeStatus.PAID, OneOffChargeStatus.VOIDED)
    ]
    for row in unpaid:
        if Payment.objects.filter(one_off_charge=row, status=PaymentStatus.PENDING).exists():
            return CANCEL_PENDING
        if row.amount_paid > 0:
            return CANCEL_PARTIAL
        if _open_redebit(row):
            return CANCEL_REDEBIT
        if jobs.is_active(jobs.latest_for(row)):
            return SHIFT_BUSY
    return ""


def cancel(plan: InstallmentPlan, note: str, actor: User | None) -> Outcome:
    """Cancel the plan (admin, D11; see the module docstring)."""
    if actor is None or not meets(role_of(actor), Role.ADMIN):
        return Outcome(False, CANCEL_ADMIN)
    text = visible_text(note or "")
    if not text:
        return Outcome(False, NOTE_REQUIRED, "note")
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return Outcome(False, NOTE_PAYMENT_DATA, "note")
    with transaction.atomic():
        locked, rows = _lock_plan(plan.pk)
        if locked.status not in LIVE_PLAN_STATUSES:
            return Outcome(False, NOT_LIVE)
        reason = _cancel_refusal(rows)
        if reason:
            return Outcome(False, reason)
        before = plan_state(locked)
        locked.status = InstallmentPlanStatus.CANCELLED
        locked.cancelled_at = timezone.now()
        locked.note = text
        locked.save(update_fields=["status", "cancelled_at", "note", "updated_at"])
        cancelled = []
        for row in rows:
            if row.status != OneOffChargeStatus.OWED:
                continue
            outcome = one_off_charges.cancel_charge(row.pk, note=text, actor=actor)
            if not outcome.ok:
                transaction.set_rollback(True)
                return Outcome(False, outcome.message)
            cancelled.append(row.installment_no)
        _audit(
            actor,
            CANCELLED_ACTION,
            ENTITY_PLAN,
            locked.pk,
            before,
            {**plan_state(locked), "note": text, "cancelled_installments": cancelled},
        )
        one_off_charges.recompute_parent(
            locked, actor=actor, context={"plan": str(locked.pk), "note": text}
        )
        parent = locked.parent
        parent.refresh_from_db()
    return Outcome(True, CANCELLED.format(balance=format_amount(balance_of(parent))), plan=locked)
