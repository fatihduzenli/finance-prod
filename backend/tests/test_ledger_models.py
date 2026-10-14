"""Ledger tables: constraints, money and model rules (PLAN-004 §6 rows 3, 4 and 9).

Row 3: every CHECK and unique constraint of PLAN-004 §4.1 is exercised at database
level. Each case inserts a valid row (accepted) and an invalid one (``IntegrityError``)
with ``bulk_create``, which skips ``save()`` and so reaches the database unchanged.
``test_every_constraint_has_a_case`` fails when a constraint is added without a case.

The factories here build one valid, unsaved row per model (dependencies are saved) and
are reused by the other PLAN-004 test modules.
"""

from __future__ import annotations

import functools
import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.utils import timezone

from ledger import models as ledger
from ledger.validators import PAYMENT_DATA_MESSAGE

pytestmark = pytest.mark.django_db

requires_postgresql = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="database triggers exist on PostgreSQL only; the test database is not PostgreSQL",
)

D = Decimal
_numbers = itertools.count(1)


# --- Factories: one valid, unsaved row per model ------------------------------------


def saved[ModelT: models.Model](instance: ModelT) -> ModelT:
    instance.save()
    return instance


def make_user() -> User:
    return User.objects.create(username=f"user-{uuid.uuid4().hex[:12]}@example.com")


def _default(fields: dict[str, Any], name: str, factory: Callable[[], Any]) -> None:
    if name not in fields:
        fields[name] = factory()


def new_operator(**fields: Any) -> ledger.Operator:
    return ledger.Operator(**{"display_name": "Test Operator LLC", "email": None, **fields})


def new_store(**fields: Any) -> ledger.Store:
    values = {
        "external_label": "Store 900 Testville",
        "store_number": 900,
        "open_date": date(2024, 1, 1),
        "needs_open_date": False,
        "status": "active",
    }
    return ledger.Store(**{**values, **fields})


def new_lease(**fields: Any) -> ledger.Lease:
    _default(fields, "store", lambda: saved(new_store()))
    _default(fields, "operator", lambda: saved(new_operator()))
    values = {"debit_day": 5, "collection_status": "active", "payment_channel": "stax"}
    return ledger.Lease(**{**values, **fields})


def new_rent_history(**fields: Any) -> ledger.LeaseRentHistory:
    _default(fields, "lease", lambda: saved(new_lease()))
    values = {"amount": D("2450.00"), "effective_from": date(2026, 1, 1), "source": "staff"}
    return ledger.LeaseRentHistory(**{**values, **fields})


def new_schedule(**fields: Any) -> ledger.CollectionSchedule:
    _default(fields, "lease", lambda: saved(new_lease()))
    values = {
        "kind": "rent",
        "seq": 1,
        "debit_day": 5,
        "amount": D("2450.00"),
        "state": "active",
    }
    return ledger.CollectionSchedule(**{**values, **fields})


def new_rent_charge(**fields: Any) -> ledger.RentCharge:
    _default(fields, "lease", lambda: saved(new_lease()))
    values = {
        "period_month": date(2026, 9, 1),
        "seq": 1,
        "due_on": date(2026, 9, 5),
        "amount_due": D("2450.00"),
        "amount_paid": D("0.00"),
        "status": "due",
    }
    return ledger.RentCharge(**{**values, **fields})


def new_premium(**fields: Any) -> ledger.InsurancePremium:
    return ledger.InsurancePremium(**{"policy_year": 2026, "amount": D("695.00"), **fields})


def new_insurance_charge(**fields: Any) -> ledger.InsuranceCharge:
    _default(fields, "lease", lambda: saved(new_lease()))
    values = {
        "policy_year": 2026,
        "due_on": date(2026, 1, 15),
        "amount": D("695.00"),
        "needs_premium": False,
    }
    return ledger.InsuranceCharge(**{**values, **fields})


def new_fee_type(**fields: Any) -> ledger.FeeType:
    values = {"code": f"test_fee_{next(_numbers)}", "name": "Test fee", "sort_order": 900}
    return ledger.FeeType(**{**values, **fields})


def new_setup_charge(**fields: Any) -> ledger.OneOffCharge:
    _default(fields, "lease", lambda: saved(new_lease()))
    _default(fields, "fee_type", lambda: saved(new_fee_type()))
    values = {"price": D("1000.00"), "due_date": date(2026, 10, 1), "status": "owed"}
    return ledger.OneOffCharge(**{**values, **fields})


def new_one_off_charge(**fields: Any) -> ledger.OneOffCharge:
    """A one-off charge of a category other than setup, on the operator alone (PLAN-010d)."""
    _default(fields, "operator", lambda: saved(new_operator()))
    values = {
        "category": "damage",
        "collection": "manual",
        "description": "Broken shelf",
        "price": D("300.00"),
        "due_date": date(2026, 10, 1),
        "status": "owed",
    }
    return ledger.OneOffCharge(**{**values, **fields})


def new_store_cost(**fields: Any) -> ledger.StoreCost:
    _default(fields, "store", lambda: saved(new_store()))
    values = {"landlord_rent": D("2500.00"), "effective_from": date(2026, 1, 1), "source": "staff"}
    return ledger.StoreCost(**{**values, **fields})


def _superseded_store_cost(**fields: Any) -> ledger.StoreCost:
    """An unsaved superseded row, its replacement saved (PLAN-010c D10)."""
    replacement = saved(new_store_cost(effective_from=date(2026, 1, 1)))
    return new_store_cost(
        store=replacement.store,
        effective_from=date(2026, 1, 1),
        superseded_at=datetime(2026, 10, 5, tzinfo=UTC),
        superseded_by=replacement,
        **fields,
    )


def _self_superseded_store_cost() -> ledger.StoreCost:
    row_id = uuid.uuid4()
    return new_store_cost(
        id=row_id,
        superseded_at=datetime(2026, 10, 5, tzinfo=UTC),
        superseded_by_id=row_id,
        change_note="Wrong amount.",
    )


def new_deposit_resolution(**fields: Any) -> ledger.DepositResolution:
    _default(fields, "setup_charge", lambda: saved(new_setup_charge()))
    values = {
        "kind": "refunded",
        "method": "wire",
        "amount": D("1000.00"),
        "resolved_on": date(2026, 10, 1),
        "note": "Paid back by wire.",
    }
    return ledger.DepositResolution(**{**values, **fields})


def new_charge_note(**fields: Any) -> ledger.ChargeNote:
    if "insurance_charge" not in fields:
        _default(fields, "rent_charge", lambda: saved(new_rent_charge()))
    return ledger.ChargeNote(**{"body": "Operator called about the debit.", **fields})


def new_adjustment(**fields: Any) -> ledger.ChargeAdjustment:
    _default(fields, "to_charge", lambda: saved(new_rent_charge()))
    _default(fields, "created_by", make_user)
    values = {"amount": D("150.00"), "reason": "credit", "note": "Goodwill credit."}
    return ledger.ChargeAdjustment(**{**values, **fields})


def new_store_balance(**fields: Any) -> ledger.StoreBalance:
    _default(fields, "store", lambda: saved(new_store()))
    return ledger.StoreBalance(**{"kind": "sign", "amount": D("-50.00"), **fields})


def new_note(**fields: Any) -> ledger.Note:
    values = {"entity_type": "store", "entity_id": uuid.uuid4(), "body": "Test note."}
    return ledger.Note(**{**values, **fields})


def new_invoice(**fields: Any) -> ledger.Invoice:
    _default(fields, "operator", lambda: saved(new_operator()))
    values = {
        "invoice_number": f"INV-2026-{next(_numbers):04d}",
        "issue_date": date(2026, 9, 1),
        "due_date": date(2026, 9, 5),
        "subtotal": D("2450.00"),
        "tax": D("0.00"),
        "total": D("2450.00"),
    }
    return ledger.Invoice(**{**values, **fields})


def new_line_item(**fields: Any) -> ledger.InvoiceLineItem:
    _default(fields, "invoice", lambda: saved(new_invoice()))
    values = {
        "position": 1,
        "description": "Rent for September 2026",
        "quantity": D("1.0000"),
        "unit_price": D("2450.00"),
        "line_total": D("2450.00"),
    }
    return ledger.InvoiceLineItem(**{**values, **fields})


def new_invoice_draft(**fields: Any) -> ledger.InvoiceDraft:
    _default(fields, "operator", lambda: saved(new_operator()))
    return ledger.InvoiceDraft(**{"memo": "Signage and repairs, October.", **fields})


def new_invoice_draft_line(**fields: Any) -> ledger.InvoiceDraftLine:
    _default(fields, "draft", lambda: saved(new_invoice_draft()))
    values = {
        "position": 1,
        "item": "Sign repair",
        "description": "",
        "quantity": D("3.0000"),
        "unit_amount": D("33.33"),
        "line_total": D("99.99"),
    }
    return ledger.InvoiceDraftLine(**{**values, **fields})


def _line_of_a_charge(**fields: Any) -> ledger.InvoiceLineItem:
    """A line item naming its one-off charge (PLAN-012 D6)."""
    _default(fields, "one_off_charge", lambda: saved(new_one_off_charge()))
    return new_line_item(**fields)


def _sent_draft(**fields: Any) -> ledger.InvoiceDraft:
    _default(fields, "sent_invoice", lambda: saved(new_invoice()))
    return new_invoice_draft(**{"status": "sent", **fields})


def new_payment(**fields: Any) -> ledger.Payment:
    values = {"kind": "rent", "method": "ach", "amount": D("2450.00"), "status": "settled"}
    return ledger.Payment(**{**values, **fields})


def new_audit_log(**fields: Any) -> ledger.AuditLog:
    values = {"action": "update", "entity_type": "operator", "entity_id": str(uuid.uuid4())}
    return ledger.AuditLog(**{**values, **fields})


def new_legacy_record(**fields: Any) -> ledger.LegacyRecord:
    return ledger.LegacyRecord(**{"source_tab": "Rents", "row_number": 2, "raw": {}, **fields})


def new_import_audit(**fields: Any) -> ledger.ImportAudit:
    values = {
        "sheet_row": 2,
        "sheet_col": "D",
        "raw_value": "2450",
        "target_table": "rent_charges",
        "target_id": str(uuid.uuid4()),
        "rule_applied": "amount",
    }
    return ledger.ImportAudit(**{**values, **fields})


def new_document(**fields: Any) -> ledger.Document:
    if "lease" not in fields:
        _default(fields, "store", lambda: saved(new_store()))
    document_id = uuid.uuid4()
    values = {
        "id": document_id,
        "kind": "lease_agreement",
        "source": "upload",
        "original_filename": "lease.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
        "blob_path": f"documents/store/{uuid.uuid4()}/{document_id}",
        "sha256": uuid.uuid4().hex * 2,
    }
    return ledger.Document(**{**values, **fields})


