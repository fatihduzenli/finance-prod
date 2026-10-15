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
- the **action bar** (D2): which actions the role may take on this store, each pointing at
  an existing form with the store and its lease in the query string (``?store=``,
  ``?lease=``); a store without a current lease offers New lease instead of the lease's
  actions; a viewer gets none;
- the **open tab** only (D3, ``?tab=``, Charges by default): Charges, Invoices, Payments,
  Schedules and History are built here; Documents and Contact log are built by their own
  view helpers (:mod:`ledger.views.documents`, :mod:`ledger.views.contact_log`). History
  is for admins; another role asking for it gets Charges.

Nothing here writes or calls the payment provider. Money is ``Decimal``; provider
identifiers are masked.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery, Sum
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role, meets
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
    (TAB_SCHEDULES, "Schedules"),
    (TAB_DOCUMENTS, "Documents"),
    (TAB_CONTACTS, "Contact log"),
    (TAB_HISTORY, "History"),
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

CENT = Decimal("0.01")
KIND_RENT = "rent"
KIND_ONE_OFF = "one_off"
KIND_INSURANCE = "insurance"

AUTOPAY_NONE = "Not set up"
NEXT_RENT = "Rent debit"
NEXT_REDEBIT = "Re-debit of a returned payment"

_CLOSED_ONE_OFF = (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)


def tab_url(store_id: uuid.UUID, tab: str) -> str:
    """The store page opened on ``tab``, scrolled to it (D8)."""
    page = reverse("ledger:stores:detail", kwargs={"pk": store_id})
    return f"{page}?tab={tab}#tab-{tab}"


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
    #: "Active", "Paused", "Failed", "Not created" or :data:`AUTOPAY_NONE`.
    autopay: str
    #: Why autopay starts later (a split month), or ``""``.
    autopay_moved: str
    open_balance: Decimal
    credit: Decimal
    next_payment: NextPayment | None
    open_promise: ContactEntry | None


def _autopay_state(schedules: list[CollectionSchedule]) -> str:
    states = {row.state for row in schedules}
    for state in (ScheduleState.ACTIVE, ScheduleState.PAUSED, ScheduleState.FAILED):
        if state in states:
            return str(ScheduleState(state).label)
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


def _open_balance(store: Store, operators: Iterable[uuid.UUID], today: date) -> Decimal:
    """The store's rows of the open balances of every operator its leases bill."""
    rows = [
        row
        for operator in operators
        for row in receivables.open_balances(today, operator=operator)
        if row.store_id == store.pk
    ]
    return receivables.total(rows)


def _header(store: Store, store_leases: list[Lease], today: date, now: datetime) -> Header:
    lease = lease_end.current_lease(store, today)
    operators = {row.operator_id for row in store_leases if row.operator_id is not None}
    balance = _open_balance(store, sorted(operators, key=str), today)
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
    )


# --- The action bar (D2) -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    key: str
    label: str
    url: str
    element_id: str
    danger: bool = False


def _with(url: str, **params: str) -> str:
    return f"{url}?{urlencode(params)}"


