"""The store page as the workspace (docs/qa/PLAN-012d-store-workspace.md D1 to D3).

:func:`store_workspace` builds what the store page shows above its details, read only:

- the **header**: the store's current lease (:func:`ledger.services.lease_end.current_lease`)
  and its operator, the rent in effect, the autopay state of its rent schedules
  (:func:`ledger.services.leases.live_schedules`, moved past a split month by
  :func:`ledger.services.leases.autopay_move`), the **open balance** (the store's rows of
  :func:`ledger.services.receivables.open_balances`, for every operator the store's
  leases bill), the **credit** held (:func:`ledger.services.credits.balance`), the **next
  scheduled payment** (the earliest of the next rent debit, the next one-off autopay due
  date, the next installment due and the next re-debit of a returned payment) and the
  **open promise** (:func:`ledger.services.contacts.open_promise`);
- the **action bar** (D2, PLAN-012e-2 D5): at most Create invoice, Add payment and the
  Lease menu (admin), each pointing at an existing form with the store and its lease in
  the query string (``?store=``, ``?lease=``); a store without a current lease offers New
  lease instead (admin); a viewer gets none; Create missing charges (PLAN-012d D13) is a
  link on the Charges tab;
- the **open tab** only (D3, ``?tab=``, Charges by default): Charges, Invoices, Payments,
  Schedules and History are built here; Documents and Contact log are built by their own
  view helpers (:mod:`ledger.views.documents`, :mod:`ledger.views.contact_log`). History
  is for admins; another role asking for it gets Charges. The Charges tab's open rows
  show the header's figures (:class:`StoreBalances`): a charge a live re-debit carries
  owes only what the re-debit does not collect, and reads "Carried by re-debit" once
  nothing is left (finding 12d-2).

:func:`operator_workspace` builds the operator page the same way (D14): the selected
store (``?store=``, one of the operator's stores, else the first by label) with its header
card and bar on the operator's current lease of it (nothing when the store's current lease
is another operator's), the operator's open balance across its stores, and the open tab
over the operator's records (:data:`Owner`): Charges, Invoices (the operator's drafts,
operator-level ones included, and invoices), Payments and Schedules with a store column;
Documents and History (with the operator's own rows) of the selected store.

Nothing here writes or calls the payment provider. Money is ``Decimal``; provider
identifiers are masked.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery, Sum
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role, meets
from finance.dates import month_text
from ledger.models import (
    LIVE_PLAN_STATUSES,
    ZERO,
    CollectionMethod,
    CollectionSchedule,
    ContactEntry,
    DepositResolution,
    DepositResolutionKind,
    InstallmentPlan,
    InsuranceCharge,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceLineItem,
    InvoiceStatus,
    Lease,
    LeaseRentHistory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    ScheduleState,
    Store,
)
from ledger.services import (
    audit,
    contacts,
    credits,
    lease_end,
    leases,
    payments_ledger,
    receivables,
)
from ledger.services import insurance as insurance_years
from ledger.wording import format_amount
from payments.provider import mask_identifier

TAB_CHARGES = "charges"
TAB_INVOICES = "invoices"
TAB_PAYMENTS = "payments"
TAB_SCHEDULES = "schedules"
TAB_DOCUMENTS = "documents"
TAB_CONTACTS = "contacts"
TAB_HISTORY = "history"
#: The tabs in page order, with their labels.
TABS: tuple[tuple[str, str], ...] = (
    (TAB_CHARGES, "Charges"),
    (TAB_INVOICES, "Invoices"),
    (TAB_PAYMENTS, "Payments"),
    (TAB_SCHEDULES, "Autopay"),
    (TAB_DOCUMENTS, "Documents"),
    (TAB_CONTACTS, "Contact log"),
    (TAB_HISTORY, "Activity"),
)
#: The tabs only an admin sees.
ADMIN_TABS: frozenset[str] = frozenset({TAB_HISTORY})

FILTER_OPEN = "open"
FILTER_PAID = "paid"
FILTER_UNPAID = "unpaid"
FILTER_ALL = "all"
CHARGE_FILTERS: tuple[tuple[str, str], ...] = (
    (FILTER_OPEN, "Open"),
    (FILTER_PAID, "Paid"),
    (FILTER_ALL, "All"),
)
INVOICE_FILTERS: tuple[tuple[str, str], ...] = ((FILTER_UNPAID, "Unpaid"), (FILTER_ALL, "All"))
#: The newest payments the Payments tab lists; the payments ledger has every one.
PAYMENTS_LIMIT = 100

KIND_RENT = "rent"
KIND_ONE_OFF = "one_off"
KIND_INSURANCE = "insurance"

AUTOPAY_NONE = "Not set up"
NEXT_RENT = "Rent debit"
NEXT_REDEBIT = "Re-debit of a returned payment"
#: The status of a charge whose balance a live re-debit collects (finding 12d-2).
CARRIED_BY_REDEBIT = "Carried by re-debit"

_CLOSED_ONE_OFF = (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)


def tab_url(store_id: uuid.UUID, tab: str) -> str:
    """The store page opened on ``tab``, scrolled to it (D8)."""
    page = reverse("ledger:stores:detail", kwargs={"pk": store_id})
    return f"{page}?tab={tab}#tab-{tab}"


#: Whose records a tab lists: one store's leases, or one operator's (PLAN-012d D14).
type Owner = Store | Operator


def _owned(owner: Owner, *, direct: bool = False) -> Q:
    """The rows of ``owner``'s records: the leases of a store, or of an operator
    (``direct``: by the row's own ``operator`` column, for one-off charges and plans,
    which may have no lease)."""
    if isinstance(owner, Operator):
        return Q(operator=owner) if direct else Q(lease__operator=owner)
    return Q(lease__store=owner)


def _owner_query(owner: Owner) -> dict[str, str]:
    """The exports' and the ledgers' filter on ``owner``."""
    return {"operator" if isinstance(owner, Operator) else "store": str(owner.pk)}


def operator_tab_url(operator_id: uuid.UUID, tab: str, store_id: uuid.UUID | None = None) -> str:
    """The operator page opened on ``tab`` (and on ``store_id``), without a fragment, as a
    form's ``?next=`` is (D14)."""
    page = reverse("ledger:operators:detail", kwargs={"pk": operator_id})
    query = {"store": str(store_id)} if store_id is not None else {}
    return f"{page}?{urlencode({**query, 'tab': tab})}"


