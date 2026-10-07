"""Who owes what, and since when (docs/qa/PLAN-011-dashboard-reports-mobile.md D1, D3).

Read only: nothing here writes or calls the payment provider.

**Open balances** (:func:`open_balances`) is the one place that decides what is owed:
one :class:`OpenBalance` per open charge, from three queries, whatever the number of
charges:

- **rent**: rent charges with a ``balance`` above zero that are not ``waived`` (a
  pending charge is still owed until its payment settles);
- **insurance**: insurance charges with an amount (a charge that still needs its premium
  has no amount and is owed nothing yet), less their settled ``insurance`` payments and
  what their re-debits collected, plus their settled refunds; a legacy charge the sheet
  shows paid owes nothing;
- **one-off**: one-off charges with a ``balance`` above zero, neither ``waived`` nor
  ``voided``, except deposits (a refundable fee type: money held, never owed revenue).

**A rescheduled return** (PLAN-011b D5): the returned payment reopened its charge, and the
Reschedule made a ``redebit`` one-off charge that collects the same amount again. What
the re-debit collects (up to its price; its NSF fee never counts) is paid on that charge
too (:func:`ledger.services.rent_run.redebit_collected`: stored on a rent or one-off
charge, derived here for an insurance charge). The open amount is owed once: while the
re-debit charge is neither waived nor voided, what it has still to collect is taken off
the open balance of the charge the returned payment was for (:func:`carried_by_redebits`),
so the operator owes the re-debit and its NSF fee, and once the re-debit is paid nothing
is left on either. A re-debit of a returned payment on a charge billed with others (a
re-debit's invoice, or a builder invoice's first line, PLAN-012 D6) carries that
payment's amount: what that charge's balance leaves over is carried by its NSF fee, or
by the invoice's later lines in line order. A re-debit is listed as a one-off charge
(``Re-debit: Re-debit of Rent 2026-09``); its fee as ``NSF fee: ...``.

Every other figure here sums those rows: the dashboard's total outstanding and overdue
(:func:`dashboard`), the arrears list (:func:`arrears`) and the A/R aging (:func:`aging`,
PLAN-011 D4: by operator and days past due, 0 or less, 1 to 30, 31 to 60, 61 to 90, over
90), whose grand total is therefore the dashboard's total outstanding. The customer
statement reads the same rows for one operator (``open_balances(today, operator=...)``:
the charges billed to it, a one-off charge's own operator before its lease's). A charge
is **overdue** when its due date is before ``today`` (the date in the project's
``TIME_ZONE``).

**Arrears** (D3): one row per operator that is behind: at least one rent charge
``unpaid`` or ``partial`` due before today, or an insurance or one-off balance due
before today. *Months behind* counts the distinct rent months of those rent charges (a
twice-monthly lease's two charges of one month are one month); the bucket is ``1``,
``2`` or ``3+`` (``0`` when only insurance or one-off charges are overdue). *Total
owed* is every open balance of the operator, overdue or not. A charge whose payment is
pending is owed but not behind: it never puts an operator on the list by itself.

**The dashboard** (D1) adds what is not a balance: payments settled this month net of
their settled refunds (deposits and deposit refunds left out), invoices created but not
sent, invoices whose last provider status is ``ATTEMPTED`` (a failed debit attempt),
today's run summary from grouped counts over the rent run's definitions
(:func:`ledger.services.rent_run.due_counts`) and the returns queue's own query, the
last reconciliation, and the waived charges whose invoice is still live (D0).

Money is ``Decimal`` throughout; nothing is converted to a float.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Exists, F, Max, OuterRef, Q, QuerySet, Sum
from django.db.models.functions import Coalesce

from ledger.models import (
    ZERO,
    InsuranceCharge,
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    LegacyStatus,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentChannel,
    PaymentKind,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import one_off_charges, rent_run, returns
from ledger.services.csv_export import CsvWriter
from ledger.services.pnl import add_months
from payments.models import ExternalRef, ReconciliationRun, ReconciliationStatus

KIND_RENT = "rent"
KIND_INSURANCE = "insurance"
KIND_ONE_OFF = "one_off"
#: The provider status of an invoice whose debit attempt failed (D1).
PROVIDER_ATTEMPTED = "ATTEMPTED"
#: Rent statuses that make a month count as behind (D3).
BEHIND_STATUSES: tuple[str, ...] = (RentChargeStatus.UNPAID, RentChargeStatus.PARTIAL)
#: Most rows the dashboard's lists show; the count above each list is the full one.
DASHBOARD_LIST_LIMIT = 50

BUCKET_NONE = "0"
BUCKET_ONE = "1"
BUCKET_TWO = "2"
BUCKET_THREE = "3+"
BUCKETS: tuple[str, ...] = (BUCKET_ONE, BUCKET_TWO, BUCKET_THREE, BUCKET_NONE)
BUCKET_LABELS: dict[str, str] = {
    BUCKET_ONE: "1 month",
    BUCKET_TWO: "2 months",
    BUCKET_THREE: "3 or more months",
    BUCKET_NONE: "Other charges only",
}

SORT_OPERATOR = "operator"
SORT_MONTHS = "months"
SORT_OWED = "owed"
SORT_LAST_PAYMENT = "last_payment"
SORTS: tuple[str, ...] = (SORT_MONTHS, SORT_OWED, SORT_OPERATOR, SORT_LAST_PAYMENT)

NO_OPERATOR = "No operator"
BAD_OPERATOR = "Unknown operator; showing every operator."
BAD_BUCKET = "Unknown months-behind bucket; showing every bucket."
BAD_SORT = "Unknown sort; sorted by months behind."
#: Longest state filter read from the query string.
STATE_MAX_LENGTH = 50


@dataclass(frozen=True, slots=True)
class OpenBalance:
    """One open charge and what is still owed on it."""

    kind: str
    charge_id: uuid.UUID
    operator_id: uuid.UUID | None
    operator_name: str
    store_id: uuid.UUID | None
    store_label: str
    store_state: str
    #: The lease's payment channel; ``""`` for a one-off charge without a lease.
    channel: str
    due_on: date
    balance: Decimal
    #: The charge's stored status (rent and one-off); ``""`` for insurance.
    status: str = ""
    #: The rent month of a rent charge; ``None`` otherwise.
    period_month: date | None = None
    #: What the charge is for, as a statement names it.
    description: str = ""
    #: The charge's lease; ``None`` for a one-off charge without one.
    lease_id: uuid.UUID | None = None

    def overdue(self, today: date) -> bool:
        return self.due_on < today


def _money(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _rent_description(period_month: date, seq: int) -> str:
    return f"Rent {period_month:%Y-%m}" + (f" (debit {seq})" if seq > 1 else "")


def _rent_balances(operator: uuid.UUID | None) -> list[OpenBalance]:
    rows = RentCharge.objects.filter(balance__gt=0).exclude(status=RentChargeStatus.WAIVED)
    if operator is not None:
        rows = rows.filter(lease__operator=operator)
    values = rows.values(
        "pk",
        "due_on",
        "balance",
        "status",
        "period_month",
        "seq",
        "lease",
        operator=F("lease__operator"),
        operator_name=F("lease__operator__display_name"),
        store=F("lease__store"),
        store_label=F("lease__store__external_label"),
        store_state=F("lease__store__state"),
        channel=F("lease__payment_channel"),
    ).order_by()
    return [
        OpenBalance(
            kind=KIND_RENT,
            charge_id=row["pk"],
            operator_id=row["operator"],
            operator_name=row["operator_name"] or "",
            store_id=row["store"],
            store_label=row["store_label"] or "",
            store_state=row["store_state"] or "",
            channel=row["channel"] or "",
            due_on=row["due_on"],
            balance=_money(row["balance"]),
            status=row["status"],
            period_month=row["period_month"],
            description=_rent_description(row["period_month"], row["seq"]),
            lease_id=row["lease"],
        )
        for row in values
    ]


def _insurance_balances(
    operator: uuid.UUID | None, collected: Mapping[uuid.UUID, Decimal]
) -> list[OpenBalance]:
    settled = Q(payments__status=PaymentStatus.SETTLED, payments__kind=PaymentKind.INSURANCE)
    charges = InsuranceCharge.objects.filter(amount__isnull=False).exclude(
        legacy_status=LegacyStatus.PAID
    )
    if operator is not None:
        charges = charges.filter(lease__operator=operator)
    rows = (
        charges.values(
            "pk",
            "due_on",
            "amount",
            "policy_year",
            "lease",
            operator=F("lease__operator"),
            operator_name=F("lease__operator__display_name"),
            store=F("lease__store"),
            store_label=F("lease__store__external_label"),
            store_state=F("lease__store__state"),
            channel=F("lease__payment_channel"),
        )
        .annotate(
            paid=Sum("payments__amount", filter=settled & Q(payments__is_refund=False)),
            refunded=Sum("payments__amount", filter=settled & Q(payments__is_refund=True)),
        )
        .order_by()
    )
    found = []
    for row in rows:
        balance = (
            _money(row["amount"])
            - _money(row["paid"])
            + _money(row["refunded"])
            - collected.get(row["pk"], ZERO)
        )
        if balance <= 0:
            continue
        found.append(
            OpenBalance(
                kind=KIND_INSURANCE,
                charge_id=row["pk"],
                operator_id=row["operator"],
                operator_name=row["operator_name"] or "",
                store_id=row["store"],
                store_label=row["store_label"] or "",
                store_state=row["store_state"] or "",
                channel=row["channel"] or "",
                due_on=row["due_on"],
                balance=balance,
                description=f"Insurance, policy year {row['policy_year']}",
                lease_id=row["lease"],
            )
        )
    return found


def _one_off_balances(operator: uuid.UUID | None) -> list[OpenBalance]:
    charges = (
        OneOffCharge.objects.filter(balance__gt=0)
        .exclude(status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .filter(Q(fee_type__isnull=True) | Q(fee_type__refundable=False))
    )
    if operator is not None:
        charges = charges.filter(
            Q(operator=operator) | Q(operator__isnull=True, lease__operator=operator)
        )
    categories = dict(OneOffCategory.choices)
    rows = charges.values(
        "pk",
        "due_date",
        "balance",
        "status",
        "category",
        "description",
        "lease",
        fee_name=F("fee_type__name"),
        billed=Coalesce("operator", "lease__operator"),
        billed_name=Coalesce("operator__display_name", "lease__operator__display_name"),
        store=F("lease__store"),
        store_label=F("lease__store__external_label"),
        store_state=F("lease__store__state"),
        channel=F("lease__payment_channel"),
    ).order_by()
    return [
        OpenBalance(
            kind=KIND_ONE_OFF,
            charge_id=row["pk"],
            operator_id=row["billed"],
            operator_name=row["billed_name"] or "",
            store_id=row["store"],
            store_label=row["store_label"] or "",
            store_state=row["store_state"] or "",
            channel=row["channel"] or "",
            due_on=row["due_date"],
            balance=_money(row["balance"]),
            status=row["status"],
            description=_one_off_description(categories, row),
            lease_id=row["lease"],
        )
        for row in rows
    ]


def _one_off_description(categories: Mapping[str, object], row: Mapping[str, object]) -> str:
    label = str(categories.get(str(row["category"]), row["category"]))
    detail = str(row["fee_name"] or row["description"] or "")
    return f"{label}: {detail}" if detail else label


def carried_by_redebits(
    operator: uuid.UUID | None = None,
) -> tuple[dict[tuple[str, uuid.UUID], Decimal], dict[uuid.UUID, Decimal]]:
    """In one query (PLAN-011b D5): per (kind, charge id), the amount live re-debit
    charges have still to collect again for the charge a returned payment was for; and
    per insurance charge, what its re-debits collected (each up to its price, whatever
    its status since), as :func:`ledger.services.rent_run.redebit_collected` counts it
    for the charges that store their paid amount."""
    rows = OneOffCharge.objects.filter(
        category=OneOffCategory.REDEBIT, source_payment__isnull=False
    )
    if operator is not None:
        rows = rows.filter(
            Q(operator=operator) | Q(operator__isnull=True, lease__operator=operator)
        )
    carried: dict[tuple[str, uuid.UUID], Decimal] = {}
    insurance_collected: dict[uuid.UUID, Decimal] = {}
    for status, price, paid, rent, insurance, one_off in rows.values_list(
        "status",
        "price",
        "amount_paid",
        "source_payment__rent_charge",
        "source_payment__insurance_charge",
        "source_payment__one_off_charge",
    ).order_by():
        collected = min(max(_money(paid), ZERO), _money(price))
        if insurance is not None:
            insurance_collected[insurance] = insurance_collected.get(insurance, ZERO) + collected
        if status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED):
            continue
        for kind, charge in (
            (KIND_RENT, rent),
            (KIND_INSURANCE, insurance),
            (KIND_ONE_OFF, one_off),
        ):
            if charge is not None:
                key = (kind, charge)
                carried[key] = carried.get(key, ZERO) + _money(price) - collected
    return carried, insurance_collected


def _billed_siblings(sources: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Per one-off charge in ``sources``, the charges billed with it, neither waived nor
    voided, in the order a payment on it pays them: a re-debit's companion fees in
    creation order (PLAN-011b D5), then a builder invoice's later lines in line order
    (PLAN-012 D6); the sibling definition of the allocation
    (:func:`ledger.services.one_off_charges.billed_companions`). Two queries."""
    closed = [OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED]
    siblings: dict[uuid.UUID, list[uuid.UUID]] = {}
    for fee, source in (
        OneOffCharge.objects.filter(companion_of__in=sources)
        .exclude(status__in=closed)
        .order_by("created_at", "id")
        .values_list("pk", "companion_of")
    ):
        if source is not None:
            siblings.setdefault(source, []).append(fee)
    for later, source in (
        InvoiceLineItem.objects.filter(
            invoice__one_off_charge__in=sources, one_off_charge__isnull=False
        )
        .exclude(one_off_charge=F("invoice__one_off_charge"))
        .exclude(one_off_charge__status__in=closed)
        .order_by("position")
        .values_list("one_off_charge", "invoice__one_off_charge")
    ):
        siblings.setdefault(source, []).append(later)
    return siblings


