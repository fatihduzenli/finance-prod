"""Revenue by month, payments by method, deposits versus transactions and the customer
statement (docs/qa/PLAN-011-dashboard-reports-mobile.md D4). The A/R aging lives with the
open balances it sums (:func:`ledger.services.receivables.aging`).

Read only: nothing here writes or calls the payment provider.

**The range** (:func:`parse_range`) is the P&L's: ``from`` and ``to`` are months written
``YYYY-MM``, both inclusive, read by :func:`ledger.services.pnl.parse_query` (the default
is the last three complete months and the current one, at most ``pnl.MAX_MONTHS``
months; an unusable value is replaced with a sentence, never an error). Dates are date
columns; a month is a calendar month.

**Revenue by month** (:func:`revenue_by_month`) is the P&L's row engine summed by month:
rent billed (by rent month) and collected (by settled date, less refunds); insurance
billed (by due date) and collected; other fees billed (by due date) and collected,
which include the withheld part of resolved deposits (revenue from the day it is
withheld, PLAN-010b D7), shown apart as well. Deposits held are not revenue and are
not here. Revenue is counted by **settled month**; the aging is by **due date**: a rent
charge due in July and paid in August is July's billing and August's collection.

**Payments by method** (:func:`payments_by_method`): settled payments by ``settled_on``
month and method (ACH, Zelle, wire, other), less the settled refunds of the month by
the refund's own method; deposits and deposit refunds left out, and credits applied or
paid back (PLAN-012d D6b: no money moves by a method), as on the dashboard's
"paid this month" (:func:`ledger.services.receivables.paid_in`), so a month's total is
that figure. Cards do not exist in this app; cash or check appear only as the payment
provider's mark of a manual payment, recorded here as Zelle, wire or other.

**Deposits versus transactions** (:func:`deposits_vs_transactions`): the bank deposits the
provider reported (the ``deposits`` table, by ``deposit_date``) against the settled ACH
payments and refunds of the same months (security deposits included: they reach the
bank like rent); the difference is gross deposits less net ACH. The unresolved
reconciliation findings of step (c), ``deposit_unmatched`` and
``settled_payment_undeposited``, are listed whatever their date: they are open now.

**The customer statement** (:func:`statement`): one operator, every store. Its open
charges are :func:`ledger.services.receivables.open_balances` for that operator only
(the charges billed to it); its payment history is every payment billed to it (through
its charge's lease, a one-off charge's own operator, or the invoice) whose settled date,
or initiation date while pending, is in the range: payments, refunds, and deposits shown
as held. A settled payment on a multi-line invoice of the invoice builder counts by the
lines it paid (:func:`ledger.services.payments_ledger.invoice_line_parts`): its part on a
deposit line is held, the rest paid. An archived operator has a statement like any other.

Every figure is a ``Sum`` over ``numeric(12,2)`` columns or a ``Decimal`` sum.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import Coalesce, TruncMonth

from ledger.models import (
    ZERO,
    MoneyMethod,
    OneOffCategory,
    Operator,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
)
from ledger.services import pnl, receivables
from ledger.services.csv_export import CsvWriter
from ledger.services.payments_ledger import charge_lease, invoice_line_parts
from payments.models import Deposit, MismatchKind, ReconciliationMismatch
from payments.provider import mask_identifier
from payments.webhooks.handlers.common import ENTITY_PAYMENT

TOTAL = "Total"
#: The reconciliation findings the deposits report lists (step c).
UNMATCHED_KINDS: tuple[str, ...] = (
    MismatchKind.DEPOSIT_UNMATCHED,
    MismatchKind.SETTLED_PAYMENT_UNDEPOSITED,
)


def _money(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _month(value: object) -> date:
    if not isinstance(value, date):
        raise TypeError("A month is a date.")
    return value.replace(day=1)


# --- The range ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportRange:
    from_month: date
    to_month: date
    #: Why the range differs from what was asked, one sentence each.
    messages: tuple[str, ...] = ()

    @property
    def months(self) -> list[date]:
        return pnl.PnlQuery(self.from_month, self.to_month).months

    @property
    def start(self) -> date:
        return self.from_month

    @property
    def end(self) -> date:
        """The last day of the range."""
        return pnl.month_end(self.to_month)

    def as_query(self) -> dict[str, str]:
        return {"from": f"{self.from_month:%Y-%m}", "to": f"{self.to_month:%Y-%m}"}


def parse_range(params: Mapping[str, str], today: date | None = None) -> ReportRange:
    """The range from query-string values; never raises (the P&L's bounds)."""
    query = pnl.parse_query(
        {"from": params.get("from", "") or "", "to": params.get("to", "") or ""}, today
    )
    return ReportRange(query.from_month, query.to_month, query.messages)


# --- Revenue by month -----------------------------------------------------------------


@dataclass(slots=True)
class RevenueMonth:
    month: date | None
    rent_billed: Decimal = ZERO
    rent_collected: Decimal = ZERO
    insurance_billed: Decimal = ZERO
    insurance_collected: Decimal = ZERO
    #: Fees other than insurance (setup fees, adjustments, damage, other).
    fees_billed: Decimal = ZERO
    #: Includes ``withheld_deposits``.
    fees_collected: Decimal = ZERO
    withheld_deposits: Decimal = ZERO

    @property
    def total_billed(self) -> Decimal:
        return self.rent_billed + self.insurance_billed + self.fees_billed

    @property
    def total_collected(self) -> Decimal:
        return self.rent_collected + self.insurance_collected + self.fees_collected

    def add(self, item: pnl.Figures) -> None:
        self.rent_billed += item.rent_billed
        self.rent_collected += item.rent_collected
        self.insurance_billed += item.insurance_billed
        self.insurance_collected += item.insurance_collected
        self.fees_billed += item.fees_billed - item.insurance_billed
        self.fees_collected += item.fees_collected - item.insurance_collected
        self.withheld_deposits += item.withheld_deposits


@dataclass(frozen=True, slots=True)
class Revenue:
    months: list[RevenueMonth]
    total: RevenueMonth


def revenue_by_month(selected: ReportRange) -> Revenue:
    """One line per month of the range (every month, empty or not), then the total."""
    by_month = {month: RevenueMonth(month) for month in selected.months}
    total = RevenueMonth(None)
    for row in pnl.rows(pnl.PnlQuery(selected.from_month, selected.to_month)):
        by_month[row.month].add(row)
        total.add(row)
    return Revenue(months=list(by_month.values()), total=total)


REVENUE_CSV_HEADER = (
    "Month",
    "Rent billed (USD)",
    "Rent collected (USD)",
    "Insurance billed (USD)",
    "Insurance collected (USD)",
    "Other fees billed (USD)",
    "Other fees collected (USD)",
    "Withheld deposits (USD)",
    "Total billed (USD)",
    "Total collected (USD)",
)


def _revenue_record(item: RevenueMonth) -> list[object]:
    return [
        f"{item.month:%Y-%m}" if item.month is not None else TOTAL,
        item.rent_billed,
        item.rent_collected,
        item.insurance_billed,
        item.insurance_collected,
        item.fees_billed,
        item.fees_collected,
        item.withheld_deposits,
        item.total_billed,
        item.total_collected,
    ]


def revenue_csv_lines(report: Revenue) -> Iterator[str]:
    records = [_revenue_record(item) for item in [*report.months, report.total]]
    return CsvWriter(REVENUE_CSV_HEADER).lines(records)


# --- Payments by method ---------------------------------------------------------------

#: The methods money is received by: a credit applied (PLAN-012d D6a) moves no money.
METHODS: tuple[str, ...] = tuple(MoneyMethod.values)
CARDS_NOTE = (
    "Cards do not exist in this app. A manual payment marked at the payment provider as "
    "cash or check is recorded here as Zelle, wire or other, with a note."
)


def _deposit_payment() -> Q:
    """A payment on a deposit (a refundable fee type), its refund, or a deposit refund."""
    return Q(one_off_charge__fee_type__refundable=True) | Q(kind=PaymentKind.REFUND_DEPOSIT)


def _credit_payment() -> Q:
    """A credit applied to a charge or paid back (PLAN-012d D6a, D6b): money held, not
    received or refunded by a method."""
    return Q(method=PaymentMethod.CREDIT) | Q(kind=PaymentKind.CREDIT_REFUND)


@dataclass(slots=True)
class MethodMonth:
    month: date | None
    amounts: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(METHODS, ZERO))
    #: Settled payments (not refunds) counted.
    count: int = 0

    @property
    def total(self) -> Decimal:
        return sum(self.amounts.values(), ZERO)

    @property
    def ordered(self) -> list[Decimal]:
        return [self.amounts[method] for method in METHODS]


