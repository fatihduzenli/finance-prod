"""Leases, rent history and autopay enrolment (docs/qa/PLAN-007-leases-and-rent-run.md).

The only module through which the lease screens reach the payment provider. Views call
the functions below; each one that needs the provider obtains it from
:func:`payments.get_provider` itself.

Rules kept here:

- **Rent** is never overwritten. A change closes the open ``lease_rent_history`` row on
  the day before the new rent takes effect and opens a new one (D5). The *current* rent
  is the row in effect today; a row that starts later is the *upcoming* rent.
- **Future-dated rent** (QA owner's decision, 2026-10-01): a schedule enabled before an
  upcoming change uses the rent in effect on its first ``DTSTART`` and is flagged
  ``needs_review`` at once ("Rent changes on <date>; ..."), so staff pause it and enable
  it again on or after that date.
- **One provider schedule per debit day** (D3): ``FREQ=MONTHLY``, ``DTSTART`` the next
  occurrence of the day at 12:00:00Z strictly in the future; the amount is the rent, or
  half of it for a twice-monthly lease with the odd cent on the first debit.
- **Check before create.** :func:`enable_autopay` locks the lease row and reads
  ``external_refs`` before any provider call; a schedule that already has a reference
  is never created again, so a second click creates nothing. A schedule whose last
  create failed is first looked for in the provider's list, in case the failed request
  took effect, and adopted when exactly one unlinked schedule matches.
- **Methods from the snapshot** (PLAN-010 Batch A follow-up): the enrolment page reads
  the payment methods of the lease's customer (PLAN-014 C3: the lease's own, or that of
  the lease whose account it shares, :func:`account_lease`) from the latest
  ``provider_snapshots`` row of that customer reference, written by
  :func:`refresh_payment_methods` in the worker; the page never calls the provider.
  :func:`enable_autopay` still reads the list from the provider before it creates
  anything.
- **Bank only.** The chosen payment method must be in the provider's list for the
  lease's customer, be a ``bank`` method and not be deleted; a card is refused even
  when its ID is posted directly (D2).
- **Large debits** (PLAN-007c §1), compared per debit: above ``PROVIDER_MAX_TXN_AMOUNT``
  (when set) enrolment is refused whatever is confirmed; else above
  ``LARGE_DEBIT_WARNING_AMOUNT`` it needs ``confirmed=True``, recorded in a
  ``lease.autopay_large_debit_confirmed`` audit row before any provider write.
- **Never an amount on an update** (D4): pause sends ``active=False``; resume sends
  ``active=True`` and a rule with a new future ``DTSTART``. A schedule that no longer
  matches the lease is flagged ``needs_review`` and cannot be resumed; staff pause it
  and enable autopay again, which ends the old row and creates a new schedule.
- **The first charge on save** (PLAN-012b-b D2): :func:`create_lease` and
  :func:`update_lease` run the rent run's catch-up for the lease in their transaction
  (:func:`ledger.services.rent_run.catch_up_lease`), so a lease that started on or
  before today shows its charges at once; a lease starting later gets nothing until the
  run on or after its start.
- **Ending a lease** (PLAN-010b Batch C): :func:`end_lease` sets the end date, the
  reason and the ``ended`` status; it is refused while autopay is on (pause it first).
- **Autopay moved for installments** (PLAN-012b-b D13): when a rent charge that the
  lease's active rent autopay would debit is split, the worker's release job calls
  :func:`move_autopay`: every active rent schedule of the lease is deleted at the
  provider as :func:`stop_autopay` deletes it (a 404 counts as already gone; the row is
  ``ended``, audited ``lease.autopay_deleted_for_move`` with the plan), then created
  again through the enrolment's create (:func:`_create_one`, the lease's linked bank
  method) as a new row: the split charge's debit (its seq) from the debit after that
  charge's debit instant (:func:`debit_instant` of its due date), so its first debit is
  next month's; any other debit from now, so its next debit is kept. Audited once per
  plan on the lease as :data:`MOVED_ACTION` with the plan, the skipped and the resumed
  dates and the new schedules (identifiers masked). A failure between the delete and
  the create leaves autopay off for that debit, the failure audited; a repeat of the job
  deletes nothing already deleted for the plan and creates what is missing (adopting a
  schedule a failed create made). The new schedules are live rent schedules: the lease
  end and the transfer list them as blockers until autopay is stopped. The lease page
  says "Autopay resumes <date> (this month is collected in installments)"
  (:func:`autopay_move`) while the plan is live and that debit is ahead.
  TODO-VERIFY (to record): the payment provider starts a schedule created with a
  ``DTSTART`` next month on that date, without catching up the occurrence skipped this
  month.

Every change writes one ``audit_log`` row; provider identifiers in it are masked.
Messages are fixed sentences; a provider rejection adds only the field names the
transport extracted (never a value).
"""

from __future__ import annotations

import calendar
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

import payments
from finance.dates import day_text
from ledger.models import (
    LIVE_PLAN_STATUSES,
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    InstallmentPlan,
    Lease,
    LeaseBilling,
    LeaseRentHistory,
    Operator,
    PaymentChannel,
    RentSource,
    ScheduleKind,
    ScheduleState,
    Store,
    SyncStatus,
)
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from ledger.wording import format_amount, provider_name, provider_sentence
from payments import jobs, lease_sync
from payments.errors import ProviderError, ProviderRejected
from payments.models import ExternalRef, JobKind, ProviderSnapshot
from payments.provider import (
    Amounts,
    LineItem,
    PaymentMethodRecord,
    PaymentProvider,
    ScheduleInput,
    ScheduleQuery,
    ScheduleRecord,
    ScheduleUpdate,
    mask_identifier,
)

ENTITY_LEASE = "lease"
#: The audit action of a lease whose rent autopay was moved past a split month (D13).
MOVED_ACTION = "collection_schedule.moved_for_installments"
#: The audit action of each schedule deleted by that move, with the plan.
DELETED_FOR_MOVE_ACTION = "lease.autopay_deleted_for_move"
ENTITY_SCHEDULE = "collection_schedule"
KIND_SCHEDULE = "schedule"
PROVIDER_KEY = lease_sync.PROVIDER_KEY

CENT = Decimal("0.01")
#: D3: every debit is scheduled at this UTC time of day.
DEBIT_HOUR_UTC = 12
#: TODO-VERIFY-036: how a monthly rule behaves on day 29 to 31 in a shorter month is not
#: in the specification. Such a day is refused for autopay until it is verified.
LAST_SCHEDULABLE_DAY = 28
#: The lease fields staff edit, in form order (rent is handled apart, D5).
LEASE_FIELDS: tuple[str, ...] = (
    "store",
    "operator",
    "debit_day",
    "second_debit_day",
    "payment_channel",
    "started_on",
    "collection_status",
    "prorate_first_month",
)
_RULE_DAY = re.compile(r"\ADTSTART=(\d{4})(\d{2})(\d{2})T")

# --- Fixed sentences ------------------------------------------------------------------

NO_OPERATOR = "The lease has no operator."
OPERATOR_ARCHIVED = "The operator is archived."
NOT_ACTIVE = "Only an active lease can be enrolled in autopay."
NOT_PROVIDER_CHANNEL = provider_sentence(
    "The payment channel is not bank debit through {provider}."
)
NO_DEBIT_DAY = "The lease has no debit day."
DAY_NOT_SCHEDULABLE = "Autopay cannot use the 29th, 30th or 31st. Choose a day from 1 to 28."
NO_RENT = "The lease has no rent."
NO_PROVIDER_CUSTOMER = provider_sentence("The operator is not connected to {provider}.")
#: PLAN-014 C5: a one-off charge without a lease whose operator has no active lease.
NO_LEASE_BILLING = "This charge has no lease to bill from."
NO_BANK_METHOD = "No bank method linked."
METHODS_NOT_READ = "The operator's payment methods have not been read yet."
METHODS_READ = "Payment methods read: {count}."
NO_EMAIL = "No e-mail: operator will not receive receipts."
SCHEDULE_DIFFERS = "The autopay amount differs from the rent: pause autopay and set it up again."
RENT_CHANGES = "Rent changes on {date}; pause and re-enable autopay on or after that date."
RENT_NOT_STARTED = "No rent is in effect on the first debit date ({date}). Nothing was created."
PAUSE_FIRST = "The autopay no longer matches the lease. Pause autopay first, then enable it again."
RESUME_REFUSED = (
    "The autopay no longer matches the lease, so it cannot be resumed. Enable autopay again."
)
ALREADY_ENABLED = "Autopay is already set up for every debit day. Nothing was created."
NOTHING_TO_PAUSE = "Autopay is not on."
NOTHING_TO_RESUME = "Autopay is not paused."
#: PLAN-012e D5 amendment: stopping autopay.
NOTHING_TO_STOP = "Autopay is already off."
NOTHING_TO_MOVE = provider_sentence(
    "No active rent autopay to move: nothing was changed in {provider}."
)
AUTOPAY_MOVED = "Autopay moved: it resumes {date}; this month is collected in installments."
AUTOPAY_RESUMES = "Autopay resumes {date} (this month is collected in installments)"
AUTOPAY_STOPPED = provider_sentence("Autopay is off: it was cancelled in {provider}.")
CHOOSE_METHOD = "Choose a bank payment method."
METHOD_NOT_FOUND = provider_sentence(
    "That payment method is not one of this operator's methods in {provider}. Nothing was created."
)
CARD_REFUSED = "Card payment methods are not allowed. Choose a bank method. Nothing was created."
METHOD_DELETED = provider_sentence(
    "That payment method is deleted in {provider}. Nothing was created."
)
SEVERAL_MATCHES = provider_sentence(
    "Several autopays in {provider} match a debit whose last create failed. Nothing was "
    "created; review them in the {provider} dashboard."
)
PROVIDER_REJECTED = provider_sentence("{provider} refused the request.")
PROVIDER_FAILED = provider_sentence("{provider} could not be reached. Try again later.")
#: PLAN-007c §1. Amounts are formatted by :func:`format_amount`.
LIMIT_EXCEEDED = provider_sentence(
    "Exceeds the {provider} debit limit of {limit}. Autopay cannot be enabled."
)
CONFIRM_LARGE_DEBIT = (
    "I confirm this debit of {amount} exceeds the warning threshold of {threshold} "
    "and may be enabled."
)
RENT_DATE_TOO_EARLY = (
    "The new rent must take effect at least two days after the current rent started ({start})."
)
#: Ending a lease (PLAN-010b Batch C).
ALREADY_ENDED = "This lease has already ended. Nothing was changed."
END_DATE_REQUIRED = "Enter the date the lease ended."
END_BEFORE_START = "The end date cannot be before the lease started ({start})."
END_IN_FUTURE = "The end date cannot be in the future."
END_REASON_REQUIRED = "A reason is required: say why the lease ended."
END_AUTOPAY_ON = (
    "Autopay is on for this lease. Pause autopay first, then end the lease. Nothing was changed."
)


