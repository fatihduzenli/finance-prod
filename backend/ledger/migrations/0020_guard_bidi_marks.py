"""The card-number guard reads the bidirectional marks and controls as invisible
(docs/qa/PLAN-010d Batch A finding, ``ledger.validators.INVISIBLE_CHARACTERS``).

``finance_contains_card_number`` is replaced by the function of migration ``0002`` with
one change: the characters it removes before matching are the application's
``INVISIBLE_CHARACTERS`` of this step (U+200E, U+200F, U+202A to U+202E, U+2066 to
U+2069 added), so the database and the application keep reading the same text. The
triggers call the function by name and are not touched. The reverse puts back the
function of ``0002``. Nothing is done on any other database vendor.
"""

from __future__ import annotations

import importlib

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

_GUARDS = importlib.import_module("ledger.migrations.0002_postgresql_guards")

#: The characters removed before matching, as of this migration (a copy, so a later
#: change of ``ledger.validators`` needs its own migration).
INVISIBLE = (
    "\u200b\u200c\u200d\u2060\ufeff\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_INVISIBLE_SQL = " || ".join(f"chr({ord(char)})" for char in INVISIBLE)


def _function_sql() -> str:
    before: str = _GUARDS.CONTAINS_CARD_NUMBER_SQL
    old: str = _GUARDS._INVISIBLE_SQL
    if before.count(old) != 1:
        raise RuntimeError("The guard function of migration 0002 is not the expected text.")
    return before.replace(old, _INVISIBLE_SQL)


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_function_sql(), params=None)


def backwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(_GUARDS.CONTAINS_CARD_NUMBER_SQL, params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0019_one_off_charges_constraints"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards, elidable=False),
    ]
