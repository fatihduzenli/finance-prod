"""PLAN-012g Batch A (Fable): Light · Dark · System per user. The server renders
``<html data-theme>`` from the stored choice (System for anonymous and for a user without a
row), the control saves through one POST with a validated ``next``, the theme-color meta follows,
the invoice preview stays light, and the sign-in shell follows System. Plus the adversarial cases
of plan §5: a hostile value, a hostile ``next``, no CSRF, GET, a viewer, a row that vanishes."""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from accounts.models import UserPreference
from accounts.roles import Role
from accounts.themes import THEMES, set_theme, theme_for
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

THEME_URL = "finance:theme"


def html_tag(page: str) -> str:
    match = re.search(r"<html[^>]*>", page)
    assert match, page[:200]
    return match.group(0)


def theme_metas(page: str) -> list[str]:
    return re.findall(r'<meta name="theme-color"[^>]*>', page)


# --- What the server renders ---------------------------------------------------------------------


def test_a_user_without_a_row_and_the_sign_in_shell_follow_system() -> None:
    client, user = client_for(Role.BILLING)
    assert theme_for(user) == "system" and not UserPreference.objects.filter(user=user).exists()
    assert html_tag(client.get("/").content.decode()) == '<html lang="en" data-theme="system">'
    anonymous = Client().get("/account/login/").content.decode()
    assert html_tag(anonymous) == '<html lang="en" data-theme="system">'
    assert sorted(theme_metas(anonymous)) == sorted(theme_metas(client.get("/").content.decode()))


@pytest.mark.parametrize("theme", THEMES)
def test_the_stored_choice_reaches_every_page_and_the_meta(theme: str) -> None:
    client, user = client_for(Role.ADMIN)
    set_theme(user, theme)
    for url in ("/", reverse("finance:settings"), "/stores/"):
        page = client.get(url, follow=True).content.decode()
        assert html_tag(page) == f'<html lang="en" data-theme="{theme}">', url
        metas = theme_metas(page)
        if theme == "system":
            assert len(metas) == 2
            assert any("prefers-color-scheme: dark" in m and "#171721" in m for m in metas)
            assert any("prefers-color-scheme: light" in m and "#0b2545" in m for m in metas)
        else:
            assert len(metas) == 1 and ("#171721" if theme == "dark" else "#0b2545") in metas[0]


def test_the_control_marks_the_current_choice_in_the_sidebar_the_sheet_and_settings() -> None:
    client, user = client_for(Role.VIEWER)
    set_theme(user, "dark")
    page = client.get(reverse("finance:settings")).content.decode()
    forms = re.findall(
        r'<form[^>]*id="(?:theme-switch|more-theme-switch|settings-theme-switch)"[^>]*>.*?</form>',
        page,
        re.S,
    )
    assert len(forms) == 3, "the sidebar, the More sheet and the settings card carry the control"
    for form in forms:
        pressed = re.findall(r'value="(\w+)" aria-pressed="true"', form)
        assert pressed == ["dark"], form
        assert 'name="csrfmiddlewaretoken"' in form
        assert set(re.findall(r'name="theme" value="(\w+)"', form)) == set(THEMES)
    assert "Appearance" in page


def test_the_invoice_preview_is_light_whatever_the_user_chose() -> None:
    from tests.receivables_helpers import a_lease, a_store, an_operator
    from tests.test_invoice_builder import a_draft, two_lines

    client, user = client_for(Role.BILLING)
    set_theme(user, "dark")
    operator = an_operator("Acme Stores")
    draft = a_draft(operator, a_lease(a_store(), operator), two_lines())
    preview = client.get(f"/invoices/drafts/{draft.pk}/preview/").content.decode()
    assert html_tag(preview) == '<html lang="en" data-theme="light">'
    printed = client.get(f"/invoices/drafts/{draft.pk}/print/").content.decode()
    assert "data-theme" not in html_tag(printed)  # standalone, no attribute: the light tokens


# --- Saving ---------------------------------------------------------------------------------


@pytest.mark.parametrize("theme", THEMES)
def test_saving_redirects_back_and_sticks(theme: str) -> None:
    client, user = client_for(Role.VIEWER)  # any signed-in role
    response = client.post(reverse(THEME_URL), {"theme": theme, "next": "/stores/?status=active"})
    assert (response.status_code, response["Location"]) == (302, "/stores/?status=active")
    assert theme_for(user) == theme
    assert UserPreference.objects.filter(user=user).count() == 1
    client.post(reverse(THEME_URL), {"theme": "light", "next": "/"})
    assert UserPreference.objects.filter(user=user).count() == 1  # one row, updated


@pytest.mark.parametrize(
    "value",
    ["", "DARK", "blue", "<script>alert(1)</script>", "dark; drop", "d" * 300, "system\n", "1"],
)
def test_a_value_outside_the_three_is_refused_and_nothing_is_saved(value: str) -> None:
    client, user = client_for(Role.ADMIN)
    response = client.post(reverse(THEME_URL), {"theme": value, "next": "/"})
    assert response.status_code == 400
    assert value not in response.content.decode() or len(value) < 2  # never echoed
    assert not UserPreference.objects.filter(user=user).exists()


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example.com/",
        "//evil.example.com/x",
        "javascript:alert(1)",
        "/\\evil.example.com",
        "http://testserver@evil.example.com/",
        "https://evil.example.com/" + "a" * 300,
        "",
    ],
)
def test_a_hostile_next_falls_back_to_the_dashboard(hostile: str) -> None:
    client, user = client_for(Role.BILLING)
    response = client.post(reverse(THEME_URL), {"theme": "dark", "next": hostile})
    assert response.status_code == 302
    assert response["Location"] == "/", hostile
    assert theme_for(user) == "dark"  # the choice is saved; only the redirect is tamed


def test_get_is_not_allowed_and_anonymous_cannot_save() -> None:
    client, _ = client_for(Role.BILLING)
    assert client.get(reverse(THEME_URL)).status_code == 405
    anonymous = Client()
    response = anonymous.post(reverse(THEME_URL), {"theme": "dark", "next": "/"})
    assert response.status_code in (302, 401, 403)
    assert not UserPreference.objects.exists()


def test_without_the_csrf_token_the_post_is_refused() -> None:
    _, user = client_for(Role.BILLING)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(user)
    response = strict.post(reverse(THEME_URL), {"theme": "dark", "next": "/"})
    assert response.status_code == 403
    assert theme_for(user) == "system"


def test_a_row_deleted_between_requests_is_recreated_not_crashed() -> None:
    client, user = client_for(Role.ADMIN)
    set_theme(user, "dark")
    UserPreference.objects.filter(user=user).delete()
    assert theme_for(user) == "system"
    assert client.post(reverse(THEME_URL), {"theme": "light", "next": "/"}).status_code == 302
    assert theme_for(user) == "light"


def test_set_theme_refuses_a_value_outside_the_three() -> None:
    _, user = client_for(Role.VIEWER)
    with pytest.raises(ValueError):
        set_theme(user, "sepia")
    assert not UserPreference.objects.filter(user=user).exists()
