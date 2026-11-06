"""The pages on the sign-in frame (two_factor/_base.html) besides the sign-in form: "Sign in
required" (401), "Two-factor authentication required" (403) and "Signed out everywhere" take the
sign-in page's right-panel composition (two_factor/_auth_head.html: the icon in the teal-tinted
square, the heading, the sentence as the sub-line; the primary action full width in
.auth-actions), keep their words and statuses; and the footer and the "Staff of … only." line
print the brand once when the company name is empty or the brand's own (owner, 2026-11-01:
"Monvera · Monvera" on staging)."""

from __future__ import annotations

import re

import pytest
from django.template.loader import render_to_string
from django.test import Client

from accounts.roles import Role
from finance.context_processors import Company
from finance.templatetags.icons import ICONS
from ledger.models import CompanySettings
from tests.auth_helpers import LOGIN_PATH, make_user, verified_client

pytestmark = pytest.mark.django_db


def main_of(page: str) -> str:
    return page[page.index('<main class="auth-main" id="auth-main">') : page.index("</main>")]


def assert_the_frame_head(page: str, icon: str, heading: str, sub: str) -> None:
    """The icon square holds the named icon, then the one heading, then the sub-line."""
    main = main_of(page)
    square = re.search(r'<span class="auth-lock" aria-hidden="true">(<svg.*?</svg>)</span>', main)
    assert square is not None, main
    assert ICONS[icon] in square.group(1)
    assert f'<h1 id="auth-heading">{heading}</h1>' in main
    assert f'<p class="auth-sub" id="auth-sub">{sub}</p>' in main
    assert main.index("auth-lock") < main.index("<h1") < main.index("auth-sub")
    assert page.count("<h1") == 1
    assert '<body class="auth">' in page and 'id="auth-brand"' in page  # the left panel stays


def actions_of(page: str) -> str:
    main = main_of(page)
    return main.split('<div class="auth-actions">', 1)[1].rsplit("</div>", 1)[0]


def test_the_401_page_takes_the_sign_in_composition_and_keeps_its_next_link() -> None:
    response = Client().get("/stores/")
    assert response.status_code == 401
    page = response.content.decode()
    assert_the_frame_head(
        page,
        "lock",
        "Sign in required",
        "You need to sign in, with your password and your authenticator code, to see this page.",
    )
    assert actions_of(page) == (
        f'<a class="btn btn-primary" id="sign-in-link" href="{LOGIN_PATH}?next=%2Fstores%2F">'
        "Sign in</a>"
    )
    assert "<title>[SANDBOX] Sign in required · Monvera</title>" in page


def test_the_mfa_page_for_a_user_without_a_device_offers_the_setup_over_sign_out() -> None:
    user, _ = make_user("frame-nodevice", Role.VIEWER, with_device=False)
    client = Client()
    client.force_login(user)
    response = client.get("/stores/")
    assert response.status_code == 403
    page = response.content.decode()
    assert_the_frame_head(
        page,
        "lock",
        "Two-factor authentication required",
        "Every account must use an authenticator app. Set one up to continue.",
    )
    actions = actions_of(page)
    assert actions.index('class="btn btn-primary" id="mfa-setup-link"') < actions.index(
        '<form method="post" action="/account/logout/">'
    )
    assert ">Set up two-factor authentication</a>" in actions
    assert '<button class="btn" type="submit">Sign out</button>' in actions


def test_the_mfa_page_for_an_unverified_session_offers_sign_out_alone() -> None:
    user, device = make_user("frame-unverified", Role.VIEWER)
    assert device is not None
    client = Client()
    client.force_login(user)  # the password, not the code
    response = client.get("/stores/")
    assert response.status_code == 403
    page = response.content.decode()
    assert_the_frame_head(
        page,
        "lock",
        "Two-factor authentication required",
        "This session has not been verified with your authenticator code. "
        "Sign out and sign in again.",
    )
    actions = actions_of(page)
    assert "mfa-setup-link" not in actions and "btn-primary" not in actions
    assert '<button class="btn" type="submit">Sign out</button>' in actions


def test_signed_out_everywhere_takes_the_composition_with_a_check() -> None:
    user, device = make_user("frame-everywhere", Role.VIEWER)
    assert device is not None
    response = verified_client(user, device).post("/account/sign-out-everywhere/")
    assert response.status_code == 200
    page = response.content.decode()
    assert_the_frame_head(
        page,
        "check",
        "Signed out everywhere",
        "Every session on every device has been ended, including this one.",
    )
    assert actions_of(page) == f'<a class="btn btn-primary" href="{LOGIN_PATH}">Sign in again</a>'


def test_the_sign_in_page_keeps_its_heading_id_through_the_shared_head() -> None:
    page = Client().get(LOGIN_PATH).content.decode()
    main = main_of(page)
    assert '<span class="auth-lock" aria-hidden="true"><svg' in main
    assert ICONS["lock"] in main.split("</span>", 1)[0]
    assert '<h1 id="sign-in-heading">Sign in to Monvera</h1>' in main
    assert main.count('id="auth-sub"') == 1  # the page's own sub-line, not a second one


def name_company(name: str) -> None:
    row = CompanySettings.current()
    row.name = name
    row.save()


@pytest.mark.parametrize("name", ["Monvera", " monvera ", "MONVERA"])
def test_the_brand_is_printed_once_when_the_name_is_the_brands(name: str) -> None:
    name_company(name)
    login = Client().get(LOGIN_PATH).content.decode()
    assert '<p class="auth-company" id="auth-company">Monvera</p>' in login
    assert '<p class="auth-sub" id="auth-sub">Staff only.</p>' in login
    assert "Monvera · Monvera" not in login and "Staff of" not in login
    # The footer is the frame's: the 401 page says it the same way.
    unauthorised = Client().get("/stores/").content.decode()
    assert '<p class="auth-company" id="auth-company">Monvera</p>' in unauthorised


def line_for(name: str, line: str) -> str:
    company = Company(name=name, short_name="Monvera", provider_name="Stax", logo_url="")
    return render_to_string("two_factor/_company_line.html", {"company": company, "line": line})


@pytest.mark.parametrize("name", ["", "   "])
def test_an_empty_name_prints_the_brand_alone(name: str) -> None:
    # The row refuses a blank name (company_settings_name_not_blank); the partial still copes.
    assert line_for(name, "footer") == "Monvera"
    assert line_for(name, "staff") == "Staff only."


def test_the_partial_prints_another_name_as_given() -> None:
    assert line_for("Global Retail Group", "footer") == "Global Retail Group · Monvera"
    assert line_for("Global Retail Group", "staff") == "Staff of Global Retail Group only."


@pytest.mark.parametrize("name", ["Global Retail Group", "Monvera Holdings", "Monver"])
def test_another_name_is_printed_with_the_brand(name: str) -> None:
    name_company(name)
    login = Client().get(LOGIN_PATH).content.decode()
    assert f'<p class="auth-company" id="auth-company">{name} · Monvera</p>' in login
    assert f'<p class="auth-sub" id="auth-sub">Staff of {name} only.</p>' in login
    unauthorised = Client().get("/stores/").content.decode()
    assert f'<p class="auth-company" id="auth-company">{name} · Monvera</p>' in unauthorised