class AutopayAction(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"
    RESUMED = "resumed"
    STOPPED = "stopped"
    MOVED = "moved"
    ALREADY = "already"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AutopayOutcome:
    action: AutopayAction
    message: str
    #: Key names a ``ProviderRejected`` body named; never a value.
    fields: tuple[str, ...] = ()
    #: Error class name of a provider failure, empty otherwise.
    failure: str = ""

    @property
    def ok(self) -> bool:
        return self.action in (
            AutopayAction.ENABLED,
            AutopayAction.PAUSED,
            AutopayAction.RESUMED,
            AutopayAction.STOPPED,
            AutopayAction.MOVED,
            AutopayAction.ALREADY,
        )

    @property
    def text(self) -> str:
        """The message, with the rejected field names when there are any."""
        if self.fields:
            return f"{self.message} Fields {provider_name()} rejected: {', '.join(self.fields)}."
        return self.message


@dataclass(frozen=True, slots=True)
class PlannedDebit:
    seq: int
    debit_day: int
    amount: Decimal
    #: The first debit (``DTSTART``) when planned for enrolment; ``None`` otherwise.
    starts: datetime | None = None
    #: An upcoming rent change after ``starts``: the schedule is flagged at creation.
    changes_on: date | None = None


@dataclass(frozen=True, slots=True)
class MethodChoices:
    """The operator's methods at the provider, for the enrolment screen."""

    methods: tuple[PaymentMethodRecord, ...] = ()
    #: A fixed sentence when the list could not be read; empty otherwise.
    error: str = ""
    fields: tuple[str, ...] = ()
    #: When the snapshot shown was read; ``None`` when no snapshot exists.
    fetched_at: datetime | None = None

    @property
    def has_bank(self) -> bool:
        return any(method.is_usable_bank for method in self.methods)


@dataclass(frozen=True, slots=True)
class MethodsRefresh:
    """The outcome of :func:`refresh_payment_methods`."""

    ok: bool
    message: str
    #: Key names a ``ProviderRejected`` body named; never a value.
    fields: tuple[str, ...] = ()
    #: Error class name of a provider failure, empty otherwise.
    failure: str = ""


class LeaseRefused(Exception):  # noqa: N818 (reads as an outcome, like the provider errors)
    """A change that the lease rules refuse; ``str(error)`` is a fixed sentence."""

    def __init__(self, field: str | None, message: str) -> None:
        super().__init__(message)
        self.field = field


# --- Money and dates ------------------------------------------------------------------


def debit_amounts(rent: Decimal, *, twice_monthly: bool) -> tuple[Decimal, ...]:
    """D3: the rent, or two halves with the odd cent on the first debit."""
    if not twice_monthly:
        return (rent,)
    second = (rent / 2).quantize(CENT, rounding=ROUND_DOWN)
    return (rent - second, second)


def next_debit_at(day: int, now: datetime) -> datetime:
    """The next ``day`` of a month at 12:00:00Z strictly after ``now`` (D3).

    A month without that day is skipped (only reachable for days 29 to 31, which
    :func:`autopay_blockers` refuses; TODO-VERIFY-036).
    """
    year, month = now.year, now.month
    while True:
        if day <= calendar.monthrange(year, month)[1]:
            candidate = datetime(year, month, day, DEBIT_HOUR_UTC, tzinfo=UTC)
            if candidate > now:
                return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def debit_instant(day: date) -> datetime:
    """The moment a debit due on ``day`` runs: 12:00:00Z that day (D3)."""
    return datetime(day.year, day.month, day.day, DEBIT_HOUR_UTC, tzinfo=UTC)


def schedule_rule(day: int, now: datetime) -> str:
    """``DTSTART=YYYYMMDDTHHMMSSZ;FREQ=MONTHLY`` (spec: DTSTART in the future, that format)."""
    return f"DTSTART={next_debit_at(day, now):%Y%m%dT%H%M%SZ};FREQ=MONTHLY"


def is_large_debit(amount: Decimal) -> bool:
    """Above the configured warning threshold (PLAN-007 §4.3); the amount itself is allowed."""
    threshold: Decimal = settings.LARGE_DEBIT_WARNING_AMOUNT
    return amount > threshold


def exceeds_provider_limit(amount: Decimal) -> bool:
    """Above ``PROVIDER_MAX_TXN_AMOUNT``; never when the limit is unknown (PLAN-007c §1)."""
    limit: Decimal | None = settings.PROVIDER_MAX_TXN_AMOUNT
    return limit is not None and amount > limit


def provider_limit_refusal(amounts: Iterable[Decimal]) -> str:
    """:data:`LIMIT_EXCEEDED` when any debit is above the confirmed limit, else ``""``."""
    limit: Decimal | None = settings.PROVIDER_MAX_TXN_AMOUNT
    if limit is None or not any(exceeds_provider_limit(amount) for amount in amounts):
        return ""
    return LIMIT_EXCEEDED.format(limit=format_amount(limit))


def largest_large_debit(amounts: Iterable[Decimal]) -> Decimal | None:
    """The largest debit above the warning threshold, or ``None``."""
    return max((amount for amount in amounts if is_large_debit(amount)), default=None)


def large_debit_confirmation(amounts: Iterable[Decimal]) -> str:
    """:data:`CONFIRM_LARGE_DEBIT` for the largest debit above the warning threshold,
    or ``""`` when none is above it."""
    largest = largest_large_debit(amounts)
    if largest is None:
        return ""
    return CONFIRM_LARGE_DEBIT.format(
        amount=format_amount(largest),
        threshold=format_amount(settings.LARGE_DEBIT_WARNING_AMOUNT),
    )


# --- Reads ----------------------------------------------------------------------------


def rent_on(lease: Lease, day: date) -> LeaseRentHistory | None:
    """The rent row in effect on ``day``: started on or before it and not ended before it."""
    return (
        lease.rent_history.filter(effective_from__lte=day)
        .exclude(effective_to__lt=day)
        .order_by("-effective_from")
        .first()
    )


def current_rent(lease: Lease, today: date | None = None) -> LeaseRentHistory | None:
    """The rent row in effect today (QA owner's decision on future-dated rent)."""
    return rent_on(lease, today or timezone.localdate())


def upcoming_rent(lease: Lease, today: date | None = None) -> LeaseRentHistory | None:
    """The next rent row that starts after today, if any."""
    day = today or timezone.localdate()
    return lease.rent_history.filter(effective_from__gt=day).order_by("effective_from").first()


def open_rent(lease: Lease) -> LeaseRentHistory | None:
    """The latest rent row, the one with no end (one at most, by constraint).

    It is the row a rent edit compares against and closes; it may start in the future.
    """
    return lease.rent_history.filter(effective_to__isnull=True).first()


def planned_debits(lease: Lease, rent: Decimal) -> list[PlannedDebit]:
    """One debit per debit day of the lease, in seq order."""
    days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
    amounts = debit_amounts(rent, twice_monthly=len(days) == 2)
    return [
        PlannedDebit(seq, day, amount)
        for seq, (day, amount) in enumerate(zip(days, amounts, strict=True), start=1)
    ]


def debit_plan(lease: Lease, now: datetime) -> list[PlannedDebit]:
    """The debits enrolment would create now: each at the rent in effect on its first
    ``DTSTART``, with the date of any later rent change (QA owner's decision).

    A debit with no rent in effect on its first date is left out; :func:`enable_autopay`
    refuses the lease in that case.
    """
    days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
    plan: list[PlannedDebit] = []
    for seq, day in enumerate(days, start=1):
        starts = next_debit_at(day, now)
        row = rent_on(lease, starts.date())
        if row is None:
            continue
        amount = debit_amounts(row.amount, twice_monthly=len(days) == 2)[seq - 1]
        later = upcoming_rent(lease, starts.date())
        plan.append(
            PlannedDebit(
                seq,
                day,
                amount,
                starts=starts,
                changes_on=later.effective_from if later is not None else None,
            )
        )
    return plan


def schedule_review_message(
    lease: Lease, row: CollectionSchedule, today: date | None = None
) -> str:
    """Why a flagged schedule needs review, or ``""`` when it is not flagged.

    A schedule that still matches today's rent but meets an upcoming change gets the
    dated message; any other mismatch gets :data:`SCHEDULE_DIFFERS`.
    """
    if not row.needs_review:
        return ""
    day = today or timezone.localdate()
    later = upcoming_rent(lease, day)
    now_row = current_rent(lease, day)
    if later is not None and now_row is not None:
        planned = {debit.seq: debit for debit in planned_debits(lease, now_row.amount)}
        debit = planned.get(row.seq)
        if debit is not None and (debit.debit_day, debit.amount) == (row.debit_day, row.amount):
            return RENT_CHANGES.format(date=day_text(later.effective_from))
    return SCHEDULE_DIFFERS


def live_schedules(lease: Lease) -> list[CollectionSchedule]:
    """The lease's rent schedules that are not ended, in seq order."""
    return list(lease.schedules.filter(kind=ScheduleKind.RENT).exclude(state=ScheduleState.ENDED))


def schedule_ref(schedule: CollectionSchedule) -> str:
    """The provider schedule ID of a row, or ``""`` (unmasked; callers mask)."""
    ref = ExternalRef.objects.filter(
        entity_type=ENTITY_SCHEDULE,
        entity_id=schedule.pk,
        provider=PROVIDER_KEY,
        external_kind=KIND_SCHEDULE,
        superseded_at__isnull=True,
    ).first()
    return ref.external_id if ref is not None else ""


def account_lease(lease: Lease | None) -> Lease | None:
    """The lease whose processor customer bills ``lease`` (PLAN-014 C3): the lease it
    shares the account of, else itself; one hop, never further."""
    if lease is None:
        return None
    billing = lease_sync.billing_of(lease)
    if billing.shares_account_of_id is not None:
        return billing.shares_account_of
    return lease


def customer_ref(lease: Lease | None) -> str:
    """The provider customer ID that bills the lease, or ``""`` (unmasked)."""
    owner = account_lease(lease)
    if owner is None:
        return ""
    return lease_sync.current_refs(owner).get(lease_sync.KIND_CUSTOMER, "")


def linked_payment_method(lease: Lease | None) -> str:
    """The payment-method ID staff linked to the lease's account, or ``""`` (unmasked)."""
    owner = account_lease(lease)
    if owner is None:
        return ""
    return lease_sync.current_refs(owner).get(lease_sync.KIND_PAYMENT_METHOD, "")


def first_active_lease(operator: Operator | None) -> Lease | None:
    """The operator's first active lease (collection active or paused, not ended): the
    earliest start (none last), then the lowest store number (PLAN-014 C5)."""
    if operator is None:
        return None
    return (
        Lease.objects.filter(
            operator=operator,
            ended_on__isnull=True,
            collection_status__in=[CollectionStatus.ACTIVE, CollectionStatus.PAUSED],
        )
        .select_related("store", "operator")
        .order_by(
            F("started_on").asc(nulls_last=True),
            F("store__store_number").asc(nulls_last=True),
            "id",
        )
        .first()
    )


def first_active_leases(operator_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Lease]:
    """:func:`first_active_lease` of each of these operators that has one, with its billing
    row (one query)."""
    found: dict[uuid.UUID, Lease] = {}
    rows = (
        Lease.objects.filter(
            operator_id__in=list(operator_ids),
            ended_on__isnull=True,
            collection_status__in=[CollectionStatus.ACTIVE, CollectionStatus.PAUSED],
        )
        .select_related("store", "operator", "billing")
        .order_by(
            F("started_on").asc(nulls_last=True),
            F("store__store_number").asc(nulls_last=True),
            "id",
        )
    )
    for lease in rows:
        if lease.operator_id is not None:
            found.setdefault(lease.operator_id, lease)
    return found


def billing_lease(charge: Any) -> Lease | None:
    """The lease whose billing details and account bill ``charge`` (PLAN-014 C5): a rent
    or insurance charge's lease; a one-off charge's lease, else its operator's first
    active lease; ``None`` when there is none (callers refuse with
    :data:`NO_LEASE_BILLING`)."""
    lease: Lease | None = getattr(charge, "lease", None)
    if lease is not None:
        return lease
    return first_active_lease(getattr(charge, "operator", None))


def billing_for(charge: Any) -> LeaseBilling | None:
    """The billing details of :func:`billing_lease`, or ``None``."""
    lease = billing_lease(charge)
    return lease_sync.billing_of(lease) if lease is not None else None


def lease_for_creation_key(raw: object) -> Lease | None:
    """The lease a create form with this key already made, if any."""
    try:
        key = uuid.UUID(str(raw))
    except ValueError:
        return None
    return _by_creation_key(key)


def autopay_blockers(lease: Lease) -> list[str]:
    """Why autopay cannot be enabled now, from local data only (no provider call)."""
    blockers: list[str] = []
    operator = lease.operator
    if operator is None:
        blockers.append(NO_OPERATOR)
    elif operator.archived_at is not None:
        blockers.append(OPERATOR_ARCHIVED)
    if lease.collection_status != CollectionStatus.ACTIVE:
        blockers.append(NOT_ACTIVE)
    if lease.payment_channel != PaymentChannel.PROVIDER_ACH:
        blockers.append(NOT_PROVIDER_CHANNEL)
    if lease.debit_day is None:
        blockers.append(NO_DEBIT_DAY)
    elif any(
        day > LAST_SCHEDULABLE_DAY
        for day in (lease.debit_day, lease.second_debit_day)
        if day is not None
    ):
        blockers.append(DAY_NOT_SCHEDULABLE)
    if open_rent(lease) is None:
        blockers.append(NO_RENT)
    if operator is not None and not customer_ref(lease):
        blockers.append(NO_PROVIDER_CUSTOMER)
    return blockers


def enrolment_notes(lease: Lease) -> list[str]:
    """Information that does not block enrolment."""
    operator = lease.operator
    notes: list[str] = []
    if operator is None:
        return notes
    if not linked_payment_method(lease):
        notes.append(NO_BANK_METHOD)
    if not lease_sync.billing_of(lease).email:
        notes.append(NO_EMAIL)
    return notes


#: The fields of a payment-method record kept in its snapshot (Rule 1: nothing else).
METHOD_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "external_id",
    "raw_status",
    "method",
    "last_four",
    "bank_type",
    "nickname",
    "is_default",
)


