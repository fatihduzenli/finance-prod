"""Admin wiring for the ledger: operators, stores and leases, read-only
(docs/qa/PLAN-006-operators.md §2, D9; PLAN-007b-stores.md §1).

Staff create and edit operators, stores and leases on the staff screens, which write the
audit log (and, for operators and leases, talk to the payment provider). The admin only
lists them: no add, change or delete permission, so nothing can be hard-deleted here.
The ``admin`` group holds ``view_*`` on the three models (ledger migration 0008), so an
admin-group user who is not a superuser can open them. The reserved tables (documents,
outbound messages) are deliberately not registered.

The fee catalogue and the insurance premiums are edited here, by an admin only
(docs/qa/PLAN-010b D1, D9, D12): add and change, never delete (a fee type no longer
offered is made inactive; a premium is kept for the years it priced). The ``admin``
group holds view, add and change on both (ledger migration 0013); the registry refuses
any POST to them below admin. Every save writes an ``audit_log`` row with the fields
before and after.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import admin
from django.contrib.auth.models import User
from django.db import models
from django.http import HttpRequest

from ledger.models import AuditLog, FeeType, InsurancePremium, Lease, Operator, Store

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


def _fields(row: models.Model, names: tuple[str, ...]) -> dict[str, Any]:
    values = {}
    for name in names:
        value = getattr(row, name)
        values[name] = value if isinstance(value, bool | int | str) or value is None else str(value)
    return values


class AuditedAdmin(_ModelAdminBase):
    """Add and change only; every save is written to ``audit_log``."""

    audited_fields: tuple[str, ...] = ()
    entity_type = ""

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def save_model(
        self, request: HttpRequest, obj: Any, form: forms.ModelForm[Any], change: bool
    ) -> None:
        before = None
        if change:
            stored = type(obj)._default_manager.filter(pk=obj.pk).first()
            before = _fields(stored, self.audited_fields) if stored is not None else None
        super().save_model(request, obj, form, change)
        AuditLog.objects.create(
            actor=request.user if isinstance(request.user, User) else None,
            action=f"{self.entity_type}.{'changed' if change else 'created'}_in_admin",
            entity_type=self.entity_type,
            entity_id=str(obj.pk),
            before=before,
            after=_fields(obj, self.audited_fields),
        )


@admin.register(FeeType)
class FeeTypeAdmin(AuditedAdmin):
    list_display = (
        "name",
        "code",
        "default_cost",
        "default_price",
        "refundable",
        "active",
        "sort_order",
    )
    list_filter = ("active", "refundable")
    ordering = ("sort_order", "name")
    audited_fields = (
        "code",
        "name",
        "default_cost",
        "default_price",
        "refundable",
        "active",
        "sort_order",
    )
    entity_type = "fee_type"


@admin.register(InsurancePremium)
class InsurancePremiumAdmin(AuditedAdmin):
    list_display = ("policy_year", "amount", "cost")
    ordering = ("-policy_year",)
    audited_fields = ("policy_year", "amount", "cost")
    entity_type = "insurance_premium"
