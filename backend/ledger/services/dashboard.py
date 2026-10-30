"""The dashboard's derived blocks (docs/qa/PLAN-012f-approved-design.md D7; DESIGN-SPEC §4).

Read only: nothing here writes or calls the payment provider. Each block is one query, or
none when it reads what the page already loaded:

- :func:`pending_debits` (one query): the bank debits (``ach``, not a refund) still
  ``pending``, their count and amount, and the day they settle by: the latest one's
  initiation day plus :data:`ACH_SETTLE_BUSINESS_DAYS` business days (the documented ACH
  window, docs/CLAUDE.md §10: settled or returned within about five business days; Saturdays
  and Sundays are skipped, bank holidays are not known here);
- :func:`collection_progress` (one query; the "Collected this month" figure's share line): the
  month's rent charges, waived ones left out,
  by status: what was billed (``amount_due``), what was collected on them (``amount_paid``,
  at most each charge's amount, so the share never passes 100 %), the share as a whole
  per cent rounded down (``None`` when nothing is billed);
- :func:`recent_payments` (one query): the last :data:`RECENT_LIMIT` settled payments (not a
  refund, not a credit applied: no money came in), newest first, each with when it settled
  in words ("Today 14:02", "Yesterday", else the date; the time is the row's last change in
  the project's time zone, shown only when that was the settlement day), the store (its
  title, "#1991 Istanbul", :func:`ledger.services.stores.title`), what it
  paid for, the method and the amount.

The cards of the owner's staging review (docs/qa/PLAN-012f-b-staging-review.md D3 to D6); the
Cashflow card reads a range of whole months (:func:`parse_range`: this month, the last 3, 6 or
12 months with the current one, or a custom From to To of at most :data:`MAX_RANGE_MONTHS`
months; the last 12 by default, PLAN-012g C2):

- :func:`cashflow` (the P&L's rows, :func:`ledger.services.pnl.rows`): per month the
  collected income, the known costs and the net, totalled over the range; a store whose
  costs are not entered is left out of the expenses and counted, never read as zero;
  :func:`cashflows` builds several ranges' cards from one read of the P&L's rows (the
  dashboard's laptop and phone defaults, PLAN-012g C2);
- :func:`payment_status` (two queries): the rent charges and the one-off charges due in its
  own period (PLAN-014 A3, :func:`period_for`: a calendar week, Sunday to Saturday, or a
  calendar month; not the Cashflow range) as Overdue, Paid and Upcoming by the grid's status
  rule, the pending ones counted apart (D20: an installment, an autopay-plan occurrence and a
  re-debit count through their parent or source, never on their own);
- :func:`mini_calendar` (the Calendar page's three queries): a month's days and items.

Money is ``Decimal``; dates are the project's ``TIME_ZONE`` days.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal

from django.db.models import Count, Exists, F, Max, OuterRef, Sum
from django.db.models.functions import Least
from django.utils import timezone

from finance.dates import day_short_text, day_text, month_long_text, month_text
from ledger.models import (
    ZERO,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import calendar, grid, pnl, stores

#: Business days an ACH debit takes to settle (docs/CLAUDE.md §10, the provider's ACH page).
ACH_SETTLE_BUSINESS_DAYS = 5
#: Payments the Recent payments card lists.
RECENT_LIMIT = 5

TODAY_WORD = "Today"
YESTERDAY_WORD = "Yesterday"


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
    return Collection(
        month=month.replace(day=1),
        billed=billed,
        collected=collected,
        percent=min(percent, 100) if percent is not None else None,
    )


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


# --- The cards' date range (PLAN-012f-b Batch A) ---------------------------------------------

RANGE_THIS = "this"
RANGE_3M = "3m"
RANGE_6M = "6m"
RANGE_12M = "12m"
RANGE_CUSTOM = "custom"
#: The range select's choices, in its order.
RANGE_CHOICES: tuple[tuple[str, str], ...] = (
    (RANGE_THIS, "This month"),
    (RANGE_3M, "Last 3 months"),
    (RANGE_6M, "Last 6 months"),
    (RANGE_12M, "Last 12 months"),
    (RANGE_CUSTOM, "Custom"),
)
#: The months each preset range covers, the current month last.
_PRESET_MONTHS: dict[str, int] = {RANGE_THIS: 1, RANGE_3M: 3, RANGE_6M: 6, RANGE_12M: 12}
#: The range when ``?range=`` is absent (the laptop's, DESIGN-SPEC §7.5) and the phone's own
#: default, which the dashboard computes beside it (PLAN-012g C2).
RANGE_DEFAULT = RANGE_12M
RANGE_DEFAULT_NARROW = RANGE_6M
#: The longest custom range, in months.
MAX_RANGE_MONTHS = 36
RANGE_MESSAGE = "Choose a range of whole months, the earlier month first, at most three years."


@dataclass(frozen=True, slots=True)
class Range:
    """The months the Cashflow card covers, both included."""

    key: str
    label: str
    from_month: date
    to_month: date

    @property
    def months(self) -> list[date]:
        count = (self.to_month.year - self.from_month.year) * 12
        count += self.to_month.month - self.from_month.month + 1
        return [pnl.add_months(self.from_month, n) for n in range(count)]

    @property
    def end(self) -> date:
        """The range's last day."""
        return pnl.month_end(self.to_month)

    def as_query(self) -> dict[str, str]:
        """The range as the card's GET form writes it (``YYYY-MM`` months)."""
        values = {"range": self.key}
        if self.key == RANGE_CUSTOM:
            values["from"] = f"{self.from_month:%Y-%m}"
            values["to"] = f"{self.to_month:%Y-%m}"
        return values


