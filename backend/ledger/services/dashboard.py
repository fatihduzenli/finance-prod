"""The dashboard's mini calendar (docs/qa/PLAN-012f-b-staging-review.md D6; DESIGN-SPEC §4a), and
the business-day arithmetic the installments share. The dashboard's other figures are
:mod:`ledger.services.dashboard_v2` (PLAN-015 Batch 1).

Read only: nothing here writes or calls the payment provider.

- :func:`mini_calendar` (the Calendar page's three queries, and three or four of its own for
  the promises, installments and due dates, :func:`money_items`): a month's days and items, and
  the next seven days' items from today (:class:`NextDays`, owner 2026-11-01): at most
  :data:`NEXT_ROWS` rows with the store's title and the amount, read by at most four more
  queries over the rows shown (the stores; the rent charges, the rent in effect and the
  re-debit charges behind the amounts), never one per row.

Money is ``Decimal``; dates are the project's ``TIME_ZONE`` days.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Exists, OuterRef, Q, Sum
from django.urls import reverse

from finance.dates import day_short_text
from ledger.models import (
    LIVE_PLAN_STATUSES,
    NOT_BILLED_RENT,
    ZERO,
    ContactEntry,
    ContactKind,
    ContactStatus,
    InstallmentPlan,
    InsuranceCharge,
    Lease,
    LeaseRentHistory,
    LegacyStatus,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    PaymentKind,
    PaymentStatus,
    RentCharge,
    Store,
)
from ledger.services import calendar, pnl, receivables, rent_run, stores
from ledger.services.leases import debit_amounts


def add_business_days(day: date, count: int) -> date:
    """``day`` plus ``count`` business days (Monday to Friday)."""
    found = day
    left = count
    while left > 0:
        found += timedelta(days=1)
        if found.weekday() < 5:
            left -= 1
    return found


# --- The mini calendar (D6) ------------------------------------------------------------------


#: The kinds the dashboard reads beside the Calendar's (owner 2026-11-01, Fable's review): an
#: open promise to pay, an open installment of a live plan, and any other open charge's due
#: date (a one-off charge or an insurance charge with a balance; not a rent charge, which the
#: debit day carries, not a deposit, not a plan's parent).
KIND_PROMISE = "promise"
KIND_INSTALLMENT = "installment"
KIND_DUE = "due"
KIND_LABELS: dict[str, str] = {
    **calendar.KIND_LABELS,
    KIND_PROMISE: "Promise to pay",
    KIND_INSTALLMENT: "Installment",
    KIND_DUE: "Due date",
}
#: The order of a day's dots and of the legend (PLAN-012g C3; owner 2026-11-01): the debit
#: day, the re-debit, the installment, the due date, the promise, the lease end, then the
#: insurance anniversary, the reminder and the note.
DOT_ORDER: tuple[str, ...] = (
    calendar.KIND_DEBIT,
    calendar.KIND_REDEBIT,
    KIND_INSTALLMENT,
    KIND_DUE,
    KIND_PROMISE,
    calendar.KIND_LEASE_END,
    calendar.KIND_INSURANCE,
    calendar.KIND_REMINDER,
    calendar.KIND_NOTE,
)
#: The dots a day shows at most, one per kind present.
MAX_DOTS = 3
_WEEKDAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _kinds_of(items: Iterable[calendar.CalendarItem]) -> list[str]:
    present = {item.kind for item in items}
    return [kind for kind in DOT_ORDER if kind in present]


@dataclass(frozen=True, slots=True)
class MiniDay:
    day: date
    today: bool
    in_month: bool
    items: tuple[calendar.CalendarItem, ...]

    @property
    def kinds(self) -> list[str]:
        """The day's dots: one per kind present, in :data:`DOT_ORDER`, at most
        :data:`MAX_DOTS`."""
        return _kinds_of(self.items)[:MAX_DOTS]

    @property
    def label(self) -> str:
        """The phone panel's heading: "Wed, Oct 28"."""
        return f"{_WEEKDAYS_SHORT[self.day.weekday()]}, {day_short_text(self.day)}"