def _customer_ref_row(lease: Lease | None) -> ExternalRef | None:
    """The customer reference that bills the lease (through a shared account)."""
    owner = account_lease(lease)
    if owner is None:
        return None
    return ExternalRef.objects.filter(
        entity_type=lease_sync.ENTITY_LEASE,
        entity_id=owner.pk,
        provider=PROVIDER_KEY,
        external_kind=lease_sync.KIND_CUSTOMER,
        superseded_at__isnull=True,
    ).first()


def _method_snapshots(ref: ExternalRef) -> list[ProviderSnapshot]:
    """The method-list snapshots of a customer reference, newest first. The customer
    webhook keeps the customer object (a JSON object) under the same reference; a method
    list is a JSON array."""
    return [
        snapshot
        for snapshot in ref.snapshots.order_by("-fetched_at", "-id")
        if isinstance(snapshot.payload, list)
    ]


def _method_payload(method: PaymentMethodRecord) -> dict[str, str | bool]:
    return {name: getattr(method, name) for name in METHOD_SNAPSHOT_FIELDS}


def _method_from_payload(item: object, fetched_at: datetime) -> PaymentMethodRecord | None:
    if not isinstance(item, dict) or not isinstance(item.get("external_id"), str):
        return None
    values: dict[str, Any] = {}
    for name in METHOD_SNAPSHOT_FIELDS:
        value = item.get(name)
        wanted = bool if name == "is_default" else str
        if isinstance(value, wanted):
            values[name] = value
    values.setdefault("raw_status", "")
    return PaymentMethodRecord(fetched_at=fetched_at, **values)


