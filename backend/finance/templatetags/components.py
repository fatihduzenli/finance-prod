"""Template helpers of the component set (docs/qa/PLAN-012c-design-pass.md D5; the approved
design's components, docs/qa/PLAN-012f-approved-design.md D6 and docs/design/DESIGN-SPEC.md §3).

``{% status_badge label %}`` (or ``{% status_badge label value %}``) renders a status as a
pill of its *family*: ``<span class="badge badge-<family>"><span class="badge-icon"
aria-hidden="true">✓</span> label</span>``. The family is found from the stored value when one
is given, else from the label (case and spaces ignored). The seven families are the spec's:
paid ✓, unpaid ●, overdue ● (dark red, white text), pending ◐, partial ◑, waived – and neutral
○. Today's statuses map onto them: a rent charge ``due`` (unpaid, not yet overdue) and a
one-off charge ``owed`` are *unpaid*; a rent charge ``unpaid`` (past due) is *overdue*;
``voided`` and "Not yet due" are *neutral*; ``attention`` (a returned payment, a store or row
that needs review, a missing premium) is *unpaid*. The icon is in the markup, so that it prints
and reads without the style sheet, hidden from screen readers (the label is the text): colour
is never the only signal. A status this module does not know renders as a plain ``.badge``.

``{% kpi label value note tone %}``: a KPI card (label, value, third line); ``tone="unpaid"``
sets the value in the unpaid red. ``{% progress segments %}``: a stacked bar of
``(family, amount)`` segments with its legend; the widths are rounded down to a tenth of a per
cent, so they sum to at most 100 %, and a zero total draws nothing. ``{% date_chip day %}``:
the month over the day. ``{% avatar letter family %}``: the Notes & follow-ups' round letter,
in the pending or the neutral colours.

``step_links|with_done`` adds a fourth item to each wizard step (label, link, current):
``True`` for the steps before the current one, which ``.steps`` marks done.
``{% step_line step_links %}``: the phone's form of the same list (PLAN-012f D10; DESIGN-SPEC §6):
"Step 2 of 7 · Statement" and a bar of the steps reached; nothing without a current step.

``page|page_numbers``: the list footer's page numbers, gaps as "…" (PLAN-012f-a D3).

``store.state|state_name``: a store's two-letter state code as its full name ("TX" is
"Texas"; PLAN-012f-b D11); a value that is not a code is shown as stored.

``actions|action_named:"key"`` finds a workspace action (ledger.services.workspace.Action)
by its key, or ``None``: an empty state names it as its next step.

``{% dialog id="…" title="…" size="sm" open=False return_url="" %} … {% enddialog %}``
(PLAN-012f-b D2; DESIGN-SPEC §4 States, the confirmation dialog, and §4a): a ``<dialog
class="dialog dialog-<size>">`` (``sm`` 440 px, ``lg`` 640 px; a sheet on phones) with its
head (the title as an ``h2`` and a "×" close button), the content in ``.dialog-body``. The
caller writes its buttons in a ``<div class="dialog-foot">`` inside the content (so that a
form can hold its fields and its buttons). ``open=True`` gives the dialog the ``open``
attribute, so that it shows with scripts off, and ``data-open``: the script
(finance/static/finance/dialog.js) makes it modal on load and, when ``return_url`` is a path
on this site, goes there on close; any other ``return_url`` is dropped.

Every helper works on what the view already passed: no query, no setting. Every text is
escaped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from django import template
from django.template.base import FilterExpression, NodeList, Parser, Token, token_kwargs
from django.template.context import Context
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe

from ledger.services.stores import state_text
from ledger.wording import format_amount

register = template.Library()

#: The badge families (DESIGN-SPEC §3), each with a ``.badge-<family>`` rule in app.css.
PAID = "paid"
UNPAID = "unpaid"
OVERDUE = "overdue"
PENDING = "pending"
PARTIAL = "partial"
WAIVED = "waived"
NEUTRAL = "neutral"
FAMILIES: tuple[str, ...] = (PAID, UNPAID, OVERDUE, PENDING, PARTIAL, WAIVED, NEUTRAL)
#: The icon of each family, as the spec writes it.
GLYPHS: dict[str, str] = {
    PAID: "✓",  # ✓
    UNPAID: "●",  # ●
    OVERDUE: "●",  # ●
    PENDING: "◐",  # ◐
    PARTIAL: "◑",  # ◑
    WAIVED: "–",  # –
    NEUTRAL: "○",  # ○
}
#: The family's word where a component names the family itself (the progress legend).
WORDS: dict[str, str] = {
    PAID: "Paid",
    UNPAID: "Unpaid",
    OVERDUE: "Overdue",
    PENDING: "Pending",
    PARTIAL: "Partly paid",
    WAIVED: "Waived",
    NEUTRAL: "Other",
}

#: Stored values and on-screen labels (lower case) to their family. Labels from
#: RentChargeStatus, OneOffChargeStatus, PaymentStatus, the store statuses
#: (ledger.services.stores.STATUS_BADGES), the insurance rows of the store page and the grid's
#: filter (with the copy list's wording, §4.5).
_FAMILY_OF: dict[str, str] = {
    "paid": PAID,
    "settled": PAID,
    "due": UNPAID,
    "owed": UNPAID,
    "unpaid, not yet overdue": UNPAID,
    "attention": UNPAID,
    "returned": UNPAID,
    "needs premium": UNPAID,
    "needs review": UNPAID,
    "unpaid": OVERDUE,
    "unpaid, overdue": OVERDUE,
    "overdue": OVERDUE,
    "pending": PENDING,
    "carried by re-debit": PENDING,
    "partial": PARTIAL,
    "partially paid": PARTIAL,
    "partly paid": PARTIAL,
    "waived": WAIVED,
    "voided": NEUTRAL,
    "not_due": NEUTRAL,
    "not due": NEUTRAL,
    "not yet due": NEUTRAL,
    "neutral": NEUTRAL,
}

#: The families the KPI value may take (DESIGN-SPEC §3: red for unpaid balances).
KPI_TONES: tuple[str, ...] = (UNPAID,)
#: The families of the Notes & follow-ups' avatar (DESIGN-SPEC §3: P pending, C neutral).
AVATAR_FAMILIES: tuple[str, ...] = (PENDING, NEUTRAL)
#: A tenth of a per cent: the progress bar's widths are rounded down to it.
_TENTH = Decimal("0.1")


def badge_family(*candidates: object) -> str:
    """The family of the first candidate this module knows, or ``""``."""
    for candidate in candidates:
        if candidate is None:
            continue
        found = _FAMILY_OF.get(" ".join(str(candidate).split()).lower())
        if found:
            return found
    return ""


@register.simple_tag
def status_badge(label: object, value: object = None) -> SafeString:
    """A status badge: its family from ``value`` (or ``label``), its text ``label``."""
    family = badge_family(value, label)
    if not family:
        return format_html('<span class="badge">{}</span>', label)
    return format_html(
        '<span class="badge badge-{}"><span class="badge-icon" aria-hidden="true">{}</span>'
        " {}</span>",
        family,
        GLYPHS[family],
        label,
    )


#: What ``kpi``'s ``value_attrs`` may hold: ``data-*`` attributes with plain values only.
_VALUE_ATTRS_RE = re.compile(r'(?: data-[a-z][a-z-]*="[A-Za-z0-9_-]*")*')
#: What ``kpi``'s ``value_class`` may hold: class names separated by spaces.
_VALUE_CLASS_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_-]*(?: [A-Za-z][A-Za-z0-9_-]*)*)?")


@register.simple_tag
def kpi(
    label: object,
    value: object,
    note: object = "",
    tone: str = "",
    *,
    value_class: str = "",
    value_attrs: str = "",
) -> SafeString:
    """A KPI card: the label, the value (red for ``tone="unpaid"``), the third line.

    ``value_class`` adds class names to the value and ``value_attrs`` gives it ``data-*``
    attributes (a script's hooks, as the statement step's live Deductions tile, PLAN-013a
    C7); both are template literals, and anything else is a template mistake
    (``ValueError``). Without them the output is unchanged."""
    if not _VALUE_CLASS_RE.fullmatch(value_class):
        raise ValueError(f"kpi value_class {value_class!r} is not class names")
    attrs = f" {value_attrs.strip()}" if value_attrs.strip() else ""
    if not _VALUE_ATTRS_RE.fullmatch(attrs):
        raise ValueError(f"kpi value_attrs {value_attrs!r} are not plain data- attributes")
    value_class = " ".join(
        part
        for part in ("kpi-value", f"tone-{tone}" if tone in KPI_TONES else "", value_class)
        if part
    )
    third = format_html('<span class="kpi-note">{}</span>', note) if note not in ("", None) else ""
    return format_html(
        '<div class="kpi"><span class="kpi-label">{}</span><span class="{}"{}>{}</span>{}</div>',
        label,
        value_class,
        mark_safe(attrs),  # noqa: S308 (checked above: data- attributes with plain values)
        value,
        third,
    )


def _positive(value: object) -> Decimal:
    """``value`` as a ``Decimal`` above zero, else zero (a negative, a non-number)."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return amount if amount.is_finite() and amount > 0 else Decimal(0)


