"""Responsive tables (docs/qa/PLAN-011-dashboard-reports-mobile.md D9).

``{% responsive_table %}<table>…</table>{% endresponsive_table %}`` gives every ``<td>``
of the table a ``data-label`` with the text of its column's header, so that under 720 px
``finance/app.css`` can show each row as a card of "label: value" lines. The table is
wrapped in a ``table-cards`` region; its own tag is left as it is.

``{% responsive_table "scroll" %}`` keeps the table a table instead: it is wrapped in a
``table-scroll`` region that scrolls sideways with its first column sticky (the audit page
and the reports). Its cells are labelled too.

The headers are the ``<th>`` of the first row of the table's ``<thead>``, ``colspan``
counted. A table without a ``<thead>`` is left as it is. A cell that already has a
``data-label``, and a cell spanning more than one column (a "No rows." line), get none.
The markup is the template's own output: the labels are its header texts, unescaped and
escaped again, never request data.
"""

from __future__ import annotations

import html
import re

from django import template
from django.template.base import NodeList, Parser, Token
from django.utils.safestring import SafeString, mark_safe

register = template.Library()

CARDS = "cards"
SCROLL = "scroll"
MODES = (CARDS, SCROLL)

_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HEADER_CELL_RE = re.compile(r"<th\b([^>]*)>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_COLSPAN_RE = re.compile(r"""\bcolspan\s*=\s*["']?(\d+)""", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_SPACE_RE = re.compile(r"\s+")


def _span(attributes: str) -> int:
    match = _COLSPAN_RE.search(attributes)
    return max(int(match.group(1)), 1) if match else 1


def _text(markup: str) -> str:
    return _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", markup))).strip()


def headers(table: str) -> list[str]:
    """The header text of each column, from the first row of the ``<thead>``."""
    head = _THEAD_RE.search(table)
    if head is None:
        return []
    row = _ROW_RE.search(head.group(1))
    if row is None:
        return []
    labels: list[str] = []
    for attributes, inner in _HEADER_CELL_RE.findall(row.group(1)):
        labels.extend([_text(inner)] * _span(attributes))
    return labels


def _label_cells(body: str, labels: list[str]) -> str:
    """``body`` with each ``<td>`` of each row labelled by its column."""
    out: list[str] = []
    position = 0
    column = 0
    for match in re.finditer(r"<tr\b[^>]*>|<(t[dh])\b([^>]*)>", body, re.IGNORECASE):
        if match.group(1) is None:  # a new row
            column = 0
            continue
        name, attributes = match.group(1).lower(), match.group(2)
        span = _span(attributes)
        if (
            name == "td"
            and span == 1
            and column < len(labels)
            and labels[column]
            and "data-label" not in attributes.lower()
        ):
            out.append(body[position : match.end() - 1])
            out.append(f' data-label="{html.escape(labels[column], quote=True)}">')
            position = match.end()
        column += span
    out.append(body[position:])
    return "".join(out)


def responsive(table: str, mode: str = CARDS) -> str:
    """The table's markup with labelled cells and its mode's class or wrapper."""
    labels = headers(table)
    head = _THEAD_RE.search(table)
    if labels and head is not None:
        table = table[: head.end()] + _label_cells(table[head.end() :], labels)
    if mode == SCROLL:
        return f'<div class="table-scroll" tabindex="0">{table}</div>'
    return f'<div class="table-cards">{table}</div>' if labels else table


class ResponsiveTableNode(template.Node):
    def __init__(self, nodelist: NodeList, mode: str) -> None:
        self.nodelist = nodelist
        self.mode = mode

    def render(self, context: template.Context) -> SafeString:
        # The nodelist's output is the template's own escaped markup; only attributes
        # built from it (escaped again) are added.
        return mark_safe(responsive(self.nodelist.render(context), self.mode))  # noqa: S308


@register.tag("responsive_table")
def responsive_table(parser: Parser, token: Token) -> ResponsiveTableNode:
    bits = token.split_contents()
    mode = CARDS
    if len(bits) == 2:
        mode = bits[1].strip("\"'")
    if len(bits) > 2 or mode not in MODES:
        raise template.TemplateSyntaxError(
            f"{bits[0]} takes no argument or one of {', '.join(repr(m) for m in MODES)}."
        )
    nodelist = parser.parse(("endresponsive_table",))
    parser.delete_first_token()
    return ResponsiveTableNode(nodelist, mode)