def _actions(store: Store, header: Header, role: Role | None, today: date) -> list[Action]:
    """The bar's actions ``role`` may take (D2), in bar order; none for a viewer."""
    billing = meets(role, Role.BILLING)
    admin = meets(role, Role.ADMIN)
    if not billing:
        return []
    found: list[Action] = []
    lease = header.lease
    known = {"store": str(store.pk)}
    if lease is not None:
        known["lease"] = str(lease.pk)
        operator = header.operator
        if operator is not None and operator.archived_at is None:
            found.append(
                Action(
                    "create_invoice",
                    "Create invoice",
                    _with(reverse("ledger:invoices:new_store", kwargs={"pk": store.pk}), **known),
                    "new-invoice",
                )
            )
        found.append(
            Action(
                "record_payment",
                "Record a payment",
                reverse("ledger:stores:record_payment", kwargs={"pk": store.pk}),
                "bar-record-payment",
            )
        )
        if admin:
            found.append(
                Action(
                    "charge_now",
                    "Charge now",
                    reverse("ledger:stores:charge_now_pick", kwargs={"pk": store.pk}),
                    "bar-charge-now",
                )
            )
        setup = reverse("ledger:leases:add_setup_charge", kwargs={"pk": lease.pk})
        found.append(
            Action(
                "add_setup_charge",
                "Add setup charge",
                _with(setup, **known),
                "bar-add-setup-charge",
            )
        )
        # "Schedule a payment" arrives in Batch B as ``ledger:stores:schedule_payment``.
    found.append(
        Action(
            "upload_document",
            "Upload document",
            tab_url(store.pk, TAB_DOCUMENTS),
            "bar-upload-document",
        )
    )
    contact_lease = contacts.store_contact_lease(store, today)
    if contact_lease is not None:
        found.append(
            Action(
                "add_contact",
                "Add contact entry",
                _with(
                    reverse("ledger:stores:contacts_add", kwargs={"pk": store.pk}),
                    store=str(store.pk),
                    lease=str(contact_lease.pk),
                ),
                "bar-add-contact",
            )
        )
    if not admin:
        return found
    if lease is None:
        found.append(
            Action(
                "new_lease",
                "New lease",
                _with(reverse("ledger:leases:create"), store=str(store.pk)),
                "bar-new-lease",
            )
        )
        return found
    found.append(
        Action(
            "edit_lease",
            "Edit lease",
            _with(reverse("ledger:leases:edit", kwargs={"pk": lease.pk}), **known),
            "bar-edit-lease",
        )
    )
    found.append(
        Action(
            "transfer",
            "Transfer to another operator",
            reverse("ledger:stores:transfer", kwargs={"pk": store.pk}),
            "store-transfer",
        )
    )
    found.append(
        Action(
            "end_lease",
            "End lease",
            reverse("ledger:stores:end_lease", kwargs={"pk": store.pk}),
            "store-end-lease",
            danger=True,
        )
    )
    return found


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


def _rent_rows(store: Store, chosen: str) -> QuerySet[RentCharge]:
    rows = RentCharge.objects.filter(lease__store=store).annotate(
        on_plan=_on_live_plan("rent_charge")
    )
    if chosen == FILTER_OPEN:
        rows = rows.filter(balance__gt=0).exclude(status=RentChargeStatus.WAIVED)
    elif chosen == FILTER_PAID:
        rows = rows.filter(status=RentChargeStatus.PAID)
    return rows.select_related("lease__operator").order_by("-due_on", "-seq", "id")


_resolution = DepositResolution.objects.filter(setup_charge=OuterRef("pk"))


def _one_off_rows(store: Store, chosen: str) -> QuerySet[OneOffCharge]:
    rows = OneOffCharge.objects.filter(lease__store=store).annotate(
        on_plan=_on_live_plan("one_off_charge"),
        resolution_kind=Subquery(_resolution.values("kind")[:1]),
        resolution_amount=Subquery(_resolution.values("amount")[:1]),
    )
    if chosen == FILTER_OPEN:
        rows = rows.filter(balance__gt=0).exclude(status__in=_CLOSED_ONE_OFF)
    elif chosen == FILTER_PAID:
        rows = rows.filter(status=OneOffChargeStatus.PAID)
    return rows.select_related("fee_type", "lease__operator").order_by(
        "-due_date", "-created_at", "id"
    )


