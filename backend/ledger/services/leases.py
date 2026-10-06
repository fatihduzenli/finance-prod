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
  the operator's payment methods from the latest ``provider_snapshots`` row of its
  customer reference, written by :func:`refresh_payment_methods` in the worker; the
  page never calls the provider. :func:`enable_autopay` still reads the list from the
  provider before it creates anything.
- **Bank only.** The chosen payment method must be in the provider's list for the
  operator's customer, be a ``bank`` method and not be deleted; a card is refused even
  when its ID is posted directly (D2).
- **Large debits** (PLAN-007c §1), compared per debit: above ``PROVIDER_MAX_TXN_AMOUNT``
  (when set) enrolment is refused whatever is confirmed; else above
  ``LARGE_DEBIT_WARNING_AMOUNT`` it needs ``confirmed=True``, recorded in a
  ``lease.autopay_large_debit_confirmed`` audit row before any provider write.
- **Never an amount on an update** (D4): pause sends ``active=False``; resume sends
  ``active=True`` and a rule with a new future ``DTSTART``. A schedule that no longer
  matches the lease is flagged ``needs_review`` and cannot be resumed; staff pause it
  and enable autopay again, which ends the old row and creates a new schedule.
- **Ending a lease** (PLAN-010b Batch C): :func:`end_lease` sets the end date, the
  reason and the ``ended`` status; it is refused while autopay is on (pause it first).

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
from django.utils import timezone

import payments
from ledger.models import (
    AuditLog,
    CollectionSchedule,
    CollectionStatus,
    Lease,
    LeaseRentHistory,
    Operator,
    PaymentChannel,
    RentSource,
    ScheduleKind,
    ScheduleState,
    Store,
)
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from payments import operator_sync
from payments.errors import ProviderError, ProviderRejected
from payments.models import ExternalRef, ProviderSnapshot
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
ENTITY_SCHEDULE = "collection_schedule"
KIND_SCHEDULE = "schedule"
PROVIDER_KEY = operator_sync.PROVIDER_KEY

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
)
_RULE_DAY = re.compile(r"\ADTSTART=(\d{4})(\d{2})(\d{2})T")

# --- Fixed sentences ------------------------------------------------------------------

NO_OPERATOR = "The lease has no operator."
OPERATOR_ARCHIVED = "The operator is archived."
NOT_ACTIVE = "Only an active lease can be enrolled in autopay."
NOT_PROVIDER_CHANNEL = "The payment channel is not ACH debit through the payment provider."
NO_DEBIT_DAY = "The lease has no debit day."
DAY_NOT_SCHEDULABLE = (
    "Debit days 29 to 31 cannot be scheduled yet (TODO-VERIFY-036). Choose a day from 1 to 28."
)
NO_RENT = "The lease has no rent."
NO_PROVIDER_CUSTOMER = "Operator has no payment provider customer."
NO_BANK_METHOD = "No bank method linked."
METHODS_NOT_READ = "The operator's payment methods have not been read yet."
METHODS_READ = "Payment methods read: {count}."
NO_EMAIL = "No email: operator will not receive receipts."
SCHEDULE_DIFFERS = "Schedule amount differs from rent; pause and re-enable autopay."
RENT_CHANGES = "Rent changes on {date}; pause and re-enable autopay on or after that date."
RENT_NOT_STARTED = "No rent is in effect on the first debit date ({date}). Nothing was created."
PAUSE_FIRST = "A schedule no longer matches the lease. Pause autopay first, then enable it again."
RESUME_REFUSED = (
    "A schedule no longer matches the lease, so it cannot be resumed. Enable autopay again."
)
ALREADY_ENABLED = "Autopay is already set up for every debit day. Nothing was created."
NOTHING_TO_PAUSE = "No active schedule to pause."
NOTHING_TO_RESUME = "No paused schedule to resume."
CHOOSE_METHOD = "Choose a bank payment method."
METHOD_NOT_FOUND = (
    "That payment method is not one of this operator's methods at the payment provider. "
    "Nothing was created."
)
CARD_REFUSED = "Card payment methods are not allowed. Choose a bank method. Nothing was created."
METHOD_DELETED = "That payment method is deleted at the payment provider. Nothing was created."
SEVERAL_MATCHES = (
    "Several schedules at the payment provider match a debit whose last create failed. "
    "Nothing was created; review them in the provider's dashboard."
)
PROVIDER_REJECTED = "The payment provider refused the request."
PROVIDER_FAILED = "The payment provider could not be reached or did not answer. Try again later."
#: PLAN-007c §1. Amounts are formatted by :func:`format_amount`.
LIMIT_EXCEEDED = "Exceeds the confirmed provider limit of {limit}. Autopay cannot be enabled."
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
            AutopayAction.ALREADY,
        )

    @property
    def text(self) -> str:
        """The message, with the rejected field names when there are any."""
        if self.fields:
            return f"{self.message} Fields the provider rejected: {', '.join(self.fields)}."
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