def new_lease_document(**fields: Any) -> ledger.Document:
    _default(fields, "lease", lambda: saved(new_lease()))
    return new_document(store=None, **fields)


def new_operator_document(**fields: Any) -> ledger.Document:
    """Dispute evidence kept on an operator (PLAN-012 D8)."""
    _default(fields, "operator", lambda: saved(new_operator()))
    return new_document(store=None, kind="dispute_evidence", **fields)


def new_dispute(**fields: Any) -> ledger.Dispute:
    values = {
        "reason": "FRAUD",
        "amount": D("2450.00"),
        "status": "OPEN",
        "respond_by": datetime(2026, 10, 20, tzinfo=UTC),
        "fetched_at": datetime(2026, 10, 6, tzinfo=UTC),
        "raw_summary": {"status": "OPEN"},
    }
    return ledger.Dispute(**{**values, **fields})


def new_dispute_evidence(**fields: Any) -> ledger.DisputeEvidence:
    _default(fields, "dispute", lambda: saved(new_dispute()))
    _default(fields, "document", lambda: saved(new_operator_document()))
    return ledger.DisputeEvidence(**{"name": "Signed lease page 3", **fields})


def new_message_template(**fields: Any) -> ledger.MessageTemplate:
    values = {"code": f"test_template_{next(_numbers)}", "subject": "Notice", "body": "Owed."}
    return ledger.MessageTemplate(**{**values, **fields})


def new_collection_step(**fields: Any) -> ledger.CollectionStep:
    _default(fields, "template", lambda: saved(new_message_template()))
    return ledger.CollectionStep(**{"day_offset": 100 + next(_numbers) % 200, **fields})


def new_outbound_message(**fields: Any) -> ledger.OutboundMessage:
    _default(fields, "operator", lambda: saved(new_operator()))
    _default(fields, "step", lambda: saved(new_collection_step()))
    values = {
        "template": fields["step"].template,
        "charge_entity_type": "rent_charge",
        "charge_entity_id": uuid.uuid4(),
        "to_address": "ops@example.com",
        "subject": "Notice",
    }
    return ledger.OutboundMessage(**{**values, **fields})


def new_contact_entry(**fields: Any) -> ledger.ContactEntry:
    _default(fields, "operator", lambda: saved(new_operator()))
    values = {"kind": "call", "happened_on": date(2026, 10, 5), "body": "Called about rent."}
    return ledger.ContactEntry(**{**values, **fields})


def new_promise(**fields: Any) -> ledger.ContactEntry:
    values = {
        "kind": "promise",
        "promised_date": date(2026, 10, 9),
        "promised_amount": D("1500.00"),
    }
    return new_contact_entry(**{**values, **fields})


def new_calendar_entry(**fields: Any) -> ledger.CalendarEntry:
    values = {"kind": "note", "on": date(2026, 10, 20), "title": "Visit the store."}
    return ledger.CalendarEntry(**{**values, **fields})


def new_contact_correction(**fields: Any) -> ledger.ContactCorrection:
    _default(fields, "entry", lambda: saved(new_contact_entry()))
    values = {"body": "The visit was on the 4th."}
    return ledger.ContactCorrection(**{**values, **fields})


def new_calendar_feed(**fields: Any) -> ledger.CalendarFeed:
    _default(fields, "user", make_user)
    values = {"token_hash": uuid.uuid4().bytes * 2}
    return ledger.CalendarFeed(**{**values, **fields})


def new_lease_end(**fields: Any) -> ledger.LeaseEnd:
    """A draft lease end (PLAN-012e D4)."""
    _default(fields, "lease", lambda: saved(new_lease()))
    values = {"ended_on": date(2026, 10, 31), "reason": "moved_out"}
    return ledger.LeaseEnd(**{**values, **fields})


def new_confirmed_lease_end(**fields: Any) -> ledger.LeaseEnd:
    values = {"status": "confirmed", "confirmed_at": datetime(2026, 11, 1, 12, tzinfo=UTC)}
    return new_lease_end(**{**values, **fields})


def new_lease_end_deduction(**fields: Any) -> ledger.LeaseEndDeduction:
    _default(fields, "lease_end", lambda: saved(new_lease_end()))
    values = {"kind": "cleaning", "description": "Deep clean", "amount": D("150.00")}
    return ledger.LeaseEndDeduction(**{**values, **fields})


def new_lease_transfer(**fields: Any) -> ledger.LeaseTransfer:
    _default(fields, "outgoing_lease", lambda: saved(new_lease()))
    _default(fields, "store", lambda: fields["outgoing_lease"].store)
    return ledger.LeaseTransfer(**fields)


def new_installment_plan(**fields: Any) -> ledger.InstallmentPlan:
    """An active plan of a one-off charge (PLAN-012e D9)."""
    if "rent_charge" not in fields:
        _default(fields, "one_off_charge", lambda: saved(new_one_off_charge()))
    parent = fields.get("one_off_charge") or fields.get("rent_charge")
    _default(
        fields,
        "operator",
        lambda: (
            parent.operator if isinstance(parent, ledger.OneOffCharge) else saved(new_operator())
        ),
    )
    values = {
        "count": 3,
        "first_due": date(2026, 11, 1),
        "cadence": "monthly",
        "collection": "manual",
        "confirmed_at": datetime(2026, 10, 6, 12, tzinfo=UTC),
    }
    return ledger.InstallmentPlan(**{**values, **fields})


def new_installment(**fields: Any) -> ledger.OneOffCharge:
    """An installment of a plan (PLAN-012e D9)."""
    _default(fields, "plan", lambda: saved(new_installment_plan()))
    values = {"category": "installment", "description": "Installment 1 of 3", "installment_no": 1}
    return new_one_off_charge(**{**values, **fields})


def _second_plan(*, cancelled_first: bool, rent: bool) -> ledger.InstallmentPlan:
    parent = saved(new_rent_charge()) if rent else saved(new_one_off_charge())
    key = "rent_charge" if rent else "one_off_charge"
    first: dict[str, Any] = {key: parent}
    if cancelled_first:
        first |= {
            "status": "cancelled",
            "cancelled_at": datetime(2026, 10, 7, tzinfo=UTC),
            "note": "Stopped",
        }
    insert(new_installment_plan(**first))
    return new_installment_plan(**{key: parent})


def new_final_statement_message(**fields: Any) -> ledger.OutboundMessage:
    """A final statement e-mail (PLAN-012e D8): no step, a lease end, a document."""
    _default(fields, "operator", lambda: saved(new_operator()))
    _default(fields, "template", lambda: saved(new_message_template()))
    values = {
        "kind": "final_statement",
        "step": None,
        "charge_entity_type": "lease_end",
        "charge_entity_id": uuid.uuid4(),
        "to_address": "ops@example.com",
    }
    return ledger.OutboundMessage(**{**values, **fields})


LEDGER_FACTORIES: dict[type[models.Model], Callable[..., models.Model]] = {
    ledger.Operator: new_operator,
    ledger.Store: new_store,
    ledger.Lease: new_lease,
    ledger.LeaseRentHistory: new_rent_history,
    ledger.CollectionSchedule: new_schedule,
    ledger.RentCharge: new_rent_charge,
    ledger.InsurancePremium: new_premium,
    ledger.InsuranceCharge: new_insurance_charge,
    ledger.FeeType: new_fee_type,
    ledger.OneOffCharge: new_setup_charge,
    ledger.StoreCost: new_store_cost,
    ledger.DepositResolution: new_deposit_resolution,
    ledger.ChargeNote: new_charge_note,
    ledger.ChargeAdjustment: new_adjustment,
    ledger.StoreBalance: new_store_balance,
    ledger.Note: new_note,
    ledger.Invoice: new_invoice,
    ledger.InvoiceLineItem: new_line_item,
    ledger.InvoiceDraft: new_invoice_draft,
    ledger.InvoiceDraftLine: new_invoice_draft_line,
    ledger.Payment: new_payment,
    ledger.AuditLog: new_audit_log,
    ledger.LegacyRecord: new_legacy_record,
    ledger.ImportAudit: new_import_audit,
    ledger.Document: new_document,
    ledger.Dispute: new_dispute,
    ledger.DisputeEvidence: new_dispute_evidence,
    ledger.MessageTemplate: new_message_template,
    ledger.CollectionStep: new_collection_step,
    ledger.OutboundMessage: new_outbound_message,
    ledger.ContactEntry: new_contact_entry,
    ledger.ContactCorrection: new_contact_correction,
    ledger.CalendarEntry: new_calendar_entry,
    ledger.CalendarFeed: new_calendar_feed,
    ledger.LeaseEnd: new_lease_end,
    ledger.LeaseEndDeduction: new_lease_end_deduction,
    ledger.LeaseTransfer: new_lease_transfer,
    ledger.InstallmentPlan: new_installment_plan,
}


# --- Constraint cases ---------------------------------------------------------------


@dataclass(frozen=True)
class ConstraintCase:
    """``valid`` and ``invalid`` each return one unsaved row; setup rows are saved."""

    name: str
    valid: Callable[[], models.Model]
    invalid: Callable[[], models.Model]
    #: A field-level ``unique=True``: PostgreSQL names it itself, so match the column.
    column: str | None = None
    #: False when the invalid row cannot avoid breaking a second constraint too (the
    #: database then may name either); the constraint's presence is proven separately
    #: by ``test_every_constraint_exists_in_the_database``.
    exact: bool = True

    def __str__(self) -> str:
        return self.name


def insert(instance: models.Model) -> None:
    type(instance)._default_manager.bulk_create([instance])


def assert_accepted(case: ConstraintCase) -> None:
    instance = case.valid()
    insert(instance)
    assert type(instance)._default_manager.filter(pk=instance.pk).exists()


def assert_rejected(case: ConstraintCase) -> None:
    instance = case.invalid()
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        insert(instance)
    message = str(raised.value)
    if case.column is not None:
        assert case.column in message
    elif case.exact and (connection.vendor == "postgresql" or case.name not in unique_names()):
        # sqlite names a failed CHECK constraint, but a failed unique index by column.
        assert case.name in message


def unique_names() -> set[str]:
    from django.apps import apps

    return {
        constraint.name
        for model in apps.get_models()
        for constraint in model._meta.constraints
        if isinstance(constraint, models.UniqueConstraint)
    }


def unique_pair(
    factory: Callable[..., models.Model], first: dict[str, Any], second: dict[str, Any]
) -> Callable[[], models.Model]:
    """Save a row built with ``first``, then return an unsaved row built with ``second``.

    Values in ``second`` may be callables of the first row (to share its parent).
    """

    def build() -> models.Model:
        existing = factory(**first)
        insert(existing)
        return factory(
            **{key: value(existing) if callable(value) else value for key, value in second.items()}
        )

    return build


