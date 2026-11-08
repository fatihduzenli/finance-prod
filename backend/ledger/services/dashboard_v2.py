"""The dashboard of Design v2 (docs/qa/PLAN-015-design-v2.md §4.2; the owner's mockup
docs/design/dashboard-v2-dark-laptop.png).

Read only: nothing here writes or calls the payment provider. Each block reads the services
its report reads, so that a figure equals that report for the same range, in a number of
queries that does not grow with the stores, the operators or the payments.

**The period** (:func:`window`, ``?period=`` read by
:func:`finance.templatetags.components.parse_period`): this calendar month, this calendar
quarter or this calendar year, whole months; the previous period is the same number of months
before it. It scopes the four KPIs only (the owner's decision 2).

**The KPIs** (:func:`kpis`):

- **Due**: rent and charges billed in the period, the Revenue by month report's total billed
  (:func:`ledger.services.reports.revenue_by_month`, the P&L's rows); the stores billed; the
  next rent due day of the period from today (else its first);
- **Collected**: the Payments by method report's total for the period
  (:func:`ledger.services.reports.payments_by_method`: settled payments less settled refunds,
  deposits and credits left out; a payment voided is not settled, so it never counts), the
  whole per cent of Due rounded down (not capped: a period may collect arrears), and the change
  against the previous period in per cent to one decimal (none when the previous period
  collected nothing);
- **Overdue**: the open balances past due today (:func:`ledger.services.receivables.open_balances`,
  the Aging report's rows: its total less its Current bucket), the stores, the oldest days past
  due, and the Needs attention count (:func:`ledger.services.attention.attention`, the items
  left out by the per-kind limit counted). A balance, so the same in every period;
- **Bank deposits**: :func:`banking.services.summary` for the period: the credits posted, the
  deposits still New and the connected account.

**Collections** (:func:`collections`, :func:`collections_by_year`): Payments by method per
month of a year (January to the current month) or per year of the last :data:`YEARS` years,
with Revenue by month's billed as each bar's Due; the current month or year flagged.

**Operator tiles** (:func:`operator_tiles`): the operators with a lease in effect today (the
contact log's rule), their open balance, their stores (the Operators list's count: a lease not
ended before today), a chip, by balance (largest first), then name.

**Activity** (:func:`activity`): the feed of the last :data:`ACTIVITY_DAYS` days from the
payments, the invoices, the promises, the returns and the lease ends (each source at most
:data:`SOURCE_LIMIT` rows, newest first), or today's Needs attention items; ``q`` keeps the rows
whose store, operator and amount hold every word; :data:`PAGE_SIZE` a page.

Money is ``Decimal``; dates are the project's ``TIME_ZONE`` days.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.core.paginator import Page, Paginator
from django.db.models import Count, Exists, Min, OuterRef, Q
from django.urls import reverse
from django.utils import timezone

from banking import services as banking
from finance.dates import day_text, month_long_text, month_name_text, month_short_text
from finance.templatetags.components import (
    NEUTRAL,
    OVERDUE,
    PAID,
    PARTIAL,
    PENDING,
    UNPAID,
    WAIVED,
    badge_family,
)
from ledger.models import (
    NOT_BILLED_RENT,
    ZERO,
    ContactEntry,
    ContactKind,
    ContactStatus,
    Invoice,
    InvoiceStatus,
    Lease,
    Operator,
    Payment,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    Store,
)
from ledger.services import attention, contacts, pnl, receivables, reports, search, stores
from ledger.services.payments_ledger import charge_lease
from ledger.wording import format_amount, provider_sentence

PERIOD_MONTH = "month"
PERIOD_QUARTER = "quarter"
PERIOD_YEAR = "year"
_PERIOD_MONTHS: dict[str, int] = {PERIOD_MONTH: 1, PERIOD_QUARTER: 3, PERIOD_YEAR: 12}
#: The Due card's title by period.
DUE_TITLES: dict[str, str] = {
    PERIOD_MONTH: "Due this month",
    PERIOD_QUARTER: "Due this quarter",
    PERIOD_YEAR: "Due this year",
}
NOTHING_DUE = "nothing due"
NO_BANK = "no bank connected"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def ordinal_day(day: date) -> str:
    """``Nov 1st``, ``Nov 22nd``, ``Nov 13th`` (the mockup's due day)."""
    number = day.day
    suffix = (
        "th" if 11 <= number % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    )
    return f"{month_short_text(day)} {number}{suffix}"


# --- The period ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    """Whole months, ``from_month`` to ``to_month`` (first days, both inclusive)."""

    period: str
    from_month: date
    to_month: date

    @property
    def start(self) -> date:
        return self.from_month

    @property
    def end(self) -> date:
        return pnl.month_end(self.to_month)

    @property
    def report_range(self) -> reports.ReportRange:
        return reports.ReportRange(self.from_month, self.to_month)

    def previous(self) -> Window:
        months = _PERIOD_MONTHS[self.period]
        return Window(
            self.period,
            pnl.add_months(self.from_month, -months),
            pnl.add_months(self.to_month, -months),
        )


def window(period: str, today: date) -> Window:
    """The calendar month, quarter or year of ``today``; any other ``period`` is the month."""
    month = today.replace(day=1)
    if period == PERIOD_QUARTER:
        first = month.replace(month=(month.month - 1) // 3 * 3 + 1)
        return Window(PERIOD_QUARTER, first, pnl.add_months(first, 2))
    if period == PERIOD_YEAR:
        return Window(PERIOD_YEAR, month.replace(month=1), month.replace(month=12))
    return Window(PERIOD_MONTH, month, month)


# --- The KPIs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Due:
    title: str
    amount: Decimal
    stores: int
    #: The next rent due day of the period from today, else its first; ``None`` without one.
    next_due: date | None

    @property
    def sub(self) -> str:
        """``38 stores · due Nov 1st``."""
        text = _plural(self.stores, "store")
        return f"{text} · due {ordinal_day(self.next_due)}" if self.next_due else text


@dataclass(frozen=True, slots=True)
class Collected:
    amount: Decimal
    #: The whole per cent of Due, rounded down; ``None`` when nothing is due.
    percent: int | None
    #: Against the previous period, in per cent to one decimal; ``None`` without a figure.
    delta: Decimal | None

    @property
    def sub(self) -> str:
        """``77% of what is due``, or :data:`NOTHING_DUE`."""
        return f"{self.percent}% of what is due" if self.percent is not None else NOTHING_DUE

    @property
    def badge(self) -> str:
        """``↑ 4.2%``, ``↓ 3.0%``, ``0.0%``; ``""`` without a previous figure."""
        if self.delta is None:
            return ""
        if self.delta > 0:
            return f"↑ {self.delta}%"
        if self.delta < 0:
            return f"↓ {-self.delta}%"
        return f"{self.delta}%"

    @property
    def badge_tone(self) -> str:
        if self.delta is None or self.delta == 0:
            return NEUTRAL
        return PAID if self.delta > 0 else UNPAID


@dataclass(frozen=True, slots=True)
class Overdue:
    amount: Decimal
    stores: int
    #: The days the oldest overdue charge is past due; ``0`` without one.
    oldest_days: int
    #: The Needs attention items today (the per-kind limit's left-out ones counted).
    attention: int

    @property
    def sub(self) -> str:
        """``9 stores · oldest 45 days · 7 need attention`` (the last part when above zero)."""
        parts = [_plural(self.stores, "store")]
        if self.oldest_days:
            parts.append(f"oldest {_plural(self.oldest_days, 'day')}")
        if self.attention:
            parts.append(f"{self.attention} need{'s' if self.attention == 1 else ''} attention")
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class BankDeposits:
    amount: Decimal
    #: ``Chase ····4821``; ``""`` without a connection.
    account: str
    unmatched: int

    @property
    def sub(self) -> str:
        return self.account or NO_BANK

    @property
    def badge(self) -> str:
        return f"{self.unmatched} unmatched" if self.unmatched else ""


@dataclass(frozen=True, slots=True)
class Kpis:
    window: Window
    due: Due
    collected: Collected
    overdue: Overdue
    bank: BankDeposits


@dataclass(frozen=True, slots=True)
class Reads:
    """The P&L's rows and Payments by method over one range of whole months, read once for the
    KPIs and the Collections bars (PLAN-015 Batch 1, Fable's gate: never twice a request)."""

    rows: list[pnl.PnlRow]
    by_method: reports.PaymentsByMethod

    def billed(self, from_month: date, to_month: date) -> tuple[Decimal, int]:
        """Revenue by month's total billed from ``from_month`` to ``to_month`` and the stores
        billed."""
        total = reports.RevenueMonth(None)
        billed_stores: set[uuid.UUID] = set()
        for row in self.rows:
            if not from_month <= row.month <= to_month:
                continue
            total.add(row)
            if row.store_id is not None and row.rent_billed + row.fees_billed > 0:
                billed_stores.add(row.store_id)
        return total.total_billed, len(billed_stores)

    def billed_by_month(self) -> dict[date, Decimal]:
        """Revenue by month's billed per month (as
        :func:`ledger.services.reports.revenue_by_month` sums them)."""
        months: dict[date, reports.RevenueMonth] = {}
        for row in self.rows:
            months.setdefault(row.month, reports.RevenueMonth(row.month)).add(row)
        return {month: item.total_billed for month, item in months.items()}

    def collected(self, from_month: date, to_month: date) -> Decimal:
        """Payments by method's total from ``from_month`` to ``to_month``."""
        return sum(
            (
                item.total
                for item in self.by_method.months
                if item.month is not None and from_month <= item.month <= to_month
            ),
            ZERO,
        )


def reads(from_month: date, to_month: date) -> Reads:
    """:class:`Reads` from ``from_month`` to ``to_month`` (the P&L's fixed queries and one)."""
    return Reads(
        rows=pnl.rows(pnl.PnlQuery(from_month, to_month)),
        by_method=reports.payments_by_method(reports.ReportRange(from_month, to_month)),
    )


def chart_range(today: date, yearly: bool) -> tuple[date, date]:
    """The Collections bars' months: January to today's month, or the last :data:`YEARS`
    years to today's month."""
    first = date(today.year - YEARS + 1, 1, 1) if yearly else date(today.year, 1, 1)
    return first, today.replace(day=1)


def page_reads(period: str, today: date, yearly: bool) -> Reads:
    """One :class:`Reads` covering the period, the period before it and the bars."""
    selected = window(period, today)
    first, last = chart_range(today, yearly)
    return reads(min(selected.previous().from_month, first), max(selected.to_month, last))


def _next_due(selected: Window, today: date) -> date | None:
    """The period's rent due day from ``today``, else its first (one query)."""
    found = (
        RentCharge.objects.filter(due_on__gte=selected.start, due_on__lte=selected.end)
        .exclude(status__in=NOT_BILLED_RENT)
        .aggregate(next=Min("due_on", filter=Q(due_on__gte=today)), first=Min("due_on"))
    )
    day = found["next"] or found["first"]
    return day if isinstance(day, date) else None


def percent_of(part: Decimal, whole: Decimal) -> int | None:
    """The whole per cent ``part`` is of ``whole``, rounded down; ``None`` when ``whole`` is
    not above zero."""
    if whole <= 0:
        return None
    return int((part * 100 / whole).to_integral_value(rounding=ROUND_FLOOR))


def change_of(current: Decimal, previous: Decimal) -> Decimal | None:
    """The change from ``previous`` to ``current`` in per cent, to one decimal; ``None`` when
    ``previous`` is not above zero."""
    if previous <= 0:
        return None
    return ((current - previous) * 100 / previous).quantize(Decimal("0.1"), ROUND_HALF_UP)


def collected_by_window(selected: Window, read: Reads | None = None) -> tuple[Decimal, Decimal]:
    """Payments by method's total for ``selected`` and for the period before it (one query,
    none with ``read``)."""
    before = selected.previous()
    found = read if read is not None else reads(before.from_month, selected.to_month)
    return (
        found.collected(selected.from_month, selected.to_month),
        found.collected(before.from_month, before.to_month),
    )


def needs_attention(
    today: date, balances: Sequence[receivables.OpenBalance] | None = None
) -> attention.Attention:
    """Today's Needs attention items, their stores titled (one more query); ``balances`` the
    open balances of ``today`` when the caller read them already."""
    found = attention.attention(
        today,
        leases=attention.dashboard_leases(),
        balances=list(balances) if balances is not None else None,
    )
    ids = {item.store_id for item in found.items if item.store_id is not None}
    titles = {store.pk: stores.title(store) for store in Store.objects.filter(pk__in=ids)}
    found.items = [
        replace(item, store=titles[item.store_id]) if item.store_id in titles else item
        for item in found.items
    ]
    return found


def attention_total(found: attention.Attention) -> int:
    return len(found.items) + sum(found.more.values())


def kpis(
    period: str,
    today: date,
    *,
    balances: Sequence[receivables.OpenBalance] | None = None,
    attention_count: int | None = None,
    read: Reads | None = None,
) -> Kpis:
    """The four KPIs for ``period`` (see the module docstring). ``balances``,
    ``attention_count`` and ``read`` (covering the period and the one before it) are the open
    balances, the Needs attention count and the reports' rows when the caller read them
    already."""
    selected = window(period, today)
    if read is None:
        read = reads(selected.previous().from_month, selected.to_month)
    due_amount, due_stores = read.billed(selected.from_month, selected.to_month)
    current, previous = collected_by_window(selected, read)
    rows = list(balances) if balances is not None else receivables.open_balances(today)
    overdue_rows = [row for row in rows if row.overdue(today)]
    count = (
        attention_count
        if attention_count is not None
        else attention_total(needs_attention(today, rows))
    )
    bank = banking.summary(selected.start, selected.end)
    return Kpis(
        window=selected,
        due=Due(DUE_TITLES[selected.period], due_amount, due_stores, _next_due(selected, today)),
        collected=Collected(current, percent_of(current, due_amount), change_of(current, previous)),
        overdue=Overdue(
            amount=receivables.total(overdue_rows),
            stores=len({row.store_id for row in overdue_rows if row.store_id is not None}),
            oldest_days=max(
                (receivables.days_past_due(row, today) for row in overdue_rows), default=0
            ),
            attention=count,
        ),
        bank=BankDeposits(bank.total, bank.account, bank.unmatched),
    )


# --- Collections -----------------------------------------------------------------------------

#: The years the Yearly chart shows, the current one last.
YEARS = 5


@dataclass(frozen=True, slots=True)
class CollectionBar:
    #: Under the bar: ``Nov``, or ``2026``.
    label: str
    #: The tooltip's heading: ``November 2026``, or ``2026``.
    title: str
    collected: Decimal
    due: Decimal
    current: bool


@dataclass(frozen=True, slots=True)
class Collections:
    bars: list[CollectionBar]
    total: Decimal
    #: Under the total: ``collected from January to November 2026``.
    since: str
    yearly: bool = False


def _bars(read: Reads, first: date, last: date) -> list[tuple[date, Decimal, Decimal]]:
    """(month, collected, due) per month from ``first`` to ``last``."""
    due = read.billed_by_month()
    month, found = first, []
    while month <= last:
        found.append((month, read.collected(month, month), due.get(month, ZERO)))
        month = pnl.add_months(month, 1)
    return found


def collections(year: int, today: date, read: Reads | None = None) -> Collections:
    """The months of ``year`` up to today's month (every month of a past year); ``read``
    covers them when the caller read it already."""
    first = date(year, 1, 1)
    last = today.replace(day=1) if year == today.year else date(year, 12, 1)
    months = _bars(read if read is not None else reads(first, last), first, last)
    this_month = today.replace(day=1)
    return Collections(
        bars=[
            CollectionBar(
                month_short_text(month), month_long_text(month), paid, due, month == this_month
            )
            for month, paid, due in months
        ],
        total=sum((paid for _, paid, _ in months), ZERO),
        since=f"collected from {month_name_text(first)} to {month_long_text(last)}",
    )


def collections_by_year(today: date, years: int = YEARS, read: Reads | None = None) -> Collections:
    """One bar per year of the last ``years`` years, the current one to today's month."""
    first = date(today.year - years + 1, 1, 1)
    last = today.replace(day=1)
    months = _bars(read if read is not None else reads(first, last), first, last)
    by_year: dict[int, tuple[Decimal, Decimal]] = {
        year: (ZERO, ZERO) for year in range(first.year, today.year + 1)
    }
    for month, paid, due in months:
        so_far_paid, so_far_due = by_year[month.year]
        by_year[month.year] = (so_far_paid + paid, so_far_due + due)
    return Collections(
        bars=[
            CollectionBar(str(year), str(year), paid, due, year == today.year)
            for year, (paid, due) in by_year.items()
        ],
        total=sum((paid for _, paid, _ in months), ZERO),
        since=f"collected from {first.year} to {month_long_text(today)}",
        yearly=True,
    )


# --- Operator tiles --------------------------------------------------------------------------

TILE_LIMIT = 4
PAID_UP = "Paid up"


@dataclass(frozen=True, slots=True)
class OperatorTile:
    operator_id: uuid.UUID
    name: str
    balance: Decimal
    stores: int
    tone: str
    chip: str

    @property
    def url(self) -> str:
        return reverse("ledger:operators:detail", kwargs={"pk": self.operator_id})


def _chip(
    rows: Sequence[receivables.OpenBalance], promise: date | None, today: date
) -> tuple[str, str]:
    """(tone, words) of an operator's chip: Paid up; else Promise · the promised day; else
    Overdue n days (the oldest); else Partial (a charge partly paid); else Due · the next due
    day."""
    if not rows:
        return PAID, PAID_UP
    if promise is not None:
        return PENDING, f"Promise · {month_short_text(promise)} {promise.day}"
    overdue = [receivables.days_past_due(row, today) for row in rows if row.overdue(today)]
    if overdue:
        return OVERDUE, f"Overdue {_plural(max(overdue), 'day')}"
    if any(row.status == RentChargeStatus.PARTIAL for row in rows):
        return PARTIAL, "Partial"
    due = min(row.due_on for row in rows)
    return PENDING, f"Due · {month_short_text(due)} {due.day}"


def operator_tiles(
    today: date,
    limit: int = TILE_LIMIT,
    *,
    balances: Sequence[receivables.OpenBalance] | None = None,
) -> list[OperatorTile]:
    """The first ``limit`` tiles (see the module docstring; two queries and the open
    balances')."""
    in_effect = Lease.objects.filter(contacts._in_effect(today), operator=OuterRef("pk"))
    operators = list(
        Operator.objects.filter(Exists(in_effect))
        .annotate(
            store_count=Count(
                "leases__store",
                distinct=True,
                filter=Q(leases__ended_on__isnull=True) | Q(leases__ended_on__gte=today),
            )
        )
        .values_list("pk", "display_name", "store_count")
    )
    rows = list(balances) if balances is not None else receivables.open_balances(today)
    by_operator: dict[uuid.UUID, list[receivables.OpenBalance]] = {}
    for row in rows:
        if row.operator_id is not None:
            by_operator.setdefault(row.operator_id, []).append(row)
    promises = dict(
        ContactEntry.objects.filter(
            kind=ContactKind.PROMISE,
            status=ContactStatus.OPEN,
            deleted_at__isnull=True,
            promised_date__gte=today,
        )
        .values("operator")
        .annotate(first=Min("promised_date"))
        .values_list("operator", "first")
        .order_by()
    )
    tiles = []
    for pk, name, store_count in operators:
        owed = by_operator.get(pk, [])
        tone, words = _chip(owed, promises.get(pk) if owed else None, today)
        tiles.append(OperatorTile(pk, name, receivables.total(owed), store_count, tone, words))
    tiles.sort(key=lambda tile: (-tile.balance, tile.name.casefold(), str(tile.operator_id)))
    return tiles[:limit]


# --- Activity --------------------------------------------------------------------------------

ACTIVITY_DAYS = 90
SOURCE_LIMIT = 200
PAGE_SIZE = 5

KIND_ALL = ""
KIND_PAYMENTS = "payments"
KIND_INVOICES = "invoices"
KIND_PROMISES = "promises"
KIND_RETURNS = "returns"
KIND_LEASES = "leases"
KIND_ATTENTION = "attention"
#: The kind select's values and words, in its order.
KINDS: tuple[tuple[str, str], ...] = (
    (KIND_ALL, "All activity"),
    (KIND_PAYMENTS, "Payments"),
    (KIND_INVOICES, "Invoices"),
    (KIND_PROMISES, "Promises"),
    (KIND_RETURNS, "Returns"),
    (KIND_LEASES, "Leases"),
    (KIND_ATTENTION, "Needs attention"),
)
_KIND_VALUES = frozenset(value for value, _ in KINDS)

PAYMENT_RECEIVED = "Payment received"
PAID_OUTSIDE = provider_sentence("Paid outside {provider}")
INVOICE_SENT = "Invoice sent"
PROMISE_TO_PAY = "Promise to pay"
PROMISE_BROKEN = "Promise broken"
PAYMENT_RETURNED = "Payment returned (NSF)"
LEASE_ENDED = "Lease ended"
EMPTY = "No activity matches."
EMPTY_ATTENTION = "Nothing needs attention."


def parse_kind(raw: object) -> str:
    """``raw`` when it is one of :data:`KINDS`' values, else every kind."""
    return raw if isinstance(raw, str) and raw in _KIND_VALUES else KIND_ALL


@dataclass(frozen=True, slots=True)
class ActivityRow:
    kind: str
    activity: str
    store: str
    operator: str
    on: date
    amount: Decimal | None
    tone: str
    chip: str
    url: str = ""

    def matches(self, words: Sequence[str]) -> bool:
        """Every word is in the store, the operator or the amount (``$1,066.67``, ``1,066.67``
        or ``1066.67``), case ignored."""
        amount = ""
        if self.amount is not None:
            shown = format_amount(self.amount)
            amount = f"{shown} {shown.replace('$', '')} {shown.replace('$', '').replace(',', '')}"
        haystack = f"{self.store} {self.operator} {amount}".casefold()
        return all(word.casefold() in haystack for word in words)


@dataclass(frozen=True, slots=True)
class Activity:
    #: The page's rows.
    rows: list[ActivityRow]
    total: int
    page: Page[ActivityRow]
    query: str
    kind: str

    @property
    def empty_text(self) -> str:
        return EMPTY_ATTENTION if self.kind == KIND_ATTENTION and not self.query else EMPTY


def _since(today: date) -> date:
    return today - timedelta(days=ACTIVITY_DAYS)


def _store_title(lease: Lease | None) -> str:
    return stores.title(lease.store) if lease is not None else ""


def _payments(today: date) -> list[ActivityRow]:
    """Settled payments received (not a refund, not a credit applied): by bank debit, Payment
    received (✓ Paid); recorded by hand, Paid outside {provider} (Paid, or Partial while the
    charge still has a balance)."""
    found = (
        Payment.objects.filter(
            status=PaymentStatus.SETTLED,
            is_refund=False,
            settled_on__gte=_since(today),
            settled_on__lte=today,
        )
        .exclude(method=PaymentMethod.CREDIT)
        .select_related(
            "invoice__operator",
            "rent_charge__lease__store",
            "rent_charge__lease__operator",
            "insurance_charge__lease__store",
            "insurance_charge__lease__operator",
            "one_off_charge__lease__store",
            "one_off_charge__lease__operator",
            "one_off_charge__operator",
        )
        .order_by("-settled_on", "-updated_at", "-id")[:SOURCE_LIMIT]
    )
    rows = []
    for payment in found:
        lease = charge_lease(payment)
        operator = payment.invoice.operator if payment.invoice is not None else None
        if operator is None and payment.one_off_charge is not None:
            operator = payment.one_off_charge.operator
        if operator is None and lease is not None:
            operator = lease.operator
        settled_on = payment.settled_on or today
        if payment.method == PaymentMethod.ACH:
            label, tone, chip = PAYMENT_RECEIVED, PAID, "Paid"
        else:
            charge = payment.rent_charge or payment.one_off_charge
            still = charge is not None and Decimal(charge.balance) > 0
            label, tone, chip = (
                str(PAID_OUTSIDE),
                (PARTIAL if still else PAID),
                ("Partial" if still else "Paid"),
            )
        rows.append(
            ActivityRow(
                KIND_PAYMENTS,
                label,
                _store_title(lease),
                operator.display_name if operator is not None else "",
                settled_on,
                Decimal(payment.amount),
                tone,
                chip,
                reverse("ledger:payments:detail", kwargs={"pk": payment.pk}),
            )
        )
    return rows


def _invoice_lease(invoice: Invoice) -> Lease | None:
    for charge in (invoice.rent_charge, invoice.insurance_charge, invoice.one_off_charge):
        if charge is not None and charge.lease is not None:
            return charge.lease
    return None


def _invoices(today: date) -> list[ActivityRow]:
    """Invoices sent (not void): Paid once paid in full, else Unpaid."""
    found = (
        Invoice.objects.filter(sent_at__isnull=False)
        .exclude(internal_status=InvoiceStatus.VOID)
        .filter(sent_at__date__gte=_since(today))
        .select_related(
            "operator",
            "rent_charge__lease__store",
            "insurance_charge__lease__store",
            "one_off_charge__lease__store",
        )
        .order_by("-sent_at", "-id")[:SOURCE_LIMIT]
    )
    rows = []
    for invoice in found:
        sent_at = invoice.sent_at
        if sent_at is None:
            continue
        on = timezone.localtime(sent_at).date()
        if on > today:
            continue
        paid = invoice.total > 0 and invoice.amount_paid >= invoice.total
        rows.append(
            ActivityRow(
                KIND_INVOICES,
                INVOICE_SENT,
                _store_title(_invoice_lease(invoice)),
                invoice.operator.display_name,
                on,
                Decimal(invoice.total),
                PAID if paid else UNPAID,
                "Paid" if paid else "Unpaid",
            )
        )
    return rows


def _promises(today: date) -> list[ActivityRow]:
    """Promises to pay of the window (● Due <day> while open, ✓ Kept, Rescheduled) and the
    promises broken (:func:`ledger.services.contacts.broken_promises`, and those recorded
    broken), each once: a broken one as Promise broken, dated the day after its promised day."""
    broken = {entry.pk: entry for entry in contacts.broken_promises(today)}
    found = (
        ContactEntry.objects.filter(
            kind=ContactKind.PROMISE, deleted_at__isnull=True, happened_on__gte=_since(today)
        )
        .select_related("operator", "lease__store")
        .order_by("-happened_on", "-created_at", "-id")[:SOURCE_LIMIT]
    )
    rows = []
    entries = {entry.pk: entry for entry in found}
    for entry in [*entries.values(), *(e for pk, e in broken.items() if pk not in entries)]:
        store = _store_title(entry.lease)
        amount = Decimal(entry.promised_amount) if entry.promised_amount is not None else None
        promised = entry.promised_date or entry.happened_on
        if entry.pk in broken or entry.status == ContactStatus.BROKEN:
            rows.append(
                ActivityRow(
                    KIND_PROMISES,
                    PROMISE_BROKEN,
                    store,
                    entry.operator.display_name,
                    min(promised + timedelta(days=1), today),
                    amount,
                    UNPAID,
                    "Broken",
                )
            )
            continue
        if entry.status == ContactStatus.KEPT:
            tone, chip = PAID, "Kept"
        elif entry.status == ContactStatus.RESCHEDULED:
            tone, chip = NEUTRAL, "Rescheduled"
        else:
            tone, chip = PENDING, f"Due {day_text(promised)}"
        rows.append(
            ActivityRow(
                KIND_PROMISES,
                PROMISE_TO_PAY,
                store,
                entry.operator.display_name,
                entry.happened_on,
                amount,
                tone,
                chip,
            )
        )
    return rows


def _returns(today: date) -> list[ActivityRow]:
    """Payments returned in the window, the reason (else the return code) on the chip."""
    found = (
        Payment.objects.filter(status=PaymentStatus.RETURNED, returned_at__isnull=False)
        .filter(returned_at__date__gte=_since(today))
        .select_related(
            "invoice__operator",
            "rent_charge__lease__store",
            "rent_charge__lease__operator",
            "insurance_charge__lease__store",
            "insurance_charge__lease__operator",
            "one_off_charge__lease__store",
            "one_off_charge__lease__operator",
            "one_off_charge__operator",
        )
        .order_by("-returned_at", "-id")[:SOURCE_LIMIT]
    )
    rows = []
    for payment in found:
        returned_at = payment.returned_at
        if returned_at is None:
            continue
        on = timezone.localtime(returned_at).date()
        if on > today:
            continue
        lease = charge_lease(payment)
        operator = payment.invoice.operator if payment.invoice is not None else None
        if operator is None and payment.one_off_charge is not None:
            operator = payment.one_off_charge.operator
        if operator is None and lease is not None:
            operator = lease.operator
        rows.append(
            ActivityRow(
                KIND_RETURNS,
                PAYMENT_RETURNED,
                _store_title(lease),
                operator.display_name if operator is not None else "",
                on,
                Decimal(payment.amount),
                OVERDUE,
                payment.return_reason or payment.return_code or "Returned",
                reverse("ledger:payments:detail", kwargs={"pk": payment.pk}),
            )
        )
    return rows


def _lease_ends(today: date) -> list[ActivityRow]:
    """Leases ended in the window: ○ Vacant · n days while the store has no lease in effect
    today, else ○ Ended."""
    relet = Lease.objects.filter(contacts._in_effect(today), store=OuterRef("store")).exclude(
        pk=OuterRef("pk")
    )
    found = (
        Lease.objects.filter(ended_on__gte=_since(today), ended_on__lte=today)
        .annotate(relet=Exists(relet))
        .select_related("store", "operator")
        .order_by("-ended_on", "-id")[:SOURCE_LIMIT]
    )
    rows = []
    for lease in found:
        ended_on = lease.ended_on
        if ended_on is None:
            continue
        days = (today - ended_on).days
        chip = "Ended" if getattr(lease, "relet", False) else f"Vacant · {_plural(days, 'day')}"
        rows.append(
            ActivityRow(
                KIND_LEASES,
                LEASE_ENDED,
                stores.title(lease.store),
                lease.operator.display_name if lease.operator is not None else "",
                ended_on,
                None,
                NEUTRAL,
                chip,
                reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
            )
        )
    return rows


def _attention_rows(found: attention.Attention, today: date) -> list[ActivityRow]:
    """Today's Needs attention items, each with its why badge's family as the chip."""
    rows = []
    for item in found.items:
        family = badge_family(item.badge_value)
        tone = NEUTRAL if family in ("", WAIVED) else family
        rows.append(
            ActivityRow(
                KIND_ATTENTION,
                item.details or item.why,
                item.store,
                item.operator_name,
                item.since or today,
                item.amount,
                tone,
                item.why,
                item.url,
            )
        )
    return rows


_SOURCES = {
    KIND_PAYMENTS: _payments,
    KIND_INVOICES: _invoices,
    KIND_PROMISES: _promises,
    KIND_RETURNS: _returns,
    KIND_LEASES: _lease_ends,
}


def activity_rows(
    today: date, query: str, kind: str, *, found: attention.Attention | None = None
) -> list[ActivityRow]:
    """Every row of ``kind`` matching ``query`` (cleaned as the search box cleans it), newest
    first. ``found`` is today's Needs attention when the caller read it already."""
    chosen = parse_kind(kind)
    if chosen == KIND_ATTENTION:
        rows = _attention_rows(found if found is not None else needs_attention(today), today)
    else:
        rows = []
        for name, source in _SOURCES.items():
            if chosen in (KIND_ALL, name):
                rows += source(today)
        rows.sort(key=lambda row: row.on, reverse=True)
    words = search.words(search.clean_query(query))
    return [row for row in rows if row.matches(words)] if words else rows


def activity(
    today: date,
    query: str,
    kind: str,
    page: object,
    *,
    found: attention.Attention | None = None,
) -> Activity:
    """One page of :func:`activity_rows`: :data:`PAGE_SIZE` rows; a page number past the last
    is the last page, one that is not a number the first."""
    cleaned = search.clean_query(query)
    chosen = parse_kind(kind)
    rows = activity_rows(today, cleaned, chosen, found=found)
    paginator = Paginator(rows, PAGE_SIZE)
    current = paginator.get_page(page if isinstance(page, str | int) else None)
    return Activity(
        rows=list(current.object_list),
        total=paginator.count,
        page=current,
        query=cleaned,
        kind=chosen,
    )
