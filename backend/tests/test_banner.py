"""The sandbox banner is present, permanent, and leaks nothing (docs/CLAUDE.md Rule 6)."""

from __future__ import annotations

import re

import pytest
from django.test import Client, override_settings

# The two-factor login page keeps its wizard state in the database-backed session.
pytestmark = pytest.mark.django_db

BANNER_TEXT = "SANDBOX — test system, no real money"
BANNER_ID = 'id="sandbox-banner"'

_BANNER_ELEMENT_RE = re.compile(r"<div id=\"sandbox-banner\".*?</div>", re.DOTALL)


def _admin_login_html(client: Client) -> str:
    # PLAN-003: /admin/login/ now answers 401 to an anonymous request; the login page
    # staff actually use is the two-factor login.
    response = client.get("/account/login/")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    return response.content.decode()


def test_banner_is_injected_once_into_admin_html(client: Client) -> None:
    html = _admin_login_html(client)
    assert html.count(BANNER_ID) == 1
    assert html.count(BANNER_TEXT) == 1


def test_banner_text_is_exact(client: Client) -> None:
    match = _BANNER_ELEMENT_RE.search(_admin_login_html(client))
    assert match is not None
    element = match.group(0)
    # PLAN-012f D4: the full text, then the phone's shorter one (shown at 768 px and under).
    assert (
        f'><span class="sandbox-long">{BANNER_TEXT}</span>'
        '<span class="sandbox-short">SANDBOX — test system</span></div>'
    ) in element


def test_banner_sits_after_the_body_tag(client: Client) -> None:
    html = _admin_login_html(client)
    body_index = html.index("<body")
    body_tag_end = html.index(">", body_index)
    assert html[body_tag_end + 1 :].lstrip().startswith("<style>")
    assert html.index(BANNER_ID) > body_tag_end


def test_banner_is_fixed_full_width_and_yellow(client: Client) -> None:
    match = _BANNER_ELEMENT_RE.search(_admin_login_html(client))
    assert match is not None
    element = match.group(0)
    assert "position:fixed" in element
    assert "top:0" in element
    assert "width:100%" in element
    # The approved design (docs/design/DESIGN-SPEC.md §1, PLAN-012f D3): #6B4E00 on #FFF3C4,
    # 12 px, 600, letter-spacing .04em, upper case. PLAN-012g A5: the sandbox tokens, with the
    # light values as fallbacks for the pages that never load tokens.css (this admin page).
    assert "background:var(--sandbox-bg,#FFF3C4);color:var(--sandbox-text,#6B4E00);" in element
    assert "font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;" in (
        element
    )
    assert 'role="status"' in element


def test_the_phone_reads_the_short_text(client: Client) -> None:
    html = _admin_login_html(client)
    after_body = html[html.index("<body") :]
    style = after_body[after_body.index("<style>") : after_body.index("</style>")]
    assert ".sandbox-short{display:none;}" in style
    assert (
        "@media (max-width:768px){.sandbox-long{display:none;}.sandbox-short{display:inline;}}"
    ) in style


def test_banner_pushes_content_down(client: Client) -> None:
    assert "body{padding-top:26px !important;}" in _admin_login_html(client)


def test_banner_has_no_dismiss_control(client: Client) -> None:
    match = _BANNER_ELEMENT_RE.search(_admin_login_html(client))
    assert match is not None
    element = match.group(0).lower()
    for forbidden in ("button", "onclick", "close", "dismiss", "<a ", "href"):
        assert forbidden not in element


def test_title_is_prefixed(client: Client) -> None:
    assert "<title>[SANDBOX] " in _admin_login_html(client)


@override_settings(STAX_ENV="live")
def test_no_banner_when_live(client: Client) -> None:
    html = _admin_login_html(client)
    assert BANNER_ID not in html
    assert BANNER_TEXT not in html
    assert "[SANDBOX]" not in html


def test_healthz_is_json_without_a_banner(client: Client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    body = response.content.decode()
    assert BANNER_ID not in body
    assert body == '{"status": "ok", "app_env": "local", "stax_env": "sandbox"}'


def test_no_stax_credentials_reach_the_page(client: Client, fake_stax_api_key: str) -> None:
    for path in ("/admin/login/", "/healthz"):
        body = client.get(path).content.decode()
        assert fake_stax_api_key not in body
        assert "STAX_API_KEY" not in body
        assert "apiprod.fattlabs.com" not in body
