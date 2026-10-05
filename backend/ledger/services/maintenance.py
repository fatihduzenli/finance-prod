"""Database upkeep the application runs itself (docs/qa/PLAN-010c D11).

:func:`analyze` refreshes the PostgreSQL planner's statistics for the tables of the
given models, so the queries that read them right after a bulk import are planned on
the rows that are there now. It is the one place that issues ``ANALYZE``; a bulk
importer calls it after a real run that changed rows (``import_store_costs`` does).
On any other database engine it does nothing.
"""

from __future__ import annotations

from django.db import connection, models

POSTGRESQL = "postgresql"


def analyze(*model_classes: type[models.Model]) -> None:
    """``ANALYZE`` each model's table on PostgreSQL, one statement per table; nothing
    elsewhere. Table names come from the models, quoted, never from input."""
    if connection.vendor != POSTGRESQL:
        return
    with connection.cursor() as cursor:
        for model in model_classes:
            table = connection.ops.quote_name(model._meta.db_table)
            cursor.execute(f"ANALYZE {table}")