def payment_method_choices(lease: Lease | None) -> MethodChoices:
    """The methods of the customer that bills the lease, from the latest snapshot (no
    provider call), or why there are none. ``fetched_at`` is ``None`` when the methods
    were never read."""
    ref = _customer_ref_row(lease)
    if ref is None:
        return MethodChoices(error=NO_PROVIDER_CUSTOMER)
    snapshots = _method_snapshots(ref)
    if not snapshots:
        return MethodChoices()
    latest = snapshots[0]
    methods = (_method_from_payload(item, latest.fetched_at) for item in latest.payload)
    return MethodChoices(
        methods=tuple(method for method in methods if method is not None),
        fetched_at=latest.fetched_at,
    )


def linked_method_record(lease: Lease | None) -> PaymentMethodRecord | None:
    """The lease's linked payment method (through a shared account) as the latest stored
    snapshot shows it, or ``None`` when nothing is linked or the snapshot does not list it
    (no provider call)."""
    linked = linked_payment_method(lease)
    if not linked:
        return None
    return next(
        (
            method
            for method in payment_method_choices(lease).methods
            if method.external_id == linked
        ),
        None,
    )


def bank_last4(method: PaymentMethodRecord | None) -> str:
    """The four digits ``LeaseBilling.bank_last4`` caches, or ``""``."""
    digits = method.last_four if method is not None else ""
    return digits if len(digits) == 4 and digits.isdigit() else ""


def refresh_payment_methods(lease_id: uuid.UUID) -> MethodsRefresh:
    """Read the payment methods of the customer that bills the lease from the provider
    (one list call) and keep them as the one method-list snapshot of that customer
    reference (worker only); the linked method's last four are cached on the account
    lease's billing row (``bank_last4``).

    Nothing is written before the call; a failure keeps the previous snapshot. The new
    snapshot replaces the previous ones in one transaction.
    """
    lease = Lease.objects.select_related("operator", "store").filter(pk=lease_id).first()
    ref = _customer_ref_row(lease)
    if ref is None:
        return MethodsRefresh(False, NO_PROVIDER_CUSTOMER)
    try:
        methods = payments.get_provider().list_payment_methods(ref.external_id)
    except ProviderError as error:
        return MethodsRefresh(
            False, _failure_message(error), _fields(error), lease_sync.failure_name(error)
        )
    with transaction.atomic():
        _store_methods(ref, methods, account_lease(lease))
    return MethodsRefresh(True, METHODS_READ.format(count=len(methods)))


def _store_methods(
    ref: ExternalRef, methods: list[PaymentMethodRecord], owner: Lease | None
) -> None:
    """Keep ``methods`` as the one method-list snapshot of the customer reference and
    cache the linked method's last four on ``owner``'s billing row (the caller's
    transaction)."""
    fetched_at = max((method.fetched_at for method in methods), default=timezone.now())
    old = [snapshot.pk for snapshot in _method_snapshots(ref)]
    ProviderSnapshot.objects.create(
        external_ref=ref,
        payload=[_method_payload(method) for method in methods],
        fetched_at=fetched_at,
    )
    ProviderSnapshot.objects.filter(pk__in=old).delete()
    if owner is not None:
        linked = linked_payment_method(owner)
        digits = bank_last4(next((m for m in methods if m.external_id == linked), None))
        LeaseBilling.objects.update_or_create(lease=owner, defaults={"bank_last4": digits})


# --- Bank account entry (PLAN-014 C7) ---------------------------------------------------

#: Refusal reasons of :func:`attach_bank_method` (stored in its audit row; never shown).
ATTACH_INVALID_ID = "InvalidId"
ATTACH_ARCHIVED = "Archived"
ATTACH_SHARES_ACCOUNT = "SharesAccount"
ATTACH_NO_CUSTOMER = "NoProviderCustomer"
ATTACH_LINKED_ELSEWHERE = "AlreadyLinkedElsewhere"
ATTACH_NOT_THIS_CUSTOMER = "NotThisCustomer"
ATTACH_NOT_BANK = "NotBankMethod"
#: The sentence of each refusal; a provider failure has its own (:data:`ATTACH_FAILED`).
ATTACH_MESSAGES: dict[str, str] = {
    ATTACH_INVALID_ID: "That is not a bank method ID. Nothing was saved.",
    ATTACH_ARCHIVED: "This operator is archived.",
    ATTACH_SHARES_ACCOUNT: (
        "This lease uses another lease's account. Switch it to its own account first. "
        "Nothing was saved."
    ),
    ATTACH_NO_CUSTOMER: provider_sentence(
        "The lease is not connected to {provider} yet. Save its billing details with an "
        "e-mail first. Nothing was saved."
    ),
    ATTACH_LINKED_ELSEWHERE: provider_sentence(
        "That {provider} bank method is already connected to another lease. Nothing was saved."
    ),
    ATTACH_NOT_THIS_CUSTOMER: provider_sentence(
        "That bank method does not belong to this lease's {provider} customer. Nothing was saved."
    ),
    ATTACH_NOT_BANK: "That payment method is not a usable bank account. Nothing was saved.",
}
ATTACH_FAILED = provider_sentence(
    "{provider} could not confirm the bank account. Nothing was saved; try again later."
)
ATTACH_ALREADY = "This bank account is already connected to the lease. Nothing was changed."
ATTACH_DONE = "Bank account ····{last4} connected."
ATTACH_DONE_NO_DIGITS = "Bank account connected."


@dataclass(frozen=True, slots=True)
class BankAttach:
    """The outcome of :func:`attach_bank_method`."""

    ok: bool
    message: str
    #: A refusal reason of :data:`ATTACH_MESSAGES` or a provider error's class name.
    reason: str = ""


def attach_bank_method(
    lease_id: uuid.UUID, payment_method_external_id: str, *, actor: User | None
) -> BankAttach:
    """Verify and link the bank method the hosted fields created under the lease's own
    customer (worker only; ``JobKind.LEASE_METHOD_ATTACH``).

    Under the lease's row lock: the identifier's syntax, the operator not archived, the
    lease not sharing another's account, its own customer reference, and the method not
    linked to any other entity. Then one provider call reads the customer's payment
    methods (the per-customer list: a method of another customer is not in it, and the
    adapter drops any object naming another customer); the id must be in it and be a usable
    bank method. Only then is the lease's current ``payment_method`` reference superseded
    and the new one written, the list kept as the customer's method snapshot and the last
    four cached (``LeaseBilling.bank_last4``). Audited ``lease.bank_attached`` (masked
    identifiers, the last four) or ``lease.bank_attach_refused`` (the reason name); a
    method already linked to the lease changes nothing and makes no call.
    """
    with transaction.atomic():
        lease = (
            Lease.objects.select_for_update().select_related("operator", "store").get(pk=lease_id)
        )
        reason = _attach_refusal(lease, payment_method_external_id)
        if reason:
            return _attach_refused(actor, lease, reason, payment_method_external_id)
        refs = lease_sync.current_refs(lease)
        customer = refs[lease_sync.KIND_CUSTOMER]
        previous = refs.get(lease_sync.KIND_PAYMENT_METHOD, "")
        if previous == payment_method_external_id:
            return BankAttach(True, ATTACH_ALREADY)
        try:
            methods = payments.get_provider().list_payment_methods(customer)
        except ProviderError as error:
            return _attach_refused(
                actor, lease, lease_sync.failure_name(error), payment_method_external_id
            )
        chosen = next((m for m in methods if m.external_id == payment_method_external_id), None)
        if chosen is None:
            return _attach_refused(
                actor, lease, ATTACH_NOT_THIS_CUSTOMER, payment_method_external_id
            )
        if not chosen.is_usable_bank:
            return _attach_refused(actor, lease, ATTACH_NOT_BANK, payment_method_external_id)
        now = timezone.now()
        ExternalRef.objects.filter(
            entity_type=lease_sync.ENTITY_LEASE,
            entity_id=lease.pk,
            provider=PROVIDER_KEY,
            external_kind=lease_sync.KIND_PAYMENT_METHOD,
            superseded_at__isnull=True,
        ).update(superseded_at=now)
        ExternalRef.objects.create(
            entity_type=lease_sync.ENTITY_LEASE,
            entity_id=lease.pk,
            provider=PROVIDER_KEY,
            external_kind=lease_sync.KIND_PAYMENT_METHOD,
            external_id=payment_method_external_id,
        )
        customer_row = _customer_ref_row(lease)
        if customer_row is not None:
            _store_methods(customer_row, methods, lease)
        digits = bank_last4(chosen)
        _audit(
            actor,
            "lease.bank_attached",
            lease,
            {"payment_method": mask_identifier(previous) if previous else ""},
            {
                "customer": mask_identifier(customer),
                "payment_method": mask_identifier(payment_method_external_id),
                "last4": digits,
            },
        )
    return BankAttach(True, ATTACH_DONE.format(last4=digits) if digits else ATTACH_DONE_NO_DIGITS)


