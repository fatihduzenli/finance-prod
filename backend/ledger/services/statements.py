"""The final statement of a lease end (docs/qa/PLAN-012e-lease-end-transfer-installments.md
D3, D5(c), D5(d), D7).

:func:`final` reads, without writing anything, what confirming the lease end would do and
what it would leave: the last month's rent (D3), the lease's open balances after it, the
deposits held, the typed deductions, and the settlement figures. The confirm step of
:mod:`ledger.services.lease_end` computes it again under its row locks and applies it;
the figures are kept as ``lease_ends.statement_json`` (:meth:`Final.to_json`), from which
the PDF (:func:`render_pdf`) and the confirmed page are drawn.

**The last month** (D3), the month of the end date, at the rent in effect on that date:

- with *Prorate last month* on, the month is worth :func:`ledger.services.proration.prorated`
  from the 1st (or the start date, when the lease started that month) to the end date.
  Existing rent charges of the month (not waived) are reduced to that amount, the last
  debit first. A charge paid beyond its new amount gives the excess back: a **credit**,
  refunded with the settlement as a refund of a payment on that charge, so the charge
  ends paid at its new amount. Without a charge for the month, one is created for the
  prorated amount (manual collection, nothing at the payment provider);
- with it off, the month's charges stand as generated; without any, each debit whose
  day falls on or before the end date is created for its full share.

**Months after the end** (D3 amendment): a rent charge (not waived) whose month is after
the month of the end date is waived at confirm (note "After lease end (wizard)"). One
with nothing paid is listed as "Waived: <month> rent, after the end date"; one paid or
partly paid is a **credit**: each of its settled payments is refunded with the settlement
for what is not yet refunded, as the last month's credit is.

**Open balances** are :func:`ledger.services.receivables.open_balances` of the lease
(deposits are never open balances), with the last month as it will be and without the
months after the end.

**Settlement** (D5(c), D5(d)): the deductions are covered by the deposits held, the
unpaid-balance offset first (it is applied to the open charges, oldest first, as "deposit
offset" payments), then the other deductions. What the deposits do not cover of the
other deductions is the **lease-end balance**, a new charge; what they do not cover of
the offset stays open on its charges. **Refund due** = deposit held − deductions covered
+ credit (the last month's and the months after the end); **amount owed** = lease-end
balance + open balance not offset.

Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from ledger.models import (
    ZERO,
    DeductionKind,
    LeaseEnd,
    LeaseEndReason,
    OneOffCharge,
    Payment,
    PaymentStatus,
    RefundMethod,
    RentCharge,
    RentChargeStatus,
)
from ledger.services import one_off_charges, proration, receivables
from ledger.services.leases import debit_amounts, rent_on
from ledger.services.rent_run import falls_on

#: What a new charge of the last month is named on the statement.
NEW_CHARGE = "new charge"
OFFSET_REFERENCE = "deposit offset"


# --- Figures ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RentChange:
    """What D3 does to one rent charge of the last month; ``charge_id`` ``None`` is a
    charge to create."""

    charge_id: uuid.UUID | None
    seq: int
    due_on: date
    before: Decimal
    after: Decimal
    paid: Decimal
    #: The credit given back on this charge (paid beyond its new amount).
    refund: Decimal
    #: The payment the credit is a refund of.
    parent_payment_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class LastMonth:
    month: date
    rent: Decimal
    amount: Decimal
    text: str
    prorate: bool
    changes: tuple[RentChange, ...]
    credit: Decimal


@dataclass(frozen=True, slots=True)
class CreditRefund:
    """Part of a credit given back as a refund of one settled payment."""

    parent_payment_id: uuid.UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AfterEnd:
    """A rent charge of a month after the end month, waived at confirm (D3 amendment);
    what was paid on it is refunded (``refunds``)."""

    charge_id: uuid.UUID
    month: date
    seq: int
    amount_due: Decimal
    paid: Decimal
    status: str
    refunds: tuple[CreditRefund, ...]

    @property
    def credit(self) -> Decimal:
        return sum((refund.amount for refund in self.refunds), ZERO)

    @property
    def label(self) -> str:
        debit = f" (debit {self.seq})" if self.seq > 1 else ""
        return f"{self.month:%B %Y} rent{debit}"

    @property
    def text(self) -> str:
        """The statement's line: "Waived: <month> rent, after the end date", or the
        credit of one paid."""
        if self.credit > 0:
            return f"Credit: {self.label} paid, after the end date"
        return f"Waived: {self.label}, after the end date"


@dataclass(frozen=True, slots=True)
class OpenLine:
    kind: str
    charge_id: uuid.UUID | None
    description: str
    due_on: date
    balance: Decimal


@dataclass(frozen=True, slots=True)
class DepositLine:
    charge_id: uuid.UUID
    label: str
    held: Decimal


@dataclass(frozen=True, slots=True)
class DeductionLine:
    kind: str
    label: str
    description: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Final:
    company: str
    operator: str
    store: str
    started_on: date | None
    ended_on: date
    reason: str
    last_month: LastMonth | None
    #: D3 amendment: the rent charges of the months after the end month.
    after_end: tuple[AfterEnd, ...]
    open_lines: tuple[OpenLine, ...]
    open_total: Decimal
    deposits: tuple[DepositLine, ...]
    deposit_held: Decimal
    deductions: tuple[DeductionLine, ...]
    offset_total: Decimal
    other_total: Decimal
    #: The last month's credit and the months after the end's.
    credit: Decimal
    offset_applied: Decimal
    withheld: Decimal
    deposit_refund: Decimal
    refund_due: Decimal
    #: The lease-end balance: other deductions the deposits do not cover (a new charge).
    debt: Decimal
    still_open: Decimal
    amount_owed: Decimal
    refund_method: str
    refund_on: date | None
    refund_reference: str
    prepared_on: date

    @property
    def deductions_total(self) -> Decimal:
        return self.offset_total + self.other_total

    @property
    def refund_method_label(self) -> str:
        return str(RefundMethod(self.refund_method).label) if self.refund_method else ""

    def to_json(self) -> dict[str, Any]:
        encoded: dict[str, Any] = _encode(self)
        return encoded


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {name: _encode(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _money(raw: object) -> Decimal:
    return Decimal(str(raw))


def _day(raw: object) -> date | None:
    return date.fromisoformat(str(raw)) if raw else None


def _key(raw: object) -> uuid.UUID | None:
    return uuid.UUID(str(raw)) if raw else None


def from_json(data: Mapping[str, Any]) -> Final:
    """The figures kept in ``statement_json``, as :class:`Final`."""
    last = data.get("last_month")
    last_month = (
        LastMonth(
            month=date.fromisoformat(last["month"]),
            rent=_money(last["rent"]),
            amount=_money(last["amount"]),
            text=str(last["text"]),
            prorate=bool(last["prorate"]),
            changes=tuple(
                RentChange(
                    charge_id=_key(item["charge_id"]),
                    seq=int(item["seq"]),
                    due_on=date.fromisoformat(item["due_on"]),
                    before=_money(item["before"]),
                    after=_money(item["after"]),
                    paid=_money(item["paid"]),
                    refund=_money(item["refund"]),
                    parent_payment_id=_key(item["parent_payment_id"]),
                )
                for item in last["changes"]
            ),
            credit=_money(last["credit"]),
        )
        if last
        else None
    )
    after_end = tuple(
        AfterEnd(
            charge_id=uuid.UUID(str(item["charge_id"])),
            month=date.fromisoformat(item["month"]),
            seq=int(item["seq"]),
            amount_due=_money(item["amount_due"]),
            paid=_money(item["paid"]),
            status=str(item["status"]),
            refunds=tuple(
                CreditRefund(uuid.UUID(str(refund["parent_payment_id"])), _money(refund["amount"]))
                for refund in item["refunds"]
            ),
        )
        for item in data.get("after_end", ())
    )
    ended = _day(data["ended_on"])
    prepared = _day(data["prepared_on"])
    if ended is None or prepared is None:
        raise ValueError("The statement has no end date.")
    return Final(
        company=str(data["company"]),
        operator=str(data["operator"]),
        store=str(data["store"]),
        started_on=_day(data["started_on"]),
        ended_on=ended,
        reason=str(data["reason"]),
        last_month=last_month,
        after_end=after_end,
        open_lines=tuple(
            OpenLine(
                kind=str(item["kind"]),
                charge_id=_key(item["charge_id"]),
                description=str(item["description"]),
                due_on=date.fromisoformat(item["due_on"]),
                balance=_money(item["balance"]),
            )
            for item in data["open_lines"]
        ),
        open_total=_money(data["open_total"]),
        deposits=tuple(
            DepositLine(
                charge_id=uuid.UUID(str(item["charge_id"])),
                label=str(item["label"]),
                held=_money(item["held"]),
            )
            for item in data["deposits"]
        ),
        deposit_held=_money(data["deposit_held"]),
        deductions=tuple(
            DeductionLine(
                kind=str(item["kind"]),
                label=str(item["label"]),
                description=str(item["description"]),
                amount=_money(item["amount"]),
            )
            for item in data["deductions"]
        ),
        offset_total=_money(data["offset_total"]),
        other_total=_money(data["other_total"]),
        credit=_money(data["credit"]),
        offset_applied=_money(data["offset_applied"]),
        withheld=_money(data["withheld"]),
        deposit_refund=_money(data["deposit_refund"]),
        refund_due=_money(data["refund_due"]),
        debt=_money(data["debt"]),
        still_open=_money(data["still_open"]),
        amount_owed=_money(data["amount_owed"]),
        refund_method=str(data["refund_method"]),
        refund_on=_day(data["refund_on"]),
        refund_reference=str(data["refund_reference"]),
        prepared_on=prepared,
    )


# --- The last month (D3) -------------------------------------------------------------------


def _refund_parent(charge: RentCharge, amount: Decimal) -> uuid.UUID | None:
    """The newest settled payment on ``charge`` with at least ``amount`` not yet
    refunded, which a credit of ``amount`` is a refund of."""
    payments = Payment.objects.filter(
        rent_charge=charge, status=PaymentStatus.SETTLED, is_refund=False
    ).order_by("-settled_on", "-created_at", "-id")
    for payment in payments:
        refunded = (
            Payment.objects.filter(
                parent_payment=payment, is_refund=True, status=PaymentStatus.SETTLED
            ).aggregate(total=Sum("amount"))["total"]
            or ZERO
        )
        if payment.amount - Decimal(refunded) >= amount:
            return payment.pk
    return None


def _due_on(debit_day: int | None, month: date, ended: date) -> date:
    """A new last-month charge is due on its debit day, or on the end date when that
    day falls after it (or the lease has no debit day)."""
    if debit_day is None:
        return ended
    day = month.replace(day=falls_on(debit_day, month.year, month.month))
    return min(day, ended)


def last_month(lease_end: LeaseEnd) -> LastMonth | None:
    """What D3 does to the month of the end date; ``None`` when no rent is in effect on
    the end date."""
    lease = lease_end.lease
    ended = lease_end.ended_on
    if ended is None:
        return None
    rent = rent_on(lease, ended)
    if rent is None:
        return None
    month = ended.replace(day=1)
    start = lease.started_on
    first = start.day if start is not None and start.replace(day=1) == month else 1
    prorate = lease_end.prorate_last_month
    if prorate:
        part = proration.prorated(rent.amount, month, first, ended.day)
    else:
        total_days = proration.days_in(month)
        part = proration.Prorated(total_days, total_days, rent.amount, "")
    charges = list(
        RentCharge.objects.filter(lease=lease, period_month=month)
        .exclude(status=RentChargeStatus.WAIVED)
        .order_by("seq")
    )
    changes: list[RentChange] = []
    credit = ZERO
    debit_days = [day for day in (lease.debit_day, lease.second_debit_day) if day is not None]
    if charges and prorate:
        billed = sum((charge.amount_due for charge in charges), ZERO)
        if part.amount < billed:
            to_cut = billed - part.amount
            for charge in reversed(charges):
                if to_cut <= 0:
                    break
                after = charge.amount_due - min(to_cut, charge.amount_due)
                refund = max(charge.amount_paid - after, ZERO)
                parent = _refund_parent(charge, refund) if refund > 0 else None
                if refund > 0 and parent is None:
                    # Nothing to give the excess back through: the charge keeps it.
                    after, refund = charge.amount_paid, ZERO
                if after == charge.amount_due:
                    continue
                to_cut -= charge.amount_due - after
                credit += refund
                changes.append(
                    RentChange(
                        charge.pk,
                        charge.seq,
                        charge.due_on,
                        charge.amount_due,
                        after,
                        charge.amount_paid,
                        refund,
                        parent,
                    )
                )
        elif part.amount > billed:
            taken = {charge.seq for charge in charges}
            free = [seq for seq in (1, 2) if seq not in taken]
            if free:
                day = debit_days[free[0] - 1] if len(debit_days) >= free[0] else None
                changes.append(
                    RentChange(
                        None,
                        free[0],
                        _due_on(day, month, ended),
                        ZERO,
                        part.amount - billed,
                        ZERO,
                        ZERO,
                        None,
                    )
                )
    elif not charges and prorate:
        day = debit_days[0] if debit_days else None
        changes.append(
            RentChange(None, 1, _due_on(day, month, ended), ZERO, part.amount, ZERO, ZERO, None)
        )
    elif not charges:
        amounts = debit_amounts(rent.amount, twice_monthly=len(debit_days) == 2)
        for seq, debit_day in enumerate(debit_days, start=1):
            on = month.replace(day=falls_on(debit_day, month.year, month.month))
            if on <= ended and (start is None or on >= start):
                changes.append(RentChange(None, seq, on, ZERO, amounts[seq - 1], ZERO, ZERO, None))
    return LastMonth(
        month=month,
        rent=rent.amount,
        amount=part.amount,
        text=part.text,
        prorate=prorate,
        changes=tuple(changes),
        credit=credit,
    )


# --- The months after the end (D3 amendment) ------------------------------------------------


def _credit_refunds(charge: RentCharge) -> tuple[CreditRefund, ...]:
    """A refund of each settled payment on ``charge`` for what is not yet refunded of
    it, oldest first."""
    refunds: list[CreditRefund] = []
    payments = Payment.objects.filter(
        rent_charge=charge, status=PaymentStatus.SETTLED, is_refund=False
    ).order_by("settled_on", "created_at", "id")
    for payment in payments:
        refunded = (
            Payment.objects.filter(
                parent_payment=payment, is_refund=True, status=PaymentStatus.SETTLED
            ).aggregate(total=Sum("amount"))["total"]
            or ZERO
        )
        left = payment.amount - Decimal(refunded)
        if left > 0:
            refunds.append(CreditRefund(payment.pk, left))
    return tuple(refunds)


def after_end(lease_end: LeaseEnd) -> tuple[AfterEnd, ...]:
    """The lease's rent charges (not waived) of the months after the month of the end
    date, by month and debit."""
    ended = lease_end.ended_on
    if ended is None:
        return ()
    charges = (
        RentCharge.objects.filter(lease=lease_end.lease, period_month__gt=ended.replace(day=1))
        .exclude(status=RentChargeStatus.WAIVED)
        .order_by("period_month", "seq")
    )
    return tuple(
        AfterEnd(
            charge_id=charge.pk,
            month=charge.period_month,
            seq=charge.seq,
            amount_due=charge.amount_due,
            paid=charge.amount_paid,
            status=charge.status,
            refunds=_credit_refunds(charge) if charge.amount_paid > 0 else (),
        )
        for charge in charges
    )


# --- Open balances, deposits ---------------------------------------------------------------


def _open_lines(
    lease_end: LeaseEnd, today: date, last: LastMonth | None, later: Iterable[AfterEnd] = ()
) -> list[OpenLine]:
    lease = lease_end.lease
    waived = {item.charge_id for item in later}
    rows = [
        row
        for row in receivables.open_balances(today, operator=lease.operator_id)
        if row.lease_id == lease.pk
        and not (row.kind == receivables.KIND_RENT and row.charge_id in waived)
    ]
    changed = {
        change.charge_id: change
        for change in (last.changes if last is not None else ())
        if change.charge_id is not None
    }
    lines: list[OpenLine] = []
    for row in rows:
        balance = row.balance
        change = changed.get(row.charge_id) if row.kind == receivables.KIND_RENT else None
        if change is not None:
            balance = change.after - (change.paid - change.refund)
        if balance <= 0:
            continue
        lines.append(OpenLine(row.kind, row.charge_id, row.description, row.due_on, balance))
    if last is not None:
        for change in last.changes:
            if change.charge_id is None:
                description = f"Rent {last.month:%Y-%m}" + (
                    f" (debit {change.seq})" if change.seq > 1 else ""
                )
                if last.text:
                    description += f": {last.text}"
                lines.append(
                    OpenLine(receivables.KIND_RENT, None, description, change.due_on, change.after)
                )
    return sorted(lines, key=lambda line: (line.due_on, line.kind, line.description))


def deposit_charges(lease_end: LeaseEnd) -> list[OneOffCharge]:
    """The lease's deposits (refundable fee types) not yet resolved, oldest first."""
    return list(
        OneOffCharge.objects.filter(lease=lease_end.lease, fee_type__refundable=True)
        .exclude(deposit_resolution__isnull=False)
        .select_related("fee_type")
        .order_by("due_date", "created_at", "id")
    )