def _second_redebit(*, same_payment: bool) -> ledger.OneOffCharge:
    """Save a re-debit of one payment; return an unsaved re-debit of the same or another."""
    payment = saved(new_payment(status="returned"))
    insert(new_one_off_charge(category="redebit", description="Re-debit", source_payment=payment))
    other = payment if same_payment else saved(new_payment(status="returned"))
    return new_one_off_charge(category="redebit", description="Re-debit", source_payment=other)


def _second_fee(*, same_redebit: bool) -> ledger.OneOffCharge:
    """Save an NSF fee of one charge; return an unsaved fee of the same or another."""
    first = saved(new_one_off_charge())
    insert(new_one_off_charge(category="nsf_fee", description="NSF fee", companion_of=first))
    other = first if same_redebit else saved(new_one_off_charge())
    return new_one_off_charge(category="nsf_fee", description="NSF fee", companion_of=other)


def choice_cases(
    factories: dict[type[models.Model], Callable[..., models.Model]],
) -> list[ConstraintCase]:
    """``<table>_<field>_valid`` for every field with choices."""

    def with_bogus(factory: Callable[..., models.Model], name: str) -> Callable[[], models.Model]:
        return lambda: factory(**{name: "bogus"})

    return [
        ConstraintCase(
            f"{model._meta.db_table}_{field.name}_valid",
            factory,
            with_bogus(factory, field.attname),
        )
        for model, factory in factories.items()
        for field in model._meta.concrete_fields
        if field.choices
    ]


def declared_constraints(app_label: str) -> set[str]:
    """Every named constraint plus every field-level unique column, of one app."""
    from django.apps import apps

    names: set[str] = set()
    for model in apps.get_app_config(app_label).get_models():
        names |= {constraint.name for constraint in model._meta.constraints if constraint.name}
        for field in model._meta.concrete_fields:
            if field.unique and not field.primary_key:
                names.add(f"{model._meta.db_table}.{field.column}")
    return names


def _same_lease(row: Any) -> Any:
    return row.lease


def _same_store(row: Any) -> Any:
    return row.store


def _field_unique(
    table: str, column: str, factory: Callable[..., models.Model], value: Any, other: Any
) -> ConstraintCase:
    return ConstraintCase(
        f"{table}.{column}",
        unique_pair(factory, {column: value}, {column: other}),
        unique_pair(factory, {column: value}, {column: value}),
        column=column,
    )


_CARD = "4111 1111 1111 1111"
_UUID_1 = uuid.UUID("5f0c2d8e-9b1a-4c3e-8f7d-2a6b1c9e0d4f")
_UUID_2 = uuid.UUID("0b7e6a1c-3d2f-4e8a-9c5b-7d1e2f3a4b6c")


def _second_promise_closed_by_the_same_entry() -> ledger.ContactEntry:
    closing = saved(new_contact_entry())
    insert(new_promise(operator=closing.operator, closed_by=closing))
    return new_promise(operator=closing.operator, closed_by=closing)


def _second_transfer_of_one_end() -> ledger.LeaseTransfer:
    end = saved(new_lease_end())
    insert(new_lease_transfer(lease_end=end))
    return new_lease_transfer(lease_end=end)


def _same_lease_transfer() -> ledger.LeaseTransfer:
    lease = saved(new_lease())
    return new_lease_transfer(outgoing_lease=lease, incoming_lease=lease)


def _confirmed_transfer(**fields: Any) -> ledger.LeaseTransfer:
    values: dict[str, Any] = {
        "status": "confirmed",
        "confirmed_at": datetime(2026, 11, 1, 12, tzinfo=UTC),
        "lease_end": saved(new_lease_end()),
        "incoming_lease": saved(new_lease()),
    }
    return new_lease_transfer(**{**values, **fields})


def _second_store_transfer(*, confirmed_first: bool) -> Callable[[], ledger.LeaseTransfer]:
    """Save a transfer of a store (confirmed or a draft); return a second draft of it."""

    def build() -> ledger.LeaseTransfer:
        first = _confirmed_transfer() if confirmed_first else new_lease_transfer()
        insert(first)
        return new_lease_transfer(store=first.store, outgoing_lease=first.outgoing_lease)

    return build


def _two_charges(**second: Any) -> tuple[ledger.RentCharge, ledger.RentCharge]:
    first = saved(new_rent_charge())
    other_lease = saved(new_lease(**second))
    return first, saved(new_rent_charge(lease=other_lease))


