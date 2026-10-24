"""Profit and loss by store, operator and month (docs/qa/PLAN-010b-setup-fees-store-costs-pnl.md
D11).

Read only: nothing here writes or calls the payment provider.

**The query** (:func:`parse_query`) comes from the query string: ``from`` and ``to`` are
months written ``YYYY-MM`` (both inclusive), ``group`` is ``store``, ``operator`` or
``month``, ``detail=1`` lists the underlying rows. The default range is the last three
complete months plus the current one. A month that cannot be read gives the default
range, a ``to`` before ``from`` is swapped, a range longer than :data:`MAX_MONTHS` keeps
its last :data:`MAX_MONTHS` months; each with a sentence in ``PnlQuery.messages``, never
an error. A month is a calendar month; "today" is the date in the project's
``TIME_ZONE`` (every date read here is a date column, so no instant is converted).

**One row per (store, operator, month)** (:func:`rows`), the operator being the lease's
(``None`` when the lease has none, and for a cost-only row); a one-off charge's is the
operator it bills, and a one-off charge without a lease is on a row without a store
(:data:`NO_STORE`), which never carries store costs. A row exists when a lease
of that operator on that store is active on any day of the month, or any figure below
is not zero, or the month is cost-only. A lease is active from ``started_on`` (or
always, when empty) to ``ended_on`` (or open); a prospective lease, and an ended lease
with no end date, is never active. The columns:

- **rent billed**: rent charges by ``period_month``, except waived ones (the only rent
  charge status that is not a bill; there is no voided or cancelled status), plus
  one-off charges of category ``arrears`` (rent owed, PLAN-010d D7), neither waived nor
  voided, by ``due_date`` month;
- **rent collected**: settled ``rent`` payments, and settled ``one_off`` payments on
  ``arrears`` charges, by ``settled_on`` month, minus the settled refunds of them by
  their own ``settled_on`` month;
- **fees billed**: one-off charges of every other category (``setup`` of a
  non-refundable fee type, ``adjustment``, ``damage``, ``other``), neither waived nor
  voided, by ``due_date`` month, plus insurance charges with an amount, by ``due_on``
  month, plus the withheld part of resolved deposits less its offset part, by
  ``resolved_on`` month (ADR-0012 §7, PLAN-012b-b D12: the deductions the deposit
  covers are billed and collected in that month; the rest is the "Lease-end balance"
  charge, billed by its due date), so billed is at least collected for the month;
- **fees collected**: settled ``one_off`` payments on those charges and ``insurance``
  payments by ``settled_on`` month minus their settled refunds, plus the withheld part
  of resolved deposits by ``resolved_on`` month less any part offset against the
  lease's open charges at its end (PLAN-012e D5(d): those payments count where they
  paid) (``withheld_deposits``, shown apart as well); the insurance parts of fees
  billed and collected are also kept apart (``insurance_billed``,
  ``insurance_collected``) for the revenue report (PLAN-011 D4);
- **landlord rent** and **other costs**: the live store cost row in effect on the
  month's first day (a superseded row, PLAN-010c D10, counts for nothing); none is
  "unknown" (``None``), never zero. An empty other cost on a row in effect is zero;
- **fee costs**: the ``cost`` of the month's non-refundable setup charges by
  ``due_date`` (waived ones included: the item was bought all the same; the other
  categories have no cost), plus the
  premium ``cost`` of the policy year of each insurance charge billed that month; any
  missing cost (a null cost, a policy year with no premium or a null premium cost)
  makes the whole figure unknown;
- **margins** on a billed basis (rent billed + fees billed - landlord rent - other
  costs - fee costs) and on a collected basis (the collected figures, the same costs);
  unknown when any cost is unknown, and :attr:`Figures.unknown` names which.

**Store costs are counted once per store and month**, on one row: the operator of the
lease active on the month's first day (the earliest started when several), else of the
first lease active during the month, else a cost-only row; when the store has neither
a lease active nor a cost-only month but has figures (a late payment of an old lease),
on the first of those rows. The other rows of that store and month carry zero store
costs (``carries_store_costs`` is false).

**Cost-only**: a month in which the store has a cost row in effect on any day (or a
cost row whose ``landlord_lease_end`` has not passed by the month's first day) and no
lease active on any day. Its row has no operator and is flagged.

**Re-debits and NSF fees** (PLAN-011b D5): an ``nsf_fee`` charge is a fee like any
other (billed by due date). A ``redebit`` charge is never billed (the charge its returned
payment was for is billed already); the settled payments on it are collected, by
``settled_on`` month: the part that pays the re-debit's price (payments in settled
order, refunds taken back from that part first) counts as **rent collected** when the
re-collected payment was rent (a rent payment, a payment on an ``arrears`` charge, or on a
re-debit of either, two levels deep) and as **fees collected** otherwise; the part beyond
the price pays the NSF fee and counts as fees collected (:func:`_add_redebits`, one
query).

**Multi-line invoices** (the invoice builder, PLAN-012 D6): each line is a one-off charge
billed by its own category as above; a payment on the invoice is a payment of its first
line's charge, and is collected split as it pays the lines, in line order, each part by
its line's category (:func:`_add_invoice_lines`, two queries). A **Rent** line
(PLAN-012b-b D7) bills the month's rent charge, billed by ``period_month`` as any rent;
its part of a payment is rent collected, in the payment's settled month, on the row of
the lease it bills; a deposit line's part is counted nowhere.

**Installment plans** (PLAN-012e D12): the money of a split charge is counted once,
under its parent. The parent stays billed as it was; an ``installment`` charge is never
billed. A settled payment on an installment (less its settled refunds) is collected
under the parent's category in the month the payment settled (``settled_on``), like
every other payment, and a refund in its own ``settled_on`` month: as **rent collected**
when the parent is a rent charge or an ``arrears`` charge, and as fees collected
otherwise (:func:`_add_installments`, one query). A re-debit of a returned installment
payment re-collects rent when the installment's parent is rent.

**Deposits** (refundable fee types) are counted nowhere, except their withheld part as
above and :func:`deposits_held` (the page footer).

Everything is computed with a fixed number of grouped queries, whatever the number of
stores, leases or months (:func:`rows`); the arithmetic is ``Decimal``.

**The CSV** (:func:`csv_lines`): one header row, then the rows the page shows (the
groups, or with ``detail=1`` the underlying rows), then a ``total`` row. ``Level`` says
which. Written by :class:`ledger.services.csv_export.CsvWriter`: money is plain with two
decimals and text cells are formula-safe. An unknown amount is written as the empty
string and its ``... known`` column says ``no``; a known one says ``yes``.
``Cost-only months`` counts the cost-only rows in the line (0 or 1 for a detail row);
``Unknown inputs`` names the unknown costs.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from finance.dates import month_text
from ledger.models import (
    ZERO,
    CollectionStatus,
    DepositResolution,
    InsuranceCharge,
    InsurancePremium,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    Store,
    StoreCost,
)
from ledger.services.csv_export import CsvWriter
from ledger.services.payments_ledger import (
    RENT_LINE,
    deposits_released,
    invoice_line_parts,
    multi_line_charges,
    on_multi_line_invoice,
)

#: The longest range shown, in months.
MAX_MONTHS = 36
#: The default range: this many complete months before the current one, and the current.
DEFAULT_PAST_MONTHS = 3
MIN_YEAR = 2000
MAX_YEAR = 2100

GROUP_STORE = "store"
GROUP_OPERATOR = "operator"
GROUP_MONTH = "month"
GROUP_CHOICES: tuple[tuple[str, str], ...] = (
    (GROUP_STORE, "Store"),
    (GROUP_OPERATOR, "Operator"),
    (GROUP_MONTH, "Month"),
)
NO_OPERATOR = "No operator"
#: The store label of a row of one-off charges that have no lease.
NO_STORE = "No store"

#: The figures that can be unknown, as named on the page and in the CSV.
LANDLORD_RENT = "landlord rent"
OTHER_COSTS = "other costs"
FEE_COSTS = "fee costs"

# --- Fixed sentences ------------------------------------------------------------------

UNREADABLE = "The months could not be read (choose a month); showing {start} to {end}."
SWAPPED = "The end month was before the start month; showing {start} to {end}."
CAPPED = "At most {limit} months are shown; showing {start} to {end}."
BAD_GROUP = "Unknown grouping; grouped by store."

#: ASCII digits only: ``\d`` would accept other scripts' digits.
_MONTH = re.compile(r"\A([0-9]{4})-([0-9]{2})\Z")


# --- Months ---------------------------------------------------------------------------


def add_months(month: date, count: int) -> date:
    """The first day of the month ``count`` months after ``month`` (before, if negative)."""
    index = month.year * 12 + month.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def month_end(month: date) -> date:
    return add_months(month, 1) - timedelta(days=1)


def _span(start: date, end: date) -> int:
    """The number of months from ``start`` to ``end``, both included."""
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _label(month: date) -> str:
    """The month as a URL, a query string and the CSV write it: ``2026-07``."""
    return f"{month:%Y-%m}"


def label_for_screen(month: date) -> str:
    """The month as a page reads it (DESIGN-SPEC §5): ``Jul 2026``."""
    return month_text(month)


def _parse_month(raw: str) -> date | None:
    match = _MONTH.match(raw.strip())
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not (MIN_YEAR <= year <= MAX_YEAR and 1 <= month <= 12):
        return None
    return date(year, month, 1)


# --- The query ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PnlQuery:
    from_month: date
    to_month: date
    group: str = GROUP_STORE
    detail: bool = False
    #: Why the range or grouping differs from what was asked, one sentence each.
    messages: tuple[str, ...] = ()

    @property
    def months(self) -> list[date]:
        return [
            add_months(self.from_month, n) for n in range(_span(self.from_month, self.to_month))
        ]

    @property
    def end(self) -> date:
        """The last day of the range."""
        return month_end(self.to_month)

    def as_query(self) -> dict[str, str]:
        values = {
            "from": _label(self.from_month),
            "to": _label(self.to_month),
            "group": self.group,
        }
        if self.detail:
            values["detail"] = "1"
        return values


def default_range(today: date | None = None) -> tuple[date, date]:
    current = (today or timezone.localdate()).replace(day=1)
    return add_months(current, -DEFAULT_PAST_MONTHS), current


def parse_query(params: Mapping[str, str], today: date | None = None) -> PnlQuery:
    """The query from query-string values; never raises (see the module docstring)."""
    default_from, default_to = default_range(today)
    raw_from, raw_to = params.get("from", "") or "", params.get("to", "") or ""
    messages: list[str] = []
    start = _parse_month(raw_from) if raw_from else None
    end = _parse_month(raw_to) if raw_to else None
    if (raw_from and start is None) or (raw_to and end is None):
        start, end = default_from, default_to
        messages.append(UNREADABLE.format(start=label_for_screen(start), end=label_for_screen(end)))
    if end is None:
        end = default_to
    if start is None:
        start = add_months(end, -DEFAULT_PAST_MONTHS)
    if end < start:
        start, end = end, start
        messages.append(SWAPPED.format(start=label_for_screen(start), end=label_for_screen(end)))
    if _span(start, end) > MAX_MONTHS:
        start = add_months(end, -(MAX_MONTHS - 1))
        messages.append(
            CAPPED.format(
                limit=MAX_MONTHS, start=label_for_screen(start), end=label_for_screen(end)
            )
        )
    group = params.get("group", "") or GROUP_STORE
    if group not in dict(GROUP_CHOICES):
        group = GROUP_STORE
        messages.append(BAD_GROUP)
    return PnlQuery(
        from_month=start,
        to_month=end,
        group=group,
        detail=params.get("detail") == "1",
        messages=tuple(messages),
    )


# --- Rows -----------------------------------------------------------------------------


def _sum_known(values: Iterable[Decimal | None]) -> Decimal | None:
    total = ZERO
    for value in values:
        if value is None:
            return None
        total += value
    return total


@dataclass(frozen=True, slots=True)
class Figures:
    """The money columns of a row, a group or the total."""

    rent_billed: Decimal = ZERO
    rent_collected: Decimal = ZERO
    fees_billed: Decimal = ZERO
    #: Includes ``withheld_deposits`` and ``insurance_collected``.
    fees_collected: Decimal = ZERO
    withheld_deposits: Decimal = ZERO
    #: The insurance part of ``fees_billed`` and ``fees_collected`` (PLAN-011 D4).
    insurance_billed: Decimal = ZERO
    insurance_collected: Decimal = ZERO
    #: ``None``: unknown.
    landlord_rent: Decimal | None = ZERO
    other_costs: Decimal | None = ZERO
    fee_costs: Decimal | None = ZERO

    @property
    def costs(self) -> Decimal | None:
        return _sum_known((self.landlord_rent, self.other_costs, self.fee_costs))

    @property
    def margin_billed(self) -> Decimal | None:
        costs = self.costs
        return None if costs is None else self.rent_billed + self.fees_billed - costs

    @property
    def margin_collected(self) -> Decimal | None:
        costs = self.costs
        return None if costs is None else self.rent_collected + self.fees_collected - costs

    @property
    def unknown(self) -> tuple[str, ...]:
        """The inputs that are unknown, which make both margins unknown."""
        named = (
            (LANDLORD_RENT, self.landlord_rent),
            (OTHER_COSTS, self.other_costs),
            (FEE_COSTS, self.fee_costs),
        )
        return tuple(name for name, value in named if value is None)


@dataclass(frozen=True, slots=True)
class PnlRow(Figures):
    store_id: uuid.UUID | None = None
    store: str = ""
    operator_id: uuid.UUID | None = None
    operator: str = NO_OPERATOR
    month: date = date.min
    #: No lease active on any day of the month while the store is held (D11).
    cost_only: bool = False
    #: The store's landlord rent and other costs for the month are on this row.
    carries_store_costs: bool = False


@dataclass(frozen=True, slots=True)
class PnlGroup(Figures):
    label: str = ""
    rows: tuple[PnlRow, ...] = ()

    @property
    def cost_only_count(self) -> int:
        return sum(1 for row in self.rows if row.cost_only)


def _sum(rows: Iterable[Figures]) -> dict[str, Any]:
    items = list(rows)

    def total(name: str) -> Decimal:
        return sum((getattr(item, name) for item in items), ZERO)

    def known(name: str) -> Decimal | None:
        return _sum_known(getattr(item, name) for item in items)

    return {
        "rent_billed": total("rent_billed"),
        "rent_collected": total("rent_collected"),
        "fees_billed": total("fees_billed"),
        "fees_collected": total("fees_collected"),
        "withheld_deposits": total("withheld_deposits"),
        "insurance_billed": total("insurance_billed"),
        "insurance_collected": total("insurance_collected"),
        "landlord_rent": known("landlord_rent"),
        "other_costs": known("other_costs"),
        "fee_costs": known("fee_costs"),
    }


@dataclass(slots=True)
class _Acc:
    rent_billed: Decimal = ZERO
    rent_collected: Decimal = ZERO
    fees_billed: Decimal = ZERO
    fees_collected: Decimal = ZERO
    withheld_deposits: Decimal = ZERO
    insurance_billed: Decimal = ZERO
    insurance_collected: Decimal = ZERO
    fee_costs: Decimal = ZERO
    fee_costs_unknown: bool = False
    cost_only: bool = False
    carries: bool = False


type _Key = tuple[uuid.UUID | None, uuid.UUID | None, date]


@dataclass(slots=True)
class _LeaseSpan:
    store: uuid.UUID
    operator: uuid.UUID | None
    started_on: date | None
    ended_on: date | None
    pk: uuid.UUID

    def active_in(self, month: date) -> bool:
        return (self.started_on is None or self.started_on <= month_end(month)) and (
            self.ended_on is None or self.ended_on >= month
        )

    def active_on(self, day: date) -> bool:
        return (self.started_on is None or self.started_on <= day) and (
            self.ended_on is None or self.ended_on >= day
        )


@dataclass(slots=True)
class _CostRow:
    landlord_rent: Decimal
    other_monthly_cost: Decimal | None
    landlord_lease_end: date | None
    effective_from: date
    effective_to: date | None

    def in_effect_on(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_to is None or self.effective_to >= day
        )

    def holds(self, month: date) -> bool:
        """In effect on any day of the month, or the landlord lease still runs then."""
        last = month_end(month)
        if self.effective_from > last:
            return False
        in_effect = self.effective_to is None or self.effective_to >= month
        lease_runs = self.landlord_lease_end is not None and self.landlord_lease_end >= month
        return in_effect or lease_runs


def _money_or_zero(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _payments(
    kind: str,
    charge: str,
    start: date,
    end: date,
    *filters: Q,
    operator: Any = None,
    **extra: Any,
) -> Iterator[Any]:
    """Settled payments of one kind on one charge type, and their settled refunds, summed
    by the charge's store and operator (``operator``, else the lease's) and the
    ``settled_on`` month."""
    return iter(
        Payment.objects.filter(
            *filters,
            kind=kind,
            status=PaymentStatus.SETTLED,
            settled_on__gte=start,
            settled_on__lte=end,
            **{f"{charge}__isnull": False},
            **extra,
        )
        .values(
            store=F(f"{charge}__lease__store"),
            operator=operator if operator is not None else F(f"{charge}__lease__operator"),
            month=TruncMonth("settled_on"),
        )
        .annotate(
            paid=Sum("amount", filter=Q(is_refund=False)),
            refunded=Sum("amount", filter=Q(is_refund=True)),
        )
        .order_by()
    )


def _month(value: object) -> date:
    """A truncated month as a date (the backends agree on the type for a date column)."""
    if not isinstance(value, date):
        raise TypeError("A month is a date.")
    return value.replace(day=1)


def _key(item: Mapping[str, Any]) -> _Key:
    return (item["store"], item["operator"], _month(item["month"]))


def _add_billed_and_collected(acc: dict[_Key, _Acc], start: date, end: date) -> None:
    """Rent and fees billed and collected, withheld deposits and fee costs (8 queries)."""
    rent_charges = (
        RentCharge.objects.filter(period_month__gte=start, period_month__lte=end)
        .exclude(status=RentChargeStatus.WAIVED)
        .values(store=F("lease__store"), operator=F("lease__operator"), month=F("period_month"))
        .annotate(total=Sum("amount_due"))
        .order_by()
    )
    for rent in rent_charges:
        acc[_key(rent)].rent_billed += _money_or_zero(rent["total"])

    one_off_operator = Coalesce("one_off_charge__operator", "one_off_charge__lease__operator")
    not_deposit = Q(one_off_charge__fee_type__isnull=True) | Q(
        one_off_charge__fee_type__refundable=False
    )
    arrears = Q(one_off_charge__category=OneOffCategory.ARREARS)
    redebit = Q(one_off_charge__category=OneOffCategory.REDEBIT)
    installment = Q(one_off_charge__category=OneOffCategory.INSTALLMENT)
    # A payment on a multi-line invoice is split by its lines (_add_invoice_lines).
    one_line = ~Q(one_off_charge__in=multi_line_charges())
    rent_payments = (
        _payments(PaymentKind.RENT, "rent_charge", start, end),
        _payments(
            PaymentKind.ONE_OFF,
            "one_off_charge",
            start,
            end,
            arrears,
            one_line,
            operator=one_off_operator,
        ),
    )
    for source in rent_payments:
        for paid in source:
            net = _money_or_zero(paid["paid"]) - _money_or_zero(paid["refunded"])
            acc[_key(paid)].rent_collected += net
    for paid in _payments(PaymentKind.INSURANCE, "insurance_charge", start, end):
        net = _money_or_zero(paid["paid"]) - _money_or_zero(paid["refunded"])
        acc[_key(paid)].fees_collected += net
        acc[_key(paid)].insurance_collected += net
    for paid in _payments(
        PaymentKind.ONE_OFF,
        "one_off_charge",
        start,
        end,
        not_deposit,
        ~arrears,
        ~redebit,
        ~installment,
        one_line,
        operator=one_off_operator,
    ):
        net = _money_or_zero(paid["paid"]) - _money_or_zero(paid["refunded"])
        acc[_key(paid)].fees_collected += net

    setup = OneOffCategory.SETUP
    one_off = (
        OneOffCharge.objects.filter(due_date__gte=start, due_date__lte=end)
        .filter(Q(fee_type__isnull=True) | Q(fee_type__refundable=False))
        .values(
            "category",
            store=F("lease__store"),
            # Not ``operator``: the model has a field of that name.
            billed_operator=Coalesce("operator", "lease__operator"),
            month=TruncMonth("due_date"),
        )
        .annotate(
            billed=Sum(
                "price",
                filter=~Q(status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED]),
            ),
            cost_total=Sum("cost", filter=Q(category=setup)),
            missing=Count("pk", filter=Q(category=setup, cost__isnull=True)),
        )
        .order_by()
    )
    for charge in one_off:
        row = acc[(charge["store"], charge["billed_operator"], _month(charge["month"]))]
        if charge["category"] == OneOffCategory.ARREARS:
            row.rent_billed += _money_or_zero(charge["billed"])
            continue
        if charge["category"] in (OneOffCategory.REDEBIT, OneOffCategory.INSTALLMENT):
            # Collected only: what it collects was billed on its own charge (PLAN-011b
            # D5) or on its plan's parent (PLAN-012e D12).
            continue
        row.fees_billed += _money_or_zero(charge["billed"])
        row.fee_costs += _money_or_zero(charge["cost_total"])
        row.fee_costs_unknown |= bool(charge["missing"])

    _add_insurance(acc, start, end)
    _add_redebits(acc, start, end)
    _add_invoice_lines(acc, start, end)
    _add_installments(acc, start, end)

    # PLAN-012e D5(d): the offset part of a withheld deposit paid the lease's open
    # charges, and those payments are counted where they were paid; only the rest of
    # the withheld amount is revenue here. ADR-0012 §7 (PLAN-012b-b D12): that rest is
    # the deductions the deposit covers, billed and collected in the resolution month.
    withheld = (
        DepositResolution.objects.filter(
            resolved_on__gte=start,
            resolved_on__lte=end,
            withheld_amount__gt=F("offset_amount"),
        )
        .values(
            store=F("setup_charge__lease__store"),
            operator=Coalesce("setup_charge__operator", "setup_charge__lease__operator"),
            month=TruncMonth("resolved_on"),
        )
        .annotate(total=Sum(F("withheld_amount") - F("offset_amount")))
        .order_by()
    )
    for resolution in withheld:
        row = acc[_key(resolution)]
        amount = _money_or_zero(resolution["total"])
        row.withheld_deposits += amount
        row.fees_billed += amount
        row.fees_collected += amount


def _add_invoice_lines(acc: dict[_Key, _Acc], start: date, end: date) -> None:
    """Settled payments on multi-line invoices of the invoice builder (PLAN-012 D6),
    split as they pay the lines (:func:`ledger.services.payments_ledger.invoice_line_parts`).
    Each part counts by its line's category as the other one-off payments do:
    ``arrears`` as rent collected, a deposit (a refundable fee type) nowhere, anything
    else as fees collected; a Rent line's part (PLAN-012b-b D7) as rent collected on the
    row of the lease it bills (5 queries)."""
    settled = Payment.objects.filter(
        on_multi_line_invoice(), settled_on__gte=start, settled_on__lte=end
    )
    places = {
        row["pk"]: row
        for row in settled.values(
            "pk",
            "settled_on",
            store=F("one_off_charge__lease__store"),
            operator=Coalesce("one_off_charge__operator", "one_off_charge__lease__operator"),
        )
    }
    for payment, parts in invoice_line_parts(settled).items():
        row = places[payment]
        settled_on: date | None = row["settled_on"]
        if settled_on is None:
            continue
        month = settled_on.replace(day=1)
        target = acc[(row["store"], row["operator"], month)]
        for part in parts:
            if part.category == RENT_LINE:
                acc[(part.store_id, part.operator_id, month)].rent_collected += part.share
            elif part.category == OneOffCategory.ARREARS:
                target.rent_collected += part.share
            elif not part.refundable:
                target.fees_collected += part.share


def _add_installments(acc: dict[_Key, _Acc], start: date, end: date) -> None:
    """Settled payments on installments, less their settled refunds, by ``settled_on``
    month and under the category of the plan's parent (PLAN-012e D12; see the module
    docstring). One query."""
    plan = "one_off_charge__plan"
    rows = (
        Payment.objects.filter(
            kind=PaymentKind.ONE_OFF,
            status=PaymentStatus.SETTLED,
            settled_on__gte=start,
            settled_on__lte=end,
            one_off_charge__category=OneOffCategory.INSTALLMENT,
        )
        .values(
            store=F("one_off_charge__lease__store"),
            operator=Coalesce("one_off_charge__operator", "one_off_charge__lease__operator"),
            month=TruncMonth("settled_on"),
            rent_parent=F(f"{plan}__rent_charge"),
            parent_category=F(f"{plan}__one_off_charge__category"),
        )
        .annotate(
            paid=Sum("amount", filter=Q(is_refund=False)),
            refunded=Sum("amount", filter=Q(is_refund=True)),
        )
        .order_by()
    )
    for row in rows:
        net = _money_or_zero(row["paid"]) - _money_or_zero(row["refunded"])
        target = acc[_key(row)]
        if row["rent_parent"] is not None or row["parent_category"] == OneOffCategory.ARREARS:
            target.rent_collected += net
        else:
            target.fees_collected += net


def _rent_like(kind: object, category: object) -> bool:
    """A payment of rent: of kind ``rent``, or on an ``arrears`` one-off charge."""
    return kind == PaymentKind.RENT or (
        kind == PaymentKind.ONE_OFF and category == OneOffCategory.ARREARS
    )


def _re_collects_rent(row: Mapping[str, Any]) -> bool:
    """Whether a re-debit charge collects rent again: its returned payment was rent, or
    was a payment on a re-debit of rent (two levels; deeper counts as fees), or on an
    installment of a rent charge or of an ``arrears`` charge (PLAN-012e D12)."""
    if _rent_like(row["kind_1"], row["category_1"]):
        return True
    if row["category_1"] == OneOffCategory.INSTALLMENT:
        return row["plan_rent"] is not None or row["plan_category"] == OneOffCategory.ARREARS
    return row["category_1"] == OneOffCategory.REDEBIT and _rent_like(
        row["kind_2"], row["category_2"]
    )


def _add_redebits(acc: dict[_Key, _Acc], start: date, end: date) -> None:
    """Settled payments on re-debit charges, split between the re-debit's price (rent or
    fees by what it re-collects) and the NSF fee beyond it (fees); see the module
    docstring. Every settled payment up to ``end`` is read, in settled order, so that the
    split of a payment in the range knows what the earlier ones paid (1 query)."""
    source = "one_off_charge__source_payment"
    payments = (
        Payment.objects.filter(
            kind=PaymentKind.ONE_OFF,
            status=PaymentStatus.SETTLED,
            settled_on__lte=end,
            one_off_charge__category=OneOffCategory.REDEBIT,
        )
        .values(
            "amount",
            "is_refund",
            "settled_on",
            charge=F("one_off_charge"),
            price=F("one_off_charge__price"),
            store=F("one_off_charge__lease__store"),
            operator=Coalesce("one_off_charge__operator", "one_off_charge__lease__operator"),
            kind_1=F(f"{source}__kind"),
            category_1=F(f"{source}__one_off_charge__category"),
            kind_2=F(f"{source}__one_off_charge__source_payment__kind"),
            category_2=F(f"{source}__one_off_charge__source_payment__one_off_charge__category"),
            plan_rent=F(f"{source}__one_off_charge__plan__rent_charge"),
            plan_category=F(f"{source}__one_off_charge__plan__one_off_charge__category"),
        )
        .order_by("settled_on", "created_at", "id")
    )
    paid: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for row in payments:
        price = _money_or_zero(row["price"])
        amount = _money_or_zero(row["amount"])
        signed = -amount if row["is_refund"] else amount
        before = paid[row["charge"]]
        after = before + signed
        paid[row["charge"]] = after
        on_price = min(max(after, ZERO), price) - min(max(before, ZERO), price)
        beyond = signed - on_price
        settled_on = row["settled_on"]
        if settled_on is None or settled_on < start:
            continue
        target = acc[(row["store"], row["operator"], settled_on.replace(day=1))]
        if _re_collects_rent(row):
            target.rent_collected += on_price
        else:
            target.fees_collected += on_price
        target.fees_collected += beyond


def _add_insurance(acc: dict[_Key, _Acc], start: date, end: date) -> None:
    """Insurance charges billed, and the premium cost of each (2 queries)."""
    premium_costs: dict[int, Decimal | None] = {
        year: (Decimal(str(cost)) if cost is not None else None)
        for year, cost in InsurancePremium.objects.values_list("policy_year", "cost")
    }
    charges = (
        InsuranceCharge.objects.filter(due_on__gte=start, due_on__lte=end, amount__isnull=False)
        .values(
            "policy_year",
            store=F("lease__store"),
            operator=F("lease__operator"),
            month=TruncMonth("due_on"),
        )
        .annotate(billed=Sum("amount"), count=Count("pk"))
        .order_by()
    )
    for charge in charges:
        row = acc[_key(charge)]
        row.fees_billed += _money_or_zero(charge["billed"])
        row.insurance_billed += _money_or_zero(charge["billed"])
        cost = premium_costs.get(charge["policy_year"])
        if cost is None:
            row.fee_costs_unknown = True
        else:
            row.fee_costs += cost * charge["count"]


def _lease_spans(start: date, end: date) -> dict[uuid.UUID, list[_LeaseSpan]]:
    """The leases that may be active in the range, by store (1 query)."""
    spans: dict[uuid.UUID, list[_LeaseSpan]] = defaultdict(list)
    for store, operator, started_on, ended_on, pk in (
        Lease.objects.exclude(collection_status=CollectionStatus.PROSPECTIVE)
        .exclude(collection_status=CollectionStatus.ENDED, ended_on__isnull=True)
        .filter(Q(started_on__isnull=True) | Q(started_on__lte=end))
        .filter(Q(ended_on__isnull=True) | Q(ended_on__gte=start))
        .values_list("store_id", "operator_id", "started_on", "ended_on", "id")
    ):
        spans[store].append(_LeaseSpan(store, operator, started_on, ended_on, pk))
    return spans


def _cost_rows(start: date, end: date) -> dict[uuid.UUID, list[_CostRow]]:
    """The live store cost rows that may matter in the range, by store (1 query). A
    superseded row (PLAN-010c D10) counts for nothing."""
    rows_by_store: dict[uuid.UUID, list[_CostRow]] = defaultdict(list)
    for store, rent, other, lease_end, effective_from, effective_to in (
        StoreCost.objects.filter(superseded_at__isnull=True, effective_from__lte=end)
        .filter(
            Q(effective_to__isnull=True)
            | Q(effective_to__gte=start)
            | Q(landlord_lease_end__gte=start)
        )
        .values_list(
            "store_id",
            "landlord_rent",
            "other_monthly_cost",
            "landlord_lease_end",
            "effective_from",
            "effective_to",
        )
    ):
        rows_by_store[store].append(
            _CostRow(
                Decimal(str(rent)),
                Decimal(str(other)) if other is not None else None,
                lease_end,
                effective_from,
                effective_to,
            )
        )
    return rows_by_store


@dataclass(slots=True)
class _StoreCosts:
    landlord_rent: Decimal | None
    other_costs: Decimal | None


def _place_store_costs(
    acc: dict[_Key, _Acc],
    months: list[date],
    spans: Mapping[uuid.UUID, list[_LeaseSpan]],
    costs: Mapping[uuid.UUID, list[_CostRow]],
    names: Mapping[uuid.UUID | None, str],
) -> dict[_Key, _StoreCosts]:
    """Add a row for every lease active in a month and every cost-only month, and put
    each store's costs of a month on one row (see the module docstring)."""
    with_figures: dict[tuple[uuid.UUID, date], list[_Key]] = defaultdict(list)
    for k in acc:
        if k[0] is not None:
            with_figures[(k[0], k[2])].append(k)
    placed: dict[_Key, _StoreCosts] = {}
    for store in set(spans) | set(costs) | {k[0] for k in acc if k[0] is not None}:
        store_spans = spans.get(store, [])
        cost_rows = costs.get(store, [])
        for month in months:
            active = [span for span in store_spans if span.active_in(month)]
            for span in active:
                acc.setdefault((store, span.operator, month), _Acc())
            carrier: _Key | None = None
            if active:
                on_first = [span for span in active if span.active_on(month)]
                chosen = min(
                    on_first or active,
                    key=lambda span: (span.started_on or date.min, str(span.pk)),
                )
                carrier = (store, chosen.operator, month)
            elif any(row.holds(month) for row in cost_rows):
                carrier = (store, None, month)
                acc[carrier].cost_only = True
            elif with_figures.get((store, month)):
                carrier = min(
                    with_figures[(store, month)],
                    key=lambda k: (k[1] is not None, names.get(k[1], ""), str(k[1])),
                )
            if carrier is None:
                continue
            acc[carrier].carries = True
            in_effect = next((row for row in cost_rows if row.in_effect_on(month)), None)
            placed[carrier] = (
                _StoreCosts(in_effect.landlord_rent, in_effect.other_monthly_cost or ZERO)
                if in_effect is not None
                else _StoreCosts(None, None)
            )
    return placed