@dataclass(frozen=True, slots=True)
class PaymentsByMethod:
    months: list[MethodMonth]
    total: MethodMonth


def payments_by_method(selected: ReportRange) -> PaymentsByMethod:
    """Settled payments less settled refunds, by month and method (one query)."""
    by_month = {month: MethodMonth(month) for month in selected.months}
    total = MethodMonth(None)
    sums = (
        Payment.objects.filter(
            status=PaymentStatus.SETTLED,
            settled_on__gte=selected.start,
            settled_on__lte=selected.end,
        )
        .exclude(_deposit_payment())
        .exclude(_credit_payment())
        .values("method", month=TruncMonth("settled_on"))
        .annotate(
            paid=Sum("amount", filter=Q(is_refund=False)),
            refunded=Sum("amount", filter=Q(is_refund=True)),
            count=Count("pk", filter=Q(is_refund=False)),
        )
        .order_by()
    )
    for item in sums:
        net = _money(item["paid"]) - _money(item["refunded"])
        line = by_month[_month(item["month"])]
        for target in (line, total):
            target.amounts[item["method"]] = target.amounts.get(item["method"], ZERO) + net
            target.count += item["count"]
    return PaymentsByMethod(months=list(by_month.values()), total=total)


PAYMENTS_BY_METHOD_CSV_HEADER = (
    "Month",
    "ACH (USD)",
    "Zelle (USD)",
    "Wire (USD)",
    "Other (USD)",
    "Total (USD)",
    "Payments",
)


