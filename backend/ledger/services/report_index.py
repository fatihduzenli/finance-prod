"""The Reports index's live figures (docs/qa/PLAN-014b item 2).

Each card of ``ledger:reports:index`` shows one figure and its sub-line, computed by the
report's own service over the range that report's page uses for the same day or month, so
that a figure always equals what the report itself shows (never a parallel query):

- Aging: :func:`receivables.aging` as of today without a filter, its grand total and its
  number of store rows (the "No store" rows hold one-off charges without a lease: they count
  in the total, not as stores).
- Payments by method: :func:`reports.payments_by_method` for this month, its total.
- Bank account check: :func:`reports.unmatched_items`, how many, and the day the oldest was
  found (the page lists every open finding whatever its range, so "since" is that day).
- Revenue by month: :func:`reports.revenue_by_month` for this month, its total billed.
- Profit and loss: :func:`pnl.total` of :func:`pnl.rows` for this month, its billed margin
  (``None`` when a cost is unknown, with the unknown inputs named).
- Calendar: :func:`calendar.items` over the calendar page's NEXT 7 DAYS window
  (:func:`calendar.next_days_window`), how many and the first of
  :func:`calendar.next_days`.

The query count does not depend on the data: each service's is fixed. The view runs
:func:`figures` inside the reports' statement timeout (``ledger.views.reports.bounded``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finance.dates import day_short_text
from ledger.services import calendar, pnl, receivables, reports

NOTHING_SCHEDULED = "nothing scheduled"


@dataclass(frozen=True, slots=True)
class ReportFigures:
    #: The first day of this month: the month of the ranged figures.
    month: date
    aging_total: Decimal
    aging_stores: int
    collected_month: Decimal
    unmatched: int
    #: The day the oldest open finding was found; ``None`` when there is none.
    unmatched_since: date | None
    billed_month: Decimal
    #: ``None`` when a cost of the month is unknown (:attr:`margin_missing` says which).
    margin_month: Decimal | None
    #: The unknown inputs, comma-separated ("landlord rent, fee costs"); empty when known.
    margin_missing: str
    calendar_week: int
    #: "<day> · <title>" of the next item, or :data:`NOTHING_SCHEDULED`.
    calendar_next: str


def figures(today: date) -> ReportFigures:
    month = today.replace(day=1)
    this_month = reports.ReportRange(month, month)
    aging = receivables.aging(today, receivables.AgingFilter())
    margin = pnl.total(pnl.rows(pnl.PnlQuery(month, month)))
    found = calendar.items(*calendar.next_days_window(today))
    upcoming = calendar.next_days(found, today)
    first = upcoming[0].items[0] if upcoming else None
    upcoming_text = (
        f"{day_short_text(first.on)} · {first.title}" if first is not None else NOTHING_SCHEDULED
    )
    unmatched = reports.unmatched_items()
    return ReportFigures(
        month=month,
        aging_total=aging.totals.total,
        aging_stores=sum(1 for row in aging.rows if row.store_id is not None),
        collected_month=reports.payments_by_method(this_month).total.total,
        unmatched=len(unmatched),
        unmatched_since=unmatched[0].found_on if unmatched else None,
        billed_month=reports.revenue_by_month(this_month).total.total_billed,
        margin_month=margin.margin_billed,
        margin_missing=", ".join(margin.unknown),
        calendar_week=sum(group.count for group in upcoming),
        calendar_next=upcoming_text,
    )
