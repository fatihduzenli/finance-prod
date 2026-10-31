"""Business days for bank debits (docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md
F2): Monday to Friday, the US Federal Reserve holidays excluded.

:data:`FEDERAL_RESERVE_HOLIDAYS` is the Federal Reserve Banks' holiday schedule for 2026 to
2028 (source: Federal Reserve Financial Services, "Holiday Schedules",
frbservices.org/about/holiday-schedules, read 2026-10-30), the days the banks are closed: a
holiday on a Sunday is kept on the Monday after; one on a Saturday is not moved (the banks
are open the Friday before), so it is not listed. Outside those years only weekends are
skipped; the table must be extended before 2029.

The automatic re-debit of a returned debit goes on the third business day after the return's
notice (:func:`add`), and staff have until the second to stop it
(:mod:`ledger.services.nsf`).
"""

from __future__ import annotations

from datetime import date, timedelta

FEDERAL_RESERVE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # Birthday of Martin Luther King, Jr.
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth National Independence Day
        # Independence Day, Saturday July 4: not moved.
        date(2026, 9, 7),  # Labor Day
        date(2026, 10, 12),  # Columbus Day
        date(2026, 11, 11),  # Veterans Day
        date(2026, 11, 26),  # Thanksgiving Day
        date(2026, 12, 25),  # Christmas Day
        # 2027
        date(2027, 1, 1),  # New Year's Day
        date(2027, 1, 18),  # Birthday of Martin Luther King, Jr.
        date(2027, 2, 15),  # Washington's Birthday
        date(2027, 5, 31),  # Memorial Day
        # Juneteenth, Saturday June 19: not moved.
        date(2027, 7, 5),  # Independence Day (Sunday July 4, kept on the Monday)
        date(2027, 9, 6),  # Labor Day
        date(2027, 10, 11),  # Columbus Day
        date(2027, 11, 11),  # Veterans Day
        date(2027, 11, 25),  # Thanksgiving Day
        # Christmas Day, Saturday December 25: not moved.
        # 2028 (New Year's Day, Saturday January 1: not moved)
        date(2028, 1, 17),  # Birthday of Martin Luther King, Jr.
        date(2028, 2, 21),  # Washington's Birthday
        date(2028, 5, 29),  # Memorial Day
        date(2028, 6, 19),  # Juneteenth National Independence Day
        date(2028, 7, 4),  # Independence Day
        date(2028, 9, 4),  # Labor Day
        date(2028, 10, 9),  # Columbus Day
        # Veterans Day, Saturday November 11: not moved.
        date(2028, 11, 23),  # Thanksgiving Day
        date(2028, 12, 25),  # Christmas Day
    }
)


def is_business_day(day: date) -> bool:
    """Monday to Friday and not a Federal Reserve holiday."""
    return day.weekday() < 5 and day not in FEDERAL_RESERVE_HOLIDAYS


def add(day: date, count: int) -> date:
    """The ``count``-th business day after ``day`` (``day`` itself never counts; ``count``
    0 is ``day``). ``ValueError`` for a negative ``count``."""
    if count < 0:
        raise ValueError("count must not be negative")
    found = day
    while count > 0:
        found += timedelta(days=1)
        if is_business_day(found):
            count -= 1
    return found
