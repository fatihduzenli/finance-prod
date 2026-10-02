"""Provider-side tables and the provider_status write rule (PLAN-004 §6 rows 3 and 8).

Row 3 for ``external_refs``, ``provider_snapshots``, ``deposits``, ``webhook_events``,
``sync_state``, ``jobs`` (PLAN-010 D1), ``reconciliation_runs`` and
``reconciliation_mismatches`` (PLAN-010 D10), with the constraint-case machinery of
``test_ledger_models``.

Row 8: ``invoices.provider_status`` is written only inside
``payments.sync_writer.provider_write()``; on PostgreSQL a trigger rejects every other
write, and the permission does not outlive the block.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.db import IntegrityError, connection, models, transaction
from django.test import override_settings

from ledger.models import PROVIDER_STATUS_NOTE, Invoice
from payments import models as provider
from payments.demo import write_demo_provider_statuses
from payments.sync_writer import SETTING_NAME, provider_write
from tests.test_ledger_models import (
    ConstraintCase,
    _default,
    assert_accepted,
    assert_rejected,
    declared_constraints,
    new_invoice,
    requires_postgresql,
    saved,
    unique_pair,
)

pytestmark = pytest.mark.django_db

D = Decimal
BACKEND = Path(__file__).resolve().parents[1]
FETCHED_AT = datetime(2026, 9, 29, 6, tzinfo=UTC)


# --- Factories ----------------------------------------------------------------------


def new_external_ref(**fields: Any) -> provider.ExternalRef:
    values = {
        "entity_type": "operator",
        "entity_id": uuid.uuid4(),
        "provider": "stax_pay",
        "external_kind": "customer",
        "external_id": f"test-customer-{uuid.uuid4().hex[:8]}",
    }
    return provider.ExternalRef(**{**values, **fields})


def new_snapshot(**fields: Any) -> provider.ProviderSnapshot:
    _default(fields, "external_ref", lambda: saved(new_external_ref()))
    values = {"payload": {"status": "PAID"}, "fetched_at": FETCHED_AT}
    return provider.ProviderSnapshot(**{**values, **fields})


def new_deposit(**fields: Any) -> provider.Deposit:
    values = {
        "provider": "stax_pay",
        "provider_deposit_id": "test-deposit-0001",
        "deposit_date": FETCHED_AT.date(),
        "gross": D("2450.00"),
        "fees": D("12.25"),
        "net": D("2437.75"),
        "status": "settled",
    }
    return provider.Deposit(**{**values, **fields})


def new_webhook_event(**fields: Any) -> provider.WebhookEvent:
    values = {
        "event_type": "update_invoice",
        "provider_object_id": "test-invoice-0001",
        "dedupe_hash": "a" * 64,
    }
    return provider.WebhookEvent(**{**values, **fields})


def new_sync_state(**fields: Any) -> provider.SyncState:
    return provider.SyncState(**{"job_name": "nightly_reconciliation", **fields})


def new_job(**fields: Any) -> provider.Job:
    values = {
        "kind": provider.JobKind.OPERATOR_SYNC,
        "entity_type": provider.JobEntity.OPERATOR,
        "entity_id": uuid.uuid4(),
    }
    return provider.Job(**{**values, **fields})


def new_reconciliation_run(**fields: Any) -> provider.ReconciliationRun:
    values = {"trigger": provider.ReconciliationTrigger.MANUAL, "started_at": FETCHED_AT}
    return provider.ReconciliationRun(**{**values, **fields})


def new_reconciliation_mismatch(**fields: Any) -> provider.ReconciliationMismatch:
    _default(fields, "run", lambda: saved(new_reconciliation_run()))
    values = {
        "kind": provider.MismatchKind.PENDING_TOO_OLD,
        "entity_type": "payment",
        "entity_id": uuid.uuid4(),
    }
    return provider.ReconciliationMismatch(**{**values, **fields})


PAYMENTS_FACTORIES: dict[type[models.Model], Callable[..., models.Model]] = {
    provider.ExternalRef: new_external_ref,
    provider.ProviderSnapshot: new_snapshot,
    provider.Deposit: new_deposit,
    provider.WebhookEvent: new_webhook_event,
    provider.SyncState: new_sync_state,
    provider.Job: new_job,
    provider.ReconciliationRun: new_reconciliation_run,
    provider.ReconciliationMismatch: new_reconciliation_mismatch,
}


def _same_entity(row: Any) -> Any:
    return row.entity_id


PAYMENTS_CASES: list[ConstraintCase] = [
    ConstraintCase(
        "external_refs_external_id_unique",
        unique_pair(
            new_external_ref,
            {"external_id": "test-0001"},
            {"external_id": "test-0001", "external_kind": "payment_method"},
        ),
        unique_pair(new_external_ref, {"external_id": "test-0001"}, {"external_id": "test-0001"}),
    ),
    ConstraintCase(
        "external_refs_one_current_ref",
        unique_pair(
            new_external_ref,
            {"superseded_at": FETCHED_AT},
            {"entity_id": _same_entity},
        ),
        unique_pair(new_external_ref, {}, {"entity_id": _same_entity}),
    ),
    ConstraintCase(
        "deposits_provider_id_unique",
        unique_pair(new_deposit, {}, {"provider_deposit_id": "test-deposit-0002"}),
        unique_pair(new_deposit, {}, {}),
    ),
    ConstraintCase(
        "deposits_net_is_gross_minus_fees",
        lambda: new_deposit(gross=D("0.30"), fees=D("0.10"), net=D("0.20")),
        lambda: new_deposit(gross=D("0.30"), fees=D("0.10"), net=D("0.21")),
    ),
    ConstraintCase(
        "webhook_events.dedupe_hash",
        unique_pair(new_webhook_event, {}, {"dedupe_hash": "b" * 64}),
        unique_pair(new_webhook_event, {}, {}),
        column="dedupe_hash",
    ),
    ConstraintCase(
        "webhook_events_event_source_valid",
        lambda: new_webhook_event(event_source="shape"),
        lambda: new_webhook_event(event_source="query_string"),
    ),
    ConstraintCase(
        "webhook_events_object_id_source_valid",
        lambda: new_webhook_event(object_id_source="mergeVars.invoice.id"),
        lambda: new_webhook_event(object_id_source="body.anything"),
    ),
    ConstraintCase(
        "sync_state.job_name",
        unique_pair(new_sync_state, {}, {"job_name": "webhook_refetch"}),
        unique_pair(new_sync_state, {}, {}),
        column="job_name",
    ),
    ConstraintCase(
        "jobs_kind_valid",
        lambda: new_job(kind="payment_void", entity_type="payment"),
        lambda: new_job(kind="webhook_event"),
    ),
    ConstraintCase(
        "jobs_entity_type_valid",
        lambda: new_job(entity_type="rent_charge"),
        lambda: new_job(entity_type="store"),
    ),
    ConstraintCase(
        "jobs_status_valid",
        lambda: new_job(status="dead"),
        lambda: new_job(status="retrying"),
    ),
    ConstraintCase(
        "jobs.replay_key",
        unique_pair(
            new_job,
            {"replay_key": uuid.UUID(int=1)},
            {"replay_key": uuid.UUID(int=2)},
        ),
        unique_pair(new_job, {"replay_key": uuid.UUID(int=1)}, {"replay_key": uuid.UUID(int=1)}),
        column="replay_key",
    ),
    # PLAN-010 D10.
    ConstraintCase(
        "reconciliation_runs_trigger_valid",
        lambda: new_reconciliation_run(trigger="scheduled"),
        lambda: new_reconciliation_run(trigger="cron"),
    ),
    ConstraintCase(
        "reconciliation_runs_status_valid",
        lambda: new_reconciliation_run(status="skipped"),
        lambda: new_reconciliation_run(status="paused"),
    ),
    ConstraintCase(
        "reconciliation_runs_step_reached_valid",
        lambda: new_reconciliation_run(step_reached="c"),
        lambda: new_reconciliation_run(step_reached="g"),
    ),
    ConstraintCase(
        "reconciliation_mismatches_kind_valid",
        lambda: new_reconciliation_mismatch(kind="deposit_unmatched"),
        lambda: new_reconciliation_mismatch(kind="amount_differs"),
    ),
    ConstraintCase(
        "reconciliation_mismatches_resolved_has_note",
        lambda: new_reconciliation_mismatch(resolved_at=FETCHED_AT, resolution_note="Checked."),
        lambda: new_reconciliation_mismatch(resolved_at=FETCHED_AT, resolution_note="  "),
    ),
    ConstraintCase(
        "reconciliation_mismatches_one_open",
        unique_pair(
            new_reconciliation_mismatch,
            {},
            {"entity_id": _same_entity, "resolved_at": FETCHED_AT, "resolution_note": "Done."},
        ),
        unique_pair(new_reconciliation_mismatch, {}, {"entity_id": _same_entity}),
    ),
]


# --- Row 3 --------------------------------------------------------------------------


def test_every_constraint_has_a_case() -> None:
    assert {case.name for case in PAYMENTS_CASES} == declared_constraints("payments")


@pytest.mark.parametrize("case", PAYMENTS_CASES, ids=str)
def test_constraint_accepts_a_valid_row(case: ConstraintCase) -> None:
    assert_accepted(case)


@pytest.mark.parametrize("case", PAYMENTS_CASES, ids=str)
def test_constraint_rejects_an_invalid_row(case: ConstraintCase) -> None:
    assert_rejected(case)


def test_a_superseded_ref_frees_the_entity_for_a_new_one() -> None:
    first = saved(new_external_ref())
    provider.ExternalRef.objects.filter(pk=first.pk).update(superseded_at=FETCHED_AT)
    saved(new_external_ref(entity_id=first.entity_id))
    assert provider.ExternalRef.objects.filter(entity_id=first.entity_id).count() == 2


# --- Row 8: provider_status ---------------------------------------------------------


def _setting() -> str | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [SETTING_NAME])
        row = cursor.fetchone()
    assert row is not None
    value: str | None = row[0]
    return value


@requires_postgresql
def test_plain_save_with_a_provider_status_is_rejected() -> None:
    invoice = new_invoice(provider_status="PAID")
    with pytest.raises(IntegrityError, match="written only by the sync layer"):
        with transaction.atomic():
            invoice.save()
    assert not Invoice.objects.filter(pk=invoice.pk).exists()


@requires_postgresql
@pytest.mark.parametrize("path", ["save", "queryset_update", "raw_update"])
def test_changing_provider_status_outside_the_sync_writer_is_rejected(path: str) -> None:
    invoice = saved(new_invoice())
    with pytest.raises(IntegrityError, match="written only by the sync layer"):
        with transaction.atomic():
            if path == "save":
                invoice.provider_status = "PAID"
                invoice.save()
            elif path == "queryset_update":
                Invoice.objects.filter(pk=invoice.pk).update(provider_status="PAID")
            else:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE invoices SET provider_status = 'PAID' WHERE id = %s", [invoice.pk]
                    )
    invoice.refresh_from_db()
    assert invoice.provider_status == ""


_RAW_INSERT = (
    "INSERT INTO invoices (id, invoice_number, operator_id, issue_date, due_date, memo, "
    "subtotal, tax, total, amount_paid, internal_status, provider_status, allow_card, "
    "allow_ach, creation_key, legacy, created_at, updated_at) "
    "SELECT %s, 'INV-2026-8888', operator_id, issue_date, due_date, memo, subtotal, tax, "
    "total, amount_paid, internal_status, 'PAID', allow_card, allow_ach, %s, legacy, "
    "created_at, updated_at FROM invoices WHERE id = %s"
)


@requires_postgresql
@pytest.mark.parametrize("path", ["bulk_create", "raw_insert"])
def test_inserting_a_provider_status_outside_the_sync_writer_is_rejected(path: str) -> None:
    template = saved(new_invoice())
    with pytest.raises(IntegrityError, match="written only by the sync layer"):
        with transaction.atomic():
            if path == "bulk_create":
                Invoice.objects.bulk_create([new_invoice(provider_status="PAID")])
            else:
                with connection.cursor() as cursor:
                    cursor.execute(_RAW_INSERT, [uuid.uuid4(), uuid.uuid4(), template.pk])
    assert list(Invoice.objects.values_list("provider_status", flat=True)) == [""]


def test_other_invoice_changes_are_allowed_outside_the_sync_writer() -> None:
    invoice = saved(new_invoice())
    with provider_write():
        Invoice.objects.filter(pk=invoice.pk).update(provider_status="SENT")
    invoice.refresh_from_db()
    invoice.memo = "Second reminder sent."
    invoice.internal_status = "sent"
    invoice.save()
    invoice.refresh_from_db()
    assert (invoice.memo, invoice.provider_status) == ("Second reminder sent.", "SENT")


def test_provider_write_allows_insert_and_update() -> None:
    with provider_write():
        invoice = saved(new_invoice(provider_status="DRAFT"))
        Invoice.objects.filter(pk=invoice.pk).update(provider_status="PARTIALLY APPLIED")
    invoice.refresh_from_db()
    assert invoice.provider_status == "PARTIALLY APPLIED"


@requires_postgresql
def test_the_setting_is_off_again_after_the_block_within_the_same_transaction() -> None:
    invoice = saved(new_invoice())
    with provider_write():
        assert _setting() == "on"
    assert _setting() == "off"
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(provider_status="PAID")


@requires_postgresql
def test_the_setting_is_undone_when_the_block_raises() -> None:
    invoice = saved(new_invoice())
    with pytest.raises(RuntimeError, match="sync failed"), provider_write():
        Invoice.objects.filter(pk=invoice.pk).update(provider_status="PAID")
        raise RuntimeError("sync failed")
    assert _setting() != "on"
    invoice.refresh_from_db()
    assert invoice.provider_status == ""


@requires_postgresql
@pytest.mark.django_db(transaction=True)
def test_the_setting_does_not_leak_to_the_next_transaction() -> None:
    invoice = saved(new_invoice())
    with provider_write():
        Invoice.objects.filter(pk=invoice.pk).update(provider_status="SENT")
        assert _setting() == "on"
    # A new transaction: set_config(..., true) ended with the previous one.
    with transaction.atomic():
        assert _setting() in (None, "", "off")
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(provider_status="PAID")
    invoice.refresh_from_db()
    assert invoice.provider_status == "SENT"


@requires_postgresql
def test_provider_status_has_the_column_comment() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT col_description('invoices'::regclass, attnum) FROM pg_attribute "
            "WHERE attrelid = 'invoices'::regclass AND attname = 'provider_status'"
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == PROVIDER_STATUS_NOTE


def _imports_sync_writer(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any("sync_writer" in alias.name for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "sync_writer" in module or any(alias.name == "sync_writer" for alias in node.names):
                return True
        elif isinstance(node, ast.Call) and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and "sync_writer" in str(argument.value):
                return True
    return False


def test_no_module_outside_payments_imports_sync_writer() -> None:
    offenders = [
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if path.parts[len(BACKEND.parts)] not in ("payments", "tests")
        and _imports_sync_writer(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_the_import_check_detects_an_import() -> None:
    for source in (
        "from payments.sync_writer import provider_write",
        "from payments import sync_writer",
        "import payments.sync_writer",
        "importlib.import_module('payments.sync_writer')",
    ):
        assert _imports_sync_writer(ast.parse(source)), source


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_demo_provider_statuses_are_refused_outside_local(app_env: str) -> None:
    invoice = saved(new_invoice())
    with override_settings(APP_ENV=app_env), pytest.raises(RuntimeError, match="APP_ENV=local"):
        write_demo_provider_statuses({invoice.pk: "PAID"})
    invoice.refresh_from_db()
    assert invoice.provider_status == ""


def test_demo_provider_statuses_change_only_what_differs() -> None:
    invoice = saved(new_invoice())
    assert write_demo_provider_statuses({invoice.pk: "VIEWED"}) == 1
    assert write_demo_provider_statuses({invoice.pk: "VIEWED"}) == 0
    invoice.refresh_from_db()
    assert invoice.provider_status == "VIEWED"
