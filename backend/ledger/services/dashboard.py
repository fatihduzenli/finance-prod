"""The dashboard's derived blocks (docs/qa/PLAN-012f-approved-design.md D7; DESIGN-SPEC §4).

Read only: nothing here writes or calls the payment provider. Each block is one query, or
none when it reads what the page already loaded:

- :func:`pending_debits` (one query): the bank debits (``ach``, not a refund) still
  ``pending``, their count and amount, and the day they settle by: the latest one's
  initiation day plus :data:`ACH_SETTLE_BUSINESS_DAYS` business days (the documented ACH
  window, docs/CLAUDE.md §10: settled or returned within about five business days; Saturdays
  and Sundays are skipped, bank holidays are not known here);
- :func:`collection_progress` (one query): the month's rent charges, waived ones left out,
  by status: what was billed (``amount_due``), what was collected on them (``amount_paid``,
  at most each charge's amount, so the share never passes 100 %), the share as a whole
  per cent rounded down (``None`` when nothing is billed) and the progress bar's segments
  (Paid, Pending, Partly paid, Unpaid: the amounts billed of the charges in each status);
- :func:`coming_up` (no query: the leases the attention list already read, and
  :func:`pending_debits`): up to three items in date order: the next rent debit day after
  today with the number of stores debited that day (leases in collection, a known channel,
  started by then and not ended; days 29 to 31 fall on a short month's last day, as the rent
  run counts them), the ACH settlement of the pending debits, and the next insurance
  renewals (the stores' open-date anniversaries from today, held by a lease that day and not
  closed by then) with their count;
- :func:`recent_payments` (one query): the last :data:`RECENT_LIMIT` settled payments (not a
  refund, not a credit applied: no money came in), newest first, each with when it settled
  in words ("Today 14:02", "Yesterday", else the date; the time is the row's last change in
  the project's time zone, shown only when that was the settlement day), the store (its
  title, "#1991 Istanbul", :func:`ledger.services.stores.title`), what it
  paid for, the method and the amount.

Money is ``Decimal``; dates are the project's ``TIME_ZONE`` days.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal

from django.db.models import Count, F, Max, Sum
from django.db.models.functions import Least
from django.utils import timezone

from finance.dates import day_text, month_text
from ledger.models import (
    ZERO,
    CollectionStatus,
    Lease,
    Payment,
    PaymentChannel,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    StoreStatus,
)
from ledger.services import insurance, rent_run, stores
from ledger.wording import format_amount

#: Business days an ACH debit takes to settle (docs/CLAUDE.md §10, the provider's ACH page).
ACH_SETTLE_BUSINESS_DAYS = 5
#: Payments the Recent payments card lists.
RECENT_LIMIT = 5
#: Items the Coming up card lists.
COMING_UP_LIMIT = 3
#: How far ahead the next rent debit day is looked for (a month and a day).
RENT_LOOKAHEAD_DAYS = 32

TODAY_WORD = "Today"
YESTERDAY_WORD = "Yesterday"
ACH_SETTLEMENT = "ACH settlement"
RENT_DAY = "Rent day"
INSURANCE_RENEWALS = "Insurance renewals"

#: The progress bar's families (finance.templatetags.components) of each rent status, in bar
#: order: Paid, Pending, Partly paid, Unpaid (due and unpaid both).
SEGMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("paid", (RentChargeStatus.PAID,)),
    ("pending", (RentChargeStatus.PENDING,)),
    ("partial", (RentChargeStatus.PARTIAL,)),
    ("unpaid", (RentChargeStatus.DUE, RentChargeStatus.UNPAID)),
)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def add_business_days(day: date, count: int) -> date:
    """``day`` plus ``count`` business days (Monday to Friday)."""
    found = day
    left = count
    while left > 0:
        found += timedelta(days=1)
        if found.weekday() < 5:
            left -= 1
    return found


# --- Pending bank debits ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingDebits:
    count: int
    amount: Decimal
    #: The latest pending debit's initiation day plus the ACH window; ``None`` without one.
    settle_by: date | None

    @property
    def note(self) -> str:
        """The KPI's third line: "19 debits · settle by Oct 24, 2026"."""
        text = _plural(self.count, "debit")
        return f"{text} · settle by {day_text(self.settle_by)}" if self.settle_by else text