def _attach_refusal(lease: Lease, payment_method_external_id: str) -> str:
    """Why :func:`attach_bank_method` refuses before any provider call, or ``""``."""
    if not lease_sync.IDENTIFIER.match(payment_method_external_id or ""):
        return ATTACH_INVALID_ID
    if lease.operator is not None and lease.operator.archived_at is not None:
        return ATTACH_ARCHIVED
    if lease_sync.billing_of(lease).shares_account_of_id is not None:
        return ATTACH_SHARES_ACCOUNT
    if not lease_sync.current_refs(lease).get(lease_sync.KIND_CUSTOMER):
        return ATTACH_NO_CUSTOMER
    elsewhere = (
        ExternalRef.objects.filter(
            provider=PROVIDER_KEY,
            external_kind=lease_sync.KIND_PAYMENT_METHOD,
            external_id=payment_method_external_id,
        )
        .exclude(entity_type=lease_sync.ENTITY_LEASE, entity_id=lease.pk)
        .exists()
    )
    return ATTACH_LINKED_ELSEWHERE if elsewhere else ""


def _attach_refused(
    actor: User | None, lease: Lease, reason: str, payment_method_external_id: str
) -> BankAttach:
    """Audit a refused attach (the reason name and the masked id only) and report it."""
    masked = (
        mask_identifier(payment_method_external_id)
        if lease_sync.IDENTIFIER.match(payment_method_external_id or "")
        else ""
    )
    _audit(
        actor,
        "lease.bank_attach_refused",
        lease,
        None,
        {"reason": reason, "payment_method": masked},
    )
    return BankAttach(False, ATTACH_MESSAGES.get(reason, ATTACH_FAILED), reason)


# --- The bank account of a new lease (PLAN-014 C4) ---------------------------------------

#: The bank-account choices of the New lease and Transfer billing details: no account yet
#: (the lease gets its own processor customer and no method), the hosted fields after the
#: save (C7), or ``same:<lease id>``, the account of another lease of the same operator.
ACCOUNT_LATER = "later"
ACCOUNT_ENTER = "enter"
ACCOUNT_SAME = "same:"
ACCOUNT_INVALID = "Choose how the bank account is set up."
ACCOUNT_NOT_SHAREABLE = (
    "That lease's bank account cannot be used for this lease: it belongs to another "
    "operator, has no usable bank method or uses another lease's account. Choose again."
)


@dataclass(frozen=True, slots=True)
class SharedAccount:
    """A lease whose account a new lease of the same operator may use, with the last four
    of its bank method ("Use the same account as #1991 (····4821)")."""

    lease: Lease
    last4: str

    @property
    def value(self) -> str:
        return f"{ACCOUNT_SAME}{self.lease.pk}"


def shareable_accounts(operator_ids: Iterable[uuid.UUID]) -> list[SharedAccount]:
    """The leases of these operators, not ended, that hold their own processor customer
    and a usable linked bank method and do not use another lease's account, by store
    number (local data only: the latest method snapshot)."""
    leases = Lease.objects.filter(operator_id__in=list(operator_ids), ended_on__isnull=True)
    own = ExternalRef.objects.filter(
        entity_type=lease_sync.ENTITY_LEASE,
        provider=PROVIDER_KEY,
        superseded_at__isnull=True,
        entity_id__in=leases.values("pk"),
    )
    with_customer = own.filter(external_kind=lease_sync.KIND_CUSTOMER).values("entity_id")
    with_method = own.filter(external_kind=lease_sync.KIND_PAYMENT_METHOD).values("entity_id")
    candidates = (
        leases.filter(pk__in=with_customer)
        .filter(pk__in=with_method)
        .exclude(collection_status=CollectionStatus.ENDED)
        .exclude(billing__shares_account_of__isnull=False)
        .select_related("store", "operator")
        .order_by(F("store__store_number").asc(nulls_last=True), "store__external_label", "id")
    )
    found = []
    for lease in candidates:
        method = linked_method_record(lease)
        if method is not None and method.is_usable_bank:
            found.append(SharedAccount(lease, bank_last4(method)))
    return found


def shared_account(operator: Operator | None, account: str) -> Lease | None:
    """The lease whose account ``account`` names (``same:<lease id>``), checked again
    under its row lock: of ``operator``, not ended, its own customer and a usable bank
    method, not itself sharing; ``None`` for :data:`ACCOUNT_LATER` and
    :data:`ACCOUNT_ENTER`. Raises :class:`LeaseRefused` otherwise."""
    if account in (ACCOUNT_LATER, ACCOUNT_ENTER):
        return None
    if not account.startswith(ACCOUNT_SAME):
        raise LeaseRefused("account", ACCOUNT_INVALID)
    try:
        key = uuid.UUID(account.removeprefix(ACCOUNT_SAME))
    except ValueError:
        raise LeaseRefused("account", ACCOUNT_INVALID) from None
    other = Lease.objects.select_for_update().filter(pk=key).first()
    if (
        other is None
        or operator is None
        or other.operator_id != operator.pk
        or other.pk not in {found.lease.pk for found in shareable_accounts([operator.pk])}
    ):
        raise LeaseRefused("account", ACCOUNT_NOT_SHAREABLE)
    return other


def start_billing(
    lease: Lease, billing: Mapping[str, Any], account: str, *, actor: User | None
) -> LeaseBilling:
    """Write a new lease's billing details and its bank-account choice (the caller's
    transaction; PLAN-014 C4). ``same:<lease id>`` shares that lease's account
    (:func:`shared_account`, refused with :class:`LeaseRefused`); otherwise the lease is
    its own customer: with a billing e-mail it is marked ``pending`` and one
    ``lease_sync`` is queued (the part-1 rule), without one it waits for a manual link.
    Nothing is copied from the operator. Audited ``lease_billing.changed``."""
    shares = shared_account(lease.operator, account)
    row = LeaseBilling(lease=lease, shares_account_of=shares)
    for name in lease_sync.BILLING_FIELDS:
        setattr(row, name, billing.get(name) or "")
    row.save()
    lease.billing = row
    queue = False
    if shares is None and lease.operator is not None:
        queue = bool(row.email)
        lease.sync_status = SyncStatus.PENDING if queue else SyncStatus.AWAITING_LINK
        lease.save(update_fields=["sync_status", "updated_at"])
    _audit(actor, "lease_billing.changed", lease, None, lease_sync.billing_snapshot(row))
    if queue:
        jobs.enqueue(JobKind.LEASE_SYNC, lease, None, actor)
    return row


# --- Lease writes ---------------------------------------------------------------------


def create_lease(
    fields: Mapping[str, Any],
    *,
    rent: Decimal,
    creation_key: uuid.UUID,
    actor: User | None,
    today: date | None = None,
    billing: Mapping[str, Any] | None = None,
    account: str = ACCOUNT_LATER,
) -> tuple[Lease, bool]:
    """Save a new lease and its first rent row, or return the lease this key made.

    The key is recorded in the ``lease.created`` audit row; the store row is locked
    while it is looked up, so two submissions of one form make one lease. With
    ``billing`` (PLAN-014 C4), the lease's billing details and bank-account choice are
    written in the same transaction (:func:`start_billing`); a refused choice raises
    :class:`LeaseRefused` and nothing is saved.
    """
    values = _editable(fields)
    store: Store = values["store"]
    with transaction.atomic():
        Store.objects.select_for_update().get(pk=store.pk)
        existing = _by_creation_key(creation_key)
        if existing is not None:
            return existing, False
        lease = Lease(**values)
        lease.save()
        LeaseRentHistory.objects.create(
            lease=lease,
            amount=rent,
            effective_from=lease.started_on or today or timezone.localdate(),
            source=RentSource.STAFF,
        )
        _audit(
            actor,
            "lease.created",
            lease,
            None,
            {**_snapshot(lease), "creation_key": str(creation_key)},
        )
        _catch_up(lease, actor, today)
        if billing is not None:
            start_billing(lease, billing, account, actor=actor)
    return lease, True


def update_lease(
    lease_id: uuid.UUID,
    fields: Mapping[str, Any],
    *,
    rent: Decimal,
    rent_effective_from: date | None,
    actor: User | None,
    today: date | None = None,
) -> Lease:
    """Apply staff edits under a row lock (D5).

    A rent change closes the open rent row the day before ``rent_effective_from``
    (today when empty) and opens a new one; it raises :class:`LeaseRefused` when that
    date is not at least two days after the open row started. Any schedule with a
    provider reference that no longer matches the lease is flagged ``needs_review``.
    """
    with transaction.atomic():
        lease = Lease.objects.select_for_update().get(pk=lease_id)
        before = _snapshot(lease)
        old_operator_id = lease.operator_id
        for name, value in _editable(fields).items():
            setattr(lease, name, value)
        lease.save()
        open_row = open_rent(lease)
        if open_row is None or open_row.amount != rent:
            starts = rent_effective_from or today or timezone.localdate()
            if open_row is not None:
                closes = starts - timedelta(days=1)
                if closes <= open_row.effective_from:
                    raise LeaseRefused(
                        "rent_effective_from",
                        RENT_DATE_TOO_EARLY.format(start=day_text(open_row.effective_from)),
                    )
                open_row.effective_to = closes
                open_row.save(update_fields=["effective_to"])
            LeaseRentHistory.objects.create(
                lease=lease, amount=rent, effective_from=starts, source=RentSource.STAFF
            )
        _flag_stale_schedules(lease, rent, operator_changed=lease.operator_id != old_operator_id)
        after = _snapshot(lease)
        if after != before:
            _audit(actor, "lease.updated", lease, before, after)
        _catch_up(lease, actor, today)
    return lease