def progress_widths(segments: Iterable[Sequence[Any]]) -> list[tuple[str, Decimal, Decimal]]:
    """``(family, amount, width %)`` of each segment with an amount above zero, the widths
    rounded down to a tenth so that they sum to at most 100; ``[]`` for a zero total. A family
    this module does not know raises ``ValueError`` (a template mistake)."""
    found: list[tuple[str, Decimal]] = []
    for family, value in segments:
        if family not in FAMILIES:
            raise ValueError(f"unknown progress family {family!r}")
        amount = _positive(value)
        if amount > 0:
            found.append((str(family), amount))
    total = sum((amount for _, amount in found), Decimal(0))
    if total <= 0:
        return []
    return [
        (family, amount, (amount * 100 / total).quantize(_TENTH, rounding=ROUND_FLOOR))
        for family, amount in found
    ]


@register.simple_tag
def progress(segments: Iterable[Sequence[Any]]) -> SafeString:
    """A stacked bar of ``(family, amount)`` segments in the status colours with its legend
    of 8 px squares; nothing for a zero total."""
    widths = progress_widths(segments)
    if not widths:
        return mark_safe("")  # noqa: S308 - the empty string
    described = ", ".join(
        f"{WORDS[family]} {format_amount(amount)}" for family, amount, _ in widths
    )
    bar = format_html_join(
        "",
        '<span class="progress-segment progress-{}" style="width: {}%"></span>',
        ((family, width) for family, _, width in widths),
    )
    legend = format_html_join(
        "",
        '<li><span class="progress-key progress-{}" aria-hidden="true"></span>{}</li>',
        ((family, WORDS[family]) for family, _, _ in widths),
    )
    return format_html(
        '<div class="progress"><div class="progress-bar" role="img" aria-label="{}">{}</div>'
        '<ul class="progress-legend">{}</ul></div>',
        described,
        bar,
        legend,
    )