def _back(owner: Owner, tab: str) -> str:
    """The query string a form opened from ``owner``'s page carries to come back to its
    ``tab`` (D8): ``?store=`` on the store page, ``?next=`` on the operator page."""
    if isinstance(owner, Operator):
        return "?" + urlencode({"next": operator_tab_url(owner.pk, tab)})
    return "?" + urlencode({"store": str(owner.pk)})


def tabs_for(role: Role | None) -> list[tuple[str, str]]:
    """The tabs ``role`` sees: History for admins only."""
    admin = meets(role, Role.ADMIN)
    return [(name, label) for name, label in TABS if admin or name not in ADMIN_TABS]


def chosen_tab(raw: str | None, role: Role | None, *, default: str = TAB_CHARGES) -> str:
    """The tab of ``?tab=``: one ``role`` sees, else ``default``."""
    names = {name for name, _ in tabs_for(role)}
    return raw if raw in names else default


def _chosen_filter(raw: str | None, choices: tuple[tuple[str, str], ...]) -> str:
    values = [value for value, _ in choices]
    return raw if raw in values else values[0]


# --- The header ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NextPayment:
    on: date
    what: str


@dataclass(frozen=True, slots=True)
class Header:
    lease: Lease | None
    operator: Operator | None
    rent: LeaseRentHistory | None
    #: "On", "Paused", "Failed", "Not set up" or :data:`AUTOPAY_NONE`.
    autopay: str
    #: Why autopay starts later (a split month), or ``""``.
    autopay_moved: str
    open_balance: Decimal
    credit: Decimal
    next_payment: NextPayment | None
    open_promise: ContactEntry | None
    #: The side cards' lines (PLAN-012f D8), from rows already read, but the deposit (one
    #: query): the open charges counted in the balance (the Charges tab's pill), the debit
    #: days in words ("1st", "1st and 15th"), the deposit ("$3,000.00 · not collected"), the
    #: next rent debit (autopay's, else the lease's debit day) and the store's next
    #: insurance renewal (its open-date anniversary, the lease holding the store that day).
    open_count: int = 0
    debit_day_text: str = ""
    deposit_text: str = ""
    next_rent: date | None = None
    next_insurance: date | None = None

    @property
    def lease_since(self) -> date | None:
        return self.lease.started_on if self.lease is not None else None


def _autopay_state(schedules: list[CollectionSchedule]) -> str:
    states = {row.state for row in schedules}
    for state in (ScheduleState.ACTIVE, ScheduleState.PAUSED, ScheduleState.FAILED):
        if state in states:
            return str(state.label)
    return str(ScheduleState.NONE.label) if schedules else AUTOPAY_NONE


def _next_rent(
    schedules: list[CollectionSchedule], moved: leases.AutopayMove | None, now: datetime
) -> date | None:
    found: list[date] = []
    for row in schedules:
        if row.state != ScheduleState.ACTIVE or row.debit_day is None:
            continue
        run = leases.next_debit_at(row.debit_day, now)
        if moved is not None and str(row.pk) in moved.starts:
            run = moved.starts[str(row.pk)]
        found.append(run.date())
    return min(found) if found else None


def _next_one_off(lease: Lease, today: date) -> tuple[date, str] | None:
    """The next one-off charge collected by autopay on its due date, or the next
    installment of a live plan, still owed."""
    charge = (
        OneOffCharge.objects.filter(lease=lease, balance__gt=0, due_date__gte=today)
        .exclude(status__in=_CLOSED_ONE_OFF)
        .filter(Q(collection=CollectionMethod.AUTOPAY_DUE) | Q(plan__status__in=LIVE_PLAN_STATUSES))
        .select_related("fee_type")
        .order_by("due_date", "created_at", "id")
        .first()
    )
    return (charge.due_date, charge.label) if charge is not None else None


def _next_redebit(lease: Lease, today: date) -> date | None:
    on_lease = (
        Q(rent_charge__lease=lease)
        | Q(one_off_charge__lease=lease)
        | Q(insurance_charge__lease=lease)
    )
    found: date | None = (
        Payment.objects.filter(on_lease, redebit_date__gte=today)
        .order_by("redebit_date")
        .values_list("redebit_date", flat=True)
        .first()
    )
    return found


def _next_payment(
    lease: Lease,
    schedules: list[CollectionSchedule],
    moved: leases.AutopayMove | None,
    today: date,
    now: datetime,
) -> NextPayment | None:
    found: list[NextPayment] = []
    rent = _next_rent(schedules, moved, now)
    if rent is not None:
        found.append(NextPayment(rent, NEXT_RENT))
    one_off = _next_one_off(lease, today)
    if one_off is not None:
        found.append(NextPayment(*one_off))
    redebit = _next_redebit(lease, today)
    if redebit is not None:
        found.append(NextPayment(redebit, NEXT_REDEBIT))
    return min(found, key=lambda item: item.on) if found else None


@dataclass(frozen=True, slots=True)
class StoreBalances:
    """The store's rows of :func:`ledger.services.receivables.open_balances` for every
    operator its leases bill: what each open charge owes, which charges a live re-debit
    carries, and their total (the header's open balance)."""

    owed: dict[tuple[str, uuid.UUID], Decimal]
    carried: frozenset[tuple[str, uuid.UUID]]
    total: Decimal


def _balances_of(
    rows: Iterable[receivables.OpenBalance],
    carried: Iterable[tuple[str, uuid.UUID]],
    store_id: uuid.UUID | None = None,
) -> StoreBalances:
    """:class:`StoreBalances` of receivables ``rows`` (those of ``store_id`` when given)."""
    owed = {
        (row.kind, row.charge_id): row.balance
        for row in rows
        if store_id is None or row.store_id == store_id
    }
    return StoreBalances(owed, frozenset(carried), sum(owed.values(), ZERO))