def _spill_to_companions(
    rows: list[OpenBalance], carried: Mapping[tuple[str, uuid.UUID], Decimal]
) -> dict[tuple[str, uuid.UUID], Decimal]:
    """``carried``, where a re-debit carries a payment on a charge billed with others (a
    re-debit's invoice with its NSF fee, or a builder invoice's first line with its later
    lines): what that charge's own balance leaves over is carried by its siblings in the
    order a payment pays them (:func:`_billed_siblings`; the allocation of PLAN-011b D5
    and PLAN-012 D6). Two more queries, only when a one-off charge is carried."""
    sources = [charge for kind, charge in carried if kind == KIND_ONE_OFF]
    if not sources:
        return dict(carried)
    balances = {(row.kind, row.charge_id): row.balance for row in rows}
    spilled = dict(carried)
    for source, fees in _billed_siblings(sources).items():
        left = carried[(KIND_ONE_OFF, source)] - balances.get((KIND_ONE_OFF, source), ZERO)
        for fee in fees:
            if left <= 0:
                break
            key = (KIND_ONE_OFF, fee)
            share = min(left, balances.get(key, ZERO))
            spilled[key] = spilled.get(key, ZERO) + share
            left -= share
    return spilled


def _less_carried(
    rows: list[OpenBalance], carried: Mapping[tuple[str, uuid.UUID], Decimal]
) -> list[OpenBalance]:
    kept = []
    for row in rows:
        amount = carried.get((row.kind, row.charge_id))
        if amount is None:
            kept.append(row)
        elif row.balance > amount:
            kept.append(replace(row, balance=row.balance - amount))
    return kept