def _custom_label(start: date, end: date) -> str:
    return month_text(start) if start == end else f"{month_text(start)} to {month_text(end)}"


def parse_range(params: Mapping[str, str], today: date) -> Range | None:
    """``?range=this|3m|6m|12m|custom&from=YYYY-MM&to=YYYY-MM``; :data:`RANGE_DEFAULT`
    (``12m``) when empty. ``None``
    (the page shows :data:`RANGE_MESSAGE`) for an unknown key, or a custom range without
    both months, with ``to`` before ``from`` or longer than :data:`MAX_RANGE_MONTHS`."""
    key = (params.get("range", "") or "").strip() or RANGE_DEFAULT
    current = today.replace(day=1)
    if key in _PRESET_MONTHS:
        first = pnl.add_months(current, -(_PRESET_MONTHS[key] - 1))
        return Range(key, dict(RANGE_CHOICES)[key], first, current)
    if key != RANGE_CUSTOM:
        return None
    start = pnl._parse_month(params.get("from", "") or "")
    end = pnl._parse_month(params.get("to", "") or "")
    if start is None or end is None or end < start:
        return None
    found = Range(key, _custom_label(start, end), start, end)
    return found if len(found.months) <= MAX_RANGE_MONTHS else None


def form_months(params: Mapping[str, str], today: date) -> tuple[date, date]:
    """The months the custom From and To selects show: the ones asked for when readable,
    else the current month."""
    current = today.replace(day=1)
    start = pnl._parse_month(params.get("from", "") or "") or current
    end = pnl._parse_month(params.get("to", "") or "") or current
    return start, end


def picker_months(selected: Range | None, start: date, end: date, today: date) -> list[date]:
    """The months the custom From and To selects offer: the grid's window around the current
    month (:func:`ledger.services.grid.picker_months`) with the range's months, or with the
    two months asked for when the range could not be used."""
    current = today.replace(day=1)
    window = grid.picker_months(selected or Range(RANGE_CUSTOM, "", current, current), today)
    return sorted(set(window) | {start, end})


# --- Cashflow (D3, D4) -----------------------------------------------------------------------

#: The bars the Cashflow card draws for the last 12 months and a custom range; the shorter
#: presets draw their own months (PLAN-012g C2).
CASHFLOW_BARS = 12
#: The presets drawn as their own months, without the padding to twelve bars.
_UNPADDED = frozenset({RANGE_THIS, RANGE_3M, RANGE_6M})


