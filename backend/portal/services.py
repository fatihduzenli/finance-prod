"""Everything a portal page reads or writes (PLAN-014 H3, H6). Every function takes the
signed-in :class:`portal.models.PortalUser` and reads only what is billed to its operator
(``operator_id``); no portal view touches a model.

**Isolation.** A store is the operator's when a lease of the operator names it
(``Lease.operator``, ended leases included): :func:`store_of` answers ``None`` for any other id
(another operator's, unknown, unreadable), and the views answer 404, never 403. What a store
page lists is filtered by the store **and** by the operator billed, so a store that passed
from one operator to another shows each only their own charges, invoices and payments.

**Pages.** :func:`home`: every store of the operator, by store number, an ended one (no lease
of the operator not ended) marked, with what is open on it. :func:`store_page`: the open
charges with their due dates and balances, autopay "on"/"off" (the rent schedule of the
operator's current lease, read only) and the bank on file ("····4821", the lease's or the one
it shares; "none"). :func:`invoices`: the invoices sent (number, date, amount, status: Open,
Paid or Void; drafts and invoices not sent are not shown). :func:`payments`: settled, pending
and returned payments, with the method and, for a bank debit, the last four of the bank on
file. :func:`statement`: the operator's statement, as the signed statement link shows it.
:func:`set_theme` and :func:`change_password` are the Account page's.

**Links.** :func:`accept_invitation` and :func:`reset_password` take the live token
:func:`portal.tokens.find` answered: the new password (Django's validators), the token used
once (a second request with it is refused), the account activated by an invitation; audited
``portal_user.activated`` and ``portal_user.password_reset``. A new password ends every other
session of the account at its next request.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, QuerySet
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.themes import THEMES
from ledger.models import (
    ZERO,
    AuditLog,
    CollectionStatus,
    Invoice,
    InvoiceStatus,
    Lease,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Store,
)
from ledger.services import messaging, receivables, reports, statement_links, workspace
from payments import lease_sync
from portal import tokens
from portal.models import PortalToken, PortalUser, TokenKind

#: What a portal page calls a bank account it has no last four for.
NO_BANK = "none"
#: The invoices an operator sees: sent ones (never a draft or one not sent yet).
SHOWN_INVOICE_STATUSES = (InvoiceStatus.SENT, InvoiceStatus.CLOSED, InvoiceStatus.VOID)
SHOWN_PAYMENT_STATUSES = (PaymentStatus.SETTLED, PaymentStatus.PENDING, PaymentStatus.RETURNED)

CURRENT_WRONG = "Your current password is not right."
MISMATCH = "The two new passwords are not the same."
UNKNOWN_THEME = "Choose Light, Dark or System."


# --- Stores ------------------------------------------------------------------------------------


def _leases(user: PortalUser) -> QuerySet[Lease]:
    return Lease.objects.filter(operator_id=user.operator_id)


def _is_current(lease: Lease) -> bool:
    return lease.ended_on is None and lease.collection_status != CollectionStatus.ENDED


def store_choices(user: PortalUser) -> list[Store]:
    """The operator's stores, for the store switcher (one query)."""
    return list(
        Store.objects.filter(pk__in=_leases(user).values("store_id")).order_by(
            F("store_number").asc(nulls_last=True), "external_label", "id"
        )
    )


def store_of(user: PortalUser, raw_id: object) -> Store | None:
    """The store ``raw_id`` names when a lease of the user's operator names it; else ``None``."""
    try:
        key = uuid.UUID(str(raw_id))
    except ValueError:
        return None
    return Store.objects.filter(pk=key, pk__in=_leases(user).values("store_id")).first()


@dataclass(frozen=True, slots=True)
class StoreRow:
    store: Store
    ended: bool
    balance: Decimal


def _open_rows(user: PortalUser, today: date) -> list[receivables.OpenBalance]:
    return receivables.open_balances(today, operator=user.operator_id)


def home(user: PortalUser, today: date) -> list[StoreRow]:
    """Every store of the operator with its open balance (see the module docstring)."""
    current: dict[uuid.UUID, bool] = defaultdict(bool)
    for lease in _leases(user).only("store_id", "ended_on", "collection_status"):
        current[lease.store_id] |= _is_current(lease)
    balances: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for row in _open_rows(user, today):
        if row.store_id is not None:
            balances[row.store_id] += row.balance
    return [
        StoreRow(store, ended=not current[store.pk], balance=balances[store.pk])
        for store in store_choices(user)
    ]


@dataclass(frozen=True, slots=True)
class StorePage:
    store: Store
    ended: bool
    open_charges: list[receivables.OpenBalance]
    balance: Decimal
    autopay: bool
    bank_last4: str


def _lease_of(user: PortalUser, store: Store) -> Lease | None:
    """The operator's current lease of the store, else its latest."""
    leases = list(
        _leases(user)
        .filter(store=store)
        .select_related("billing", "billing__shares_account_of__billing")
        .order_by(F("started_on").desc(nulls_last=True), "-created_at")
    )
    return next((lease for lease in leases if _is_current(lease)), leases[0] if leases else None)


def bank_last4(lease: Lease | None) -> str:
    """The last four of the bank the lease debits (its own, or the account it shares)."""
    if lease is None:
        return ""
    billing = lease_sync.billing_of(lease)
    shares = billing.shares_account_of
    owner = lease_sync.billing_of(shares) if shares is not None else billing
    return str(owner.bank_last4 or "")


