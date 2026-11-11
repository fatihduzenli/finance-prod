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
ledger/static/ledger/charts.js reads them) and a ``<title>`` with the same words, the
tooltip without the script. The ``<svg>`` is ``role="img"`` with a sentence as its
``aria-label``.

``{% pnl_bars months %}`` (PLAN-015 Batch 4) draws the Profit and loss chart on the same
frame (:class:`_Frame`: the viewBox, the axis and its ticks, the bars' footprint, radius and
labels): a Collected and a Costs bar per month (see :func:`pnl_bars`).

``{% chart_key key %}`` draws the 8 px key of a chart colour (``collected``, ``current``,
``costs``) for the legend.

Every colour is a ``var(--…)`` reference to a token of ``tokens.css`` (DESIGN-SPEC §7.3, both
themes); no literal colour is written. Coordinates are computed in ``Decimal`` and written
with one decimal; money is written by :func:`ledger.wording.format_amount`. Every text is
escaped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
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
COSTS_FILL = "var(--chart-expenses)"
AXIS_STROKE = "var(--chart-axis)"
LABEL_FILL = "var(--chart-label)"
#: Every chart colour by its key (``{% chart_key %}``).
KEY_COLOURS: dict[str, str] = {
    "collected": COLLECTED_FILL,
    "current": CURRENT_FILL,
    "costs": COSTS_FILL,
}

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
#: The gap between a P&L month's two bars.
PAIR_GAP = Decimal(3)
#: The room a slot's label needs ("Sep" at 11 px); a narrower slot labels every n-th slot.
LABEL_ROOM = Decimal(28)
#: A P&L figure that is not known.
UNKNOWN = "unknown"
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
        return "$" + f"{thousands:f}k"
    return "$" + f"{value.normalize():f}"


