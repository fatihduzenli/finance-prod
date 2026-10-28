"""PLAN-012g Batch A (Fable): the token sets. The light block keeps the §1 values and gains
the names the dark theme needs; the two dark blocks (``[data-theme="dark"]`` and the System
block under ``prefers-color-scheme: dark``) are identical, sit inside ``@media screen`` so print
never sees them, and hold the §7.2 / §7.3 values; every token the dark block sets exists in the
light block too, so no colour depends on the theme attribute being present."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TOKENS = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance" / "tokens.css"

SPEC_DARK = {
    "--sidebar-gradient": "none",
    "--sidebar-bg": "#171721",
    "--sidebar-border": "#2E2E3D",
    "--sidebar-text": "#A3A3B3",
    "--sidebar-active-bg": "rgba(46,196,166,.18)",
    "--sidebar-active-text": "#FFFFFF",
    "--bg": "#171721",
    "--surface": "#1E1E2A",
    "--surface-2": "#272735",
    "--border": "#2E2E3D",
    "--border-soft": "#26263A",
    "--input-border": "#70707D",
    "--text": "#EDEDF3",
    "--text-2": "#C3C3CC",
    "--muted": "#A3A3B3",
    "--faint": "#70707D",
    "--link": "#2EC4A6",
    "--btn-primary-bg": "#0E8F86",
    "--btn-primary-border": "#0A6B64",
    "--btn-primary-text": "#FFFFFF",
    "--paid-bg": "#1F3A2C",
    "--paid-text": "#7FDCA4",
    "--unpaid-bg": "#3A2226",
    "--unpaid-text": "#F2A3A8",
    "--overdue-bg": "#5A1D24",
    "--overdue-text": "#FFD6D9",
    "--pending-bg": "#3A3420",
    "--pending-text": "#F0D68A",
    "--partial-bg": "#3D2A1A",
    "--partial-text": "#F5B98A",
    "--waived-bg": "#2E2E3D",
    "--waived-text": "#C3C3CC",
    "--neutral-bg": "#2E2E3D",
    "--neutral-text": "#C3C3CC",
    "--sandbox-bg": "#3A3420",
    "--sandbox-text": "#F0D68A",
    # §7.3 dark chart and dot colours
    "--chart-income": "#7C8CF8",
    "--chart-expenses": "#3E4460",
    "--chart-net": "#2EC4A6",
    "--chart-overdue": "#E5484D",
    "--chart-paid": "#46C47E",
    "--chart-upcoming": "#8FA3BF",
    "--dot-rent": "#2EC4A6",
    "--dot-settles": "#46C47E",
    "--dot-promise": "#E5B84D",
    "--dot-lease-end": "#E5484D",
    # §7.6
    "--header-bg": "var(--bg)",
    "--card-border": "transparent",
    "--popover-border": "#3A3A4C",
    "--popover-shadow": "0 12px 32px rgba(0,0,0,.45)",
    "--month-current": "rgba(46,196,166,.08)",
    "color-scheme": "dark",
}

SPEC_LIGHT_ADDED = {
    "--sidebar-bg": "#0B2545",
    "--sidebar-border": "transparent",
    "--header-bg": "var(--surface)",
    "--card-border": "var(--border)",
    "--text-on-accent": "#FFFFFF",
    "--btn-primary-text": "#FFFFFF",
    "--sandbox-bg": "#FFF3C4",
    "--sandbox-text": "#6B4E00",
    "--chart-income": "#5B6BE8",
    "--chart-expenses": "#C9CEDD",
    "--chart-net": "#0E8F86",
    "--chart-overdue": "#B42318",
    "--chart-paid": "#1B7F3B",
    "--chart-upcoming": "#0B2545",
    "--dot-rent": "#13A89E",
    "--dot-settles": "#1B7F3B",
    "--dot-promise": "#B7791F",
    "--dot-lease-end": "#B42318",
    "--popover-shadow": "none",
}


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def declarations(block: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, value in re.findall(r"(--[\w-]+|color-scheme)\s*:\s*([^;]+);", block):
        found[name] = " ".join(value.split())
    return found


def block_after(css: str, selector_pattern: str) -> str:
    match = re.search(selector_pattern + r"\s*\{", css)
    assert match, selector_pattern
    depth, start = 1, match.end()
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start:index]
    raise AssertionError("unterminated block")


@pytest.fixture(scope="module")
def css() -> str:
    return strip_comments(TOKENS.read_text())


def test_the_light_block_gains_the_names_the_dark_theme_needs(css: str) -> None:
    light = declarations(block_after(css, r"(?<![\[\w\-\]])\s:root"))
    for name, value in SPEC_LIGHT_ADDED.items():
        assert light.get(name) == value, (name, light.get(name))
    assert light["--bg"] == "#F4F6F9" and light["--text"] == "#0B2545"  # §1 untouched


def test_the_two_dark_blocks_are_identical_and_hold_the_spec(css: str) -> None:
    dark = declarations(block_after(css, r':root\[data-theme="dark"\]'))
    system = declarations(block_after(css, r':root\[data-theme="system"\]'))
    assert dark == system, set(dark.items()) ^ set(system.items())
    for name, value in SPEC_DARK.items():
        assert dark.get(name) == value, (name, dark.get(name))


def test_every_dark_token_exists_in_light_so_no_colour_needs_the_attribute(css: str) -> None:
    light = declarations(block_after(css, r"(?<![\[\w\-\]])\s:root"))
    dark = declarations(block_after(css, r':root\[data-theme="dark"\]'))
    missing = sorted(set(dark) - set(light) - {"color-scheme"})
    assert not missing, missing


def test_the_dark_blocks_sit_inside_media_screen_so_print_stays_light(css: str) -> None:
    screen = block_after(css, r"@media\s+screen")
    assert ':root[data-theme="dark"]' in screen
    assert "prefers-color-scheme: dark" in screen and ':root[data-theme="system"]' in screen
    outside = css.replace(screen, "")
    assert 'data-theme="dark"' not in outside and 'data-theme="system"' not in outside


def test_the_system_block_is_under_prefers_color_scheme_dark(css: str) -> None:
    media = block_after(css, r"@media\s*\(prefers-color-scheme:\s*dark\)")
    assert ':root[data-theme="system"]' in media
    assert ':root[data-theme="dark"]' not in media
