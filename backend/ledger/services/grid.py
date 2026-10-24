"""The month grid: stores down, months across (docs/qa/PLAN-011-dashboard-reports-mobile.md
D2; SCOPE Flow 3 and Flow 4).

Read only: nothing here writes or calls the payment provider.

**The query** (:func:`parse_query`): ``from`` and ``to`` are months written ``YYYY-MM``
(both inclusive); the default is the 10 months before the current one, the current
month and the next (PLAN-012d D12; :func:`default_range`). A month that cannot be read
gives the default range (it is never echoed), a ``to`` before ``from`` is swapped and a
range longer than :data:`MAX_MONTHS` keeps its last :data:`MAX_MONTHS` months, each with
a sentence, never an error. The page's month pickers are plain selects (PLAN-012d D12:
not HTML month inputs, which Safari does not render) offering the current month and the
:data:`PICKER_MONTHS` before and after it, with the shown range's months added
(:func:`picker_months`). The filters are
``operator`` (an operator's id), ``state`` (the store's state, any case), ``channel`` (a
lease payment channel) and ``status`` (a cell status); an unusable one is dropped with a
sentence. ``q`` (PLAN-012d D16) keeps the stores whose number, name, city or operator
matches every word (:func:`ledger.services.search.filter_stores`, in the stores query);
empty, no filter.

**Rows** are stores, in store-number order (stores without a number after, by label):
label, number, state, and the operator and channel of the store's *current* lease (the
lease active today, else the latest started). The page's store cell (PLAN-012e-4 D4) reads
:attr:`GridRow.store_title` ("#1991 Istanbul") over the operator's name; the state and the
channel stay in the CSV and the filters. Every store is a row, with or without a
lease. The operator and channel filters apply to those current values; the status filter
keeps a row with at least one cell of that status in the range.

**Cells** are the rent charges of the store's leases in each month, together: a
twice-monthly lease's two charges, or the charges of two leases of one month after a
transfer, make one cell with their sums. The cell's status, from its charges' statuses
leaving out waived ones (all waived: ``waived``): all ``paid`` is ``paid``; any
``pending`` is ``pending``; something paid but not all is ``partial``; any ``unpaid`` is
``unpaid``; otherwise ``due`` (unpaid, not yet overdue: its text reads "Unpaid" as
``unpaid``'s does, its colour and glyph tell them apart; PLAN-012e-4 D2). A month with no
charge (PLAN-012e-4 D3) is blank, its label the month only, when it is outside the store's
leases: before the month the first lease started or after the month the last lease ended
(a lease without a start or an end is open on that side; a store without a lease is outside
in every month). Inside them it is ``not due`` ("Not yet due") when it is after today's
month, else blank. A cell with a charge is never blank. A charge split
into a live installment plan (PLAN-012e D12) counts what its installments were paid as
paid (``partial`` once any is paid, else ``due``) and the cell shows the plan's progress
(``plan 1 of 3 paid, 333.33 of 1,000.00``); a completed plan's charge is ``paid``. A cell links to
its first charge (by lease creation and sequence) and shows the note marker when any of its
charges has a note. A month billed on an invoice builder invoice's Rent line (PLAN-012b-b
D6) adds that invoice's number to the cell's text (``paid 1,000.00 INV-2026-0012``; not
once the invoice is void); its
status is its charge's as any other, so a rent paid before its month is ``paid`` in that
month's cell, and a future month billed or paid in advance shows it with the invoice
number (PLAN-012d D12).

**The page's cell block** (PLAN-012f D6): the amount (:attr:`Cell.amount_text`) over the
status line (:attr:`Cell.status_words`, then :attr:`Cell.details`); an overdue cell (``unpaid``)
reads "Overdue N d", N the days since the earliest due date of its unpaid charges
(:attr:`Cell.overdue_days`). The link's label (:attr:`Cell.label`) says the same in full words.
The CSV keeps :func:`cell_text`.

**Column totals** (:attr:`Grid.columns`): per month, the count of cells of each status
and the sum collected (``amount_paid``).

**The CSV** (:func:`csv_lines`) follows the sheet's column order (``docs/COLUMN_MAPPING.md``
§2, §3): ``Store number`` (derived from column A), ``Store`` (A), ``State`` (C),
``Operator`` (D), ``Channel`` (derived from K), then one column per month (L onwards),
headed ``YYYY-MM``, with the cell text (:func:`cell_text`); written by
:class:`ledger.services.csv_export.CsvWriter`, so text cells are formula-safe.

Five queries build the grid whatever the number of stores and months: the stores with
their leases, the rent charges of the range (with a note flag), the first initiation
date of their pending payments, and the live installment plans of the range's charges
with their installments. Money is ``Decimal``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Protocol

from django.db.models import Exists, Min, OuterRef, Prefetch
from django.utils import timezone

from finance.dates import day_text, month_text
from ledger import wording
from ledger.models import (
    ZERO,
    ChargeNote,
    CollectionStatus,
    InvoiceStatus,
    Lease,
    Operator,
    Payment,
    PaymentChannel,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    Store,
)
from ledger.services import installments, search, stores
from ledger.services.csv_export import CsvWriter
from ledger.services.pnl import (
    CAPPED,
    SWAPPED,
    UNREADABLE,
    _label,
    _parse_month,
    _span,
    add_months,
    label_for_screen,
)

#: The longest range shown, in months (D2).
MAX_MONTHS = 24
#: The default range (PLAN-012d D12): this many months before the current one, the
#: current month and :data:`DEFAULT_NEXT_MONTHS` after it.
DEFAULT_PAST_MONTHS = 10
DEFAULT_NEXT_MONTHS = 1
#: The month pickers offer the current month and this many before and after it.
PICKER_MONTHS = 24

STATUS_PAID = RentChargeStatus.PAID.value
STATUS_UNPAID = RentChargeStatus.UNPAID.value
STATUS_PENDING = RentChargeStatus.PENDING.value
STATUS_PARTIAL = RentChargeStatus.PARTIAL.value
STATUS_DUE = RentChargeStatus.DUE.value
STATUS_WAIVED = RentChargeStatus.WAIVED.value
STATUS_NOT_DUE = "not_due"
#: The statuses the filter offers, in the legend's order (PLAN-012e-4 D2: the owner's order,
#: "Unpaid" in its two shades in place of "Due" and "Unpaid").
CELL_STATUSES: tuple[tuple[str, str], ...] = (
    (STATUS_PAID, "Paid"),
    (STATUS_DUE, "Unpaid, not yet overdue"),
    (STATUS_UNPAID, "Unpaid, overdue"),
    (STATUS_PENDING, "Pending"),
    (STATUS_PARTIAL, "Partly paid"),
    (STATUS_WAIVED, "Waived"),
    (STATUS_NOT_DUE, "Not yet due"),
)
NOTE_MARKER = "●"
#: The status line's words of each cell status (PLAN-012f D6); ``unpaid`` (overdue) reads
#: "Overdue N d" (:attr:`Cell.status_words`).
STATUS_WORDS: dict[str, str] = {
    STATUS_PAID: "Paid",
    STATUS_DUE: "Unpaid",
    STATUS_PENDING: "Pending",
    STATUS_PARTIAL: "Partly paid",
    STATUS_WAIVED: "Waived",
    STATUS_NOT_DUE: "Not yet due",
}

CSV_FIXED_HEADER = ("Store number", "Store", "State", "Operator", "Channel")

BAD_OPERATOR = "Unknown operator; showing every operator."
BAD_CHANNEL = "Unknown channel; showing every channel."
BAD_STATUS = "Unknown status; showing every status."
#: Longest state filter read from the query string.
STATE_MAX_LENGTH = 50


def default_range(today: date | None = None) -> tuple[date, date]:
    """The 10 months before this one, this month and the next (12 months)."""
    current = (today or timezone.localdate()).replace(day=1)
    return add_months(current, -DEFAULT_PAST_MONTHS), add_months(current, DEFAULT_NEXT_MONTHS)


@dataclass(frozen=True, slots=True)
class GridQuery:
    from_month: date
    to_month: date
    operator: uuid.UUID | None = None
    state: str = ""
    channel: str = ""
    status: str = ""
    #: The words filter (PLAN-012d D16), cleaned by :func:`ledger.services.search.clean_query`.
    q: str = ""
    #: Why a value differs from what was asked, one sentence each.
    messages: tuple[str, ...] = ()

    @property
    def months(self) -> list[date]:
        return [
            add_months(self.from_month, n) for n in range(_span(self.from_month, self.to_month))
        ]

    def as_query(self) -> dict[str, str]:
        values = {
            "from": _label(self.from_month),
            "to": _label(self.to_month),
            "operator": str(self.operator) if self.operator else "",
            "state": self.state,
            "channel": self.channel,
            "status": self.status,
            "q": self.q,
        }
        return {name: value for name, value in values.items() if value}


def _range(params: Mapping[str, str], today: date | None) -> tuple[date, date, list[str]]:
    default_from, default_to = default_range(today)
    raw_from, raw_to = params.get("from", "") or "", params.get("to", "") or ""
    messages: list[str] = []
    start = _parse_month(raw_from) if raw_from else None
    end = _parse_month(raw_to) if raw_to else None
    if (raw_from and start is None) or (raw_to and end is None):
        start, end = default_from, default_to
        messages.append(UNREADABLE.format(start=label_for_screen(start), end=label_for_screen(end)))
    if end is None:
        end = default_to
    if start is None:
        start = add_months(end, -(DEFAULT_PAST_MONTHS + DEFAULT_NEXT_MONTHS))
    if end < start:
        start, end = end, start
        messages.append(SWAPPED.format(start=label_for_screen(start), end=label_for_screen(end)))
    if _span(start, end) > MAX_MONTHS:
        start = add_months(end, -(MAX_MONTHS - 1))
        messages.append(
            CAPPED.format(
                limit=MAX_MONTHS, start=label_for_screen(start), end=label_for_screen(end)
            )
        )
    return start, end, messages


def parse_query(params: Mapping[str, str], today: date | None = None) -> GridQuery:
    """The query from query-string values; never raises (see the module docstring)."""
    start, end, messages = _range(params, today)
    operator: uuid.UUID | None = None
    raw_operator = (params.get("operator", "") or "").strip()
    if raw_operator:
        try:
            operator = uuid.UUID(raw_operator)
        except ValueError:
            messages.append(BAD_OPERATOR)
    state = (params.get("state", "") or "").strip()[:STATE_MAX_LENGTH]
    channel = (params.get("channel", "") or "").strip()
    if channel and channel not in PaymentChannel.values:
        messages.append(BAD_CHANNEL)
        channel = ""
    status = (params.get("status", "") or "").strip()
    if status and status not in dict(CELL_STATUSES):
        messages.append(BAD_STATUS)
        status = ""
    q = search.clean_query(params.get("q", ""))
    return GridQuery(start, end, operator, state, channel, status, q, tuple(messages))


class MonthRange(Protocol):
    """A query with a month range: the grid's, the P&L's or a report's."""

    @property
    def months(self) -> list[date]: ...


