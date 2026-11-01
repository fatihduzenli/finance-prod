"""PLAN-014 I-b item 7: the staff sign-in page redesign — the texts (the headline, the four
cards with the processor's name from the company settings, the footer and "Staff of … only."
with the company's name), the sandbox pill only on the sandbox, "Need help?" and "Forgot
password?" as mailto: links to the support e-mail and absent without one, the decorative mosaic
hidden from screen readers and the same on every render, the password's labelled "Show" toggle
(a static script, no inline one), "Keep me signed in" (ticked: the persistent 30-minute cookie;
unticked: a browser-session cookie; the idle and absolute limits end a kept session all the
same), and the password step's "Sign in" without Cancel or Next while the token step keeps
them."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings

from accounts import signals
from accounts.roles import Role
from ledger.models import CompanySettings
from tests.auth_helpers import (
    LOGIN_PATH,
    TEST_PASSWORD,
    WIZARD_STEP_FIELD,
    current_code,
    make_user,
    token_step,
)

pytestmark = pytest.mark.django_db

STATIC = Path(__file__).resolve().parents[1] / "finance" / "static" / "finance"
SUPPORT = "help@example.com"


def company(name: str = "Global Retail Group", support: str = SUPPORT) -> None:
    row = CompanySettings.current()
    row.name = name
    row.support_email = support
    row.save()


def login_page() -> str:
    response = Client().get(LOGIN_PATH)
    assert response.status_code == 200
    return response.content.decode()


def test_the_page_says_the_owners_texts_with_the_company_and_the_processor() -> None:
    company()
    row = CompanySettings.current()
    row.processor_name = "Acme Pay"
    row.save()
    page = login_page()
    assert ">Rent collection for every store, in one place.</p>" in page
    assert (
        "Every square is a store's month: paid, pending or late. Monvera keeps them all in view."
        in page
    )
    cards = re.findall(r"<strong>([^<]+)</strong><span>([^<]+)</span>", page)
    assert cards == [
        ("Monthly overview", "Every store and month in one colour-coded grid."),
        ("Bank debits", "Autopay and one-off debits through Acme Pay."),
        ("Reminders", "Due, late and returned-payment notices sent for you."),
        ("Promises", "Track who promised what, and when."),
    ]
    assert '<p class="auth-company" id="auth-company">Global Retail Group · Monvera</p>' in page
    assert ">Staff of Global Retail Group only.</p>" in page
    assert '<h1 id="sign-in-heading">Sign in to Monvera</h1>' in page


def test_the_sandbox_pill_shows_on_the_sandbox_only() -> None:
    assert 'id="sandbox-banner"' in login_page()
    with override_settings(STAX_ENV="live"):
        assert 'id="sandbox-banner"' not in login_page()


def test_need_help_and_forgot_password_are_mailto_links_to_the_support_email() -> None:
    company()
    page = login_page()
    assert f'<a href="mailto:{SUPPORT}" id="auth-help">Need help?</a>' in page
    assert (
        f'<a href="mailto:{SUPPORT}?subject=Password%20reset" id="auth-forgot">Forgot password?</a>'
    ) in page
    assert "reset/" not in page  # no self-service reset route, only the e-mail


def test_without_a_support_email_neither_link_is_on_the_page() -> None:
    company(support="")
    page = login_page()
    assert "Need help?" not in page and "Forgot password?" not in page
    assert "mailto:" not in page
    assert '<div class="auth-top" id="auth-top"></div>' in page  # the row keeps its place


def test_the_mosaic_is_decoration_and_the_same_on_every_render() -> None:
    first, second = login_page(), login_page()
    mosaic = re.search(r'<div class="auth-mosaic"[^>]*>.*?</div></div>', first, re.S)
    assert mosaic is not None and 'aria-hidden="true"' in mosaic.group(0)
    tiles = re.findall(r'<i class="tile-([ptlwn])"></i>', mosaic.group(0))
    assert len(tiles) == 168 and set(tiles) == {"p", "t", "l", "w", "n"}
    assert tiles.count("p") > len(tiles) / 3  # mostly paid green
    assert mosaic.group(0) in second
    assert "<img" not in mosaic.group(0)  # CSS tiles, no image download


def test_the_password_and_its_show_toggle_are_labelled_and_the_script_is_static() -> None:
    page = login_page()
    assert '<label for="id_auth-password">Password</label>' in page
    toggle = re.search(
        r'<button type="button"[^>]*id="auth-show-password"[^>]*>Show</button>', page
    )
    assert toggle is not None
    for attribute in (
        'aria-controls="id_auth-password"',
        'aria-label="Show password"',
        'aria-pressed="false"',
        "data-password-toggle",
        " hidden",
    ):
        assert attribute in toggle.group(0), attribute
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", page, re.S)
    assert scripts == [""]  # one script, with a src and no inline code
    assert '<script src="/static/finance/password_toggle.js" defer></script>' in page
    script = (STATIC / "password_toggle.js").read_text(encoding="utf-8")
    assert 'field.type = showing ? "text" : "password";' in script
    assert "aria-pressed" in script and "button.hidden = false;" in script
    assert '<label class="inline" for="id_auth-remember">' in page
    assert 'id="id_auth-remember"> Keep me signed in</label>' in page


def _sign_in(client: Client, username: str, *, keep: bool) -> str:
    data = {WIZARD_STEP_FIELD: "auth", "auth-username": username, "auth-password": TEST_PASSWORD}
    if keep:
        data["auth-remember"] = "on"
    response = client.post(LOGIN_PATH, data)
    assert response.status_code == 200  # the token step
    return response.content.decode()


def _signed_in(keep: bool, name: str) -> Client:
    user, device = make_user(name, Role.VIEWER)
    assert device is not None
    client = Client()
    _sign_in(client, user.get_username(), keep=keep)
    response = token_step(client, current_code(device))
    assert response.status_code == 302, response.status_code
    return client


def test_keep_me_signed_in_keeps_the_persistent_cookie_and_unticked_ends_with_the_browser() -> None:
    kept = _signed_in(True, "kept-user")
    cookie = kept.cookies[settings.SESSION_COOKIE_NAME]
    assert int(cookie["max-age"]) == settings.SESSION_COOKIE_AGE == 30 * 60
    assert kept.session.get_expire_at_browser_close() is False
    assert kept.get("/stores/").status_code == 200
    assert int(kept.cookies[settings.SESSION_COOKIE_NAME]["max-age"]) == 30 * 60

    browser = _signed_in(False, "browser-user")
    cookie = browser.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie["max-age"] == "" and cookie["expires"] == ""
    assert browser.session.get_expire_at_browser_close() is True
    assert browser.get("/stores/").status_code == 200
    assert browser.cookies[settings.SESSION_COOKIE_NAME]["max-age"] == ""
    # The stored session lasts no longer either way.
    assert browser.session.get_expiry_age() == kept.session.get_expiry_age() == 30 * 60


class FakeClock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def test_the_idle_limit_still_ends_a_kept_session(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(signals, "now", clock)
    kept = _signed_in(True, "kept-idle")
    clock.now += settings.SESSION_COOKIE_AGE - 5
    assert kept.get("/stores/").status_code == 200
    clock.now += settings.SESSION_COOKIE_AGE
    assert kept.get("/stores/").status_code == 401


def test_the_absolute_limit_still_ends_a_kept_session(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(signals, "now", clock)
    kept = _signed_in(True, "kept-busy")
    step = settings.SESSION_COOKIE_AGE // 2
    elapsed = 0
    while elapsed + step < settings.SESSION_ABSOLUTE_MAX_AGE:
        clock.now += step
        elapsed += step
        assert kept.get("/stores/").status_code == 200, elapsed
    clock.now += step
    assert kept.get("/stores/").status_code == 401
    assert settings.SESSION_ABSOLUTE_MAX_AGE == 12 * 60 * 60


def test_the_password_step_reads_sign_in_and_the_token_step_keeps_next_and_cancel() -> None:
    page = login_page()
    form = page.split('id="sign-in-form"', 1)[1].split("</form>", 1)[0]
    assert 'id="sign-in-button">Sign in</button>' in form
    assert 'btn-primary">Next</button>' not in form and "auth-cancel" not in form
    user, _ = make_user("steps-user", Role.VIEWER)
    client = Client()
    token = _sign_in(client, user.get_username(), keep=False)
    actions = token.split('<div class="auth-actions">', 1)[1].split("</div>", 1)[0]
    assert '<button type="submit" class="btn btn-primary">Next</button>' in actions
    assert 'class="link-action auth-cancel">Cancel</a>' in actions
    assert "auth-remember" not in token.split('id="sign-in-form"', 1)[1]


def test_the_credentials_box_alone_shows_the_focus() -> None:
    # Review of I-b: one focus indicator, the box's 2 px --link ring; the fields inside are
    # borderless and draw no ring of their own (the global :focus-visible ring is taken back).
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert (
        ".auth-credentials:focus-within { border-color: var(--link); "
        "box-shadow: 0 0 0 1px var(--link); }"
    ) in css
    assert ".auth-main .auth-credential input:focus-visible { box-shadow: none; }" in css
    inner = css.split(".auth-main .auth-credential input[type=text], ", 1)[1].split("}", 1)[0]
    assert "border: 0;" in inner