@register.simple_tag
def date_chip(day: object) -> SafeString:
    """The date chip of a "Coming up" list: the month over the day; nothing without a date."""
    if not isinstance(day, date):
        return mark_safe("")  # noqa: S308 - the empty string
    return format_html(
        '<time class="date-chip" datetime="{}"><span class="date-chip-month">{}</span>'
        '<span class="date-chip-day">{}</span></time>',
        f"{day:%Y-%m-%d}",
        f"{day:%b}",
        day.day,
    )


@register.simple_tag
def avatar(letter: object, family: str = NEUTRAL) -> SafeString:
    """The Notes & follow-ups' 34 px round letter in the ``family``'s colours (pending or
    neutral), hidden from screen readers: the entry's kind is its text."""
    if family not in AVATAR_FAMILIES:
        raise ValueError(f"unknown avatar family {family!r}")
    return format_html(
        '<span class="avatar avatar-{}" aria-hidden="true">{}</span>',
        family,
        str(letter).strip()[:1].upper(),
    )


@register.filter
def with_done(steps: Iterable[Sequence[Any]]) -> list[tuple[Any, Any, bool, bool]]:
    """(label, link, current, done) per step; done: before the current step."""
    found: list[tuple[Any, Any, bool, bool]] = []
    seen_current = False
    for label, url, is_current in steps:
        found.append((label, url, bool(is_current), not seen_current and not is_current))
        seen_current = seen_current or bool(is_current)
    if not seen_current:  # no current step: nothing is known to be done
        return [(label, url, is_current, False) for label, url, is_current, _ in found]
    return found


