"""``{{ amount|money }}``: an amount as ``$2,000.00`` (docs/qa/PLAN-012c-design-pass.md §4.8).

The filter is :func:`ledger.wording.format_amount` for a template: a ``Decimal``, an ``int``,
a ``float`` or a numeric string is written with a thousands separator, two decimals and a
leading minus for a negative (``-$2,000.00``); ``None``, an empty value and anything that is
not a finite number render as the empty string, so ``|money|default:"—"`` shows a dash; so
does an amount too large to be one (more than :data:`MAX_INTEGER_DIGITS` digits before the
point), which would otherwise be written out digit by digit. The empty string is the only
fallback, so nothing typed reaches the page through this filter. Python code imports
:func:`format_amount` from here or from ``ledger.wording``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template

from ledger.wording import format_amount

__all__ = ["MAX_INTEGER_DIGITS", "format_amount", "money"]

register = template.Library()

#: No amount the ledger stores comes near this; a longer one is not an amount.
MAX_INTEGER_DIGITS = 30


@register.filter(name="money")
def money(value: object) -> str:
    """``value`` as ``$2,000.00``, or ``""`` when it is not a finite number of a sane size."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float | str):
        return ""
    try:
        amount = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError):  # ValueError: an int too long for str()
        return ""
    if not amount.is_finite() or (amount and amount.adjusted() >= MAX_INTEGER_DIGITS):
        return ""
    return format_amount(amount)
