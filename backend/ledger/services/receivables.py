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

**Installment plans** (PLAN-012e D9, D12): a rent or one-off charge whose plan is live
(``active`` or ``attention``) is not listed: what it owes is owed through its
installments, each a one-off charge listed on its own due date (``Installment:
Installment 1 of 3: Rent 2026-10``). The installments of a cancelled plan are not listed
either: their parent is open again for what they did not collect, while the worker
voids them.

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
(:func:`dashboard`), the arrears list (:func:`arrears`) and the Aging report (:func:`aging`,
PLAN-014 G1: by store and days past due, 0 or less, 1 to 30, 31 to 60, 61 to 90, over 90;
a one-off charge without a lease under "No store" with its operator), whose grand total
without a filter is therefore the dashboard's total outstanding. Its filters: the store's
state, an operator, a brand (or none), the charge kind, and Include deposits, which adds
the deposits still owed (:func:`owed_deposits`, not in the dashboard's figure). The
state, operator and brand filters choose rows, never a row's amounts. The customer
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
pending is owed but not behind: it never puts an operator on the list by itself. The
search (``q``, PLAN-014 A5) keeps the rows where every word is in the operator's name or in
the name of one of its stores, or starts one of its stores' numbers (case-insensitive; one
query for the numbers, only when there is a word).

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

from finance.dates import month_text
from finance.us_states import state_name
from ledger.models import (
    BANK_DEBIT_LABEL,
    LIVE_PLAN_STATUSES,
    ZERO,
    InstallmentPlanStatus,
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
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    Store,
)
from ledger.services import one_off_charges, rent_run, returns, search
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
    #: What the charge is for, as a statement names it (a page's text: "Rent Oct 2026").
    description: str = ""
    #: The charge's lease; ``None`` for a one-off charge without one.
    lease_id: uuid.UUID | None = None
    #: ``description`` as an export writes it ("Rent 2026-10", DESIGN-SPEC §5); ``""`` when
    #: it is the same text (see :attr:`csv_description`).
    export_description: str = ""

    @property
    def csv_description(self) -> str:
        """What the charge is for in a CSV: ISO months."""
        return self.export_description or self.description

    def overdue(self, today: date) -> bool:
        return self.due_on < today


