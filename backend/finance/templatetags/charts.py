"""The dashboard's charts as inline SVG (docs/qa/PLAN-012f-b-staging-review.md D3;
docs/design/DESIGN-SPEC.md §4a): no chart library and no script.

``{% bars rows %}`` draws the Cashflow card's months: ``rows`` are objects with ``month``,
``income``, ``expenses`` (``None``: unknown, no bar), ``net`` (``None``: no point) and
``in_range`` (``False``: a month before the range, its bars faded, its label kept legible).
Each month is a pair of bars, Income and Expenses, each with a ``<title>`` holding its figure;
the frame is the same for any number of months and the bars widen with their month's share
of it (twelve months: :data:`BAR_WIDTH`; fewer: wider, at most :data:`MAX_BAR_WIDTH`;
PLAN-012g C2),
and the net is a line through the months it is known in. The scale
holds zero and every figure, so a negative month reads below the zero line. The ``<svg>`` is
``role="img"`` with a sentence as its ``aria-label``. Every month's label is written, as a
``<text class="bar-label">``; the labels kept where the chart is narrow (a phone) are every
third month counting back from the last, the first month of the range and today's month
(``{% bars rows today %}``); the others carry ``bar-label-minor``, which the phone's style
hides (:func:`major_months`). A bar's ``<title>`` names its month either way.

``{% donut parts total %}`` draws the Payment status card's ring: ``parts`` are objects
with ``key`` (``overdue``, ``paid`` or ``upcoming``), ``label`` and ``amount``; each part
with an amount above zero is an arc (``stroke-dasharray`` over a ``pathLength`` of 100) in
its colour, and the total is written in the middle through :func:`format_amount` over the
word "Total"; ``{% donut parts total count %}`` writes the charges' count over the word
"charges" instead (the Payment status card, PLAN-012g C4). A zero total draws the empty ring.

``{% chart_key key %}`` draws the 8 px key of a chart colour (``income``, ``expenses``,
``net``, ``overdue``, ``paid``, ``upcoming``) for a legend or a box beside its word.

Every colour is a ``var(--chart-*)`` reference to a token of ``tokens.css`` (PLAN-012g A4;
DESIGN-SPEC §7.3, both themes); no literal colour is written.

Coordinates are computed in ``Decimal`` and written with one decimal; money is written by
:func:`ledger.wording.format_amount`, months by :func:`finance.dates.month_text`. Every text
is escaped.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe

from finance.dates import month_text
from ledger.wording import format_amount

register = template.Library()

#: The chart colours are tokens (tokens.css, DESIGN-SPEC §7.3; PLAN-012g A4), written verbatim
#: into the SVG attributes so each theme resolves its own value.
INCOME_FILL = "var(--chart-income)"
EXPENSES_FILL = "var(--chart-expenses)"
NET_STROKE = "var(--chart-net)"
AXIS_STROKE = "var(--chart-axis)"
LABEL_FILL = "var(--chart-label)"
#: The donut's colours by part.
DONUT_COLOURS: dict[str, str] = {
    "overdue": "var(--chart-overdue)",
    "paid": "var(--chart-paid)",
    "upcoming": "var(--chart-upcoming)",
}
RING_EMPTY = "var(--chart-ring-empty)"
#: Every chart colour by its key (``{% chart_key %}``).
KEY_COLOURS: dict[str, str] = {
    "income": INCOME_FILL,
    "expenses": EXPENSES_FILL,
    "net": NET_STROKE,
    **DONUT_COLOURS,
}

#: The chart's frame, in viewBox units.
WIDTH = Decimal(720)
PLOT_TOP = Decimal(8)
PLOT_HEIGHT = Decimal(172)
LABEL_Y = Decimal(200)
HEIGHT = Decimal(208)
BAR_WIDTH = Decimal(16)
BAR_GAP = Decimal(3)
#: The months :data:`BAR_WIDTH` and :data:`BAR_GAP` are drawn for; fewer months widen both.
BAR_MONTHS = 12
MAX_BAR_WIDTH = Decimal(48)
OUTSIDE_OPACITY = "0.4"
#: On a narrow chart a label every this many months, counted back from the last (A3-1).
LABEL_EVERY = 3

_TENTH = Decimal("0.1")


def _coord(value: Decimal) -> str:
    return str(value.quantize(_TENTH, rounding=ROUND_HALF_UP))


def _amount(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) and value.is_finite() else None


def _figures(row: Any) -> list[Decimal]:
    found = [_amount(row.income), _amount(row.expenses), _amount(row.net)]
    return [value for value in found if value is not None]


def _bar(
    x: Decimal, width: Decimal, value: Decimal, zero: Decimal, scale: Decimal, fill: str, title: str
) -> str:
    top = zero - value * scale
    y, height = (top, zero - top) if value >= 0 else (zero, top - zero)
    return format_html(
        '<rect x="{}" y="{}" width="{}" height="{}" fill="{}"><title>{}</title></rect>',
        _coord(x),
        _coord(y),
        _coord(width),
        _coord(height),
        fill,
        title,
    )


def major_months(items: list[Any], today: date | None = None) -> set[int]:
    """The indexes of the months whose label stays on a narrow chart: the first month of
    the range (the first row ``in_range``) and today's month, when it is drawn, then every
    :data:`LABEL_EVERY`th month counted back from the last that is not beside one of those
    (two labels side by side would overlap)."""
    kept: set[int] = set()
    first = next((index for index, row in enumerate(items) if row.in_range), None)
    if first is not None:
        kept.add(first)
    if today is not None:
        kept.update(
            index
            for index, row in enumerate(items)
            if (row.month.year, row.month.month) == (today.year, today.month)
        )
    forced = set(kept)
    for index in range(len(items) - 1, -1, -LABEL_EVERY):
        if all(abs(index - other) > 1 for other in forced):
            kept.add(index)
    return kept


@register.simple_tag
def bars(rows: Iterable[Any], today: date | None = None) -> SafeString:
    """The Cashflow chart (see the module docstring)."""
    items = list(rows)
    if not items:
        return mark_safe("")  # noqa: S308 - the empty string
    major = major_months(items, today)
    values = [value for row in items for value in _figures(row)]
    high = max([Decimal(0), *values])
    low = min([Decimal(0), *values])
    spread = high - low or Decimal(1)
    scale = PLOT_HEIGHT / spread
    zero = PLOT_TOP + high * scale
    step = WIDTH / len(items)
    widen = min(step * BAR_MONTHS / WIDTH, MAX_BAR_WIDTH / BAR_WIDTH)
    bar_width, bar_gap = BAR_WIDTH * widen, BAR_GAP * widen
    groups: list[str] = []
    runs: list[list[str]] = [[]]
    for index, row in enumerate(items):
        centre = step * index + step / 2
        label = month_text(row.month)
        income = _amount(row.income) or Decimal(0)
        expenses = _amount(row.expenses)
        parts = [
            _bar(
                centre - bar_width - bar_gap / 2,
                bar_width,
                income,
                zero,
                scale,
                INCOME_FILL,
                f"Income {label}: {format_amount(income)}",
            )
        ]
        if expenses is not None:
            parts.append(
                _bar(
                    centre + bar_gap / 2,
                    bar_width,
                    expenses,
                    zero,
                    scale,
                    EXPENSES_FILL,
                    f"Expenses {label}: {format_amount(expenses)}",
                )
            )
        groups.append(
            format_html(
                '<g class="bar-month"><g class="bar-pair"{}>{}</g><text class="{}" x="{}" y="{}" '
                'text-anchor="middle" font-size="11" fill="{}">{}</text></g>',
                format_html(' opacity="{}"', OUTSIDE_OPACITY) if not row.in_range else "",
                format_html_join("", "{}", ((part,) for part in parts)),
                "bar-label" if index in major else "bar-label bar-label-minor",
                _coord(centre),
                _coord(LABEL_Y),
                LABEL_FILL,
                label,
            )
        )
        net = _amount(row.net)
        if net is None:
            runs.append([])
        else:
            runs[-1].append(f"{_coord(centre)},{_coord(zero - net * scale)}")
    lines = format_html_join(
        "",
        '<polyline points="{}" fill="none" stroke="{}" stroke-width="2" />',
        ((" ".join(run), NET_STROKE) for run in runs if run),
    )
    sentence = (
        f"Income and expenses by month from {month_text(items[0].month)} to "
        f"{month_text(items[-1].month)}, with the net as a line."
    )
    return format_html(
        '<svg class="bars-chart" viewBox="0 0 {} {}" role="img" aria-label="{}">'
        '<line x1="0" y1="{}" x2="{}" y2="{}" stroke="{}" />{}{}</svg>',
        _coord(WIDTH),
        _coord(HEIGHT),
        sentence,
        _coord(zero),
        _coord(WIDTH),
        _coord(zero),
        AXIS_STROKE,
        format_html_join("", "{}", ((group,) for group in groups)),
        lines,
    )


#: The donut's ring, in viewBox units (a 120 square).
RING_RADIUS = 48
RING_WIDTH = 14


@register.simple_tag
def donut(parts: Iterable[Any], total: object, count: int | None = None) -> SafeString:
    """The Payment status ring with the total, or the count, in the middle (see the module
    docstring)."""
    shown: list[tuple[str, str, Decimal]] = []
    for part in parts:
        value = _amount(part.amount)
        if value is not None and value > 0 and part.key in DONUT_COLOURS:
            shown.append((str(part.key), str(part.label), value))
    whole = sum((amount for _, _, amount in shown), Decimal(0))
    arcs: list[tuple[str, str, str, str, str, str, str]] = []
    offset = Decimal(0)
    for key, label, amount in shown:
        length = amount * 100 / whole
        arcs.append(
            (
                str(RING_RADIUS),
                DONUT_COLOURS[key],
                str(RING_WIDTH),
                f"{_coord(length)} {_coord(Decimal(100) - length)}",
                _coord(-offset),
                label,
                format_amount(amount),
            )
        )
        offset += length
    circles = format_html_join(
        "",
        '<circle cx="60" cy="60" r="{}" fill="none" stroke="{}" stroke-width="{}" '
        'pathLength="100" stroke-dasharray="{}" stroke-dashoffset="{}" '
        'transform="rotate(-90 60 60)"><title>{}: {}</title></circle>',
        arcs,
    )
    described = ", ".join(f"{label} {format_amount(amount)}" for _, label, amount in shown)
    total_text = format_amount(_amount(total) or Decimal(0))
    middle, word = (
        (total_text, "Total") if count is None else (str(count), "charge" if count == 1 else "charges")
    )
    return format_html(
        '<div class="donut"><svg viewBox="0 0 120 120" role="img" aria-label="{}">'
        '<circle cx="60" cy="60" r="{}" fill="none" stroke="{}" stroke-width="{}" />{}</svg>'
        '<span class="donut-total"><span class="donut-amount">{}</span>'
        '<span class="donut-label">{}</span></span></div>',
        f"Total {total_text}" + (f": {described}" if described else ""),
        RING_RADIUS,
        RING_EMPTY,
        RING_WIDTH,
        circles,
        middle,
        word,
    )


@register.simple_tag
def chart_key(key: str) -> SafeString:
    """The 8 px key of a chart colour (a short line for ``net``); a template mistake (an
    unknown key) raises ``ValueError``."""
    if key not in KEY_COLOURS:
        raise ValueError(f"unknown chart key {key!r}")
    if key == "net":
        return format_html(
            '<svg class="chart-key" width="12" height="8" viewBox="0 0 12 8" aria-hidden="true">'
            '<line x1="0" y1="4" x2="12" y2="4" stroke="{}" stroke-width="2" /></svg>',
            KEY_COLOURS[key],
        )
    return format_html(
        '<svg class="chart-key" width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">'
        '<rect width="8" height="8" rx="2" fill="{}" /></svg>',
        KEY_COLOURS[key],
    )