def _open_rows(
    today: date, operator: uuid.UUID
) -> tuple[list[receivables.OpenBalance], set[tuple[str, uuid.UUID]]]:
    """The operator's receivables rows and the charges a live re-debit carries."""
    rows, carrying = receivables.open_balances_and_carried(today, operator=operator)
    return rows, {key for key, amount in carrying.items() if amount > 0}


def store_balances(store: Store, store_leases: Iterable[Lease], today: date) -> StoreBalances:
    """:class:`StoreBalances` of ``store``, one receivables read per operator."""
    operators = sorted(
        {row.operator_id for row in store_leases if row.operator_id is not None}, key=str
    )
    rows: list[receivables.OpenBalance] = []
    carried: set[tuple[str, uuid.UUID]] = set()
    for operator in operators:
        found, carrying = _open_rows(today, operator)
        rows += found
        carried |= carrying
    return _balances_of(rows, carried, store.pk)


def ordinal(day: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th``, ``11th``, ``21st``."""
    if 10 <= day % 100 <= 20:
        return f"{day}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _debit_day_text(lease: Lease) -> str:
    days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
    return " and ".join(ordinal(day) for day in days)


def _deposit_text(lease: Lease) -> str:
    """The lease's deposit (its refundable one-off charges, not waived or voided; one
    query): ``$3,000.00 · not collected``, ``· collected``, or what was collected so far."""
    found = (
        OneOffCharge.objects.filter(lease=lease, fee_type__refundable=True)
        .exclude(status__in=_CLOSED_ONE_OFF)
        .aggregate(price=Sum("price"), paid=Sum("amount_paid"))
    )
    if found["price"] is None:
        return ""
    price, paid = _money(found["price"]), _money(found["paid"])
    if paid >= price:
        state = "collected"
    elif paid <= 0:
        state = "not collected"
    else:
        state = f"{format_amount(paid)} collected"
    return f"{format_amount(price)} · {state}"


def _next_debit_day(lease: Lease, today: date) -> date | None:
    """The next day after ``today`` a debit day of ``lease`` falls on (29 to 31: a short
    month's last day, as the rent run counts them)."""
    days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
    if not days:
        return None
    found: list[date] = []
    for months_ahead in (0, 1):
        first = (today.replace(day=1) + timedelta(days=32 * months_ahead)).replace(day=1)
        last = calendar.monthrange(first.year, first.month)[1]
        found += [first.replace(day=min(day, last)) for day in days]
    later = [day for day in found if day > today]
    return min(later) if later else None


def _next_insurance(lease: Lease, today: date) -> date | None:
    """The store's next insurance renewal from ``today``: its open-date anniversary, while
    the store is not closed by then and ``lease`` holds it that day."""
    store = lease.store
    if store.open_date is None:
        return None
    due = insurance_years.anniversary(store.open_date, today.year)
    if due < today:
        due = insurance_years.anniversary(store.open_date, today.year + 1)
    if store.closed_at is not None and due >= store.closed_at:
        return None
    return due if insurance_years.holds_on(lease, due) else None


def _header(
    lease: Lease | None, today: date, now: datetime, balance: Decimal, open_count: int = 0
) -> Header:
    """The header card of ``lease`` (the store's current one, or ``None``), with
    ``balance`` as its open balance over ``open_count`` open charges."""
    if lease is None:
        return Header(
            lease=None,
            operator=None,
            rent=None,
            autopay=AUTOPAY_NONE,
            autopay_moved="",
            open_balance=balance,
            credit=Decimal("0.00"),
            next_payment=None,
            open_promise=None,
            open_count=open_count,
        )
    schedules = leases.live_schedules(lease)
    moved = leases.autopay_move(lease, now)
    return Header(
        lease=lease,
        operator=lease.operator,
        rent=leases.current_rent(lease, today),
        autopay=_autopay_state(schedules),
        autopay_moved=moved.text if moved is not None else "",
        open_balance=balance,
        credit=credits.balance(lease),
        next_payment=_next_payment(lease, schedules, moved, today, now),
        open_promise=contacts.open_promise(lease.operator, lease),
        open_count=open_count,
        debit_day_text=_debit_day_text(lease),
        deposit_text=_deposit_text(lease),
        next_rent=_next_rent(schedules, moved, now) or _next_debit_day(lease, today),
        next_insurance=_next_insurance(lease, today),
    )


# --- The action bar (D2, PLAN-012e-2 D5) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """One control of the bar: a link to ``url``, or a menu (``items``, no url of its own)
    rendered as a native disclosure. ``element_id`` is the control's id on the page."""

    key: str
    label: str
    url: str
    primary: bool = False
    danger: bool = False
    items: tuple[Action, ...] = ()
    element_id: str = ""


def _with(url: str, **params: str) -> str:
    return f"{url}?{urlencode(params)}"


def _actions(store: Store, header: Header, role: Role | None) -> list[Action]:
    """The bar's actions ``role`` may take (PLAN-012e-2 D5), in bar order: Create invoice
    (primary; the operator not archived), Add payment and, for an admin, the Lease menu
    (Edit lease, Transfer to another operator, End lease); a store without a current
    lease offers New lease to an admin; a viewer gets none. Upload, Add entry and Create
    missing charges are on their tabs."""
    billing = meets(role, Role.BILLING)
    admin = meets(role, Role.ADMIN)
    if not billing:
        return []
    lease = header.lease
    if lease is None:
        if not admin:
            return []
        return [
            Action(
                "new_lease",
                "New lease",
                _with(reverse("ledger:leases:create"), store=str(store.pk)),
                element_id="new-lease",
            )
        ]
    known = {"store": str(store.pk), "lease": str(lease.pk)}
    found: list[Action] = []
    operator = header.operator
    if operator is not None and operator.archived_at is None:
        found.append(
            Action(
                "create_invoice",
                "Create invoice",
                _with(reverse("ledger:invoices:new_store", kwargs={"pk": store.pk}), **known),
                primary=True,
                element_id="create-invoice",
            )
        )
    found.append(
        Action(
            "record_payment",
            "Add payment",
            reverse("ledger:stores:record_payment", kwargs={"pk": store.pk}),
            element_id="record-payment",
        )
    )
    if admin:
        found.append(
            Action(
                "lease",
                "Lease",
                "",
                items=(
                    Action(
                        "edit_lease",
                        "Edit lease",
                        _with(reverse("ledger:leases:edit", kwargs={"pk": lease.pk}), **known),
                        element_id="bar-edit-lease",
                    ),
                    Action(
                        "transfer",
                        "Transfer to another operator",
                        reverse("ledger:stores:transfer", kwargs={"pk": store.pk}),
                        element_id="store-transfer",
                    ),
                    Action(
                        "end_lease",
                        "End lease",
                        reverse("ledger:stores:end_lease", kwargs={"pk": store.pk}),
                        danger=True,
                        element_id="store-end-lease",
                    ),
                ),
                element_id="lease-menu",
            )
        )
    return found


def _create_charges_url(store: Store, header: Header, role: Role | None) -> str:
    """Create missing charges (PLAN-012d D13; a link on the Charges tab since PLAN-012e-2
    D5): billing, on a store with a current lease; ``""`` otherwise."""
    if header.lease is None or not meets(role, Role.BILLING):
        return ""
    return reverse("ledger:stores:create_charges", kwargs={"pk": store.pk})


# --- Charges tab ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvoiceLink:
    number: str
    url: str


@dataclass(slots=True)
class ChargeRow:
    kind: str
    charge_id: uuid.UUID
    kind_label: str
    description: str
    due: date
    billed: Decimal | None
    paid: Decimal
    balance: Decimal
    status: str
    #: The charge's page, or "" (an insurance charge has none).
    url: str
    lease: Lease | None
    #: The per-charge payment form (billing, an open charge not on a live plan), or "".
    payment_url: str = ""
    #: Resolve deposit (admin, a deposit of an ended lease not resolved yet), or "".
    resolve_url: str = ""
    #: A resolved deposit: "Refunded $2000.00", "Withheld $..." or "Transferred $...".
    resolution: str = ""
    badges: list[str] = field(default_factory=list)
    invoices: list[InvoiceLink] = field(default_factory=list)


def _on_live_plan(column: str) -> Exists:
    return Exists(
        InstallmentPlan.objects.filter(**{column: OuterRef("pk")}, status__in=LIVE_PLAN_STATUSES)
    )


def _rent_rows(owner: Owner, chosen: str) -> QuerySet[RentCharge]:
    rows = RentCharge.objects.filter(_owned(owner)).annotate(on_plan=_on_live_plan("rent_charge"))
    if chosen == FILTER_OPEN:
        rows = rows.filter(balance__gt=0).exclude(status=RentChargeStatus.WAIVED)
    elif chosen == FILTER_PAID:
        rows = rows.filter(status=RentChargeStatus.PAID)
    return rows.select_related("lease__operator", "lease__store").order_by("-due_on", "-seq", "id")


_resolution = DepositResolution.objects.filter(setup_charge=OuterRef("pk"))


def _one_off_rows(owner: Owner, chosen: str) -> QuerySet[OneOffCharge]:
    rows = OneOffCharge.objects.filter(_owned(owner, direct=True)).annotate(
        on_plan=_on_live_plan("one_off_charge"),
        resolution_kind=Subquery(_resolution.values("kind")[:1]),
        resolution_amount=Subquery(_resolution.values("amount")[:1]),
    )
    if chosen == FILTER_OPEN:
        rows = rows.filter(balance__gt=0).exclude(status__in=_CLOSED_ONE_OFF)
    elif chosen == FILTER_PAID:
        rows = rows.filter(status=OneOffChargeStatus.PAID)
    return rows.select_related("fee_type", "lease__operator", "lease__store").order_by(
        "-due_date", "-created_at", "id"
    )


def _insurance_rows(owner: Owner) -> QuerySet[InsuranceCharge]:
    settled = Q(payments__status=PaymentStatus.SETTLED, payments__kind=PaymentKind.INSURANCE)
    return (
        InsuranceCharge.objects.filter(_owned(owner))
        .annotate(
            paid_in=Sum("payments__amount", filter=settled & Q(payments__is_refund=False)),
            refunded=Sum("payments__amount", filter=settled & Q(payments__is_refund=True)),
        )
        .select_related("lease__operator", "lease__store")
        .order_by("-due_on", "id")
    )


def _money(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def _insurance_row(
    charge: InsuranceCharge,
    chosen: str,
    carried: Mapping[uuid.UUID, Decimal],
    collected: Mapping[uuid.UUID, Decimal],
) -> ChargeRow | None:
    """An insurance charge as a row, or ``None`` when ``chosen`` leaves it out. Its
    balance is the one :mod:`ledger.services.receivables` lists (re-debits included, so
    the tab's total agrees with the header); what its re-debits collected is paid on it."""
    paid_in = _money(getattr(charge, "paid_in", None))
    refunded = _money(getattr(charge, "refunded", None))
    redebited = collected.get(charge.pk, ZERO)
    paid = paid_in - refunded + redebited
    known = charge.amount is not None
    balance = receivables.insurance_owed(
        charge.amount,
        paid=paid_in,
        refunded=refunded,
        collected=redebited,
        carried=carried.get(charge.pk, ZERO),
        legacy_status=charge.legacy_status,
    )
    if chosen == FILTER_OPEN and (not known or balance <= 0):
        return None
    if chosen == FILTER_PAID and (not known or balance > 0):
        return None
    if not known:
        status = "Needs premium"
    elif balance > 0:
        status = "Owed"
    elif carried.get(charge.pk, ZERO) > 0:
        # 12d-2: a live re-debit collects what is left, so nothing is owed here.
        status = CARRIED_BY_REDEBIT
    else:
        status = "Paid"
    return ChargeRow(
        kind=KIND_INSURANCE,
        charge_id=charge.pk,
        kind_label="Insurance",
        description=f"Insurance, policy year {charge.policy_year}",
        due=charge.due_on,
        billed=charge.amount,
        paid=paid,
        balance=balance,
        status=status,
        url="",
        lease=charge.lease,
        badges=["Needs review"] if charge.needs_review else [],
    )


def _invoice_links(
    rent_ids: list[uuid.UUID],
    one_off_ids: list[uuid.UUID],
    insurance_ids: list[uuid.UUID],
    billing: bool,
) -> dict[tuple[str, uuid.UUID], list[InvoiceLink]]:
    """Per (kind, charge id), the invoices billing it, linked to the invoice builder's
    page of the invoice when it has one (three queries)."""
    pairs: dict[tuple[str, uuid.UUID], dict[uuid.UUID, str]] = {}

    def add(kind: str, charge: uuid.UUID | None, invoice: uuid.UUID, number: str) -> None:
        if charge is not None:
            pairs.setdefault((kind, charge), {})[invoice] = number

    for invoice, number, rent, one_off, insurance in Invoice.objects.filter(
        Q(rent_charge__in=rent_ids)
        | Q(one_off_charge__in=one_off_ids)
        | Q(insurance_charge__in=insurance_ids)
    ).values_list("pk", "invoice_number", "rent_charge", "one_off_charge", "insurance_charge"):
        add(KIND_RENT, rent, invoice, number)
        add(KIND_ONE_OFF, one_off, invoice, number)
        add(KIND_INSURANCE, insurance, invoice, number)
    for invoice, number, rent, one_off in InvoiceLineItem.objects.filter(
        Q(rent_charge__in=rent_ids) | Q(one_off_charge__in=one_off_ids)
    ).values_list("invoice", "invoice__invoice_number", "rent_charge", "one_off_charge"):
        add(KIND_RENT, rent, invoice, number)
        add(KIND_ONE_OFF, one_off, invoice, number)
    invoices = {invoice for found in pairs.values() for invoice in found}
    drafts = dict(
        InvoiceDraft.objects.filter(sent_invoice__in=invoices).values_list("sent_invoice", "pk")
    )
    route = "ledger:invoices:draft" if billing else "ledger:invoices:preview"
    return {
        key: [
            InvoiceLink(
                number,
                reverse(route, kwargs={"pk": drafts[invoice]}) if invoice in drafts else "",
            )
            for invoice, number in sorted(found.items(), key=lambda pair: pair[1])
        ]
        for key, found in pairs.items()
    }


def _rent_description(charge: RentCharge) -> str:
    text = f"Rent {month_text(charge.period_month)}" + (
        f" (debit {charge.seq})" if charge.seq > 1 else ""
    )
    return f"{text}: {charge.proration_text}" if charge.proration_text else text


def _rent_row(charge: RentCharge, billing: bool, back: str) -> ChargeRow:
    on_plan = bool(getattr(charge, "on_plan", False))
    open_charge = charge.balance > 0 and charge.status != RentChargeStatus.WAIVED
    return ChargeRow(
        kind=KIND_RENT,
        charge_id=charge.pk,
        kind_label="Rent",
        description=_rent_description(charge),
        due=charge.due_on,
        billed=charge.amount_due,
        paid=charge.amount_paid,
        balance=charge.balance,
        status=charge.get_status_display(),
        url=reverse("ledger:charges:detail", kwargs={"pk": charge.pk}),
        lease=charge.lease,
        payment_url=(
            reverse("ledger:charges:record_payment", kwargs={"pk": charge.pk}) + back
            if billing and open_charge and not on_plan
            else ""
        ),
        badges=[
            *(["On installment plan"] if on_plan else []),
            *(["Needs review"] if charge.needs_review else []),
        ],
    )


def _one_off_row(charge: OneOffCharge, role: Role | None, back: str) -> ChargeRow:
    billing = meets(role, Role.BILLING)
    on_plan = bool(getattr(charge, "on_plan", False))
    open_charge = charge.balance > 0 and charge.status not in _CLOSED_ONE_OFF
    deposit = charge.fee_type is not None and charge.fee_type.refundable
    resolvable = (
        meets(role, Role.ADMIN)
        and deposit
        and charge.lease is not None
        and charge.lease.ended_on is not None
        and getattr(charge, "resolution_kind", None) is None
    )
    kind = getattr(charge, "resolution_kind", None)
    resolution = (
        f"{DepositResolutionKind(kind).label} "
        f"{format_amount(_money(getattr(charge, 'resolution_amount', None)))}"
        if kind
        else ""
    )
    return ChargeRow(
        kind=KIND_ONE_OFF,
        charge_id=charge.pk,
        kind_label=charge.get_category_display(),
        description=charge.label,
        due=charge.due_date,
        billed=charge.price,
        paid=charge.amount_paid,
        balance=charge.balance,
        status=charge.get_status_display(),
        url=reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk}),
        lease=charge.lease,
        payment_url=(
            reverse("ledger:one_off_charges:payment", kwargs={"pk": charge.pk}) + back
            if billing and open_charge and not on_plan
            else ""
        ),
        resolve_url=(
            reverse(
                "ledger:leases:resolve_deposit",
                kwargs={"pk": charge.lease_id, "charge": charge.pk},
            )
            if resolvable
            else ""
        ),
        resolution=resolution,
        badges=[
            *(["Deposit"] if deposit else []),
            *(["On installment plan"] if on_plan else []),
            *(["Needs review"] if charge.needs_review else []),
        ],
    )


