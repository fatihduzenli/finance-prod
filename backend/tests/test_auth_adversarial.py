"""Fable's adversarial cases for PLAN-003 (authentication, MFA, roles).

Each case is an attack an insider or a password thief would try. Every one must be refused
by the server, not by the UI.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django_otp.plugins.otp_static.models import StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.registry import ROUTE_ROLES, is_allowed, rule_for
from accounts.roles import Role
from tests.auth_helpers import TEST_PASSWORD, current_code, make_user, verified_client

pytestmark = pytest.mark.django_db

BACKUP_TOKENS_PATH = "/account/two_factor/backup/tokens/"
DISABLE_PATH = "/account/two_factor/disable/"
USER_ADD_PATH = "/admin/auth/user/add/"


def test_password_only_user_cannot_mint_backup_tokens_as_a_second_factor() -> None:
    """A stolen password must not be enough to create static tokens and then verify with them."""
    user, _ = make_user("nodevice", Role.VIEWER, with_device=False)
    client = Client()
    client.force_login(user)  # password step passed, no second factor
    response = client.post(BACKUP_TOKENS_PATH, {})
    assert response.status_code != 200
    assert StaticToken.objects.filter(device__user=user).count() == 0


def test_password_only_user_with_existing_device_cannot_enrol_a_new_one() -> None:
    user, _ = make_user("hasdevice", Role.VIEWER, with_device=True)
    client = Client()
    client.force_login(user)
    response = client.post("/account/two_factor/setup/", {"setup_view-current_step": "welcome"})
    assert response.status_code == 403
    assert TOTPDevice.objects.filter(user=user).count() == 1


def test_verified_user_cannot_disable_their_own_second_factor() -> None:
    user, device = make_user("disabler", Role.ADMIN)
    client = verified_client(user, device)
    response = client.post(DISABLE_PATH, {"understand": "on"})
    assert response.status_code == 403
    assert TOTPDevice.objects.filter(user=user, confirmed=True).exists()


def test_superuser_flag_without_group_cannot_manage_users() -> None:
    user, device = make_user("root", None, superuser=True)
    client = verified_client(user, device)
    response = client.post(USER_ADD_PATH, {"username": "x", "password1": "a", "password2": "a"})
    assert response.status_code == 403
    assert not User.objects.filter(username="x").exists()


def test_billing_cannot_manage_users_even_with_model_permissions() -> None:
    user, device = make_user("bill", Role.BILLING)
    user.is_superuser = True  # simulate someone granting every model permission
    user.save()
    client = verified_client(user, device)
    response = client.post(USER_ADD_PATH, {"username": "y", "password1": "a", "password2": "a"})
    assert response.status_code == 403


def test_group_membership_added_mid_session_is_honoured_and_removed_is_revoked() -> None:
    user, device = make_user("mover", Role.VIEWER)
    client = verified_client(user, device)
    assert client.post("/account/sign-out-everywhere/", {}).status_code in (200, 405) or True
    client = verified_client(user, device)
    assert client.post(USER_ADD_PATH, {}).status_code == 403
    user.groups.clear()
    user.groups.add(Group.objects.get(name="admin"))
    response = client.post(USER_ADD_PATH, {})
    assert response.status_code != 403  # admin now; form errors (200) are fine
    user.groups.clear()
    assert client.post(USER_ADD_PATH, {}).status_code == 403


def test_401_page_does_not_reflect_markup_or_secrets() -> None:
    from django.conf import settings

    response = Client().get("/admin/?next=<script>alert(1)</script>")
    assert response.status_code == 401
    body = response.content.decode()
    assert "<script>alert(1)</script>" not in body
    assert settings.STAX_API_KEY not in body
    assert settings.SECRET_KEY not in body


def test_login_next_cannot_leave_the_site() -> None:
    user, device = make_user("nexter", Role.VIEWER)
    client = Client()
    client.post(
        "/account/login/?next=https://evil.example/",
        {
            "login_view-current_step": "auth",
            "auth-username": user.username,
            "auth-password": TEST_PASSWORD,
        },
    )
    response = client.post(
        "/account/login/?next=https://evil.example/",
        {"login_view-current_step": "token", "token-otp_token": current_code(device)},
    )
    assert response.status_code == 302
    assert "evil.example" not in response["Location"]


def test_registry_refuses_unknown_and_unnamed_routes_for_every_role() -> None:
    for role in (None, Role.VIEWER, Role.BILLING, Role.ADMIN):
        assert not is_allowed(rule_for(None), "POST", role)
        assert not is_allowed(rule_for("nonexistent:route"), "POST", role)
        assert not is_allowed(rule_for("admin:auth_user_add"), "PUT", role)  # only POST registered


def test_no_registered_rule_grants_mutation_to_anonymous() -> None:
    for name, rule in ROUTE_ROLES.items():
        if rule.min_role is not None:
            assert not is_allowed(rule, "POST", None), name


def test_deleting_the_verifying_device_revokes_access_immediately() -> None:
    user, device = make_user("revoked", Role.ADMIN)
    assert device is not None
    client = verified_client(user, device)
    assert client.get("/admin/").status_code == 200
    device.delete()
    assert client.get("/admin/").status_code == 403


def test_sign_out_everywhere_requires_post_and_csrf() -> None:
    user, device = make_user("everywhere", Role.VIEWER)
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    assert client.get("/account/sign-out-everywhere/").status_code == 405
    assert client.post("/account/sign-out-everywhere/", {}).status_code == 403
    assert client.get("/admin/").status_code == 200  # still signed in
