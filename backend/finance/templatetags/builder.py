"""``{{ row_form|line_total }}``: one invoice builder row's line total (PLAN-012f-a D5).

The Lines card's Total column and the SUMMARY card's lines show each kept row's amount as the
page opens; ``ledger/invoice_lines.js`` keeps them in step as the row is typed. The amount is
:func:`ledger.services.invoice_builder.shown_line_total` of the row's quantity and unit
amount, the rule the view's total sums, so the lines always add up to the total under them.
The template leaves a removed row's amount out.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django import template

from ledger.services.invoice_builder import shown_line_total

register = template.Library()


@register.filter(name="line_total")
def line_total(form: Any) -> Decimal:
    """The line total of one row of the builder's formset."""
    return shown_line_total(form["quantity"].value(), form["unit_amount"].value())
