"""Seed the fee catalogue and the premium costs (docs/qa/PLAN-010b D1, D2, D9).

The seven fee types of D1. Only first-year insurance has default amounts (cost 300.00,
price 695.00, as the owner stated); the others start empty because no amount is
standard. First-year insurance is seeded inactive (D2): the lease's first insurance
charge already bills year one (ADR-0005), so an active setup charge of this type would
bill the operator twice. A type that already exists is left as it is.

``insurance_premiums.cost`` is set to 300.00 for the policy years 2025 and 2026 where
those rows exist and have no cost yet; other years are left for the admin.

The ``admin`` group gets view, add and change on fee types and insurance premiums,
which the Django admin edits (D12); ``billing`` and ``viewer`` get nothing.

The reverse removes the seeded types that no setup charge uses, clears the two costs
this migration set and takes the permissions back.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.apps.registry import Apps
from django.contrib.auth.management import create_permissions
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

#: (code, name, default cost, default price, refundable, active), in display order.
FEE_TYPES: tuple[tuple[str, str, Decimal | None, Decimal | None, bool, bool], ...] = (
    ("access_fee", "Access fee", None, None, False, True),
    ("security_deposit", "Security deposit", None, None, True, True),
    ("sign_fee", "Sign fee", None, None, False, True),
    (
        "first_year_insurance",
        "First-year insurance",
        Decimal("300.00"),
        Decimal("695.00"),
        False,
        False,
    ),
    ("architecture_fee", "Architecture fee", None, None, False, True),
    ("business_purchase_fee", "Business purchase fee", None, None, False, True),
    ("phone_service", "Phone service", None, None, False, True),
)
SORT_STEP = 10
PREMIUM_COST = Decimal("300.00")
PREMIUM_COST_YEARS = (2025, 2026)
ADMIN_GROUP = "admin"
#: (model_name, codename) of each permission granted to the admin group.
ADMIN_PERMISSIONS = tuple(
    (model, f"{action}_{model}")
    for model in ("feetype", "insurancepremium")
    for action in ("view", "add", "change")
)


def _permissions(apps: Apps) -> list[Any]:
    permission_model = apps.get_model("auth", "Permission")
    return [
        permission
        for model, codename in ADMIN_PERMISSIONS
        for permission in permission_model.objects.filter(
            content_type__app_label="ledger", content_type__model=model, codename=codename
        )
    ]


def grant(apps: Apps) -> None:
    # Permissions are normally created after migrate finishes; create them now so they
    # can be attached (as 0008_admin_group_view_permissions does).
    app_config = apps.get_app_config("ledger")
    app_config.models_module = True  # type: ignore[assignment]
    create_permissions(app_config, verbosity=0, apps=apps)
    app_config.models_module = None
    permissions = _permissions(apps)
    if len(permissions) != len(ADMIN_PERMISSIONS):
        raise RuntimeError("A ledger permission is missing.")
    group, _ = apps.get_model("auth", "Group").objects.get_or_create(name=ADMIN_GROUP)
    group.permissions.add(*permissions)


def seed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    fee_type = apps.get_model("ledger", "FeeType")
    for position, (code, name, cost, price, refundable, active) in enumerate(FEE_TYPES, 1):
        fee_type.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "default_cost": cost,
                "default_price": price,
                "refundable": refundable,
                "active": active,
                "sort_order": position * SORT_STEP,
            },
        )
    apps.get_model("ledger", "InsurancePremium").objects.filter(
        policy_year__in=PREMIUM_COST_YEARS, cost__isnull=True
    ).update(cost=PREMIUM_COST)
    grant(apps)


def unseed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("ledger", "FeeType").objects.filter(
        # The historical model's related name (``one_off_charges`` since 0017).
        code__in=[row[0] for row in FEE_TYPES],
        setup_charges__isnull=True,  # type: ignore[misc]
    ).delete()
    apps.get_model("ledger", "InsurancePremium").objects.filter(
        policy_year__in=PREMIUM_COST_YEARS, cost=PREMIUM_COST
    ).update(cost=None)
    group = apps.get_model("auth", "Group").objects.filter(name=ADMIN_GROUP).first()
    if group is not None:
        group.permissions.remove(*_permissions(apps))


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0012_setup_charges"),
        ("accounts", "0002_role_groups"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(seed, unseed, elidable=False)]