def picker_months(query: MonthRange, today: date | None = None) -> list[date]:
    """The months a page's From and To selects offer (``ledger/_month_select.html``: the
    grid and the reports, PLAN-012d D12 and the owner's Safari rule), first first: the
    current month and :data:`PICKER_MONTHS` before and after it, with the range shown
    added when outside them so that it can always be submitted again."""
    current = (today or timezone.localdate()).replace(day=1)
    window = {add_months(current, n) for n in range(-PICKER_MONTHS, PICKER_MONTHS + 1)}
    return sorted(window.union(query.months))


# --- Cells and rows -------------------------------------------------------------------


@dataclass(slots=True)
class Cell:
    month: date
    status: str = ""
    amount_due: Decimal = ZERO
    amount_paid: Decimal = ZERO
    #: The first charge of the cell, for the click-through; ``None`` without a charge.
    charge_id: uuid.UUID | None = None
    has_note: bool = False
    #: The first initiation date of a pending payment of the cell's charges.
    pending_from: date | None = None
    #: The arithmetic of the cell's prorated charges (PLAN-012e D2), for its title.
    proration_text: str = ""
    #: The progress of the cell's live installment plans (PLAN-012e D12).
    plan_text: str = ""
    #: The numbers of the invoices whose Rent line billed the cell's charges (PLAN-012b-b
    #: D6), in charge order.
    invoice_numbers: str = ""
    #: A month with no charge outside the store's leases (PLAN-012e-4 D3).
    outside_lease: bool = False
    #: An overdue cell's days since the earliest due date of its unpaid charges (PLAN-012f D6).
    overdue_days: int | None = None

    @property
    def text(self) -> str:
        return cell_text(self)

    @property
    def amount_text(self) -> str:
        """The block's first line: paid, "paid of due" when partly paid, else the amount due;
        nothing for "Not yet due" and a blank cell."""
        if self.status == STATUS_PAID:
            return _amount(self.amount_paid)
        if self.status == STATUS_PARTIAL:
            return f"{_amount(self.amount_paid)} of {_amount(self.amount_due)}"
        if self.status in (STATUS_UNPAID, STATUS_DUE, STATUS_PENDING, STATUS_WAIVED):
            return _amount(self.amount_due)
        return ""

    @property
    def status_words(self) -> str:
        """The status line's words: "Overdue N d" for an overdue cell, else its status's."""
        if self.status == STATUS_UNPAID:
            return "Overdue" if self.overdue_days is None else f"Overdue {self.overdue_days} d"
        return STATUS_WORDS.get(self.status, "")

    @property
    def details(self) -> str:
        """What the status line adds: the plan's progress, the day a pending payment started
        and the invoice numbers, joined by " · "."""
        since = f"from {day_text(self.pending_from)}" if self.pending_from else ""
        return " · ".join(part for part in (self.plan_text, since, self.invoice_numbers) if part)

    @property
    def label(self) -> str:
        """What a screen reader says for the cell: the month only outside the leases, else
        the block's lines in full words."""
        if self.outside_lease:
            return month_text(self.month)
        words = self.status_words
        if self.status == STATUS_UNPAID and self.overdue_days is not None:
            words = f"Overdue {self.overdue_days} day{'' if self.overdue_days == 1 else 's'}"
        parts = [part for part in (self.amount_text, words, self.details) if part]
        text = ", ".join(parts) or "no charge"
        return f"{month_text(self.month)}: {text}{', has a note' if self.has_note else ''}"


