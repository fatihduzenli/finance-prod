"""The transfer wizard: a store handed from one operator to another
(docs/qa/PLAN-012e-lease-end-transfer-installments.md D6).

The wizard's state is one :class:`~ledger.models.LeaseTransfer` row per store in
progress (:func:`start`), with the outgoing lease's :class:`~ledger.models.LeaseEnd`
draft (reason ``transfer``, fixed); it can be left and resumed and is deleted on Cancel
with its lease end (:func:`cancel`). Its steps:

1. **Incoming operator** (:func:`set_operator`): an operator that is not archived and is
   not the outgoing one, picked by name (:func:`search_operators`) or created on the page
   (the operator's sync job queues as on the operator form).
2. **End of the outgoing lease**: the end-of-lease wizard's steps
   (:mod:`ledger.services.lease_end`): the end date, the statement and deductions, the
   settlement.
3. **Blockers** (:func:`blockers`): every lease-end blocker, plus every charge of the
   outgoing lease with money still owed once the lease ends (the statement's open
   lines: the last month as prorated, a month after the end waived; before the end date
   is set, the open balances as they stand). The end month's charge that does not exist
   yet is not listed: the statement step creates it (PLAN-012b-b D10). A deposit held is
   not a blocker. The wizard never calls the payment provider.
4. **Incoming lease** (:func:`set_incoming`): the start date (the day after the end date
   or later), the rent, the debit day(s), the channel, *Prorate first month*, the
   deposit amount and the setup items ticked from the catalogue.
5. **Confirm** (:func:`confirm`, one explicit "are you sure" for both effects): in one
   transaction under the store's and the transfer's row locks, the blockers and the
   dates are read again, the outgoing lease ends with every effect of its statement
   (:func:`ledger.services.lease_end.confirm_within`; its PDF and e-mail follow through
   the ``lease_end_documents`` job), the incoming lease is created
   (:func:`ledger.services.leases.create_lease`, status active), and **one** builder
   draft on the incoming operator and lease holds the deposit and the ticked setup items
   (:func:`ledger.services.invoice_builder.create_draft`): nothing reaches the payment
   provider, and no charge exists, until staff send the draft.

**A lease-end balance blocks by default** (D14, the owner's decision 3 of 2026-10-14).
When the statement would leave the outgoing operator owing money after the deposit
(:func:`debt_of`, the ``debt`` of :func:`ledger.services.statements.final`), the confirm
is refused unless an admin allowed it with a written reason (:func:`override_debt`: the
amount, the reason, who and when are kept on the transfer and audited). The balance may
not have grown since it was allowed. Confirmed, the "Lease-end balance" charge stays on
the outgoing operator and the attention list shows it until it is paid
(:mod:`ledger.services.attention`, ``lease_end_balance``).

**Invariant:** no day has two leases of the store in effect. The incoming lease starts
after the end date, and no other lease of the store may be active or paused on or after
its start; a refusal leaves everything as it was.

A refusal is one sentence and writes nothing. Every step writes one ``audit_log`` row
(entity ``lease_transfer``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from accounts.roles import Role, meets, role_of
from ledger.models import (
    ZERO,
    AuditLog,
    CollectionStatus,
    FeeType,
    Lease,
    LeaseEnd,
    LeaseEndReason,
    LeaseEndStatus,
    LeaseTransfer,
    LeaseTransferStatus,
    Operator,
    PaymentChannel,
    Store,
)
from ledger.services import (
    credits,
    deposits,
    invoice_builder,
    lease_end,
    leases,
    one_off_charges,
    receivables,
    rent_run,
    statements,
    workspace,
)
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from ledger.wording import format_amount

ENTITY = "lease_transfer"
#: Most operators the search lists.
SEARCH_LIMIT = 20
MEMO = "Move-in charges for store {label}"

# --- Blocker kinds -----------------------------------------------------------------------

OPEN_BALANCE = "open_balance"
#: PLAN-012d D6b: a credit held on the outgoing lease is the outgoing operator's money.
CREDIT_HELD = "credit_held"
CREDIT_REMAINS = (
    "A credit of {amount} remains on the outgoing operator: refund it in the lease end first."
)

# --- Fixed sentences -----------------------------------------------------------------------

NOT_CURRENT = lease_end.NOT_CURRENT
NO_OUTGOING_OPERATOR = "The lease has no operator to transfer from."
END_IN_PROGRESS = (
    "A lease end is in progress for this lease: finish or cancel it before a transfer."
)
NOT_DRAFT = "This transfer is already confirmed. Nothing was changed."
GONE = "The transfer no longer exists."
OPERATOR_REQUIRED = "Choose the operator who takes the store over."
OPERATOR_ARCHIVED = "That operator is archived; choose another. Nothing was changed."
OPERATOR_SAME = "The store already belongs to that operator; choose another."
OPERATOR_SAVED = "Incoming operator saved."
END_DATE_REQUIRED = "Set the end date of the outgoing lease first."
START_REQUIRED = "Enter the start date of the incoming lease."
START_TOO_EARLY = (
    "The incoming lease must start after the outgoing lease ends on {end}: on {first} or later."
)
RENT_INVALID = "Enter a monthly rent above zero, in cents."
DEBIT_DAY_REQUIRED = "Enter the debit day, 1 to 31."
SECOND_DAY_INVALID = "The second debit day must be 1 to 31 and differ from the first."
CHANNEL_REQUIRED = "Choose how the incoming operator pays."
DEPOSIT_INVALID = "The deposit must be zero or more, in cents, and at most {max}."
NO_DEPOSIT_TYPE = (
    "The catalogue has no active security deposit item: set the deposit to zero or "
    "make the item active."
)
SETUP_NOT_OFFERED = "{name} is not in the setup catalogue."
SETUP_TWICE = "{name} appears twice."
SETUP_PRICE = "{name}: enter a price above zero, in cents, at most {max}."
INCOMING_SAVED = "Incoming lease saved."
INCOMING_REQUIRED = "Set the incoming lease first."
CONFIRM_REQUIRED = "Check the box to confirm the transfer with these effects."
BLOCKED = "The store cannot be transferred yet: {count} item(s) are listed. Nothing was changed."
OTHER_LEASE = (
    "Another lease of this store is in effect on or after {start}; two leases cannot be in "
    "effect on one day. Nothing was changed."
)
CHANGED_MEANWHILE = "The store changed while confirming. Nothing was changed; try again."
CONFIRMED = (
    "The store is transferred. The outgoing statement is being prepared; review and send "
    "the move-in invoice draft."
)
CONFIRMED_NO_DRAFT = "The store is transferred. The outgoing statement is being prepared."
CANCELLED = "The transfer was cancelled. Nothing was changed."
DEBT_BLOCKS = (
    "The outgoing operator would still owe {amount} after the deposit. Collect it or change "
    "the deductions first; an admin may allow it with a written reason. Nothing was changed."
)
DEBT_GREW = (
    "The balance grew since it was allowed: {now} now, {allowed} allowed. Allow it again. "
    "Nothing was changed."
)
OVERRIDE_ADMIN = "Only an admin can allow a lease-end balance to remain."
REASON_REQUIRED = "A reason is required: say why the balance may remain."
REASON_PAYMENT_DATA = PAYMENT_DATA_MESSAGE
NOTHING_TO_ALLOW = "The statement leaves no balance; nothing to allow."
DEBT_ALLOWED = (
    "Balance of {amount} allowed to remain on the outgoing operator; the charge stays on the "
    "attention list until paid."
)


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    field: str | None = None
    transfer: LeaseTransfer | None = None


@dataclass(frozen=True, slots=True)
class SetupInput:
    """One ticked setup item: its catalogue fee type and the price charged."""

    fee_type: FeeType
    price: Decimal


@dataclass(frozen=True, slots=True)
class SetupLine:
    fee_type: FeeType
    price: Decimal


class _Abort(Exception):  # noqa: N818 - internal signal, rolls the step back
    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def _refused(message: str, field: str | None = None) -> Outcome:
    return Outcome(False, message, field)


def _audit(
    actor: User | None,
    action: str,
    entity_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )


def _state(transfer: LeaseTransfer) -> dict[str, Any]:
    def day(value: date | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "store": str(transfer.store_id),
        "outgoing_lease": str(transfer.outgoing_lease_id),
        "lease_end": str(transfer.lease_end_id) if transfer.lease_end_id else None,
        "status": transfer.status,
        "incoming_operator": (
            str(transfer.incoming_operator_id) if transfer.incoming_operator_id else None
        ),
        "incoming_started_on": day(transfer.incoming_started_on),
        "incoming_rent": str(transfer.incoming_rent) if transfer.incoming_rent else None,
        "incoming_debit_day": transfer.incoming_debit_day,
        "incoming_second_debit_day": transfer.incoming_second_debit_day,
        "incoming_channel": transfer.incoming_channel,
        "incoming_prorate_first_month": transfer.incoming_prorate_first_month,
        "deposit_amount": (
            str(transfer.deposit_amount) if transfer.deposit_amount is not None else None
        ),
        "setup_lines": list(transfer.setup_lines or []),
    }


def _override_state(transfer: LeaseTransfer) -> dict[str, Any]:
    """The allowed lease-end balance (D14), when one is set."""
    if transfer.debt_override_at is None:
        return {}
    amount = transfer.debt_override_amount
    return {
        "debt_override_amount": str(amount) if amount is not None else None,
        "debt_override_by": transfer.debt_override_by_id,
        "debt_override_at": transfer.debt_override_at.isoformat(),
    }


# --- Reads ---------------------------------------------------------------------------------


def in_progress(store: Store) -> LeaseTransfer | None:
    return (
        LeaseTransfer.objects.filter(store=store, status=LeaseTransferStatus.DRAFT)
        .select_related("outgoing_lease", "lease_end", "incoming_operator")
        .first()
    )


def search_operators(query: str, transfer: LeaseTransfer) -> list[Operator]:
    """Operators that are not archived whose display, legal or company name holds
    ``query`` (ignoring case), by name; the outgoing operator is left out."""
    text = (query or "").strip()
    if not text:
        return []
    rows = (
        Operator.objects.filter(archived_at__isnull=True)
        .filter(
            Q(display_name__icontains=text)
            | Q(legal_name__icontains=text)
            | Q(company__icontains=text)
        )
        .order_by("display_name", "id")
    )
    outgoing = transfer.outgoing_lease.operator_id
    if outgoing is not None:
        rows = rows.exclude(pk=outgoing)
    return list(rows[:SEARCH_LIMIT])


def deposit_type() -> FeeType | None:
    """The catalogue's security deposit item when it is offered (active; the wizard's
    deposit is its own field, whatever the checklist flag, PLAN-012d D9)."""
    return next(
        (
            fee_type
            for fee_type in one_off_charges.offered_types()
            if fee_type.code == deposits.DEPOSIT_FEE_CODE
        ),
        None,
    )


def setup_types() -> list[FeeType]:
    """The setup checklist of the incoming lease: the offered catalogue items that are not
    deposits (the deposit has its own amount), in catalogue order."""
    return [fee_type for fee_type in one_off_charges.checklist_types() if not fee_type.refundable]


def default_deposit(transfer: LeaseTransfer) -> Decimal | None:
    """The catalogue's deposit price, else the outgoing lease's deposit price."""
    found = deposit_type()
    if found is not None and found.default_price is not None:
        return Decimal(found.default_price)
    outgoing = (
        transfer.outgoing_lease.one_off_charges.filter(fee_type__code=deposits.DEPOSIT_FEE_CODE)
        .order_by("due_date", "created_at")
        .first()
    )
    return Decimal(outgoing.price) if outgoing is not None else None


def setup_lines(transfer: LeaseTransfer) -> list[SetupLine]:
    """The saved setup items with their fee types, in the saved order; an item no longer
    in the catalogue is left out (confirm refuses it again through the builder)."""
    stored = list(transfer.setup_lines or [])
    ids = [str(item.get("fee_type")) for item in stored if isinstance(item, dict)]
    found = {str(row.pk): row for row in FeeType.objects.filter(pk__in=ids)}
    lines = []
    for item in stored:
        fee_type = found.get(str(item.get("fee_type"))) if isinstance(item, dict) else None
        if fee_type is not None:
            lines.append(SetupLine(fee_type, Decimal(str(item.get("price")))))
    return lines


def operator_set(transfer: LeaseTransfer) -> bool:
    return transfer.incoming_operator_id is not None


def incoming_set(transfer: LeaseTransfer) -> bool:
    return transfer.incoming_started_on is not None and transfer.incoming_rent is not None


def _end(transfer: LeaseTransfer) -> LeaseEnd:
    end = transfer.lease_end
    if end is None:
        raise _Abort(GONE)
    return end


def _balance_url(kind: str, charge_id: uuid.UUID | None, lease: Lease) -> str:
    if kind == receivables.KIND_RENT and charge_id is not None:
        return reverse("ledger:charges:detail", kwargs={"pk": charge_id})
    if kind == receivables.KIND_ONE_OFF and charge_id is not None:
        return reverse("ledger:one_off_charges:detail", kwargs={"pk": charge_id})
    return reverse("ledger:leases:detail", kwargs={"pk": lease.pk})


def balance_blockers(transfer: LeaseTransfer, today: date | None = None) -> list[lease_end.Blocker]:
    """Every charge of the outgoing lease with money still owed once it ends, one each
    with its page (D6(b)). Deposits are never listed (they are held, not owed; an unpaid
    one is a lease-end blocker). A charge the end month still needs is not listed: the
    statement step creates it (PLAN-012b-b D10). Once the terms are set, a balance is
    listed as the statement leaves it: the unpaid-balance offset pays the open lines
    oldest due first, as the confirm pays them (PLAN-012b-b D10, D12). A credit held on
    the outgoing lease blocks too (PLAN-012d D6b): it is the outgoing operator's money."""
    day = today or timezone.localdate()
    lease = transfer.outgoing_lease
    end = transfer.lease_end
    if end is not None and lease_end.terms_set(end):
        figures = statements.final(end, today=day)
        offset = figures.offset_applied
        lines = []
        for line in figures.open_lines:
            covered = min(offset, line.balance)
            offset -= covered
            lines.append((line.kind, line.charge_id, line.description, line.balance - covered))
    else:
        lines = [
            (row.kind, row.charge_id, row.description, row.balance)
            for row in receivables.open_balances(day, operator=lease.operator_id)
            if row.lease_id == lease.pk and row.balance > 0
        ]
    found = []
    for kind, charge_id, description, balance in lines:
        if balance <= 0:
            continue
        if charge_id is None:
            # PLAN-012b-b D10: the end month's charge not created yet is created when the
            # statement step opens (lease_end.open_statement); it blocks once it exists.
            continue
        text = (
            f"{description}: {format_amount(balance)} is still owed by the outgoing "
            "operator. It must be paid before the store is transferred."
        )
        found.append(lease_end.Blocker(OPEN_BALANCE, text, _balance_url(kind, charge_id, lease)))
    held = credits.balance(lease)
    if held > 0:
        found.append(
            lease_end.Blocker(
                CREDIT_HELD,
                CREDIT_REMAINS.format(amount=format_amount(held)),
                workspace.tab_url(lease.store_id, workspace.TAB_PAYMENTS),
            )
        )
    return found


