"""The app's identity on a home screen and in a tab (owner, 2026-11-05): "Monvera".

The manifest names the app, whatever the company's name, carries the dark theme's ``--bg`` as
its colours and the brand icons with a maskable one, and answers signed out (a phone reads it
from the sign-in page). The icons are opaque squares on the navy-to-teal gradient
(``backend/scripts/make_brand_icons.py``). The staff shell, the sign-in frame and the portal
share one head block (``finance/_app_identity.html``) and end every tab title with
"· Monvera". The manifest's own route, colours and icon list are also pinned in
``tests/test_pwa.py``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import Client
from PIL import Image

from accounts.roles import Role
from finance import pwa
from ledger.models import CompanySettings
from tests.auth_helpers import make_user, verified_client
from tests.text_helpers import assert_absent

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parents[1]
TOKENS = BACKEND / "finance" / "static" / "finance" / "tokens.css"
BRAND = "/static/finance/brand/"
#: The gradient's two ends (top, bottom).
TOP = (0x0B, 0x25, 0x45)
BOTTOM = (0x0E, 0x4A, 0x5C)
#: (static path, side) of every PNG the head or the manifest names.
PNGS = [
    ("finance/brand/monvera_touch_180.png", 180),
    ("finance/brand/monvera_icon_192.png", 192),
    ("finance/brand/monvera_icon_512.png", 512),
    ("finance/brand/monvera_icon_512_maskable.png", 512),
    ("finance/brand/favicon-32.png", 32),
    ("finance/brand/favicon-16.png", 16),
]
STAFF_MANIFEST = '<link rel="manifest" href="/manifest.webmanifest">'
PORTAL_MANIFEST = '<link rel="manifest" href="/portal/manifest.webmanifest">'
#: The head block, as each shell renders it (the manifest link apart: the staff shell's or the
#: portal's).
HEAD_LINKS = (
    f'<link rel="icon" href="{BRAND}monvera_icon.svg" type="image/svg+xml">',
    f'<link rel="icon" href="{BRAND}favicon-32.png" type="image/png" sizes="32x32">',
    f'<link rel="icon" href="{BRAND}favicon-16.png" type="image/png" sizes="16x16">',
    f'<link rel="apple-touch-icon" sizes="180x180" href="{BRAND}monvera_touch_180.png">',
    '<meta name="application-name" content="Monvera">',
    '<meta name="apple-mobile-web-app-title" content="Monvera">',
)


@pytest.fixture(autouse=True)
def fresh_precache() -> Iterator[None]:
    pwa.precache.cache_clear()
    yield
    pwa.precache.cache_clear()


def staff_client() -> Client:
    user, device = make_user(f"identity-{uuid.uuid4().hex[:8]}", Role.VIEWER)
    assert device is not None
    return verified_client(user, device)


def head_of(html: str) -> str:
    return html.split("<head>", 1)[1].split("</head>", 1)[0]


def titles_of(html: str) -> list[str]:
    return re.findall(r"<title>(.*?)</title>", html, flags=re.S)


def dark_bg() -> str:
    """``--bg`` of the stored dark choice's block in tokens.css."""
    css = TOKENS.read_text(encoding="utf-8")
    block = css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]
    match = re.search(r"--bg:\s*(#[0-9A-Fa-f]{6})\s*;", block)
    assert match is not None
    return match.group(1).lower()


def rgb(image: Image.Image, x: int, y: int) -> tuple[int, ...]:
    pixel = image.getpixel((x, y))
    assert isinstance(pixel, tuple), pixel
    return tuple(int(value) for value in pixel)


def close(pixel: tuple[int, ...], colour: tuple[int, int, int], within: int = 3) -> bool:
    return all(abs(a - b) <= within for a, b in zip(pixel, colour, strict=True))


# --- The manifest ------------------------------------------------------------------------------