@dataclass(frozen=True, slots=True)
class CashflowMonth:
    """One month's bars: collected income, the known costs and their difference."""

    month: date
    income: Decimal
    #: ``None``: no store of the month has its costs entered while some store lacks them.
    expenses: Decimal | None
    net: Decimal | None
    #: ``False`` for the months before a custom range that pad the chart to twelve bars.
    in_range: bool


@dataclass(frozen=True, slots=True)
class Cashflow:
    range: Range
    #: The chart's bars, oldest first (at most :data:`CASHFLOW_BARS`; a short preset's own
    #: months).
    months: tuple[CashflowMonth, ...]
    #: The range's totals (the padding months never count).
    income: Decimal
    expenses: Decimal | None
    net: Decimal | None
    #: The stores left out of Expenses in at least one month of the range.
    stores_without_cost: int

    @property
    def without_cost_line(self) -> str:
        """The muted line: "2 stores without a cost entered"; empty when every cost is in."""
        if not self.stores_without_cost:
            return ""
        return f"{_plural(self.stores_without_cost, 'store')} without a cost entered"


def _chart_months(selected: Range) -> list[date]:
    """The bars' months: a short preset's own months (this month, the last 3 or 6); else the
    range's last twelve, or the range padded to the left."""
    if selected.key in _UNPADDED:
        return selected.months
    last = selected.to_month
    return [pnl.add_months(last, n) for n in range(-(CASHFLOW_BARS - 1), 1)]


def _month_figures(
    rows: list[pnl.PnlRow],
) -> tuple[Decimal, Decimal | None, set[uuid.UUID]]:
    """A month's (income, expenses, stores without their costs) from its P&L rows. A store
    whose costs are unknown (no cost row in effect, or a fee cost not entered) is left out
    of the expenses whole, never counted as zero; the P&L's rows of no store (one-off
    charges without a lease) count when their fee costs are known."""
    income = pnl.total(rows)
    by_store: dict[uuid.UUID | None, list[pnl.PnlRow]] = defaultdict(list)
    for row in rows:
        by_store[row.store_id].append(row)
    known: list[pnl.PnlRow] = []
    without: set[uuid.UUID] = set()
    for store_id, members in by_store.items():
        if all(row.costs is not None for row in members):
            known.extend(members)
        elif store_id is not None:
            without.add(store_id)
    costs = pnl.total(known).costs if known or not without else None
    return income.rent_collected + income.fees_collected, costs, without


def cashflow(selected: Range) -> Cashflow:
    """The Cashflow card from the P&L's rows (:func:`ledger.services.pnl.rows`, one call over
    the chart's months and the range together), so that its totals are the P&L's for the
    same months: income = rent collected + fees collected; expenses = landlord rent + other
    costs + fee costs of the stores whose costs are known that month; net = the
    difference."""
    return cashflows((selected,))[0]


def cashflows(ranges: Sequence[Range]) -> list[Cashflow]:
    """:func:`cashflow` for each range, in order, from one call of the P&L's rows over every
    month any of them shows or totals (the dashboard's two default cards, PLAN-012g C2: the
    last 6 months are within the last 12, so the phone's card costs no query)."""
    shown_by = [_chart_months(selected) for selected in ranges]
    start = min(
        min(shown[0], selected.from_month) for shown, selected in zip(shown_by, ranges, strict=True)
    )
    end = max(selected.to_month for selected in ranges)
    query = pnl.PnlQuery(from_month=start, to_month=end)
    by_month: dict[date, list[pnl.PnlRow]] = defaultdict(list)
    for row in pnl.rows(query):
        by_month[row.month].append(row)
    figures: dict[date, tuple[Decimal, Decimal | None, set[uuid.UUID]]] = {}
    return [
        _cashflow_card(selected, shown, by_month, figures)
        for selected, shown in zip(ranges, shown_by, strict=True)
    ]