def blockers(transfer: LeaseTransfer, today: date | None = None) -> list[lease_end.Blocker]:
    """The lease-end blockers of the outgoing lease and its open balances (D6(b))."""
    day = today or timezone.localdate()
    return lease_end.blockers(transfer.outgoing_lease, day) + balance_blockers(transfer, day)


def debt_of(transfer: LeaseTransfer, today: date | None = None) -> Decimal:
    """The lease-end balance the statement would charge the outgoing operator now (the
    ``debt`` of :func:`ledger.services.statements.final`, as the confirm charges it);
    zero while the end's terms are not set (D14)."""
    end = transfer.lease_end
    if end is None or not lease_end.terms_set(end):
        return ZERO
    return statements.final(end, today=today or timezone.localdate()).debt


def incoming_lines(transfer: LeaseTransfer) -> list[invoice_builder.LineInput]:
    """The move-in draft's lines: the deposit (when above zero) and each setup item."""
    lines = []
    deposit = deposit_type()
    if transfer.deposit_amount and deposit is not None:
        lines.append(invoice_builder.LineInput(deposit.name, "", None, transfer.deposit_amount))
    for line in setup_lines(transfer):
        lines.append(invoice_builder.LineInput(line.fee_type.name, "", None, line.price))
    return lines


# --- Steps ---------------------------------------------------------------------------------


