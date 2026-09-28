"""Fable's adversarial cases for the SANDBOX banner and secret non-leakage (PLAN-002 §5)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings as dj_settings
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.test import Client, override_settings

BANNER_TEXT = "SANDBOX — NO REAL MONEY"


@pytest.mark.django_db
def test_admin_login_has_exactly_one_banner_in_sandbox(client: Client) -> None:
    html = client.get("/admin/login/").content.decode()
    assert html.count('id="sandbox-banner"') == 1
    assert BANNER_TEXT in html
    title = html.split("<title>", 1)[1].split("</title>", 1)[0]
    assert title.lstrip().startswith("[SANDBOX] ")


@pytest.mark.django_db
def test_banner_has_no_interactive_or_dismiss_affordance(client: Client) -> None:
    html = client.get("/admin/login/").content.decode()
    start = html.index('id="sandbox-banner"')
    tag_start = html.rfind("<div", 0, start)
    banner = html[tag_start : html.index("</div>", start) + 6].lower()
    forbidden_markers = (
        "<button",
        "onclick",
        "close",
        "dismiss",
        "<a ",
        "display:none",
        "display: none",
    )
    for forbidden in forbidden_markers:
        assert forbidden not in banner, forbidden


@pytest.mark.django_db
def test_banner_absent_when_live(client: Client) -> None:
    with override_settings(STAX_ENV="live"):
        html = client.get("/admin/login/").content.decode()
    assert "sandbox-banner" not in html
    assert BANNER_TEXT not in html


def test_banner_not_injected_into_json(client: Client) -> None:
    body = client.get("/healthz").content.decode()
    assert "sandbox-banner" not in body
    assert body.lstrip().startswith("{")


def test_middleware_skips_streaming_and_non_html() -> None:
    from finance.middleware import SandboxBannerMiddleware

    def html_stream(_request: object) -> StreamingHttpResponse:
        return StreamingHttpResponse(
            iter([b"<html><body>x</body></html>"]), content_type="text/html"
        )

    def plain(_request: object) -> HttpResponse:
        return HttpResponse("<body>not html</body>", content_type="text/plain")

    def json(_request: object) -> JsonResponse:
        return JsonResponse({"body": "<body>"})

    for view in (html_stream, plain, json):
        resp = SandboxBannerMiddleware(view)(object())  # type: ignore[arg-type]
        if isinstance(resp, StreamingHttpResponse):
            content = b"".join(resp.streaming_content)  # type: ignore[arg-type]
        else:
            assert isinstance(resp, HttpResponse)
            content = resp.content
        assert b"sandbox-banner" not in content


def test_healthz_has_no_secret_names_or_values(client: Client) -> None:
    body = client.get("/healthz").content.decode()
    assert "STAX_API_KEY" not in body
    assert "TOKEN" not in body.upper()
    if dj_settings.STAX_API_KEY:
        assert dj_settings.STAX_API_KEY not in body


@pytest.mark.django_db
def test_rendered_pages_never_contain_api_key(client: Client) -> None:
    key = dj_settings.STAX_API_KEY
    assert key, "test env must set a distinctive fake key"
    for path in ("/admin/login/", "/healthz", "/does-not-exist/"):
        assert key not in client.get(path).content.decode(errors="ignore")


def test_no_template_or_static_references_secret_settings() -> None:
    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"STAX_API_KEY|STAX_WEBHOOK_URL_TOKEN|apiprod\.fattlabs\.com")
    offenders = [
        str(p)
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".html", ".js", ".css", ".txt", ".svg"}
        and "staticfiles" not in p.parts
        and pattern.search(p.read_text(errors="ignore"))
    ]
    assert offenders == []