@register.simple_tag
def step_line(steps: Iterable[Sequence[Any]] | None) -> SafeString:
    """The phone's step line, "Step N of M · <the current step's label>", over a bar of N
    of M; nothing when no step is current."""
    listed = list(steps or ())
    position = next(
        (number for number, (_, _, is_current) in enumerate(listed, 1) if is_current), None
    )
    if position is None:
        return mark_safe("")  # noqa: S308 - the empty string
    return format_html(
        '<p class="steps-line" id="wizard-step-line">Step {} of {} '
        '<span class="steps-name">· {}</span></p>'
        '<progress class="steps-progress" value="{}" max="{}" aria-hidden="true"></progress>',
        position,
        len(listed),
        listed[position - 1][0],
        position,
        len(listed),
    )


@register.filter
def state_name(code: object) -> str:
    """The full name of a state code; a value that is not a code as stored, "" for none."""
    return state_text(code if isinstance(code, str) else "")


@register.filter
def action_named(actions: Iterable[Any] | None, key: str) -> Any:
    """The action whose ``key`` is ``key``, or ``None``."""
    for action in actions or ():
        if getattr(action, "key", None) == key:
            return action
    return None


@register.filter
def page_numbers(page: Any) -> list[tuple[str, int | None, bool]]:
    """The list footer's page numbers (PLAN-012f-a D3; DESIGN-SPEC §4 Stores list) around
    ``page`` (a Django ``Page``): (text, number to link or ``None`` for a gap "…", current),
    the first and last pages and one on each side of the current one."""
    found: list[tuple[str, int | None, bool]] = []
    for number in page.paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1):
        if isinstance(number, int):
            found.append((str(number), number, number == page.number))
        else:
            found.append((str(number), None, False))
    return found


#: The dialog's sizes (D2): the confirmation frame and the form frame.
DIALOG_SIZES: tuple[str, ...] = ("sm", "lg")
_DIALOG_ARGUMENTS: frozenset[str] = frozenset({"id", "title", "size", "open", "return_url"})


def _local_path(url: object) -> str:
    """``url`` when it is a path on this site ("/…", not "//…"), else ``""``."""
    text = str(url or "")
    if text.startswith("/") and not text.startswith("//") and "\\" not in text:
        return text
    return ""


class DialogNode(template.Node):
    def __init__(self, nodelist: NodeList, arguments: dict[str, FilterExpression]) -> None:
        self.nodelist = nodelist
        self.arguments = arguments

    def render(self, context: Context) -> SafeString:
        values = {name: value.resolve(context) for name, value in self.arguments.items()}
        size = str(values.get("size") or "sm")
        if size not in DIALOG_SIZES:
            raise template.TemplateSyntaxError(f"unknown dialog size {size!r}")
        element_id = str(values.get("id") or "")
        if not element_id:
            raise template.TemplateSyntaxError("a dialog needs an id")
        return_url = _local_path(values.get("return_url"))
        return format_html(
            '<dialog class="dialog dialog-{size}" id="{id}" aria-labelledby="{id}-title"'
            "{shown}{ret}{op}>"
            '<div class="dialog-head"><h2 id="{id}-title">{title}</h2>'
            '<button type="button" class="dialog-close" aria-label="Close">&times;</button></div>'
            '<div class="dialog-body">{body}</div></dialog>',
            size=size,
            id=element_id,
            title=values.get("title") or "",
            ret=format_html(' data-return-url="{}"', return_url) if return_url else "",
            # PLAN-012f-b D2: a server-opened dialog shows without the script as well.
            shown=mark_safe(" open") if values.get("open") else "",  # noqa: S308 - a constant
            op=mark_safe(" data-open") if values.get("open") else "",  # noqa: S308 - a constant
            body=self.nodelist.render(context),
        )


@register.tag("dialog")
def dialog(parser: Parser, token: Token) -> DialogNode:
    """``{% dialog id="…" title="…" size="sm|lg" open=… return_url=… %}…{% enddialog %}``."""
    bits = token.split_contents()[1:]
    arguments = token_kwargs(bits, parser)
    if bits:
        raise template.TemplateSyntaxError("dialog takes keyword arguments only")
    unknown = set(arguments) - _DIALOG_ARGUMENTS
    if unknown:
        raise template.TemplateSyntaxError(f"unknown dialog arguments {sorted(unknown)}")
    nodelist = parser.parse(("enddialog",))
    parser.delete_first_token()
    return DialogNode(nodelist, arguments)
