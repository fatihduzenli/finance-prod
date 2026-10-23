"""Adversarial cases for PLAN-012f Batch A (tokens, the sidebar, the page header, the phone
shell), by the QA owner (plan §6). A company name with markup is escaped in the sidebar and the
user card; the sidebar's groups show only the pages the role may open; the More sheet offers
Sign out as a POST form and nothing a viewer may not open; the bottom bar's active item follows
the page; the search box is a GET form with a bounded query; the fonts load from the app's
origin only (`tests/e2e/test_fonts_12f.py`); the banner is one element on every
page with the title prefix kept."""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from accounts.roles import Role
from ledger.models import CompanySettings
from tests.test_operator_views import client_for

pytestmark = pytest.mark.django_db

MARKUP = '<script>alert("x")</script>'
ADMIN_ONLY = ("/audit/", "/admin/")  # Settings is every role's own page (owner, 2026-10-22)


def _block(body: str, element_id: str, closing: str) -> str:
    start = body.index(f'id="{element_id}"')
    return body[start : body.index(closing, start)]


# --- escaping in the shell -----------------------------------------------------------------------


def test_a_company_name_with_markup_is_escaped_in_the_sidebar_and_the_user_card() -> None:
    row = CompanySettings.current()
    row.name = f"Monvera {MARKUP}"
    row.short_name = f"Mv {MARKUP}"
    row.save()
    admin, _ = client_for(Role.ADMIN)
    body = admin.get("/").content.decode()
    assert MARKUP not in body
    assert 'id="sidebar"' in body and 'id="user-card"' in body
    assert MARKUP not in body.split("<title>")[1].split("</title>")[0]


# --- the sidebar and the More sheet follow the role ----------------------------------------------


def test_a_viewer_sees_no_admin_page_in_the_sidebar_nor_in_the_more_sheet() -> None:
    viewer, _ = client_for(Role.VIEWER)
    body = viewer.get("/").content.decode()
    sidebar = (
        _block(body, "sidebar", "</aside>")
        if "</aside>" in body
        else _block(body, "sidebar", "</nav>")
    )
    for path in ADMIN_ONLY:
        assert f'href="{path}' not in sidebar, path
    if 'id="more-sheet"' in body:
        sheet = _block(body, "more-sheet", "</details>")
        for path in ADMIN_ONLY:
            assert f'href="{path}' not in sheet, path
        # Sign out is a POST form, never a link.
        assert "<form" in sheet and 'method="post"' in sheet
        assert not re.search(r'<a [^>]*href="[^"]*sign-out', sheet)


def test_sign_out_in_the_user_card_is_a_post_form_and_a_get_signs_nobody_out() -> None:
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/").content.decode()
    card = _block(body, "user-card", "</div>")
    assert 'method="post"' in card and "csrfmiddlewaretoken" in card
    action = re.search(r'<form [^>]*action="([^"]+)"', card)
    assert action is not None
    response = billing.get(action.group(1))
    assert response.status_code in (405, 403, 302)
    assert billing.get("/").status_code == 200  # still signed in


# --- the bottom bar's active item ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path, active",
    [
        ("/", "tab-dashboard"),
        ("/stores/", "tab-stores"),
        ("/grid/", "tab-grid"),
        ("/operators/", "tab-more"),
        ("/arrears/", "tab-more"),
    ],
)
def test_the_bottom_bar_marks_the_page_it_is_on_and_only_that_one(path: str, active: str) -> None:
    billing, _ = client_for(Role.BILLING)
    body = billing.get(path).content.decode()
    bar = _block(body, "tab-bar", "</nav>")
    marked = re.findall(r'id="(tab-[a-z]+)"[^>]*aria-current="page"', bar) + re.findall(
        r'aria-current="page"[^>]*id="(tab-[a-z]+)"', bar
    )
    assert marked == [active], (path, marked)
    assert bar.count("<a ") + bar.count("<summary") >= 4


# --- the search box and the banner ---------------------------------------------------------------


def test_the_header_search_is_a_bounded_get_form_and_a_hostile_query_echoes_nothing() -> None:
    billing, _ = client_for(Role.BILLING)
    body = billing.get("/").content.decode()
    form = re.search(r'<form [^>]*id="nav-search"[^>]*>(.*?)</form>', body, re.S)
    assert form is not None
    assert 'method="get"' in form.group(0)
    assert 'maxlength="100"' in form.group(1)
    hostile = billing.get(reverse("ledger:search"), {"q": MARKUP})
    assert hostile.status_code == 200 and MARKUP not in hostile.content.decode()


def test_the_banner_is_one_element_on_every_page_and_the_title_prefix_stays() -> None:
    admin, _ = client_for(Role.ADMIN)
    for path in ("/", "/stores/", "/grid/", "/help/", "/admin/"):
        body = admin.get(path).content.decode()
        assert body.count('id="sandbox-banner"') == 1, path
        assert "<title>[SANDBOX]" in body, path
    anonymous = Client().get("/account/login/").content.decode()
    assert anonymous.count('id="sandbox-banner"') == 1
