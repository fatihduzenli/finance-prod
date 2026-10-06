"""Password + TOTP login, the MFA gate and the 401 policy (PLAN-003 §5 item 5, §6)."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client, RequestFactory
from django_otp.plugins.otp_totp.models import TOTPDevice
from two_factor.plugins.registry import registry as method_registry

from accounts.roles import Role
from accounts.views import _relative_next
from tests.auth_helpers import (
    LOGIN_PATH,
    current_code,
    make_user,
    password_step,
    redirect_target,
    token_step,
    verified_client,
)

pytestmark = pytest.mark.django_db

BANNER_ID = 'id="sandbox-banner"'


def test_password_alone_does_not_sign_in(client: Client) -> None:
    make_user("pat", Role.VIEWER)
    password_step(client, "pat")
    assert "_auth_user_id" not in client.session
    assert client.get("/admin/").status_code == 401


def test_wrong_totp_code_does_not_sign_in(client: Client) -> None:
    _, device = make_user("wrong", Role.VIEWER)
    assert device is not None
    password_step(client, "wrong")
    bad = str((int(current_code(device)) + 1) % 10**6).zfill(6)
    response = token_step(client, bad)
    assert response.status_code == 200  # form redisplayed with an error
    assert client.get("/admin/").status_code == 401


def test_password_and_totp_reach_admin(client: Client) -> None:
    _, device = make_user("vera", Role.VIEWER)
    assert device is not None
    password_step(client, "vera")
    response = token_step(client, current_code(device))
    assert response.status_code == 302
    assert redirect_target(response) == "/"
    index = client.get("/admin/")
    assert index.status_code == 200
    assert BANNER_ID in index.content.decode()


def test_login_redirect_rejects_absolute_next(client: Client) -> None:
    _, device = make_user("nina", Role.VIEWER)
    assert device is not None
    password_step(client, "nina")
    response = token_step(client, current_code(device), next_path="https://evil.example.com/x")
    assert response.status_code == 302
    assert response["Location"] == "/"


def test_login_redirect_rejects_protocol_relative_next(client: Client) -> None:
    _, device = make_user("nora", Role.VIEWER)
    assert device is not None
    password_step(client, "nora")
    response = token_step(client, current_code(device), next_path="//evil.example.com/x")
    assert response["Location"] == "/"


def test_session_without_otp_cannot_use_admin() -> None:
    user, _ = make_user("half", Role.ADMIN)
    client = Client()
    client.force_login(user)  # password-authenticated, second factor never passed
    for path in ("/admin/", "/admin/auth/user/", "/account/two_factor/"):
        response = client.get(path)
        assert response.status_code == 403, path
    assert client.post("/admin/auth/group/add/", {"name": "x"}).status_code == 403


def test_unverified_user_with_a_device_cannot_enrol_another() -> None:
    """Otherwise a password alone would be enough to register a new authenticator."""
    user, _ = make_user("enrol", Role.ADMIN)
    client = Client()
    client.force_login(user)
    assert client.get("/account/two_factor/setup/").status_code == 403
    assert (
        client.post(
            "/account/two_factor/setup/", {"setup_view-current_step": "welcome"}
        ).status_code
        == 403
    )
    assert client.post("/account/two_factor/backup/tokens/").status_code == 403
    assert TOTPDevice.objects.filter(user=user).count() == 1


def test_user_without_device_is_sent_to_setup_only(client: Client) -> None:
    make_user("newbie", Role.VIEWER, with_device=False)
    password_step(client, "newbie")
    assert "_auth_user_id" in client.session  # signed in, not verified
    denied = client.get("/admin/")
    assert denied.status_code == 403
    body = denied.content.decode()
    assert 'href="/account/two_factor/setup/"' in body
    assert BANNER_ID in body
    setup = client.get("/account/two_factor/setup/")
    assert setup.status_code == 200
    assert BANNER_ID in setup.content.decode()


def test_deleting_the_verifying_device_ends_access_on_next_request() -> None:
    user, device = make_user("gone", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device)
    assert client.get("/admin/").status_code == 200
    device.delete()
    assert client.get("/admin/").status_code == 403


def test_logout_by_get_is_refused() -> None:
    user, device = make_user("leave", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device)
    assert client.get("/account/logout/").status_code == 405
    assert client.get("/admin/").status_code == 200
    assert client.post("/account/logout/").status_code == 302
    assert client.get("/admin/").status_code == 401


def test_unverified_user_can_log_out() -> None:
    user, _ = make_user("out", Role.VIEWER)
    client = Client()
    client.force_login(user)
    assert client.post("/account/logout/").status_code == 302
    assert client.get("/admin/").status_code == 401


def test_mfa_setup_is_not_reachable_without_a_session(client: Client) -> None:
    for path in (
        "/account/two_factor/setup/",
        "/account/two_factor/qrcode/",
        "/account/two_factor/backup/tokens/",
    ):
        assert client.get(path).status_code == 401, path


def test_admin_login_url_answers_401_not_a_password_form(client: Client) -> None:
    response = client.get("/admin/login/")
    assert response.status_code == 401
    body = response.content.decode()
    assert 'name="password"' not in body
    assert BANNER_ID in body


def test_401_page_links_to_login_with_relative_next(client: Client) -> None:
    response = client.get("/admin/auth/user/?q=a")
    assert response.status_code == 401
    assert 'href="/account/login/?next=%2Fadmin%2Fauth%2Fuser%2F%3Fq%3Da"' in (
        response.content.decode()
    )


def test_401_page_does_not_reflect_markup(client: Client) -> None:
    body = client.get('/admin/"><script>alert(1)</script>').content.decode()
    assert "<script>alert(1)" not in body


def test_401_body_has_no_secrets(client: Client, fake_stax_api_key: str) -> None:
    body = client.get("/admin/").content.decode()
    assert fake_stax_api_key not in body
    assert settings.SECRET_KEY not in body
    assert "Traceback" not in body


@pytest.mark.parametrize("hostile", ["//evil.example.com/x", "https://evil.example.com/"])
def test_relative_next_drops_anything_but_a_same_site_path(hostile: str) -> None:
    request = RequestFactory().get("/")
    request.path = hostile
    assert _relative_next(request) is None


def test_relative_next_keeps_a_backslash_path_escaped() -> None:
    request = RequestFactory().get("/")
    request.path = "/\\evil.example.com"
    # get_full_path() percent-encodes the backslash, so browsers cannot read it as "//".
    assert _relative_next(request) == "/%5Cevil.example.com"


def test_login_page_carries_the_banner(client: Client) -> None:
    response = client.get(LOGIN_PATH)
    assert response.status_code == 200
    html = response.content.decode()
    assert html.count(BANNER_ID) == 1
    assert "<title>[SANDBOX] " in html
    # The package default base loads Bootstrap/jQuery from a CDN; ours loads nothing.
    assert "cdnjs" not in html
    assert "<script" not in html


def test_remember_device_cookie_is_off() -> None:
    assert settings.TWO_FACTOR_REMEMBER_COOKIE_AGE is None


def test_no_phone_or_other_methods_installed() -> None:
    assert [method.code for method in method_registry.get_methods()] == ["generator"]
    for app in settings.INSTALLED_APPS:
        assert not app.startswith("two_factor.plugins"), app
        assert "phonenumber" not in app, app
        assert app not in {"django_otp.plugins.otp_email", "django_otp.plugins.otp_hotp"}


def test_session_cookie_flags() -> None:
    _, device = make_user("cookie", Role.VIEWER)
    assert device is not None
    client = Client()
    password_step(client, "cookie")
    token_step(client, current_code(device))
    cookie = client.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie["httponly"]
    assert cookie["samesite"] == "Strict"
    assert int(cookie["max-age"]) == 30 * 60