def start(store: Store, actor: User | None, today: date | None = None) -> LeaseTransfer:
    """The store's transfer in progress, made now with the current lease's end draft
    (reason ``transfer``) or found. A plain draft of the lease that never reached the
    statement step is adopted (:func:`ledger.services.lease_end.adopt_for_transfer`,
    PLAN-012b-b D9). ``ValueError`` with one sentence when the store has no lease in
    effect, the lease has no operator, or a plain lease end is in progress (its
    statement step opened)."""
    day = today or timezone.localdate()
    with transaction.atomic():
        Store.objects.select_for_update().get(pk=store.pk)
        found = in_progress(store)
        if found is not None:
            return found
        lease = lease_end.current_lease(store, day)
        if lease is None:
            raise ValueError(NOT_CURRENT)
        if lease.operator_id is None:
            raise ValueError(NO_OUTGOING_OPERATOR)
        existing = LeaseEnd.objects.filter(lease=lease).first()
        if existing is not None and existing.status == LeaseEndStatus.DRAFT:
            if lease_end.in_progress(existing):
                raise ValueError(END_IN_PROGRESS)
            end = lease_end.adopt_for_transfer(existing, actor)
        else:
            end = lease_end.start(lease, actor, reason=LeaseEndReason.TRANSFER)
        transfer = LeaseTransfer.objects.create(
            store=store, outgoing_lease=lease, lease_end=end, created_by=actor
        )
        _audit(actor, "lease_transfer.started", transfer.pk, None, _state(transfer))
    return transfer