def _amount(value: Decimal) -> str:
    return wording.format_amount(value)


def cell_text(cell: Cell) -> str:
    """The cell as the CSV writes it (D2), with the numbers of the invoices that billed
    its charges (PLAN-012b-b D6)."""
    text = _status_text(cell)
    if cell.invoice_numbers and text:
        return f"{text} {cell.invoice_numbers}"
    return text


def _status_text(cell: Cell) -> str:
    if cell.plan_text:
        return cell.plan_text
    if cell.status == STATUS_PAID:
        return f"Paid {_amount(cell.amount_paid)}"
    if cell.status in (STATUS_UNPAID, STATUS_DUE):
        return f"Unpaid {_amount(cell.amount_due)}"
    if cell.status == STATUS_PENDING:
        since = f" (from {cell.pending_from.isoformat()})" if cell.pending_from else ""
        return f"Pending {_amount(cell.amount_due)}{since}"
    if cell.status == STATUS_PARTIAL:
        return f"Partly paid {_amount(cell.amount_paid)} of {_amount(cell.amount_due)}"
    if cell.status == STATUS_WAIVED:
        return "Waived"
    if cell.status == STATUS_NOT_DUE:
        return "Not yet due"
    return ""


def combined_status(statuses: list[str], amount_paid: Decimal) -> str:
    """The status of a cell from its charges' statuses (see the module docstring)."""
    counted = [status for status in statuses if status != STATUS_WAIVED]
    if not counted:
        return STATUS_WAIVED
    if all(status == STATUS_PAID for status in counted):
        return STATUS_PAID
    if STATUS_PENDING in counted:
        return STATUS_PENDING
    if amount_paid > 0 or STATUS_PARTIAL in counted:
        return STATUS_PARTIAL
    if STATUS_UNPAID in counted:
        return STATUS_UNPAID
    return STATUS_DUE


