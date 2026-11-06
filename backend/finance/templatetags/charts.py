"""The dashboard's Collections chart as inline SVG (docs/qa/PLAN-015-design-v2.md §4.2; the
owner's mockup docs/design/dashboard-v2-dark-laptop.png): no chart library.

``{% collection_bars bars %}`` draws one bar per month (or per year): ``bars`` are objects
with ``label`` (under the bar: "Nov", "2026"), ``title`` (the tooltip's heading: "November
2026"), ``collected`` and ``due`` (``Decimal``) and ``current`` (the current month or year,
drawn in ``--green``; the others in ``--chart-income``). The y axis is in dollars, written in
thousands ("$120k") once a step reaches $1,000, with a grid line per step; the scale's top is
the first step of 1, 2, 2.5 or 5 times a power of ten that holds the largest figure in
:data:`TICKS` steps. Each bar is a ``<g class="collection-bar">`` with its month and figures
in ``data-label``, ``data-collected`` and ``data-due`` (the hover tooltip of
ledger/static/ledger/dashboard.js reads them) and a ``<title>`` with the same words, the
tooltip without the script. The ``<svg>`` is ``role="img"`` with a sentence as its
``aria-label``.

``{% chart_key key %}`` draws the 8 px key of a chart colour (``collected``, ``current``) for
the legend.

Every colour is a ``var(--…)`` reference to a token of ``tokens.css`` (DESIGN-SPEC §7.3, both
themes); no literal colour is written. Coordinates are computed in ``Decimal`` and written
with one decimal; money is written by :func:`ledger.wording.format_amount`. Every text is
escaped.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe

from ledger.wording import format_amount

register = template.Library()

#: The chart colours are tokens (tokens.css, DESIGN-SPEC §7.3), written verbatim into the SVG
#: attributes so each theme resolves its own value.
COLLECTED_FILL = "var(--chart-income)"
CURRENT_FILL = "var(--green)"
AXIS_STROKE = "var(--chart-axis)"
LABEL_FILL = "var(--chart-label)"
#: Every chart colour by its key (``{% chart_key %}``).
KEY_COLOURS: dict[str, str] = {"collected": COLLECTED_FILL, "current": CURRENT_FILL}

#: The chart's frame, in viewBox units.
WIDTH = Decimal(720)
AXIS_WIDTH = Decimal(52)
PLOT_TOP = Decimal(10)
PLOT_BOTTOM = Decimal(200)
LABEL_Y = Decimal(222)
HEIGHT = Decimal(232)
#: A bar's share of its slot, and the widest bar.
BAR_SHARE = Decimal("0.62")
MAX_BAR_WIDTH = Decimal(56)
BAR_RADIUS = Decimal(4)
#: The grid's steps above zero.
TICKS = 4
_NICE = (Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5), Decimal(10))
_THOUSAND = Decimal(1000)
_TENTH = Decimal("0.1")


def _coord(value: Decimal) -> str:
    return str(value.quantize(_TENTH, rounding=ROUND_HALF_UP))


def _amount(value: object) -> Decimal:
    return value if isinstance(value, Decimal) and value.is_finite() and value > 0 else Decimal(0)


def nice_step(high: Decimal) -> Decimal:
    """The grid's step: the first of 1, 2, 2.5, 5 times a power of ten with :data:`TICKS`
    steps holding ``high``; $1,000 for an empty chart."""
    if high <= 0:
        return _THOUSAND
    raw = high / TICKS
    magnitude = Decimal(10) ** (raw.adjusted())
    for factor in _NICE:
        if factor * magnitude >= raw:
            return factor * magnitude
    return _NICE[-1] * magnitude  # pragma: no cover - 10 x magnitude always holds raw


def tick_label(value: Decimal, step: Decimal) -> str:
    """``$120k`` once ``step`` reaches $1,000, else ``$250``."""
    if step >= _THOUSAND:
        thousands = (value / _THOUSAND).normalize()
        return f"${thousands:f}k"
    return f"${value.normalize():f}"


@register.simple_tag
def collection_bars(rows: Iterable[Any]) -> SafeString:
    """The Collections chart (see the module docstring)."""
    items = list(rows)
    if not items:
        return mark_safe("")  # noqa: S308 - the empty string
    high = max(_amount(row.collected) for row in items)
    step = nice_step(high)
    top = step * TICKS
    height = PLOT_BOTTOM - PLOT_TOP
    scale = height / top
    slot = (WIDTH - AXIS_WIDTH) / len(items)
    bar_width = min(slot * BAR_SHARE, MAX_BAR_WIDTH)
    grid = format_html_join(
        "",
        '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" />'
        '<text class="axis-label" x="{}" y="{}" text-anchor="end" font-size="11" fill="{}">'
        "{}</text>",
        (
            (
                _coord(AXIS_WIDTH),
                _coord(PLOT_BOTTOM - step * n * scale),
                _coord(WIDTH),
                _coord(PLOT_BOTTOM - step * n * scale),
                AXIS_STROKE,
                _coord(AXIS_WIDTH - 8),
                _coord(PLOT_BOTTOM - step * n * scale + 4),
                LABEL_FILL,
                tick_label(step * n, step),
            )
            for n in range(TICKS + 1)
        ),
    )
    groups = []
    for index, row in enumerate(items):
        collected = _amount(row.collected)
        due = _amount(row.due)
        centre = AXIS_WIDTH + slot * index + slot / 2
        bar_height = collected * scale
        groups.append(
            format_html(
                '<g class="collection-bar{}" data-label="{}" data-collected="{}" data-due="{}">'
                '<rect x="{}" y="{}" width="{}" height="{}" rx="{}" fill="{}" />'
                "<title>{}: collected {}, due {}</title>"
                '<text class="bar-label" x="{}" y="{}" text-anchor="middle" font-size="11" '
                'fill="{}">{}</text></g>',
                " is-current" if row.current else "",
                row.title,
                format_amount(collected),
                format_amount(due),
                _coord(centre - bar_width / 2),
                _coord(PLOT_BOTTOM - bar_height),
                _coord(bar_width),
                _coord(bar_height),
                _coord(min(BAR_RADIUS, bar_height / 2)),
                CURRENT_FILL if row.current else COLLECTED_FILL,
                row.title,
                format_amount(collected),
                format_amount(due),
                _coord(centre),
                _coord(LABEL_Y),
                LABEL_FILL,
                row.label,
            )
        )
    current = next((row.title for row in items if row.current), "")
    sentence = f"Collected from {items[0].title} to {items[-1].title}" + (
        f"; {current} is the current one." if current else "."
    )
    return format_html(
        '<svg class="collections-svg" viewBox="0 0 {} {}" role="img" aria-label="{}">{}{}</svg>',
        _coord(WIDTH),
        _coord(HEIGHT),
        sentence,
        grid,
        format_html_join("", "{}", ((group,) for group in groups)),
    )


@register.simple_tag
def chart_key(key: str) -> SafeString:
    """The 8 px key of a chart colour; a template mistake (an unknown key) raises
    ``ValueError``."""
    if key not in KEY_COLOURS:
        raise ValueError(f"unknown chart key {key!r}")
    return format_html(
        '<svg class="chart-key" width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">'
        '<rect width="8" height="8" rx="2" fill="{}" /></svg>',
        KEY_COLOURS[key],
    )