def _as_receivables(row: ChargeRow, balances: StoreBalances) -> None:
    """Give an open rent or one-off row the balance the header counts (finding 12d-2):
    less what a live re-debit carries, "Carried by re-debit" once that is all of it."""
    key = (row.kind, row.charge_id)
    if key in balances.owed:
        row.balance = balances.owed[key]
    elif key in balances.carried:
        row.balance = ZERO
    if key in balances.carried and row.balance <= 0:
        row.status = CARRIED_BY_REDEBIT


def charges_tab(
    owner: Owner, chosen: str, role: Role | None, balances: StoreBalances | None = None
) -> dict[str, Any]:
    """The Charges tab (D3): the rent, one-off and insurance charges of the store's
    leases (of the operator's, with the store named, on the operator page: D14) in one
    table, newest due first, filtered ``open`` (the default), ``paid`` or ``all``; each
    with its invoices and, for billing, its payment form (which returns to the page). An
    open rent or one-off charge shows the balance of
    :func:`ledger.services.receivables.open_balances` (a rescheduled return's carried
    amount subtracted, finding 12d-2), as the insurance row does, so the open charges
    total the header's balance; ``balances`` is the header's, read here when not given.
    Seven queries whatever the number of charges, besides ``balances``."""
    billing = meets(role, Role.BILLING)
    back = _back(owner, TAB_PAYMENTS)
    if balances is None:
        today = timezone.localdate()
        balances = (
            _balances_of(*_open_rows(today, owner.pk))
            if isinstance(owner, Operator)
            else store_balances(owner, owner.leases.all(), today)
        )
    rows: list[ChargeRow] = []
    for rent in _rent_rows(owner, chosen):
        rent_row = _rent_row(rent, billing, back)
        if rent.balance > 0 and rent.status != RentChargeStatus.WAIVED:
            _as_receivables(rent_row, balances)
        rows.append(rent_row)
    for one_off in _one_off_rows(owner, chosen):
        one_off_row = _one_off_row(one_off, role, back)
        if one_off.balance > 0 and one_off.status not in _CLOSED_ONE_OFF:
            _as_receivables(one_off_row, balances)
        rows.append(one_off_row)
    if chosen == FILTER_OPEN:
        # A charge a re-debit carries in full is not open any more.
        rows = [listed for listed in rows if listed.balance > 0]
    insurance = list(_insurance_rows(owner))
    carried, collected = (
        receivables.insurance_redebits([charge.pk for charge in insurance])
        if insurance
        else ({}, {})
    )
    rows += [
        row for charge in insurance if (row := _insurance_row(charge, chosen, carried, collected))
    ]
    links = _invoice_links(
        [row.charge_id for row in rows if row.kind == KIND_RENT],
        [row.charge_id for row in rows if row.kind == KIND_ONE_OFF],
        [row.charge_id for row in rows if row.kind == KIND_INSURANCE],
        billing,
    )
    for row in rows:
        row.invoices = links.get((row.kind, row.charge_id), [])
    rows.sort(key=lambda row: (row.due, row.kind, str(row.charge_id)), reverse=True)
    return {
        "charge_rows": rows,
        "charge_filter": chosen,
        "charge_filters": CHARGE_FILTERS,
        "charge_total": sum((row.balance for row in rows), ZERO),
        "several_leases": len({row.lease.pk for row in rows if row.lease is not None}) > 1,
        "by_store": isinstance(owner, Operator),
        "export_query": urlencode(_owner_query(owner)),
    }