def _locked_draft(transfer_id: uuid.UUID) -> LeaseTransfer:
    transfer = (
        LeaseTransfer.objects.select_for_update(of=("self",))
        .select_related("store", "outgoing_lease", "lease_end", "incoming_operator")
        .get(pk=transfer_id)
    )
    if transfer.status != LeaseTransferStatus.DRAFT:
        raise _Abort(NOT_DRAFT)
    return transfer


def _operator_refusal(operator: Operator | None, transfer: LeaseTransfer) -> str:
    if operator is None:
        return OPERATOR_REQUIRED
    if operator.archived_at is not None:
        return OPERATOR_ARCHIVED
    if operator.pk == transfer.outgoing_lease.operator_id:
        return OPERATOR_SAME
    return ""


def set_operator(
    transfer: LeaseTransfer, operator: Operator | None, *, actor: User | None
) -> Outcome:
    """Step 1: the operator who takes the store over."""
    try:
        with transaction.atomic():
            row = _locked_draft(transfer.pk)
            current = (
                Operator.objects.select_for_update().get(pk=operator.pk)
                if operator is not None
                else None
            )
            refusal = _operator_refusal(current, row)
            if refusal:
                raise _Abort(refusal, "operator")
            before = _state(row)
            row.incoming_operator = current
            row.save(update_fields=["incoming_operator", "updated_at"])
            _audit(actor, "lease_transfer.operator_set", row.pk, before, _state(row))
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    except LeaseTransfer.DoesNotExist:
        return _refused(GONE)
    return Outcome(True, OPERATOR_SAVED, transfer=row)


