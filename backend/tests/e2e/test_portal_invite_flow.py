"""The portal's one-time links in real browsers, Chromium and WebKit (owner, staging, 2026-11-03:
Chrome refused the set-password form with Django's CSRF page because ``Referrer-Policy:
no-referrer`` made it send ``Origin: null``).

An admin signs in on the staff side, invites an operator with an e-mail and a current lease,
presses "Copy invite link" and reads the link from the dialog; a new context with no cookies
opens the link, chooses a password, lands on the portal's Home with the store, signs out and
signs in again with the e-mail and the password. The reset-confirm page's form, the same header
path, is sent from a fresh context too. Each form's POST carries this site's ``Origin``.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from django.contrib.auth.hashers import make_password
from django.utils import timezone
from playwright.sync_api import Browser, Error, Page, Playwright, Request
from pytest_django.live_server_helper import LiveServer

from accounts.roles import Role
from ledger.models import MessageTemplate, Operator
from portal import tokens
from portal.models import PortalUser, TokenKind
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user
from tests.receivables_helpers import a_lease, a_store, an_operator

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

PASSWORD = "e2e-only invite flow passphrase 2026"
NEW_PASSWORD = "e2e-only reset flow passphrase 2026"
EMAIL = "invite.flow@example.com"
BROWSERS = ["chromium", "webkit"]


@pytest.fixture(params=BROWSERS)
def any_browser(request: pytest.FixtureRequest, playwright: Playwright) -> Iterator[Browser]:
    name: str = request.param
    try:
        browser = getattr(playwright, name).launch()
    except Error as error:
        pytest.skip(f"{name} is not installed (uv run playwright install {name}): {error}")
    yield browser
    browser.close()


@pytest.fixture(autouse=True)
def links_on_this_server(settings: Any, live_server: LiveServer) -> None:
    """The copied link names the live server, so the browser opens it as it is."""
    settings.APP_BASE_URL = live_server.url


def seeded_templates() -> None:
    """The portal's two e-mail templates (a transactional test flushes the migration's seed)."""
    seed = importlib.import_module("ledger.migrations.0064_portal_messages")
    for code, name, subject, body in seed.TEMPLATES:
        MessageTemplate.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "internal": False,
                "subject": subject,
                "body": body,
                "channel": "email",
                "active": True,
            },
        )


def an_invitable_operator() -> Operator:
    operator = an_operator("Invite Flow")
    operator.email = EMAIL
    operator.save()
    a_lease(a_store("1901", number=1901), operator, started=date(2026, 1, 1))
    return operator


def staff_sign_in(browser: Browser, live_server: LiveServer) -> Page:
    _, device = make_user("e2e-invite-flow-admin", Role.ADMIN)
    assert device is not None
    page = browser.new_context().new_page()
    page.goto(f"{live_server.url}/account/login/")
    page.fill("#id_auth-username", "e2e-invite-flow-admin")
    page.fill("#id_auth-password", TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    page.locator("#id_token-otp_token").fill(current_code(device))
    page.get_by_role("button", name="Next").click()
    page.wait_for_url(f"{live_server.url}/")
    return page


def watch_posts(page: Page) -> list[Request]:
    posts: list[Request] = []
    page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
    return posts


def choose_password(page: Page, password: str) -> None:
    page.fill("#new-password", password)
    page.fill("#confirm-password", password)
    page.get_by_role("button", name="Save and sign in").click()


def assert_sent_with_this_origin(posts: list[Request], path: str, live_server: LiveServer) -> None:
    sent = [request for request in posts if request.url == f"{live_server.url}{path}"]
    assert len(sent) == 1, [request.url for request in posts]
    assert sent[0].headers.get("origin") == live_server.url  # never "null"


def test_the_copied_invite_link_sets_the_password_and_signs_in(
    live_server: LiveServer, any_browser: Browser
) -> None:
    seeded_templates()
    operator = an_invitable_operator()
    staff = staff_sign_in(any_browser, live_server)
    staff.goto(f"{live_server.url}/operators/{operator.pk}/")
    with staff.expect_navigation():
        staff.locator("#portal-invite-form button").click()
    assert staff.url == f"{live_server.url}/operators/{operator.pk}/#portal-card"
    assert "Invited, not accepted yet" in staff.locator("#portal-state").inner_text()
    with staff.expect_navigation():
        staff.locator("#portal-copy-link-form button").click()
    field = staff.locator("#portal-copied-link-field")
    field.wait_for()
    link = field.input_value()
    match = re.fullmatch(re.escape(live_server.url) + r"(/portal/invite/[A-Za-z0-9_-]+/)", link)
    assert match is not None, link
    path = match.group(1)
    staff.context.close()

    # A new context: no cookie of any kind.
    operator_context = any_browser.new_context()
    assert operator_context.cookies() == []
    page = operator_context.new_page()
    posts = watch_posts(page)
    opened = page.goto(link)
    assert opened is not None and opened.status == 200
    assert opened.headers["referrer-policy"] == "same-origin"
    assert "csrftoken" in {cookie["name"] for cookie in operator_context.cookies()}
    assert page.locator("#link-email").inner_text() == EMAIL
    choose_password(page, PASSWORD)
    page.wait_for_url(f"{live_server.url}/portal/")
    assert_sent_with_this_origin(posts, path, live_server)
    body = page.locator("main").inner_text()
    assert "Something went wrong" not in body
    assert re.search(r"\b1901\b", body), body
    page.get_by_role("button", name=re.compile("Sign out")).first.click()
    page.wait_for_url(re.compile(r"/portal/login/"))
    page.fill("input[name=email]", EMAIL)
    page.fill("input[name=password]", PASSWORD)
    page.get_by_role("button", name=re.compile("Sign in")).click()
    page.wait_for_url(f"{live_server.url}/portal/")
    assert re.search(r"\b1901\b", page.locator("main").inner_text())
    user = PortalUser.objects.get(operator=operator)
    assert user.activated_at is not None and user.is_active
    operator_context.close()


def test_the_reset_link_form_is_accepted_from_a_fresh_context(
    live_server: LiveServer, any_browser: Browser
) -> None:
    operator = an_invitable_operator()
    user = PortalUser.objects.create(
        operator=operator,
        email=EMAIL,
        is_active=True,
        invited_at=timezone.now(),
        activated_at=timezone.now(),
        password=make_password(PASSWORD),
    )
    _, raw = tokens.issue(user, TokenKind.RESET)
    path = f"/portal/reset/{raw}/"
    context = any_browser.new_context()
    page = context.new_page()
    posts = watch_posts(page)
    opened = page.goto(f"{live_server.url}{path}")
    assert opened is not None and opened.status == 200
    assert opened.headers["referrer-policy"] == "same-origin"
    choose_password(page, NEW_PASSWORD)
    page.wait_for_url(f"{live_server.url}/portal/")
    assert_sent_with_this_origin(posts, path, live_server)
    assert re.search(r"\b1901\b", page.locator("main").inner_text())
    context.close()


def test_a_form_that_fails_the_check_gets_the_portal_page_with_try_again(
    live_server: LiveServer, any_browser: Browser
) -> None:
    operator = an_invitable_operator()
    user = PortalUser.objects.create(operator=operator, email=EMAIL, invited_at=timezone.now())
    _, raw = tokens.issue(user, TokenKind.INVITE)
    path = f"/portal/invite/{raw}/"
    context = any_browser.new_context()
    page = context.new_page()
    page.goto(f"{live_server.url}{path}")
    context.clear_cookies()  # the CSRF cookie is gone, as after a long pause
    choose_password(page, PASSWORD)
    heading = page.locator("#form-failed h1")
    heading.wait_for()
    assert heading.inner_text() == "Something went wrong with the form"
    assert "CSRF" not in page.locator("body").inner_text()
    page.locator("#form-failed-again").click()
    page.wait_for_url(f"{live_server.url}{path}")
    choose_password(page, PASSWORD)
    page.wait_for_url(f"{live_server.url}/portal/")
    context.close()
