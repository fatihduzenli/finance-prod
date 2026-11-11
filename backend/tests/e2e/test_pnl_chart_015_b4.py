"""PLAN-015 Batch 4 in a browser (Chromium; WebKit in the CI image only): hovering a month of
the Profit and loss chart shows the tooltip of ledger/charts.js with the month, Collected,
Costs and Margin from the month's data-* attributes; leaving the chart hides it. The
dashboard's own tooltip is tests/e2e/test_dashboard_v2.py's (unchanged)."""

from __future__ import annotations

from datetime import date

import pytest
from playwright.sync_api import BrowserContext, expect
from pytest_django.live_server_helper import LiveServer

from tests import test_pnl as pnl_world
from tests.e2e.test_dashboard_v2 import any_browser, laptop, shot, signed_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

__all__ = ["any_browser", "laptop"]  # the fixtures, used by name


def test_hovering_a_pnl_month_shows_collected_costs_and_margin(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    store = pnl_world.a_store()
    lease = pnl_world.a_lease(store)
    pnl_world.a_cost(store)
    pnl_world.paid(pnl_world.rent(lease, pnl_world.JULY), "3500.00", date(2026, 7, 5))
    pnl_world.paid(pnl_world.rent(lease, pnl_world.AUGUST), "2000.00", date(2026, 8, 5))
    page = signed_in(laptop, live_server, "e2e-b4-pnl-hover")
    page.goto(f"{live_server.url}/reports/pnl/?from=2026-07&to=2026-09")
    tooltip = page.locator("#pnl-tooltip")
    assert not tooltip.is_visible()
    months = page.locator("#pnl-chart .pnl-month")
    assert months.count() == 3
    months.nth(1).locator("rect").first.hover()
    expect(tooltip).to_be_visible()
    text = " ".join(tooltip.inner_text().split())
    assert text == "Aug 2026 Collected $2,000.00 Costs $2,500.00 Margin -$500.00", text
    assert page.locator("#pnl-chart.is-hovering .pnl-month.is-hover").count() == 1
    shot(page, "pnl-chart-tooltip-dark-laptop")
    page.mouse.move(5, 5)
    expect(tooltip).to_be_hidden()
