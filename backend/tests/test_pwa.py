"""Install to the home screen (docs/qa/PLAN-011-dashboard-reports-mobile.md D9, §6, §7):
the web app manifest, the service worker's allow-list and fetch rule, and
``Cache-Control: private, no-store`` on every non-static response.

The worker is checked as source: its allow-list holds only ``/static/`` paths, it stores a
response in one place only, inside the static branch, and it never answers a navigation.
The browser side (registration, what lands in the cache) is in
``tests/e2e/test_mobile.py``.
"""

from __future__ import annotations

import json
import re
import struct
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.utils.cache import patch_cache_control

from accounts.roles import Role
from finance import pwa
from finance.middleware import PrivateNoStoreMiddleware
from tests.auth_helpers import make_user, verified_client

pytestmark = pytest.mark.django_db

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(autouse=True)
def fresh_precache() -> Iterator[None]:
    pwa.precache.cache_clear()
    yield
    pwa.precache.cache_clear()


def client_for(role: Role = Role.VIEWER) -> Client:
    user, device = make_user(f"pwa-{uuid.uuid4().hex[:8]}", role)
    assert device is not None
    return verified_client(user, device)


def worker() -> str:
    response = client_for().get("/sw.js")
    assert response.status_code == 200
    return response.content.decode()


def embedded(source: str, name: str) -> object:
    match = re.search(rf"^const {name} = (.*);$", source, re.MULTILINE)
    assert match is not None, name
    return json.loads(match.group(1))


def fetch_handler(source: str) -> str:
    start = source.index('self.addEventListener("fetch"')
    return source[start:]


# --- Manifest ---------------------------------------------------------------------------------


def test_the_manifest_is_served_as_a_web_app_manifest() -> None:
    response = client_for().get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/manifest+json"
    data = json.loads(response.content)
    assert data["name"] == "Finance" and data["short_name"] == "Finance"
    assert data["start_url"] == "/" and data["scope"] == "/"
    assert data["display"] == "standalone"
    assert re.fullmatch(r"#[0-9a-f]{6}", data["background_color"])
    assert re.fullmatch(r"#[0-9a-f]{6}", data["theme_color"])
    assert [(icon["sizes"], icon["type"]) for icon in data["icons"]] == [
        ("192x192", "image/png"),
        ("512x512", "image/png"),
    ]


@pytest.mark.parametrize(("path", "size"), [(path, sizes) for path, sizes in pwa.ICONS])
def test_each_icon_is_a_png_of_its_declared_size(path: str, size: str) -> None:
    found = finders.find(path)
    assert found is not None, path
    head = Path(str(found)).read_bytes()[:24]
    assert head[:8] == PNG_SIGNATURE
    width, height = struct.unpack(">II", head[16:24])
    assert f"{width}x{height}" == size


def test_the_manifest_carries_no_setting_no_host_and_no_secret() -> None:
    body = client_for().get("/manifest.webmanifest").content.decode()
    assert "://" not in body  # no absolute URL, so no host name of any environment
    for value in (settings.SECRET_KEY, settings.STAX_API_KEY, settings.APP_BASE_URL):
        assert value not in body
    for icon in json.loads(body)["icons"]:
        assert icon["src"].startswith("/static/finance/icons/")


@pytest.mark.parametrize("path", ["/manifest.webmanifest", "/sw.js"])
def test_both_routes_need_a_verified_sign_in(path: str) -> None:
    response = Client().get(path)
    assert response.status_code == 401
    body = response.content.decode()
    assert "PRECACHE" not in body and "standalone" not in body


@pytest.mark.parametrize("path", ["/manifest.webmanifest", "/sw.js"])
def test_both_routes_refuse_a_post(path: str) -> None:
    assert client_for(Role.ADMIN).post(path).status_code in (403, 405)


# --- Service worker ----------------------------------------------------------------------------


def test_the_worker_is_served_as_javascript_with_the_root_scope() -> None:
    response = client_for().get("/sw.js")
    assert response["Content-Type"] == "application/javascript"
    assert response["Service-Worker-Allowed"] == "/"


def test_the_allow_list_holds_only_the_apps_own_static_files() -> None:
    urls = embedded(worker(), "PRECACHE")
    assert isinstance(urls, list) and urls
    assert all(isinstance(url, str) and url.startswith("/static/") for url in urls)
    assert all(url.startswith(("/static/finance/", "/static/ledger/")) for url in urls)
    assert "/static/finance/app.css" in urls
    assert "/static/ledger/document_drop.js" in urls
    assert not any("/admin/" in url or url.endswith((".DS_Store", "~")) for url in urls)
    for page in ("/", "/healthz", "/grid/", "/reports/", "/sw.js", "/manifest.webmanifest"):
        assert page not in urls


def test_the_worker_stores_a_response_only_in_its_static_branch() -> None:
    source = worker()
    # Install stores the allow-list and nothing else.
    assert source.count(".addAll(") == 1
    assert "cache.addAll(\n          PRECACHE.map(" in source
    # At run time, exactly one put, after the passthrough return of the fetch handler.
    assert source.count(".put(") == 1
    handler = fetch_handler(source)
    passthrough = handler.index("if (!isPrecached(event.request)) {")
    assert handler.index("return;", passthrough) < handler.index(".put(")
    assert ".put(" in handler
    # A response marked private or no-store is never stored, even a static one.
    assert "!/private|no-store/i.test(control)" in handler
    # No other storage, no push, no background sync, no offline page.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))
    for absent in ("indexedDB", "localStorage", '"push"', '"sync"', "periodicsync", "offline"):
        assert absent not in code


