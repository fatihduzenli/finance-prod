"""The route registry fails closed and covers every route (PLAN-003 §5 items 5-6).

The enumeration test walks the live URL map. A new route that is neither public,
read-only, nor given a rule makes it fail, so an unsafe endpoint cannot ship without
a role decision.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client
from django.views.generic.base import RedirectView

from accounts import registry
from tests.auth_helpers import iter_routes, sample_path


def _is_classified(name: str) -> bool:
    return (
        name in registry.PUBLIC_ROUTE_NAMES
        or name in registry.READ_ONLY_ROUTE_NAMES
        or registry.rule_for(name) is not None
    )


def _is_known_unnamed_admin_route(raw: str, pattern: object) -> bool:
    """Django admin's two unnamed helpers: the object-id redirect and the catch-all.

    Both answer GET only with a redirect or 404; an unsafe request to them has no rule
    and is refused by RoleEnforcementMiddleware.
    """
    callback = getattr(pattern, "callback", None)
    view_class = getattr(callback, "view_class", None)
    if not raw.startswith("/admin/"):
        return False
    if isinstance(view_class, type) and issubclass(view_class, RedirectView):
        return True
    return getattr(callback, "__name__", "") == "catch_all_view"


def test_every_route_is_classified() -> None:
    unclassified = [
        name or raw
        for name, raw, pattern in iter_routes()
        if (name is None and not _is_known_unnamed_admin_route(raw, pattern))
        or (name is not None and not _is_classified(name))
    ]
    assert unclassified == [], f"routes without a registry decision: {unclassified}"


def test_registry_has_no_stale_entries() -> None:
    names = {name for name, _, _ in iter_routes() if name}
    stale = [
        name
        for name in (
            set(registry.ROUTE_ROLES)
            | registry.READ_ONLY_ROUTE_NAMES
            | registry.PUBLIC_ROUTE_NAMES
            | registry.MFA_SETUP_ROUTE_NAMES
            | registry.MFA_EXEMPT_ROUTE_NAMES
        )
        if name not in names
    ]
    stale += [
        prefix
        for prefix in registry.ADMIN_MODEL_PREFIX_RULES
        if not any(name.startswith(prefix) for name in names)
    ]
    assert stale == []


def test_read_only_routes_have_no_unsafe_rule() -> None:
    for name in registry.READ_ONLY_ROUTE_NAMES:
        assert registry.rule_for(name) is None, name


def test_public_set_is_exactly_login_healthz_webhook_receiver_and_static() -> None:
    # The webhook receiver is public and takes POST (PLAN-008 D2); it checks its token.
    assert registry.PUBLIC_ROUTE_NAMES == {"two_factor:login", "healthz", "webhooks:receive"}
    assert registry.PUBLIC_UNSAFE_ROUTE_NAMES == {"two_factor:login", "webhooks:receive"}
    assert registry.PUBLIC_PATH_PREFIXES == (f"/{settings.STATIC_URL.lstrip('/')}",)


def test_unregistered_name_has_no_rule() -> None:
    assert registry.rule_for(None) is None
    assert registry.rule_for("payments:refund") is None
    assert registry.rule_for("other:auth_user_add") is None
    # A prefix rule is anchored at the namespace: a look-alike does not match.
    assert registry.rule_for("xadmin:auth_user_add") is None


def _non_public_routes() -> list[tuple[str, str]]:
    return [
        (name or raw, sample_path(raw))
        for name, raw, _ in iter_routes()
        if name not in registry.PUBLIC_ROUTE_NAMES
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_unauthenticated_gets_401_on_every_non_public_route(client: Client, method: str) -> None:
    routes = _non_public_routes()
    assert len(routes) > 40
    for label, path in routes:
        response = getattr(client, method)(path)
        assert response.status_code == 401, (label, path, response.status_code)
        assert not response.has_header("Location"), label
        assert not response.has_header("WWW-Authenticate"), label


@pytest.mark.django_db
def test_unknown_paths_get_401_not_404(client: Client) -> None:
    for path in ("/", "/does-not-exist/", "/account/", "/admin", "/webhooks/stax/x/"):
        assert client.get(path).status_code == 401, path


@pytest.mark.django_db
def test_public_routes_answer_without_a_session(client: Client) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/account/login/").status_code == 200
    # Missing static file: not 401, the prefix is public (and simply not found).
    assert client.get(f"/{settings.STATIC_URL.lstrip('/')}nope.css").status_code == 404


@pytest.mark.django_db
def test_healthz_refuses_unsafe_methods(client: Client) -> None:
    # Public for GET only: a POST has no rule, so it is refused even though it is public.
    assert client.post("/healthz").status_code == 403