def _money(value: Decimal | None, maximum: Decimal) -> bool:
    return (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.quantize(rent_run.CENT)
        and Decimal("0") <= value <= maximum
    )


def _start_refusal(started_on: date | None, ended_on: date) -> str:
    if started_on is None:
        return START_REQUIRED
    first = ended_on + timedelta(days=1)
    if started_on < first:
        return START_TOO_EARLY.format(end=ended_on.isoformat(), first=first.isoformat())
    return ""


def _clean_setup(items: Sequence[SetupInput]) -> list[dict[str, str]]:
    offered = {fee_type.pk for fee_type in setup_types()}
    seen: set[uuid.UUID] = set()
    lines = []
    for item in items:
        name = item.fee_type.name
        if item.fee_type.pk not in offered:
            raise _Abort(SETUP_NOT_OFFERED.format(name=name), "setup")
        if item.fee_type.pk in seen:
            raise _Abort(SETUP_TWICE.format(name=name), "setup")
        seen.add(item.fee_type.pk)
        if not _money(item.price, one_off_charges.AMOUNT_MAX) or item.price <= 0:
            raise _Abort(
                SETUP_PRICE.format(name=name, max=format_amount(one_off_charges.AMOUNT_MAX)),
                "setup",
            )
        lines.append({"fee_type": str(item.fee_type.pk), "price": str(item.price)})
    return lines