def open_balances(today: date, operator: uuid.UUID | None = None) -> list[OpenBalance]:
    """Every open charge (four queries; six when a one-off charge is carried by a
    re-debit), oldest due first; with ``operator``, only the
    charges billed to that operator (a one-off charge's own operator, else its lease's).
    A charge whose returned payment was rescheduled owes only what its re-debit does not
    collect again (see the module docstring). ``today`` does not change which charges
    are open; it is taken so that every caller states the day it reads."""
    carried, insurance_collected = carried_by_redebits(operator)
    rows = (
        _rent_balances(operator)
        + _insurance_balances(operator, insurance_collected)
        + _one_off_balances(operator)
    )
    if carried:
        rows = _less_carried(rows, _spill_to_companions(rows, carried))
    return sorted(rows, key=lambda row: (row.due_on, row.kind, row.store_label, str(row.charge_id)))


def total(rows: Iterable[OpenBalance]) -> Decimal:
    return sum((row.balance for row in rows), ZERO)


# --- Arrears (D3) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArrearsFilter:
    operator: uuid.UUID | None = None
    state: str = ""
    bucket: str = ""
    sort: str = SORT_MONTHS
    descending: bool = True
    #: Why a value was not used, one sentence each.
    messages: tuple[str, ...] = ()

    def as_query(self) -> dict[str, str]:
        values = {
            "operator": str(self.operator) if self.operator else "",
            "state": self.state,
            "bucket": self.bucket,
            "sort": self.sort,
            "dir": "desc" if self.descending else "asc",
        }
        return {name: value for name, value in values.items() if value}


