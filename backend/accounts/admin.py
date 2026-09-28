"""Admin wiring: OTP-required admin site and a read-only UserSession list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest
from two_factor.admin import AdminSiteOTPRequired

from accounts.models import UserSession

# Every admin view requires a user verified by the second factor, and /admin/login/
# hands over to the two-factor login instead of offering a password-only form.
admin.site.__class__ = AdminSiteOTPRequired


if TYPE_CHECKING:
    _UserSessionAdminBase = admin.ModelAdmin[UserSession]
else:  # ModelAdmin is only generic in the type stubs.
    _UserSessionAdminBase = admin.ModelAdmin


@admin.register(UserSession)
class UserSessionAdmin(_UserSessionAdminBase):
    list_display = ("user", "created_at", "last_seen_at", "user_agent")
    list_filter = ("user",)
    ordering = ("-last_seen_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
