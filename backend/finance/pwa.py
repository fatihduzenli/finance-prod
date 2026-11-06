"""Install to the home screen (docs/qa/PLAN-011-dashboard-reports-mobile.md D9).

``/manifest.webmanifest`` describes the app; ``/sw.js`` is a service worker that keeps
**only** the app's own static files (``finance/`` and ``ledger/`` under ``STATIC_URL``)
for offline-fast loading. Every other request, every page, every export and every API
call, goes to the network as the worker never answers it: nothing with financial data is
ever stored in the browser's cache. There is no offline mode, no push and no background
sync.

The allow-list is read once, at startup, from the static files the app ships, and is
embedded in the worker with a version that is a digest of those files: a deploy that
changes one of them changes the worker's bytes, so the browser installs the new worker,
which fetches fresh copies and deletes the old caches. With ``DEBUG`` the list is empty
and the worker caches nothing.

Static files are served without hashed names (``finance.settings.STORAGES``), so the list
comes from the static file finders rather than a ``staticfiles.json`` manifest.

The manifest is public (``accounts.registry.PUBLIC_ROUTE_NAMES``): a browser reads it from
the sign-in page too, without a session, so a phone that adds the site to its home screen
there gets the app's name and icons rather than a fallback built from the page title and the
host. It carries no setting, no host name and nothing of the company row, so the page links
it without credentials. The service worker needs a signed-in, verified user like every staff
page (``accounts.registry`` lists it as read-only).

The manifest names the app, "Monvera" (owner, 2026-11-05: the home-screen identity is the
app's, so PLAN-012c D2's company name gives way here; the company's name stays inside the
pages). Its icons are the brand's, the M mark on the navy-to-teal gradient square,
opaque (``scripts/make_brand_icons.py``): 192 and 512 px PNGs, a 512 px maskable one with
more padding, and the SVG. Its colours are the dark theme's ``--bg`` (tokens.css), so the
splash and the standalone window's bar match the icon's dark ground.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache

from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.views.decorators.http import require_safe

#: The static path prefixes the worker may keep: the app's own files, not the admin's.
PRECACHE_PREFIXES: tuple[str, ...] = ("finance/", "ledger/")
#: collectstatic's default ignore patterns (dot files such as .DS_Store, editor backups).
IGNORE_PATTERNS = ["CVS", ".*", "*~"]
MANIFEST_CONTENT_TYPE = "application/manifest+json"
WORKER_CONTENT_TYPE = "application/javascript"
WORKER_TEMPLATE = "finance/sw.js"
#: ``--navy`` in finance/static/finance/tokens.css: the phone bar's gradient starts on it.
THEME_COLOR = "#0b2545"
#: ``--bg`` of the dark theme (PLAN-012g A2): the page's theme-color meta in dark, and the
#: manifest's theme and background colours.
THEME_COLOR_DARK = "#171721"
#: The app's name on a home screen (owner, 2026-11-05), whatever the company's name.
APP_NAME = "Monvera"
#: (static path, sizes, type, purpose) of each icon in the manifest.
ICONS: tuple[tuple[str, str, str, str], ...] = (
    ("finance/brand/monvera_icon_192.png", "192x192", "image/png", "any"),
    ("finance/brand/monvera_icon_512.png", "512x512", "image/png", "any"),
    ("finance/brand/monvera_icon_512_maskable.png", "512x512", "image/png", "maskable"),
    ("finance/brand/monvera_icon.svg", "any", "image/svg+xml", "any"),
)


@dataclass(frozen=True, slots=True)
class Precache:
    urls: tuple[str, ...]
    version: str


def _static_files() -> list[tuple[str, bytes]]:
    """``(path, content)`` of every static file under :data:`PRECACHE_PREFIXES`."""
    found: dict[str, bytes] = {}
    for finder in finders.get_finders():
        for path, storage in finder.list(IGNORE_PATTERNS):
            normalised = path.replace("\\", "/")
            if normalised in found or not normalised.startswith(PRECACHE_PREFIXES):
                continue
            with storage.open(path) as handle:
                found[normalised] = handle.read()
    return sorted(found.items())


@cache
def precache() -> Precache:
    """The worker's allow-list and version, computed once per process."""
    if settings.DEBUG:
        return Precache(urls=(), version="debug")
    files = _static_files()
    digest = hashlib.sha256()
    for path, content in files:
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return Precache(urls=tuple(static(path) for path, _ in files), version=digest.hexdigest()[:16])


def worker_source() -> str:
    """The service worker's JavaScript, its allow-list and version embedded as JSON."""
    current = precache()
    return render_to_string(
        WORKER_TEMPLATE,
        {
            "precache_json": json.dumps(list(current.urls)),
            "version_json": json.dumps(current.version),
        },
    )


def manifest_data() -> dict[str, object]:
    """The web app manifest: the same for every request and every visitor."""
    return {
        "name": APP_NAME,
        "short_name": APP_NAME,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": THEME_COLOR_DARK,
        "theme_color": THEME_COLOR_DARK,
        "icons": [
            {"src": static(path), "sizes": sizes, "type": kind, "purpose": purpose}
            for path, sizes, kind, purpose in ICONS
        ],
    }


@require_safe
def manifest(request: HttpRequest) -> HttpResponse:
    """``finance:manifest``, public."""
    return HttpResponse(json.dumps(manifest_data()), content_type=MANIFEST_CONTENT_TYPE)


@require_safe
def service_worker(request: HttpRequest) -> HttpResponse:
    """``finance:service_worker``: served at the site root so its scope is the site."""
    response = HttpResponse(worker_source(), content_type=WORKER_CONTENT_TYPE)
    response["Service-Worker-Allowed"] = "/"
    return response
