"""Rent for part of a month (docs/qa/PLAN-012e-lease-end-transfer-installments.md D1).

:func:`prorated` charges the days used: ``rent × days_used ÷ days_in_month``, rounded to
the cent once, half up (the owner's example: 13 to 31 October of $2,000.00 is
$1,225.81). The first and the last day both count. A span from the 1st to the month's
last day is the full rent, with no text.

The text shows the arithmetic, as the first rent charge and the final statement print
it: ``13–31 Oct, 19 of 31 days of $2,000.00 = $1,225.81`` (an en dash between the days,
the month's short name, thousands separators). A single day reads ``13 Oct``.

Pure: no database, no clock.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ledger.wording import format_amount

CENT = Decimal("0.01")
EN_DASH = "–"
MONTH_NOT_FIRST = "The month must be given as its first day."
DAYS_OUT_OF_RANGE = "The days must lie in the month, the first not after the last."
RENT_NOT_POSITIVE = "The rent must be more than zero."


@dataclass(frozen=True, slots=True)
class Prorated:
    days_used: int
    days_in_month: int
    amount: Decimal
    #: The arithmetic as printed; ``""`` for a full month.
    text: str

    @property
    def full(self) -> bool:
        return self.days_used == self.days_in_month


def days_in(month: date) -> int:
    return calendar.monthrange(month.year, month.month)[1]


def last_day(month: date) -> date:
    return month.replace(day=days_in(month))


def money(amount: Decimal) -> str:
    """``$1,234.56`` (:func:`ledger.wording.format_amount`)."""
    return format_amount(amount)


def span_text(month: date, first_day: int, last: int) -> str:
    name = f"{month:%b}"
    if first_day == last:
        return f"{first_day} {name}"
    return f"{first_day}{EN_DASH}{last} {name}"


def prorated(rent: Decimal, month: date, first_day: int, last_day: int) -> Prorated:
    """The rent of ``month`` (its first day) for the days ``first_day`` to ``last_day``,
    both counted (D1). ``ValueError`` for a month not given as its 1st, days outside the
    month or in the wrong order, or a rent that is not above zero."""
    if month.day != 1:
        raise ValueError(MONTH_NOT_FIRST)
    total = days_in(month)
    if not 1 <= first_day <= last_day <= total:
        raise ValueError(DAYS_OUT_OF_RANGE)
    if not isinstance(rent, Decimal) or not rent.is_finite() or rent <= 0:
        raise ValueError(RENT_NOT_POSITIVE)
    used = last_day - first_day + 1
    if used == total:
        return Prorated(used, total, rent, "")
    amount = (rent * used / total).quantize(CENT, rounding=ROUND_HALF_UP)
    text = (
        f"{span_text(month, first_day, last_day)}, {used} of {total} days of "
        f"{money(rent)} = {money(amount)}"
    )
    return Prorated(used, total, amount, text)