LEDGER_CASES: list[ConstraintCase] = [
    # operators
    ConstraintCase(
        "operators_display_name_not_blank",
        new_operator,
        lambda: new_operator(display_name="  \t "),
    ),
    ConstraintCase(
        "operators_email_normalised",
        lambda: new_operator(email="ops@example.com"),
        lambda: new_operator(email="Ops@Example.com "),
    ),
    ConstraintCase(
        "operators_email_not_empty",
        lambda: new_operator(email=None),
        lambda: new_operator(email=""),
    ),
    ConstraintCase(
        "operators_email_unique",
        unique_pair(new_operator, {"email": "a@example.com"}, {"email": None}),
        unique_pair(new_operator, {"email": "a@example.com"}, {"email": "a@example.com"}),
    ),
    # stores
    ConstraintCase(
        "stores_needs_open_date_matches",
        lambda: new_store(open_date=None, needs_open_date=True),
        lambda: new_store(open_date=None, needs_open_date=False),
    ),
    # leases
    ConstraintCase(
        "leases_debit_day_range", lambda: new_lease(debit_day=31), lambda: new_lease(debit_day=32)
    ),
    ConstraintCase(
        "leases_second_debit_day_range",
        lambda: new_lease(debit_day=1, second_debit_day=15),
        lambda: new_lease(debit_day=1, second_debit_day=0),
    ),
    ConstraintCase(
        "leases_second_debit_day_valid",
        lambda: new_lease(debit_day=1, second_debit_day=15),
        lambda: new_lease(debit_day=15, second_debit_day=15),
    ),
    ConstraintCase(
        "leases_active_channel_known",
        lambda: new_lease(collection_status="paused", payment_channel="unknown"),
        lambda: new_lease(collection_status="active", payment_channel="unknown"),
    ),
    ConstraintCase(
        "leases_active_has_day_and_operator",
        lambda: new_lease(collection_status="prospective", operator=None, debit_day=None),
        lambda: new_lease(collection_status="active", operator=None),
    ),
    ConstraintCase(
        "leases_ended_after_started",
        lambda: new_lease(started_on=date(2024, 1, 1), ended_on=date(2024, 1, 1)),
        lambda: new_lease(started_on=date(2024, 1, 2), ended_on=date(2024, 1, 1)),
    ),
    # lease_rent_history
    ConstraintCase(
        "lease_rent_history_amount_positive",
        lambda: new_rent_history(amount=D("0.01")),
        lambda: new_rent_history(amount=D("0.00")),
    ),
    ConstraintCase(
        "lease_rent_history_effective_order",
        lambda: new_rent_history(effective_from=date(2025, 1, 1), effective_to=date(2025, 1, 2)),
        lambda: new_rent_history(effective_from=date(2025, 1, 1), effective_to=date(2025, 1, 1)),
    ),
    ConstraintCase(
        "lease_rent_history_lease_from_unique",
        unique_pair(
            new_rent_history,
            {"effective_to": date(2026, 1, 1), "effective_from": date(2025, 1, 1)},
            {"lease": _same_lease, "effective_from": date(2026, 1, 1)},
        ),
        unique_pair(
            new_rent_history,
            {"effective_to": date(2026, 1, 1), "effective_from": date(2025, 1, 1)},
            {
                "lease": _same_lease,
                "effective_from": date(2025, 1, 1),
                "effective_to": date(2025, 6, 1),
            },
        ),
    ),
    ConstraintCase(
        "lease_rent_history_one_open_row",
        unique_pair(
            new_rent_history,
            {"effective_from": date(2025, 1, 1), "effective_to": date(2026, 1, 1)},
            {"lease": _same_lease, "effective_from": date(2026, 1, 1)},
        ),
        unique_pair(
            new_rent_history,
            {"effective_from": date(2025, 1, 1)},
            {"lease": _same_lease, "effective_from": date(2026, 1, 1)},
        ),
    ),
    # collection_schedules
    ConstraintCase(
        "collection_schedules_seq_valid",
        lambda: new_schedule(seq=2),
        lambda: new_schedule(seq=3),
    ),
    ConstraintCase(
        "collection_schedules_amount_positive",
        lambda: new_schedule(amount=D("0.01")),
        lambda: new_schedule(amount=D("-1.00")),
    ),
    ConstraintCase(
        "collection_schedules_debit_day_range",
        lambda: new_schedule(debit_day=1),
        lambda: new_schedule(debit_day=40),
    ),
    ConstraintCase(
        "collection_schedules_live_unique",
        unique_pair(new_schedule, {"state": "ended"}, {"lease": _same_lease, "state": "active"}),
        unique_pair(new_schedule, {"state": "paused"}, {"lease": _same_lease, "state": "active"}),
    ),
    # rent_charges
    ConstraintCase(
        "rent_charges_period_unique",
        unique_pair(new_rent_charge, {}, {"lease": _same_lease, "seq": 2}),
        unique_pair(new_rent_charge, {}, {"lease": _same_lease, "seq": 1}),
    ),
    ConstraintCase(
        "rent_charges_period_first_of_month",
        lambda: new_rent_charge(period_month=date(2026, 2, 1)),
        lambda: new_rent_charge(period_month=date(2026, 2, 2)),
    ),
    ConstraintCase(
        "rent_charges_seq_valid",
        lambda: new_rent_charge(seq=2),
        lambda: new_rent_charge(seq=0),
    ),
    ConstraintCase(
        "rent_charges_amount_due_not_negative",
        lambda: new_rent_charge(amount_due=D("0.00")),
        lambda: new_rent_charge(amount_due=D("-0.01")),
    ),
    ConstraintCase(
        "rent_charges_amount_paid_not_negative",
        lambda: new_rent_charge(amount_paid=D("0.00")),
        lambda: new_rent_charge(amount_paid=D("-0.01")),
    ),
    _field_unique("rent_charges", "local_ref", new_rent_charge, _UUID_1, _UUID_2),
    # insurance_premiums
    ConstraintCase(
        "insurance_premiums_policy_year_range",
        lambda: new_premium(policy_year=2100),
        lambda: new_premium(policy_year=2101),
    ),
    ConstraintCase(
        "insurance_premiums_amount_positive",
        lambda: new_premium(amount=D("0.01")),
        lambda: new_premium(amount=D("0.00")),
    ),
    _field_unique("insurance_premiums", "policy_year", new_premium, 2025, 2026),
    ConstraintCase(
        "insurance_premiums_cost_not_negative",
        lambda: new_premium(cost=D("0.00")),
        lambda: new_premium(cost=D("-0.01")),
    ),
    # fee_types (PLAN-010b D1)
    ConstraintCase("fee_types_name_not_blank", new_fee_type, lambda: new_fee_type(name=" ")),
    ConstraintCase(
        "fee_types_default_cost_not_negative",
        lambda: new_fee_type(default_cost=D("0.00")),
        lambda: new_fee_type(default_cost=D("-0.01")),
    ),
    ConstraintCase(
        "fee_types_default_price_positive",
        lambda: new_fee_type(default_price=D("0.01")),
        lambda: new_fee_type(default_price=D("0.00")),
    ),
    _field_unique("fee_types", "code", new_fee_type, "test_code_a", "test_code_b"),
    # one_off_charges (PLAN-010b D3, PLAN-010d D1)
    ConstraintCase(
        "one_off_charges_price_positive",
        lambda: new_setup_charge(price=D("0.01")),
        lambda: new_setup_charge(price=D("0.00")),
    ),
    ConstraintCase(
        "one_off_charges_cost_not_negative",
        lambda: new_setup_charge(cost=D("0.00")),
        lambda: new_setup_charge(cost=D("-0.01")),
    ),
    ConstraintCase(
        "one_off_charges_amount_paid_not_negative",
        lambda: new_setup_charge(amount_paid=D("0.00")),
        lambda: new_setup_charge(amount_paid=D("-0.01")),
    ),
    ConstraintCase(
        "one_off_charges_waived_has_note",
        lambda: new_setup_charge(
            status="waived",
            waived_at=datetime(2026, 10, 1, tzinfo=UTC),
            waive_note="Owner waived the sign fee.",
        ),
        lambda: new_setup_charge(
            status="waived", waived_at=datetime(2026, 10, 1, tzinfo=UTC), waive_note=" "
        ),
    ),
    ConstraintCase(
        # Partial: a legacy balance may sit beside a real charge of the same fee type.
        "one_off_charges_lease_fee_type_unique",
        unique_pair(
            new_setup_charge,
            {},
            {"lease": _same_lease, "fee_type": lambda row: row.fee_type, "legacy": True},
        ),
        unique_pair(
            new_setup_charge, {}, {"lease": _same_lease, "fee_type": lambda row: row.fee_type}
        ),
    ),
    # PLAN-010d D1: categories, collections, the void and the Charge now key.
    ConstraintCase(
        "one_off_charges_fee_type_iff_setup",
        new_one_off_charge,
        lambda: new_one_off_charge(fee_type=saved(new_fee_type())),
    ),
    ConstraintCase(
        "one_off_charges_fee_type_iff_setup",
        new_setup_charge,
        lambda: new_setup_charge(fee_type=None),
    ),
    ConstraintCase(
        "one_off_charges_description_required",
        lambda: new_setup_charge(description=""),
        lambda: new_one_off_charge(description=" "),
    ),
    ConstraintCase(
        "one_off_charges_collection_none_only_setup",
        lambda: new_setup_charge(collection="none"),
        lambda: new_one_off_charge(collection="none"),
    ),
    ConstraintCase(
        "one_off_charges_has_operator",
        lambda: new_setup_charge(operator=None),
        lambda: new_one_off_charge(operator=None, lease=saved(new_lease())),
    ),
    ConstraintCase(
        "one_off_charges_voided_has_note",
        lambda: new_one_off_charge(
            status="voided",
            voided_at=datetime(2026, 10, 5, tzinfo=UTC),
            void_note="Charged in error.",
        ),
        lambda: new_one_off_charge(
            status="voided", voided_at=datetime(2026, 10, 5, tzinfo=UTC), void_note=" "
        ),
    ),
    ConstraintCase(
        "one_off_charges_voided_has_note",
        new_one_off_charge,
        lambda: new_one_off_charge(status="owed", voided_at=datetime(2026, 10, 5, tzinfo=UTC)),
    ),
    _field_unique(
        "one_off_charges", "charge_now_key", new_one_off_charge, uuid.uuid4(), uuid.uuid4()
    ),
    # store_costs (PLAN-010b D8)
    ConstraintCase(
        "store_costs_landlord_rent_not_negative",
        lambda: new_store_cost(landlord_rent=D("0.00")),
        lambda: new_store_cost(landlord_rent=D("-0.01")),
    ),
    ConstraintCase(
        "store_costs_other_monthly_cost_not_negative",
        lambda: new_store_cost(other_monthly_cost=D("0.00")),
        lambda: new_store_cost(other_monthly_cost=D("-0.01")),
    ),
    ConstraintCase(
        # A one-day row is allowed (effective_to is inclusive); a row ending before it
        # starts is not.
        "store_costs_effective_order",
        lambda: new_store_cost(effective_from=date(2025, 1, 1), effective_to=date(2025, 1, 1)),
        lambda: new_store_cost(effective_from=date(2025, 1, 2), effective_to=date(2025, 1, 1)),
    ),
    ConstraintCase(
        "store_costs_store_from_unique",
        unique_pair(
            new_store_cost,
            {"effective_from": date(2025, 1, 1), "effective_to": date(2025, 12, 31)},
            {"store": lambda row: row.store, "effective_from": date(2026, 1, 1)},
        ),
        unique_pair(
            new_store_cost,
            {"effective_from": date(2025, 1, 1), "effective_to": date(2025, 12, 31)},
            {
                "store": lambda row: row.store,
                "effective_from": date(2025, 1, 1),
                "effective_to": date(2025, 6, 1),
            },
        ),
    ),
    ConstraintCase(
        "store_costs_one_open_row",
        unique_pair(
            new_store_cost,
            {"effective_from": date(2025, 1, 1), "effective_to": date(2025, 12, 31)},
            {"store": lambda row: row.store, "effective_from": date(2026, 1, 1)},
        ),
        unique_pair(
            new_store_cost,
            {"effective_from": date(2025, 1, 1)},
            {"store": lambda row: row.store, "effective_from": date(2026, 1, 1)},
        ),
    ),
    # store_costs corrections (PLAN-010c D10)
    ConstraintCase(
        "store_costs_superseded_complete",
        lambda: _superseded_store_cost(change_note="Wrong amount."),
        lambda: _superseded_store_cost(change_note=" "),
    ),
    ConstraintCase(
        "store_costs_not_superseded_by_itself",
        lambda: _superseded_store_cost(change_note="Wrong amount."),
        _self_superseded_store_cost,
    ),
    # deposit_resolutions (PLAN-010b D7)
    ConstraintCase(
        "deposit_resolutions_amount_positive",
        lambda: new_deposit_resolution(amount=D("0.01")),
        lambda: new_deposit_resolution(amount=D("0.00")),
    ),
    ConstraintCase(
        "deposit_resolutions_withheld_amount_not_negative",
        lambda: new_deposit_resolution(withheld_amount=D("0.00")),
        lambda: new_deposit_resolution(withheld_amount=D("-0.01")),
    ),
    ConstraintCase(
        "deposit_resolutions_note_not_blank",
        new_deposit_resolution,
        lambda: new_deposit_resolution(note=" \n "),
    ),
    ConstraintCase(
        "deposit_resolutions_method_iff_refunded",
        lambda: new_deposit_resolution(kind="refunded", method="zelle"),
        lambda: new_deposit_resolution(kind="refunded", method=""),
    ),
    ConstraintCase(
        "deposit_resolutions_to_lease_iff_transferred",
        lambda: new_deposit_resolution(kind="transferred", method="", to_lease=saved(new_lease())),
        lambda: new_deposit_resolution(kind="transferred", method="", to_lease=None),
    ),
    # PLAN-012e D5(d): the offset part lies within the withheld part.
    ConstraintCase(
        "deposit_resolutions_offset_within_withheld",
        lambda: new_deposit_resolution(withheld_amount=D("100.00"), offset_amount=D("100.00")),
        lambda: new_deposit_resolution(withheld_amount=D("100.00"), offset_amount=D("100.01")),
    ),
    ConstraintCase(
        "deposit_resolutions.setup_charge_id",
        unique_pair(new_deposit_resolution, {}, {}),
        unique_pair(new_deposit_resolution, {}, {"setup_charge": lambda row: row.setup_charge}),
        column="setup_charge_id",
    ),
    # insurance_charges
    ConstraintCase(
        "insurance_charges_lease_year_unique",
        unique_pair(new_insurance_charge, {}, {"lease": _same_lease, "policy_year": 2027}),
        unique_pair(new_insurance_charge, {}, {"lease": _same_lease}),
    ),
    ConstraintCase(
        "insurance_charges_needs_premium_matches",
        lambda: new_insurance_charge(amount=None, needs_premium=True),
        lambda: new_insurance_charge(amount=None, needs_premium=False),
    ),
    ConstraintCase(
        "insurance_charges_amount_positive",
        lambda: new_insurance_charge(amount=D("0.01")),
        lambda: new_insurance_charge(amount=D("0.00")),
    ),
    # charge_notes
    ConstraintCase(
        "charge_notes_exactly_one_charge",
        lambda: new_charge_note(rent_charge=None, insurance_charge=saved(new_insurance_charge())),
        lambda: new_charge_note(
            rent_charge=saved(new_rent_charge()), insurance_charge=saved(new_insurance_charge())
        ),
    ),
    ConstraintCase(
        "charge_notes_body_not_blank",
        new_charge_note,
        lambda: new_charge_note(body="\n  "),
    ),
    # charge_adjustments
    ConstraintCase(
        "charge_adjustments_amount_positive",
        lambda: new_adjustment(amount=D("0.01")),
        lambda: new_adjustment(amount=D("0.00")),
    ),
    ConstraintCase(
        "charge_adjustments_note_not_blank",
        new_adjustment,
        lambda: new_adjustment(note=" "),
    ),
    ConstraintCase(
        "charge_adjustments_distinct_charges",
        lambda: new_adjustment(from_charge=saved(new_rent_charge())),
        lambda: (lambda charge: new_adjustment(from_charge=charge, to_charge=charge))(
            saved(new_rent_charge())
        ),
    ),
    ConstraintCase(
        "charge_adjustments_cross_store_has_source",
        lambda: new_adjustment(reason="cross_store_balance", from_charge=saved(new_rent_charge())),
        lambda: new_adjustment(reason="cross_store_balance", from_charge=None),
    ),
    # store_balances
    ConstraintCase(
        "store_balances_kind_unique",
        unique_pair(
            new_store_balance, {}, {"store": lambda row: row.store, "kind": "deposit_or_access"}
        ),
        unique_pair(new_store_balance, {}, {"store": lambda row: row.store}),
    ),
    # notes
    ConstraintCase("notes_body_not_blank", new_note, lambda: new_note(body="")),
    # invoices
    ConstraintCase(
        "invoices_number_format",
        lambda: new_invoice(invoice_number="INV-2026-12345"),
        lambda: new_invoice(invoice_number="INV-26-0001"),
    ),
    ConstraintCase(
        "invoices_at_most_one_source",
        lambda: new_invoice(rent_charge=saved(new_rent_charge())),
        lambda: new_invoice(
            rent_charge=saved(new_rent_charge()), insurance_charge=saved(new_insurance_charge())
        ),
    ),
    ConstraintCase(
        # Partial: any number of invoices without a rent charge; one per charge.
        "invoices_one_per_rent_charge",
        unique_pair(new_invoice, {}, {}),
        lambda: unique_pair(
            new_invoice,
            {"rent_charge": saved(new_rent_charge())},
            {"rent_charge": lambda row: row.rent_charge},
        )(),
    ),
    ConstraintCase(
        # Partial: any number of invoices without a one-off charge; one per charge.
        "invoices_one_per_one_off_charge",
        unique_pair(new_invoice, {}, {}),
        lambda: unique_pair(
            new_invoice,
            {"one_off_charge": saved(new_setup_charge())},
            {"one_off_charge": lambda row: row.one_off_charge},
        )(),
    ),
    ConstraintCase(
        # PLAN-012a D2: total = subtotal + tax + fees + surcharge.
        "invoices_total_is_sum_of_parts",
        lambda: new_invoice(
            subtotal=D("0.10"), tax=D("0.20"), fees=D("0.40"), surcharge=D("0.80"), total=D("1.50")
        ),
        lambda: new_invoice(subtotal=D("0.10"), tax=D("0.20"), total=D("0.31")),
    ),
    ConstraintCase(
        "invoices_fees_not_negative",
        lambda: new_invoice(subtotal=D("1.00"), fees=D("0.00"), total=D("1.00")),
        lambda: new_invoice(subtotal=D("2.00"), fees=D("-1.00"), total=D("1.00")),
    ),
    ConstraintCase(
        "invoices_surcharge_not_negative",
        lambda: new_invoice(subtotal=D("1.00"), surcharge=D("0.00"), total=D("1.00")),
        lambda: new_invoice(subtotal=D("2.00"), surcharge=D("-1.00"), total=D("1.00")),
    ),
    ConstraintCase(
        "invoices_subtotal_not_negative",
        lambda: new_invoice(subtotal=D("0.00"), total=D("0.00")),
        lambda: new_invoice(subtotal=D("-1.00"), tax=D("1.00"), total=D("0.00")),
    ),
    ConstraintCase(
        "invoices_tax_not_negative",
        lambda: new_invoice(subtotal=D("1.00"), tax=D("0.00"), total=D("1.00")),
        lambda: new_invoice(subtotal=D("2.00"), tax=D("-1.00"), total=D("1.00")),
    ),
    # total = subtotal + tax, so a negative total always comes with a negative part.
    ConstraintCase(
        "invoices_total_not_negative",
        lambda: new_invoice(subtotal=D("0.00"), tax=D("0.00"), total=D("0.00")),
        lambda: new_invoice(subtotal=D("0.00"), tax=D("-0.01"), total=D("-0.01")),
        exact=False,
    ),
    ConstraintCase(
        "invoices_amount_paid_not_negative",
        lambda: new_invoice(amount_paid=D("0.00")),
        lambda: new_invoice(amount_paid=D("-5.00")),
    ),
    ConstraintCase(
        "invoices_due_after_issue",
        lambda: new_invoice(issue_date=date(2026, 9, 5), due_date=date(2026, 9, 5)),
        lambda: new_invoice(issue_date=date(2026, 9, 5), due_date=date(2026, 9, 4)),
    ),
    ConstraintCase(
        "invoices_no_card",
        lambda: new_invoice(allow_card=False),
        lambda: new_invoice(allow_card=True),
    ),
    _field_unique("invoices", "invoice_number", new_invoice, "INV-2026-9001", "INV-2026-9002"),
    _field_unique("invoices", "creation_key", new_invoice, _UUID_1, _UUID_2),
    # operators (PLAN-006 D6)
    _field_unique("operators", "creation_key", new_operator, _UUID_1, _UUID_2),
    # operators (PLAN-012a D3): empty or E.164.
    ConstraintCase(
        "operators_sms_phone_e164",
        lambda: new_operator(sms_phone="+14045551234"),
        lambda: new_operator(sms_phone="404-555-1234"),
    ),
    # invoice_line_items
    ConstraintCase(
        "invoice_line_items_position_unique",
        unique_pair(new_line_item, {}, {"invoice": lambda row: row.invoice, "position": 2}),
        unique_pair(new_line_item, {}, {"invoice": lambda row: row.invoice}),
    ),
    ConstraintCase(
        "invoice_line_items_position_positive",
        lambda: new_line_item(position=1),
        lambda: new_line_item(position=0),
    ),
    ConstraintCase(
        "invoice_line_items_quantity_positive",
        lambda: new_line_item(quantity=D("0.0001")),
        lambda: new_line_item(quantity=D("0.0000")),
    ),
    ConstraintCase(
        "invoice_line_items_one_per_one_off_charge",
        unique_pair(_line_of_a_charge, {}, {}),
        unique_pair(_line_of_a_charge, {}, {"one_off_charge": lambda row: row.one_off_charge}),
    ),
    ConstraintCase(
        # PLAN-012a D1: the closed list.
        "invoice_line_items_kind_valid",
        lambda: new_line_item(kind="discount"),
        lambda: new_line_item(kind="credit"),
    ),
    # invoice_drafts and invoice_draft_lines (PLAN-012 D6)
    ConstraintCase(
        "invoice_drafts_status_valid",
        lambda: new_invoice_draft(status="cancelled"),
        lambda: new_invoice_draft(status="posted"),
    ),
    ConstraintCase(
        "invoice_drafts_sent_iff_invoice",
        _sent_draft,
        lambda: new_invoice_draft(status="sent"),
    ),
    ConstraintCase(
        "invoice_drafts_one_per_invoice",
        unique_pair(_sent_draft, {}, {}),
        unique_pair(_sent_draft, {}, {"sent_invoice": lambda row: row.sent_invoice}),
    ),
    ConstraintCase(
        "invoice_draft_lines_position_unique",
        unique_pair(new_invoice_draft_line, {}, {"draft": lambda row: row.draft, "position": 2}),
        unique_pair(new_invoice_draft_line, {}, {"draft": lambda row: row.draft}),
    ),
    ConstraintCase(
        "invoice_draft_lines_position_positive",
        lambda: new_invoice_draft_line(position=1),
        lambda: new_invoice_draft_line(position=0),
    ),
    ConstraintCase(
        # PLAN-012b D1: the item is the line's text; the description is an optional detail.
        "invoice_draft_lines_item_not_blank",
        new_invoice_draft_line,
        lambda: new_invoice_draft_line(item=" \n"),
    ),
    ConstraintCase(
        "invoice_draft_lines_quantity_positive",
        lambda: new_invoice_draft_line(
            quantity=D("0.0001"), unit_amount=D("100.00"), line_total=D("0.01")
        ),
        lambda: new_invoice_draft_line(quantity=D("0.0000"), line_total=D("0.01")),
        # The total no longer matches the quantity times the unit amount either.
        exact=False,
    ),
    ConstraintCase(
        "invoice_draft_lines_unit_amount_positive",
        lambda: new_invoice_draft_line(
            quantity=D("1"), unit_amount=D("0.01"), line_total=D("0.01")
        ),
        lambda: new_invoice_draft_line(
            quantity=D("1"), unit_amount=D("0.00"), line_total=D("0.01")
        ),
        exact=False,
    ),
    ConstraintCase(
        "invoice_draft_lines_line_total_positive",
        lambda: new_invoice_draft_line(
            quantity=D("0.5000"), unit_amount=D("0.01"), line_total=D("0.01")
        ),
        # 0.0001 times 0.01 rounds to zero: within half a cent of the product, not positive.
        lambda: new_invoice_draft_line(
            quantity=D("0.0001"), unit_amount=D("0.01"), line_total=D("0.00")
        ),
    ),
    ConstraintCase(
        "invoice_draft_lines_total_is_quantity_times_unit",
        # 0.125 rounds half up to 0.13: half a cent away is accepted.
        lambda: new_invoice_draft_line(
            quantity=D("0.5000"), unit_amount=D("0.25"), line_total=D("0.13")
        ),
        lambda: new_invoice_draft_line(
            quantity=D("3.0000"), unit_amount=D("33.33"), line_total=D("100.00")
        ),
    ),
    # payments
    ConstraintCase(
        "payments_amount_positive",
        lambda: new_payment(amount=D("0.01")),
        lambda: new_payment(amount=D("0.00")),
    ),
    ConstraintCase(
        "payments_refund_has_parent",
        lambda: new_payment(is_refund=True, parent_payment=saved(new_payment())),
        lambda: new_payment(is_refund=True, parent_payment=None),
    ),
    ConstraintCase(
        "payments_policy_year_iff_insurance",
        lambda: new_payment(kind="insurance", policy_year=2026),
        lambda: new_payment(kind="rent", policy_year=2026),
    ),
    ConstraintCase(
        "payments_manual_method_has_note",
        lambda: new_payment(method="zelle", note="Zelle received on 2026-09-05."),
        lambda: new_payment(method="wire", note="   "),
    ),
    ConstraintCase(
        "payments_one_off_kind_iff_one_off_charge",
        lambda: new_payment(kind="one_off", one_off_charge=saved(new_setup_charge())),
        lambda: new_payment(kind="one_off", one_off_charge=None),
    ),
    ConstraintCase(
        "payments_one_off_charge_only_target",
        lambda: new_payment(kind="one_off", one_off_charge=saved(new_setup_charge())),
        lambda: new_payment(
            kind="one_off",
            one_off_charge=saved(new_setup_charge()),
            rent_charge=saved(new_rent_charge()),
        ),
    ),
    ConstraintCase(
        "contact_entries_promise_fields",
        new_contact_entry,
        lambda: new_contact_entry(promised_amount=D("100.00"), promised_date=date(2026, 10, 9)),
    ),
    ConstraintCase(
        "contact_entries_promise_fields",
        new_promise,
        lambda: new_promise(promised_amount=None),
    ),
    ConstraintCase(
        "payments_refund_deposit_manual",
        lambda: new_payment(
            kind="refund_deposit",
            method="wire",
            note="Deposit returned by wire.",
            one_off_charge=saved(new_setup_charge()),
        ),
        lambda: new_payment(
            kind="refund_deposit", method="ach", one_off_charge=saved(new_setup_charge())
        ),
    ),
    ConstraintCase(
        "payments_resolved_return_has_note",
        lambda: new_payment(
            method="ach",
            status="returned",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Operator paid by wire.",
            resolution_kind="note",
        ),
        lambda: new_payment(
            method="ach",
            status="settled",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Not a return.",
            resolution_kind="note",
        ),
    ),
    # documents (PLAN-010c D4)
    ConstraintCase(
        "documents_exactly_one_owner",
        lambda: new_document(store=None, lease=saved(new_lease())),
        lambda: new_document(store=saved(new_store()), lease=saved(new_lease())),
    ),
    # PLAN-012 D8: an operator keeps dispute evidence only, and only an operator does.
    ConstraintCase(
        "documents_operator_keeps_dispute_evidence",
        new_operator_document,
        lambda: new_document(kind="dispute_evidence"),
    ),
    # PLAN-012e D5: a final statement is kept by a store only.
    ConstraintCase(
        "documents_final_statement_on_store",
        lambda: new_document(kind="final_statement"),
        lambda: new_document(store=None, lease=saved(new_lease()), kind="final_statement"),
    ),
    ConstraintCase(
        "documents_operator_sha256_live_unique",
        unique_pair(new_operator_document, {"sha256": "c" * 64}, {"sha256": "c" * 64}),
        unique_pair(
            new_operator_document,
            {"sha256": "c" * 64},
            {"sha256": "c" * 64, "operator": lambda row: row.operator},
        ),
    ),
    # dispute_evidence (PLAN-012 D8)
    ConstraintCase(
        "dispute_evidence_status_valid",
        lambda: new_dispute_evidence(status="failed", error="ProviderRejected"),
        lambda: new_dispute_evidence(status="lost"),
    ),
    ConstraintCase(
        "dispute_evidence_name_not_blank",
        new_dispute_evidence,
        lambda: new_dispute_evidence(name=" "),
    ),
    ConstraintCase(
        "dispute_evidence_sent_has_time",
        lambda: new_dispute_evidence(status="sent", sent_at=datetime(2026, 10, 6, tzinfo=UTC)),
        lambda: new_dispute_evidence(status="sent"),
    ),
    ConstraintCase(
        "documents_size_bytes_positive",
        lambda: new_document(size_bytes=1),
        lambda: new_document(size_bytes=0),
    ),
    ConstraintCase(
        "documents_original_filename_not_blank",
        new_document,
        lambda: new_document(original_filename="  "),
    ),
    ConstraintCase(
        "documents_deleted_has_note",
        lambda: new_document(
            deleted_at=datetime(2026, 10, 5, tzinfo=UTC), delete_note="Uploaded twice."
        ),
        lambda: new_document(deleted_at=datetime(2026, 10, 5, tzinfo=UTC), delete_note=" "),
    ),
    ConstraintCase(
        "documents_store_sha256_live_unique",
        # The same file on another store: allowed.
        unique_pair(new_document, {"sha256": "a" * 64}, {"sha256": "a" * 64}),
        unique_pair(new_document, {"sha256": "a" * 64}, {"sha256": "a" * 64, "store": _same_store}),
    ),
    ConstraintCase(
        "documents_lease_sha256_live_unique",
        unique_pair(new_lease_document, {"sha256": "b" * 64}, {"sha256": "b" * 64}),
        unique_pair(
            new_lease_document, {"sha256": "b" * 64}, {"sha256": "b" * 64, "lease": _same_lease}
        ),
    ),
    _field_unique("documents", "blob_path", new_document, "documents/a", "documents/b"),
    ConstraintCase(
        "documents_sha256_hex",
        lambda: new_document(sha256="0123456789abcdef" * 4),
        lambda: new_document(sha256="0123456789ABCDEF" * 4),
    ),
    # contact_entries (PLAN-011b D7)
    ConstraintCase(
        "contact_entries_body_not_blank",
        new_contact_entry,
        lambda: new_contact_entry(body=" \n\t"),
    ),
    ConstraintCase(
        "contact_entries_promise_fields",
        new_promise,
        lambda: new_promise(promised_date=None),
    ),
    ConstraintCase(
        "contact_entries_promised_amount_positive",
        new_promise,
        lambda: new_promise(promised_amount=D("0.00")),
    ),
    ConstraintCase(
        "contact_entries_closed_by_promise_only",
        lambda: new_promise(closed_by=saved(new_contact_entry())),
        lambda: new_contact_entry(closed_by=saved(new_contact_entry())),
    ),
    ConstraintCase(
        "contact_entries.closed_by_id",
        unique_pair(
            new_promise,
            {"closed_by": None},
            {"closed_by": lambda row: saved(new_contact_entry(operator=row.operator))},
        ),
        lambda: _second_promise_closed_by_the_same_entry(),
        column="closed_by_id",
    ),
    # contact_entries and contact_corrections (PLAN-012 D2, D4, D5)
    ConstraintCase(
        "contact_entries_status_promise_only",
        lambda: new_promise(status="broken"),
        lambda: new_contact_entry(status="broken"),
    ),
    ConstraintCase(
        "contact_entries_kept_by_payment_kept",
        lambda: new_promise(status="kept", kept_by_payment=saved(new_payment())),
        lambda: new_promise(status="open", kept_by_payment=saved(new_payment())),
    ),
    ConstraintCase(
        "contact_entries_deleted_all_or_none",
        lambda: new_contact_entry(
            deleted_at=datetime(2026, 10, 7, tzinfo=UTC),
            deleted_by=make_user(),
            delete_reason="Wrong operator.",
        ),
        lambda: new_contact_entry(
            deleted_at=datetime(2026, 10, 7, tzinfo=UTC), delete_reason="Wrong operator."
        ),
    ),
    ConstraintCase(
        "contact_corrections_body_not_blank",
        new_contact_correction,
        lambda: new_contact_correction(body=" \n"),
    ),
    # calendar_entries (PLAN-011b D8)
    ConstraintCase(
        "calendar_entries_title_not_blank",
        new_calendar_entry,
        lambda: new_calendar_entry(title="   "),
    ),
    # calendar_feeds (PLAN-011b D8)
    ConstraintCase(
        "calendar_feeds_token_hash_size",
        lambda: new_calendar_feed(token_hash=bytes(32)),
        lambda: new_calendar_feed(token_hash=bytes(31)),
    ),
    ConstraintCase(
        "calendar_feeds_one_live_per_user",
        unique_pair(
            new_calendar_feed,
            {"revoked_at": datetime(2026, 10, 1, tzinfo=UTC)},
            {"user": lambda row: row.user},
        ),
        unique_pair(new_calendar_feed, {}, {"user": lambda row: row.user}),
    ),
    _field_unique("calendar_feeds", "token_hash", new_calendar_feed, b"\x01" * 32, b"\x02" * 32),
    # message_templates, collection_steps, outbound_messages (PLAN-011b D4)
    ConstraintCase(
        "message_templates_subject_not_blank",
        new_message_template,
        lambda: new_message_template(subject=" "),
    ),
    ConstraintCase(
        "message_templates_body_not_blank",
        new_message_template,
        lambda: new_message_template(body="\n"),
    ),
    ConstraintCase(
        "message_templates_code_shape",
        lambda: new_message_template(code="test_code_ok"),
        lambda: new_message_template(code="Test-Code"),
    ),
    _field_unique("message_templates", "code", new_message_template, "test_one", "test_two"),
    ConstraintCase(
        "collection_steps_day_offset_range",
        lambda: new_collection_step(day_offset=365),
        lambda: new_collection_step(day_offset=366),
    ),
    ConstraintCase(
        "collection_steps_day_unique",
        unique_pair(new_collection_step, {"day_offset": 300}, {"day_offset": 301}),
        unique_pair(new_collection_step, {"day_offset": 300}, {"day_offset": 300}),
    ),
    ConstraintCase(
        "outbound_messages_body_sha256_hex",
        lambda: new_outbound_message(body_sha256="0123456789abcdef" * 4),
        lambda: new_outbound_message(body_sha256="0123456789ABCDEF" * 4),
    ),
    ConstraintCase(
        "outbound_messages_error_class_name",
        lambda: new_outbound_message(error="ReadTimeout"),
        lambda: new_outbound_message(error="Read timeout"),
    ),
    ConstraintCase(
        "outbound_messages_mail_message_id_shape",
        lambda: new_outbound_message(mail_message_id="18f0a1b2_c-D"),
        lambda: new_outbound_message(mail_message_id="18f0 a1b2"),
    ),
    ConstraintCase(
        "outbound_messages_manual_has_sender",
        lambda: new_outbound_message(manual=True, sent_by=make_user()),
        lambda: new_outbound_message(manual=True, sent_by=None),
    ),
    ConstraintCase(
        "outbound_messages_one_per_charge_step",
        unique_pair(
            new_outbound_message,
            {},
            {
                "step": lambda row: row.step,
                "charge_entity_id": lambda row: row.charge_entity_id,
                "manual": True,
                "sent_by": lambda row: make_user(),
            },
        ),
        unique_pair(
            new_outbound_message,
            {},
            {"step": lambda row: row.step, "charge_entity_id": lambda row: row.charge_entity_id},
        ),
    ),
    # PLAN-012e D8: kinds of outbound messages.
    ConstraintCase(
        "outbound_messages_step_iff_ladder",
        new_final_statement_message,
        lambda: new_final_statement_message(kind="ladder"),
    ),
    ConstraintCase(
        "outbound_messages_document_only_final_statement",
        lambda: new_final_statement_message(document=saved(new_document())),
        lambda: new_outbound_message(document=saved(new_document())),
    ),
    ConstraintCase(
        "outbound_messages_one_final_statement",
        unique_pair(
            new_final_statement_message,
            {},
            {
                "charge_entity_id": lambda row: row.charge_entity_id,
                "manual": True,
                "sent_by": lambda row: make_user(),
            },
        ),
        unique_pair(
            new_final_statement_message,
            {},
            {"charge_entity_id": lambda row: row.charge_entity_id},
        ),
    ),
    # lease_ends, lease_end_deductions, lease_transfers (PLAN-012e D4, D6)
    ConstraintCase(
        "lease_ends_confirmed_complete",
        new_confirmed_lease_end,
        lambda: new_confirmed_lease_end(reason=""),
    ),
    ConstraintCase(
        "lease_ends_confirmed_at_iff_confirmed",
        new_lease_end,
        lambda: new_lease_end(confirmed_at=datetime(2026, 11, 1, 12, tzinfo=UTC)),
    ),
    ConstraintCase(
        "lease_ends.lease_id",
        unique_pair(new_lease_end, {}, {}),
        unique_pair(new_lease_end, {}, {"lease": _same_lease}),
        column="lease_id",
    ),
    ConstraintCase(
        "lease_transfers.lease_end_id",
        unique_pair(
            new_lease_transfer,
            {"lease_end": None},
            {"lease_end": lambda row: saved(new_lease_end())},
        ),
        _second_transfer_of_one_end,
        column="lease_end_id",
    ),
    ConstraintCase(
        "lease_end_deductions_amount_positive",
        lambda: new_lease_end_deduction(amount=D("0.01")),
        lambda: new_lease_end_deduction(amount=D("0.00")),
    ),
    ConstraintCase(
        "lease_transfers_two_leases",
        lambda: new_lease_transfer(incoming_lease=saved(new_lease())),
        lambda: _same_lease_transfer(),
    ),
    # PLAN-012e D6: the transfer wizard's state (0037).
    ConstraintCase(
        "lease_transfers_incoming_debit_day_range",
        lambda: new_lease_transfer(incoming_debit_day=31),
        lambda: new_lease_transfer(incoming_debit_day=0),
    ),
    ConstraintCase(
        "lease_transfers_incoming_second_debit_day_range",
        lambda: new_lease_transfer(incoming_second_debit_day=1),
        lambda: new_lease_transfer(incoming_second_debit_day=32),
    ),
    ConstraintCase(
        "lease_transfers_incoming_rent_positive",
        lambda: new_lease_transfer(incoming_rent=D("0.01")),
        lambda: new_lease_transfer(incoming_rent=D("0.00")),
    ),
    ConstraintCase(
        "lease_transfers_deposit_amount_not_negative",
        lambda: new_lease_transfer(deposit_amount=D("0.00")),
        lambda: new_lease_transfer(deposit_amount=D("-0.01")),
    ),
    ConstraintCase(
        "lease_transfers_confirmed_complete",
        _confirmed_transfer,
        lambda: _confirmed_transfer(incoming_lease=None),
    ),
    ConstraintCase(
        "lease_transfers_confirmed_at_iff_confirmed",
        new_lease_transfer,
        lambda: new_lease_transfer(confirmed_at=datetime(2026, 11, 1, 12, tzinfo=UTC)),
    ),
    ConstraintCase(
        "lease_transfers_one_draft_per_store",
        _second_store_transfer(confirmed_first=True),
        _second_store_transfer(confirmed_first=False),
    ),
    # legacy_records
    ConstraintCase(
        "legacy_records_row_unique",
        unique_pair(new_legacy_record, {}, {"row_number": 3}),
        unique_pair(new_legacy_record, {}, {}),
    ),
    # PLAN-011b D5: re-debit and NSF fee charges, the NSF counter, the resolution kind.
    ConstraintCase(
        "one_off_charges_source_payment_iff_redebit",
        lambda: new_one_off_charge(
            category="redebit",
            description="Re-debit of Rent 2026-09",
            source_payment=saved(new_payment()),
        ),
        lambda: new_one_off_charge(source_payment=saved(new_payment())),
    ),
    ConstraintCase(
        "one_off_charges_companion_iff_nsf_fee",
        lambda: new_one_off_charge(
            category="nsf_fee", description="NSF fee", companion_of=saved(new_one_off_charge())
        ),
        lambda: new_one_off_charge(category="nsf_fee", description="NSF fee"),
    ),
    ConstraintCase(
        "one_off_charges_one_redebit_per_payment",
        lambda: _second_redebit(same_payment=False),
        lambda: _second_redebit(same_payment=True),
    ),
    ConstraintCase(
        "one_off_charges_one_fee_per_redebit",
        lambda: _second_fee(same_redebit=False),
        lambda: _second_fee(same_redebit=True),
    ),
    # PLAN-012e D9: installment plans (0038).
    ConstraintCase(
        "one_off_charges_plan_iff_installment",
        lambda: new_installment(),
        lambda: new_installment(plan=None),
    ),
    ConstraintCase(
        "installment_plans_one_parent",
        lambda: new_installment_plan(),
        lambda: new_installment_plan(
            rent_charge=saved(new_rent_charge()), one_off_charge=saved(new_one_off_charge())
        ),
    ),
    ConstraintCase(
        "installment_plans_count_range",
        lambda: new_installment_plan(count=12),
        lambda: new_installment_plan(count=13),
    ),
    ConstraintCase(
        "installment_plans_cancelled_has_note",
        lambda: new_installment_plan(
            status="cancelled", cancelled_at=datetime(2026, 10, 7, tzinfo=UTC), note="Stopped"
        ),
        lambda: new_installment_plan(
            status="cancelled", cancelled_at=datetime(2026, 10, 7, tzinfo=UTC)
        ),
    ),
    ConstraintCase(
        "installment_plans_one_per_rent_charge",
        lambda: _second_plan(cancelled_first=True, rent=True),
        lambda: _second_plan(cancelled_first=False, rent=True),
    ),
    ConstraintCase(
        "installment_plans_one_per_one_off_charge",
        lambda: _second_plan(cancelled_first=True, rent=False),
        lambda: _second_plan(cancelled_first=False, rent=False),
    ),
    ConstraintCase(
        "operators_nsf_return_count_not_negative",
        lambda: new_operator(nsf_return_count=0),
        lambda: new_operator(nsf_return_count=-1),
    ),
    ConstraintCase(
        "payments_resolution_kind_iff_resolved",
        lambda: new_payment(
            status="returned",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Rescheduled.",
            resolution_kind="note",
        ),
        lambda: new_payment(
            status="returned",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Rescheduled.",
        ),
    ),
    ConstraintCase(
        "payments_redebit_date_iff_rescheduled",
        lambda: new_payment(
            status="returned",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Rescheduled.",
            resolution_kind="rescheduled",
            redebit_date=date(2026, 10, 9),
        ),
        lambda: new_payment(
            status="returned",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Rescheduled.",
            resolution_kind="rescheduled",
        ),
    ),
    # A bogus resolution kind on an unresolved payment breaks the pairing CHECK as well;
    # on a resolved one only the choice CHECK.
    ConstraintCase(
        "payments_resolution_kind_valid",
        lambda: new_payment(
            status="returned",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Done.",
            resolution_kind="note",
        ),
        lambda: new_payment(
            status="returned",
            resolved_at=datetime(2026, 10, 1, tzinfo=UTC),
            resolution_note="Done.",
            resolution_kind="bogus",
        ),
    ),
    *(
        case
        for case in choice_cases(LEDGER_FACTORIES)
        if case.name != "payments_resolution_kind_valid"
    ),
]

