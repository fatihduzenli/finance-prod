"""The portal's link pages and the styled CSRF failure (owner, staging, 2026-11-03).

Chrome sent ``Origin: null`` with the set-password form under ``Referrer-Policy: no-referrer``
and Django refused it with its plain page. The invite and reset-confirm pages now send
``same-origin`` (no-store and noindex as before, nothing loaded from another host, the CSRF cookie
set on the first GET); a form that fails the check gets the 403 of its shell with "Try again"
(``finance.errors``), Django's reason logged without the path and never shown; ``handler403`` is
the styled page too. The browsers are in ``tests/e2e/test_portal_invite_flow.py``.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory
from django.urls import reverse

from accounts import registry
from accounts.roles import Role
from finance import errors
from finance import urls as finance_urls
from portal import tokens
from portal.models import PortalToken, PortalUser, TokenKind
from tests.auth_helpers import iter_routes, sample_path
from tests.test_operator_views import client_for
from tests.test_portal import World, active_user, portal_client, text
from tests.test_portal import clock as clock  # noqa: PLC0414 (the fixture, re-exported)
from tests.test_portal import world as world  # noqa: PLC0414 (the fixture, re-exported)

pytestmark = pytest.mark.django_db

NEW_PASSWORD = "portal-test-only-Csrf-58"
#: Django's own words, and its reasons: none may reach the page.
DJANGO_WORDS = (
    "CSRF verification failed",
    "Forbidden",
    "Origin checking",
    "Referer checking",
    "CSRF cookie",
    "CSRF token",
    "trusted origins",
)


def invited(world: World) -> tuple[PortalUser, str]:
    user = PortalUser.objects.create(operator=world.a, email=str(world.a.email))
    _, raw = tokens.issue(user, TokenKind.INVITE)
    return user, f"/portal/invite/{raw}/"


def reset_link(world: World) -> tuple[PortalUser, str]:
    user = active_user(world.a)
    _, raw = tokens.issue(user, TokenKind.RESET)
    return user, f"/portal/reset/{raw}/"


def csrf_token(page: Any) -> str:
    found = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text(page))
    assert found is not None
    return found.group(1)


def assert_styled_portal_failure(response: Any, again: str) -> str:
    html = text(response)
    assert response.status_code == 403
    assert 'class="portal"' in html and 'id="portal-bar"' in html  # the portal shell
    assert 'id="form-failed"' in html and "Something went wrong with the form" in html
    assert f'id="form-failed-again" href="{again}">Try again</a>' in html
    for words in DJANGO_WORDS:
        assert words not in html, words
    assert response["X-Robots-Tag"] == "noindex, nofollow"
    return html


# --- The headers on the link pages ----------------------------------------------------------


@pytest.mark.parametrize("make", [invited, reset_link], ids=["invite", "reset-confirm"])
def test_the_link_pages_send_same_origin_no_store_noindex_and_set_the_csrf_cookie(
    world: World, make: Any
) -> None:
    _, path = make(world)
    page: Any = Client(enforce_csrf_checks=True).get(path)  # a fresh context: no cookie
    assert page.status_code == 200
    assert page["Referrer-Policy"] == "same-origin"
    assert "no-referrer" not in str(page.headers)
    assert "no-store" in page["Cache-Control"]
    assert page["X-Robots-Tag"] == "noindex, nofollow"
    assert "csrftoken" in page.cookies and page.cookies["csrftoken"].value
    # A refused token: the same header.
    gone: Any = Client().get(path.rstrip("/").rsplit("/", 1)[0] + "/not-a-token/")
    assert gone.status_code == 404 and gone["Referrer-Policy"] == "same-origin"


@pytest.mark.parametrize("make", [invited, reset_link], ids=["invite", "reset-confirm"])
def test_the_link_pages_load_nothing_from_another_host(world: World, make: Any) -> None:
    _, path = make(world)
    html = text(Client().get(path))
    addresses = re.findall(r"https?://[^\s\"'<>)]+", html)
    addresses += re.findall(r"(?:src|href|action)\s*=\s*[\"'](//[^\"']+)", html)
    for address in addresses:
        assert urlsplit(address).hostname in ("testserver", None), address
    # Every asset and the form are this site's own paths.
    for target in re.findall(r"(?:src|href|action)\s*=\s*[\"']([^\"']*)", html):
        assert target == "" or target.startswith("/") or target.startswith("#"), target


def test_the_invite_form_signs_in_with_this_sites_origin_and_never_with_origin_null(
    world: World,
) -> None:
    user, path = invited(world)
    # Chrome under the old policy: Origin null. The styled page; the link still works.
    chrome_then = Client(enforce_csrf_checks=True)
    token = csrf_token(chrome_then.get(path))
    refused = chrome_then.post(
        path,
        {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD, "csrfmiddlewaretoken": token},
        HTTP_ORIGIN="null",
    )
    assert_styled_portal_failure(refused, path)
    user.refresh_from_db()
    assert user.activated_at is None
    # The page's own origin (what same-origin lets the browser send): signed in.
    now = Client(enforce_csrf_checks=True)
    token = csrf_token(now.get(path))
    done = now.post(
        path,
        {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD, "csrfmiddlewaretoken": token},
        HTTP_ORIGIN="http://testserver",
        HTTP_REFERER=f"http://testserver{path}",
    )
    assert done.status_code == 302 and done["Location"] == "/portal/"
    assert now.get("/portal/").status_code == 200


# --- The styled failure ---------------------------------------------------------------------


@pytest.mark.parametrize("make", [invited, reset_link], ids=["invite", "reset-confirm"])
def test_a_post_without_a_token_to_a_link_page_gets_the_portal_page_and_logs_no_token(
    world: World, make: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _, path = make(world)
    raw = path.rstrip("/").rsplit("/", 1)[1]
    with caplog.at_level(logging.WARNING):
        response = Client(enforce_csrf_checks=True).post(
            path, {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
        )
    html = assert_styled_portal_failure(response, path)
    assert 'id="store-switcher"' not in html  # signed out: the bar holds the company only
    # Logged: the reason and the route, never the path and its token.
    assert "CSRF verification failed (CSRF cookie not set.)" in caplog.text
    assert raw not in caplog.text and "Forbidden (" not in caplog.text
    # Nothing was used.
    found = PortalToken.objects.get(token_hash=tokens.digest(raw))
    assert found.used_at is None


def test_try_again_goes_to_the_page_the_form_was_on(world: World) -> None:
    user = active_user(world.a)
    signed = portal_client(user)
    signed.handler.enforce_csrf_checks = True
    store = f"/portal/stores/{world.store_a.pk}/"
    # The theme form on a store page posts to Account: back to the store page.
    response = signed.post(
        "/portal/account/", {"theme": "dark"}, HTTP_REFERER=f"http://testserver{store}"
    )
    html = assert_styled_portal_failure(response, store)
    assert 'id="store-switcher"' in html  # signed in: the operator's own bar
    # Sign out answers POST only: no Referer, Home.
    assert_styled_portal_failure(signed.post("/portal/logout/"), "/portal/")
    assert_styled_portal_failure(signed.post("/portal/account/password/"), "/portal/")
    # Without a Referer, a page that answers GET is opened again, its query kept.
    pay = f"/portal/stores/{world.store_a.pk}/pay/?charge=1"
    assert_styled_portal_failure(signed.post(pay), pay)
    # Another site, or a staff page, is never the target.
    for referer in ("https://evil.example.com/portal/", "http://testserver/operators/"):
        response = signed.post("/portal/account/", {"theme": "dark"}, HTTP_REFERER=referer)
        assert_styled_portal_failure(response, "/portal/account/")


def test_the_staff_shell_gets_the_staff_error_card_with_try_again() -> None:
    client, _ = client_for(Role.BILLING)
    client.handler.enforce_csrf_checks = True
    create = reverse("ledger:operators:create")
    response: Any = client.post(create, {}, HTTP_REFERER="http://testserver/operators/")
    html = text(response)
    assert response.status_code == 403
    assert 'id="error-page"' in html and "Something went wrong with the form" in html
    assert 'id="error-try-again" href="/operators/">Try again</a>' in html
    assert 'id="error-dashboard"' in html  # signed in: the staff shell
    assert 'class="portal"' not in html
    for words in DJANGO_WORDS:
        assert words not in html, words
    # No Referer, or another site's, or a portal page: the dashboard.
    for referer in ("", "https://evil.example.com/operators/", "http://testserver/portal/"):
        again = text(client.post(create, {}, headers={"referer": referer} if referer else {}))
        assert 'id="error-try-again" href="/">Try again</a>' in again, referer


def test_the_staff_sign_in_form_failure_is_styled_without_the_staff_shell() -> None:
    response: Any = Client(enforce_csrf_checks=True).post(
        "/account/login/", {"auth-username": "x", "auth-password": "y"}
    )
    html = text(response)
    assert response.status_code == 403
    assert 'id="error-page"' in html and 'id="error-try-again" href="/">Try again</a>' in html
    assert 'id="error-dashboard"' not in html
    for words in DJANGO_WORDS:
        assert words not in html, words


def test_the_portal_post_only_routes_are_exactly_the_ones_that_refuse_get(world: World) -> None:
    signed = portal_client(active_user(world.a))
    refusing = set()
    for name, raw, _ in iter_routes():
        if registry.is_portal_route(name):
            if signed.get(sample_path(raw)).status_code == 405:
                refusing.add(name)
    assert refusing == errors.PORTAL_POST_ONLY_ROUTE_NAMES


# --- handler403 -----------------------------------------------------------------------------


def test_handler403_is_the_styled_page_in_both_shells(world: World) -> None:
    assert finance_urls.handler403 == "finance.errors.permission_denied"
    staff = RequestFactory().get("/operators/")
    staff.user = AnonymousUser()
    response = errors.permission_denied(staff)
    assert response.status_code == 403
    html = response.content.decode()
    assert 'id="error-page"' in html and "Not open to your role" in html
    assert "403 Forbidden" not in html
    portal = RequestFactory().get("/portal/stores/1/")
    portal.user = AnonymousUser()
    hidden = errors.permission_denied(portal)
    assert hidden.status_code == 404 and 'id="not-found"' in hidden.content.decode()