def _catch_up(lease: Lease, actor: User | None, today: date | None) -> None:
    """PLAN-012b-b D2: the lease's missing charges up to today, in the caller's
    transaction (nothing for a lease that starts later or is not active)."""
    from ledger.services import rent_run  # rent_run imports this module

    rent_run.catch_up_lease(lease, today or timezone.localdate(), actor)


def end_lease(
    lease_id: uuid.UUID,
    ended_on: date | None,
    end_reason: str,
    *,
    actor: User | None,
    today: date | None = None,
    reason: str = "",
) -> Lease:
    """Set the lease's end date and reason and its status to ``ended`` (PLAN-010b Batch C).

    Refused, with one sentence and nothing written, when the lease has already ended,
    the date is missing, before the start date or in the future, the reason is empty
    or holds a card or account number, or a rent schedule is active at the payment
    provider (autopay must be paused first; nothing here calls the provider). Checked
    under the lease's row lock and audited as ``lease.ended``; ``reason`` is the
    end-of-lease wizard's reason code (PLAN-012e D5), kept in the audit row.
    """
    text = visible_text(end_reason or "")
    with transaction.atomic():
        lease = _locked(lease_id)
        if lease.ended_on is not None:
            raise LeaseRefused(None, ALREADY_ENDED)
        if ended_on is None:
            raise LeaseRefused("ended_on", END_DATE_REQUIRED)
        if ended_on > (today or timezone.localdate()):
            raise LeaseRefused("ended_on", END_IN_FUTURE)
        if lease.started_on is not None and ended_on < lease.started_on:
            raise LeaseRefused(
                "ended_on", END_BEFORE_START.format(start=day_text(lease.started_on))
            )
        if not text:
            raise LeaseRefused("end_reason", END_REASON_REQUIRED)
        try:
            validate_no_payment_data(text)
        except ValidationError:
            raise LeaseRefused("end_reason", PAYMENT_DATA_MESSAGE) from None
        if autopay_on(lease):
            raise LeaseRefused(None, END_AUTOPAY_ON)
        before = {**_snapshot(lease), **_end_snapshot(lease)}
        lease.ended_on = ended_on
        lease.end_reason = text
        lease.collection_status = CollectionStatus.ENDED
        lease.save(update_fields=["ended_on", "end_reason", "collection_status", "updated_at"])
        after = {**_snapshot(lease), **_end_snapshot(lease)}
        if reason:
            after["reason"] = reason
        _audit(actor, "lease.ended", lease, before, after)
    return lease


def autopay_on(lease: Lease) -> bool:
    """Whether a rent schedule of the lease is active at the payment provider: the
    state the lease page offers "Pause autopay" for (local data only)."""
    return any(
        row.state == ScheduleState.ACTIVE and schedule_ref(row) for row in live_schedules(lease)
    )


def _end_snapshot(lease: Lease) -> dict[str, Any]:
    return {
        "ended_on": lease.ended_on.isoformat() if lease.ended_on else None,
        "end_reason": lease.end_reason,
    }


# --- Autopay --------------------------------------------------------------------------


def enable_autopay(
    lease_id: uuid.UUID,
    payment_method_external_id: str,
    *,
    actor: User | None,
    confirmed: bool = False,
    now: datetime | None = None,
) -> AutopayOutcome:
    """Create one provider schedule per debit day that has none (D3, D2).

    Order: identifier syntax; lease row lock; local blockers; ``external_refs`` (a
    lease whose every debit already has a schedule returns ``ALREADY`` with no provider
    call); the per-debit amounts of the debits to create (PLAN-007c: above the confirmed
    provider limit is refused; above the warning threshold is refused unless
    ``confirmed`` is ``True``; both with no provider call); the method list for the
    lease's customer (the chosen ID must be a bank method in it); a delete of every
    stale (flagged, paused) schedule, a 404 counting as already gone, where any other
    failure ends nothing and creates nothing; then one create per missing debit. A
    create failure stops at that debit, marks its row ``failed`` and changes nothing on
    the lease; schedules already created stay linked.
    A confirmed large debit is audited after the method check, before any provider write.
    """
    if not lease_sync.IDENTIFIER.match(payment_method_external_id or ""):
        return AutopayOutcome(AutopayAction.REFUSED, CHOOSE_METHOD)
    moment = now or timezone.now()
    with transaction.atomic():
        lease = _locked(lease_id)
        blockers = autopay_blockers(lease)
        if blockers:
            return AutopayOutcome(AutopayAction.REFUSED, blockers[0])
        operator = lease.operator
        if operator is None:  # refused by the blockers already
            return AutopayOutcome(AutopayAction.REFUSED, NO_OPERATOR)
        plan = debit_plan(lease, moment)
        days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
        if len(plan) != len(days):
            first = min(next_debit_at(day, moment) for day in days)
            return AutopayOutcome(
                AutopayAction.REFUSED, RENT_NOT_STARTED.format(date=day_text(first.date()))
            )
        live = {row.seq: (row, schedule_ref(row)) for row in live_schedules(lease)}
        if any(
            ref and row.needs_review and row.state == ScheduleState.ACTIVE
            for row, ref in live.values()
        ):
            return AutopayOutcome(AutopayAction.REFUSED, PAUSE_FIRST)
        stale = [row for row, ref in live.values() if ref and row.needs_review]
        missing = [
            debit
            for debit in plan
            if debit.seq not in live or not live[debit.seq][1] or live[debit.seq][0] in stale
        ]
        if not missing and not stale:
            return AutopayOutcome(AutopayAction.ALREADY, ALREADY_ENABLED)
        amounts = [debit.amount for debit in missing]
        limit_refusal = provider_limit_refusal(amounts)
        if limit_refusal:
            return AutopayOutcome(AutopayAction.REFUSED, limit_refusal)
        large = largest_large_debit(amounts)
        if large is not None and confirmed is not True:
            return AutopayOutcome(AutopayAction.REFUSED, large_debit_confirmation(amounts))

        provider = payments.get_provider()
        customer = customer_ref(lease)
        try:
            methods = provider.list_payment_methods(customer)
        except ProviderError as error:
            return _failed(actor, lease, "lease.autopay_failed", error, {})
        chosen = next((m for m in methods if m.external_id == payment_method_external_id), None)
        refusal = _method_refusal(chosen)
        if refusal:
            _audit(
                actor,
                "lease.autopay_refused",
                lease,
                None,
                {
                    "payment_method": mask_identifier(payment_method_external_id),
                    "reason": str(refusal),
                },
            )
            return AutopayOutcome(AutopayAction.REFUSED, refusal)
        if large is not None:
            _audit(
                actor,
                "lease.autopay_large_debit_confirmed",
                lease,
                None,
                {
                    "amount": str(large),
                    "threshold": str(settings.LARGE_DEBIT_WARNING_AMOUNT),
                    "confirmed_by": actor.pk if actor is not None else None,
                },
            )

        # PLAN-007a: every stale schedule is deleted at the provider (a 404 means it is
        # already gone) before any row is ended or any create is sent.
        for row in stale:
            ref = schedule_ref(row)
            try:
                provider.delete_scheduled_invoice(ref)
            except ProviderRejected as error:
                if error.status != HTTPStatus.NOT_FOUND:
                    return _failed(
                        actor, lease, "lease.autopay_delete_failed", error, {"seq": row.seq}
                    )
            except ProviderError as error:
                return _failed(actor, lease, "lease.autopay_delete_failed", error, {"seq": row.seq})
        for row in stale:
            ref = schedule_ref(row)
            before = _schedule_snapshot(row, ref)
            row.state = ScheduleState.ENDED
            row.save(update_fields=["state", "updated_at"])
            _audit(
                actor,
                "lease.autopay_schedule_deleted",
                lease,
                before,
                {"state": row.state, "schedule": mask_identifier(ref)},
            )
            live.pop(row.seq, None)

        for debit in missing:
            outcome = _create_one(
                provider,
                lease,
                live[debit.seq][0] if debit.seq in live else None,
                debit,
                customer=customer,
                payment_method=payment_method_external_id,
                actor=actor,
                now=moment,
            )
            if outcome is not None:
                return outcome
    return AutopayOutcome(AutopayAction.ENABLED, "Autopay enabled.")


def pause_autopay(lease_id: uuid.UUID, *, actor: User | None) -> AutopayOutcome:
    """D4: ``active=False`` on every active schedule of the lease; never an amount."""
    with transaction.atomic():
        lease = _locked(lease_id)
        rows = [
            (row, ref)
            for row in live_schedules(lease)
            if row.state == ScheduleState.ACTIVE and (ref := schedule_ref(row))
        ]
        if not rows:
            return AutopayOutcome(AutopayAction.ALREADY, NOTHING_TO_PAUSE)
        provider = payments.get_provider()
        for row, ref in rows:
            try:
                provider.update_scheduled_invoice(ref, ScheduleUpdate(active=False))
            except ProviderError as error:
                return _failed(actor, lease, "lease.autopay_pause_failed", error, {"seq": row.seq})
            before = _schedule_snapshot(row, ref)
            row.state = ScheduleState.PAUSED
            row.save(update_fields=["state", "updated_at"])
            _audit(actor, "lease.autopay_paused", lease, before, _schedule_snapshot(row, ref))
    return AutopayOutcome(AutopayAction.PAUSED, "Autopay paused.")


