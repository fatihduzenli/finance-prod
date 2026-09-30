"""Ledger tables: constraints, money and model rules (PLAN-004 §6 rows 3, 4 and 9).

Row 3: every CHECK and unique constraint of PLAN-004 §4.1 is exercised at database
level. Each case inserts a valid row (accepted) and an invalid one (``IntegrityError``)
with ``bulk_create``, which skips ``save()`` and so reaches the database unchanged.
``test_every_constraint_has_a_case`` fails when a constraint is added without a case.

The factories here build one valid, unsaved row per model (dependencies are saved) and
are reused by the other PLAN-004 test modules.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction

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
    _default(fields, "operator", lambda: saved(new_operator()))
    values = {"kind": "lease agreement", "source": "upload", "file_reference": "uploads/x.pdf"}
    return ledger.Document(**{**values, **fields})


def new_outbound_message(**fields: Any) -> ledger.OutboundMessage:
    _default(fields, "operator", lambda: saved(new_operator()))
    values = {"template": "termination_notice", "subject": "Notice", "delivery_status": ""}
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
    ledger.ChargeNote: new_charge_note,
    ledger.ChargeAdjustment: new_adjustment,
    ledger.StoreBalance: new_store_balance,
    ledger.Note: new_note,
    ledger.Invoice: new_invoice,
    ledger.InvoiceLineItem: new_line_item,
    ledger.Payment: new_payment,
    ledger.AuditLog: new_audit_log,
    ledger.LegacyRecord: new_legacy_record,
    ledger.ImportAudit: new_import_audit,
    ledger.Document: new_document,
    ledger.OutboundMessage: new_outbound_message,
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
        "invoices_total_is_subtotal_plus_tax",
        lambda: new_invoice(subtotal=D("0.10"), tax=D("0.20"), total=D("0.30")),
        lambda: new_invoice(subtotal=D("0.10"), tax=D("0.20"), total=D("0.31")),
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
    # legacy_records
    ConstraintCase(
        "legacy_records_row_unique",
        unique_pair(new_legacy_record, {}, {"row_number": 3}),
        unique_pair(new_legacy_record, {}, {}),
    ),
    *choice_cases(LEDGER_FACTORIES),
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