def rows(query: PnlQuery) -> list[PnlRow]:
    """One row per (store, operator, month) of the range, sorted by store, month and
    operator (see the module docstring for every column). Fourteen queries, whatever the
    number of rows."""
    start, end = query.from_month, query.end
    acc: dict[_Key, _Acc] = defaultdict(_Acc)
    _add_billed_and_collected(acc, start, end)
    names = _operator_names()
    placed = _place_store_costs(
        acc, query.months, _lease_spans(start, end), _cost_rows(start, end), names
    )
    labels = dict(Store.objects.values_list("id", "external_label"))
    known_zero = _StoreCosts(ZERO, ZERO)
    result = []
    for k, item in acc.items():
        store_costs = placed.get(k, known_zero)
        result.append(
            PnlRow(
                rent_billed=item.rent_billed,
                rent_collected=item.rent_collected,
                fees_billed=item.fees_billed,
                fees_collected=item.fees_collected,
                withheld_deposits=item.withheld_deposits,
                insurance_billed=item.insurance_billed,
                insurance_collected=item.insurance_collected,
                landlord_rent=store_costs.landlord_rent,
                other_costs=store_costs.other_costs,
                fee_costs=None if item.fee_costs_unknown else item.fee_costs,
                store_id=k[0],
                store=labels.get(k[0], "") if k[0] is not None else NO_STORE,
                operator_id=k[1],
                operator=names.get(k[1], NO_OPERATOR) if k[1] is not None else NO_OPERATOR,
                month=k[2],
                cost_only=item.cost_only,
                carries_store_costs=item.carries,
            )
        )
    result.sort(
        key=lambda row: (
            row.store,
            str(row.store_id),
            row.month,
            row.operator_id is not None,
            row.operator,
            str(row.operator_id),
        )
    )
    return result


