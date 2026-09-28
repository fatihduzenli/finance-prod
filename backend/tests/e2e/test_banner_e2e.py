"""The sandbox banner as a browser sees it (plan §5, e2e)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page
from pytest_django.live_server_helper import LiveServer

pytestmark = [pytest.mark.e2e, pytest.mark.django_db]

BANNER_TEXT = "SANDBOX — NO REAL MONEY"


def test_banner_is_visible_and_cannot_be_dismissed(live_server: LiveServer, page: Page) -> None:
    # PLAN-003: the two-factor login is the login page (/admin/login/ answers 401).
    page.goto(f"{live_server.url}/account/login/")

    banner = page.locator("#sandbox-banner")
    banner.wait_for(state="visible")
    assert banner.count() == 1
    assert banner.inner_text().strip() == BANNER_TEXT
    assert page.title().startswith("[SANDBOX] ")

    viewport = page.viewport_size
    assert viewport is not None
    box = banner.bounding_box()
    assert box is not None
    assert box["y"] == pytest.approx(0, abs=1)
    assert box["x"] == pytest.approx(0, abs=1)
    assert box["width"] == pytest.approx(viewport["width"], abs=2)

    # The login form must not be hidden underneath the banner.
    username_box = page.locator("#id_auth-username").bounding_box()
    assert username_box is not None
    assert username_box["y"] > box["y"] + box["height"]

    # Clicking the banner, and elsewhere, leaves it in place.
    page.mouse.click(box["width"] / 2, box["height"] / 2)
    page.mouse.click(5, viewport["height"] - 5)
    page.keyboard.press("Escape")
    assert banner.is_visible()
    assert banner.inner_text().strip() == BANNER_TEXT
    assert page.title().startswith("[SANDBOX] ")
