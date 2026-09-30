"""Admin wiring for the ledger: operators, read-only (docs/qa/PLAN-006-operators.md §2, D9).

Staff create, edit and archive operators on the operator screens, which write the
audit log and talk to the payment provider. The admin only lists them: no add, change
or delete permission, so nothing can be hard-deleted here. The reserved tables
(documents, outbound messages) are deliberately not registered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest

from ledger.models import Operator

if TYPE_CHECKING:
    _OperatorAdminBase = admin.ModelAdmin[Operator]
else:  # ModelAdmin is only generic in the type stubs.
    _OperatorAdminBase = admin.ModelAdmin


@admin.register(Operator)
class OperatorAdmin(_OperatorAdminBase):
    list_display = (
        "display_name",
        "email",
        "sync_status",
        "enrolment_status",
        "needs_review",
        "archived_at",
    )
    list_filter = ("sync_status", "enrolment_status", "needs_review")
    search_fields = ("display_name", "legal_name", "company", "email")
    ordering = ("display_name",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