def _insurance_rows(store: Store) -> QuerySet[InsuranceCharge]:
    settled = Q(payments__status=PaymentStatus.SETTLED, payments__kind=PaymentKind.INSURANCE)
    return (
        InsuranceCharge.objects.filter(lease__store=store)
        .annotate(
            paid_in=Sum("payments__amount", filter=settled & Q(payments__is_refund=False)),
            refunded=Sum("payments__amount", filter=settled & Q(payments__is_refund=True)),
        )
        .select_related("lease__operator")
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
    status = "Needs premium" if not known else ("Paid" if balance <= 0 else "Owed")
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
    text = f"Rent {charge.period_month:%Y-%m}" + (
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
        f"${_money(getattr(charge, 'resolution_amount', None)).quantize(CENT)}"
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


def charges_tab(store: Store, chosen: str, role: Role | None) -> dict[str, Any]:
    """The Charges tab (D3): the rent, one-off and insurance charges of the store's
    leases in one table, newest due first, filtered ``open`` (the default), ``paid`` or
    ``all``; each with its invoices and, for billing, its payment form (which returns to
    the store page). Seven queries whatever the number of charges."""
    billing = meets(role, Role.BILLING)
    back = "?" + urlencode({"store": str(store.pk)})
    rows = [_rent_row(charge, billing, back) for charge in _rent_rows(store, chosen)]
    rows += [_one_off_row(charge, role, back) for charge in _one_off_rows(store, chosen)]
    insurance = list(_insurance_rows(store))
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


def invoices_tab(store: Store, chosen: str, role: Role | None) -> dict[str, Any]:
    """The Invoices tab (D3): the store's never-sent drafts (Edit, Send, Delete for
    billing), then its invoices with the status here and at the payment provider, filtered
    ``unpaid`` (the default: neither closed nor void) or ``all``."""
    billing = meets(role, Role.BILLING)
    invoices = store_invoices(store)
    if chosen == FILTER_UNPAID:
        invoices = invoices.exclude(internal_status__in=[InvoiceStatus.CLOSED, InvoiceStatus.VOID])
    invoices_shown = list(invoices.select_related("operator"))
    drafts_by_invoice = dict(
        InvoiceDraft.objects.filter(
            sent_invoice__in=[row.pk for row in invoices_shown]
        ).values_list("sent_invoice", "pk")
    )
    route = "ledger:invoices:draft" if billing else "ledger:invoices:preview"
    return {
        "invoice_filter": chosen,
        "invoice_filters": INVOICE_FILTERS,
        "drafts": list(
            InvoiceDraft.objects.filter(lease__store=store, status=InvoiceDraftStatus.DRAFT)
            .select_related("operator", "lease__store")
            .annotate(total=Sum("lines__line_total"))
            .order_by("-created_at", "id")
        ),
        "invoice_rows": [
            {
                "invoice": invoice,
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


def payments_tab(store: Store) -> dict[str, Any]:
    """The Payments tab (D3): the newest :data:`PAYMENTS_LIMIT` rows of the payments
    ledger for this store, with the link to the ledger filtered on it."""
    selected = payments_ledger.PaymentFilter(store=store.pk)
    shown = list(payments_ledger.ledger_payments(selected)[: PAYMENTS_LIMIT + 1])
    return {
        "payment_rows": payments_ledger.rows_for(shown[:PAYMENTS_LIMIT]),
        "payments_more": len(shown) > PAYMENTS_LIMIT,
        "payments_limit": PAYMENTS_LIMIT,
        "payments_ledger_url": reverse("ledger:payments:list")
        + "?"
        + urlencode(selected.as_query()),
    }


# --- Schedules tab -------------------------------------------------------------------------


def schedules_tab(store: Store, today: date, now: datetime) -> dict[str, Any]:
    """The Schedules tab (D3): the live rent and insurance schedules of the store's leases
    (as the lease page shows them: next debit, masked provider schedule, why one needs
    review), the open one-off charges collected by autopay on their due date, and the
    live installment plans."""
    schedules = list(
        CollectionSchedule.objects.filter(lease__store=store)
        .exclude(state=ScheduleState.ENDED)
        .select_related("lease")
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
        "schedule_rows": rows,
        "autopay_moved": [move.text for move in moves.values() if move is not None],
        "autopay_charges": list(
            OneOffCharge.objects.filter(
                lease__store=store, collection=CollectionMethod.AUTOPAY_DUE, balance__gt=0
            )
            .exclude(status__in=_CLOSED_ONE_OFF)
            .select_related("fee_type")
            .order_by("due_date", "created_at", "id")
        ),
        "live_plans": list(
            InstallmentPlan.objects.filter(lease__store=store, status__in=LIVE_PLAN_STATUSES)
            .select_related("rent_charge", "one_off_charge__fee_type")
            .order_by("first_due", "id")
        ),
        "lease_page_leases": sorted(
            {row.lease for row in schedules},
            key=lambda lease: (str(lease.started_on), str(lease.pk)),
        ),
    }


# --- History tab ---------------------------------------------------------------------------


def history_tab(store: Store, today: date) -> dict[str, Any]:
    """The History tab (D3, admin): the newest rows of the store's records, and the
    audit page filtered on them."""
    return {
        "history_rows": audit.rows_for_store(store),
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
    store_leases = list(store.leases.all())
    header = _header(store, store_leases, today, moment)
    data: dict[str, Any] = {}
    if tab == TAB_CHARGES:
        data = charges_tab(store, _chosen_filter(chosen, CHARGE_FILTERS), role)
    elif tab == TAB_INVOICES:
        data = invoices_tab(store, _chosen_filter(chosen, INVOICE_FILTERS), role)
    elif tab == TAB_PAYMENTS:
        data = payments_tab(store)
    elif tab == TAB_SCHEDULES:
        data = schedules_tab(store, today, moment)
    elif tab == TAB_HISTORY and meets(role, Role.ADMIN):
        data = history_tab(store, today)
    return Workspace(
        store=store,
        header=header,
        actions=_actions(store, header, role, today),
        tabs=tabs_for(role),
        tab=tab,
        data=data,
    )