# Rejections the table above does not show with a single mutation.
EXTRA_REJECTIONS: list[ConstraintCase] = [
    ConstraintCase(
        "leases_second_debit_day_valid",
        new_lease,
        lambda: new_lease(debit_day=None, second_debit_day=15, collection_status="paused"),
    ),
    ConstraintCase(
        "leases_active_has_day_and_operator",
        new_lease,
        lambda: new_lease(collection_status="active", debit_day=None),
    ),
    ConstraintCase(
        "stores_needs_open_date_matches",
        new_store,
        lambda: new_store(open_date=date(2024, 1, 1), needs_open_date=True),
    ),
    ConstraintCase(
        "insurance_charges_needs_premium_matches",
        new_insurance_charge,
        lambda: new_insurance_charge(amount=D("695.00"), needs_premium=True),
    ),
    ConstraintCase(
        "charge_notes_exactly_one_charge",
        new_charge_note,
        lambda: new_charge_note(rent_charge=None, insurance_charge=None),
    ),
    ConstraintCase(
        "payments_refund_has_parent",
        new_payment,
        lambda: new_payment(is_refund=False, parent_payment=saved(new_payment())),
    ),
    ConstraintCase(
        "payments_policy_year_iff_insurance",
        new_payment,
        lambda: new_payment(kind="insurance", policy_year=None),
    ),
    ConstraintCase(
        "payments_manual_method_has_note",
        new_payment,
        lambda: new_payment(method="other", note=""),
    ),
    ConstraintCase(
        "invoices_at_most_one_source",
        new_invoice,
        lambda: new_invoice(
            insurance_charge=saved(new_insurance_charge()),
            charge_adjustment=saved(new_adjustment()),
        ),
    ),
    ConstraintCase(
        "invoices_at_most_one_source",
        new_invoice,
        lambda: new_invoice(
            one_off_charge=saved(new_setup_charge()), rent_charge=saved(new_rent_charge())
        ),
    ),
    ConstraintCase(
        "one_off_charges_waived_has_note",
        new_setup_charge,
        lambda: new_setup_charge(status="owed", waived_at=datetime(2026, 10, 1, tzinfo=UTC)),
    ),
    ConstraintCase(
        "one_off_charges_waived_has_note",
        new_setup_charge,
        lambda: new_setup_charge(status="waived", waive_note="No date."),
    ),
    ConstraintCase(
        "payments_one_off_kind_iff_one_off_charge",
        new_payment,
        lambda: new_payment(kind="rent", one_off_charge=saved(new_setup_charge())),
    ),
    ConstraintCase(
        "deposit_resolutions_method_iff_refunded",
        lambda: new_deposit_resolution(kind="withheld", method=""),
        lambda: new_deposit_resolution(kind="withheld", method="wire"),
    ),
    ConstraintCase(
        "deposit_resolutions_method_iff_refunded",
        new_deposit_resolution,
        lambda: new_deposit_resolution(kind="refunded", method="ach"),
    ),
    ConstraintCase(
        "deposit_resolutions_to_lease_iff_transferred",
        new_deposit_resolution,
        lambda: new_deposit_resolution(to_lease=saved(new_lease())),
    ),
    ConstraintCase(
        "payments_refund_deposit_manual",
        new_payment,
        lambda: new_payment(
            kind="refund_deposit",
            method="other",
            note="Paid back.",
            one_off_charge=saved(new_setup_charge()),
            is_refund=True,
            parent_payment=saved(new_payment()),
        ),
    ),
    # PLAN-012a D2: a total that ignores the fees, or the surcharge, is refused.
    ConstraintCase(
        "invoices_total_is_sum_of_parts",
        new_invoice,
        lambda: new_invoice(subtotal=D("10.00"), fees=D("2.00"), total=D("10.00")),
    ),
    ConstraintCase(
        "invoices_total_is_sum_of_parts",
        new_invoice,
        lambda: new_invoice(subtotal=D("10.00"), surcharge=D("0.30"), total=D("10.00")),
    ),
    # PLAN-012a D3: "+1" alone, 16 digits, a leading zero, a space.
    *(
        ConstraintCase(
            "operators_sms_phone_e164",
            new_operator,
            functools.partial(new_operator, sms_phone=phone),
        )
        for phone in ("+1", "+1404555123456789", "+04045551234", "+1 4045551234")
    ),
]


