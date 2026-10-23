"""The month grid on a laptop and a phone (docs/qa/PLAN-011b D2): a scrollbar that is
always there, a mirrored one above the table, the store column and the header fixed,
220 px for the store column and 80 px per month. The browser checks are in
``tests/e2e/test_grid_laptop.py`` and ``tests/e2e/test_mobile.py``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from accounts.roles import Role
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

APP_CSS = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance" / "app.css"
STORE_WIDTH = 220
#: PLAN-012f D9: 110 to 120 px, so that a cell block's two lines fit without wrapping.
MONTH_WIDTH = 116


def grid_page() -> str:
    response = client_for(Role.VIEWER)[0].get("/grid/")
    assert response.status_code == 200
    return response.content.decode()


def test_the_scrollbar_is_always_there_and_styled_for_webkit() -> None:
    page = grid_page()
    rule = re.search(r"\.grid-scroll\{([^}]*)\}", page)
    assert rule is not None
    assert "overflow-x:scroll;" in rule.group(1)  # never "auto": the bar never hides
    assert "scrollbar-gutter:stable;" in rule.group(1)
    assert "scrollbar-width:auto;" in rule.group(1)
    assert ".grid-scroll::-webkit-scrollbar" in page and "-webkit-appearance:none;" in page
    assert ".grid-scroll::-webkit-scrollbar-thumb" in page


def test_a_mirrored_scrollbar_sits_above_the_grid_and_follows_it() -> None:
    page = grid_page()
    top = page.index('id="grid-scroll-top"')
    assert top < page.index('class="grid-scroll" tabindex="0" id="grid-scroll"')
    assert '<div class="grid-scroll-top" id="grid-scroll-top" aria-hidden="true" hidden>' in page
    assert "region.scrollLeft = top.scrollLeft" in page
    assert "top.scrollLeft = region.scrollLeft" in page
    assert "top.hidden = region.scrollWidth <= region.clientWidth" in page


def test_the_store_column_and_header_are_fixed_with_the_planned_widths() -> None:
    page = grid_page()
    assert "table.grid thead th{position:sticky;top:0;" in page
    store = re.search(r"table\.grid th\.store-col\{([^}]*)\}", page)
    assert store is not None
    assert "position:sticky;left:0;" in store.group(1)
    for name in ("width", "min-width", "max-width"):
        assert f"{name}:{STORE_WIDTH}px;" in store.group(1), name
    month = re.search(r"table\.grid \.month-col,table\.grid td\{([^}]*)\}", page)
    assert month is not None
    assert (
        f"width:{MONTH_WIDTH}px;min-width:{MONTH_WIDTH}px;max-width:{MONTH_WIDTH}px;"
        in month.group(1)
    )
    # A cell's text and the column totals wrap instead of widening a month.
    assert "table.grid td{white-space:normal;overflow-wrap:break-word;}" in page
    # Twelve months and the store column: 1,612 px, reached by scrolling the grid's region
    # (the owner's decision of 2026-10-22: the grid scrolls at 1,280 px).
    assert STORE_WIDTH + 12 * MONTH_WIDTH == 1612


def test_the_phone_keeps_the_fixed_column_narrower_and_the_sideways_scroll() -> None:
    css = APP_CSS.read_text()
    phone = css.split("@media (max-width: 719.98px)")[1].split("@media print")[0]
    assert (
        "table.grid th.store-col { width: 8rem !important; min-width: 8rem !important; "
        "max-width: 9rem !important;" in phone
    )
    page = grid_page()
    # PLAN-012f D4: the phone shell pads every page alike (16 px); the grid has no margin rule
    # of its own any more, and keeps its own sideways scroll region.
    assert "main.grid-page{" not in page
    assert '<div class="grid-scroll" tabindex="0" id="grid-scroll"' in page
    assert "main, main.wide { padding: var(--space-4); }" in css
