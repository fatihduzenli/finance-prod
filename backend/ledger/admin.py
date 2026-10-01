"""Admin wiring for the ledger: operators, stores and leases, read-only
(docs/qa/PLAN-006-operators.md §2, D9; PLAN-007b-stores.md §1).

Staff create and edit operators, stores and leases on the staff screens, which write the
audit log (and, for operators and leases, talk to the payment provider). The admin only
lists them: no add, change or delete permission, so nothing can be hard-deleted here.
The ``admin`` group holds ``view_*`` on the three models (ledger migration 0008), so an
admin-group user who is not a superuser can open them. The reserved tables (documents,
outbound messages) are deliberately not registered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.http import HttpRequest

from ledger.models import Lease, Operator, Store

if TYPE_CHECKING:
    _ModelAdminBase = admin.ModelAdmin[Any]
else:  # ModelAdmin is only generic in the type stubs.
    _ModelAdminBase = admin.ModelAdmin


class ReadOnlyAdmin(_ModelAdminBase):
    """Lists and shows rows; refuses add, change and delete for everyone."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Operator)
class OperatorAdmin(ReadOnlyAdmin):
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


@admin.register(Store)
class StoreAdmin(ReadOnlyAdmin):
    list_display = (
        "external_label",
        "store_number",
        "city",
        "state",
        "open_date",
        "closed_at",
        "status",
        "needs_open_date",
        "needs_review",
    )
    list_filter = ("status", "needs_open_date", "needs_review")
    search_fields = ("external_label", "city")
    ordering = ("external_label",)


@admin.register(Lease)
class LeaseAdmin(ReadOnlyAdmin):
    list_display = (
        "store",
        "operator",
        "collection_status",
        "payment_channel",
        "debit_day",
        "second_debit_day",
        "started_on",
        "needs_review",
    )
    list_filter = ("collection_status", "payment_channel", "needs_review")
    search_fields = ("store__external_label", "operator__display_name")
    ordering = ("store__external_label",)
    list_select_related = ("store", "operator")