def set_incoming(
    transfer: LeaseTransfer,
    *,
    started_on: date | None,
    rent: Decimal | None,
    debit_day: int | None,
    second_debit_day: int | None,
    channel: str,
    prorate_first_month: bool,
    deposit_amount: Decimal | None,
    setup: Sequence[SetupInput],
    actor: User | None,
) -> Outcome:
    """Step 4: the incoming lease's terms, its deposit and its setup items. The start is
    the day after the outgoing lease's end date or later."""
    try:
        with transaction.atomic():
            row = _locked_draft(transfer.pk)
            end = _end(row)
            if end.ended_on is None:
                raise _Abort(END_DATE_REQUIRED)
            refusal = _start_refusal(started_on, end.ended_on)
            if refusal:
                raise _Abort(refusal, "started_on")
            if rent is None or not _money(rent, rent_run.MONEY_MAX) or rent <= 0:
                raise _Abort(RENT_INVALID, "rent")
            if debit_day is None or not 1 <= debit_day <= 31:
                raise _Abort(DEBIT_DAY_REQUIRED, "debit_day")
            if second_debit_day is not None and (
                not 1 <= second_debit_day <= 31 or second_debit_day == debit_day
            ):
                raise _Abort(SECOND_DAY_INVALID, "second_debit_day")
            if channel not in PaymentChannel.values or channel == PaymentChannel.UNKNOWN:
                raise _Abort(CHANNEL_REQUIRED, "channel")
            if deposit_amount is not None and not _money(
                deposit_amount, one_off_charges.AMOUNT_MAX
            ):
                raise _Abort(
                    DEPOSIT_INVALID.format(max=format_amount(one_off_charges.AMOUNT_MAX)),
                    "deposit_amount",
                )
            if deposit_amount and deposit_type() is None:
                raise _Abort(NO_DEPOSIT_TYPE, "deposit_amount")
            lines = _clean_setup(setup)
            before = _state(row)
            row.incoming_started_on = started_on
            row.incoming_rent = rent
            row.incoming_debit_day = debit_day
            row.incoming_second_debit_day = second_debit_day
            row.incoming_channel = channel
            row.incoming_prorate_first_month = prorate_first_month is True
            row.deposit_amount = deposit_amount
            row.setup_lines = lines
            row.save(
                update_fields=[
                    "incoming_started_on",
                    "incoming_rent",
                    "incoming_debit_day",
                    "incoming_second_debit_day",
                    "incoming_channel",
                    "incoming_prorate_first_month",
                    "deposit_amount",
                    "setup_lines",
                    "updated_at",
                ]
            )
            _audit(actor, "lease_transfer.incoming_set", row.pk, before, _state(row))
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    except LeaseTransfer.DoesNotExist:
        return _refused(GONE)
    return Outcome(True, INCOMING_SAVED, transfer=row)


