"""The "E-mail invoice" payment channel (docs/qa/PLAN-014b item 1, Fable's decision 1).

``PaymentChannel.INVOICE`` (``invoice``) joins the lease's channels and the transfer's
incoming channel: each column's ``choices`` and its CHECK (``leases_payment_channel_valid``,
``lease_transfers_incoming_channel_valid``) accept the new value. No row is touched and no
column is added, so no payment-data trigger changes (an empty ``GUARDED_COLUMNS``).

The reverse refuses, with one sentence and nothing changed, while a lease or a transfer holds
``invoice``: the old CHECK would reject it. The values are read from the model (as ``0037`` and
``0046`` read them) so that the provider's stored value is written in one place (the provider
isolation test).
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

import ledger.models

GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {}
NEW_VALUE = "invoice"
#: The channels before this migration (``0046``'s) and after it.
BEFORE = [
    choice
    for choice in ledger.models.PaymentChannel.choices
    if choice[0] not in ledger.models.LATER_CHANNELS
]
AFTER = list(ledger.models.PaymentChannel.choices)
CANNOT_REVERSE = (
    "A lease or a lease transfer uses the E-mail invoice channel; "
    "this migration refuses to reverse."
)


def _values(choices: list[tuple[str, str]]) -> list[str]:
    return [value for value, _label in choices]


def _lease_check(choices: list[tuple[str, str]]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(payment_channel__in=_values(choices)), name="leases_payment_channel_valid"
    )


def _transfer_check(choices: list[tuple[str, str]]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(incoming_channel__in=[*_values(choices), ""]),
        name="lease_transfers_incoming_channel_valid",
    )


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Any check this transaction queued runs before the ALTER TABLE statements (``0018``)."""
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if (
        apps.get_model("ledger", "Lease").objects.filter(payment_channel=NEW_VALUE).exists()
        or apps.get_model("ledger", "LeaseTransfer")
        .objects.filter(incoming_channel=NEW_VALUE)
        .exists()
    ):
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0069_pay_line_and_settings_holds")]

    operations = [
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.RemoveConstraint(model_name="lease", name="leases_payment_channel_valid"),
        migrations.RemoveConstraint(
            model_name="leasetransfer", name="lease_transfers_incoming_channel_valid"
        ),
        migrations.AlterField(
            model_name="lease",
            name="payment_channel",
            field=models.CharField(choices=AFTER, max_length=32),
        ),
        migrations.AlterField(
            model_name="leasetransfer",
            name="incoming_channel",
            field=models.CharField(blank=True, choices=AFTER, default="", max_length=32),
        ),
        migrations.AddConstraint(model_name="lease", constraint=_lease_check(AFTER)),
        migrations.AddConstraint(model_name="leasetransfer", constraint=_transfer_check(AFTER)),
        # Last, so that the reverse checks the rows first.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
