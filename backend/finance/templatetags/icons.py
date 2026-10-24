"""``{% icon "name" %}`` (or ``{% icon "name" 22 %}``): an inline stroke icon
(docs/qa/PLAN-012f-approved-design.md D5; docs/design/DESIGN-SPEC.md §5).

Each icon is an ``<svg>`` on a 24-unit grid, drawn in ``currentColor`` with a 2-unit round
stroke (the Lucide style; the paths are this module's own), ``aria-hidden="true"`` and not
focusable: the word beside it carries the meaning. The size is in pixels, 18 by default (the
sidebar and the buttons take 16 to 18, the phone's bottom bar 22). The table is fixed: an
icon never comes from data. An unknown name or a size outside :data:`SIZES` raises at render
with ``DEBUG`` (a template mistake) and renders nothing otherwise.
"""

from __future__ import annotations

from django import template
from django.conf import settings
from django.utils.safestring import SafeString, mark_safe

register = template.Library()

#: The sizes a template may ask for, in pixels.
SIZES = range(12, 33)
DEFAULT_SIZE = 18

#: The inner markup of each icon, on a 24 x 24 grid.
ICONS: dict[str, str] = {
    "dashboard": (
        '<rect x="3" y="3" width="7" height="9" rx="1.5"/>'
        '<rect x="14" y="3" width="7" height="5" rx="1.5"/>'
        '<rect x="14" y="12" width="7" height="9" rx="1.5"/>'
        '<rect x="3" y="16" width="7" height="5" rx="1.5"/>'
    ),
    "stores": (
        '<path d="M3 9.5 4.5 4h15L21 9.5"/><path d="M4 9.5V20h16V9.5"/><path d="M9.5 20v-6h5v6"/>'
    ),
    "operators": (
        '<circle cx="12" cy="8" r="4"/><path d="M4.5 21c0-4.1 3.4-7 7.5-7s7.5 2.9 7.5 7"/>'
    ),
    "monthly-rent": (
        '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'
    ),
    "reports": '<path d="M4 4v16h16"/><path d="M8.5 16v-4M13 16V8M17.5 16v-6"/>',
    "admin": (
        '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="7"/>'
        '<path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17'
        'M17 7l2.1-2.1"/>'
    ),
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "more": (
        '<circle cx="5" cy="12" r="1.25"/><circle cx="12" cy="12" r="1.25"/>'
        '<circle cx="19" cy="12" r="1.25"/>'
    ),
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "file": (
        '<path d="M14 3H6.5A2.5 2.5 0 0 0 4 5.5v13A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 '
        '2.5-2.5V9z"/><path d="M14 3v6h6"/>'
    ),
    "image": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/>'
        '<path d="m21 15-5-5L5 21"/>'
    ),
    "calendar": (
        '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'
        '<path d="M7.5 14h2M11 14h2M14.5 14h2M7.5 17.5h2M11 17.5h2"/>'
    ),
    "user": (
        '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="10" r="3"/>'
        '<path d="M6.5 18.5c1.3-2 3.2-3 5.5-3s4.2 1 5.5 3"/>'
    ),
    "check": '<path d="m5 12.5 4.5 4.5L19 7"/>',
    "circle": '<circle cx="12" cy="12" r="8"/>',
    "circle-half": (
        '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/>'
    ),
    "dash": '<path d="M6 12h12"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 6 6 6-6 6"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    # PLAN-012f-a D4: the detail pages' empty Payments card.
    "banknote": (
        '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2"/>'
        '<path d="M6 12h.01M18 12h.01"/>'
    ),
    # PLAN-012f-a D5: the invoice builder's remove-line button.
    "trash": (
        '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>'
    ),
}


@register.simple_tag
def icon(name: str, size: int = DEFAULT_SIZE) -> SafeString:
    """The icon ``name`` at ``size`` pixels; see the module docstring."""
    inner = ICONS.get(name)
    if inner is None or not isinstance(size, int) or size not in SIZES:
        if settings.DEBUG:
            raise template.TemplateSyntaxError(f"Unknown icon or size: {name!r}, {size!r}")
        return mark_safe("")  # noqa: S308 - the empty string
    return mark_safe(  # noqa: S308 - fixed markup from ICONS and an integer size
        f'<svg class="icon icon-{name}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true" focusable="false">{inner}</svg>'
    )