def _money(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _rent_description(
    period_month: date, seq: int, proration_text: str | None = None, *, export: bool = False
) -> str:
    """``Rent Oct 2026`` (a page's month, DESIGN-SPEC §5; ``Rent 2026-10`` with ``export``),
    ``(debit 2)`` for a second debit, and a prorated month's arithmetic (PLAN-012e D2)."""
    month = f"{period_month:%Y-%m}" if export else month_text(period_month)
    text = f"Rent {month}" + (f" (debit {seq})" if seq > 1 else "")
    return f"{text}: {proration_text}" if proration_text else text


def _rent_balances(operator: uuid.UUID | None) -> list[OpenBalance]:
    rows = (
        RentCharge.objects.filter(balance__gt=0)
        .exclude(status=RentChargeStatus.WAIVED)
        .exclude(installment_plans__status__in=LIVE_PLAN_STATUSES)
    )
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
        "proration_text",
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
            description=_rent_description(row["period_month"], row["seq"], row["proration_text"]),
            lease_id=row["lease"],
            export_description=_rent_description(
                row["period_month"], row["seq"], row["proration_text"], export=True
            ),
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
        balance = insurance_owed(
            _money(row["amount"]),
            paid=_money(row["paid"]),
            refunded=_money(row["refunded"]),
            collected=collected.get(row["pk"], ZERO),
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


def insurance_owed(
    amount: Decimal | None,
    *,
    paid: Decimal,
    refunded: Decimal,
    collected: Decimal,
    carried: Decimal = ZERO,
    legacy_status: str = "",
) -> Decimal:
    """What one insurance charge owes (the module docstring): nothing while it needs its
    premium or when the sheet shows it paid; else its amount less its settled payments
    (``paid``) and what its re-debits collected (``collected``), plus its settled refunds
    (``refunded``), less what its live re-debits have still to collect (``carried``);
    never below zero."""
    if amount is None or legacy_status == LegacyStatus.PAID:
        return ZERO
    return max(amount - paid + refunded - collected - carried, ZERO)


def _one_off_balances(operator: uuid.UUID | None) -> list[OpenBalance]:
    charges = (
        OneOffCharge.objects.filter(balance__gt=0)
        .exclude(status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .filter(Q(fee_type__isnull=True) | Q(fee_type__refundable=False))
        .exclude(installment_plans__status__in=LIVE_PLAN_STATUSES)
        .exclude(plan__status=InstallmentPlanStatus.CANCELLED)
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
        "source_rent_charge",
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
    if row["source_rent_charge"] is not None:
        # PLAN-012f-b D19: a rent charge's bank debit reads as one, not as a re-debit.
        label = BANK_DEBIT_LABEL
    else:
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
    for the charges that store their paid amount. A rent charge's bank debit from Add
    payment (``source_rent_charge``, PLAN-012f-b D19) carries its amount under the rent
    charge as a re-debit of a rent payment does."""
    rows = OneOffCharge.objects.filter(
        Q(source_payment__isnull=False) | Q(source_rent_charge__isnull=False),
        category=OneOffCategory.REDEBIT,
    )
    if operator is not None:
        rows = rows.filter(
            Q(operator=operator) | Q(operator__isnull=True, lease__operator=operator)
        )
    return _redebit_amounts(rows)


def insurance_redebits(
    charges: Iterable[uuid.UUID],
) -> tuple[dict[uuid.UUID, Decimal], dict[uuid.UUID, Decimal]]:
    """For the insurance charges ``charges``, in one query: per charge, what its live
    re-debits have still to collect (as :func:`carried_by_redebits` carries it) and what
    its re-debits collected; so that :func:`insurance_owed` reads the balance
    :func:`open_balances` lists."""
    carried, collected = _redebit_amounts(
        OneOffCharge.objects.filter(
            category=OneOffCategory.REDEBIT, source_payment__insurance_charge__in=list(charges)
        )
    )
    return (
        {charge: amount for (kind, charge), amount in carried.items() if kind == KIND_INSURANCE},
        collected,
    )


def _redebit_amounts(
    rows: QuerySet[OneOffCharge],
) -> tuple[dict[tuple[str, uuid.UUID], Decimal], dict[uuid.UUID, Decimal]]:
    carried: dict[tuple[str, uuid.UUID], Decimal] = {}
    insurance_collected: dict[uuid.UUID, Decimal] = {}
    for status, price, paid, rent, debited, insurance, one_off in rows.values_list(
        "status",
        "price",
        "amount_paid",
        "source_payment__rent_charge",
        "source_rent_charge",
        "source_payment__insurance_charge",
        "source_payment__one_off_charge",
    ).order_by():
        rent = rent or debited
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
    return open_balances_and_carried(today, operator)[0]


def open_balances_and_carried(
    today: date, operator: uuid.UUID | None = None
) -> tuple[list[OpenBalance], dict[tuple[str, uuid.UUID], Decimal]]:
    """:func:`open_balances`, and per (kind, charge id) what live re-debits carry of it
    (after the spill to the charges billed with it); the store page's Charges tab shows
    the same figures (PLAN-012d finding 12d-2). A carried charge that is no longer in
    the rows owes nothing."""
    carried, insurance_collected = carried_by_redebits(operator)
    rows = (
        _rent_balances(operator)
        + _insurance_balances(operator, insurance_collected)
        + _one_off_balances(operator)
    )
    spilled: dict[tuple[str, uuid.UUID], Decimal] = {}
    if carried:
        spilled = _spill_to_companions(rows, carried)
        rows = _less_carried(rows, spilled)
    ordered = sorted(
        rows, key=lambda row: (row.due_on, row.kind, row.store_label, str(row.charge_id))
    )
    return ordered, spilled


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
    #: The search's words (``q``), cleaned.
    query: str = ""

    def as_query(self) -> dict[str, str]:
        values = {
            "operator": str(self.operator) if self.operator else "",
            "state": self.state,
            "bucket": self.bucket,
            "sort": self.sort,
            "dir": "desc" if self.descending else "asc",
            "q": self.query,
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
    return ArrearsFilter(
        operator,
        state,
        bucket,
        sort,
        descending,
        tuple(messages),
        query=search.clean_query(params.get("q")),
    )


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
        .exclude(kind__in=[PaymentKind.REFUND_DEPOSIT, PaymentKind.CREDIT_REFUND])
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


def _store_numbers(rows: Iterable[ArrearsRow]) -> dict[uuid.UUID, str]:
    """The store number, as text, of each store of ``rows`` that has one (one query)."""
    ids = {store_id for row in rows for store_id in row.stores}
    found = Store.objects.filter(pk__in=ids, store_number__isnull=False).values_list(
        "pk", "store_number"
    )
    return {pk: str(number) for pk, number in found}


def _row_matches(row: ArrearsRow, word: str, numbers: Mapping[uuid.UUID, str]) -> bool:
    """``word`` is in the operator's name or a store's name, or starts a store's number."""
    key = word.casefold()
    if key in row.operator_name.casefold():
        return True
    if any(key in label.casefold() for label in row.stores.values()):
        return True
    number = search.number_prefix(word)
    return number is not None and any(
        numbers.get(store_id, "").startswith(number) for store_id in row.stores
    )


def _searched(rows: list[ArrearsRow], query: str) -> list[ArrearsRow]:
    """The rows matching every word of ``query``; ``rows`` when it has no word."""
    found = search.words(query)
    if not found or not rows:
        return rows
    numbers = _store_numbers(rows)
    return [row for row in rows if all(_row_matches(row, word, numbers) for word in found)]


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
    rows = _searched(rows, selected.query)
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


# --- Aging by store (PLAN-014 G1) -------------------------------------------------------

AGING_CURRENT = "current"
AGING_1_30 = "1_30"
AGING_31_60 = "31_60"
AGING_61_90 = "61_90"
AGING_OVER_90 = "over_90"
#: The buckets in order, with their label: days past due 0 or less (not yet due, or due
#: today), 1 to 30, 31 to 60, 61 to 90, and 91 or more. The keys are also the store Charges
#: tab's ``?bucket=`` values.
AGING_BUCKETS: tuple[tuple[str, str], ...] = (
    (AGING_CURRENT, "Current"),
    (AGING_1_30, "1 to 30 days"),
    (AGING_31_60, "31 to 60 days"),
    (AGING_61_90, "61 to 90 days"),
    (AGING_OVER_90, "Over 90 days"),
)
AGING_BUCKET_LABELS: dict[str, str] = dict(AGING_BUCKETS)
#: The Kind filter's choices (none chosen, or ``kind=all``: every kind).
AGING_KINDS: tuple[tuple[str, str], ...] = (
    (KIND_RENT, "Rent"),
    (KIND_INSURANCE, "Insurance"),
    (KIND_ONE_OFF, "One-off"),
)
AGING_KIND_ALL = "all"
#: The Brand filter's value for the stores without a brand.
AGING_NO_BRAND = "none"
#: The Include deposits box's value.
AGING_DEPOSITS_ON = "1"
NO_STORE = "No store"
BAD_STATE = "Unknown state; showing every state."
BAD_BRAND = "Unknown brand; showing every brand."
BAD_KIND = "Unknown charge kind; showing every kind."
AGING_TOTAL = "Total"


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


@dataclass(frozen=True, slots=True)
class AgingFilter:
    """The Aging report's filters (G1): the store's state (a two-letter code), an operator,
    a brand (a brand's id as text, or :data:`AGING_NO_BRAND`), a charge kind (one of
    :data:`AGING_KINDS`, ``""`` for every kind) and whether deposits still owed are added."""

    state: str = ""
    operator: uuid.UUID | None = None
    brand: str = ""
    kind: str = ""
    include_deposits: bool = False
    #: Why a value was not used, one sentence each.
    messages: tuple[str, ...] = ()

    def as_query(self) -> dict[str, str]:
        values = {
            "state": self.state,
            "operator": str(self.operator) if self.operator else "",
            "brand": self.brand,
            "kind": self.kind,
            "deposits": AGING_DEPOSITS_ON if self.include_deposits else "",
        }
        return {name: value for name, value in values.items() if value}


def parse_aging_filter(params: Mapping[str, str]) -> AgingFilter:
    """The filter from ``?state=&operator=&brand=&kind=&deposits=1``; never raises. An
    unusable value is dropped with a sentence (the page answers 200 with it)."""
    messages: list[str] = []
    state = (params.get("state", "") or "").strip().upper()
    if state and not state_name(state):
        messages.append(BAD_STATE)
        state = ""
    operator: uuid.UUID | None = None
    raw_operator = (params.get("operator", "") or "").strip()
    if raw_operator:
        try:
            operator = uuid.UUID(raw_operator)
        except ValueError:
            messages.append(BAD_OPERATOR)
    brand = (params.get("brand", "") or "").strip()
    if brand and brand != AGING_NO_BRAND:
        try:
            brand = str(uuid.UUID(brand))
        except ValueError:
            messages.append(BAD_BRAND)
            brand = ""
    kind = (params.get("kind", "") or "").strip()
    if kind == AGING_KIND_ALL:
        kind = ""
    elif kind and kind not in dict(AGING_KINDS):
        messages.append(BAD_KIND)
        kind = ""
    deposits = (params.get("deposits", "") or "").strip() == AGING_DEPOSITS_ON
    return AgingFilter(state, operator, brand, kind, deposits, tuple(messages))


def _empty_buckets() -> dict[str, Decimal]:
    return {bucket: ZERO for bucket, _ in AGING_BUCKETS}


@dataclass(slots=True)
class AgingStoreRow:
    """One store's open balances by bucket (or, for one-off charges without a lease, one
    operator's under :data:`NO_STORE`), or the report's totals."""

    store_id: uuid.UUID | None
    store_label: str
    store_state: str = ""
    store_number: int | None = None
    brand_id: uuid.UUID | None = None
    brand_name: str = ""
    #: The operators billed by the row's charges: id → full name (``None``: no operator).
    operators: dict[uuid.UUID | None, str] = field(default_factory=dict)
    buckets: dict[str, Decimal] = field(default_factory=_empty_buckets)
    total: Decimal = ZERO
    count: int = 0
    #: The part of :attr:`total` that is deposits still owed (only with Include deposits).
    deposits: Decimal = ZERO

    @property
    def amounts(self) -> list[Decimal]:
        """The bucket amounts in :data:`AGING_BUCKETS` order."""
        return [self.buckets[bucket] for bucket, _ in AGING_BUCKETS]

    @property
    def operator_names(self) -> list[str]:
        return sorted((name or NO_OPERATOR for name in self.operators.values()), key=str.casefold)

    def add(self, amount: Decimal, bucket: str, *, deposit: bool = False) -> None:
        self.buckets[bucket] += amount
        self.total += amount
        self.count += 1
        if deposit:
            self.deposits += amount

    def add_row(self, other: AgingStoreRow) -> None:
        for bucket, amount in other.buckets.items():
            self.buckets[bucket] += amount
        self.total += other.total
        self.count += other.count
        self.deposits += other.deposits


@dataclass(frozen=True, slots=True)
class AgingReport:
    #: The only date of the report: the open balances on this day.
    as_of: date
    rows: list[AgingStoreRow]
    totals: AgingStoreRow
    #: Whether deposits still owed were added (Include deposits, for every kind or one-off).
    deposits_included: bool = False


def owed_deposits() -> QuerySet[OneOffCharge]:
    """The deposits still owed: one-off charges of a refundable fee type with a balance,
    neither waived nor voided, not on a live installment plan. :func:`open_balances`
    leaves them out (money held, not revenue); they are still collected
    (:func:`ledger.services.payments_entry.open_charges`, the Aging's Include deposits)."""
    return (
        OneOffCharge.objects.filter(fee_type__refundable=True, balance__gt=0)
        .exclude(status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .exclude(installment_plans__status__in=LIVE_PLAN_STATUSES)
    )


def _deposit_balances() -> list[OpenBalance]:
    """:func:`owed_deposits` as receivables rows, in one query."""
    rows = (
        owed_deposits()
        .values(
            "pk",
            "due_date",
            "balance",
            "status",
            "lease",
            fee_name=F("fee_type__name"),
            billed=Coalesce("operator", "lease__operator"),
            billed_name=Coalesce("operator__display_name", "lease__operator__display_name"),
            store=F("lease__store"),
            store_label=F("lease__store__external_label"),
            store_state=F("lease__store__state"),
            channel=F("lease__payment_channel"),
        )
        .order_by()
    )
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
            description=f"Deposit: {row['fee_name']}",
            lease_id=row["lease"],
        )
        for row in rows
    ]


def _aging_rows(
    items: Iterable[OpenBalance], deposits: Iterable[OpenBalance], today: date
) -> list[AgingStoreRow]:
    """The rows by store, one more query for the stores' number and brand."""
    grouped: dict[tuple[str, uuid.UUID | None], AgingStoreRow] = {}
    for deposit, charges in ((False, items), (True, deposits)):
        for item in charges:
            # A one-off charge without a lease groups under "No store" with its operator.
            key = ("store", item.store_id) if item.store_id else ("operator", item.operator_id)
            row = grouped.get(key)
            if row is None:
                row = grouped[key] = (
                    AgingStoreRow(item.store_id, item.store_label, item.store_state)
                    if item.store_id
                    else AgingStoreRow(None, NO_STORE)
                )
            row.operators.setdefault(item.operator_id, item.operator_name)
            row.add(item.balance, aging_bucket(days_past_due(item, today)), deposit=deposit)
    store_ids = [row.store_id for row in grouped.values() if row.store_id is not None]
    if store_ids:
        found = {
            pk: (number, brand, brand_name)
            for pk, number, brand, brand_name in Store.objects.filter(pk__in=store_ids)
            .values_list("pk", "store_number", "brand", "brand__name")
            .order_by()
        }
        for row in grouped.values():
            if row.store_id in found:
                row.store_number, row.brand_id, brand_name = found[row.store_id]
                row.brand_name = brand_name or ""
    return sorted(grouped.values(), key=_aging_order)


def _aging_order(row: AgingStoreRow) -> tuple[object, ...]:
    """Stores by name, then the "No store" rows by operator."""
    if row.store_id is not None:
        return (False, row.store_label.casefold(), str(row.store_id))
    return (True, " ".join(row.operator_names).casefold(), str(next(iter(row.operators))))


def _aging_kept(row: AgingStoreRow, selected: AgingFilter) -> bool:
    """Whether ``row`` passes the state, operator and brand filters (a :data:`NO_STORE` row
    has no state and no brand)."""
    if selected.state and (row.store_id is None or row.store_state.upper() != selected.state):
        return False
    if selected.operator is not None and selected.operator not in row.operators:
        return False
    if selected.brand == AGING_NO_BRAND:
        return row.store_id is not None and row.brand_id is None
    if selected.brand:
        return row.store_id is not None and str(row.brand_id) == selected.brand
    return True


def aging(today: date, selected: AgingFilter | None = None) -> AgingReport:
    """The open balances on ``today`` by store and days past due (G1): exactly the rows of
    :func:`open_balances` (of the chosen kind), so that without a filter the grand total is
    the dashboard's Unpaid figure; with Include deposits (every kind, or one-off) the
    deposits still owed are added. The state, operator and brand filters choose which rows
    appear, never a row's amounts. Five queries (seven when a re-debit carries a one-off
    charge), one more with deposits, whatever the number of stores."""
    chosen = selected or AgingFilter()
    items = [item for item in open_balances(today) if not chosen.kind or item.kind == chosen.kind]
    with_deposits = chosen.include_deposits and chosen.kind in ("", KIND_ONE_OFF)
    deposits = _deposit_balances() if with_deposits else []
    rows = [row for row in _aging_rows(items, deposits, today) if _aging_kept(row, chosen)]
    totals = AgingStoreRow(None, AGING_TOTAL)
    for row in rows:
        totals.add_row(row)
    return AgingReport(as_of=today, rows=rows, totals=totals, deposits_included=with_deposits)


AGING_CSV_HEADER = (
    "Store",
    "Store number",
    "State",
    "Brand",
    "Operator",
    *(f"{label} (USD)" for _, label in AGING_BUCKETS),
    "Total (USD)",
    "Open charges",
)


def aging_csv_lines(report: AgingReport) -> Iterator[str]:
    """One line per row of the page, then the totals; no query."""
    records: list[list[object]] = [
        [
            row.store_label,
            row.store_number,
            row.store_state,
            row.brand_name,
            "; ".join(row.operator_names),
            *row.amounts,
            row.total,
            row.count,
        ]
        for row in report.rows
    ]
    totals = report.totals
    records.append([AGING_TOTAL, "", "", "", "", *totals.amounts, totals.total, totals.count])
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
    #: The stores with an open balance, and those with an overdue one (PLAN-012f D7: the
    #: Unpaid balances card's "N stores · M overdue"; a charge without a store is not counted).
    outstanding_stores: int = 0
    overdue_stores: int = 0


def paid_in(start: date, end: date) -> Decimal:
    """Settled payments with ``settled_on`` in ``[start, end]`` less the settled refunds
    in that range; deposits and deposit refunds left out, and credits applied or paid
    back (PLAN-012d D6b: money held, not received; one query)."""
    deposit = (
        Q(one_off_charge__fee_type__refundable=True)
        | Q(kind=PaymentKind.REFUND_DEPOSIT)
        # A credit applied or paid back moves money held, not money received (PLAN-012d
        # D6a, D6b).
        | Q(method=PaymentMethod.CREDIT)
        | Q(kind=PaymentKind.CREDIT_REFUND)
    )
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
        outstanding_stores=len({row.store_id for row in rows if row.store_id is not None}),
        overdue_stores=len({row.store_id for row in overdue if row.store_id is not None}),
    )