@dataclass(slots=True)
class GridRow:
    store: Store
    operator: Operator | None
    channel: str
    cells: list[Cell]

    @property
    def channel_label(self) -> str:
        return str(dict(PaymentChannel.choices).get(self.channel, self.channel))

    @property
    def store_title(self) -> str:
        """The store cell's first line (PLAN-012e-4 D4): "#1991 Istanbul"
        (:func:`ledger.services.stores.title`)."""
        return stores.title(self.store)


@dataclass(slots=True)
class ColumnTotal:
    month: date
    counts: dict[str, int] = field(default_factory=dict)
    collected: Decimal = ZERO

    def count(self, status: str) -> int:
        return self.counts.get(status, 0)


@dataclass(frozen=True, slots=True)
class Grid:
    query: GridQuery
    months: list[date]
    rows: list[GridRow]
    columns: list[ColumnTotal]


def current_lease(leases: list[Lease], today: date) -> Lease | None:
    """The lease active today, else the latest started (see the module docstring); also
    the top-bar search's store operator (:mod:`ledger.services.search`)."""

    def started(lease: Lease) -> tuple[date, object]:
        return (lease.started_on or date.min, lease.created_at)

    active = [
        lease
        for lease in leases
        if lease.collection_status == CollectionStatus.ACTIVE
        and (lease.started_on is None or lease.started_on <= today)
        and (lease.ended_on is None or lease.ended_on >= today)
    ]
    pool = active or leases
    return max(pool, key=started) if pool else None