def payments_by_method_csv_lines(report: PaymentsByMethod) -> Iterator[str]:
    records: list[list[object]] = [
        [f"{item.month:%Y-%m}" if item.month is not None else TOTAL, *item.ordered]
        + [item.total, item.count]
        for item in [*report.months, report.total]
    ]
    return CsvWriter(PAYMENTS_BY_METHOD_CSV_HEADER).lines(records)


# --- Deposits versus transactions -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DepositLine:
    deposit_date: date
    #: The provider's deposit identifier, masked.
    reference: str
    status: str
    gross: Decimal
    fees: Decimal
    net: Decimal


@dataclass(slots=True)
class DepositMonth:
    month: date | None
    count: int = 0
    gross: Decimal = ZERO
    fees: Decimal = ZERO
    net: Decimal = ZERO
    ach_paid: Decimal = ZERO
    ach_refunded: Decimal = ZERO
    ach_count: int = 0

    @property
    def ach_net(self) -> Decimal:
        return self.ach_paid - self.ach_refunded

    @property
    def difference(self) -> Decimal:
        """Gross deposits less net settled ACH: zero when the bank received what settled."""
        return self.gross - self.ach_net


@dataclass(frozen=True, slots=True)
class UnmatchedItem:
    found_on: date
    kind: str
    kind_label: str
    #: The record the finding names, masked.
    record: str
    detail: str
    #: The payment page to open, when the finding names one of our payments.
    payment_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class DepositsReport:
    deposits: list[DepositLine]
    months: list[DepositMonth]
    total: DepositMonth
    unmatched: list[UnmatchedItem]


def deposits_vs_transactions(selected: ReportRange) -> DepositsReport:
    """Deposits and settled ACH by month, the deposits of the range, and the open step
    (c) findings (three queries)."""
    by_month = {month: DepositMonth(month) for month in selected.months}
    total = DepositMonth(None)
    lines = []
    for row in Deposit.objects.filter(
        deposit_date__gte=selected.start, deposit_date__lte=selected.end
    ).order_by("deposit_date", "provider_deposit_id"):
        line = DepositLine(
            deposit_date=row.deposit_date,
            reference=mask_identifier(row.provider_deposit_id),
            status=row.status,
            gross=_money(row.gross),
            fees=_money(row.fees),
            net=_money(row.net),
        )
        lines.append(line)
        for target in (by_month[row.deposit_date.replace(day=1)], total):
            target.count += 1
            target.gross += line.gross
            target.fees += line.fees
            target.net += line.net
    ach = (
        Payment.objects.filter(
            status=PaymentStatus.SETTLED,
            method=PaymentMethod.ACH,
            settled_on__gte=selected.start,
            settled_on__lte=selected.end,
        )
        .values(month=TruncMonth("settled_on"))
        .annotate(
            paid=Sum("amount", filter=Q(is_refund=False)),
            refunded=Sum("amount", filter=Q(is_refund=True)),
            count=Count("pk", filter=Q(is_refund=False)),
        )
        .order_by()
    )
    for item in ach:
        for target in (by_month[_month(item["month"])], total):
            target.ach_paid += _money(item["paid"])
            target.ach_refunded += _money(item["refunded"])
            target.ach_count += item["count"]
    return DepositsReport(
        deposits=lines,
        months=list(by_month.values()),
        total=total,
        unmatched=unmatched_items(),
    )