#: The days the mini calendar's "Next 7 days" list covers: today and the six after it.
NEXT_DAYS = 7
#: The rows the list shows at most; the rest are counted ("+N more").
NEXT_ROWS = 6
NEXT_EMPTY = "Nothing in the next 7 days."
#: A generated title's tail when the lease is debited twice a month (", debit 2").
_DEBIT_SUFFIX = re.compile(r", debit \d\Z")


@dataclass(frozen=True, slots=True)
class NextItem:
    """A row of the "Next 7 days" list: the day ("Nov 3"), the store (its title, linked;
    empty when the item has none), what it is (the day panel's words without the store)
    and the amount (``None`` when the item has none or it is not known)."""

    on: date
    kind: str
    what: str
    url: str
    store: str
    store_url: str
    amount: Decimal | None

    @property
    def label(self) -> str:
        return day_short_text(self.on)


@dataclass(frozen=True, slots=True)
class NextDays:
    start: date
    end: date
    rows: list[NextItem]
    #: The items past :data:`NEXT_ROWS`, counted, not listed.
    more: int

    @property
    def empty_line(self) -> str:
        return NEXT_EMPTY


@dataclass(frozen=True, slots=True)
class MiniCalendar:
    month: date
    previous: date
    next: date
    weeks: list[list[MiniDay]]
    upcoming: NextDays

    @property
    def days_with_items(self) -> list[MiniDay]:
        """The days that have items, in date order (the list under the grid without a script)."""
        return [cell for week in self.weeks for cell in week if cell.items]

    @property
    def legend(self) -> list[tuple[str, str]]:
        """``(kind, label)`` of every kind present in the month, in :data:`DOT_ORDER`
        (the legend under the grid; empty when the month has no items)."""
        items = [item for cell in self.days_with_items for item in cell.items]
        return [(kind, KIND_LABELS[kind]) for kind in _kinds_of(items)]


def parse_cal(raw: str, today: date) -> date:
    """``?cal=YYYY-MM``: the mini calendar's month; the current month when unreadable."""
    return pnl._parse_month(raw or "") or today.replace(day=1)


#: The UID prefix of an insurance charge's due-date item (:func:`money_items`).
_INSURANCE_DUE = "due-insurance-"


@dataclass(frozen=True, slots=True)
class _Money:
    """What the list shows of an item the dashboard reads itself (:func:`money_items`): its
    words, its amount, and the operator's name when it has no store."""

    what: str
    amount: Decimal
    where: str


def _where(number: int | None, label: str | None, city: str | None) -> str:
    return stores.title_of(number, label or "", city or "") if label is not None else ""