# --- Row 3: constraints -------------------------------------------------------------


def test_every_constraint_has_a_case() -> None:
    assert {case.name for case in LEDGER_CASES} == declared_constraints("ledger")


def database_constraint_names(app_label: str) -> set[str]:
    from django.apps import apps

    names: set[str] = set()
    with connection.cursor() as cursor:
        for model in apps.get_app_config(app_label).get_models():
            names |= set(connection.introspection.get_constraints(cursor, model._meta.db_table))
    return names


@pytest.mark.parametrize("app_label", ["ledger", "payments"])
def test_every_named_constraint_exists_in_the_database(app_label: str) -> None:
    from django.apps import apps

    declared = {
        constraint.name
        for model in apps.get_app_config(app_label).get_models()
        for constraint in model._meta.constraints
    }
    assert declared <= database_constraint_names(app_label)


@pytest.mark.parametrize("case", LEDGER_CASES, ids=str)
def test_constraint_accepts_a_valid_row(case: ConstraintCase) -> None:
    assert_accepted(case)


@pytest.mark.parametrize("case", LEDGER_CASES + EXTRA_REJECTIONS, ids=str)
def test_constraint_rejects_an_invalid_row(case: ConstraintCase) -> None:
    assert_rejected(case)


def test_blank_review_reason_and_legacy_status_are_allowed() -> None:
    insert(new_lease(review_reason=""))
    insert(new_insurance_charge(legacy_status=""))