def format_amount(amount: Decimal) -> str:
    """``$1,234.56``."""
    return f"${amount:,.2f}"


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
            return RENT_CHANGES.format(date=later.effective_from.isoformat())
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


def customer_ref(operator: Operator | None) -> str:
    if operator is None:
        return ""
    return operator_sync.current_refs(operator).get(operator_sync.KIND_CUSTOMER, "")


def linked_payment_method(operator: Operator | None) -> str:
    """The payment-method ID staff linked to the operator, or ``""`` (unmasked)."""
    if operator is None:
        return ""
    return operator_sync.current_refs(operator).get(operator_sync.KIND_PAYMENT_METHOD, "")


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
    if operator is not None and not customer_ref(operator):
        blockers.append(NO_PROVIDER_CUSTOMER)
    return blockers


def enrolment_notes(lease: Lease) -> list[str]:
    """Information that does not block enrolment."""
    operator = lease.operator
    notes: list[str] = []
    if operator is None:
        return notes
    if not linked_payment_method(operator):
        notes.append(NO_BANK_METHOD)
    if not operator.email:
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


def _customer_ref_row(operator: Operator | None) -> ExternalRef | None:
    if operator is None:
        return None
    return ExternalRef.objects.filter(
        entity_type=operator_sync.ENTITY_OPERATOR,
        entity_id=operator.pk,
        provider=PROVIDER_KEY,
        external_kind=operator_sync.KIND_CUSTOMER,
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


def payment_method_choices(lease: Lease) -> MethodChoices:
    """The operator's methods from the latest snapshot (no provider call), or why there
    are none. ``fetched_at`` is ``None`` when the methods were never read."""
    return operator_method_choices(lease.operator)


def operator_method_choices(operator: Operator | None) -> MethodChoices:
    """:func:`payment_method_choices` for an operator (no provider call)."""
    ref = _customer_ref_row(operator)
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


def linked_method_record(operator: Operator | None) -> PaymentMethodRecord | None:
    """The operator's linked payment method as the latest stored snapshot shows it, or
    ``None`` when nothing is linked or the snapshot does not list it (no provider call)."""
    linked = linked_payment_method(operator)
    if not linked:
        return None
    return next(
        (
            method
            for method in operator_method_choices(operator).methods
            if method.external_id == linked
        ),
        None,
    )


def refresh_payment_methods(operator_id: uuid.UUID) -> MethodsRefresh:
    """Read the operator's payment methods from the provider (one list call) and keep
    them as the one method-list snapshot of its customer reference (worker only).

    Nothing is written before the call; a failure keeps the previous snapshot. The new
    snapshot replaces the previous ones in one transaction.
    """
    operator = Operator.objects.filter(pk=operator_id).first()
    ref = _customer_ref_row(operator)
    if ref is None:
        return MethodsRefresh(False, NO_PROVIDER_CUSTOMER)
    try:
        methods = payments.get_provider().list_payment_methods(ref.external_id)
    except ProviderError as error:
        return MethodsRefresh(
            False, _failure_message(error), _fields(error), operator_sync.failure_name(error)
        )
    fetched_at = max((method.fetched_at for method in methods), default=timezone.now())
    with transaction.atomic():
        old = [snapshot.pk for snapshot in _method_snapshots(ref)]
        ProviderSnapshot.objects.create(
            external_ref=ref,
            payload=[_method_payload(method) for method in methods],
            fetched_at=fetched_at,
        )
        ProviderSnapshot.objects.filter(pk__in=old).delete()
    return MethodsRefresh(True, METHODS_READ.format(count=len(methods)))


# --- Lease writes ---------------------------------------------------------------------


def create_lease(
    fields: Mapping[str, Any],
    *,
    rent: Decimal,
    creation_key: uuid.UUID,
    actor: User | None,
    today: date | None = None,
) -> tuple[Lease, bool]:
    """Save a new lease and its first rent row, or return the lease this key made.

    The key is recorded in the ``lease.created`` audit row; the store row is locked
    while it is looked up, so two submissions of one form make one lease.
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
                        RENT_DATE_TOO_EARLY.format(start=open_row.effective_from.isoformat()),
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
    return lease


def end_lease(
    lease_id: uuid.UUID,
    ended_on: date | None,
    end_reason: str,
    *,
    actor: User | None,
    today: date | None = None,
) -> Lease:
    """Set the lease's end date and reason and its status to ``ended`` (PLAN-010b Batch C).

    Refused, with one sentence and nothing written, when the lease has already ended,
    the date is missing, before the start date or in the future, the reason is empty
    or holds a card or account number, or a rent schedule is active at the payment
    provider (autopay must be paused first; nothing here calls the provider). Checked
    under the lease's row lock and audited as ``lease.ended``.
    """
    reason = visible_text(end_reason or "")
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
                "ended_on", END_BEFORE_START.format(start=lease.started_on.isoformat())
            )
        if not reason:
            raise LeaseRefused("end_reason", END_REASON_REQUIRED)
        try:
            validate_no_payment_data(reason)
        except ValidationError:
            raise LeaseRefused("end_reason", PAYMENT_DATA_MESSAGE) from None
        if autopay_on(lease):
            raise LeaseRefused(None, END_AUTOPAY_ON)
        before = {**_snapshot(lease), **_end_snapshot(lease)}
        lease.ended_on = ended_on
        lease.end_reason = reason
        lease.collection_status = CollectionStatus.ENDED
        lease.save(update_fields=["ended_on", "end_reason", "collection_status", "updated_at"])
        _audit(actor, "lease.ended", lease, before, {**_snapshot(lease), **_end_snapshot(lease)})
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
    operator's customer (the chosen ID must be a bank method in it); a delete of every
    stale (flagged, paused) schedule, a 404 counting as already gone, where any other
    failure ends nothing and creates nothing; then one create per missing debit. A
    create failure stops at that debit, marks its row ``failed`` and changes nothing on
    the lease; schedules already created stay linked.
    A confirmed large debit is audited after the method check, before any provider write.
    """
    if not operator_sync.IDENTIFIER.match(payment_method_external_id or ""):
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
                AutopayAction.REFUSED, RENT_NOT_STARTED.format(date=first.date().isoformat())
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
        customer = customer_ref(operator)
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
                {"payment_method": mask_identifier(payment_method_external_id), "reason": refusal},
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
                operator,
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


# --- Helpers --------------------------------------------------------------------------


def _create_one(
    provider: PaymentProvider,
    lease: Lease,
    operator: Operator,
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
        email_notification=bool(operator.email),
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
    failure = operator_sync.failure_name(error)
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
