"""Template helpers of the component set (docs/qa/PLAN-012c-design-pass.md D5).

``{% status_badge label %}`` (or ``{% status_badge label value %}``) renders a status as
``<span class="badge badge-<status>"><span class="badge-glyph" aria-hidden="true">✓</span>
label</span>``: the status is found from the stored value when one is given, else from the
label (case and spaces ignored). Each status has its leading glyph (✓ ◻ ◐ ◑ – ○ ✕ !), in
the markup so that it prints and reads without the style sheet, hidden from screen readers
(the label is the text): colour is never the only signal. "Not due" (the grid) is a plain
``.badge.not-due``: no colour, no glyph, its text only. A status this module does not know
renders as a plain ``.badge``.

``step_links|with_done`` adds a fourth item to each wizard step (label, link, current):
``True`` for the steps before the current one, which ``.steps`` marks done.

``actions|action_named:"key"`` finds a workspace action (ledger.services.workspace.Action)
by its key, or ``None``: an empty state names it as its next step.

Every helper works on what the view already passed: no query, no setting.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django import template
from django.utils.html import format_html
from django.utils.safestring import SafeString

register = template.Library()

#: The badge statuses, each with a ``.badge-<status>`` rule in app.css.
PAID = "paid"
UNPAID = "unpaid"
PENDING = "pending"
PARTIAL = "partial"
WAIVED = "waived"
DUE = "due"
VOIDED = "voided"
ATTENTION = "attention"
BADGE_STATUSES: tuple[str, ...] = (
    PAID,
    UNPAID,
    PENDING,
    PARTIAL,
    WAIVED,
    DUE,
    VOIDED,
    ATTENTION,
)
#: The leading glyph of each badge status.
GLYPHS: dict[str, str] = {
    PAID: "\u2713",  # ✓
    UNPAID: "\u25fb",  # ◻
    PENDING: "\u25d0",  # ◐
    PARTIAL: "\u25d1",  # ◑
    WAIVED: "\u2013",  # –
    DUE: "\u25cb",  # ○ (● is the grid's note marker)
    VOIDED: "\u2715",  # ✕
    ATTENTION: "!",
}
#: Not a badge status: a plain, glyph-less badge (the grid's "Not due" cells hold no charge).
NOT_DUE = "not-due"

#: Stored values and on-screen labels (lower case) to their badge status. Labels from
#: RentChargeStatus, OneOffChargeStatus, PaymentStatus, the insurance rows of the store
#: page and the grid's filter (with the copy list's future wording, §4.5).
_STATUS_OF: dict[str, str] = {
    "paid": PAID,
    "settled": PAID,
    "unpaid": UNPAID,
    "owed": UNPAID,
    "pending": PENDING,
    "carried by re-debit": PENDING,
    "partial": PARTIAL,
    "partially paid": PARTIAL,
    "partly paid": PARTIAL,
    "waived": WAIVED,
    "due": DUE,
    "voided": VOIDED,
    "attention": ATTENTION,
    "returned": ATTENTION,
    "needs premium": ATTENTION,
    "needs review": ATTENTION,
    "not_due": NOT_DUE,
    "not due": NOT_DUE,
    "not yet due": NOT_DUE,
}


def badge_status(*candidates: object) -> str:
    """The badge status of the first candidate this module knows, or ``""``."""
    for candidate in candidates:
        if candidate is None:
            continue
        found = _STATUS_OF.get(" ".join(str(candidate).split()).lower())
        if found:
            return found
    return ""


@register.simple_tag
def status_badge(label: object, value: object = None) -> SafeString:
    """A status badge: its class from ``value`` (or ``label``), its text ``label``."""
    status = badge_status(value, label)
    if status == NOT_DUE:
        return format_html('<span class="badge not-due">{}</span>', label)
    if status:
        return format_html(
            '<span class="badge badge-{}"><span class="badge-glyph" aria-hidden="true">{}</span>'
            " {}</span>",
            status,
            GLYPHS[status],
            label,
        )
    return format_html('<span class="badge">{}</span>', label)


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


@register.filter
def action_named(actions: Iterable[Any] | None, key: str) -> Any:
    """The action whose ``key`` is ``key``, or ``None``."""
    for action in actions or ():
        if getattr(action, "key", None) == key:
            return action
    return None
