"""Dates as a person reads them on a page (DESIGN-SPEC §5 "Dates on screen"; docs/qa/
PLAN-012f-a-design-follow-up.md B4 and §4).

The owner's rule (2026-10-23): a day reads ``Nov 1, 2026``, a month ``Oct 2026``, a time
``14:02``; never ISO on a page. ISO (``2026-11-01``) stays in CSV and spreadsheet exports, the
API, audit rows, e-mails to systems, form field values and URLs. These helpers are for the
screen only; Python that builds a sentence for a page calls them, and the template filters of
:mod:`finance.templatetags.dates` are written over them.

- :func:`day_text` ``Nov 1, 2026``; :func:`day_short_text` ``Nov 1`` (only where the text
  around it names the year); :func:`day_long_text` ``Tuesday, Oct 20, 2026``;
  :func:`month_text` ``Oct 2026``; :func:`month_long_text` ``October 2026`` (a period's
  name, PLAN-014 A3); :func:`moment_text` ``Nov 1, 2026 · 14:02``.
- A ``datetime`` is read in New York (PLAN-011b D1: storage is UTC, every day the business
  names is a New York day); a naive ``datetime`` is taken to be UTC, as the pages already do.
  A ``date`` is used as it is.
- Anything but a ``date`` or a ``datetime`` (``None``, a ``str``) raises :class:`TypeError`:
  the caller decides what an absent date reads as. The filters render it as ``""``.

The month and weekday names are written out here, not taken from the locale, so the text is
the same on every machine.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from finance.env_guard import PROJECT_TIME_ZONE

__all__ = [
    "NEW_YORK",
    "day_long_text",
    "day_short_text",
    "day_text",
    "moment_text",
    "month_long_text",
    "month_text",
]

NEW_YORK = ZoneInfo(PROJECT_TIME_ZONE)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS_LONG = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _new_york(value: datetime) -> datetime:
    """``value`` in New York; a naive ``value`` is UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(NEW_YORK)


def _day(value: object) -> date:
    """The day of ``value``: a ``date`` as it is, a ``datetime``'s New York day."""
    if isinstance(value, datetime):
        return _new_york(value).date()
    if isinstance(value, date):
        return value
    raise TypeError(f"expected a date or a datetime, got {type(value).__name__}")


def day_text(value: date | datetime) -> str:
    """``Nov 1, 2026``."""
    day = _day(value)
    return f"{_MONTHS[day.month - 1]} {day.day}, {day.year}"


def day_short_text(value: date | datetime) -> str:
    """``Nov 1``: the year dropped, for text that names it already."""
    day = _day(value)
    return f"{_MONTHS[day.month - 1]} {day.day}"


def day_long_text(value: date | datetime) -> str:
    """``Tuesday, Oct 20, 2026``: the day with its weekday."""
    day = _day(value)
    return f"{_WEEKDAYS[day.weekday()]}, {day_text(day)}"


def month_text(value: date | datetime) -> str:
    """``Oct 2026``: the month of a day (a ``datetime``'s New York month)."""
    day = _day(value)
    return f"{_MONTHS[day.month - 1]} {day.year}"


def month_long_text(value: date | datetime) -> str:
    """``October 2026``: a month named in full, as a card's period reads it (PLAN-014 A3)."""
    day = _day(value)
    return f"{_MONTHS_LONG[day.month - 1]} {day.year}"


def month_short_text(value: date | datetime) -> str:
    """``Oct``: the year dropped, for text that names it already (a short chart's labels)."""
    return _MONTHS[_day(value).month - 1]


def moment_text(value: date | datetime) -> str:
    """``Nov 1, 2026 · 14:02``: a ``datetime`` in New York, 24-hour clock.

    A ``date`` has no time: it is a :class:`TypeError` like any other non-``datetime``.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    local = _new_york(value)
    return f"{day_text(local.date())} · {local:%H:%M}"