def test_the_worker_passes_navigations_and_unlisted_requests_to_the_network() -> None:
    source = worker()
    rule = source[
        source.index("function isPrecached(") : source.index('self.addEventListener("fetch"')
    ]
    assert 'request.method !== "GET" || request.mode === "navigate"' in rule
    assert "return false;" in rule
    assert 'url.search === ""' in rule and "url.origin === self.location.origin" in rule
    assert "PRECACHED.has(url.pathname)" in rule
    handler = fetch_handler(source)
    # Passthrough is "no respondWith": the respondWith call comes after the early return.
    assert handler.index("return;") < handler.index("event.respondWith(")


def test_the_version_changes_with_the_static_files(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [("finance/app.css", b"body{}")]
    monkeypatch.setattr(pwa, "_static_files", lambda: files)
    first = pwa.precache()
    pwa.precache.cache_clear()
    files[0] = ("finance/app.css", b"body{color:red}")
    second = pwa.precache()
    assert first.urls == second.urls == ("/static/finance/app.css",)
    assert first.version != second.version
    assert re.fullmatch(r"[0-9a-f]{16}", second.version)


def test_activate_deletes_every_other_cache() -> None:
    source = worker()
    activate = source[
        source.index('addEventListener("activate"') : source.index("function isPrecached(")
    ]
    assert "caches.delete(name)" in activate and "name !== CACHE_NAME" in activate
    assert 'const CACHE_NAME = "finance-static-" + VERSION;' in source
    assert isinstance(embedded(source, "VERSION"), str)


@override_settings(DEBUG=True)
def test_with_debug_the_allow_list_is_empty() -> None:
    source = worker()
    assert embedded(source, "PRECACHE") == []
    assert embedded(source, "VERSION") == "debug"


def test_the_worker_source_embeds_no_setting() -> None:
    source = worker()
    assert "://" not in source
    for value in (settings.SECRET_KEY, settings.STAX_API_KEY):
        assert value not in source


# --- Base template -------------------------------------------------------------------------------


def test_every_staff_page_links_the_manifest_and_registers_the_worker() -> None:
    page = client_for().get("/").content.decode()
    assert (
        '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">' in page
    )
    assert '<meta name="theme-color" content="#1a1a1a">' in page
    assert '<link rel="apple-touch-icon" href="/static/finance/icons/icon-192.png">' in page
    assert 'navigator.serviceWorker.register("/sw.js", {scope: "/"})' in page
    assert 'window.addEventListener("load"' in page


# --- Cache-Control -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/grid/",
        "/grid/export.csv",
        "/arrears/",
        "/reports/",
        "/reports/aging/",
        "/reports/aging/export.csv",
        "/payments/",
        "/sw.js",
        "/manifest.webmanifest",
    ],
)
def test_every_page_and_export_is_private_and_never_stored(path: str) -> None:
    response = client_for().get(path)
    assert response.status_code == 200, path
    directives = {d.strip() for d in response["Cache-Control"].split(",")}
    assert {"private", "no-store"} <= directives, path
    assert "public" not in directives


@pytest.mark.parametrize("path", ["/healthz", "/account/login/", "/operators/"])
def test_unauthenticated_answers_are_private_too(path: str) -> None:
    response = Client().get(path)
    directives = {d.strip() for d in response["Cache-Control"].split(",")}
    assert {"private", "no-store"} <= directives, path


@override_settings(WHITENOISE_USE_FINDERS=True)
def test_a_static_file_keeps_its_own_caching() -> None:
    response = Client().get("/static/finance/app.css")
    response.close()
    assert response.status_code == 200
    assert "no-store" not in response.get("Cache-Control", "")
    assert "private" not in response.get("Cache-Control", "")


def _respond_with(control: str | None) -> HttpResponse:
    response = HttpResponse("x")
    if control is not None:
        response["Cache-Control"] = control
    return response


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (None, set()),
        ("no-cache, must-revalidate, max-age=0", {"no-cache", "must-revalidate", "max-age=0"}),
        ("private", set()),
        ("no-store", set()),
    ],
)
def test_the_middleware_keeps_a_views_stricter_directives(
    given: str | None, kept: set[str]
) -> None:
    middleware = PrivateNoStoreMiddleware(lambda request: _respond_with(given))
    response = middleware(RequestFactory().get("/somewhere/"))
    directives = {d.strip() for d in response["Cache-Control"].split(",")}
    assert directives == {"private", "no-store"} | kept


def test_the_middleware_removes_public() -> None:
    def view(request: HttpRequest) -> HttpResponse:
        response = HttpResponse("x")
        patch_cache_control(response, public=True, max_age=3600)
        return response

    response = PrivateNoStoreMiddleware(view)(RequestFactory().get("/somewhere/"))
    directives = {d.strip() for d in response["Cache-Control"].split(",")}
    assert "public" not in directives and {"private", "no-store"} <= directives


def test_the_middleware_leaves_static_paths_alone() -> None:
    middleware = PrivateNoStoreMiddleware(lambda request: _respond_with("max-age=60, public"))
    response = middleware(RequestFactory().get("/static/finance/app.css"))
    assert response["Cache-Control"] == "max-age=60, public"
