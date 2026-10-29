"""PLAN-012g Batch B (Fable): the shape rules of DESIGN-SPEC §7.4. Buttons, text inputs, selects,
badges and the segmented controls are pills in both themes (one rule — the theme is tokens only);
cards, dialogs, toasts, popovers, status boxes, chips, menus, pagers and the text area keep their
corners; a pill control carries the extra horizontal padding so its text clears the curve."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance"
PILL = "border-radius: var(--radius-pill);"
CTL = "border-radius: var(--radius-ctl);"
CARD = "border-radius: var(--radius-card);"


@pytest.fixture(scope="module")
def css() -> str:
    text = (STATIC / "app.css").read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def rule(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly ``selector``."""
    pattern = r"(?:^|\}|\{)\s*" + re.escape(selector) + r"\s*\{([^}]*)\}"
    match = re.search(pattern, css, re.M)
    assert match is not None, selector
    return " ".join(match.group(1).split())


INPUTS = (
    "input[type=text], input[type=email], input[type=search], input[type=number], "
    "input[type=password], input[type=date], input[type=month], input[type=tel], "
    "input[type=url], select, textarea"
)
AUTH_INPUTS = (
    ".auth-main input[type=text], .auth-main input[type=password], "
    ".auth-main input[type=number], .auth-main input[type=email], .auth-main input[type=tel]"
)


@pytest.mark.parametrize(
    ("selector", "padding"),
    [
        (".btn", "padding: 10px 18px;"),
        (INPUTS, "padding: 9px 14px;"),
        (".nav-search input[type=search]", "padding: 9px 14px 9px 36px;"),
        (".segmented", None),
        (".segmented a, .segmented button", "padding: 5px 12px;"),
        (".radio-segmented:not(input)", None),
        (".radio-segmented > div > label", "padding: 5px 12px;"),
        (".pill-type", None),
        (".badge", None),
        (".count-pill", None),
        (AUTH_INPUTS, "padding: 12px 16px;"),
        (".auth-main .btn", "padding: 13px;"),
    ],
)
def test_the_controls_are_pills_with_room_for_their_text(
    css: str, selector: str, padding: str | None
) -> None:
    declarations = rule(css, selector)
    assert PILL in declarations, (selector, declarations)
    if padding:
        assert padding in declarations, (selector, declarations)


def test_the_small_button_keeps_its_height_and_gains_width(css: str) -> None:
    assert "padding: 6px 14px;" in rule(css, ".btn-sm")


@pytest.mark.parametrize(
    ("selector", "radius"),
    [
        ("textarea", CTL),
        (".mini-popover", CTL),
        (".status-box", CTL),
        (".date-chip", CTL),
        (".pager-page", CTL),
        (".menu li a, .menu li button", None),
        (".card", CARD),
        (".kpi", CARD),
        (".dialog", "border-radius: 14px;"),
        (".toast", "border-radius: 10px;"),
    ],
)
def test_the_surfaces_keep_their_corners(css: str, selector: str, radius: str | None) -> None:
    declarations = rule(css, selector)
    assert PILL not in declarations, (selector, declarations)
    if radius:
        assert radius in declarations, (selector, declarations)


def test_the_text_area_rule_follows_the_generic_inputs_rule(css: str) -> None:
    # The generic rule makes every field a pill; the text area's own rule after it takes that back.
    generic = css.index(INPUTS + " {")
    own = css.index("textarea { padding: 9px 12px; " + CTL)
    assert generic < own


def test_no_literal_pill_radius_remains(css: str) -> None:
    assert "999px" not in css and "99px" not in css.replace("var(--radius-pill)", "")


def test_the_phone_tap_rule_still_covers_the_segments(css: str) -> None:
    # On a coarse pointer each segment is at least 44 px tall; the pill makes a short label round
    # at the ends, which is the spec's shape, not a defect.
    phone = css[css.index("@media (max-width: 1023.98px), (pointer: coarse)") :]
    assert re.search(r"\.segmented a[^{]*\{[^}]*min-height: var\(--tap\)", phone)
