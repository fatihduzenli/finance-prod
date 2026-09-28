"""The sandbox banner is present, permanent, and leaks nothing (docs/CLAUDE.md Rule 6)."""

from __future__ import annotations

import re

from django.test import Client, override_settings

BANNER_TEXT = "SANDBOX — NO REAL MONEY"
BANNER_ID = 'id="sandbox-banner"'

_BANNER_ELEMENT_RE = re.compile(r"<div id=\"sandbox-banner\".*?</div>", re.DOTALL)


def _admin_login_html(client: Client) -> str:
    response = client.get("/admin/login/")
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
    assert f">{BANNER_TEXT}</div>" in element


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
    assert "#ffd400" in element
    assert 'role="status"' in element


def test_banner_pushes_content_down(client: Client) -> None:
    assert "body{padding-top:44px !important;}" in _admin_login_html(client)


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
