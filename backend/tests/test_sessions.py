"""Idle and absolute session expiry, UserSession rows, sign out everywhere (PLAN-003)."""

from __future__ import annotations

import time

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client

from accounts import signals
from accounts.models import UserSession
from accounts.roles import Role
from tests.auth_helpers import current_code, make_user, password_step, token_step, verified_client

pytestmark = pytest.mark.django_db


class FakeClock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(signals, "now", fake)
    return fake


def test_timeouts_are_the_planned_values() -> None:
    assert settings.SESSION_COOKIE_AGE == 30 * 60
    assert settings.SESSION_SAVE_EVERY_REQUEST is True
    assert settings.SESSION_ABSOLUTE_MAX_AGE == 12 * 60 * 60
    assert settings.SESSION_ENGINE == "django.contrib.sessions.backends.db"


def test_idle_session_expires(clock: FakeClock) -> None:
    user, device = make_user("idle", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device)
    clock.now += settings.SESSION_COOKIE_AGE - 5
    assert client.get("/admin/").status_code == 200
    clock.now += settings.SESSION_COOKIE_AGE - 5  # activity resets the idle window
    assert client.get("/admin/").status_code == 200
    clock.now += settings.SESSION_COOKIE_AGE
    assert client.get("/admin/").status_code == 401
    # Expiry flushed the session: coming back is still 401, not a revived session.
    clock.now = time.time()
    assert client.get("/admin/").status_code == 401


def test_absolute_ceiling_ends_an_active_session(clock: FakeClock) -> None:
    user, device = make_user("busy", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device)
    step = settings.SESSION_COOKIE_AGE // 2
    elapsed = 0
    while elapsed + step < settings.SESSION_ABSOLUTE_MAX_AGE:
        clock.now += step
        elapsed += step
        assert client.get("/admin/").status_code == 200, elapsed
    clock.now += step
    assert client.get("/admin/").status_code == 401


def test_session_without_login_time_is_ended() -> None:
    user, device = make_user("legacy", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device)
    session = client.session
    del session[signals.LOGIN_AT_SESSION_KEY]
    session.save()
    assert client.get("/admin/").status_code == 401


def test_expired_database_session_is_not_accepted() -> None:
    user, device = make_user("dbexp", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device)
    Session.objects.update(expire_date="2000-01-01T00:00:00Z")
    assert client.get("/admin/").status_code == 401


def test_login_records_a_user_session_and_logout_removes_it() -> None:
    _, device = make_user("rec", Role.VIEWER)
    assert device is not None
    client = Client(HTTP_USER_AGENT="A" * 500)
    password_step(client, "rec")
    token_step(client, current_code(device))
    row = UserSession.objects.get()
    assert row.session_key == client.session.session_key
    assert row.user_agent == "A" * 200
    client.post("/account/logout/")
    assert not UserSession.objects.exists()


def test_sign_out_everywhere_ends_every_session() -> None:
    user, device = make_user("everywhere", Role.VIEWER)
    other, other_device = make_user("bystander", Role.VIEWER)
    assert device is not None and other_device is not None
    laptop = verified_client(user, device)
    phone = verified_client(user, device)
    bystander = verified_client(other, other_device)
    assert UserSession.objects.filter(user=user).count() == 2

    response = laptop.post("/account/sign-out-everywhere/")
    assert response.status_code == 200
    assert 'id="sandbox-banner"' in response.content.decode()

    assert laptop.get("/admin/").status_code == 401
    assert phone.get("/admin/").status_code == 401
    assert bystander.get("/admin/").status_code == 200
    assert not UserSession.objects.filter(user=user).exists()
    assert UserSession.objects.filter(user=other).count() == 1


def test_sign_out_everywhere_catches_a_rotated_session_key() -> None:
    user, device = make_user("rotated", Role.VIEWER)
    assert device is not None
    laptop = verified_client(user, device)
    phone = verified_client(user, device)
    # A key rotation (e.g. password change) leaves the UserSession row stale.
    session = phone.session
    session.cycle_key()
    assert session.session_key is not None
    phone.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    assert phone.get("/admin/").status_code == 200
    laptop.post("/account/sign-out-everywhere/")
    assert phone.get("/admin/").status_code == 401


def test_sign_out_everywhere_refuses_get() -> None:
    user, device = make_user("getter", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device)
    assert client.get("/account/sign-out-everywhere/").status_code == 405
    assert client.get("/admin/").status_code == 200


def test_sign_out_everywhere_requires_csrf() -> None:
    user, device = make_user("csrf", Role.VIEWER)
    assert device is not None
    client = verified_client(user, device, Client(enforce_csrf_checks=True))
    assert client.post("/account/sign-out-everywhere/").status_code == 403
    assert client.get("/admin/").status_code == 200


def test_sign_out_everywhere_needs_a_session(client: Client) -> None:
    assert client.post("/account/sign-out-everywhere/").status_code == 401