def resume_autopay(
    lease_id: uuid.UUID, *, actor: User | None, now: datetime | None = None
) -> AutopayOutcome:
    """D4: ``active=True`` and a rule with a new future ``DTSTART``; never an amount.

    Refused while the lease blocks autopay or any paused schedule no longer matches it.
    """
    moment = now or timezone.now()
    with transaction.atomic():
        lease = _locked(lease_id)
        rows = [
            (row, ref)
            for row in live_schedules(lease)
            if row.state == ScheduleState.PAUSED and (ref := schedule_ref(row))
        ]
        if not rows:
            return AutopayOutcome(AutopayAction.ALREADY, NOTHING_TO_RESUME)
        blockers = autopay_blockers(lease)
        if blockers:
            return AutopayOutcome(AutopayAction.REFUSED, blockers[0])
        planned = [
            (row, ref, schedule_rule(row.debit_day, moment))
            for row, ref in rows
            if row.debit_day is not None and not row.needs_review
        ]
        if len(planned) != len(rows):
            return AutopayOutcome(AutopayAction.REFUSED, RESUME_REFUSED)
        provider = payments.get_provider()
        for row, ref, rule in planned:
            try:
                provider.update_scheduled_invoice(ref, ScheduleUpdate(active=True, rule=rule))
            except ProviderError as error:
                return _failed(actor, lease, "lease.autopay_resume_failed", error, {"seq": row.seq})
            before = _schedule_snapshot(row, ref)
            row.state = ScheduleState.ACTIVE
            row.save(update_fields=["state", "updated_at"])
            _audit(
                actor,
                "lease.autopay_resumed",
                lease,
                before,
                {**_schedule_snapshot(row, ref), "rule": rule},
            )
    return AutopayOutcome(AutopayAction.RESUMED, "Autopay resumed.")


def stoppable_schedules(lease: Lease) -> list[tuple[CollectionSchedule, str]]:
    """The lease's rent and insurance schedules active or paused at the provider (a row
    with a provider schedule ID), with that ID, in kind and seq order: what "Stop
    autopay" deletes and what the end-of-lease wizard lists as blockers."""
    rows = lease.schedules.filter(
        kind__in=[ScheduleKind.RENT, ScheduleKind.INSURANCE],
        state__in=[ScheduleState.ACTIVE, ScheduleState.PAUSED],
    ).order_by("kind", "seq")
    return [(row, ref) for row in rows if (ref := schedule_ref(row))]


def stop_autopay(lease_id: uuid.UUID, *, actor: User | None) -> AutopayOutcome:
    """PLAN-012e D5 amendment: delete every rent and insurance schedule of the lease
    active or paused at the provider (a 404 counts as already gone, as
    :func:`enable_autopay` treats stale rows), one at a time; each deleted row is set
    ``ended`` and audited ``lease.autopay_stopped``. Nothing else on the lease changes.
    Any other provider failure stops at that row, audited, and leaves it as it was; the
    rows already deleted stay ended."""
    with transaction.atomic():
        lease = _locked(lease_id)
        rows = stoppable_schedules(lease)
        if not rows:
            return AutopayOutcome(AutopayAction.ALREADY, NOTHING_TO_STOP)
        provider = payments.get_provider()
        for row, ref in rows:
            try:
                provider.delete_scheduled_invoice(ref)
            except ProviderRejected as error:
                if error.status != HTTPStatus.NOT_FOUND:
                    return _failed(
                        actor,
                        lease,
                        "lease.autopay_stop_failed",
                        error,
                        {"kind": row.kind, "seq": row.seq},
                    )
            except ProviderError as error:
                return _failed(
                    actor,
                    lease,
                    "lease.autopay_stop_failed",
                    error,
                    {"kind": row.kind, "seq": row.seq},
                )
            before = {"kind": row.kind, **_schedule_snapshot(row, ref)}
            row.state = ScheduleState.ENDED
            row.save(update_fields=["state", "updated_at"])
            _audit(
                actor,
                "lease.autopay_stopped",
                lease,
                before,
                {"kind": row.kind, **_schedule_snapshot(row, ref)},
            )
    return AutopayOutcome(AutopayAction.STOPPED, AUTOPAY_STOPPED)


@dataclass(frozen=True, slots=True)
class AutopayMove:
    """A rent autopay moved past a split month (D13) whose first debit is still ahead."""

    #: The first debit of the split charge's new schedule.
    resumes: date
    #: The first debit of each new schedule row still ahead, by the row's id.
    starts: dict[str, datetime]

    @property
    def text(self) -> str:
        return AUTOPAY_RESUMES.format(date=day_text(self.resumes))


def _move_rows(lease: Lease, action: str, plan_id: uuid.UUID) -> dict[int, dict[str, Any]]:
    """The ``action`` audit rows of the lease for this plan, by seq."""
    rows = AuditLog.objects.filter(
        action=action,
        entity_type=ENTITY_LEASE,
        entity_id=str(lease.pk),
        after__plan=str(plan_id),
    ).values_list("after", flat=True)
    return {int(after["seq"]): after for after in rows if isinstance(after, dict)}


def move_autopay(
    lease_id: uuid.UUID,
    *,
    seq: int,
    skipped: date,
    plan_id: uuid.UUID,
    actor: User | None,
    now: datetime | None = None,
) -> AutopayOutcome:
    """PLAN-012b-b D13: move the lease's active rent autopay past the debit due on
    ``skipped`` (debit ``seq``), split by plan ``plan_id``; see the module docstring.

    Order: lease row lock; the lease's customer and linked bank method (refused, with
    no provider call, without them); the rent in effect on each new first debit; then
    each active schedule not yet deleted for this plan, deleted (a 404 counts as gone),
    ended and audited :data:`DELETED_FOR_MOVE_ACTION`, one at a time; then each debit
    deleted for this plan and not yet created again, created (a row left ``failed`` by
    an earlier run is first looked for at the provider) and audited
    :data:`MOVED_ACTION`. Any provider failure stops there, audited
    ``lease.autopay_move_failed``: the rows already deleted stay ended, the job fails and
    its repeat continues. A debit stopped, paused or enabled again by staff since its
    delete is left as it is. A lease with nothing to delete or create returns
    ``ALREADY`` and writes nothing."""
    moment = now or timezone.now()
    with transaction.atomic():
        lease = _locked(lease_id)
        operator = lease.operator
        if operator is None:
            return AutopayOutcome(AutopayAction.REFUSED, NO_OPERATOR)
        customer = customer_ref(lease)
        if not customer:
            return AutopayOutcome(AutopayAction.REFUSED, NO_PROVIDER_CUSTOMER)
        payment_method = linked_payment_method(lease)
        if not payment_method:
            return AutopayOutcome(AutopayAction.REFUSED, NO_BANK_METHOD)
        deleted = _move_rows(lease, DELETED_FOR_MOVE_ACTION, plan_id)
        moved = _move_rows(lease, MOVED_ACTION, plan_id)
        rows = {row.seq: row for row in live_schedules(lease)}
        old = [
            (row, ref)
            for row in rows.values()
            if row.state == ScheduleState.ACTIVE
            and row.seq not in deleted
            and (ref := schedule_ref(row))
        ]
        missing = sorted(
            {row.seq for row, _ in old}
            | {
                number
                for number in deleted
                if number not in moved
                and (number not in rows or rows[number].state == ScheduleState.FAILED)
            }
        )
        if not missing:
            return AutopayOutcome(AutopayAction.ALREADY, NOTHING_TO_MOVE)
        # The split debit starts after its own debit instant; any other keeps its next.
        start_from = {
            number: debit_instant(skipped) if number == seq else moment for number in missing
        }
        planned = {
            debit.seq: debit
            for number in missing
            for debit in debit_plan(lease, start_from[number])
            if debit.seq == number
        }
        if len(planned) != len(missing):
            first = min(start_from.values())
            return AutopayOutcome(
                AutopayAction.REFUSED, RENT_NOT_STARTED.format(date=day_text(first.date()))
            )

        provider = payments.get_provider()
        for row, ref in old:
            try:
                provider.delete_scheduled_invoice(ref)
            except ProviderRejected as error:
                if error.status != HTTPStatus.NOT_FOUND:
                    return _failed(
                        actor, lease, "lease.autopay_move_failed", error, {"seq": row.seq}
                    )
            except ProviderError as error:
                return _failed(actor, lease, "lease.autopay_move_failed", error, {"seq": row.seq})
            before = _schedule_snapshot(row, ref)
            row.state = ScheduleState.ENDED
            row.save(update_fields=["state", "updated_at"])
            _audit(
                actor,
                DELETED_FOR_MOVE_ACTION,
                lease,
                before,
                {**_schedule_snapshot(row, ref), "plan": str(plan_id)},
            )
            rows.pop(row.seq)
        for number in missing:
            current = rows.get(number)
            debit = planned[number]
            outcome = _create_one(
                provider,
                lease,
                current,
                debit,
                customer=customer,
                payment_method=payment_method,
                actor=actor,
                now=start_from[number],
            )
            if outcome is not None:
                return outcome
            [created] = [
                row
                for row in live_schedules(lease)
                if row.seq == number and row.state == ScheduleState.ACTIVE
            ]
            _audit(
                actor,
                MOVED_ACTION,
                lease,
                None,
                {
                    "plan": str(plan_id),
                    "seq": number,
                    "skipped": skipped.isoformat() if number == seq else None,
                    "starts": next_debit_at(debit.debit_day, start_from[number]).isoformat(),
                    "row": str(created.pk),
                    "schedule": mask_identifier(schedule_ref(created)),
                },
            )
    resumes = next_debit_at(planned[seq].debit_day, start_from[seq]) if seq in planned else None
    when = day_text(resumes.date()) if resumes is not None else "-"
    return AutopayOutcome(AutopayAction.MOVED, AUTOPAY_MOVED.format(date=when))


