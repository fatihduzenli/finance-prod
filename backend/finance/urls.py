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
