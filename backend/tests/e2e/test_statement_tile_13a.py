"""PLAN-013a C7 (12f-a C-5) in a browser: the statement step's Deductions tile follows the
amounts typed in the deduction rows (ledger/table_total.js, red above zero); with scripts
off it keeps the server's value.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.urls import reverse
from playwright.sync_api import Browser, BrowserContext
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import PaymentChannel
from tests.e2e.test_pages_12f_d import LAPTOP, signed_in
from tests.receivables_helpers import a_lease, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

TILE = "#statement-tiles .kpi-value.js-total-out"
TYPED = "#deductions-typed-total .js-total-out"


@pytest.fixture
def laptop(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport=LAPTOP)
    yield context
    context.close()


@pytest.fixture
def no_script(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(viewport=LAPTOP, java_script_enabled=False)
    yield context
    context.close()


def statement_path() -> str:
    from tests.test_lease_end import draft

    lease = a_lease(a_store("0914 TILE", 914), an_operator("Tile Co"), channel=PaymentChannel.WIRE)
    return reverse("ledger:lease_ends:statement", kwargs={"pk": draft(lease).pk})


def amount(index: int) -> str:
    return f"#deduction-rows tr.deduction-row:nth-child({index}) input.js-total"


def test_the_deductions_tile_follows_the_typed_amounts(
    live_server: LiveServer, laptop: BrowserContext
) -> None:
    path = statement_path()
    page = signed_in(laptop, live_server, Role.ADMIN)
    page.goto(f"{live_server.url}{path}")
    tile = page.locator(TILE)
    assert tile.inner_text() == "$0.00"
    assert "tone-unpaid" not in (tile.get_attribute("class") or "")
    page.locator(amount(1)).fill("125.50")
    assert tile.inner_text() == "$125.50" == page.locator(TYPED).inner_text()
    assert "tone-unpaid" in (tile.get_attribute("class") or "")
    page.locator("#add-deduction").click()
    page.locator(amount(2)).fill("1000")
    assert tile.inner_text() == "$1,125.50"
    # An amount not above zero counts as nothing, as in the typed total under the table.
    page.locator(amount(2)).fill("-5")
    assert tile.inner_text() == "$125.50"
    page.locator(amount(1)).fill("")
    page.locator(amount(2)).fill("")
    assert tile.inner_text() == "$0.00"
    assert "tone-unpaid" not in (tile.get_attribute("class") or "")


def test_without_the_script_the_tile_keeps_the_servers_value(
    live_server: LiveServer, laptop: BrowserContext, no_script: BrowserContext
) -> None:
    path = statement_path()
    page = signed_in(laptop, live_server, Role.ADMIN)
    plain = no_script.new_page()
    plain.context.add_cookies(
        [
            {"name": cookie["name"], "value": cookie["value"], "url": live_server.url}
            for cookie in page.context.cookies()
        ]
    )
    plain.goto(f"{live_server.url}{path}")
    plain.locator(amount(1)).fill("125.50")
    assert plain.locator(TILE).inner_text() == "$0.00"