# --- Invoices tab ---------------------------------------------------------------------------


def store_invoices(store: Store) -> QuerySet[Invoice]:
    """The invoices billing a charge of the store's leases (directly or by a line)."""
    on_store = (
        Q(rent_charge__lease__store=store)
        | Q(one_off_charge__lease__store=store)
        | Q(insurance_charge__lease__store=store)
        | Q(line_items__one_off_charge__lease__store=store)
        | Q(line_items__rent_charge__lease__store=store)
    )
    return Invoice.objects.filter(pk__in=Invoice.objects.filter(on_store).values("pk")).order_by(
        "-issue_date", "-invoice_number"
    )


def _invoice_stores(invoice_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Per invoice, the labels of the stores whose charges it bills, joined (two
    queries)."""
    found: dict[uuid.UUID, set[str]] = {}
    for invoice, *labels in Invoice.objects.filter(pk__in=invoice_ids).values_list(
        "pk",
        "rent_charge__lease__store__external_label",
        "one_off_charge__lease__store__external_label",
        "insurance_charge__lease__store__external_label",
    ):
        found.setdefault(invoice, set()).update(label for label in labels if label)
    for invoice, *labels in InvoiceLineItem.objects.filter(invoice__in=invoice_ids).values_list(
        "invoice",
        "rent_charge__lease__store__external_label",
        "one_off_charge__lease__store__external_label",
    ):
        found.setdefault(invoice, set()).update(label for label in labels if label)
    return {invoice: ", ".join(sorted(labels)) for invoice, labels in found.items()}


def invoices_tab(owner: Owner, chosen: str, role: Role | None) -> dict[str, Any]:
    """The Invoices tab (D3): the never-sent drafts (Edit, Send, Delete for billing),
    then the invoices with the status here and at the payment provider, filtered
    ``unpaid`` (the default: neither closed nor void) or ``all``. A store's are those of
    its leases; an operator's (D14) every one billing it, its operator-level drafts
    included, each with the stores it bills."""
    billing = meets(role, Role.BILLING)
    by_operator = isinstance(owner, Operator)
    if isinstance(owner, Operator):
        invoices = Invoice.objects.filter(operator=owner).order_by("-issue_date", "-invoice_number")
        drafts = InvoiceDraft.objects.filter(operator=owner)
        # Send and Delete come back to the operator page's Invoices tab.
        edit_query, return_query = "", _back(owner, TAB_INVOICES)
    else:
        invoices = store_invoices(owner)
        drafts = InvoiceDraft.objects.filter(lease__store=owner)
        edit_query = return_query = _back(owner, TAB_INVOICES)
    if chosen == FILTER_UNPAID:
        invoices = invoices.exclude(internal_status__in=[InvoiceStatus.CLOSED, InvoiceStatus.VOID])
    invoices_shown = list(invoices.select_related("operator"))
    drafts_by_invoice = dict(
        InvoiceDraft.objects.filter(
            sent_invoice__in=[row.pk for row in invoices_shown]
        ).values_list("sent_invoice", "pk")
    )
    route = "ledger:invoices:draft" if billing else "ledger:invoices:preview"
    stores = _invoice_stores([row.pk for row in invoices_shown]) if by_operator else {}
    return {
        "invoice_filter": chosen,
        "invoice_filters": INVOICE_FILTERS,
        "drafts": list(
            drafts.filter(status=InvoiceDraftStatus.DRAFT)
            .select_related("operator", "lease__store")
            .annotate(total=Sum("lines__line_total"))
            .order_by("-created_at", "id")
        ),
        "draft_edit_query": edit_query,
        "draft_return_query": return_query,
        "by_store": by_operator,
        "invoice_rows": [
            {
                "invoice": invoice,
                "stores": stores.get(invoice.pk, ""),
                "url": (
                    reverse(route, kwargs={"pk": drafts_by_invoice[invoice.pk]})
                    if invoice.pk in drafts_by_invoice
                    else ""
                ),
                "cancel_url": (
                    reverse("ledger:invoices:cancel", kwargs={"pk": drafts_by_invoice[invoice.pk]})
                    if billing
                    and invoice.pk in drafts_by_invoice
                    and invoice.internal_status not in (InvoiceStatus.CLOSED, InvoiceStatus.VOID)
                    else ""
                ),
            }
            for invoice in invoices_shown
        ],
    }


# --- Payments tab --------------------------------------------------------------------------


def payments_tab(owner: Owner) -> dict[str, Any]:
    """The Payments tab (D3, D6b): the newest :data:`PAYMENTS_LIMIT` rows of the payments
    ledger for this store (this operator, with the store named: D14), with the link to
    the ledger filtered on it, and the credits of the leases with what applied or paid
    them back."""
    if isinstance(owner, Operator):
        selected = payments_ledger.PaymentFilter(operator=owner.pk)
        credit_rows = credits.rows_for_operator(owner)
    else:
        selected = payments_ledger.PaymentFilter(store=owner.pk)
        credit_rows = credits.rows_for_store(owner)
    shown = list(payments_ledger.ledger_payments(selected)[: PAYMENTS_LIMIT + 1])
    return {
        "by_store": isinstance(owner, Operator),
        "credit_rows": credit_rows,
        "payment_rows": payments_ledger.rows_for(shown[:PAYMENTS_LIMIT]),
        "payments_more": len(shown) > PAYMENTS_LIMIT,
        "payments_limit": PAYMENTS_LIMIT,
        "payments_ledger_url": reverse("ledger:payments:list")
        + "?"
        + urlencode(selected.as_query()),
    }


# --- Schedules tab -------------------------------------------------------------------------


def schedules_tab(owner: Owner, today: date, now: datetime) -> dict[str, Any]:
    """The Schedules tab (D3): the live rent and insurance schedules of the store's leases
    (of the operator's, with the store named: D14; as the lease page shows them: next
    debit, masked provider schedule, why one needs review), the open one-off charges
    collected by autopay on their due date, and the live installment plans."""
    schedules = list(
        CollectionSchedule.objects.filter(_owned(owner))
        .exclude(state=ScheduleState.ENDED)
        .select_related("lease__store")
        .order_by("lease__started_on", "lease_id", "kind", "seq")
    )
    moves: dict[uuid.UUID, leases.AutopayMove | None] = {}
    rows = []
    for row in schedules:
        if row.lease_id not in moves:
            moves[row.lease_id] = leases.autopay_move(row.lease, now)
        moved = moves[row.lease_id]
        ref = leases.schedule_ref(row)
        next_run = (
            leases.next_debit_at(row.debit_day, now)
            if row.state == ScheduleState.ACTIVE and row.debit_day is not None
            else None
        )
        if next_run is not None and moved is not None and str(row.pk) in moved.starts:
            next_run = moved.starts[str(row.pk)]
        rows.append(
            {
                "row": row,
                "schedule_masked": mask_identifier(ref) if ref else "",
                "next_run": next_run,
                "review_message": (
                    leases.schedule_review_message(row.lease, row, today)
                    if row.kind == ScheduleKind.RENT
                    else ""
                ),
            }
        )
    return {
        "by_store": isinstance(owner, Operator),
        "schedule_rows": rows,
        "autopay_moved": [move.text for move in moves.values() if move is not None],
        "autopay_charges": list(
            OneOffCharge.objects.filter(
                _owned(owner, direct=True),
                collection=CollectionMethod.AUTOPAY_DUE,
                balance__gt=0,
            )
            .exclude(status__in=_CLOSED_ONE_OFF)
            .select_related("fee_type", "lease__store")
            .order_by("due_date", "created_at", "id")
        ),
        "live_plans": list(
            InstallmentPlan.objects.filter(
                _owned(owner, direct=True), status__in=LIVE_PLAN_STATUSES
            )
            .select_related("rent_charge", "one_off_charge__fee_type", "lease__store")
            .order_by("first_due", "id")
        ),
        "lease_page_leases": sorted(
            {row.lease for row in schedules},
            key=lambda lease: (str(lease.started_on), str(lease.pk)),
        ),
    }


# --- History tab ---------------------------------------------------------------------------


def history_tab(store: Store, today: date, operator: Operator | None = None) -> dict[str, Any]:
    """The History tab (D3, admin): the newest rows of the store's records (and of
    ``operator`` itself, on the operator page: D14), and the audit page filtered on the
    store's."""
    return {
        "history_rows": audit.rows_for_store(store, operator=operator),
        "history_size": audit.STORE_HISTORY_SIZE,
        "history_url": reverse("ledger:audit:page")
        + "?"
        + urlencode(audit.store_query(store, today)),
    }


