"""Builders for the dashboard, grid and arrears tests (PLAN-011 Batch A). Not a test module.

Rows are written directly, as the sheet import will write them: a store, a lease with
its rent row, rent charges with their stored status and amounts, and the payments that
explain them. Nothing here calls a service that queues provider work.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from ledger.models import (
    CollectionStatus,
    InsuranceCharge,
    Invoice,
    Lease,
    LeaseRentHistory,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    RentSource,
    Store,
    StoreStatus,
)
from payments.models import ExternalRef

D = Decimal
ZERO = D("0.00")
LEASE_START = date(2025, 1, 1)


def an_operator(name: str = "") -> Operator:
    return Operator.objects.create(display_name=name or f"Operator {uuid.uuid4().hex[:6]}")


def a_store(label: str = "", number: int | None = None, state: str = "GA") -> Store:
    return Store.objects.create(
        external_label=label or (str(number) if number is not None else uuid.uuid4().hex[:8]),
        store_number=number,
        state=state,
        status=StoreStatus.ACTIVE,
    )


def a_lease(
    store: Store | None = None,
    operator: Operator | None = None,
    *,
    started: date | None = LEASE_START,
    channel: str = PaymentChannel.PROVIDER_ACH,
    debit_day: int = 5,
    second_debit_day: int | None = None,
    rent: str = "3500.00",
    status: str = CollectionStatus.ACTIVE,
) -> Lease:
    lease = Lease.objects.create(
        store=store or a_store(),
        operator=operator if operator is not None else an_operator(),
        debit_day=debit_day,
        second_debit_day=second_debit_day,
        collection_status=status,
        payment_channel=channel,
        started_on=started,
    )
    LeaseRentHistory.objects.create(
        lease=lease,
        amount=D(rent),
        effective_from=started or LEASE_START,
        source=RentSource.STAFF,
    )
    return lease


def a_rent_charge(
    lease: Lease,
    month: date,
    status: str = RentChargeStatus.UNPAID,
    due: str = "3500.00",
    paid: str = "0.00",
    *,
    seq: int = 1,
    day: int | None = None,
) -> RentCharge:
    return RentCharge.objects.create(
        lease=lease,
        period_month=month,
        seq=seq,
        due_on=month.replace(day=day or lease.debit_day or 1),
        amount_due=D(due),
        amount_paid=D(paid),
        status=status,
    )


def a_paid_rent_charge(lease: Lease, month: date, amount: str, settled_on: date) -> RentCharge:
    """A rent charge paid in full by one settled ACH payment."""
    charge = a_rent_charge(lease, month, RentChargeStatus.PAID, amount, amount)
    a_payment(charge, amount, settled_on)
    return charge


def a_payment(
    charge: RentCharge,
    amount: str,
    settled_on: date | None,
    status: str = PaymentStatus.SETTLED,
    initiated_on: date | None = None,
) -> Payment:
    return Payment.objects.create(
        rent_charge=charge,
        kind=PaymentKind.RENT,
        method=PaymentMethod.ACH,
        amount=D(amount),
        status=status,
        initiated_on=initiated_on or settled_on,
        settled_on=settled_on,
    )


def an_insurance_charge(
    lease: Lease, year: int, due_on: date, amount: str | None = "695.00", **fields: object
) -> InsuranceCharge:
    return InsuranceCharge.objects.create(
        lease=lease,
        policy_year=year,
        due_on=due_on,
        amount=D(amount) if amount is not None else None,
        **fields,
    )


def a_one_off_charge(
    operator: Operator,
    price: str,
    due_date: date,
    *,
    lease: Lease | None = None,
    status: str = OneOffChargeStatus.OWED,
    paid: str = "0.00",
    **fields: object,
) -> OneOffCharge:
    values: dict[str, object] = {
        "operator": operator,
        "lease": lease,
        "category": OneOffCategory.DAMAGE,
        "collection": "manual",
        "description": "Broken shelf",
        "price": D(price),
        "due_date": due_date,
        "amount_paid": D(paid),
        "status": status,
    }
    values.update(fields)
    return OneOffCharge.objects.create(**values)


def an_invoice(operator: Operator, number: int, total: str = "100.00", **fields: object) -> Invoice:
    values: dict[str, object] = {
        "invoice_number": f"INV-2026-{number:04d}",
        "operator": operator,
        "issue_date": date(2026, 8, 1),
        "due_date": date(2026, 8, 5),
        "subtotal": D(total),
        "total": D(total),
    }
    values.update(fields)
    return Invoice.objects.create(**values)


def link_provider_invoice(invoice: Invoice, external_id: str) -> None:
    ExternalRef.objects.create(
        entity_type="invoice",
        entity_id=invoice.pk,
        provider="stax_pay",
        external_kind="invoice",
        external_id=external_id,
    )