def unmatched_items() -> list[UnmatchedItem]:
    """The unresolved step (c) findings, oldest first."""
    labels = dict(MismatchKind.choices)
    return [
        UnmatchedItem(
            found_on=row.created_at.date(),
            kind=row.kind,
            kind_label=str(labels.get(row.kind, row.kind)),
            record=mask_identifier(str(row.entity_id)),
            detail=row.detail,
            payment_id=row.entity_id if row.entity_type == ENTITY_PAYMENT else None,
        )
        for row in ReconciliationMismatch.objects.filter(
            kind__in=UNMATCHED_KINDS, resolved_at__isnull=True
        ).order_by("created_at", "id")
    ]


DEPOSITS_CSV_HEADER = (
    "Line",
    "Date",
    "Month",
    "Deposit reference (masked)",
    "Status",
    "Gross (USD)",
    "Fees (USD)",
    "Net (USD)",
    "Settled ACH payments (USD)",
    "Settled ACH refunds (USD)",
    "Difference (USD)",
    "Detail",
)
LINE_DEPOSIT = "Deposit"
LINE_MONTH = "Month"
LINE_UNMATCHED = "Unmatched"


def deposits_csv_lines(report: DepositsReport) -> Iterator[str]:
    """The deposits, then one line per month and the total, then the open findings."""
    records: list[list[object]] = [
        [LINE_DEPOSIT, line.deposit_date, f"{line.deposit_date:%Y-%m}", line.reference]
        + [line.status, line.gross, line.fees, line.net, None, None, None, ""]
        for line in report.deposits
    ]
    for item in [*report.months, report.total]:
        records.append(
            [
                LINE_MONTH if item.month is not None else TOTAL,
                None,
                f"{item.month:%Y-%m}" if item.month is not None else "",
                "",
                "",
                item.gross,
                item.fees,
                item.net,
                item.ach_paid,
                item.ach_refunded,
                item.difference,
                "",
            ]
        )
    records.extend(
        [LINE_UNMATCHED, item.found_on, "", item.record, item.kind_label]
        + [None, None, None, None, None, None, item.detail]
        for item in report.unmatched
    )
    return CsvWriter(DEPOSITS_CSV_HEADER).lines(records)


# --- The customer statement -----------------------------------------------------------

KIND_PAYMENT = "Payment"
KIND_REFUND = "Refund"
KIND_DEPOSIT_HELD = "Deposit held"
KIND_DEPOSIT_REFUND = "Deposit refund"


@dataclass(frozen=True, slots=True)
class HistoryLine:
    payment: Payment
    on: date | None
    kind: str
    store: str
    description: str


@dataclass(frozen=True, slots=True)
class Statement:
    operator: Operator
    range: ReportRange
    today: date
    open_charges: list[receivables.OpenBalance]
    open_total: Decimal
    overdue_total: Decimal
    history: list[HistoryLine]
    #: Settled payments (not deposits) of the range.
    paid: Decimal
    #: Settled refunds (not of deposits) of the range.
    refunded: Decimal
    #: Settled deposit payments less settled deposit refunds of the range.
    deposits_held: Decimal
    #: Pending payments (not deposits) of the range.
    pending: Decimal


def _billed_operator() -> Coalesce:
    return Coalesce(
        "rent_charge__lease__operator",
        "insurance_charge__lease__operator",
        "one_off_charge__operator",
        "one_off_charge__lease__operator",
        "invoice__operator",
    )


def statement_payments(operator: Operator, selected: ReportRange) -> QuerySet[Payment]:
    """The payments billed to ``operator`` whose settled date (or initiation date while
    not settled) is in the range, oldest first (one query)."""
    return (
        Payment.objects.select_related(
            "rent_charge__lease__store",
            "insurance_charge__lease__store",
            "one_off_charge__lease__store",
            "one_off_charge__fee_type",
        )
        .annotate(billed=_billed_operator(), on=Coalesce("settled_on", "initiated_on"))
        .filter(billed=operator.pk, on__gte=selected.start, on__lte=selected.end)
        .order_by("on", "created_at", "id")
    )


def _is_deposit(payment: Payment) -> bool:
    if payment.kind == PaymentKind.REFUND_DEPOSIT:
        return True
    charge = payment.one_off_charge
    return charge is not None and charge.fee_type is not None and charge.fee_type.refundable