def parse_arrears_filter(params: Mapping[str, str]) -> ArrearsFilter:
    """The filter from query-string values; never raises. Unusable values are dropped
    with a sentence (D3: a bad filter answers 200 with a message)."""
    messages: list[str] = []
    operator: uuid.UUID | None = None
    raw_operator = (params.get("operator", "") or "").strip()
    if raw_operator:
        try:
            operator = uuid.UUID(raw_operator)
        except ValueError:
            messages.append(BAD_OPERATOR)
    state = (params.get("state", "") or "").strip()[:STATE_MAX_LENGTH]
    bucket = (params.get("bucket", "") or "").strip()
    if bucket and bucket not in BUCKETS:
        messages.append(BAD_BUCKET)
        bucket = ""
    sort = (params.get("sort", "") or "").strip() or SORT_MONTHS
    if sort not in SORTS:
        messages.append(BAD_SORT)
        sort = SORT_MONTHS
    descending = (params.get("dir", "") or "desc") != "asc"
    return ArrearsFilter(operator, state, bucket, sort, descending, tuple(messages))


@dataclass(slots=True)
class ArrearsRow:
    operator_id: uuid.UUID | None
    operator_name: str
    months_behind: int = 0
    total_owed: Decimal = ZERO
    overdue: Decimal = ZERO
    last_payment: date | None = None
    channels: set[str] = field(default_factory=set)
    stores: dict[uuid.UUID, str] = field(default_factory=dict)
    states: set[str] = field(default_factory=set)
    _months: set[date] = field(default_factory=set)
    _behind: bool = False

    @property
    def bucket(self) -> str:
        if self.months_behind >= 3:
            return BUCKET_THREE
        if self.months_behind == 2:
            return BUCKET_TWO
        if self.months_behind == 1:
            return BUCKET_ONE
        return BUCKET_NONE

    @property
    def store_labels(self) -> list[str]:
        return sorted(self.stores.values())

    @property
    def channel_labels(self) -> list[str]:
        labels = dict(PaymentChannel.choices)
        return [str(labels.get(channel, channel)) for channel in sorted(self.channels)]