def _operator_names() -> dict[uuid.UUID | None, str]:
    return dict(Operator.objects.values_list("id", "display_name"))


# --- Groups and totals ----------------------------------------------------------------


def grouped(items: Iterable[PnlRow], group: str) -> list[PnlGroup]:
    """The rows summed by store, operator or month. A cost that is unknown on any row
    is unknown for its group."""
    buckets: dict[Any, list[PnlRow]] = defaultdict(list)
    labels: dict[Any, str] = {}
    order: dict[Any, tuple[Any, ...]] = {}
    for row in items:
        if group == GROUP_OPERATOR:
            bucket: Any = row.operator_id
            labels[bucket] = row.operator
            order[bucket] = (row.operator_id is None, row.operator, str(row.operator_id))
        elif group == GROUP_MONTH:
            bucket = row.month
            labels[bucket] = label_for_screen(row.month)
            order[bucket] = (row.month,)
        else:
            bucket = row.store_id
            labels[bucket] = row.store
            order[bucket] = (row.store, str(row.store_id))
        buckets[bucket].append(row)
    return [
        PnlGroup(**_sum(members), label=labels[bucket], rows=tuple(members))
        for bucket, members in sorted(buckets.items(), key=lambda pair: order[pair[0]])
    ]


def total(items: Iterable[Figures]) -> Figures:
    return Figures(**_sum(items))


