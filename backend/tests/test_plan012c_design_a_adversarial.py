"""Adversarial cases for PLAN-012c Batch A (tokens, company settings, the shell, PWA), by the
QA owner (plan §6). A company or provider name with markup is escaped in the bar, the title and
the sentences; the logo override cannot point outside the documents storage; the phone menu
works without JavaScript; the sandbox banner stays and cannot be dismissed; the manifest never
carries markup. (The file name says "design" because ``test_plan012c_a_adversarial.py`` is the
earlier PLAN-012c-a's.)"""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import CompanySettings
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
NAME = f"Monvera {MARKUP}"


def _settings(**fields: object) -> CompanySettings:
    row = CompanySettings.current()
    for name, value in fields.items():
        setattr(row, name, value)
    row.save()
    return row


# --- names with markup never reach the page raw ---------------------------------------


def test_a_company_or_provider_name_with_markup_is_escaped_everywhere() -> None:
    _settings(name=NAME, short_name=NAME, provider_name=f"Stax {MARKUP}")
    viewer, _ = client_for(Role.VIEWER)
    for path in ("/", reverse("ledger:stores:list"), reverse("ledger:search") + "?q=x"):
        response = viewer.get(path)
        assert response.status_code == 200, path
        body = response.content.decode()
        assert MARKUP not in body, path
        assert "&lt;script&gt;" in body, path
    login = Client().get("/account/login/")
    assert login.status_code == 200 and MARKUP not in login.content.decode()


def test_the_manifest_carries_the_name_as_data_and_the_brand_icons_exist() -> None:
    """The manifest stays behind the sign-in (PLAN-011 D9); the icons are the brand files."""
    from django.contrib.staticfiles import finders

    _settings(name=NAME, short_name=NAME)
    assert Client().get("/manifest.webmanifest").status_code in (401, 404)
    viewer, _ = client_for(Role.VIEWER)
    manifest = None
    for path in ("/manifest.webmanifest", "/manifest.json"):
        response = viewer.get(path)
        if response.status_code == 200:
            manifest = response
            break
    assert manifest is not None, "no manifest route answered a signed-in viewer"
    data = json.loads(manifest.content)
    assert isinstance(data["name"], str) and MARKUP in data["name"]  # data, not markup
    icons = data.get("icons", [])
    assert icons and all("brand" in icon["src"] for icon in icons)
    for icon in icons:
        static_path = icon["src"].split("/static/", 1)[1].split("?")[0]
        assert finders.find(static_path), icon["src"]


# --- the logo override stays inside the documents storage ----------------------------------


@pytest.mark.parametrize(
    "logo",
    [
        "https://evil.example/logo.svg",
        "//evil.example/logo.svg",
        "../../../etc/passwd",
        "javascript:alert(1)",
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
    ],
)
def test_a_logo_override_cannot_point_outside_the_documents_storage(logo: str) -> None:
    row = CompanySettings.current()
    row.logo = logo
    try:
        row.full_clean()
        row.save()
    except Exception:  # noqa: BLE001 - a validation refusal is the good outcome
        return
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get("/").content.decode()
    assert "evil.example" not in body and "javascript:" not in body
    assert "data:image" not in body and "etc/passwd" not in body
    # The bar falls back to the brand logo.
    assert "monvera_logo_dark" in body


# --- the phone menu and the banner -----------------------------------------------------------


def test_the_phone_menu_works_without_javascript_and_the_banner_stays() -> None:
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get("/").content.decode()
    assert 'id="nav-toggle"' in body and 'type="checkbox"' in body
    assert 'for="nav-toggle"' in body  # the label toggles it: no script needed
    assert "SANDBOX" in body
    # PLAN-012f D11: the sidebar item reads "Monthly rent" (the page title keeps "overview").
    for label in ("Dashboard", "Stores", "Operators", "Monthly rent", "Reports", "Admin"):
        assert label in body, label
    assert "Rent run" not in body and ">Grid<" not in body
    anonymous = Client().get("/account/login/").content.decode()
    assert "SANDBOX" in anonymous
    title = anonymous.split("<title>")[1].split("</title>")[0]
    assert "Monvera" in title