def _cashflow_card(
    selected: Range,
    shown: list[date],
    by_month: Mapping[date, list[pnl.PnlRow]],
    figures: dict[date, tuple[Decimal, Decimal | None, set[uuid.UUID]]],
) -> Cashflow:
    """One range's card from the months' P&L rows (``figures`` caches each month's)."""
    in_range = set(selected.months)
    for month in set(shown) | in_range:
        if month not in figures:
            figures[month] = _month_figures(by_month.get(month, []))
    bars = []
    for month in shown:
        income, expenses, _ = figures[month]
        bars.append(
            CashflowMonth(
                month=month,
                income=income,
                expenses=expenses,
                net=income - expenses if expenses is not None else None,
                in_range=month in in_range,
            )
        )
    income_total = sum((figures[month][0] for month in in_range), ZERO)
    known_costs = [cost for month in in_range if (cost := figures[month][1]) is not None]
    expenses_total = sum(known_costs, ZERO) if known_costs else None
    without: set[uuid.UUID] = set()
    for month in in_range:
        without |= figures[month][2]
    return Cashflow(
        range=selected,
        months=tuple(bars),
        income=income_total,
        expenses=expenses_total,
        net=income_total - expenses_total if expenses_total is not None else None,
        stores_without_cost=len(without),
    )


# --- Payment status (D5) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusPart:
    """One box under the donut: its charges' count and amount."""

    key: str
    label: str
    count: int
    amount: Decimal


PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"
#: The pill's segments, in its order.
PERIOD_CHOICES: tuple[tuple[str, str], ...] = (
    (PERIOD_WEEKLY, "Weekly"),
    (PERIOD_MONTHLY, "Monthly"),
)
#: How far from today ``?ps_at=`` may move the period, in years either way.
PERIOD_MAX_YEARS = 5


@dataclass(frozen=True, slots=True)
class Period:
    """The days the Payment status card covers, both included (PLAN-014 A3)."""

    kind: str
    start: date
    end: date
    #: "Oct 25 – 31", "Oct 25 – Nov 1" (a week across two months) or "October 2026".
    label: str
    #: A day of the previous and of the next period (``‹ ›``'s ``?ps_at=``).
    prev_at: date
    next_at: date


def _years_from(day: date, years: int) -> date:
    """``day`` moved by whole years (February 29 to February 28 in a common year)."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def _week_label(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{day_short_text(start)} – {end.day}"
    return f"{day_short_text(start)} – {day_short_text(end)}"


def period_for(kind: str, at: date | None, today: date) -> Period:
    """The week (Sunday to Saturday) or the month holding ``at``: an unknown ``kind`` is
    weekly; ``at`` is today when ``None`` or more than :data:`PERIOD_MAX_YEARS` years from
    today."""
    day = at or today
    if not _years_from(today, -PERIOD_MAX_YEARS) <= day <= _years_from(today, PERIOD_MAX_YEARS):
        day = today
    if kind == PERIOD_MONTHLY:
        first = day.replace(day=1)
        return Period(
            kind=PERIOD_MONTHLY,
            start=first,
            end=pnl.month_end(first),
            label=month_long_text(first),
            prev_at=pnl.add_months(first, -1),
            next_at=pnl.add_months(first, 1),
        )
    start = day - timedelta(days=(day.weekday() + 1) % 7)
    end = start + timedelta(days=6)
    return Period(
        kind=PERIOD_WEEKLY,
        start=start,
        end=end,
        label=_week_label(start, end),
        prev_at=start - timedelta(days=7),
        next_at=start + timedelta(days=7),
    )


@dataclass(frozen=True, slots=True)
class PaymentStatusCard:
    period: Period
    overdue: StatusPart
    paid: StatusPart
    upcoming: StatusPart
    #: Pending charges and the partly paid ones not yet due: never in the donut.
    pending: int

    @property
    def parts(self) -> tuple[StatusPart, StatusPart, StatusPart]:
        """The donut's parts, in its order."""
        return self.overdue, self.paid, self.upcoming

    @property
    def total(self) -> Decimal:
        return self.overdue.amount + self.paid.amount + self.upcoming.amount

    @property
    def count(self) -> int:
        """The charges in the donut (the pending ones are counted apart)."""
        return self.overdue.count + self.paid.count + self.upcoming.count


