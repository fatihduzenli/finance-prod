"""The indexes of PLAN-011 D6, on the engine under test, and the migration that adds them
reverses (docs/qa/PLAN-011-dashboard-reports-mobile.md D6)."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db

INDEXES = {
    ("rent_charges", "rent_charges_status_due_idx"): ["status", "due_on"],
    ("payments", "payments_settled_status_idx"): ["settled_on", "status"],
    ("invoices", "invoices_provider_status_idx"): ["provider_status"],
    ("one_off_charges", "one_off_charges_op_status_idx"): ["operator_id", "status"],
    ("insurance_charges", "insurance_lease_due_idx"): ["lease_id", "due_on"],
}


def _indexes() -> dict[tuple[str, str], list[str]]:
    found = {}
    with connection.cursor() as cursor:
        for table, name in INDEXES:
            constraints = connection.introspection.get_constraints(cursor, table)
            if name in constraints:
                found[(table, name)] = list(constraints[name]["columns"])
    return found


def test_the_five_indexes_exist_with_their_columns() -> None:
    assert _indexes() == INDEXES


@pytest.mark.django_db(transaction=True)
def test_the_migration_reverses() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([("ledger", "0021_audit_log_indexes")])
    assert _indexes() == {}
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert _indexes() == INDEXES
