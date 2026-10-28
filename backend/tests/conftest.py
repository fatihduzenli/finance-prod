"""Shared test setup.

pytest-django imports ``finance.settings`` while it starts up, and that import
validates the environment (``finance.env_guard``). The defaults below are therefore
applied at conftest import time, before Django is configured.

Every value here is an obvious placeholder. ``STAX_API_KEY`` and ``SENTRY_DSN`` are
forced rather than defaulted: a test run must never pick up a real key from the shell
and must never be able to talk to a real error tracker.
"""

from __future__ import annotations

import os
from pathlib import Path

import django
import pytest

#: Repository root (this file is backend/tests/conftest.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Obviously fake, distinctive, and safe to grep for in a served asset.
FAKE_STAX_API_KEY = "FAKE-TEST-KEY-do-not-use-XYZ123"

_ENV_DEFAULTS = {
    "APP_ENV": "local",
    "STAX_ENV": "sandbox",
    "STAX_API_BASE_URL": "https://apiprod.fattlabs.com",
    "DATABASE_URL": "sqlite:///:memory:",
    "APP_BASE_URL": "http://localhost:8000",
    "DJANGO_SECRET_KEY": "test-only-not-secret",
}

for _name, _value in _ENV_DEFAULTS.items():
    os.environ.setdefault(_name, _value)

os.environ["STAX_API_KEY"] = FAKE_STAX_API_KEY
os.environ["DJANGO_DEBUG"] = "0"
os.environ.pop("SENTRY_DSN", None)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "finance.settings")

# pytest-django configures Django while it loads initial conftests, which happens
# before this file is imported, so it finds no settings module and skips that step.
# Setting the variables above and then calling django.setup() here is what makes the
# placeholder environment, rather than the developer's shell, the one under test.
django.setup()


@pytest.fixture
def fake_stax_api_key() -> str:
    """The fake key the test environment runs with."""
    return FAKE_STAX_API_KEY


@pytest.fixture
def repo_root() -> Path:
    """Repository root, for tests that run a repository script."""
    return REPO_ROOT


# --- The migration-test job's shards (PLAN-013a A1) -----------------------------------------
#: Which CI shard runs each test of the migration selection (the round-trip file, the report
#: indexes, the two rename cases and the concurrency case): balanced from the durations measured
#: on 2026-10-27 (27:37 in all; three shards of 490 to 545 s), keyed by the test's name (unique
#: across those files). `tests/test_ci_shards.py` proves every test of those files is here
#: exactly once. `-m shardN` selects one shard.
ROUNDTRIP_SHARDS: dict[str, int] = {
    # shard 1: the fresh round trips, the rename and concurrency cases, the index round trip,
    # the kind, item-on-sqlite and promise cases, the fast checks.
    "test_models_and_migrations_agree": 1,
    "test_the_check_method_migration_alters_choices_only": 1,
    "test_round_trip_on_a_fresh_sqlite_database": 1,
    "test_round_trip_on_a_fresh_postgresql_database": 1,
    "test_the_rename_migrates_over_existing_rows_and_back": 1,
    "test_the_kind_migration_over_existing_rows_on_sqlite": 1,
    "test_the_kind_migration_over_existing_rows_on_postgresql": 1,
    "test_the_item_migration_over_existing_rows_on_sqlite": 1,
    "test_the_one_open_promise_migration_over_existing_rows_on_sqlite": 1,
    "test_the_one_open_promise_migration_over_existing_rows_on_postgresql": 1,
    "test_sqlmigrate_of_the_check_method_migration_touches_only_its_checks": 1,
    "test_payments_initial_migration_depends_on_ledger": 1,
    "test_the_five_indexes_exist_with_their_columns": 1,
    "test_the_migration_reverses": 1,
    "test_the_rename_migration_rewrites_rows_and_reverses": 1,
    "test_the_rename_migration_refuses_to_reverse_over_a_one_off_charge": 1,
    "test_two_concurrent_runs_both_succeed_and_migrate_once": 1,
    # shard 2: lease end, transfer, installments, debt override, statement opened, rent lines,
    # credits, item-on-PostgreSQL.
    "test_the_item_migration_over_existing_rows_on_postgresql": 2,
    "test_the_lease_end_migrations_over_existing_rows_on_sqlite": 2,
    "test_the_lease_end_migrations_over_existing_rows_on_postgresql": 2,
    "test_the_transfer_fields_migration_over_an_existing_row_on_postgresql": 2,
    "test_the_installments_migration_over_an_existing_row_on_postgresql": 2,
    "test_the_debt_override_migration_over_an_existing_row_on_postgresql": 2,
    "test_the_statement_opened_migration_over_an_existing_row_on_postgresql": 2,
    "test_the_rent_lines_migration_over_existing_rows_on_postgresql": 2,
    "test_the_credits_migration_over_existing_rows_on_postgresql": 2,
    # shard 3: setup flag, company settings, store address, check method, autopay plans.
    "test_the_setup_flag_and_existing_charge_migrations_over_existing_rows_on_sqlite": 3,
    "test_the_setup_flag_and_existing_charge_migrations_over_existing_rows_on_postgresql": 3,
    "test_the_company_settings_migration_over_existing_rows_on_sqlite": 3,
    "test_the_company_settings_migration_over_existing_rows_on_postgresql": 3,
    "test_the_store_address_migration_over_existing_rows_on_sqlite": 3,
    "test_the_store_address_migration_over_existing_rows_on_postgresql": 3,
    "test_the_check_method_migration_over_existing_rows_on_sqlite": 3,
    "test_the_check_method_migration_over_existing_rows_on_postgresql": 3,
    "test_the_autopay_plans_migration_over_existing_rows_on_sqlite": 3,
    "test_the_autopay_plans_migration_over_existing_rows_on_postgresql": 3,
}
SHARD_FILES = (
    "test_migrations_roundtrip.py",
    "test_report_indexes.py",
    "test_one_off_charges.py",
    "test_staging_defects_adversarial.py",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Each test of the migration selection gets its shard's marker (PLAN-013a A1), so CI's
    `-m shard1` … `-m shard3` split the job three ways with no test twice or missing."""
    for item in items:
        if item.path.name not in SHARD_FILES:
            continue
        shard = ROUNDTRIP_SHARDS.get(item.name.split("[", 1)[0])
        if shard is not None:
            item.add_marker(getattr(pytest.mark, f"shard{shard}"))
