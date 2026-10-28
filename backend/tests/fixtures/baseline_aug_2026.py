"""August 2026 as the Rents sheet shows it (docs/qa/PLAN-011-dashboard-reports-mobile.md D8;
docs/COLUMN_MAPPING.md §3 "Post-import check"): 392 rent charges collected for exactly
$1,466,000.00 (an invented total; the sheet's real figure lives in the owner's private
inputs), 47 unpaid, 13 pending. A builder, not committed data: every row is made
by :func:`build_baseline` in the test's own database, until Step 13 brings the migrated
rows.

**The amounts' rule** (deterministic, not uniform):

- collected charge ``i`` (0 to 390): ``2000 + (i × 7919 mod 3500)`` dollars and
  ``(i × 37 mod 100)`` cents; the 392nd is the remainder that makes the total exactly
  $1,466,000.00 (it comes to $5,651.35);
- unpaid charge ``j`` (0 to 46): ``1800 + (j × 613 mod 4200)`` dollars and
  ``(j × 53 mod 100)`` cents;
- pending charge ``k`` (0 to 12): ``2500 + (k × 389 mod 3000)`` dollars.

**The shape**: one store, one active lease (started 2025-03-01, rent = the charge's
amount) and one August rent charge per row, 452 stores in all, numbered from 1000, in
seven states. Debit days run 1 to 25 by store, except the 13 pending ones, all debited
on the 9th (the sheet's "9/9 pending"; each has a pending ACH payment initiated on
2026-08-09). Every tenth collected or unpaid lease pays by wire, the others by ACH
through the provider. Collected charges are paid in full by a settled payment two days
after their due date (still in August). Operators: collected stores rotate over all
:data:`OPERATOR_COUNT` operators; the unpaid ones over the first
:data:`UNPAID_OPERATOR_COUNT` (so 7 of them owe two stores); the pending ones each have
their own operator outside those, so no operator is both behind and pending.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ledger.models import (
    CollectionStatus,
    Lease,
    LeaseRentHistory,
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

PERIOD = date(2026, 8, 1)
#: The day the checks read the month on: its last day, after every due date.
AS_OF = date(2026, 8, 31)
LEASE_START = date(2025, 3, 1)
COLLECTED_COUNT = 392
COLLECTED_TOTAL = Decimal("1466000.00")
UNPAID_COUNT = 47
PENDING_COUNT = 13
OPERATOR_COUNT = 150
UNPAID_OPERATOR_COUNT = 40
#: The pending operators are numbered from here, after every unpaid operator.
PENDING_OPERATOR_START = 100
PENDING_DEBIT_DAY = 9
FIRST_STORE_NUMBER = 1000
STATES = ("GA", "FL", "TX", "AL", "SC", "NC", "TN")
CENT = Decimal("0.01")


def collected_amounts() -> list[Decimal]:
    amounts = [
        (Decimal(2000 + (i * 7919) % 3500) + Decimal((i * 37) % 100) / 100).quantize(CENT)
        for i in range(COLLECTED_COUNT - 1)
    ]
    amounts.append(COLLECTED_TOTAL - sum(amounts, Decimal("0.00")))
    return amounts


def unpaid_amounts() -> list[Decimal]:
    return [
        (Decimal(1800 + (j * 613) % 4200) + Decimal((j * 53) % 100) / 100).quantize(CENT)
        for j in range(UNPAID_COUNT)
    ]


def pending_amounts() -> list[Decimal]:
    return [Decimal(2500 + (k * 389) % 3000).quantize(CENT) for k in range(PENDING_COUNT)]


@dataclass(frozen=True, slots=True)
class Baseline:
    operators: list[Operator]
    collected: list[RentCharge]
    unpaid: list[RentCharge]
    pending: list[RentCharge]
    #: How many leases are debited on each day of the month.
    due_by_day: dict[int, int]

    @property
    def unpaid_total(self) -> Decimal:
        return sum((charge.amount_due for charge in self.unpaid), Decimal("0.00"))

    @property
    def pending_total(self) -> Decimal:
        return sum((charge.amount_due for charge in self.pending), Decimal("0.00"))

    @property
    def unpaid_operator_ids(self) -> set[uuid.UUID]:
        return {self.operators[j % UNPAID_OPERATOR_COUNT].pk for j in range(UNPAID_COUNT)}


@dataclass(frozen=True, slots=True)
class _Row:
    status: str
    amount: Decimal
    operator: Operator
    debit_day: int
    channel: str


def _rows(operators: list[Operator]) -> list[_Row]:
    rows: list[_Row] = []
    for i, amount in enumerate(collected_amounts()):
        rows.append(
            _Row(
                RentChargeStatus.PAID,
                amount,
                operators[i % OPERATOR_COUNT],
                i % 25 + 1,
                PaymentChannel.WIRE if i % 10 == 9 else PaymentChannel.PROVIDER_ACH,
            )
        )
    for j, amount in enumerate(unpaid_amounts()):
        rows.append(
            _Row(
                RentChargeStatus.UNPAID,
                amount,
                operators[j % UNPAID_OPERATOR_COUNT],
                (j * 3) % 25 + 1,
                PaymentChannel.WIRE if j % 10 == 9 else PaymentChannel.PROVIDER_ACH,
            )
        )
    for k, amount in enumerate(pending_amounts()):
        rows.append(
            _Row(
                RentChargeStatus.PENDING,
                amount,
                operators[PENDING_OPERATOR_START + k],
                PENDING_DEBIT_DAY,
                PaymentChannel.PROVIDER_ACH,
            )
        )
    return rows


def build_baseline() -> Baseline:
    """Write the August 2026 baseline (see the module docstring) and return its rows."""
    operators = Operator.objects.bulk_create(
        [Operator(display_name=f"Operator {n:03d}") for n in range(OPERATOR_COUNT)]
    )
    rows = _rows(operators)
    stores = Store.objects.bulk_create(
        [
            Store(
                external_label=str(FIRST_STORE_NUMBER + n),
                store_number=FIRST_STORE_NUMBER + n,
                city=f"City {n % 31}",
                state=STATES[n % len(STATES)],
                status=StoreStatus.ACTIVE,
                needs_open_date=True,
            )
            for n in range(len(rows))
        ]
    )
    leases = Lease.objects.bulk_create(
        [
            Lease(
                store=store,
                operator=row.operator,
                debit_day=row.debit_day,
                collection_status=CollectionStatus.ACTIVE,
                payment_channel=row.channel,
                started_on=LEASE_START,
            )
            for store, row in zip(stores, rows, strict=True)
        ]
    )
    LeaseRentHistory.objects.bulk_create(
        [
            LeaseRentHistory(
                lease=lease, amount=row.amount, effective_from=LEASE_START, source=RentSource.STAFF
            )
            for lease, row in zip(leases, rows, strict=True)
        ]
    )
    charges = RentCharge.objects.bulk_create(
        [
            RentCharge(
                lease=lease,
                period_month=PERIOD,
                seq=1,
                due_on=PERIOD.replace(day=row.debit_day),
                amount_due=row.amount,
                amount_paid=row.amount if row.status == RentChargeStatus.PAID else Decimal("0.00"),
                status=row.status,
            )
            for lease, row in zip(leases, rows, strict=True)
        ]
    )
    payments = []
    for charge, row in zip(charges, rows, strict=True):
        if row.status == RentChargeStatus.PAID:
            wire = row.channel == PaymentChannel.WIRE
            payments.append(
                Payment(
                    rent_charge=charge,
                    kind=PaymentKind.RENT,
                    method=PaymentMethod.WIRE if wire else PaymentMethod.ACH,
                    amount=row.amount,
                    status=PaymentStatus.SETTLED,
                    initiated_on=charge.due_on,
                    settled_on=charge.due_on + timedelta(days=2),
                    note="Wire received." if wire else "",
                )
            )
        elif row.status == RentChargeStatus.PENDING:
            payments.append(
                Payment(
                    rent_charge=charge,
                    kind=PaymentKind.RENT,
                    method=PaymentMethod.ACH,
                    amount=row.amount,
                    status=PaymentStatus.PENDING,
                    initiated_on=PERIOD.replace(day=PENDING_DEBIT_DAY),
                )
            )
    Payment.objects.bulk_create(payments)
    by_status: dict[str, list[RentCharge]] = {status: [] for status in RentChargeStatus.values}
    for charge in charges:
        by_status[charge.status].append(charge)
    return Baseline(
        operators=operators,
        collected=by_status[RentChargeStatus.PAID],
        unpaid=by_status[RentChargeStatus.UNPAID],
        pending=by_status[RentChargeStatus.PENDING],
        due_by_day=dict(Counter(row.debit_day for row in rows)),
    )
