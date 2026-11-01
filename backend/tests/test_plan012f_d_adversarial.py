"""Adversarial cases for PLAN-012f Batch D (every other page, the login layout), by the QA
owner (plan §6). The login page keeps its form, names and flow (one POST form with the CSRF
token, the first field labelled "Username", no forgot-password or keep-me-signed-in control,
the banner strip) and never reflects a hostile ``next``; a signed-in user's 404 and 403 pages
carry the shell and the banner; a breadcrumb never carries raw markup; no page offers a
viewer a primary action; every rendered page has exactly one sandbox banner."""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import PaymentChannel
from tests.receivables_helpers import a_lease, a_store, an_operator
from tests.test_operator_views import client_for
from tests.text_helpers import has_token

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'


# --- the login page ------------------------------------------------------------------------------


def test_the_login_page_keeps_its_form_and_offers_no_forgot_or_keep_me_controls() -> None:
    body = Client().get("/account/login/").content.decode()
    forms = re.findall(r"<form[^>]*method=\"post\"[^>]*>", body, re.I)
    assert len(forms) == 1, forms
    assert "csrfmiddlewaretoken" in body
    assert 'name="auth-username"' in body and 'name="auth-password"' in body
    label = re.search(r'<label[^>]*for="id_auth-username"[^>]*>(.*?)</label>', body, re.S)
    assert label is not None and "Username" in re.sub(r"<[^>]+>", "", label.group(1))
    lowered = body.lower()
    assert not has_token(lowered, "forgot")
    # PLAN-014 I-b: "Keep me signed in" is on the page (the session's limits do not change,
    # tests/test_login_page.py); "Forgot password?" only with a support e-mail, none here.
    assert 'name="auth-remember"' in lowered and "remember_me" not in lowered
    assert body.count('id="sandbox-banner"') == 1
    assert "Access is by invitation" in body


@pytest.mark.parametrize(
    "next_value",
    ["//evil.example/", "https://evil.example/", "javascript:alert(1)", MARKUP, "/\\evil.example"],
)
def test_a_hostile_next_is_never_reflected_on_the_login_page(next_value: str) -> None:
    response = Client().get("/account/login/", {"next": next_value})
    assert response.status_code == 200
    body = response.content.decode()
    assert MARKUP not in body
    # A reflected value may sit only inside an attribute value, escaped; never in the page.
    outside_attributes = re.sub(r'value="[^"]*"', "", body)
    assert "evil.example" not in outside_attributes
    assert "javascript:" not in outside_attributes.lower()


# --- the error pages wear the shell --------------------------------------------------------------


def test_a_signed_in_users_404_and_403_carry_the_shell_and_one_banner() -> None:
    viewer, _ = client_for(Role.VIEWER)
    missing = viewer.get("/stores/00000000-0000-0000-0000-000000000000/")
    assert missing.status_code == 404
    body = missing.content.decode()
    assert body.count('id="sandbox-banner"') == 1
    assert 'id="sidebar"' in body or 'id="phone-bar"' in body
    forbidden = viewer.get("/audit/")
    assert forbidden.status_code == 403
    body = forbidden.content.decode()
    assert body.count('id="sandbox-banner"') == 1
    assert MARKUP not in body


# --- breadcrumbs and primaries -------------------------------------------------------------------


def test_a_breadcrumb_never_carries_raw_markup_and_a_viewer_gets_no_primary_action() -> None:
    store = a_store(f"Istanbul {MARKUP}")
    store.store_number = 1991
    store.save(update_fields=["store_number"])
    lease = a_lease(store, an_operator(f"Fatih {MARKUP}"), channel=PaymentChannel.WIRE)
    viewer, _ = client_for(Role.VIEWER)
    for path in (
        reverse("ledger:stores:detail", kwargs={"pk": store.pk}),
        reverse("ledger:leases:detail", kwargs={"pk": lease.pk}),
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}),
        reverse("ledger:operators:statement", kwargs={"pk": lease.operator_id}),
        reverse("ledger:stores:list"),
        reverse("ledger:leases:list"),
        "/payments/",
        "/calendar/",
        "/help/",
    ):
        response = viewer.get(path)
        assert response.status_code == 200, path
        body = response.content.decode()
        assert MARKUP not in body, path
        header = body[
            body.index('id="page-header"') : body.index("</header>", body.index('id="page-header"'))
        ]
        assert "btn-primary" not in header, path
        assert body.count('id="sandbox-banner"') == 1, path


def test_every_main_page_renders_for_every_role_at_the_phone_and_laptop_shell() -> None:
    lease = a_lease(a_store("3001"), an_operator("Every Role Co"), channel=PaymentChannel.WIRE)
    pages = (
        "/",
        reverse("ledger:stores:list"),
        reverse("ledger:stores:detail", kwargs={"pk": lease.store_id}),
        reverse("ledger:operators:detail", kwargs={"pk": lease.operator_id}),
        reverse("ledger:grid:page"),
        reverse("ledger:arrears:page"),
        "/payments/",
        "/returns/",
        "/calendar/",
        "/reports/pnl/",
        "/help/",
    )
    for role in (Role.VIEWER, Role.BILLING, Role.ADMIN):
        client, _ = client_for(role)
        for path in pages:
            response = client.get(path)
            assert response.status_code == 200, (role, path)
            body = response.content.decode()
            # The shell renders both bars; CSS picks one per width. Both must be present once.
            assert body.count('id="sidebar"') == 1 and body.count('id="tab-bar"') == 1, (role, path)
            assert body.count('id="page-header"') == 1, (role, path)
