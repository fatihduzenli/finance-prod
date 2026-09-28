"""Roles and server-side enforcement of unsafe requests (PLAN-003 §5 item 5, §6)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.contrib.auth.models import Group
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings

from accounts import registry
from accounts.middleware import RoleEnforcementMiddleware
from accounts.registry import POST, RouteRule, is_allowed
from accounts.roles import Role, role_of
from tests.auth_helpers import iter_routes, make_user, sample_path, verified_client

pytestmark = pytest.mark.django_db

ROLE_REFUSAL = "Your role does not allow this action."

SYNTHETIC_RULES = {
    "synthetic:refund": RouteRule(min_role=Role.ADMIN, methods=POST),
    "synthetic:settings": RouteRule(min_role=Role.ADMIN, methods=POST),
    "synthetic:invoice": RouteRule(min_role=Role.BILLING, methods=POST),
}


@pytest.fixture
def synthetic_routes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(registry, "ROUTE_ROLES", {**registry.ROUTE_ROLES, **SYNTHETIC_RULES})
    with override_settings(ROOT_URLCONF="tests.synthetic_urls"):
        yield


def _client_for(username: str, role: Role | None, **kwargs: bool) -> Client:
    user, device = make_user(username, role, **kwargs)
    assert device is not None
    return verified_client(user, device)


# --- role_of -------------------------------------------------------------------------


def test_role_of_returns_the_highest_group() -> None:
    user, _ = make_user("multi", Role.VIEWER)
    user.groups.add(Group.objects.get(name="billing"))
    assert role_of(user) is Role.BILLING
    user.groups.add(Group.objects.get(name="admin"))
    assert role_of(user) is Role.ADMIN


def test_role_of_ignores_look_alike_groups() -> None:
    user, _ = make_user("lookalike", None)
    for name in ("Admin", "ADMIN", "admin ", "administrators"):
        user.groups.add(Group.objects.create(name=name))
    assert role_of(user) is None


def test_superuser_flag_alone_grants_no_role() -> None:
    user, _ = make_user("root", None, superuser=True)
    assert role_of(user) is None


def test_inactive_user_has_no_role() -> None:
    user, _ = make_user("inactive", Role.ADMIN)
    user.is_active = False
    assert role_of(user) is None


# --- is_allowed ----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_are_never_refused(method: str) -> None:
    assert is_allowed(None, method, None)


def test_unsafe_method_without_rule_is_refused_for_every_role() -> None:
    for role in (*Role, None):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert not is_allowed(None, method, role)


def test_method_not_listed_in_rule_is_refused() -> None:
    rule = RouteRule(min_role=Role.VIEWER, methods=POST)
    assert not is_allowed(rule, "DELETE", Role.ADMIN)
    assert not is_allowed(rule, "PUT", Role.ADMIN)


def test_billing_is_refused_refund_and_settings_rules() -> None:
    assert not is_allowed(SYNTHETIC_RULES["synthetic:refund"], "POST", Role.BILLING)
    assert not is_allowed(SYNTHETIC_RULES["synthetic:settings"], "POST", Role.BILLING)
    assert is_allowed(SYNTHETIC_RULES["synthetic:invoice"], "POST", Role.BILLING)
    assert not is_allowed(SYNTHETIC_RULES["synthetic:invoice"], "POST", Role.VIEWER)
    assert is_allowed(SYNTHETIC_RULES["synthetic:refund"], "POST", Role.ADMIN)


# --- through the middleware ----------------------------------------------------------


@pytest.mark.usefixtures("synthetic_routes")
def test_billing_gets_403_on_refund_and_settings() -> None:
    client = _client_for("bill", Role.BILLING)
    assert client.post("/synthetic/refund/").status_code == 403
    assert client.post("/synthetic/settings/").status_code == 403
    assert client.post("/synthetic/invoice/").status_code == 200


@pytest.mark.usefixtures("synthetic_routes")
def test_admin_passes_refund_and_viewer_is_refused_invoice() -> None:
    assert _client_for("adm", Role.ADMIN).post("/synthetic/refund/").status_code == 200
    assert _client_for("view", Role.VIEWER).post("/synthetic/invoice/").status_code == 403


@pytest.mark.usefixtures("synthetic_routes")
def test_same_view_under_another_namespace_is_refused() -> None:
    client = _client_for("ns", Role.ADMIN)
    assert client.post("/other/sign-out-everywhere/").status_code == 403


def test_viewer_gets_admin_index() -> None:
    assert _client_for("viewer", Role.VIEWER).get("/admin/").status_code == 200


def _admin_only_routes() -> list[tuple[str, str]]:
    routes = []
    for name, raw, _ in iter_routes():
        rule = registry.rule_for(name)
        if rule is not None and rule.min_role is Role.ADMIN:
            routes.append((str(name), sample_path(raw)))
    return routes


def test_viewer_gets_403_on_every_admin_only_route() -> None:
    client = _client_for("viewer2", Role.VIEWER)
    routes = _admin_only_routes()
    assert len(routes) >= 20
    for name, path in routes:
        response = client.post(path, {"name": "x"})
        assert response.status_code == 403, (name, path)
        # Refused by the registry, not merely by Django's model permissions.
        assert ROLE_REFUSAL in response.content.decode(), name


def test_billing_gets_403_on_every_admin_only_route() -> None:
    client = _client_for("billing2", Role.BILLING)
    for name, path in _admin_only_routes():
        assert client.post(path, {"name": "x"}).status_code == 403, name


def test_superuser_without_group_is_refused_mutation() -> None:
    client = _client_for("super", None, superuser=True)
    assert client.post("/admin/auth/group/add/", {"name": "sneaky"}).status_code == 403
    assert not Group.objects.filter(name="sneaky").exists()


def test_admin_role_can_manage_groups() -> None:
    client = _client_for("boss", Role.ADMIN)
    response = client.post("/admin/auth/group/add/", {"name": "made-by-admin"})
    assert response.status_code == 302
    assert Group.objects.filter(name="made-by-admin").exists()


def test_viewer_can_change_own_password_route() -> None:
    client = _client_for("pwd", Role.VIEWER)
    # Passes the registry; the admin form then rejects the empty submission (200).
    assert client.post("/admin/password_change/", {}).status_code == 200


@pytest.mark.parametrize(
    ("headers", "data"),
    [
        ({"HTTP_X_HTTP_METHOD_OVERRIDE": "GET"}, {"name": "x"}),
        ({"HTTP_X_METHOD_OVERRIDE": "GET"}, {"name": "x"}),
        ({}, {"name": "x", "_method": "GET"}),
    ],
)
def test_method_override_is_not_honoured(headers: dict[str, str], data: dict[str, str]) -> None:
    client = _client_for("override", Role.VIEWER)
    response = client.post("/admin/auth/group/add/", data, **headers)  # type: ignore[arg-type]
    assert response.status_code == 403
    assert not Group.objects.filter(name="x").exists()


@pytest.mark.parametrize("method", ["head", "options"])
def test_head_and_options_pass_the_role_check(method: str) -> None:
    user, _ = make_user("safe", None)
    request = getattr(RequestFactory(), method)("/admin/auth/group/add/")
    request.user = user
    middleware = RoleEnforcementMiddleware(lambda _request: HttpResponse())
    assert middleware.process_view(request, lambda _r: HttpResponse(), (), {}) is None


def test_role_less_user_can_still_log_out() -> None:
    client = _client_for("nogroup", None)
    assert client.post("/account/logout/").status_code == 302