def deposits_held(query: PnlQuery) -> Decimal:
    """Deposits held at the end of the range: settled deposit payments (net of their
    settled refunds) less the deposits refunded, withheld or transferred by then. A
    payment on a multi-line invoice counts by the lines it paid: its part on a deposit
    line is held (PLAN-012 D6). Not revenue; shown under the report only."""
    end = query.end
    deposit = Q(one_off_charge__fee_type__refundable=True, status=PaymentStatus.SETTLED)
    split = on_multi_line_invoice()
    held_by_lines = sum(
        (
            part.share
            for parts in invoice_line_parts(
                Payment.objects.filter(split, settled_on__lte=end)
            ).values()
            for part in parts
            if part.refundable
        ),
        ZERO,
    )
    sums = (
        Payment.objects.filter(deposit, settled_on__lte=end)
        .exclude(split)
        .aggregate(
            paid=Sum("amount", filter=Q(kind=PaymentKind.ONE_OFF, is_refund=False)),
            refunded=Sum("amount", filter=Q(kind=PaymentKind.ONE_OFF, is_refund=True)),
            paid_back=Sum("amount", filter=Q(kind=PaymentKind.REFUND_DEPOSIT)),
        )
    )
    return (
        _money_or_zero(sums["paid"])
        - _money_or_zero(sums["refunded"])
        + held_by_lines
        - _money_or_zero(sums["paid_back"])
        - deposits_released(DepositResolution.objects.filter(resolved_on__lte=end))
    )