# --- The page ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Workspace:
    store: Store
    header: Header
    actions: list[Action]
    tabs: list[tuple[str, str]]
    tab: str
    #: The open tab's data (empty for Documents and Contact log, built by their views).
    data: dict[str, Any]
    #: The Charges tab's Create missing charges (a POST), or "" (:func:`_create_charges_url`).
    create_charges_url: str = ""


def _tab_data(
    owner: Owner,
    tab: str,
    chosen: str,
    role: Role | None,
    balances: StoreBalances,
    today: date,
    now: datetime,
) -> dict[str, Any]:
    """The open tab's data over the records of ``owner`` (a store, or an operator); empty
    for the tabs built elsewhere (Documents, Contact log, History)."""
    if tab == TAB_CHARGES:
        return charges_tab(owner, _chosen_filter(chosen, CHARGE_FILTERS), role, balances)
    if tab == TAB_INVOICES:
        return invoices_tab(owner, _chosen_filter(chosen, INVOICE_FILTERS), role)
    if tab == TAB_PAYMENTS:
        return payments_tab(owner)
    if tab == TAB_SCHEDULES:
        return schedules_tab(owner, today, now)
    return {}


def store_workspace(
    store: Store,
    today: date,
    role: Role | None,
    *,
    tab: str = TAB_CHARGES,
    chosen: str = "",
    now: datetime | None = None,
) -> Workspace:
    """The header, the action bar and the open tab of ``store`` for ``role`` (D1); ``tab``
    is a name :func:`chosen_tab` returned, ``chosen`` the tab's filter (``?filter=``)."""
    moment = now or timezone.now()
    balances = store_balances(store, store.leases.all(), today)
    header = _header(
        lease_end.current_lease(store, today),
        today,
        moment,
        balances.total,
        len(balances.owed),
    )
    return Workspace(
        store=store,
        header=header,
        actions=_actions(store, header, role),
        tabs=tabs_for(role),
        tab=tab,
        data=(
            history_tab(store, today)
            if tab == TAB_HISTORY and meets(role, Role.ADMIN)
            else _tab_data(store, tab, chosen, role, balances, today, moment)
        ),
        create_charges_url=_create_charges_url(store, header, role),
    )