def _deposit_lines(lease_end: LeaseEnd) -> list[DepositLine]:
    lines = []
    for charge in deposit_charges(lease_end):
        held = one_off_charges.settled(charge)
        if held > 0:
            lines.append(DepositLine(charge.pk, charge.label, held))
    return lines


def _total(amounts: Iterable[Decimal]) -> Decimal:
    return sum(amounts, ZERO)


# --- The statement --------------------------------------------------------------------------


def open_total(lease_end: LeaseEnd, today: date | None = None) -> Decimal:
    """The open balance the unpaid-balance offset may not exceed."""
    day = today or timezone.localdate()
    lines = _open_lines(lease_end, day, last_month(lease_end), after_end(lease_end))
    return _total(line.balance for line in lines)


def final(lease_end: LeaseEnd, *, today: date | None = None) -> Final:
    """The statement as confirming would leave it (see the module docstring)."""
    day = today or timezone.localdate()
    lease = lease_end.lease
    if lease_end.ended_on is None:
        raise ValueError("The lease end has no end date.")
    last = last_month(lease_end)
    later = after_end(lease_end)
    lines = _open_lines(lease_end, day, last, later)
    deposits = _deposit_lines(lease_end)
    deductions = [
        DeductionLine(row.kind, str(DeductionKind(row.kind).label), row.description, row.amount)
        for row in lease_end.deductions.all()
    ]
    open_sum = _total(line.balance for line in lines)
    held = _total(line.held for line in deposits)
    offset = _total(line.amount for line in deductions if line.kind == DeductionKind.UNPAID_BALANCE)
    other = _total(line.amount for line in deductions if line.kind != DeductionKind.UNPAID_BALANCE)
    credit = (last.credit if last is not None else ZERO) + _total(item.credit for item in later)
    offset_applied = min(offset, held)
    other_withheld = min(other, held - offset_applied)
    withheld = offset_applied + other_withheld
    deposit_refund = held - withheld
    debt = other - other_withheld
    still_open = max(open_sum - offset_applied, ZERO)
    reason = str(LeaseEndReason(lease_end.reason).label) if lease_end.reason else ""
    if lease_end.reason_note:
        reason = f"{reason}: {lease_end.reason_note}" if reason else lease_end.reason_note
    return Final(
        company=str(settings.COMPANY_NAME),
        operator=lease.operator.display_name if lease.operator is not None else "",
        store=lease.store.external_label,
        started_on=lease.started_on,
        ended_on=lease_end.ended_on,
        reason=reason,
        last_month=last,
        after_end=later,
        open_lines=tuple(lines),
        open_total=open_sum,
        deposits=tuple(deposits),
        deposit_held=held,
        deductions=tuple(deductions),
        offset_total=offset,
        other_total=other,
        credit=credit,
        offset_applied=offset_applied,
        withheld=withheld,
        deposit_refund=deposit_refund,
        refund_due=deposit_refund + credit,
        debt=debt,
        still_open=still_open,
        amount_owed=debt + still_open,
        refund_method=lease_end.refund_method,
        refund_on=lease_end.refund_on,
        refund_reference=lease_end.refund_reference,
        prepared_on=day,
    )