@dataclass(frozen=True, slots=True)
class ArrearsReport:
    rows: list[ArrearsRow]
    #: Per bucket (in :data:`BUCKETS` order): the bucket, its label, operators, total owed.
    bucket_totals: list[tuple[str, str, int, Decimal]]
    count: int
    total_owed: Decimal


def last_payments() -> dict[uuid.UUID, date]:
    """The latest settled payment date of each operator, over every kind (one query)."""
    rows = (
        Payment.objects.filter(status=PaymentStatus.SETTLED, is_refund=False)
        .exclude(kind=PaymentKind.REFUND_DEPOSIT)
        .values(
            billed=Coalesce(
                "rent_charge__lease__operator",
                "insurance_charge__lease__operator",
                "one_off_charge__operator",
                "one_off_charge__lease__operator",
                "invoice__operator",
            )
        )
        .annotate(last=Max("settled_on"))
        .order_by()
    )
    return {row["billed"]: row["last"] for row in rows if row["billed"] and row["last"]}


def _arrears_rows(today: date) -> list[ArrearsRow]:
    by_operator: dict[uuid.UUID | None, ArrearsRow] = {}
    for item in open_balances(today):
        row = by_operator.get(item.operator_id)
        if row is None:
            row = by_operator[item.operator_id] = ArrearsRow(item.operator_id, item.operator_name)
        row.total_owed += item.balance
        if item.store_id is not None:
            row.stores[item.store_id] = item.store_label
        if item.store_state:
            row.states.add(item.store_state.upper())
        if item.channel:
            row.channels.add(item.channel)
        if not item.overdue(today):
            continue
        row.overdue += item.balance
        if item.kind == KIND_RENT:
            if item.status in BEHIND_STATUSES and item.period_month is not None:
                row._months.add(item.period_month)
                row._behind = True
        else:
            row._behind = True
    behind = [row for row in by_operator.values() if row._behind]
    payments = last_payments()
    for row in behind:
        row.months_behind = len(row._months)
        row.last_payment = payments.get(row.operator_id) if row.operator_id else None
    return behind


