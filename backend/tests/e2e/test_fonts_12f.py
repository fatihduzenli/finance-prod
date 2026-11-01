"""The fonts load from the app's origin only (PLAN-012f D2), by the QA owner: an e2e case
lists every font request's host while the grid page loads and asserts it is the app's."""

from __future__ import annotations

import pytest

from accounts.roles import Role


@pytest.mark.e2e
@pytest.mark.django_db(transaction=True)
def test_every_font_request_goes_to_the_app_itself(live_server, browser) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import urlparse

    from tests.auth_helpers import TEST_PASSWORD, current_code, make_user

    _, device = make_user("fonts-viewer", Role.VIEWER)
    assert device is not None
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    hosts: set[str] = set()
    page.on(
        "request",
        lambda request: (
            hosts.add(urlparse(request.url).netloc) if request.resource_type == "font" else None
        ),
    )
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", "fonts-viewer")
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    page.goto(f"{live_server.url}/grid/")
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    context.close()
    own = urlparse(live_server.url).netloc
    assert hosts, "no font was requested: Inter is not loading"
    assert hosts == {own}, hosts