def pending_debits() -> PendingDebits:
    """The pending ACH debits (one query)."""
    found = Payment.objects.filter(
        status=PaymentStatus.PENDING, method=PaymentMethod.ACH, is_refund=False
    ).aggregate(count=Count("id"), amount=Sum("amount"), latest=Max("initiated_on"))
    latest = found["latest"]
    return PendingDebits(
        count=found["count"] or 0,
        amount=Decimal(str(found["amount"])) if found["amount"] is not None else ZERO,
        settle_by=(
            add_business_days(latest, ACH_SETTLE_BUSINESS_DAYS) if latest is not None else None
        ),
    )


# --- The month's collection ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Collection:
    month: date
    billed: Decimal
    collected: Decimal
    #: The whole per cent collected of billed, rounded down; ``None`` when nothing is billed.
    percent: int | None
    #: ``(family, amount billed)`` per segment, in bar order (the progress tag's input).
    segments: list[tuple[str, Decimal]]


def collection_progress(month: date) -> Collection:
    """The month's rent charges by status (one query; see the module docstring)."""
    rows = (
        RentCharge.objects.filter(period_month=month.replace(day=1))
        .exclude(status=RentChargeStatus.WAIVED)
        .values("status")
        .annotate(billed=Sum("amount_due"), collected=Sum(Least("amount_paid", "amount_due")))
        .order_by("status")
    )
    billed_by: dict[str, Decimal] = {}
    collected = ZERO
    for row in rows:
        billed_by[row["status"]] = Decimal(str(row["billed"] or 0))
        collected += max(Decimal(str(row["collected"] or 0)), ZERO)
    billed = sum(billed_by.values(), ZERO)
    percent = (
        int((collected * 100 / billed).to_integral_value(rounding=ROUND_FLOOR))
        if billed > 0
        else None
    )
    segments = [
        (family, sum((billed_by.get(status, ZERO) for status in statuses), ZERO))
        for family, statuses in SEGMENTS
    ]
    return Collection(
        month=month.replace(day=1),
        billed=billed,
        collected=collected,
        percent=min(percent, 100) if percent is not None else None,
        segments=segments,
    )


# --- Coming up -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComingItem:
    on: date
    title: str
    note: str


def _debits_on(lease: Lease, day: date) -> bool:
    """Whether a debit day of ``lease`` falls on ``day`` (29 to 31: a short month's last)."""
    for debit_day in (lease.debit_day, lease.second_debit_day):
        if debit_day is not None and rent_run.falls_on(debit_day, day.year, day.month) == day.day:
            return True
    return False


def _in_collection(lease: Lease, day: date) -> bool:
    return (
        lease.collection_status == CollectionStatus.ACTIVE
        and lease.payment_channel != PaymentChannel.UNKNOWN
        and (lease.started_on is None or lease.started_on <= day)
        and (lease.ended_on is None or lease.ended_on >= day)
    )