STATUS_OVERDUE = "overdue"
STATUS_PAID_PART = "paid"
STATUS_UPCOMING = "upcoming"


#: The one-off categories the Payment status card never counts on their own (PLAN-012f-b
#: §10 D20): an installment and an autopay-plan occurrence count through their parent charge,
#: a re-debit through its source.
PAYMENT_STATUS_SKIPPED_CATEGORIES: tuple[str, ...] = (
    OneOffCategory.INSTALLMENT.value,
    OneOffCategory.AUTOPAY_PLAN.value,
    OneOffCategory.REDEBIT.value,
)


def _one_off_family(status: str, amount_paid: Decimal, has_pending: bool) -> str:
    """A one-off charge's status in the grid's words (D20): paid; waived or voided as
    waived (nowhere); a pending payment (not a refund) as pending; else partly paid when
    anything is paid, unpaid otherwise."""
    if status == OneOffChargeStatus.PAID:
        return grid.STATUS_PAID
    if status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED):
        return grid.STATUS_WAIVED
    if has_pending:
        return grid.STATUS_PENDING
    if status == OneOffChargeStatus.PARTIAL or amount_paid > 0:
        return grid.STATUS_PARTIAL
    return grid.STATUS_UNPAID


def payment_status(period: Period, today: date) -> PaymentStatusCard:
    """The charges due in the period (by due date, for every box), by the grid's status of
    each (two queries): the rent charges by :func:`ledger.services.grid.combined_status`,
    and the one-off charges (PLAN-012f-b §10 D20) but an installment, an autopay-plan
    occurrence and a re-debit (:data:`PAYMENT_STATUS_SKIPPED_CATEGORIES`), by
    :func:`_one_off_family`. Overdue: open (unpaid, due or partly paid) and due before
    today, by balance. Paid: paid in full, however it was paid (its own payments, an invoice
    line, an installment plan, a credit) and whenever, by the amount paid. Upcoming: unpaid
    or due, due today or later, by balance. Pending charges and the partly paid ones not
    yet due are counted apart; waived (and voided) ones nowhere."""
    rent_rows = RentCharge.objects.filter(
        due_on__gte=period.start, due_on__lte=period.end
    ).values_list("status", "due_on", "amount_due", "amount_paid")
    pending_payment = Payment.objects.filter(
        one_off_charge=OuterRef("pk"), status=PaymentStatus.PENDING, is_refund=False
    )
    one_off_rows = (
        OneOffCharge.objects.filter(due_date__gte=period.start, due_date__lte=period.end)
        .exclude(category__in=PAYMENT_STATUS_SKIPPED_CATEGORIES)
        .annotate(has_pending=Exists(pending_payment))
        .values_list("status", "due_date", "price", "amount_paid", "has_pending")
    )
    charges: list[tuple[str, date, Decimal, Decimal]] = [
        (grid.combined_status([status], Decimal(paid)), due_on, Decimal(due), Decimal(paid))
        for status, due_on, due, paid in rent_rows
    ]
    charges += [
        (_one_off_family(status, Decimal(paid), has_pending), due_on, Decimal(price), Decimal(paid))
        for status, due_on, price, paid, has_pending in one_off_rows
    ]
    counts: dict[str, int] = defaultdict(int)
    amounts: dict[str, Decimal] = defaultdict(lambda: ZERO)
    pending = 0
    for family, due_on, due, paid in charges:
        if family == grid.STATUS_PAID:
            counts[STATUS_PAID_PART] += 1
            amounts[STATUS_PAID_PART] += paid
            continue
        if family == grid.STATUS_WAIVED:
            continue
        if family == grid.STATUS_PENDING or (family == grid.STATUS_PARTIAL and due_on >= today):
            pending += 1
            continue
        part = STATUS_OVERDUE if due_on < today else STATUS_UPCOMING
        counts[part] += 1
        amounts[part] += max(due - paid, ZERO)
    return PaymentStatusCard(
        period=period,
        overdue=StatusPart(
            STATUS_OVERDUE, "Overdue", counts[STATUS_OVERDUE], amounts[STATUS_OVERDUE]
        ),
        paid=StatusPart(
            STATUS_PAID_PART, "Paid", counts[STATUS_PAID_PART], amounts[STATUS_PAID_PART]
        ),
        upcoming=StatusPart(
            STATUS_UPCOMING, "Upcoming", counts[STATUS_UPCOMING], amounts[STATUS_UPCOMING]
        ),
        pending=pending,
    )