@dataclass(frozen=True, slots=True)
class _LeaseSpan:
    """The months of a store's leases (PLAN-012e-4 D3): from the month the first started to
    the month the last ended; ``None`` is open on that side."""

    first: date | None
    last: date | None

    def holds(self, month: date) -> bool:
        return (self.first is None or month >= self.first) and (
            self.last is None or month <= self.last
        )


def _lease_span(leases: list[Lease]) -> _LeaseSpan | None:
    """The span of ``leases``, or ``None`` without a lease (every month is outside)."""
    if not leases:
        return None
    starts = [lease.started_on for lease in leases]
    ends = [lease.ended_on for lease in leases]
    first = None if None in starts else min(s for s in starts if s is not None)
    last = None if None in ends else max(e for e in ends if e is not None)
    return _LeaseSpan(
        first.replace(day=1) if first is not None else None,
        last.replace(day=1) if last is not None else None,
    )


def _stores(q: str, today: date) -> list[Store]:
    return list(
        search.filter_stores(Store.objects.all(), q, today)
        .prefetch_related(Prefetch("leases", queryset=Lease.objects.select_related("operator")))
        .order_by("store_number", "external_label", "id")
    )


def _store_order(store: Store) -> tuple[int, int, str, str]:
    number = store.store_number
    return (number is None, number or 0, store.external_label.casefold(), str(store.pk))


@dataclass(frozen=True, slots=True)
class _Charge:
    pk: uuid.UUID
    status: str
    amount_due: Decimal
    amount_paid: Decimal
    has_note: bool
    proration_text: str = ""
    plan_text: str = ""
    invoice_number: str = ""
    due_on: date | None = None


def _charges(start: date, end: date) -> dict[tuple[uuid.UUID, date], list[_Charge]]:
    """The rent charges of the range by (store, month), first charge first (one query)."""
    rows = (
        RentCharge.objects.filter(period_month__gte=start, period_month__lte=end)
        .annotate(has_note=Exists(ChargeNote.objects.filter(rent_charge=OuterRef("pk"))))
        .values_list(
            "pk",
            "lease__store",
            "period_month",
            "status",
            "amount_due",
            "amount_paid",
            "has_note",
            "proration_text",
            "billed_by_invoice__invoice_number",
            "billed_by_invoice__internal_status",
            "due_on",
        )
        .order_by("lease__store", "period_month", "lease__created_at", "seq", "pk")
    )
    plans = installments.live_rent_progress(start, end)
    found: dict[tuple[uuid.UUID, date], list[_Charge]] = defaultdict(list)
    for pk, store, month, status, due, paid, has_note, text, number, state, due_on in rows:
        charge = _Charge(
            pk,
            status,
            Decimal(str(due)),
            Decimal(str(paid)),
            bool(has_note),
            text or "",
            # A voided invoice stays named on the charge as history, not in the cell.
            invoice_number=number if number and state != InvoiceStatus.VOID else "",
            due_on=due_on,
        )
        plan = plans.get(pk)
        if plan is not None:
            # The parent's own paid amount stays; its installments pay the rest.
            paid_now = charge.amount_paid + plan.paid
            charge = replace(
                charge,
                status=STATUS_PARTIAL if paid_now > 0 else STATUS_DUE,
                amount_paid=paid_now,
                plan_text=plan.short_text,
            )
        found[(store, month)].append(charge)
    return found


