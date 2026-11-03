"""PLAN-014b item 1 (Fable's decisions 1 to 8): the New lease page.

Decision 1, the "E-mail invoice" channel and ledger migration 0070 (forward and back, on
sqlite and on PostgreSQL: run this file on both; the reverse refused while a lease or a
transfer holds ``invoice``).
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from ledger.models import Lease, PaymentChannel
from tests.receivables_helpers import a_lease, a_store, an_operator

pytestmark = pytest.mark.django_db


# --- Decision 1: the channel and migration 0070 -----------------------------------------------

MIGRATION_0070 = importlib.import_module("ledger.migrations.0070_payment_channel_invoice")
BEFORE_0070 = ("ledger", "0069_pay_line_and_settings_holds")
AFTER_0070 = ("ledger", "0070_payment_channel_invoice")


def _migrate(*targets: tuple[str, str]) -> Any:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(list(targets))
    return MigrationExecutor(connection).loader.project_state(list(targets)).apps


def test_the_invoice_channel_is_e_mail_invoice_and_0070_adds_no_guarded_column() -> None:
    assert (PaymentChannel.INVOICE.value, PaymentChannel.INVOICE.label) == (
        "invoice",
        "E-mail invoice",
    )
    assert MIGRATION_0070.GUARDED_COLUMNS == {}
    assert "invoice" not in [value for value, _ in MIGRATION_0070.BEFORE]
    assert [value for value, _ in MIGRATION_0070.AFTER][-1] == "invoice"
    # The earlier migrations that read the channels from the model keep what they applied.
    for name in ("0037_transfer_fields", "0046_copy_labels"):
        module = importlib.import_module(f"ledger.migrations.{name}")
        assert module.CHANNELS == MIGRATION_0070.BEFORE, name


def test_a_lease_saves_the_invoice_channel_and_an_unknown_value_is_refused() -> None:
    lease = a_lease(a_store("7001", number=7001), channel=PaymentChannel.INVOICE)
    lease.refresh_from_db()
    assert lease.payment_channel == "invoice"
    with pytest.raises(IntegrityError), transaction.atomic():
        Lease.objects.filter(pk=lease.pk).update(payment_channel="e-mail")


@pytest.mark.django_db(transaction=True)
def test_0070_forward_and_back_and_the_old_check_refuses_invoice() -> None:
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        old = _migrate(BEFORE_0070)
        store = old.get_model("ledger", "Store").objects.create(
            external_label="7002", status="active"
        )
        operator = old.get_model("ledger", "Operator").objects.create(display_name="Back And Forth")
        model = old.get_model("ledger", "Lease")
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.create(
                store=store,
                operator=operator,
                debit_day=1,
                collection_status="prospective",
                payment_channel="invoice",
            )
        new = _migrate(AFTER_0070)
        row = new.get_model("ledger", "Lease").objects.create(
            store_id=store.pk,
            operator_id=operator.pk,
            debit_day=1,
            collection_status="active",
            payment_channel="invoice",
        )
        new.get_model("ledger", "Lease").objects.filter(pk=row.pk).update(payment_channel="other")
        back = _migrate(BEFORE_0070)
        assert back.get_model("ledger", "Lease").objects.get(pk=row.pk).payment_channel == "other"
    finally:
        _migrate(*leaves)
        Lease.objects.all().delete()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("holder", ["lease", "transfer"])
def test_0070_reverse_is_refused_while_a_lease_or_a_transfer_holds_invoice(holder: str) -> None:
    from ledger.models import LeaseTransfer

    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    lease = a_lease(a_store("7003", number=7003), an_operator("Holds Invoice"))
    if holder == "lease":
        Lease.objects.filter(pk=lease.pk).update(payment_channel=PaymentChannel.INVOICE)
    else:
        LeaseTransfer.objects.create(
            store=lease.store, outgoing_lease=lease, incoming_channel=PaymentChannel.INVOICE
        )
    try:
        with pytest.raises(RuntimeError, match="refuses to reverse"):
            _migrate(BEFORE_0070)
    finally:
        _migrate(*leaves)
        LeaseTransfer.objects.all().delete()
        Lease.objects.all().delete()
