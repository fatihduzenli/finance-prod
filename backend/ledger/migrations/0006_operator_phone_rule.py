"""Stored operator phones and the payment provider's phone rule (PLAN-006b §2.5).

A stored phone that passes the rule is rewritten as its normalised digits; an operator
whose stored phone fails it is flagged ``needs_review`` (the phone is kept, nothing is
deleted). Empty phones are left alone. The rule is the one the form and the adapter
use, :func:`payments.stax.validation.normalise_phone` (named through
``payments.operator_sync`` until PLAN-014 C3). The reverse changes nothing.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from payments.stax.validation import normalise_phone


def apply_phone_rule(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    operators = apps.get_model("ledger", "Operator").objects.using(schema_editor.connection.alias)
    for pk, stored in list(operators.exclude(phone="").values_list("pk", "phone")):
        digits = normalise_phone(stored)
        if digits is None:
            operators.filter(pk=pk).update(needs_review=True)
        elif digits != stored:
            operators.filter(pk=pk).update(phone=digits)


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0005_operator_sync_failure_fields"),
    ]

    operations = [
        migrations.RunPython(apply_phone_rule, migrations.RunPython.noop, elidable=False),
    ]