# --- CSV ------------------------------------------------------------------------------

CSV_HEADER: tuple[str, ...] = (
    "Level",
    "Store",
    "Operator",
    "Month",
    "Rent billed (USD)",
    "Rent collected (USD)",
    "Fees billed (USD)",
    "Fees collected (USD)",
    "Withheld deposits (USD)",
    "Landlord rent (USD)",
    "Landlord rent known",
    "Other costs (USD)",
    "Other costs known",
    "Fee costs (USD)",
    "Fee costs known",
    "Margin billed (USD)",
    "Margin billed known",
    "Margin collected (USD)",
    "Margin collected known",
    "Cost-only months",
    "Unknown inputs",
)


def _maybe(value: Decimal | None) -> list[object]:
    """An amount that may be unknown: the amount and ``yes``, or empty and ``no``."""
    return [None, False] if value is None else [value, True]


def _record(
    level: str, store: str, operator: str, month: str, item: Figures, cost_only: int
) -> list[object]:
    return [
        level,
        store,
        operator,
        month,
        item.rent_billed,
        item.rent_collected,
        item.fees_billed,
        item.fees_collected,
        item.withheld_deposits,
        *_maybe(item.landlord_rent),
        *_maybe(item.other_costs),
        *_maybe(item.fee_costs),
        *_maybe(item.margin_billed),
        *_maybe(item.margin_collected),
        cost_only,
        "; ".join(item.unknown),
    ]


