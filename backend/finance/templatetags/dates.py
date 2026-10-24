"""``{{ value|day }}`` and its siblings: dates as a person reads them on a page (DESIGN-SPEC §5
"Dates on screen"; docs/qa/PLAN-012f-a-design-follow-up.md B4 and §4).

``|day`` ``Nov 1, 2026``; ``|day_short`` ``Nov 1``; ``|day_long`` ``Tuesday, Oct 20, 2026``;
``|month`` ``Oct 2026``; ``|moment`` ``Nov 1, 2026 · 14:02``. Each filter is the matching
helper of :mod:`finance.dates` (a ``datetime`` is read in New York, a naive one as UTC); ``None``
or anything that is not a ``date``/``datetime`` renders as ``""`` (never raises on a page), so
``|day|default:"—"`` shows a dash. ISO stays where the value is a machine value (a field's
``value``, a ``data-*`` attribute, ``<time datetime>``, a URL): those keep ``|date:"Y-m-d"``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from django import template

from finance import dates

register = template.Library()


def _safe(helper: Callable[[date | datetime], str], value: object) -> str:
    """``helper(value)``, or ``""`` for ``None``, a wrong type or a ``date`` given to ``moment``."""
    if not isinstance(value, date):
        return ""
    try:
        return helper(value)
    except TypeError:
        return ""


@register.filter(name="day")
def day(value: object) -> str:
    """``Nov 1, 2026``, or ``""``."""
    return _safe(dates.day_text, value)


@register.filter(name="day_short")
def day_short(value: object) -> str:
    """``Nov 1``, or ``""``."""
    return _safe(dates.day_short_text, value)


@register.filter(name="day_long")
def day_long(value: object) -> str:
    """``Tuesday, Oct 20, 2026``, or ``""``."""
    return _safe(dates.day_long_text, value)


@register.filter(name="month")
def month(value: object) -> str:
    """``Oct 2026``, or ``""``."""
    return _safe(dates.month_text, value)


@register.filter(name="moment")
def moment(value: object) -> str:
    """``Nov 1, 2026 · 14:02`` (New York), or ``""``."""
    return _safe(dates.moment_text, value)