def money_items(
    windows: calendar.Windows,
) -> tuple[list[calendar.CalendarItem], dict[str, _Money]]:
    """The promises, installments and due dates in ``windows`` as calendar items (titled as
    the day panel shows them, "Promise to pay: #1991 Istanbul"), with what the list shows of
    each by UID. Three queries (the promises, the one-off charges, the insurance charges),
    a fourth for the insurance charges' re-debits when there are any."""
    found: list[calendar.CalendarItem] = []
    money: dict[str, _Money] = {}

    def add(
        uid: str,
        kind: str,
        on: date,
        what: str,
        amount: Decimal,
        url: str,
        place: tuple[uuid.UUID | None, str, uuid.UUID | None, str, uuid.UUID | None],
    ) -> None:
        store_id, store, operator_id, operator, lease_id = place
        where = store or operator
        found.append(
            calendar.CalendarItem(
                uid=uid,
                kind=kind,
                on=on,
                title=f"{what}: {where}" if where else what,
                body="",
                url=url,
                store_id=store_id,
                operator_id=operator_id,
                lease_id=lease_id,
            )
        )
        money[uid] = _Money(what, amount, "" if store_id is not None else operator)

    store_fields = ("lease__store_id", "lease__store__store_number", "lease__store__external_label")
    for pk, on, amount, operator_id, operator, lease_id, store_id, number, label, city in (
        ContactEntry.objects.filter(
            calendar._within("promised_date", windows),
            kind=ContactKind.PROMISE,
            status=ContactStatus.OPEN,
            deleted_at__isnull=True,
        )
        .values_list(
            "pk",
            "promised_date",
            "promised_amount",
            "operator_id",
            "operator__display_name",
            "lease_id",
            *store_fields,
            "lease__store__city",
        )
        .order_by("promised_date", "pk")
    ):
        add(
            f"promise-{pk}",
            KIND_PROMISE,
            on,
            KIND_LABELS[KIND_PROMISE],
            Decimal(amount),
            reverse("ledger:operators:detail", kwargs={"pk": operator_id}) + "#contacts",
            (store_id, _where(number, label, city), operator_id, operator or "", lease_id),
        )
    live_parent = InstallmentPlan.objects.filter(
        one_off_charge=OuterRef("pk"), status__in=LIVE_PLAN_STATUSES
    )
    for (
        pk,
        category,
        on,
        balance,
        description,
        fee_name,
        number_of,
        count,
        plan_status,
        operator_id,
        operator,
        lease_id,
        store_id,
        number,
        label,
        city,
    ) in (
        OneOffCharge.objects.filter(calendar._within("due_date", windows), balance__gt=0)
        .exclude(category=OneOffCategory.REDEBIT)  # the Calendar's own item
        .exclude(
            status__in=[
                OneOffChargeStatus.PAID,
                OneOffChargeStatus.WAIVED,
                OneOffChargeStatus.VOIDED,
            ]
        )
        .filter(Q(fee_type__isnull=True) | Q(fee_type__refundable=False))  # not a deposit
        .exclude(Exists(live_parent))  # a plan's parent: its installments are owed instead
        .values_list(
            "pk",
            "category",
            "due_date",
            "balance",
            "description",
            "fee_type__name",
            "installment_no",
            "plan__count",
            "plan__status",
            "operator_id",
            "operator__display_name",
            "lease_id",
            *store_fields,
            "lease__store__city",
        )
        .order_by("due_date", "pk")
    ):
        if category == OneOffCategory.INSTALLMENT:
            if plan_status not in LIVE_PLAN_STATUSES:
                continue
            kind, what = KIND_INSTALLMENT, f"Installment {number_of} of {count}"
        else:
            kind, what = KIND_DUE, description or fee_name or OneOffCategory(category).label
        add(
            f"{kind}-{pk}",
            kind,
            on,
            what,
            Decimal(balance),
            reverse("ledger:one_off_charges:detail", kwargs={"pk": pk}),
            (store_id, _where(number, label, city), operator_id, operator or "", lease_id),
        )
    settled = Q(payments__status=PaymentStatus.SETTLED, payments__kind=PaymentKind.INSURANCE)
    insurance = list(
        InsuranceCharge.objects.filter(calendar._within("due_on", windows), amount__isnull=False)
        .exclude(legacy_status=LegacyStatus.PAID)
        .values_list(
            "pk",
            "due_on",
            "amount",
            "policy_year",
            "lease__operator_id",
            "lease__operator__display_name",
            "lease_id",
            *store_fields,
            "lease__store__city",
        )
        .annotate(
            paid=Sum("payments__amount", filter=settled & Q(payments__is_refund=False)),
            refunded=Sum("payments__amount", filter=settled & Q(payments__is_refund=True)),
        )
        .order_by("due_on", "pk")
    )
    if insurance:
        carried, collected = receivables.insurance_redebits(row[0] for row in insurance)
        for (
            pk,
            on,
            amount,
            year,
            operator_id,
            operator,
            lease_id,
            store_id,
            number,
            label,
            city,
            paid,
            refunded,
        ) in insurance:
            balance = receivables.insurance_owed(
                Decimal(amount),
                paid=Decimal(paid or ZERO),
                refunded=Decimal(refunded or ZERO),
                collected=collected.get(pk, ZERO),
                carried=carried.get(pk, ZERO),
            )
            if balance <= 0:
                continue
            add(
                f"{_INSURANCE_DUE}{pk}",
                KIND_DUE,
                on,
                f"Insurance, policy year {year}",
                balance,
                reverse("ledger:leases:detail", kwargs={"pk": lease_id}),
                (store_id, _where(number, label, city), operator_id, operator or "", lease_id),
            )
    return found, money