# --- The mini calendar (D6) ------------------------------------------------------------------


#: The order of a day's dots and of the legend (PLAN-012g C3): the debit day, the re-debit, the
#: lease end, then the insurance anniversary, the reminder and the note.
DOT_ORDER: tuple[str, ...] = (
    calendar.KIND_DEBIT,
    calendar.KIND_REDEBIT,
    calendar.KIND_LEASE_END,
    calendar.KIND_INSURANCE,
    calendar.KIND_REMINDER,
    calendar.KIND_NOTE,
)
#: The dots a day shows at most, one per kind present.
MAX_DOTS = 3
_WEEKDAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _kinds_of(items: Iterable[calendar.CalendarItem]) -> list[str]:
    present = {item.kind for item in items}
    return [kind for kind in DOT_ORDER if kind in present]


@dataclass(frozen=True, slots=True)
class MiniDay:
    day: date
    today: bool
    in_month: bool
    items: tuple[calendar.CalendarItem, ...]

    @property
    def kinds(self) -> list[str]:
        """The day's dots: one per kind present, in :data:`DOT_ORDER`, at most
        :data:`MAX_DOTS`."""
        return _kinds_of(self.items)[:MAX_DOTS]

    @property
    def label(self) -> str:
        """The phone panel's heading: "Wed, Oct 28"."""
        return f"{_WEEKDAYS_SHORT[self.day.weekday()]}, {day_short_text(self.day)}"


@dataclass(frozen=True, slots=True)
class MiniCalendar:
    month: date
    previous: date
    next: date
    weeks: list[list[MiniDay]]

    @property
    def days_with_items(self) -> list[MiniDay]:
        """The days that have items, in date order (the list under the grid without a script)."""
        return [cell for week in self.weeks for cell in week if cell.items]

    @property
    def legend(self) -> list[tuple[str, str]]:
        """``(kind, label)`` of every kind present in the month, in :data:`DOT_ORDER`
        (the legend under the grid; empty when the month has no items)."""
        items = [item for cell in self.days_with_items for item in cell.items]
        return [(kind, calendar.KIND_LABELS[kind]) for kind in _kinds_of(items)]


def parse_cal(raw: str, today: date) -> date:
    """``?cal=YYYY-MM``: the mini calendar's month; the current month when unreadable."""
    return pnl._parse_month(raw or "") or today.replace(day=1)


def mini_calendar(month: date, today: date) -> MiniCalendar:
    """The month's weeks (Sunday first) with each day's items: the Calendar page's own
    reads (:func:`ledger.services.calendar.items_in` over the month, every kind, no store
    or operator filter, three queries; :func:`ledger.services.calendar.month_weeks`)."""
    first, last = calendar.month_bounds(month)
    found = calendar.items_in(((first, last),), calendar.Filters())
    weeks = [
        [MiniDay(cell.day, cell.day == today, cell.in_month, cell.items) for cell in week]
        for week in calendar.month_weeks(first, found)
    ]
    return MiniCalendar(
        month=first,
        previous=pnl.add_months(first, -1),
        next=pnl.add_months(first, 1),
        weeks=weeks,
    )