def test_the_manifest_answers_signed_out_with_its_fields_exactly() -> None:
    response = Client().get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/manifest+json"
    assert json.loads(response.content) == {
        "name": "Monvera",
        "short_name": "Monvera",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#171721",
        "theme_color": "#171721",
        "icons": [
            {"src": f"{BRAND}monvera_icon_192.png", "sizes": "192x192", "type": "image/png",
             "purpose": "any"},
            {"src": f"{BRAND}monvera_icon_512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any"},
            {"src": f"{BRAND}monvera_icon_512_maskable.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
            {"src": f"{BRAND}monvera_icon.svg", "sizes": "any", "type": "image/svg+xml",
             "purpose": "any"},
        ],
    }  # fmt: skip


def test_the_manifest_is_the_same_signed_in_and_signed_out() -> None:
    signed_out = Client().get("/manifest.webmanifest").content
    assert staff_client().get("/manifest.webmanifest").content == signed_out


def test_the_portals_manifest_opens_on_the_portal_signed_out() -> None:
    response = Client().get("/portal/manifest.webmanifest")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/manifest+json"
    data = json.loads(response.content)
    assert (data["start_url"], data["scope"]) == ("/portal/", "/portal/")
    # Everything else is the staff manifest's: the same app.
    staff = json.loads(Client().get("/manifest.webmanifest").content)
    assert (staff["start_url"], staff["scope"]) == ("/", "/")
    for key in ("start_url", "scope"):
        del data[key], staff[key]
    assert data == staff
    directives = {d.strip() for d in response["Cache-Control"].split(",")}
    assert {"private", "no-store"} <= directives


def test_the_portals_manifest_is_the_same_for_a_portal_or_a_staff_session() -> None:
    signed_out = Client().get("/portal/manifest.webmanifest").content
    assert staff_client().get("/portal/manifest.webmanifest").content == signed_out


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_the_portals_manifest_refuses_an_unsafe_method(method: str) -> None:
    response = getattr(Client(), method)("/portal/manifest.webmanifest")
    assert response.status_code in (403, 405)
    assert "standalone" not in response.content.decode()


def test_the_manifest_colours_are_the_dark_bg_of_tokens_css() -> None:
    data = pwa.manifest_data()
    assert data["theme_color"] == data["background_color"] == dark_bg()
    assert pwa.THEME_COLOR_DARK == dark_bg()


def test_a_company_name_never_reaches_the_public_manifest() -> None:
    row = CompanySettings.current()
    row.name, row.short_name = "Acme <script>x</script>", "Acme"
    row.save()
    body = Client().get("/manifest.webmanifest").content.decode()
    assert_absent(body, "Acme")
    assert "<script" not in body
    assert "://" not in body
    for value in (settings.SECRET_KEY, settings.STAX_API_KEY, settings.APP_BASE_URL):
        assert value not in body


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_signed_out_the_manifest_refuses_an_unsafe_method(method: str) -> None:
    response = getattr(Client(), method)("/manifest.webmanifest")
    assert response.status_code in (403, 405)
    assert "standalone" not in response.content.decode()


def test_the_service_worker_still_needs_a_sign_in() -> None:
    response = Client().get("/sw.js")
    assert response.status_code == 401
    assert "PRECACHE" not in response.content.decode()


def test_the_service_worker_keeps_the_new_icons() -> None:
    source = staff_client().get("/sw.js").content.decode()
    match = re.search(r"^const PRECACHE = (.*);$", source, re.MULTILINE)
    assert match is not None
    urls = json.loads(match.group(1))
    for path, _ in PNGS:
        assert f"/static/{path}" in urls, path
    assert f"{BRAND}monvera_icon.svg" in urls


# --- The icons ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "side"), PNGS)
def test_each_icon_is_an_opaque_square_on_the_gradient(path: str, side: int) -> None:
    found = finders.find(path)
    assert found is not None, path
    with Image.open(str(found)) as image:
        assert image.format == "PNG"
        assert image.size == (side, side)
        # RGB: no alpha channel at all, so iOS paints nothing black.
        assert image.mode == "RGB", image.mode
        last = side - 1
        corners = [rgb(image, x, y) for x, y in ((0, 0), (last, 0), (0, last), (last, last))]
    top_left, top_right, bottom_left, bottom_right = corners
    assert close(top_left, TOP) and close(top_right, TOP), (path, corners)
    assert close(bottom_left, BOTTOM) and close(bottom_right, BOTTOM), (path, corners)


def test_the_mark_sits_inside_the_maskable_safe_zone() -> None:
    """Android may crop a maskable icon to a circle of 40 % of the side's radius: every pixel
    that is not the gradient (the mark) lies inside it."""
    found = finders.find("finance/brand/monvera_icon_512_maskable.png")
    assert found is not None
    with Image.open(str(found)) as image:
        side = image.width
        centre, radius = (side - 1) / 2, 0.40 * side
        for y in range(0, side, 4):
            for x in range(0, side, 4):
                if (x - centre) ** 2 + (y - centre) ** 2 <= radius**2:
                    continue
                t = y / (side - 1)
                r, g, b = (
                    round(top + (bottom - top) * t) for top, bottom in zip(TOP, BOTTOM, strict=True)
                )
                ground = (r, g, b)
                pixel = rgb(image, x, y)
                assert close(pixel, ground, within=4), (x, y, pixel)


def test_the_svg_favicon_is_the_gradient_square_and_the_mark() -> None:
    found = finders.find("finance/brand/monvera_icon.svg")
    assert found is not None
    svg = Path(str(found)).read_text(encoding="utf-8")
    assert '<rect width="160" height="160" fill="url(#bg)"/>' in svg
    assert 'stop-color="#0B2545"' in svg and 'stop-color="#0E4A5C"' in svg
    assert 'd="M36 128 L36 44 L80 90 L124 44 L124 128"' in svg
    assert "<text" not in svg  # the mark only, not the wordmark
    assert 'fill="white"' not in svg


# --- The three heads ---------------------------------------------------------------------------


def _assert_identity_head(html: str, manifest: str = STAFF_MANIFEST) -> None:
    head = head_of(html)
    missing = [line for line in (*HEAD_LINKS, manifest) if line not in head]
    assert missing == []
    assert head.count('<link rel="manifest"') == 1
    assert head.count('<link rel="apple-touch-icon"') == 1
    assert "crossorigin" not in head
    assert '<meta name="theme-color"' in head


def test_the_staff_shell_carries_the_identity_head() -> None:
    _assert_identity_head(staff_client().get("/").content.decode())


def test_the_sign_in_page_carries_the_identity_head() -> None:
    _assert_identity_head(Client().get("/account/login/").content.decode())


def test_a_portal_page_carries_the_identity_head() -> None:
    response = Client().get("/portal/login/")
    assert response.status_code == 200
    html = response.content.decode()
    # Its own manifest, which opens on /portal/, not the staff one.
    _assert_identity_head(html, PORTAL_MANIFEST)
    # The theme-color meta is the shared one: signed out the portal is Dark (PLAN-015 §4.4).
    head = head_of(html)
    assert head.count('<meta name="theme-color"') == 1
    assert '<meta name="theme-color" content="#171721">' in head


@pytest.mark.parametrize(
    ("label", "path", "signed_in"),
    [
        ("staff dashboard", "/", True),
        ("staff stores", "/stores/", True),
        ("staff help", "/help/", True),
        ("sign-in", "/account/login/", False),
        ("sign in required", "/stores/", False),
        ("portal sign-in", "/portal/login/", False),
        ("portal reset", "/portal/reset/", False),
    ],
)
def test_every_title_ends_with_the_apps_name(label: str, path: str, signed_in: bool) -> None:
    client = staff_client() if signed_in else Client()
    row = CompanySettings.current()
    row.name, row.short_name = "Acme Rentals", "Acme"
    row.save()
    titles = titles_of(head_of(client.get(path).content.decode()))
    assert len(titles) == 1, (label, titles)
    assert titles[0].endswith(" · Monvera"), (label, titles[0])
    assert "Acme" not in titles[0] and "finance" not in titles[0].lower(), (label, titles[0])


def test_the_portal_keeps_the_companys_name_in_its_top_bar() -> None:
    row = CompanySettings.current()
    row.name = "Acme Rentals"
    row.save()
    html = Client().get("/portal/login/").content.decode()
    assert '<a class="portal-company" id="portal-company"' in html
    bar = html.split('id="portal-bar"', 1)[1].split("</header>", 1)[0]
    assert "Acme Rentals" in bar


def test_no_template_title_or_meta_says_finance() -> None:
    offenders = []
    for path in sorted(BACKEND.rglob("templates/**/*.html")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for element in re.findall(r"<title>.*?</title>|<meta [^>]*>", line):
                if "finance" in element.lower():
                    offenders.append(f"{path.relative_to(BACKEND)}:{number}")
    assert offenders == []