def test_store_balance_amount_may_be_negative() -> None:
    insert(new_store_balance(amount=D("-595.00")))


# --- Row 4: money -------------------------------------------------------------------


@pytest.mark.parametrize("amount", [D("12581.00"), D("0.01"), D("9999999999.99")], ids=str)
def test_money_round_trip_is_exact(amount: Decimal) -> None:
    charge = saved(new_rent_charge(amount_due=amount))
    stored = ledger.RentCharge.objects.get(pk=charge.pk)
    assert stored.amount_due == amount
    assert isinstance(stored.amount_due, Decimal)
    assert str(stored.amount_due) == str(amount)
    assert stored.balance == amount
    assert ledger.RentCharge.objects.filter(amount_due=amount).count() == 1


def test_balance_is_generated_and_follows_updates() -> None:
    charge = saved(new_rent_charge(amount_due=D("12581.00"), amount_paid=D("581.01")))
    charge.refresh_from_db()
    assert charge.balance == D("11999.99")
    ledger.RentCharge.objects.filter(pk=charge.pk).update(amount_paid=D("12581.00"))
    charge.refresh_from_db()
    assert charge.balance == D("0.00")


@pytest.mark.parametrize("value", [12.5, 0.1, float("nan"), True], ids=repr)
def test_money_field_refuses_inexact_values_on_save(value: object) -> None:
    charge = new_rent_charge(amount_due=value)
    with pytest.raises(ValidationError), transaction.atomic():
        charge.save()
    assert not ledger.RentCharge.objects.exists()