# --- The operator page (D14) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorWorkspace:
    """The operator page as the workspace: :class:`Workspace` of the selected store, its
    header and bar limited to the operator's lease, the tabs over every store of the
    operator, and the operator's own figures."""

    operator: Operator
    #: The operator's stores, by label: the selector's choices.
    stores: list[Store]
    #: The selected store's workspace; ``None`` when the operator has no store.
    space: Workspace | None
    #: The open tab and its data, across the operator's stores.
    tabs: list[tuple[str, str]]
    tab: str
    data: dict[str, Any]
    #: The open balance of every store of the operator (D14: the operator-level total).
    open_balance: Decimal
    #: The open charges of every store of the operator (PLAN-012f D8: the Charges tab's pill).
    open_count: int = 0


def operator_stores(operator: Operator) -> list[Store]:
    """The stores of the operator's leases, by label (the selector's choices)."""
    return list(
        Store.objects.filter(leases__operator=operator).distinct().order_by("external_label", "id")
    )


def _selected(stores: list[Store], raw: str | None) -> Store | None:
    """The store ``?store=`` names when it is one of ``stores``, else the first; a store
    of another operator is ignored."""
    for store in stores:
        if raw and str(store.pk) == raw.strip():
            return store
    return stores[0] if stores else None


def _operator_lease(store: Store, operator: Operator, today: date) -> Lease | None:
    """The store's current lease when it is the operator's; ``None`` otherwise, so that
    the operator page never shows or acts on another operator's lease."""
    lease = lease_end.current_lease(store, today)
    return lease if lease is not None and lease.operator_id == operator.pk else None


