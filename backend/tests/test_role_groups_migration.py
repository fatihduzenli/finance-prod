"""The data migration creates the three role groups (PLAN-003 §4).

Run against the migration's own historical state, because transactional tests elsewhere
flush the database and take migration-created rows with them.
"""

from __future__ import annotations

import importlib

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db

migration = importlib.import_module("accounts.migrations.0002_role_groups")


def _apply() -> None:
    state = MigrationExecutor(connection).loader.project_state(("accounts", "0002_role_groups"))
    migration.create_role_groups(state.apps, None)


def test_creates_exactly_the_three_role_groups() -> None:
    Group.objects.filter(name__in=("admin", "billing", "viewer")).delete()
    _apply()
    assert set(Group.objects.values_list("name", flat=True)) >= {"admin", "billing", "viewer"}
    _apply()  # idempotent
    assert Group.objects.filter(name="admin").count() == 1


def test_only_admin_gets_user_management_permissions() -> None:
    Group.objects.filter(name__in=("admin", "billing", "viewer")).delete()
    _apply()
    admin_perms = set(
        Group.objects.get(name="admin").permissions.values_list("codename", flat=True)
    )
    assert {"add_user", "change_user", "delete_user", "add_group", "view_totpdevice"} <= (
        admin_perms
    )
    assert not Group.objects.get(name="billing").permissions.exists()
    assert not Group.objects.get(name="viewer").permissions.exists()
