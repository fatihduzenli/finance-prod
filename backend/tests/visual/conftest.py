"""The visual regression suite (docs/qa/PLAN-012c-design-pass.md D10, §3.7).

Never part of a default run: a test marked ``visual`` is deselected unless the ``-m``
expression names ``visual`` (as the sandbox suite does), so the documented ``-m "not e2e"``
and ``-m e2e`` runs never collect it. Without Chromium (``uv run playwright install
chromium``) every test skips.

One data set for the whole session (``world.py``), written once with the server's clock
frozen at 2026-10-20 12:00 New York time and counted ids; one live server; a billing user
signed in through the real login (password, then the TOTP code), and an admin for the
admin-only lease-end wizard; one browser context per user with a fixed locale, time zone,
device scale factor 1, reduced motion, no service worker, and its clock fixed at the same
instant (``BrowserContext.clock.set_fixed_time``).

The live server here is started by this file, not by pytest-django's ``live_server``
fixture: that fixture makes every test using it a transactional test, which empties the
database after each test, and the data set is written once per session. The database is
emptied at the end of the session instead.

Options (this directory's conftest is loaded when the command names the directory):
``--update-baseline`` writes the screenshots as the new baseline instead of comparing;
``--baseline-dir`` reads and writes the baseline elsewhere than ``baseline/``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connections
from playwright.sync_api import Browser, BrowserContext, Error, Page, Playwright, ViewportSize
from pytest_django import DjangoDbBlocker
from pytest_django.live_server_helper import LiveServer

import documents_storage
from documents_storage.memory import MemoryDocumentStorage
from payments.fake import FakeProvider
from tests.auth_helpers import TEST_PASSWORD, current_code
from tests.visual import world

# Playwright's sync API runs an event loop in this thread; see tests/e2e/conftest.py.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

VISUAL_MARKER = "visual"
HERE = Path(__file__).resolve().parent
DEFAULT_BASELINE = HERE / "baseline"
OUT = HERE / "out"
DESKTOP: ViewportSize = {"width": 1280, "height": 800}
#: Animations, transitions and the text caret off: nothing moves between two shots.
STILL = (
    "*,*::before,*::after{animation:none!important;transition:none!important;"
    "caret-color:transparent!important}"
)
COMMAND = "uv run pytest backend/tests/visual -m visual --update-baseline"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("visual", "visual regression (backend/tests/visual)")
    group.addoption(
        "--update-baseline",
        action="store_true",
        default=False,
        help="write the screenshots as the new baseline instead of comparing them",
    )
    group.addoption(
        "--baseline-dir",
        default=str(DEFAULT_BASELINE),
        help="the baseline folder (default: backend/tests/visual/baseline)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if VISUAL_MARKER in (config.getoption("markexpr") or ""):
        return
    deselected = [item for item in items if item.get_closest_marker(VISUAL_MARKER)]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if not item.get_closest_marker(VISUAL_MARKER)]


@dataclass(frozen=True)
class Baseline:
    folder: Path
    update: bool
    out: Path = OUT


@pytest.fixture(scope="session")
def baseline(pytestconfig: pytest.Config) -> Baseline:
    return Baseline(
        folder=Path(pytestconfig.getoption("--baseline-dir")).resolve(),
        update=bool(pytestconfig.getoption("--update-baseline")),
    )


@pytest.fixture(scope="session")
def visual_browser(playwright: Playwright) -> Iterator[Browser]:
    try:
        browser = playwright.chromium.launch()
    except Error as error:
        pytest.skip(f"Chromium is not installed (uv run playwright install chromium): {error}")
    yield browser
    browser.close()


@dataclass(frozen=True)
class Site:
    url: str
    world: world.World


@pytest.fixture(scope="session")
def visual_site(
    django_db_setup: None,
    django_db_blocker: DjangoDbBlocker,
    visual_browser: Browser,
) -> Iterator[Site]:
    """The data set and a live server over it, for the whole session; the server's clock
    stays frozen at :data:`world.NOON` until the end. pytest-django creates the test
    database only for a collected test marked ``django_db``: the page tests carry the mark."""
    with django_db_blocker.unblock(), pytest.MonkeyPatch.context() as patch:
        clock = world.FrozenClock()
        patch.setattr("django.utils.timezone.now", clock)
        patch.setattr("payments.get_provider", FakeProvider)
        patch.setattr(documents_storage, "get_storage", MemoryDocumentStorage)
        built = world.build()
        clock.stop()
        server = LiveServer("localhost")
        server._live_server_modified_settings.enable()
        try:
            yield Site(server.url, built)
        finally:
            server._live_server_modified_settings.disable()
            # Stops the server thread and joins it; the thread closes its own connections
            # on the way out (LiveServerThread.run), the request threads after each request.
            server.stop()
            call_command("flush", interactive=False, verbosity=0)
            # This fixture's connections (opened while Playwright runs, so in its context)
            # are closed here, before pytest-django's django_db_setup finalizer (this
            # fixture depends on it) drops the test database: PostgreSQL refuses to drop a
            # database another session still holds.
            connections.close_all()


def sign_in(page: Page, site: Site, user: world.SignIn) -> None:
    """The real login: the password, then the TOTP code of the user's device."""
    page.goto(f"{site.url}/account/login/")
    page.fill("#id_auth-username", user.username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    token = page.locator("#id_token-otp_token")
    token.wait_for(state="visible")
    token.fill(current_code(user.device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{site.url}/")


@pytest.fixture(scope="session")
def visual_pages(visual_site: Site, visual_browser: Browser) -> Iterator[Callable[[str], Page]]:
    """``page_as(role)``: a page signed in as the data set's user of ``role`` ("billing", or
    "admin" for the admin-only lease-end wizard), one browser context each. Both sign in
    here, before any test: a page test runs in a transaction that is rolled back after it
    (the ``django_db`` mark), and on SQLite the live server shares that connection, so a
    session row written during a test would not outlive it."""
    opened: dict[str, tuple[BrowserContext, Page]] = {}
    for role, user in visual_site.world.sign_ins.items():
        context = visual_browser.new_context(
            viewport=DESKTOP,
            device_scale_factor=1,
            locale="en-US",
            timezone_id="America/New_York",
            reduced_motion="reduce",
            color_scheme="light",
            service_workers="block",
        )
        context.clock.set_fixed_time(world.NOON)
        page = context.new_page()
        sign_in(page, visual_site, user)
        opened[role] = (context, page)

    def page_as(role: str) -> Page:
        return opened[role][1]

    yield page_as
    for context, _ in opened.values():
        context.close()


def open_still(page: Page, url: str) -> None:
    """Open ``url``, wait for the network and the fonts, and stop anything that moves."""
    response = page.goto(url, wait_until="networkidle")
    assert response is not None and response.status == 200, (url, response and response.status)
    page.add_style_tag(content=STILL)
    page.evaluate("document.fonts.ready.then(() => true)")
    page.mouse.move(0, 0)