def _sort_key(row: ArrearsRow, sort: str) -> tuple[object, ...]:
    name = row.operator_name.casefold()
    if sort == SORT_OPERATOR:
        return (name,)
    if sort == SORT_OWED:
        return (row.total_owed, name)
    if sort == SORT_LAST_PAYMENT:
        return (row.last_payment or date.min, name)
    return (row.months_behind, row.total_owed, name)


def arrears(today: date, filters: ArrearsFilter | None = None) -> ArrearsReport:
    """Operators behind on ``today`` (see the module docstring), filtered and sorted."""
    selected = filters or ArrearsFilter()
    rows = _arrears_rows(today)
    if selected.operator is not None:
        rows = [row for row in rows if row.operator_id == selected.operator]
    if selected.state:
        wanted = selected.state.upper()
        rows = [row for row in rows if wanted in row.states]
    if selected.bucket:
        rows = [row for row in rows if row.bucket == selected.bucket]
    rows.sort(key=lambda row: _sort_key(row, selected.sort), reverse=selected.descending)
    totals = [
        (
            bucket,
            BUCKET_LABELS[bucket],
            sum(1 for row in rows if row.bucket == bucket),
            sum((row.total_owed for row in rows if row.bucket == bucket), ZERO),
        )
        for bucket in BUCKETS
    ]
    return ArrearsReport(
        rows=rows,
        bucket_totals=totals,
        count=len(rows),
        total_owed=sum((row.total_owed for row in rows), ZERO),
    )


ARREARS_CSV_HEADER = (
    "Operator",
    "Months behind",
    "Months-behind bucket",
    "Total owed (USD)",
    "Overdue (USD)",
    "Last payment date",
    "Channels",
    "Stores",
)


def arrears_csv_record(row: ArrearsRow) -> list[object]:
    return [
        row.operator_name or NO_OPERATOR,
        row.months_behind,
        BUCKET_LABELS[row.bucket],
        row.total_owed,
        row.overdue,
        row.last_payment,
        "; ".join(row.channel_labels),
        "; ".join(row.store_labels),
    ]