def _merge_anniversaries(
    found: Sequence[calendar.CalendarItem],
    extra: Sequence[calendar.CalendarItem],
    money: dict[str, _Money],
) -> list[calendar.CalendarItem]:
    """``found`` and ``extra`` as one list, an open insurance charge due on its lease's
    anniversary shown once (Fable's review): the Calendar's "Insurance anniversary" item,
    with the charge's balance as its amount; the charge's due-date item is dropped. An
    anniversary without an open charge keeps no amount; a charge due on another day stays a
    due date. ``money`` is updated in place."""
    anniversaries = {
        (item.lease_id, item.on): item for item in found if item.kind == calendar.KIND_INSURANCE
    }
    kept = list(found)
    for item in extra:
        anniversary = (
            anniversaries.get((item.lease_id, item.on))
            if item.uid.startswith(_INSURANCE_DUE)
            else None
        )
        if anniversary is None:
            kept.append(item)
            continue
        due = money.pop(item.uid)
        money[anniversary.uid] = _Money(_what(anniversary), due.amount, due.where)
    return kept


def _uid_key(item: calendar.CalendarItem) -> str:
    """The part of a generated item's UID before the host ("debit-<lease>-<seq>-<day>",
    "redebit-<charge>"): what names its record."""
    return item.uid.partition("@")[0]


def _what(item: calendar.CalendarItem) -> str:
    """The day panel's words without the store: a generated item's title before the store
    ("Debit day", "Re-debit", "Lease ends", with ", debit 2" kept); a stored entry's own
    title; a generated item without a store keeps its whole title (the operator's name)."""
    if not item.generated or item.store_id is None:  # a promise, installment, due date too
        return item.title
    head, _, rest = item.title.partition(": ")
    suffix = _DEBIT_SUFFIX.search(rest)
    return head + (suffix.group(0) if suffix else "")


def _debit_amounts(debits: Sequence[calendar.CalendarItem]) -> dict[str, Decimal]:
    """Each debit day's amount by its UID: the rent charge's amount due when the charge is
    made (a waived one has none), else what the rent run would charge that day (the rent in
    effect, the first month prorated, split for a twice-monthly lease). Two queries."""
    keys: dict[str, tuple[uuid.UUID, int, date]] = {}
    for item in debits:
        if item.lease_id is None:  # a debit day is always a lease's
            continue
        _, seq_text, _ = _uid_key(item).rsplit("-", 2)
        keys[item.uid] = (item.lease_id, int(seq_text), item.on)
    if not keys:
        return {}
    lease_ids = {lease_id for lease_id, _, _ in keys.values()}
    charges = {
        (lease_id, seq, due_on): (status, amount)
        for lease_id, seq, due_on, status, amount in RentCharge.objects.filter(
            lease_id__in=lease_ids, due_on__in={on for _, _, on in keys.values()}
        ).values_list("lease_id", "seq", "due_on", "status", "amount_due")
    }
    found: dict[str, Decimal] = {}
    missing = {uid: key for uid, key in keys.items() if key not in charges}
    for uid, key in keys.items():
        if key in charges and charges[key][0] not in NOT_BILLED_RENT:
            found[uid] = Decimal(charges[key][1])
    if not missing:
        return found
    rents: dict[uuid.UUID, list[LeaseRentHistory]] = defaultdict(list)
    leases: dict[uuid.UUID, Lease] = {}
    for row in LeaseRentHistory.objects.select_related("lease").filter(
        lease_id__in={lease_id for lease_id, _, _ in missing.values()},
        effective_from__lte=max(on for _, _, on in missing.values()),
    ):
        rents[row.lease_id].append(row)
        leases[row.lease_id] = row.lease
    for uid, (lease_id, seq, on) in missing.items():
        live = [
            row
            for row in rents[lease_id]
            if row.effective_from <= on and (row.effective_to is None or row.effective_to >= on)
        ]
        if not live:
            continue
        lease = leases[lease_id]
        rent = max(live, key=lambda row: row.effective_from).amount
        month = rent_run.first_month(lease, on.replace(day=1), rent)
        days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
        found[uid] = debit_amounts(month.amount, twice_monthly=len(days) == 2)[seq - 1]
    return found