def _history_kind(payment: Payment) -> str:
    if _is_deposit(payment):
        if payment.kind == PaymentKind.REFUND_DEPOSIT or payment.is_refund:
            return KIND_DEPOSIT_REFUND
        return KIND_DEPOSIT_HELD
    return KIND_REFUND if payment.is_refund else KIND_PAYMENT


def _charge_description(payment: Payment) -> str:
    if payment.rent_charge is not None:
        charge = payment.rent_charge
        return f"Rent {charge.period_month:%Y-%m}" + (
            f" (debit {charge.seq})" if charge.seq > 1 else ""
        )
    if payment.insurance_charge is not None:
        return f"Insurance, policy year {payment.insurance_charge.policy_year}"
    if payment.one_off_charge is not None:
        one_off = payment.one_off_charge
        label = OneOffCategory(one_off.category).label
        detail = one_off.fee_type.name if one_off.fee_type is not None else one_off.description
        return f"{label}: {detail}" if detail else str(label)
    return ""


def statement(operator: Operator, selected: ReportRange, today: date) -> Statement:
    """The statement of ``operator`` (see the module docstring); four queries."""
    charges = receivables.open_balances(today, operator=operator.pk)
    history = []
    sums: dict[str, Decimal] = defaultdict(lambda: ZERO)
    payments = statement_payments(operator, selected)
    split = invoice_line_parts(payments)
    for payment in payments:
        lease = charge_lease(payment)
        kind = _history_kind(payment)
        history.append(
            HistoryLine(
                payment=payment,
                on=getattr(payment, "on", None),
                kind=kind,
                store=lease.store.external_label if lease is not None else "",
                description=_charge_description(payment),
            )
        )
        if payment.pk in split:
            for part in split[payment.pk]:
                if part.refundable:
                    sums["held"] += part.share
                elif part.share < 0:
                    sums["refunded"] -= part.share
                else:
                    sums["paid"] += part.share
        elif payment.status == PaymentStatus.SETTLED:
            if kind == KIND_DEPOSIT_HELD:
                sums["held"] += payment.amount
            elif kind == KIND_DEPOSIT_REFUND:
                sums["held"] -= payment.amount
            elif kind == KIND_REFUND:
                sums["refunded"] += payment.amount
            else:
                sums["paid"] += payment.amount
        elif payment.status == PaymentStatus.PENDING and kind == KIND_PAYMENT:
            sums["pending"] += payment.amount
    return Statement(
        operator=operator,
        range=selected,
        today=today,
        open_charges=charges,
        open_total=receivables.total(charges),
        overdue_total=receivables.total(item for item in charges if item.overdue(today)),
        history=history,
        paid=sums["paid"],
        refunded=sums["refunded"],
        deposits_held=sums["held"],
        pending=sums["pending"],
    )


STATEMENT_CSV_HEADER = (
    "Section",
    "Date",
    "Store",
    "Description",
    "Due date",
    "Kind",
    "Status",
    "Method",
    "Amount (USD)",
    "Balance (USD)",
)
SECTION_OPEN = "Open charge"
SECTION_HISTORY = "Payment history"
TOTAL_LINES = (
    ("open_total", "Open balance"),
    ("overdue_total", "Overdue balance"),
    ("paid", "Paid in the range"),
    ("refunded", "Refunded in the range"),
    ("deposits_held", "Deposits held from the range"),
    ("pending", "Pending in the range"),
)


def statement_csv_lines(report: Statement) -> Iterator[str]:
    """The open charges, the payment history, then the totals."""
    kinds = dict(
        (
            (receivables.KIND_RENT, "Rent"),
            (receivables.KIND_INSURANCE, "Insurance"),
            (receivables.KIND_ONE_OFF, "One-off charge"),
        )
    )
    records: list[list[object]] = [
        [SECTION_OPEN, None, item.store_label, item.description, item.due_on]
        + [kinds[item.kind], item.status, "", None, item.balance]
        for item in report.open_charges
    ]
    records.extend(
        [SECTION_HISTORY, line.on, line.store, line.description, None, line.kind]
        + [line.payment.status, line.payment.method, line.payment.amount, None]
        for line in report.history
    )
    for name, label in TOTAL_LINES:
        amount: Decimal = getattr(report, name)
        records.append([TOTAL, None, "", label, None, "", "", "", amount, None])
    return CsvWriter(STATEMENT_CSV_HEADER).lines(records)