# --- The PDF (D7) ---------------------------------------------------------------------------


def long_date(day: date | None) -> str:
    """``October 31, 2026``: dates are written out so that no run of digits the
    payment-data guard reads as one number can form between two of them."""
    if day is None:
        return "not set"
    return f"{day:%B} {day.day}, {day.year}"


def pdf_filename(final_statement: Final) -> str:
    """``final-statement-<ended on>.pdf``."""
    return f"final-statement-{final_statement.ended_on:%Y%m%d}.pdf"


def _lines(statement: Final) -> list[tuple[str, str, str]]:
    """The statement as (style, label, amount) rows, in reading order."""
    money = proration.money
    rows: list[tuple[str, str, str]] = [
        ("title", f"Final statement: store {statement.store}", ""),
        ("text", statement.company, ""),
        ("text", f"Operator: {statement.operator or 'none'}", ""),
        ("text", f"Lease started: {long_date(statement.started_on)}", ""),
        ("text", f"Lease ended: {long_date(statement.ended_on)}", ""),
        ("text", f"Reason: {statement.reason or 'not given'}", ""),
        ("gap", "", ""),
    ]
    last = statement.last_month
    if last is not None:
        rows.append(("heading", f"Last month: {last.month:%B %Y}", ""))
        if last.text:
            rows.append(("text", f"Prorated: {last.text}", ""))
        else:
            rows.append(("text", f"Full month at {money(last.rent)}", money(last.amount)))
        if last.credit > 0:
            rows.append(("text", "Credit: rent paid beyond the last day", money(last.credit)))
        rows.append(("gap", "", ""))
    if statement.after_end:
        rows.append(("heading", "After the end date", ""))
        for item in statement.after_end:
            amount = item.credit if item.credit > 0 else item.amount_due
            rows.append(("text", item.text, money(amount)))
        rows.append(("gap", "", ""))
    rows.append(("heading", "Open balance", ""))
    for line in statement.open_lines:
        rows.append(
            ("text", f"{line.description}, due {long_date(line.due_on)}", money(line.balance))
        )
    if not statement.open_lines:
        rows.append(("text", "Nothing is open.", ""))
    rows.append(("total", "Open balance", money(statement.open_total)))
    rows.append(("gap", "", ""))
    rows.append(("heading", "Deposit", ""))
    for deposit in statement.deposits:
        rows.append(("text", f"Held: {deposit.label}", money(deposit.held)))
    rows.append(("total", "Deposit held", money(statement.deposit_held)))
    rows.append(("gap", "", ""))
    rows.append(("heading", "Deductions", ""))
    for deduction in statement.deductions:
        label = deduction.label + (f": {deduction.description}" if deduction.description else "")
        rows.append(("text", label, money(deduction.amount)))
    if not statement.deductions:
        rows.append(("text", "No deductions.", ""))
    rows.append(("total", "Deductions", money(statement.deductions_total)))
    rows.append(("gap", "", ""))
    rows.append(("heading", "Settlement", ""))
    rows.append(("text", "Deposit kept for the deductions", money(statement.withheld)))
    rows.append(("text", "Of which applied to the open balance", money(statement.offset_applied)))
    rows.append(("total", "Refund due to the operator", money(statement.refund_due)))
    rows.append(("total", "Amount owed by the operator", money(statement.amount_owed)))
    if statement.debt > 0:
        rows.append(("text", "Lease-end balance charged", money(statement.debt)))
    if statement.still_open > 0:
        rows.append(("text", "Still open on its charges", money(statement.still_open)))
    if statement.refund_due > 0:
        rows.append(
            (
                "text",
                f"Refund by {statement.refund_method_label or 'a method not set'} on "
                f"{long_date(statement.refund_on)}",
                "",
            )
        )
        if statement.refund_reference:
            rows.append(("text", f"Reference: {statement.refund_reference}", ""))
    rows.append(("gap", "", ""))
    rows.append(("text", f"Prepared on {long_date(statement.prepared_on)}", ""))
    return rows


def render_pdf(statement: Final) -> bytes:
    """The statement as a PDF (D7): the company, the operator, the store, the lease
    dates, the lines, the settlement and the proration text, "Prepared on <date>". No
    account or card number: methods and references only. The same figures always give
    the same bytes (``invariant``), so a retried job finds the document it stored."""
    from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
    from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=letter, invariant=1, pageCompression=0)
    canvas.setTitle(f"Final statement {statement.store}")
    canvas.setAuthor(statement.company)
    width, height = letter
    left, right, top, bottom = 72, width - 72, height - 72, 72
    y = top
    fonts = {
        "title": ("Helvetica-Bold", 16, 24),
        "heading": ("Helvetica-Bold", 12, 18),
        "total": ("Helvetica-Bold", 10, 15),
        "text": ("Helvetica", 10, 15),
        "gap": ("Helvetica", 10, 8),
    }
    for style, label, amount in _lines(statement):
        font, size, step = fonts[style]
        if y - step < bottom:
            canvas.showPage()
            y = top
        y -= step
        if style == "gap":
            continue
        canvas.setFont(font, size)
        canvas.drawString(left, y, label[:110])
        if amount:
            canvas.drawRightString(right, y, amount)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()