def _redebit_amounts(redebits: Sequence[calendar.CalendarItem]) -> dict[str, Decimal]:
    """Each re-debit's (or bank debit's) amount by its UID: the charge's price. One query."""
    if not redebits:
        return {}
    by_pk = {_uid_key(item).removeprefix("redebit-"): item.uid for item in redebits}
    return {
        by_pk[str(pk)]: Decimal(price)
        for pk, price in OneOffCharge.objects.filter(pk__in=list(by_pk)).values_list("pk", "price")
    }


def next_days(
    found: Iterable[calendar.CalendarItem],
    today: date,
    money: Mapping[str, _Money] | None = None,
) -> NextDays:
    """The "Next 7 days" list (owner 2026-11-01): ``found``'s items from today to six days
    after, by day, then kind in :data:`DOT_ORDER`, then ``found``'s order; the first
    :data:`NEXT_ROWS` as rows, the rest counted; ``money`` gives the words, amount and
    operator of the items :func:`money_items` read. At most four queries, over the rows
    shown."""
    start, end = today, today + timedelta(days=NEXT_DAYS - 1)
    within = [item for item in found if start <= item.on <= end]
    within.sort(key=lambda item: (item.on, DOT_ORDER.index(item.kind)))
    shown = within[:NEXT_ROWS]
    store_ids = {item.store_id for item in shown if item.store_id is not None}
    titles = (
        {
            pk: stores.title_of(number, label, city)
            for pk, number, label, city in Store.objects.filter(pk__in=store_ids).values_list(
                "pk", "store_number", "external_label", "city"
            )
        }
        if store_ids
        else {}
    )
    amounts = _debit_amounts([item for item in shown if item.kind == calendar.KIND_DEBIT])
    amounts |= _redebit_amounts([item for item in shown if item.kind == calendar.KIND_REDEBIT])
    read = money or {}
    amounts |= {uid: entry.amount for uid, entry in read.items()}
    rows = [
        NextItem(
            on=item.on,
            kind=item.kind,
            what=read[item.uid].what if item.uid in read else _what(item),
            url=item.url,
            store=(
                titles.get(item.store_id, "")
                if item.store_id is not None
                else (read[item.uid].where if item.uid in read else "")
            ),
            store_url=(
                reverse("ledger:stores:detail", kwargs={"pk": item.store_id})
                if item.store_id in titles
                else ""
            ),
            amount=amounts.get(item.uid),
        )
        for item in shown
    ]
    return NextDays(start=start, end=end, rows=rows, more=len(within) - len(shown))


def mini_calendar(month: date, today: date) -> MiniCalendar:
    """The month's weeks (Sunday first) with each day's items: the Calendar page's own
    reads (:func:`ledger.services.calendar.items_in` over the month and the next seven
    days, every kind, no store or operator filter, three queries;
    :func:`ledger.services.calendar.month_weeks`) with the promises, installments and due
    dates over the same windows (:func:`money_items`), and the "Next 7 days" list
    (:func:`next_days`) from the same reads."""
    first, last = calendar.month_bounds(month)
    windows = ((first, last), (today, today + timedelta(days=NEXT_DAYS - 1)))
    extra, money = money_items(windows)
    found = sorted(
        _merge_anniversaries(calendar.items_in(windows, calendar.Filters()), extra, money),
        key=lambda item: item.on,
    )
    in_month = [item for item in found if first <= item.on <= last]
    weeks = [
        [MiniDay(cell.day, cell.day == today, cell.in_month, cell.items) for cell in week]
        for week in calendar.month_weeks(first, in_month)
    ]
    return MiniCalendar(
        month=first,
        previous=pnl.add_months(first, -1),
        next=pnl.add_months(first, 1),
        weeks=weeks,
        upcoming=next_days(found, today, money),
    )
