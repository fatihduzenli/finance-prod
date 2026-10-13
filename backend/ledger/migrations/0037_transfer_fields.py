"""The transfer wizard's state (docs/qa/PLAN-012e-lease-end-transfer-installments.md D6).

Schema, on ``lease_transfers`` (created empty by ``0035``): ``status`` (``draft`` or
``confirmed``, default ``draft``), the incoming operator and the incoming lease's terms
(``incoming_operator``, ``incoming_started_on``, ``incoming_rent``,
``incoming_debit_day``, ``incoming_second_debit_day``, ``incoming_channel``,
``incoming_prorate_first_month``), ``deposit_amount`` and ``setup_lines`` (the ticked
setup items, guarded JSON). CHECKs: the status and channel values, the debit days 1 to
31, a rent above zero, a deposit not below zero, a confirmed transfer names its end,
its incoming lease and its moment; one draft per store.

Existing rows: a row with ``confirmed_at`` becomes ``confirmed``, every other row stays
a draft. On PostgreSQL ``SET CONSTRAINTS ALL IMMEDIATE`` runs any check this transaction
queued before the ``ALTER TABLE`` statements (the lesson of ``0018``), and the new JSON
column gets the payment-data trigger of ``0002_postgresql_guards``.

The reverse refuses while a transfer holds anything the new columns keep (confirmed,
an incoming operator or term, a deposit amount or a setup line).
"""

from __future__ import annotations

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q

import ledger.models
import ledger.validators

#: Table -> guarded columns created by this migration.
GUARDED_COLUMNS: dict[str, tuple[str, ...]] = {"lease_transfers": ("setup_lines",)}
#: The lease's payment channels (``leases.payment_channel``, created in ``0001`` and
#: unchanged since); read from the model so that the provider's name is written in one
#: place (the provider isolation test).
CHANNELS = list(ledger.models.PaymentChannel.choices)
CANNOT_REVERSE = (
    "Lease transfers in progress or confirmed exist; this migration refuses to reverse."
)


def run_pending_checks(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE;", params=None)


def mark_confirmed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("ledger", "LeaseTransfer").objects.filter(confirmed_at__isnull=False).update(
        status="confirmed"
    )


def create_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table, columns in GUARDED_COLUMNS.items():
        arguments = ", ".join(f"'{column}'" for column in columns)
        schema_editor.execute(
            f"CREATE TRIGGER {table}_payment_data_guard "
            f"BEFORE INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION finance_reject_payment_data({arguments});",
            params=None,
        )


def drop_guards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in GUARDED_COLUMNS:
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_payment_data_guard ON {table};", params=None
        )


def refuse_reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    kept = (
        Q(status="confirmed")
        | Q(incoming_operator__isnull=False)
        | Q(incoming_started_on__isnull=False)
        | Q(incoming_rent__isnull=False)
        | Q(incoming_debit_day__isnull=False)
        | Q(deposit_amount__isnull=False)
        | ~Q(incoming_channel="")
        | ~Q(setup_lines=[])
    )
    if apps.get_model("ledger", "LeaseTransfer").objects.filter(kept).exists():
        raise RuntimeError(CANNOT_REVERSE)
    run_pending_checks(apps, schema_editor)


def _day_range(field: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__gte": 1, f"{field}__lte": 31}),
        name=f"lease_transfers_{field}_range",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0036_messages_kind"),
    ]

    operations = [
        # Any check this transaction queued runs before the ALTER TABLE statements.
        migrations.RunPython(run_pending_checks, migrations.RunPython.noop, elidable=False),
        migrations.AddField(
            model_name="leasetransfer",
            name="status",
            field=models.CharField(
                choices=[("draft", "Draft"), ("confirmed", "Confirmed")],
                default="draft",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_operator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.operator",
            ),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_started_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_rent",
            field=ledger.models.MoneyField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_debit_day",
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_second_debit_day",
            field=models.SmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_channel",
            field=models.CharField(blank=True, choices=CHANNELS, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="incoming_prorate_first_month",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="deposit_amount",
            field=ledger.models.MoneyField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leasetransfer",
            name="setup_lines",
            field=models.JSONField(
                blank=True,
                default=list,
                validators=[ledger.validators.validate_json_has_no_payment_data],
            ),
        ),
        migrations.RunPython(mark_confirmed, migrations.RunPython.noop, elidable=False),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.CheckConstraint(
                condition=Q(status__in=["draft", "confirmed"]),
                name="lease_transfers_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.CheckConstraint(
                condition=Q(incoming_channel__in=[*(value for value, _label in CHANNELS), ""]),
                name="lease_transfers_incoming_channel_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasetransfer", constraint=_day_range("incoming_debit_day")
        ),
        migrations.AddConstraint(
            model_name="leasetransfer", constraint=_day_range("incoming_second_debit_day")
        ),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.CheckConstraint(
                condition=Q(incoming_rent__isnull=True) | Q(incoming_rent__gt=0),
                name="lease_transfers_incoming_rent_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.CheckConstraint(
                condition=Q(deposit_amount__isnull=True) | Q(deposit_amount__gte=0),
                name="lease_transfers_deposit_amount_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.CheckConstraint(
                condition=~Q(status="confirmed")
                | Q(
                    confirmed_at__isnull=False,
                    lease_end__isnull=False,
                    incoming_lease__isnull=False,
                ),
                name="lease_transfers_confirmed_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.CheckConstraint(
                condition=Q(status="confirmed") | Q(confirmed_at__isnull=True),
                name="lease_transfers_confirmed_at_iff_confirmed",
            ),
        ),
        migrations.AddConstraint(
            model_name="leasetransfer",
            constraint=models.UniqueConstraint(
                condition=Q(status="draft"),
                fields=("store",),
                name="lease_transfers_one_draft_per_store",
            ),
        ),
        migrations.RunPython(create_guards, drop_guards, elidable=False),
        # --- The reverse starts here: it refuses before anything is dropped.
        migrations.RunPython(migrations.RunPython.noop, refuse_reverse, elidable=False),
    ]