def arrears_csv_lines(report: ArrearsReport) -> Iterator[str]:
    """The arrears rows as CSV, one line at a time (header first); no query."""
    return CsvWriter(ARREARS_CSV_HEADER).lines(arrears_csv_record(row) for row in report.rows)


def arrears_operators() -> list[Operator]:
    """The operators the filter offers."""
    return list(Operator.objects.order_by("display_name", "id"))


# --- A/R aging (D4) ------------------------------------------------------------------

AGING_CURRENT = "current"
AGING_1_30 = "1-30"
AGING_31_60 = "31-60"
AGING_61_90 = "61-90"
AGING_OVER_90 = "90+"
#: The buckets in order, with their label: days past due 0 or less (not yet due, or due
#: today), 1 to 30, 31 to 60, 61 to 90, and 91 or more.
AGING_BUCKETS: tuple[tuple[str, str], ...] = (
    (AGING_CURRENT, "Current"),
    (AGING_1_30, "1 to 30 days"),
    (AGING_31_60, "31 to 60 days"),
    (AGING_61_90, "61 to 90 days"),
    (AGING_OVER_90, "Over 90 days"),
)


def days_past_due(item: OpenBalance, today: date) -> int:
    return (today - item.due_on).days


def aging_bucket(days: int) -> str:
    """The aging bucket of a charge ``days`` past due."""
    if days <= 0:
        return AGING_CURRENT
    if days <= 30:
        return AGING_1_30
    if days <= 60:
        return AGING_31_60
    if days <= 90:
        return AGING_61_90
    return AGING_OVER_90


def _empty_buckets() -> dict[str, Decimal]:
    return {bucket: ZERO for bucket, _ in AGING_BUCKETS}


@dataclass(slots=True)
class AgingRow:
    operator_id: uuid.UUID | None
    operator_name: str
    buckets: dict[str, Decimal] = field(default_factory=_empty_buckets)
    total: Decimal = ZERO
    count: int = 0

    @property
    def amounts(self) -> list[Decimal]:
        """The bucket amounts in :data:`AGING_BUCKETS` order."""
        return [self.buckets[bucket] for bucket, _ in AGING_BUCKETS]


@dataclass(frozen=True, slots=True)
class AgingReport:
    today: date
    rows: list[AgingRow]
    totals: AgingRow


def aging(today: date) -> AgingReport:
    """Open balances by operator and days past due on ``today``: exactly the rows of
    :func:`open_balances`, so the grand total is the dashboard's total outstanding."""
    by_operator: dict[uuid.UUID | None, AgingRow] = {}
    totals = AgingRow(None, "")
    for item in open_balances(today):
        row = by_operator.get(item.operator_id)
        if row is None:
            row = by_operator[item.operator_id] = AgingRow(item.operator_id, item.operator_name)
        bucket = aging_bucket(days_past_due(item, today))
        for target in (row, totals):
            target.buckets[bucket] += item.balance
            target.total += item.balance
            target.count += 1
    rows = sorted(
        by_operator.values(),
        key=lambda row: (
            row.operator_id is None,
            row.operator_name.casefold(),
            str(row.operator_id),
        ),
    )
    return AgingReport(today=today, rows=rows, totals=totals)


AGING_CSV_HEADER = (
    "Operator",
    "Current (USD)",
    "1 to 30 days (USD)",
    "31 to 60 days (USD)",
    "61 to 90 days (USD)",
    "Over 90 days (USD)",
    "Total (USD)",
    "Open charges",
)
AGING_TOTAL = "Total"


def aging_csv_lines(report: AgingReport) -> Iterator[str]:
    """One line per operator, then the totals; no query."""
    records: list[list[object]] = [
        [row.operator_name or NO_OPERATOR, *row.amounts, row.total, row.count]
        for row in report.rows
    ]
    totals = report.totals
    records.append([AGING_TOTAL, *totals.amounts, totals.total, totals.count])
    return CsvWriter(AGING_CSV_HEADER).lines(records)


# --- Dashboard (D1) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunSummary:
    due: int
    pending: int
    returned: int