@dataclass(frozen=True, slots=True)
class _Frame:
    """The shared geometry of a bar chart of ``count`` slots whose largest figure is
    ``high``: the scale (viewBox units per dollar), each slot's width and its bars' footprint
    (:data:`BAR_SHARE` of the slot, at most :data:`MAX_BAR_WIDTH`)."""

    step: Decimal
    scale: Decimal
    slot: Decimal
    bar_width: Decimal

    @classmethod
    def of(cls, high: Decimal, count: int) -> _Frame:
        step = nice_step(high)
        slot = (WIDTH - AXIS_WIDTH) / count
        return cls(
            step=step,
            scale=(PLOT_BOTTOM - PLOT_TOP) / (step * TICKS),
            slot=slot,
            bar_width=min(slot * BAR_SHARE, MAX_BAR_WIDTH),
        )

    def centre(self, index: int) -> Decimal:
        return AXIS_WIDTH + self.slot * index + self.slot / 2

    def grid(self) -> SafeString:
        """A grid line and its dollar label per step, zero included."""
        return format_html_join(
            "",
            '<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" />'
            '<text class="axis-label" x="{}" y="{}" text-anchor="end" font-size="11" fill="{}">'
            "{}</text>",
            (
                (
                    _coord(AXIS_WIDTH),
                    _coord(PLOT_BOTTOM - self.step * n * self.scale),
                    _coord(WIDTH),
                    _coord(PLOT_BOTTOM - self.step * n * self.scale),
                    AXIS_STROKE,
                    _coord(AXIS_WIDTH - 8),
                    _coord(PLOT_BOTTOM - self.step * n * self.scale + 4),
                    LABEL_FILL,
                    tick_label(self.step * n, self.step),
                )
                for n in range(TICKS + 1)
            ),
        )

    def bar(self, left: Decimal, width: Decimal, amount: Decimal, fill: str) -> SafeString:
        """A bar of ``amount`` from the zero line, ``width`` wide from ``left``."""
        height = amount * self.scale
        return format_html(
            '<rect x="{}" y="{}" width="{}" height="{}" rx="{}" fill="{}" />',
            _coord(left),
            _coord(PLOT_BOTTOM - height),
            _coord(width),
            _coord(height),
            _coord(min(BAR_RADIUS, height / 2)),
            fill,
        )

    def label(self, index: int, text: object) -> SafeString:
        """The slot's label under its bars; a slot narrower than :data:`LABEL_ROOM` labels
        only every n-th slot, so that the labels never overlap (a 36-month P&L)."""
        every = max(1, -(-int(LABEL_ROOM) // max(1, int(self.slot))))
        if index % every:
            return mark_safe("")  # noqa: S308 - the empty string
        return format_html(
            '<text class="bar-label" x="{}" y="{}" text-anchor="middle" font-size="11" '
            'fill="{}">{}</text>',
            _coord(self.centre(index)),
            _coord(LABEL_Y),
            LABEL_FILL,
            text,
        )

    @staticmethod
    def svg(sentence: str, grid: SafeString, groups: list[SafeString]) -> SafeString:
        return format_html(
            '<svg class="collections-svg" viewBox="0 0 {} {}" role="img" aria-label="{}">'
            "{}{}</svg>",
            _coord(WIDTH),
            _coord(HEIGHT),
            sentence,
            grid,
            format_html_join("", "{}", ((group,) for group in groups)),
        )


@register.simple_tag
def collection_bars(rows: Iterable[Any]) -> SafeString:
    """The Collections chart (see the module docstring)."""
    items = list(rows)
    if not items:
        return mark_safe("")  # noqa: S308 - the empty string
    frame = _Frame.of(max(_amount(row.collected) for row in items), len(items))
    groups = []
    for index, row in enumerate(items):
        collected = _amount(row.collected)
        due = _amount(row.due)
        groups.append(
            format_html(
                '<g class="collection-bar{}" data-label="{}" data-collected="{}" data-due="{}">'
                "{}<title>{}: collected {}, due {}</title>{}</g>",
                " is-current" if row.current else "",
                row.title,
                format_amount(collected),
                format_amount(due),
                frame.bar(
                    frame.centre(index) - frame.bar_width / 2,
                    frame.bar_width,
                    collected,
                    CURRENT_FILL if row.current else COLLECTED_FILL,
                ),
                row.title,
                format_amount(collected),
                format_amount(due),
                frame.label(index, row.label),
            )
        )
    current = next((row.title for row in items if row.current), "")
    sentence = f"Collected from {items[0].title} to {items[-1].title}" + (
        f"; {current} is the current one." if current else "."
    )
    return _Frame.svg(sentence, frame.grid(), groups)


def _known(amount: Decimal | None) -> str:
    return UNKNOWN if amount is None else format_amount(amount)


@register.simple_tag
def pnl_bars(rows: Iterable[Any]) -> SafeString:
    """The Profit and loss chart (PLAN-015 Batch 4 §3.3; DESIGN-SPEC §4a's Cashflow content
    in the Collections chart's style): per month, the Collected bar (``--chart-income``) and
    the Costs bar (``--chart-expenses``, deliberately quiet, §7.3) side by side in the
    footprint of one Collections bar, on the same frame, axis and labels. ``rows`` are
    objects with ``label`` (under the bars: "Oct"), ``title`` ("Oct 2026"), ``collected``
    (``Decimal``), ``costs`` and ``margin`` (``Decimal``, or ``None`` when a cost is
    unknown: no cost bar, and "unknown" in the figures). Each month is a
    ``<g class="collection-bar pnl-month">`` with ``data-label``, ``data-collected``,
    ``data-costs`` and ``data-margin`` (the tooltip of ledger/static/ledger/charts.js) and a
    ``<title>`` with the same words."""
    items = list(rows)
    if not items:
        return mark_safe("")  # noqa: S308 - the empty string
    high = max(max(_amount(row.collected), _amount(row.costs)) for row in items)
    frame = _Frame.of(high, len(items))
    width = (frame.bar_width - PAIR_GAP) / 2
    groups = []
    for index, row in enumerate(items):
        collected = _amount(row.collected)
        left = frame.centre(index) - frame.bar_width / 2
        costs = (
            frame.bar(left + width + PAIR_GAP, width, _amount(row.costs), COSTS_FILL)
            if row.costs is not None
            else mark_safe("")  # noqa: S308 - the empty string
        )
        groups.append(
            format_html(
                '<g class="collection-bar pnl-month" data-label="{}" data-collected="{}" '
                'data-costs="{}" data-margin="{}">{}{}'
                "<title>{} · Collected {} · Costs {} · Margin {}</title>{}</g>",
                row.title,
                format_amount(collected),
                _known(row.costs),
                _known(row.margin),
                frame.bar(left, width, collected, COLLECTED_FILL),
                costs,
                row.title,
                format_amount(collected),
                _known(row.costs),
                _known(row.margin),
                frame.label(index, row.label),
            )
        )
    sentence = f"Collected and costs by month from {items[0].title} to {items[-1].title}."
    return _Frame.svg(sentence, frame.grid(), groups)


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