def _operator_actions(
    operator: Operator, store: Store, header: Header, role: Role | None
) -> list[Action]:
    """The bar of the selected store on the operator page: the store page's actions on
    the operator's current lease of it (none when the store's current lease is not the
    operator's). Create invoice gets its own element id and is not the primary: the
    page's New invoice is (operator-level, PLAN-012e-2 D6)."""
    if header.lease is None:
        return []
    return [
        replace(action, element_id="bar-new-invoice", primary=False)
        if action.key == "create_invoice"
        else action
        for action in _actions(store, header, role)
    ]


def operator_workspace(
    operator: Operator,
    today: date,
    role: Role | None,
    *,
    tab: str = TAB_CHARGES,
    store: str | None = None,
    chosen: str = "",
    now: datetime | None = None,
) -> OperatorWorkspace:
    """The operator page as the workspace (D14) for ``role``: the selected store (``store``
    is the raw ``?store=``; the operator's only store by default, else the first by
    label; another operator's store is ignored) with its header card and action bar on
    the operator's lease of it, and the open tab across all the operator's stores
    (Charges, Invoices, Payments, Schedules; Documents and History are the selected
    store's, History with the operator's own rows; the contact log is the page's). One
    receivables read gives the store's figures and the operator's total."""
    moment = now or timezone.now()
    stores = operator_stores(operator)
    selected = _selected(stores, store)
    rows, carried = _open_rows(today, operator.pk)
    balances = _balances_of(rows, carried)
    space: Workspace | None = None
    if selected is not None:
        mine = _balances_of(rows, carried, selected.pk)
        header = _header(
            _operator_lease(selected, operator, today), today, moment, mine.total, len(mine.owed)
        )
        space = Workspace(
            store=selected,
            header=header,
            actions=_operator_actions(operator, selected, header, role),
            tabs=tabs_for(role),
            tab=tab,
            data={},
            create_charges_url=_create_charges_url(selected, header, role),
        )
    if tab == TAB_HISTORY:
        data = (
            history_tab(selected, today, operator)
            if selected is not None and meets(role, Role.ADMIN)
            else {}
        )
    else:
        data = _tab_data(operator, tab, chosen, role, balances, today, moment)
    return OperatorWorkspace(
        operator=operator,
        stores=stores,
        space=space,
        tabs=tabs_for(role),
        tab=tab,
        data=data,
        open_balance=balances.total,
        open_count=len(balances.owed),
    )