def next_rent_day(leases: Iterable[Lease], today: date) -> ComingItem | None:
    """The next day after ``today`` a lease in collection is debited, with its store count."""
    debited = [
        lease
        for lease in leases
        if lease.collection_status == CollectionStatus.ACTIVE
        and (lease.debit_day is not None or lease.second_debit_day is not None)
    ]
    for offset in range(1, RENT_LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        due = {
            lease.store_id
            for lease in debited
            if _in_collection(lease, day) and _debits_on(lease, day)
        }
        if due:
            return ComingItem(day, RENT_DAY, _plural(len(due), "store"))
    return None


def _anniversary_from(open_date: date, today: date) -> date:
    due = insurance.anniversary(open_date, today.year)
    return due if due >= today else insurance.anniversary(open_date, today.year + 1)


def next_renewals(leases: Iterable[Lease], today: date) -> ComingItem | None:
    """The next day stores renew their insurance (their open-date anniversary, from today),
    held by a lease that day, the store active and not closed by then; with the count."""
    days: dict[object, date] = {}
    for lease in leases:
        store = lease.store
        if store.open_date is None or store.status != StoreStatus.ACTIVE:
            continue
        due = _anniversary_from(store.open_date, today)
        if store.closed_at is not None and due >= store.closed_at:
            continue
        if insurance.holds_on(lease, due):
            days[store.pk] = due
    if not days:
        return None
    first = min(days.values())
    count = sum(1 for due in days.values() if due == first)
    return ComingItem(first, INSURANCE_RENEWALS, _plural(count, "store"))


def coming_up(leases: Iterable[Lease], today: date, debits: PendingDebits) -> list[ComingItem]:
    """Up to :data:`COMING_UP_LIMIT` items in date order (see the module docstring)."""
    rows = list(leases)
    found: list[ComingItem] = []
    if debits.count and debits.settle_by is not None:
        found.append(
            ComingItem(
                debits.settle_by,
                ACH_SETTLEMENT,
                f"{_plural(debits.count, 'debit')} · {format_amount(debits.amount)}",
            )
        )
    for item in (next_rent_day(rows, today), next_renewals(rows, today)):
        if item is not None:
            found.append(item)
    found.sort(key=lambda item: (item.on, item.title))
    return found[:COMING_UP_LIMIT]


# --- Recent payments -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecentPayment:
    payment: Payment
    when: str
    store: str
    paid_for: str
    method: str
    amount: Decimal


def _when(payment: Payment, today: date) -> str:
    settled = payment.settled_on
    if settled is None:
        return "—"
    if settled == today:
        changed = timezone.localtime(payment.updated_at) if payment.updated_at else None
        if changed is not None and changed.date() == today:
            return f"{TODAY_WORD} {changed:%H:%M}"
        return TODAY_WORD
    if settled == today - timedelta(days=1):
        return YESTERDAY_WORD
    return day_text(settled)


def _paid_for(payment: Payment) -> tuple[str, str]:
    """(store label, what the payment was for)."""
    if payment.rent_charge is not None:
        charge = payment.rent_charge
        return stores.title(charge.lease.store), f"Rent {month_text(charge.period_month)}"
    if payment.insurance_charge is not None:
        held = payment.insurance_charge
        year = payment.policy_year or held.policy_year
        return stores.title(held.lease.store), f"Insurance {year}"
    if payment.one_off_charge is not None:
        one_off = payment.one_off_charge
        store = stores.title(one_off.lease.store) if one_off.lease is not None else ""
        return store, one_off.label
    return "", ""


def recent_payments(today: date, limit: int = RECENT_LIMIT) -> list[RecentPayment]:
    """The last ``limit`` settled payments, newest first (one query)."""
    rows = (
        Payment.objects.filter(status=PaymentStatus.SETTLED, is_refund=False)
        .exclude(method=PaymentMethod.CREDIT)
        .exclude(settled_on__isnull=True)
        .select_related(
            "rent_charge__lease__store",
            "insurance_charge__lease__store",
            "one_off_charge__lease__store",
            "one_off_charge__fee_type",
        )
        .order_by(F("settled_on").desc(), "-updated_at", "-id")[:limit]
    )
    found = []
    for payment in rows:
        store, paid_for = _paid_for(payment)
        found.append(
            RecentPayment(
                payment=payment,
                when=_when(payment, today),
                store=store,
                paid_for=paid_for,
                method=payment.get_method_display(),
                amount=Decimal(payment.amount),
            )
        )
    return found