def autopay_move(lease: Lease, now: datetime | None = None) -> AutopayMove | None:
    """The lease's rent autopay moved past a split month (D13) while the plan is live
    and the split charge's new first debit is ahead, or ``None`` (local data only)."""
    moment = now or timezone.now()
    live = {
        str(pk)
        for pk in InstallmentPlan.objects.filter(
            lease=lease, status__in=LIVE_PLAN_STATUSES
        ).values_list("pk", flat=True)
    }
    if not live:
        return None
    rows = AuditLog.objects.filter(
        action=MOVED_ACTION, entity_type=ENTITY_LEASE, entity_id=str(lease.pk)
    ).order_by("id")
    resumes: date | None = None
    starts: dict[str, datetime] = {}
    for after in rows.values_list("after", flat=True):
        if not isinstance(after, dict) or after.get("plan") not in live:
            continue
        first = datetime.fromisoformat(str(after["starts"]))
        if first <= moment:
            continue
        starts[str(after["row"])] = first
        if after.get("skipped"):
            resumes = first.date()
    if resumes is None:
        return None
    return AutopayMove(resumes=resumes, starts=starts)


# --- Helpers --------------------------------------------------------------------------


def _create_one(
    provider: PaymentProvider,
    lease: Lease,
    row: CollectionSchedule | None,
    debit: PlannedDebit,
    *,
    customer: str,
    payment_method: str,
    actor: User | None,
    now: datetime,
) -> AutopayOutcome | None:
    """Create (or adopt) the schedule of one debit; ``None`` on success."""
    if row is None:
        row = CollectionSchedule(lease=lease, kind=ScheduleKind.RENT, seq=debit.seq)
    retrying = row.state == ScheduleState.FAILED
    row.debit_day = debit.debit_day
    row.amount = debit.amount
    # A rent change after the first debit makes this schedule wrong on that date.
    row.needs_review = debit.changes_on is not None
    if retrying:
        try:
            adopted = _adoptable(provider, customer, debit)
        except ProviderError as error:
            return _failed(actor, lease, "lease.autopay_failed", error, {"seq": debit.seq})
        if len(adopted) > 1:
            row.needs_review = True
            row.save()
            return AutopayOutcome(AutopayAction.REFUSED, SEVERAL_MATCHES)
        if adopted:
            _link(row, adopted[0])
            _audit(
                actor,
                "lease.autopay_adopted",
                lease,
                None,
                _schedule_snapshot(row, adopted[0].external_id),
            )
            return None
    rule = schedule_rule(debit.debit_day, now)
    line = LineItem(f"Rent, store {lease.store.external_label}", Decimal(1), debit.amount)
    schedule = ScheduleInput(
        customer_external_id=customer,
        payment_method_external_id=payment_method,
        amounts=Amounts(debit.amount, debit.amount, Decimal("0.00"), (line,)),
        rule=rule,
        email_notification=bool(lease_sync.billing_of(account_lease(lease) or lease).email),
    )
    try:
        record = provider.create_scheduled_invoice(schedule)
    except ProviderError as error:
        row.state = ScheduleState.FAILED
        row.save()
        return _failed(actor, lease, "lease.autopay_failed", error, {"seq": debit.seq})
    _link(row, record)
    _audit(
        actor,
        "lease.autopay_enabled",
        lease,
        None,
        {
            **_schedule_snapshot(row, record.external_id),
            "rule": rule,
            "payment_method": mask_identifier(payment_method),
            "email_notification": schedule.email_notification,
        },
    )
    return None


def _adoptable(
    provider: PaymentProvider, customer: str, debit: PlannedDebit
) -> list[ScheduleRecord]:
    """Unlinked provider schedules of the customer with this amount and debit day."""
    linked = set(
        ExternalRef.objects.filter(provider=PROVIDER_KEY, external_kind=KIND_SCHEDULE).values_list(
            "external_id", flat=True
        )
    )
    found = provider.list_scheduled_invoices(ScheduleQuery(customer_external_id=customer))
    return [
        record
        for record in found
        if record.external_id not in linked
        and record.total == debit.amount
        and _rule_day(record.rule) == debit.debit_day
    ]


def _rule_day(rule: str) -> int | None:
    match = _RULE_DAY.match(rule)
    return int(match.group(3)) if match else None


def _link(row: CollectionSchedule, record: ScheduleRecord) -> None:
    row.state = ScheduleState.ACTIVE
    row.save()
    ExternalRef.objects.create(
        entity_type=ENTITY_SCHEDULE,
        entity_id=row.pk,
        provider=PROVIDER_KEY,
        external_kind=KIND_SCHEDULE,
        external_id=record.external_id,
    )


def _method_refusal(method: PaymentMethodRecord | None) -> str:
    if method is None:
        return METHOD_NOT_FOUND
    if method.raw_status:
        return METHOD_DELETED
    if not method.is_usable_bank:
        return CARD_REFUSED
    return ""


def _flag_stale_schedules(lease: Lease, rent: Decimal, *, operator_changed: bool) -> None:
    """D5: flag every linked schedule whose day or amount no longer matches the lease."""
    planned = {debit.seq: debit for debit in planned_debits(lease, rent)}
    for row in live_schedules(lease):
        if row.needs_review or not schedule_ref(row):
            continue
        debit = planned.get(row.seq)
        if (
            operator_changed
            or debit is None
            or debit.debit_day != row.debit_day
            or debit.amount != row.amount
        ):
            row.needs_review = True
            row.save(update_fields=["needs_review", "updated_at"])


def _failure_message(error: ProviderError) -> str:
    return PROVIDER_REJECTED if isinstance(error, ProviderRejected) else PROVIDER_FAILED


def _fields(error: ProviderError) -> tuple[str, ...]:
    return error.error_fields if isinstance(error, ProviderRejected) else ()


def _failed(
    actor: User | None,
    lease: Lease,
    action: str,
    error: ProviderError,
    context: dict[str, Any],
) -> AutopayOutcome:
    """Audit a provider failure (class name and field names only) and report it (D12)."""
    failure = lease_sync.failure_name(error)
    fields = _fields(error)
    _audit(actor, action, lease, None, {**context, "failure": failure, "fields": list(fields)})
    return AutopayOutcome(AutopayAction.FAILED, _failure_message(error), fields, failure)


def _locked(lease_id: uuid.UUID) -> Lease:
    """The lease, row-locked. ``of=("self",)``: PostgreSQL refuses ``FOR UPDATE`` on the
    nullable side of the outer join to ``operator``; only the lease row is locked."""
    return (
        Lease.objects.select_for_update(of=("self",))
        .select_related("store", "operator")
        .get(pk=lease_id)
    )


def _by_creation_key(creation_key: uuid.UUID) -> Lease | None:
    entry = AuditLog.objects.filter(
        action="lease.created",
        entity_type=ENTITY_LEASE,
        after__creation_key=str(creation_key),
    ).first()
    if entry is None:
        return None
    return Lease.objects.filter(pk=entry.entity_id).first()


def _editable(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {name: fields[name] for name in LEASE_FIELDS if name in fields}


def _snapshot(lease: Lease) -> dict[str, Any]:
    open_row = open_rent(lease)
    return {
        "store": str(lease.store_id),
        "operator": str(lease.operator_id) if lease.operator_id else None,
        "debit_day": lease.debit_day,
        "second_debit_day": lease.second_debit_day,
        "payment_channel": lease.payment_channel,
        "started_on": lease.started_on.isoformat() if lease.started_on else None,
        "collection_status": lease.collection_status,
        "prorate_first_month": lease.prorate_first_month,
        "rent": str(open_row.amount) if open_row is not None else None,
        "rent_from": open_row.effective_from.isoformat() if open_row is not None else None,
    }


def _schedule_snapshot(row: CollectionSchedule, ref: str) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "debit_day": row.debit_day,
        "amount": str(row.amount),
        "state": row.state,
        "schedule": mask_identifier(ref) if ref else "",
    }


def _audit(
    actor: User | None,
    action: str,
    lease: Lease,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_LEASE,
        entity_id=str(lease.pk),
        before=before,
        after=after,
    )
