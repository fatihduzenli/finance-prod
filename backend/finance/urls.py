"""URL map. Django admin is the staff UI; /healthz is for the platform probe."""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.http import HttpRequest, JsonResponse
from django.urls import path


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
    path("healthz", healthz),
]
