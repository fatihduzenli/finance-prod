"""URL map: the staff screens, Django admin, /healthz for the platform probe, and the
public webhook receiver under /api/webhooks/ (docs/qa/PLAN-008-webhooks.md D2).

Every route must be classified in accounts.registry; backend/tests/test_route_registry.py
fails when one is not.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.http import HttpRequest, JsonResponse
from django.urls import include, path
from two_factor.urls import urlpatterns as two_factor_urlpatterns

from finance import help as help_pages
from finance import pwa
from finance import theme as theme_views
from ledger.views import calendar as calendar_views


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness probe. Unauthenticated by design, so it carries no secret."""
    return JsonResponse(
        {
            "status": "ok",
            "app_env": settings.APP_ENV,
            "stax_env": settings.STAX_ENV,
        }
    )


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    # Install to the home screen (PLAN-011 D9): the manifest and the service worker, at
    # the root so the worker's scope is the whole site.
    path(
        "",
        include(
            (
                [
                    path("manifest.webmanifest", pwa.manifest, name="manifest"),
                    path("sw.js", pwa.service_worker, name="service_worker"),
                    # The user's own settings: the calendar feed (PLAN-011b D8).
                    path("settings/", calendar_views.settings_page, name="settings"),
                    path(
                        "settings/calendar-feed/new/",
                        calendar_views.feed_create,
                        name="feed_create",
                    ),
                    path(
                        "settings/calendar-feed/revoke/",
                        calendar_views.feed_revoke,
                        name="feed_revoke",
                    ),
                    # The user's theme, Light, Dark or System (PLAN-012g A3).
                    path("settings/theme/", theme_views.set_theme_view, name="theme"),
                    # The help pages (PLAN-012e-2 D10): static, signed in, every role.
                    path("help/", help_pages.index, name="help"),
                    path("help/<slug:slug>/", help_pages.topic, name="help_topic"),
                ],
                "finance",
            )
        ),
    ),
    # Public, CSRF-exempt, token in the path; answers 404 to anything but a valid POST.
    path("api/webhooks/", include("payments.webhooks.urls")),
    # Login, enrolment, QR code, backup tokens, profile: all under /account/.
    path("", include(two_factor_urlpatterns)),
    # POST only; Django's LogoutView answers GET with 405.
    path("account/logout/", LogoutView.as_view(), name="logout"),
    path("account/", include("accounts.urls")),
    # Staff screens: /operators/ (docs/qa/PLAN-006-operators.md §4.1).
    path("", include("ledger.urls")),
]