def store_page(user: PortalUser, store: Store, today: date) -> StorePage:
    lease = _lease_of(user, store)
    rows = [row for row in _open_rows(user, today) if row.store_id == store.pk]
    current = lease is not None and _is_current(lease)
    return StorePage(
        store=store,
        ended=not current,
        open_charges=rows,
        balance=receivables.total(rows),
        autopay=current and lease is not None and messaging.rent_schedule_active(lease.pk),
        bank_last4=bank_last4(lease),
    )


# --- Invoices and payments ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvoiceRow:
    number: str
    issued: date
    total: Decimal
    status: str


def invoice_status(invoice: Invoice) -> str:
    if invoice.internal_status == InvoiceStatus.VOID:
        return "Void"
    return "Paid" if invoice.amount_paid >= invoice.total else "Open"


def invoices(user: PortalUser, store: Store) -> list[InvoiceRow]:
    """The invoices sent to the operator for charges of ``store``, newest first."""
    found = workspace.store_invoices(store).filter(
        operator_id=user.operator_id, internal_status__in=SHOWN_INVOICE_STATUSES
    )
    return [
        InvoiceRow(str(item.invoice_number), item.issue_date, item.total, invoice_status(item))
        for item in found
    ]


@dataclass(frozen=True, slots=True)
class PaymentRow:
    on: date | None
    description: str
    amount: Decimal
    is_refund: bool
    status: str
    method: str
    last4: str


def payments(user: PortalUser, store: Store) -> list[PaymentRow]:
    """The settled, pending and returned payments billed to the operator on ``store``'s
    charges, newest first."""
    found = (
        Payment.objects.select_related(
            "rent_charge", "insurance_charge", "one_off_charge__fee_type"
        )
        .annotate(
            billed=reports._billed_operator(),
            store_of=Coalesce(
                "rent_charge__lease__store",
                "insurance_charge__lease__store",
                "one_off_charge__lease__store",
            ),
            on=Coalesce("settled_on", "initiated_on"),
        )
        .filter(billed=user.operator_id, store_of=store.pk, status__in=SHOWN_PAYMENT_STATUSES)
        .order_by(F("on").desc(nulls_last=True), "-created_at")
    )
    last4 = bank_last4(_lease_of(user, store))
    return [
        PaymentRow(
            on=getattr(payment, "on", None),
            description=reports._charge_description(payment),
            amount=payment.amount,
            is_refund=payment.is_refund,
            status=str(payment.get_status_display()),
            method=str(payment.get_method_display()),
            last4=last4 if payment.method == PaymentMethod.ACH else "",
        )
        for payment in found
    ]


def statement(user: PortalUser, today: date) -> reports.Statement:
    """The operator's statement: the open charges and the last twelve months' payments."""
    return statement_links.statement(user.operator, today)


# --- Account -------------------------------------------------------------------------------


def set_theme(user: PortalUser, chosen: object) -> str:
    """Store the user's theme; the refusal, or ``""``."""
    if chosen not in THEMES:
        return UNKNOWN_THEME
    PortalUser.objects.filter(pk=user.pk).update(theme=chosen, updated_at=timezone.now())
    user.theme = str(chosen)
    return ""


def password_problems(password: str, confirm: str) -> list[str]:
    """Why a new password is refused (Django's validators), or nothing."""
    if password != confirm:
        return [MISMATCH]
    try:
        validate_password(password)
    except ValidationError as error:
        return list(error.messages)
    return []


def store_password(user: PortalUser, password: str) -> None:
    user.password = make_password(password)
    PortalUser.objects.filter(pk=user.pk).update(password=user.password, updated_at=timezone.now())


def change_password(user: PortalUser, current: object, new: object, confirm: object) -> list[str]:
    """The Account page's password change: the problems, or nothing once changed. Every other
    session of the user ends at its next request (:func:`portal.auth.current_user`)."""
    old, password, again = _text(current), _text(new), _text(confirm)
    if not user.password or not check_password(old, user.password):
        return [CURRENT_WRONG]
    problems = password_problems(password, again)
    if problems:
        return problems
    store_password(user, password)
    return []


# --- The one-time links' forms ----------------------------------------------------------------

LINK_USED = "This link was used already."
ACTIVATED = "portal_user.activated"
RESET_DONE = "portal_user.password_reset"


def _use_link(
    token: PortalToken, password: str, confirm: str, *, accept: bool
) -> tuple[PortalUser | None, list[str]]:
    problems = password_problems(password, confirm)
    if problems:
        return None, problems
    moment = timezone.now()
    with transaction.atomic():
        user = (
            PortalUser.objects.select_for_update().select_related("operator").get(pk=token.user_id)
        )
        if not tokens.use(token, moment):
            return None, [LINK_USED]
        store_password(user, password)
        if accept:
            user.is_active = True
            user.activated_at = moment
            user.save(update_fields=["is_active", "activated_at", "updated_at"])
        tokens.void_all(user, TokenKind.RESET, moment)
        AuditLog.objects.create(
            actor=None,
            action=ACTIVATED if accept else RESET_DONE,
            entity_type="portal_user",
            entity_id=str(user.pk),
            before=None,
            after={"operator": str(user.operator_id)},
        )
    return user, []


def accept_invitation(
    token: PortalToken, password: object, confirm: object
) -> tuple[PortalUser | None, list[str]]:
    """Set the first password from an invitation link and activate the account: the user,
    or the problems (the link is then not used)."""
    return _use_link(token, _text(password), _text(confirm), accept=True)


def reset_password(
    token: PortalToken, password: object, confirm: object
) -> tuple[PortalUser | None, list[str]]:
    """Set a new password from a reset link: the user, or the problems. Every session of
    the user then ends at its next request."""
    return _use_link(token, _text(password), _text(confirm), accept=False)


def _text(raw: object) -> str:
    return raw if isinstance(raw, str) else ""
