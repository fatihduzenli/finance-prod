"""Helpers for the PLAN-003 authentication tests. Not a test module.

Role groups are created by a data migration, but transactional tests (the e2e suite)
flush the database, so tests create them idempotently instead of relying on it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.roles import GROUP_NAMES, Role

if TYPE_CHECKING:
    from django.http import HttpResponseBase
    from django.test.client import _MonkeyPatchedWSGIResponse

#: Obviously fake; only ever stored hashed in the throwaway test database.
TEST_PASSWORD = "test-only-password-not-secret-7Q"

LOGIN_PATH = "/account/login/"
WIZARD_STEP_FIELD = "login_view-current_step"


def ensure_role_groups() -> None:
    for name in GROUP_NAMES:
        Group.objects.get_or_create(name=name)


def make_user(
    username: str,
    role: Role | None,
    *,
    staff: bool = True,
    superuser: bool = False,
    with_device: bool = True,
) -> tuple[User, TOTPDevice | None]:
    ensure_role_groups()
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_PASSWORD,
        is_staff=staff,
        is_superuser=superuser,
    )
    if role is not None:
        user.groups.add(Group.objects.get(name=role.value))
    device = None
    if with_device:
        device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    return user, device


def current_code(device: TOTPDevice) -> str:
    code = totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits)
    return str(code).zfill(device.digits)


def verified_client(user: User, device: TOTPDevice, client: Client | None = None) -> Client:
    """A client whose session passed both factors (as the two-factor login leaves it)."""
    client = client or Client()
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    return client


def password_step(client: Client, username: str, password: str = TEST_PASSWORD) -> None:
    response = client.post(
        LOGIN_PATH,
        {WIZARD_STEP_FIELD: "auth", "auth-username": username, "auth-password": password},
    )
    assert response.status_code in (200, 302), response.status_code


def token_step(client: Client, code: str, next_path: str = "") -> _MonkeyPatchedWSGIResponse:
    url = LOGIN_PATH + (f"?next={next_path}" if next_path else "")
    return client.post(url, {WIZARD_STEP_FIELD: "token", "token-otp_token": code})


def redirect_target(response: HttpResponseBase) -> str:
    location = str(response["Location"])
    return urlsplit(location).path


def iter_routes(
    patterns: list[URLPattern | URLResolver] | None = None,
    namespace: tuple[str, ...] = (),
    prefix: str = "/",
) -> list[tuple[str | None, str, URLPattern]]:
    """Every leaf route as (namespaced name or None, raw pattern string, pattern)."""
    if patterns is None:
        patterns = list(get_resolver().url_patterns)
    routes: list[tuple[str | None, str, URLPattern]] = []
    for entry in patterns:
        if isinstance(entry, URLResolver):
            child_ns = namespace + ((entry.namespace,) if entry.namespace else ())
            routes += iter_routes(list(entry.url_patterns), child_ns, prefix + str(entry.pattern))
        else:
            name = ":".join((*namespace, entry.name)) if entry.name else None
            routes.append((name, prefix + str(entry.pattern), entry))
    return routes


def sample_path(raw: str) -> str:
    """A concrete path for a route pattern, with sample values for its parameters."""
    path = raw.replace("^", "").replace("$", "")
    path = re.sub(r"\(\?P<app_label>[^)]*\)", "auth", path)
    path = re.sub(r"\(\?P<url>\.\*\)", "no-such-page/", path)
    return re.sub(r"<[^>]+>", "1", path)