def csv_records(query: PnlQuery) -> Iterator[list[object]]:
    """The records of :func:`csv_lines`, header excluded."""
    found = rows(query)
    if query.detail:
        # In the order the page lists them: group by group.
        for row in (row for group in grouped(found, query.group) for row in group.rows):
            yield _record(
                "detail",
                row.store,
                row.operator,
                _label(row.month),
                row,
                1 if row.cost_only else 0,
            )
    else:
        for group in grouped(found, query.group):
            store = group.label if query.group == GROUP_STORE else ""
            operator = group.label if query.group == GROUP_OPERATOR else ""
            # The page's label reads "Jul 2026"; the CSV keeps ISO (DESIGN-SPEC §5).
            month = _label(group.rows[0].month) if query.group == GROUP_MONTH else ""
            yield _record(query.group, store, operator, month, group, group.cost_only_count)
    yield _record("total", "", "", "", total(found), sum(1 for row in found if row.cost_only))


def csv_lines(query: PnlQuery) -> Iterator[str]:
    """The report as CSV, one line at a time (header first)."""
    return csv_lines_of(csv_records(query))


def csv_lines_of(records: Iterable[list[object]]) -> Iterator[str]:
    """The header, then ``records``, as CSV lines. The export computes its records
    first (inside the report's statement timeout, PLAN-010c D11) and streams these."""
    return CsvWriter(CSV_HEADER).lines(records)
