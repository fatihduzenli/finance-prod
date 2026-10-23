"""The sandbox banner as a browser sees it (plan §5, e2e)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer

pytestmark = [pytest.mark.e2e, pytest.mark.django_db]

BANNER_TEXT = "SANDBOX — test system, no real money"
#: What a browser reads: the approved design sets the banner in upper case (PLAN-012f D3).
SHOWN_TEXT = BANNER_TEXT.upper()


def test_banner_is_visible_and_cannot_be_dismissed(live_server: LiveServer, page: Page) -> None:
    # PLAN-003: the two-factor login is the login page (/admin/login/ answers 401).
    page.goto(f"{live_server.url}/account/login/")

    banner = page.locator("#sandbox-banner")
    banner.wait_for(state="visible")
    assert banner.count() == 1
    assert banner.inner_text().strip() == SHOWN_TEXT
    assert page.title().startswith("[SANDBOX] ")

    viewport = page.viewport_size
    assert viewport is not None
    box = banner.bounding_box()
    assert box is not None
    # PLAN-012f D13: on the sign-in page the banner is the rounded strip over the form in the
    # right panel (login-laptop.png), in view without scrolling; the staff pages keep it as the
    # full-width bar above the page header (tests/e2e/test_shell_12f.py).
    assert box["y"] >= 0 and box["y"] + box["height"] <= viewport["height"]
    assert box["x"] >= viewport["width"] / 2 - 1
    assert box["width"] == pytest.approx(400, abs=2)

    # The login form must not be hidden underneath the banner.
    username_box = page.locator("#id_auth-username").bounding_box()
    assert username_box is not None
    assert username_box["y"] > box["y"] + box["height"]

    # Clicking the banner, and elsewhere, leaves it in place.
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.click(5, viewport["height"] - 5)
    page.keyboard.press("Escape")
    assert banner.is_visible()
    assert banner.inner_text().strip() == SHOWN_TEXT
    assert page.title().startswith("[SANDBOX] ")