def _pending_since(start: date, end: date) -> dict[tuple[uuid.UUID, date], date]:
    rows = (
        Payment.objects.filter(
            status=PaymentStatus.PENDING,
            rent_charge__period_month__gte=start,
            rent_charge__period_month__lte=end,
        )
        .values("rent_charge__lease__store", "rent_charge__period_month")
        .annotate(first=Min("initiated_on"))
        .order_by()
    )
    return {
        (row["rent_charge__lease__store"], row["rent_charge__period_month"]): row["first"]
        for row in rows
        if row["first"] is not None
    }


def build(query: GridQuery, today: date | None = None) -> Grid:
    """The grid for ``query`` (three queries; see the module docstring)."""
    day = today or timezone.localdate()
    months = query.months
    start, end = months[0], months[-1]
    by_cell = _charges(start, end)
    pending = _pending_since(start, end)
    rows: list[GridRow] = []
    columns = {month: ColumnTotal(month) for month in months}
    for store in sorted(_stores(query.q, day), key=_store_order):
        leases = list(store.leases.all())
        current = current_lease(leases, day)
        operator = current.operator if current is not None else None
        channel = current.payment_channel if current is not None else ""
        if query.operator is not None and (operator is None or operator.pk != query.operator):
            continue
        if query.state and store.state.strip().upper() != query.state.upper():
            continue
        if query.channel and channel != query.channel:
            continue
        span = _lease_span(leases)
        cells = [
            _cell(month, by_cell.get((store.pk, month), []), pending, store.pk, span, day)
            for month in months
        ]
        if query.status and not any(cell.status == query.status for cell in cells):
            continue
        rows.append(GridRow(store, operator, channel, cells))
        for cell in cells:
            column = columns[cell.month]
            if cell.status:
                column.counts[cell.status] = column.counts.get(cell.status, 0) + 1
            column.collected += cell.amount_paid
    return Grid(query, months, rows, [columns[month] for month in months])


def _cell(
    month: date,
    charges: list[_Charge],
    pending: dict[tuple[uuid.UUID, date], date],
    store_id: uuid.UUID,
    span: _LeaseSpan | None,
    today: date,
) -> Cell:
    if not charges:
        if span is None or not span.holds(month):
            return Cell(month, outside_lease=True)
        return Cell(month, STATUS_NOT_DUE if month > today.replace(day=1) else "")
    due = sum((charge.amount_due for charge in charges), ZERO)
    paid = sum((charge.amount_paid for charge in charges), ZERO)
    status = combined_status([charge.status for charge in charges], paid)
    overdue_since = [
        charge.due_on
        for charge in charges
        if charge.status == STATUS_UNPAID and charge.due_on is not None
    ]
    return Cell(
        month=month,
        status=status,
        amount_due=due,
        amount_paid=paid,
        charge_id=charges[0].pk,
        has_note=any(charge.has_note for charge in charges),
        pending_from=pending.get((store_id, month)) if status == STATUS_PENDING else None,
        proration_text="; ".join(
            charge.proration_text for charge in charges if charge.proration_text
        ),
        plan_text="; ".join(charge.plan_text for charge in charges if charge.plan_text),
        invoice_numbers=" ".join(
            charge.invoice_number for charge in charges if charge.invoice_number
        ),
        overdue_days=(
            max((today - min(overdue_since)).days, 0)
            if status == STATUS_UNPAID and overdue_since
            else None
        ),
    )


# --- CSV ------------------------------------------------------------------------------


def csv_header(months: list[date]) -> list[str]:
    return [*CSV_FIXED_HEADER, *(f"{month:%Y-%m}" for month in months)]


def csv_record(row: GridRow) -> list[object]:
    store = row.store
    return [
        store.store_number,
        store.external_label,
        store.state,
        row.operator.display_name if row.operator is not None else "",
        row.channel_label,
        *(cell.text for cell in row.cells),
    ]


def csv_lines(grid: Grid) -> Iterator[str]:
    """The grid as CSV, one line at a time (header first)."""
    return CsvWriter(csv_header(grid.months)).lines(csv_record(row) for row in grid.rows)
