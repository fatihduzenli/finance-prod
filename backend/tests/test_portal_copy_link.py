"""The PORTAL card's "Copy invite link" (owner, 2026-11-01; sandbox only, admin only).

Allowed (an admin, the sandbox, local or staging): the operator page answers with a fresh
one-time link shown once, an invitation link while the account has no password, else a reset
link; the older link of that kind stops working; the audit row holds the kind and the expiry,
never the link; the response is ``no-store`` and ``no-referrer``; the link sets the password
once. Refused: billing and viewer (403), a GET (405), a live key, production, an archived
operator, a disabled account, no account; the button is hidden in each. The link is never in a
log, a message, a redirect or the database in clear.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth.hashers import make_password
from django.contrib.messages import get_messages
from django.contrib.sessions.models import Session
from django.db.models import Q
from django.test import Client
from django.utils import timezone

from accounts import registry
from accounts.roles import Role
from ledger.models import AuditLog, Operator, OutboundMessage
from portal import invitations, tokens
from portal.models import PortalToken, PortalUser, TokenKind
from tests.test_ledger_models import new_operator, saved
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

PASSWORD = "portal-test-only-Pass-91"
NEW_PASSWORD = "portal-test-only-New-73"
FIELD = re.compile(
    r'id="portal-copied-link-field" value="http://localhost:8000'
    r'(/portal/(invite|reset)/([A-Za-z0-9_-]+)/)"'
)
BUTTON = 'id="portal-copy-link-form"'


@pytest.fixture(autouse=True)
def sandbox(settings: Any) -> Any:
    settings.STAX_ENV = "sandbox"
    settings.APP_ENV = "local"
    settings.APP_BASE_URL = "http://localhost:8000"
    return settings


def an_operator(**fields: Any) -> Operator:
    return saved(
        new_operator(display_name="Alpha Owner", email="alpha.owner@example.com", **fields)
    )


def invited(operator: Operator) -> PortalUser:
    return saved(
        PortalUser(operator=operator, email=str(operator.email), invited_at=timezone.now())
    )


def active(operator: Operator) -> PortalUser:
    now = timezone.now()
    return saved(
        PortalUser(
            operator=operator,
            email=str(operator.email),
            password=make_password(PASSWORD),
            is_active=True,
            invited_at=now,
            activated_at=now,
        )
    )


def copy(staff: Client, operator: Operator) -> Any:
    return staff.post(f"/operators/{operator.pk}/portal/copy-link/")


def page(staff: Client, operator: Operator) -> str:
    return str(staff.get(f"/operators/{operator.pk}/").content.decode())


def found(response: Any) -> tuple[str, str, str]:
    html = response.content.decode()
    matches = FIELD.findall(html)
    assert len(matches) == 1, html
    path, kind, raw = matches[0]
    return path, kind, raw


def nowhere_in_the_database(raw: str) -> None:
    assert not PortalToken.objects.filter(token_hash__contains=raw).exists()
    assert not AuditLog.objects.filter(Q(after__icontains=raw) | Q(before__icontains=raw)).exists()
    assert not OutboundMessage.objects.filter(
        Q(rendered_body__contains=raw) | Q(rendered_subject__contains=raw)
    ).exists()
    for session in Session.objects.all():
        assert raw not in str(session.get_decoded())


# --- Allowed ---------------------------------------------------------------------------------


def test_an_invited_account_gets_a_fresh_invitation_link_shown_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    operator = an_operator()
    user = invited(operator)
    older, older_raw = tokens.issue(user, TokenKind.INVITE)
    tokens.keep(older)
    staff, actor = client_for(Role.ADMIN)
    with caplog.at_level(logging.DEBUG):
        response = copy(staff, operator)
    assert response.status_code == 200
    path, kind, raw = found(response)
    assert (kind, path) == ("invite", f"/portal/invite/{raw}/")
    html = response.content.decode()
    # Once: the field only; the copy button and the sentence under it.
    assert html.count(raw) == 1
    assert 'id="portal-copied-link-copy"' in html and "ledger/copy_link.js" in html
    assert "Sandbox only. Anyone with this link can set this operator&#x27;s password" in html
    assert "no-store" in response["Cache-Control"]
    assert response["Referrer-Policy"] == "no-referrer"
    assert list(get_messages(response.wsgi_request)) == []
    # The new token is the one live invitation; the older one is voided.
    token = PortalToken.objects.get(token_hash=tokens.digest(raw))
    assert token.kind == TokenKind.INVITE and token.used_at is None
    assert token.expires_at - token.created_at >= tokens.INVITE_LIFETIME - timedelta(seconds=5)
    older.refresh_from_db()
    assert older.used_at is not None
    assert tokens.find(older_raw, TokenKind.INVITE) is None
    audit = AuditLog.objects.get(action=invitations.LINK_COPIED)
    assert audit.actor == actor and audit.entity_id == str(user.pk)
    assert audit.after == {
        "operator": str(operator.pk),
        "kind": "invite",
        "expires_at": token.expires_at.isoformat(),
    }
    assert all(raw not in record.getMessage() for record in caplog.records)
    nowhere_in_the_database(raw)
    # The link sets the first password, once.
    assert Client().get(path).status_code == 200
    done = Client().post(path, {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert done.status_code == 302
    user.refresh_from_db()
    assert user.can_sign_in
    assert Client().get(path).status_code == 404


def test_an_active_account_gets_a_reset_link_and_the_older_one_stops_working() -> None:
    operator = an_operator()
    user = active(operator)
    older, older_raw = tokens.issue(user, TokenKind.RESET)
    tokens.keep(older)
    staff, _ = client_for(Role.ADMIN)
    response = copy(staff, operator)
    assert response.status_code == 200
    path, kind, raw = found(response)
    assert (kind, path) == ("reset", f"/portal/reset/{raw}/")
    token = PortalToken.objects.get(token_hash=tokens.digest(raw))
    assert token.kind == TokenKind.RESET
    assert tokens.find(older_raw, TokenKind.RESET) is None
    assert not PortalToken.objects.filter(kind=TokenKind.INVITE).exists()
    assert AuditLog.objects.get(action=invitations.LINK_COPIED).after["kind"] == "reset"
    nowhere_in_the_database(raw)
    done = Client().post(path, {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert done.status_code == 302
    assert Client().post(path, {"password": PASSWORD, "confirm": PASSWORD}).status_code == 404


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_the_button_shows_to_an_admin_on_the_sandbox_only(sandbox: Any, app_env: str) -> None:
    sandbox.APP_ENV = app_env
    operator = an_operator()
    invited(operator)
    admin, _ = client_for(Role.ADMIN)
    assert BUTTON in page(admin, operator)
    assert BUTTON not in page(client_for(Role.BILLING)[0], operator)
    assert BUTTON not in page(client_for(Role.VIEWER)[0], operator)


def test_the_outcome_never_shows_the_link_in_its_repr() -> None:
    operator = an_operator()
    user = invited(operator)
    outcome = invitations.copy_link(operator.pk, client_for(Role.ADMIN)[1])
    assert outcome.ok and outcome.link.startswith("http://localhost:8000/portal/invite/")
    assert outcome.link not in repr(outcome)
    assert outcome.expires_at == PortalToken.objects.get(user=user).expires_at


# --- Refused ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.BILLING, Role.VIEWER])
def test_billing_and_viewer_are_refused(role: Role) -> None:
    operator = an_operator()
    invited(operator)
    staff, actor = client_for(role)
    assert copy(staff, operator).status_code == 403
    assert not PortalToken.objects.exists()
    assert not AuditLog.objects.filter(action=invitations.LINK_COPIED).exists()
    # The service refuses too, whoever calls it.
    outcome = invitations.copy_link(operator.pk, actor)
    assert not outcome.ok and outcome.message == invitations.ADMIN_ONLY and outcome.link == ""
    assert not PortalToken.objects.exists()


def test_a_get_is_405() -> None:
    operator = an_operator()
    invited(operator)
    admin, _ = client_for(Role.ADMIN)
    assert admin.get(f"/operators/{operator.pk}/portal/copy-link/").status_code == 405
    assert not PortalToken.objects.exists()


def test_the_route_is_admin_and_post_only_in_the_registry() -> None:
    rule = registry.rule_for("ledger:operators:portal_copy_link")
    assert rule is not None and rule.min_role is Role.ADMIN and rule.methods == {"POST"}


def _refused(staff: Client, operator: Operator, sentence: str) -> None:
    response = copy(staff, operator)
    assert response.status_code == 302
    assert response["Location"] == f"/operators/{operator.pk}/#portal-card"
    assert [str(m) for m in get_messages(response.wsgi_request)] == [sentence]
    assert not PortalToken.objects.exists()
    assert not AuditLog.objects.filter(action=invitations.LINK_COPIED).exists()
    assert BUTTON not in page(staff, operator)
    assert 'id="portal-copied-link-field"' not in page(staff, operator)


@pytest.mark.parametrize(
    ("stax_env", "app_env"),
    [("live", "production"), ("live", "staging"), ("sandbox", "production")],
)
def test_a_live_key_or_production_is_refused(sandbox: Any, stax_env: str, app_env: str) -> None:
    sandbox.STAX_ENV = stax_env
    sandbox.APP_ENV = app_env
    operator = an_operator()
    invited(operator)
    _refused(client_for(Role.ADMIN)[0], operator, invitations.SANDBOX_ONLY)


def test_an_archived_operator_is_refused() -> None:
    operator = an_operator()
    invited(operator)
    Operator.objects.filter(pk=operator.pk).update(archived_at=timezone.now())
    _refused(client_for(Role.ADMIN)[0], operator, invitations.ARCHIVED)


def test_a_disabled_account_is_refused() -> None:
    operator = an_operator()
    user = active(operator)
    PortalUser.objects.filter(pk=user.pk).update(is_active=False, disabled_at=timezone.now())
    _refused(client_for(Role.ADMIN)[0], operator, invitations.IS_DISABLED)


def test_an_operator_without_an_account_is_refused() -> None:
    operator = an_operator()
    _refused(client_for(Role.ADMIN)[0], operator, invitations.NO_ACCOUNT)


def test_an_unknown_operator_is_404() -> None:
    admin, _ = client_for(Role.ADMIN)
    missing = "/operators/00000000-0000-0000-0000-000000000000/portal/copy-link/"
    assert admin.post(missing).status_code == 404