def cancel(transfer: LeaseTransfer, actor: User | None) -> Outcome:
    """Delete the transfer and its lease end draft; nothing else is changed."""
    try:
        with transaction.atomic():
            row = _locked_draft(transfer.pk)
            end = row.lease_end
            state = _state(row)
            pk = row.pk
            row.delete()
            _audit(actor, "lease_transfer.cancelled", pk, state, None)
            if end is not None:
                outcome = lease_end.cancel(end, actor)
                if not outcome.ok:
                    raise _Abort(outcome.message)
    except _Abort as abort:
        return _refused(abort.message)
    except LeaseTransfer.DoesNotExist:
        return _refused(GONE)
    return Outcome(True, CANCELLED)


def override_debt(
    transfer: LeaseTransfer, reason: str, actor: User | None, today: date | None = None
) -> Outcome:
    """Allow the current lease-end balance to remain on the outgoing operator (admin, D14):
    the amount, the written reason, who and when are kept on the transfer and audited.
    Allowing again replaces the earlier allowance."""
    if actor is None or not meets(role_of(actor), Role.ADMIN):
        return _refused(OVERRIDE_ADMIN)
    day = today or timezone.localdate()
    try:
        with transaction.atomic():
            row = _locked_draft(transfer.pk)
            text = visible_text(reason or "")
            if not text:
                raise _Abort(REASON_REQUIRED, "reason")
            try:
                validate_no_payment_data(text)
            except ValidationError:
                raise _Abort(REASON_PAYMENT_DATA, "reason") from None
            debt = debt_of(row, day)
            if debt <= 0:
                raise _Abort(NOTHING_TO_ALLOW)
            before = {**_state(row), **_override_state(row)}
            if row.debt_override_at is not None:
                before["debt_override_reason"] = row.debt_override_reason
            row.debt_override_amount = debt
            row.debt_override_reason = text
            row.debt_override_by = actor
            row.debt_override_at = timezone.now()
            row.save(
                update_fields=[
                    "debt_override_amount",
                    "debt_override_reason",
                    "debt_override_by",
                    "debt_override_at",
                    "updated_at",
                ]
            )
            _audit(
                actor,
                "lease_transfer.debt_overridden",
                row.pk,
                before,
                {**_state(row), **_override_state(row), "debt_override_reason": text},
            )
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    except LeaseTransfer.DoesNotExist:
        return _refused(GONE)
    return Outcome(True, DEBT_ALLOWED.format(amount=format_amount(debt)), transfer=row)


# --- Confirm -------------------------------------------------------------------------------


def _other_lease(store: Store, outgoing: Lease, start: date) -> bool:
    """Whether a lease of the store other than the outgoing one is active or paused on
    or after ``start``."""
    return (
        Lease.objects.filter(
            store=store,
            collection_status__in=[CollectionStatus.ACTIVE, CollectionStatus.PAUSED],
        )
        .exclude(pk=outgoing.pk)
        .filter(Q(ended_on__isnull=True) | Q(ended_on__gte=start))
        .exists()
    )


def _incoming_lease(row: LeaseTransfer, rent: Decimal, actor: User | None, day: date) -> Lease:
    lease, created = leases.create_lease(
        {
            "store": row.store,
            "operator": row.incoming_operator,
            "debit_day": row.incoming_debit_day,
            "second_debit_day": row.incoming_second_debit_day,
            "payment_channel": row.incoming_channel,
            "started_on": row.incoming_started_on,
            "collection_status": CollectionStatus.ACTIVE,
            "prorate_first_month": row.incoming_prorate_first_month,
        },
        rent=rent,
        creation_key=row.pk,
        actor=actor,
        today=day,
    )
    if not created:
        raise _Abort(CHANGED_MEANWHILE)
    return lease