def test_money_field_refuses_inexact_values_in_queries_and_updates() -> None:
    charge = saved(new_rent_charge())
    with pytest.raises(ValidationError):
        ledger.RentCharge.objects.filter(amount_due=2450.0).count()  # type: ignore[misc]
    with pytest.raises(ValidationError), transaction.atomic():
        ledger.RentCharge.objects.filter(pk=charge.pk).update(amount_paid=0.1)
    charge.refresh_from_db()
    assert charge.amount_paid == D("0.00")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_money_field_refuses_non_finite_decimals(value: str) -> None:
    charge = new_rent_charge(amount_due=D(value))
    with pytest.raises(ValidationError), transaction.atomic():
        charge.save()


@pytest.mark.parametrize("value", [D("2450.00"), 2450, "2450.00"], ids=repr)
def test_money_field_accepts_exact_input(value: object) -> None:
    charge = saved(new_rent_charge(amount_due=value))
    charge.refresh_from_db()
    assert charge.amount_due == D("2450.00")


def test_no_float_field_in_ledger_or_payments() -> None:
    from django.apps import apps

    for label in ("ledger", "payments"):
        for model in apps.get_app_config(label).get_models():
            for field in model._meta.concrete_fields:
                assert not isinstance(field, models.FloatField), field
                if isinstance(field, models.DecimalField):
                    assert isinstance(field, ledger.StrictDecimalField), field


# --- Model rules -----------------------------------------------------------------------


def test_operator_email_is_stored_stripped_lower_case_and_empty_as_null() -> None:
    operator = saved(new_operator(email="  Ops.Team@Example.COM "))
    assert ledger.Operator.objects.get(pk=operator.pk).email == "ops.team@example.com"
    blank = saved(new_operator(email="   "))
    assert ledger.Operator.objects.get(pk=blank.pk).email is None


def test_store_needs_open_date_follows_open_date_on_save() -> None:
    store = saved(new_store(open_date=None, needs_open_date=False))
    assert ledger.Store.objects.get(pk=store.pk).needs_open_date is True


def test_insurance_charge_needs_premium_follows_amount_on_save() -> None:
    charge = saved(new_insurance_charge(amount=None, needs_premium=False))
    assert ledger.InsuranceCharge.objects.get(pk=charge.pk).needs_premium is True


def test_cross_store_adjustment_requires_the_same_operator() -> None:
    operator = saved(new_operator())
    first = saved(new_rent_charge(lease=saved(new_lease(operator=operator))))
    second = saved(new_rent_charge(lease=saved(new_lease(operator=operator))))
    new_adjustment(reason="cross_store_balance", from_charge=first, to_charge=second).full_clean()

    stranger = saved(new_rent_charge(lease=saved(new_lease())))
    adjustment = new_adjustment(reason="cross_store_balance", from_charge=first, to_charge=stranger)
    with pytest.raises(ValidationError) as raised:
        adjustment.full_clean()
    assert raised.value.messages == [
        "A cross-store balance moves between charges of the same operator."
    ]


def test_other_adjustments_may_span_operators() -> None:
    first, second = _two_charges()
    new_adjustment(reason="credit", from_charge=first, to_charge=second).full_clean()


def test_save_runs_the_payment_data_guard() -> None:
    with pytest.raises(ValidationError) as raised:
        new_operator(notes=f"Card {_CARD}").save()
    assert raised.value.messages == [PAYMENT_DATA_MESSAGE]
    assert _CARD not in str(raised.value)
    assert not ledger.Operator.objects.exists()


def test_provider_status_is_not_editable_and_documented() -> None:
    field = ledger.Invoice._meta.get_field("provider_status")
    assert field.editable is False
    assert "sync layer" in str(field.help_text)


# --- Row 9: audit_log is append-only (PostgreSQL) -------------------------------------


@requires_postgresql
def test_audit_log_accepts_inserts() -> None:
    entry = saved(new_audit_log(after={"status": "sent"}))
    assert ledger.AuditLog.objects.filter(pk=entry.pk).exists()


@requires_postgresql
@pytest.mark.parametrize(
    "operation",
    ["save", "queryset_update", "queryset_delete", "instance_delete", "raw_update", "raw_delete"],
)
def test_audit_log_rejects_update_and_delete(operation: str) -> None:
    entry = saved(new_audit_log(after={"status": "sent"}))
    with pytest.raises(IntegrityError, match="audit_log is append-only"), transaction.atomic():
        if operation == "save":
            entry.action = "changed"
            entry.save()
        elif operation == "queryset_update":
            ledger.AuditLog.objects.filter(pk=entry.pk).update(action="changed")
        elif operation == "queryset_delete":
            ledger.AuditLog.objects.filter(pk=entry.pk).delete()
        elif operation == "instance_delete":
            entry.delete()
        else:
            with connection.cursor() as cursor:
                if operation == "raw_update":
                    cursor.execute("UPDATE audit_log SET action = 'x' WHERE id = %s", [entry.pk])
                else:
                    cursor.execute("DELETE FROM audit_log WHERE id = %s", [entry.pk])
    assert ledger.AuditLog.objects.get(pk=entry.pk).action == "update"


# --- PLAN-012a: line kind, fees and surcharge, operator consent ----------------------


def test_new_invoices_and_lines_default_to_no_fees_and_a_charge_line() -> None:
    line = saved(new_line_item())
    line.refresh_from_db()
    invoice = line.invoice
    invoice.refresh_from_db()
    assert line.kind == "charge"
    assert (invoice.fees, invoice.surcharge) == (D("0.00"), D("0.00"))
    assert invoice.total == invoice.subtotal + invoice.tax


def test_line_kind_choices_are_the_closed_list() -> None:
    assert ledger.InvoiceLineKind.values == ["charge", "fee", "surcharge", "tax", "discount"]


@pytest.mark.parametrize("phone", ["", "+14045551234", "+447911123456", "+12345678"])
def test_sms_phone_accepts_blank_and_e164(phone: str) -> None:
    operator = new_operator(sms_phone=phone)
    operator.full_clean()
    saved(operator)


@pytest.mark.parametrize(
    "phone",
    ["+1", "+1404555123456789", "404-555-1234", "4045551234", "+1 404 555 1234", "+04045551234"],
)
def test_sms_phone_refuses_anything_but_e164_with_a_sentence_naming_the_format(
    phone: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        new_operator(sms_phone=phone).full_clean()
    assert raised.value.message_dict["sms_phone"] == [ledger.SMS_PHONE_MESSAGE]
    assert "+14045551234" in ledger.SMS_PHONE_MESSAGE


@pytest.mark.parametrize(
    "text",
    [
        "+378282246310005",  # a Luhn-valid 15-digit card number in E.164 shape
        "011000015 1234567",  # a routing number next to an account number
    ],
)
def test_sms_phone_is_covered_by_the_payment_data_guard(text: str) -> None:
    with pytest.raises(ValidationError, match=PAYMENT_DATA_MESSAGE), transaction.atomic():
        new_operator(sms_phone=text).save()
    assert not ledger.Operator.objects.exists()


@requires_postgresql
def test_sms_phone_card_number_is_refused_by_the_database_trigger() -> None:
    operator = saved(new_operator())
    with pytest.raises(IntegrityError), transaction.atomic():
        ledger.Operator.objects.filter(pk=operator.pk).update(sms_phone="+378282246310005")
    operator.refresh_from_db()
    assert operator.sms_phone == ""


@pytest.mark.parametrize("field", ["sms_consent_at", "email_consent_at"])
def test_a_consent_time_in_the_future_is_refused(field: str) -> None:
    later = timezone.now() + timedelta(minutes=1)
    with pytest.raises(ValidationError) as raised:
        new_operator(**{field: later}).full_clean()
    assert raised.value.message_dict == {field: [ledger.CONSENT_IN_FUTURE_MESSAGE]}


@pytest.mark.parametrize("field", ["sms_consent_at", "email_consent_at"])
def test_a_consent_time_now_or_earlier_is_accepted(field: str) -> None:
    for moment in (timezone.now(), datetime(2026, 1, 1, tzinfo=UTC)):
        new_operator(**{field: moment}).full_clean()
