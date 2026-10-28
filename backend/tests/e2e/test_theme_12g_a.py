"""PLAN-012g Batch A in a browser (Fable): the theme paints on the first frame with no script
(System follows the browser's scheme; a stored choice wins over it), the control in the sidebar
and the More sheet saves and comes back, and print media stays light in a dark session (the P&L
and the invoice preview). The QA shots (``QA_SHOTS_DIR``) come from here."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, ViewportSize
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from accounts.themes import set_theme
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

LAPTOP: ViewportSize = {"width": 1280, "height": 800}
PHONE: ViewportSize = {"width": 390, "height": 844}
DARK_BG = "rgb(23, 23, 33)"  # #171721
LIGHT_BG = "rgb(244, 246, 249)"  # #F4F6F9
WHITE = "rgb(255, 255, 255)"


def shot(page: Page, name: str) -> None:
    folder = os.environ.get("QA_SHOTS_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=False)


def context(
    browser: Browser, viewport: ViewportSize, scheme: str, phone: bool = False
) -> BrowserContext:
    return browser.new_context(
        viewport=viewport,
        color_scheme=scheme,  # type: ignore[arg-type]
        device_scale_factor=3 if phone else 1,
        is_mobile=phone,
        has_touch=phone,
    )


def sign_in(ctx: BrowserContext, live_server: LiveServer, username: str, role: Role) -> Page:
    _, device = make_user(username, role)
    assert device is not None
    page = ctx.new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", username)
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Next").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


def body_bg(page: Page) -> str:
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


@pytest.fixture
def scheme_contexts(browser: Browser) -> Iterator[dict[str, BrowserContext]]:
    made = {s: context(browser, LAPTOP, s) for s in ("light", "dark")}
    yield made
    for ctx in made.values():
        ctx.close()


def test_system_follows_the_browser_and_a_stored_choice_wins(
    live_server: LiveServer, scheme_contexts: dict[str, BrowserContext]
) -> None:
    # Anonymous: the sign-in shell is System, so the dark browser gets dark, the light one light.
    for scheme, expected in (("light", LIGHT_BG), ("dark", DARK_BG)):
        page = scheme_contexts[scheme].new_page()
        page.goto(f"{live_server.url}/account/login/")
        assert page.get_attribute("html", "data-theme") == "system"
        assert body_bg(page) == expected, scheme
        shot(page, f"login-{scheme}")
        page.close()
    # Signed in with no choice: still System. Then Light, saved on the server, wins in the dark
    # browser.
    page = sign_in(scheme_contexts["dark"], live_server, "e2e-12g-a-dark", Role.BILLING)
    assert page.get_attribute("html", "data-theme") == "system" and body_bg(page) == DARK_BG
    shot(page, "dashboard-system-dark")
    page.locator("#theme-switch button[value=light]").first.click()
    page.wait_for_load_state("networkidle")
    assert page.get_attribute("html", "data-theme") == "light" and body_bg(page) == LIGHT_BG
    assert page.locator('#theme-switch button[value=light][aria-pressed="true"]').count() >= 1
    # Dark, chosen in the light browser, wins there too.
    other = sign_in(scheme_contexts["light"], live_server, "e2e-12g-a-light", Role.ADMIN)
    other.locator("#theme-switch button[value=dark]").first.click()
    other.wait_for_load_state("networkidle")
    assert other.get_attribute("html", "data-theme") == "dark" and body_bg(other) == DARK_BG
    shot(other, "dashboard-dark-chosen")
    # No script is needed: the theme is right on a fresh load with scripts off.
    quiet = scheme_contexts["light"].browser.new_context(viewport=LAPTOP, java_script_enabled=False)  # type: ignore[union-attr]
    try:
        user, device = make_user("e2e-12g-a-noscript", Role.VIEWER)
        assert device is not None
        set_theme(user, "dark")
        quiet_page = quiet.new_page()
        quiet_page.goto(f"{live_server.url}/account/login/")
        quiet_page.fill("#id_auth-username", "e2e-12g-a-noscript")
        quiet_page.fill("#id_auth-password", TEST_PASSWORD)
        quiet_page.get_by_role("button", name="Next").click()
        quiet_page.locator("#id_token-otp_token").fill(current_code(device))
        quiet_page.get_by_role("button", name="Next").click()
        quiet_page.wait_for_url(f"{live_server.url}/")
        assert body_bg(quiet_page) == DARK_BG
    finally:
        quiet.close()


def test_the_more_sheet_carries_the_control_on_the_phone(
    live_server: LiveServer, browser: Browser
) -> None:
    ctx = context(browser, PHONE, "light", phone=True)
    try:
        page = sign_in(ctx, live_server, "e2e-12g-a-phone", Role.BILLING)
        page.locator("#tab-more").click()
        sheet = page.locator("#more-sheet .more-appearance")
        assert sheet.is_visible() and "Appearance" in sheet.inner_text()
        shot(page, "more-sheet-appearance-phone")
        sheet.locator("button[value=dark]").click()
        page.wait_for_load_state("networkidle")
        assert page.get_attribute("html", "data-theme") == "dark" and body_bg(page) == DARK_BG
        shot(page, "dashboard-dark-phone")
    finally:
        ctx.close()


def test_print_media_stays_light_in_a_dark_session(
    live_server: LiveServer, browser: Browser
) -> None:
    ctx = context(browser, LAPTOP, "dark")
    try:
        page = sign_in(ctx, live_server, "e2e-12g-a-print", Role.ADMIN)
        page.locator("#theme-switch button[value=dark]").first.click()
        page.wait_for_load_state("networkidle")
        page.goto(f"{live_server.url}/reports/pnl/")
        assert body_bg(page) == DARK_BG
        page.emulate_media(media="print")
        assert body_bg(page) in (WHITE, LIGHT_BG)
        assert (
            page.evaluate("getComputedStyle(document.body).color") == "rgb(11, 37, 69)"
        )  # --text light
        page.emulate_media(media="screen")
        assert body_bg(page) == DARK_BG
    finally:
        ctx.close()