def confirm(
    transfer: LeaseTransfer, actor: User | None, *, confirmed: bool, today: date | None = None
) -> Outcome:
    """Step 5: end the outgoing lease and start the incoming one, in one transaction (see
    the module docstring). Refused, with nothing written, unless ``confirmed`` is exactly
    ``True``, the operator and the incoming lease are set, nothing blocks and the dates
    keep one lease in effect per day."""
    if confirmed is not True:
        return _refused(CONFIRM_REQUIRED, "confirmed")
    day = today or timezone.localdate()
    try:
        with transaction.atomic():
            Store.objects.select_for_update().get(pk=transfer.store_id)
            row = _locked_draft(transfer.pk)
            end = _end(row)
            refusal = _operator_refusal(row.incoming_operator, row)
            if refusal:
                raise _Abort(refusal)
            if not lease_end.terms_set(end) or end.ended_on is None:
                raise _Abort(lease_end.TERMS_REQUIRED)
            start = row.incoming_started_on
            rent = row.incoming_rent
            operator = row.incoming_operator
            if start is None or rent is None or operator is None:
                raise _Abort(INCOMING_REQUIRED)
            refusal = _start_refusal(start, end.ended_on)
            if refusal:
                raise _Abort(refusal)
            found = blockers(row, day)
            if found:
                raise _Abort(BLOCKED.format(count=len(found)))
            debt = debt_of(row, day)
            allowed = row.debt_override_amount
            if debt > 0 and (row.debt_override_at is None or allowed is None):
                raise _Abort(DEBT_BLOCKS.format(amount=format_amount(debt)))
            if allowed is not None and debt > allowed:
                raise _Abort(
                    DEBT_GREW.format(now=format_amount(debt), allowed=format_amount(allowed))
                )
            if _other_lease(row.store, row.outgoing_lease, start):
                raise _Abort(OTHER_LEASE.format(start=start.isoformat()))
            if row.deposit_amount and deposit_type() is None:
                raise _Abort(NO_DEPOSIT_TYPE)
            ended = lease_end.confirm_within(end, actor, day, transfer=row)
            incoming = _incoming_lease(row, rent, actor, day)
            lines = incoming_lines(row)
            draft = None
            if lines:
                made = invoice_builder.create_draft(
                    operator,
                    incoming,
                    MEMO.format(label=row.store.external_label),
                    None,
                    lines,
                    actor,
                    today=day,
                    creation_key=row.pk,
                )
                if not made.ok or made.draft is None:
                    raise _Abort(made.message)
                draft = made.draft
            before = _state(row)
            row.status = LeaseTransferStatus.CONFIRMED
            row.confirmed_at = timezone.now()
            row.incoming_lease = incoming
            row.draft = draft
            row.save(
                update_fields=["status", "confirmed_at", "incoming_lease", "draft", "updated_at"]
            )
            _audit(
                actor,
                "lease_transfer.confirmed",
                row.pk,
                before,
                {
                    **_state(row),
                    "ended_on": ended.ended_on.isoformat() if ended.ended_on else None,
                    "incoming_lease": str(incoming.pk),
                    "draft": str(draft.pk) if draft is not None else None,
                    "debt": str(debt),
                    **_override_state(row),
                },
            )
    except _Abort as abort:
        return _refused(abort.message, abort.field)
    except lease_end.Refused as refusal:
        return _refused(refusal.message, refusal.field)
    except (LeaseTransfer.DoesNotExist, Store.DoesNotExist):
        return _refused(GONE)
    except IntegrityError:
        return _refused(CHANGED_MEANWHILE)
    return Outcome(True, CONFIRMED if draft is not None else CONFIRMED_NO_DRAFT, transfer=row)
