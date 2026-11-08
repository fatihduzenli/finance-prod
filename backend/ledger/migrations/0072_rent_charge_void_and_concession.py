"""Void rent charges and free-rent concessions (docs/qa/15/PLAN-015d §3).

Schema, on ``rent_charges``:

- ``status`` gains ``void`` (a charge that never fell due: Change start date moved the start
  past its month); the ``rent_charges_status_valid`` CHECK is recreated with it.
- ``concession`` (money, default 0) and ``concession_text`` (free text, default empty): the
  free rent of the charge's days. Two CHECKs hold ``0 ≤ concession ≤ amount_due``.
- ``balance``, the generated column, becomes ``amount_due − concession − amount_paid``, and 0
  for a void charge. A generated column cannot be altered, so it is dropped and added again
  with the new expression (every existing row has no concession and no void status, so every
  balance is what it was).

Free text is guarded: on PostgreSQL the ``rent_charges`` trigger of ``0034`` is recreated with
``concession_text`` among its arguments; the reverse restores ``0034``'s columns. On PostgreSQL
any check queued earlier in the transaction runs before the first ``ALTER TABLE`` (the lesson
of ``0018``).

The reverse refuses, with one sentence and nothing changed, while a charge is void or carries
a concession: the old CHECK would reject the first and the old balance would overstate the
second.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import F, Q

import ledger.models
import ledger.validators

TABLE = "rent_charges"
_BEFORE: tuple[str, ...] = importlib.import_module(
    "ledger.migrations.0034_proration"
).GUARDED_COLUMNS[TABLE]

#: Table -> guarded columns after this migration. Replaces the ``rent_charges`` entry of ``0034``.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE: (*_BEFORE, "concession_text"),
}

STATUS_BEFORE = [
    ("due", "Due"),
    ("pending", "Pending"),
    ("paid", "Paid"),
    ("partial", "Partially paid"),
    ("unpaid", "Unpaid"),
    ("waived", "Waived"),
]
STATUS_AFTER = [*STATUS_BEFORE, ("void", "Void")]
CANNOT_REVERSE = (
    "A rent charge is void or carries a free-rent concession; this migration refuses to reverse."
)

_MONEY = models.DecimalField(decimal_places=2, max_digits=12)


def _balance(expression: models.Case) -> models.GeneratedField:
    return models.GeneratedField(db_persist=True, expression=expression, output_field=_MONEY)


BALANCE_AFTER = models.Case(
    models.When(status="void", then=models.Value(Decimal("0.00"))),
    default=F("amount_due") - F("concession") - F("amount_paid"),
    output_field=_MONEY,
)


def _status_check(choices: list[tuple[str, str]]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(status__in=[value for value, _label in choices]),
        name="rent_charges_status_valid",
    )


def _replace_trigger(schema_editor: BaseDatabaseSchemaEditor, columns: tuple[str, ...]) -> None:
    arguments = ", ".join(f"'{column}'" for column in columns)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {TABLE}_payment_data_guard ON {TABLE};", params=None
    )
    schema_editor.execute(
        f"CREATE TRIGGER {TABLE}_payment_data_guard "
        f"BEFORE INSERT OR UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
        params=None,
    )


def guard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, GUARDED_COLUMNS[TABLE])


def unguard_new_column(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    _replace_trigger(schema_editor, _BEFORE)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    charge = apps.get_model("ledger", "RentCharge")
    if charge.objects.filter(Q(status="void") | Q(concession__gt=0)).exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0071_invoice_pdfs"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(model_name="rentcharge", name="rent_charges_status_valid"),
        migrations.AlterField(
            model_name="rentcharge",
            name="status",
            field=models.CharField(choices=STATUS_AFTER, max_length=32),
        ),
        migrations.AddConstraint(model_name="rentcharge", constraint=_status_check(STATUS_AFTER)),
        migrations.AddField(
            model_name="rentcharge",
            name="concession",
            field=ledger.models.MoneyField(default=Decimal("0.00")),
        ),
        migrations.AddField(
            model_name="rentcharge",
            name="concession_text",
            field=models.TextField(
                blank=True, default="", validators=[ledger.validators.validate_no_payment_data]
            ),
        ),
        migrations.AddConstraint(
            model_name="rentcharge",
            constraint=models.CheckConstraint(
                condition=Q(concession__gte=0), name="rent_charges_concession_not_negative"
            ),
        ),
        migrations.AddConstraint(
            model_name="rentcharge",
            constraint=models.CheckConstraint(
                condition=Q(concession__lte=F("amount_due")),
                name="rent_charges_concession_within_amount",
            ),
        ),
        # A generated column cannot be altered: dropped and added with the new expression.
        migrations.RemoveField(model_name="rentcharge", name="balance"),
        migrations.AddField(model_name="rentcharge", name="balance", field=_balance(BALANCE_AFTER)),
        migrations.RunPython(guard_new_column, unguard_new_column, elidable=False),
        # Last, so that the reverse checks the rows first.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