@dataclass(frozen=True, slots=True)
class Dashboard:
    today: date
    outstanding: Decimal
    outstanding_count: int
    overdue: Decimal
    overdue_count: int
    month_start: date
    month_end: date
    paid_this_month: Decimal
    not_sent: list[Invoice]
    not_sent_count: int
    failed_attempts: list[Invoice]
    failed_attempts_count: int
    run: RunSummary
    last_reconciliation: ReconciliationRun | None
    waived_live: list[OneOffCharge]


def paid_in(start: date, end: date) -> Decimal:
    """Settled payments with ``settled_on`` in ``[start, end]`` less the settled refunds
    in that range; deposits and deposit refunds left out (one query)."""
    deposit = Q(one_off_charge__fee_type__refundable=True) | Q(kind=PaymentKind.REFUND_DEPOSIT)
    sums = (
        Payment.objects.filter(
            status=PaymentStatus.SETTLED, settled_on__gte=start, settled_on__lte=end
        )
        .exclude(deposit)
        .aggregate(
            paid=Sum("amount", filter=Q(is_refund=False)),
            refunded=Sum("amount", filter=Q(is_refund=True)),
        )
    )
    return _money(sums["paid"]) - _money(sums["refunded"])


def _with_provider_ref() -> Exists:
    return Exists(
        ExternalRef.objects.filter(
            entity_type=rent_run.ENTITY_INVOICE,
            entity_id=OuterRef("pk"),
            external_kind=rent_run.KIND_INVOICE,
            superseded_at__isnull=True,
        )
    )


def _listed(invoices: QuerySet[Invoice]) -> list[Invoice]:
    """The first :data:`DASHBOARD_LIST_LIMIT` invoices with what the list shows (one query)."""
    return list(
        invoices.select_related(
            "operator",
            "rent_charge__lease__store",
            "insurance_charge__lease__store",
            "one_off_charge__lease__store",
        )[:DASHBOARD_LIST_LIMIT]
    )


def invoice_store_label(invoice: Invoice) -> str:
    """The store of the invoice's charge, or ``""``."""
    for charge in (invoice.rent_charge, invoice.insurance_charge, invoice.one_off_charge):
        lease = getattr(charge, "lease", None) if charge is not None else None
        if lease is not None:
            return str(lease.store.external_label)
    return ""


def run_summary(today: date) -> RunSummary:
    """Today's run as the run page and the returns queue count it, in three queries."""
    counts = rent_run.due_counts(today)
    return RunSummary(
        due=counts.due, pending=counts.pending, returned=returns.open_returns().count()
    )


def dashboard(today: date) -> Dashboard:
    """Every figure of the home page (see the module docstring)."""
    rows = open_balances(today)
    overdue = [row for row in rows if row.overdue(today)]
    month_start = today.replace(day=1)
    month_end = add_months(month_start, 1) - timedelta(days=1)
    not_sent = (
        Invoice.objects.filter(internal_status__in=[InvoiceStatus.DRAFT, InvoiceStatus.READY])
        .filter(_with_provider_ref())
        .order_by("issue_date", "invoice_number")
    )
    attempted = Invoice.objects.filter(provider_status=PROVIDER_ATTEMPTED).order_by(
        "-updated_at", "invoice_number"
    )
    return Dashboard(
        today=today,
        outstanding=total(rows),
        outstanding_count=len(rows),
        overdue=total(overdue),
        overdue_count=len(overdue),
        month_start=month_start,
        month_end=month_end,
        paid_this_month=paid_in(month_start, month_end),
        not_sent=_listed(not_sent),
        not_sent_count=not_sent.count(),
        failed_attempts=_listed(attempted),
        failed_attempts_count=attempted.count(),
        run=run_summary(today),
        last_reconciliation=ReconciliationRun.objects.exclude(status=ReconciliationStatus.SKIPPED)
        .order_by("-started_at", "-id")
        .first(),
        waived_live=one_off_charges.waived_with_live_invoice(),
    )
