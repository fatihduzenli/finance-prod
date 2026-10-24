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

In cards mode each body cell also gets a ``data-role`` (docs/qa/PLAN-012f-a §3.2 B3), so that
the phone draws every row as the two-line card of DESIGN-SPEC §6: ``title`` for the cell
holding a ``cell-title`` (else the row's first cell; the cells before it are ``meta``),
``amount`` for the first ``money`` cell (else the cell under an "Amount", "Balance", "Total"
or "Price" header, in that order), ``status`` for the first cell holding a badge, ``actions``
for a cell holding a button, link-button or form (and every cell of its column), ``meta`` for
the rest (``meta-badge`` when its content is badges only, drawn without its label). A summary
table, whose title would be a money cell (the payment totals), has every cell ``meta``. A
``data-role`` written in the template is kept; a cell spanning columns gets none. A table any
of whose body cells holds a field (an input that is not hidden, a select, a textarea) is a
form and gets no roles. Every other cards table gets the class ``role-cards`` on its
``<table>``, which the phone's rules hang on (a class rather than ``:has()``), even when only
its empty row is shown, so that its totals row keeps the one-line layout. The phone lays a
row out as a wrapping flex row, not a grid: a grid area holds one cell, the meta line several.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

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
_TBODY_RE = re.compile(r"(<tbody\b[^>]*>)(.*?)(</tbody>)", re.IGNORECASE | re.DOTALL)
_BODY_CELL_RE = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
_TABLE_TAG_RE = re.compile(r"<table\b([^>]*)>", re.IGNORECASE)
_CLASS_RE = re.compile(r'\bclass\s*=\s*"([^"]*)"', re.IGNORECASE)
_FIELD_RE = re.compile(
    r"""<input\b(?![^>]*\btype\s*=\s*["']?hidden\b)|<select\b|<textarea\b""", re.IGNORECASE
)
_BADGE_RE = re.compile(r"""class\s*=\s*["']badge\b""", re.IGNORECASE)
_ACTION_RE = re.compile(r"""<button\b|<form\b|class\s*=\s*["']btn\b""", re.IGNORECASE)
_ROLE_ATTR_RE = re.compile(r"""\bdata-role\s*=\s*["']?([\w-]*)""", re.IGNORECASE)
#: The headers that name a row's amount when no cell is ``money``, by preference.
AMOUNT_HEADERS = ("Amount", "Balance", "Total", "Price")
ROLE_CLASS = "role-cards"


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


def _is_money(attributes: str) -> bool:
    match = _CLASS_RE.search(attributes)
    return match is not None and "money" in match.group(1).split()


_CELL_TITLE_RE = re.compile(r"""class\s*=\s*["'](?:[^"']*\s)?cell-title["'\s]""", re.IGNORECASE)
_ELEMENT_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>")
_VOID = frozenset({"br", "img", "wbr", "hr", "input"})


@dataclass(frozen=True)
class _Cell:
    match: re.Match[str]
    column: int
    explicit: str | None  # a data-role written in the template

    @property
    def open(self) -> bool:
        """Whether the tag may give this cell a role: a one-column ``td`` without one."""
        return (
            self.explicit is None
            and self.match.group(1).lower() == "td"
            and _span(self.match.group(2)) == 1
        )

    @property
    def inner(self) -> str:
        return self.match.group(3)


def _cells(row: str) -> list[_Cell]:
    cells: list[_Cell] = []
    column = 0
    for match in _BODY_CELL_RE.finditer(row):
        explicit = _ROLE_ATTR_RE.search(match.group(2))
        cells.append(_Cell(match, column, explicit.group(1).lower() if explicit else None))
        column += _span(match.group(2))
    return cells


def _only_badges(markup: str) -> bool:
    """Whether ``markup``'s visible content is badges only (no text or element besides)."""
    depth = 0
    position = 0
    seen = False
    for element in _ELEMENT_RE.finditer(markup):
        if depth == 0 and markup[position : element.start()].strip():
            return False
        closing, name = element.group(1), element.group(2).lower()
        if closing:
            depth -= 1
        elif name not in _VOID:
            if depth == 0:
                if name != "span" or not _BADGE_RE.search(element.group(0)):
                    return False
                seen = True
            depth += 1
        elif depth == 0:
            return False
        position = element.end()
    return seen and depth == 0 and not markup[position:].strip()


def _title_index(cells: list[_Cell]) -> int | None:
    """The row's title: the cell holding a ``cell-title``, else the row's first cell."""
    if any(cell.explicit == "title" for cell in cells):
        return None
    marked = next(
        (i for i, cell in enumerate(cells) if cell.open and _CELL_TITLE_RE.search(cell.inner)),
        None,
    )
    if marked is not None:
        return marked
    return 0 if cells and cells[0].open else None


def _is_summary(cells: list[_Cell]) -> bool:
    """A summary row: no ``cell-title`` and its title would be a money cell (the totals)."""
    title = _title_index(cells)
    return (
        title is not None
        and not _CELL_TITLE_RE.search(cells[title].inner)
        and _is_money(cells[title].match.group(2))
    )


def _row_roles(cells: list[_Cell], labels: list[str]) -> dict[int, str]:
    """The role of each open cell of a row, by its index in the row."""
    roles: dict[int, str] = {}
    taken = {cell.explicit for cell in cells if cell.explicit}
    title = _title_index(cells)
    if title is not None:
        roles[title] = "title"
        for index in range(title):
            if cells[index].open:
                roles[index] = "meta"  # the cells before the title (the operator page's store)
    rest = [i for i, cell in enumerate(cells) if cell.open and i not in roles]
    if "amount" not in taken:
        amount = next((i for i in rest if _is_money(cells[i].match.group(2))), None)
        if amount is None:
            under = {i: labels[cells[i].column] for i in rest if cells[i].column < len(labels)}
            amount = next(
                (i for header in AMOUNT_HEADERS for i in rest if under.get(i) == header), None
            )
        if amount is not None:
            roles[amount] = "amount"
    rest = [i for i in rest if i not in roles]
    if "status" not in taken:
        status = next((i for i in rest if _BADGE_RE.search(cells[i].inner)), None)
        if status is not None:
            roles[status] = "status"
    for index in rest:
        if index not in roles:
            roles[index] = "actions" if _ACTION_RE.search(cells[index].inner) else "meta"
    return roles


def _write_roles(row: str, cells: list[_Cell], roles: dict[int, str]) -> str:
    out: list[str] = []
    position = 0
    for index, cell in enumerate(cells):
        if index in roles:
            match = cell.match
            tag_end = match.start() + len(match.group(1)) + len(match.group(2)) + 1
            out.append(row[position:tag_end])
            out.append(f' data-role="{roles[index]}">')
            position = tag_end + 1
    out.append(row[position:])
    return "".join(out)


def _has_field(body: str) -> bool:
    """Whether a body cell holds a field: the table is a form and keeps its labels only."""
    return any(_FIELD_RE.search(tbody.group(2)) for tbody in _TBODY_RE.finditer(body))


def _with_roles(body: str, labels: list[str]) -> str:
    """``body`` (the table after its ``<thead>``) with roles on its ``<tbody>`` cells.

    The roles are decided row by row, then for the table: a summary table (its title would
    be a money cell, as the payment totals) has every open cell ``meta``; a column holding
    ``actions`` in any row is ``actions`` in every row (an empty cell stays hidden on the
    phone); a ``meta`` cell holding badges only is ``meta-badge`` (drawn without its label).
    """
    rows = [
        _cells(row.group(1))
        for tbody in _TBODY_RE.finditer(body)
        for row in _ROW_RE.finditer(tbody.group(2))
    ]
    summary = any(_is_summary(cells) for cells in rows)
    decided: list[dict[int, str]] = []
    for cells in rows:
        if summary:
            decided.append({i: "meta" for i, cell in enumerate(cells) if cell.open})
        else:
            decided.append(_row_roles(cells, labels))
    action_columns = {
        cells[i].column
        for cells, roles in zip(rows, decided, strict=True)
        for i, role in roles.items()
        if role == "actions"
    }
    for cells, roles in zip(rows, decided, strict=True):
        for index, role in roles.items():
            if role == "meta" and cells[index].column in action_columns:
                roles[index] = "actions"
            elif role == "meta" and _only_badges(cells[index].inner):
                roles[index] = "meta-badge"
    pending = iter(zip(rows, decided, strict=True))

    def row_roles(row: re.Match[str]) -> str:
        cells, roles = next(pending)
        start, end = row.span(1)
        whole, offset = row.group(0), row.start(0)
        inner = _write_roles(row.group(1), cells, roles)
        return whole[: start - offset] + inner + whole[end - offset :]

    def body_roles(tbody: re.Match[str]) -> str:
        return tbody.group(1) + _ROW_RE.sub(row_roles, tbody.group(2)) + tbody.group(3)

    return _TBODY_RE.sub(body_roles, body)


def _add_role_class(tag: re.Match[str]) -> str:
    attributes = tag.group(1)
    found = _CLASS_RE.search(attributes)
    if found is None:
        return f'<table{attributes} class="{ROLE_CLASS}">'
    classes = f"{found.group(1)} {ROLE_CLASS}".strip()
    return f"<table{attributes[: found.start(1)]}{classes}{attributes[found.end(1) :]}>"


def responsive(table: str, mode: str = CARDS) -> str:
    """The table's markup with labelled cells and its mode's class or wrapper."""
    labels = headers(table)
    head = _THEAD_RE.search(table)
    if labels and head is not None:
        top, body = table[: head.end()], table[head.end() :]
        if mode == CARDS and not _has_field(body):
            # The class marks the table even when no row has a role (only its empty row), so
            # that its totals row keeps the one-line layout. The roles go in before the
            # labels: a cell reads data-role, then data-label.
            top = _TABLE_TAG_RE.sub(_add_role_class, top, count=1)
            body = _with_roles(body, labels)
        table = top + _label_cells(body, labels)
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
