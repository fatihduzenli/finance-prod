"""Which open charge a bank deposit pays
(docs/qa/PLAN-014-per-lease-billing-messaging-portal-deposits.md I3).

:func:`suggest` reads :func:`ledger.services.receivables.open_balances` (the one place that
decides what is owed) and gives a :class:`Suggestion`, the first level that has a candidate:

- **exact**: an invoice number (``INV-<year>-<number>``) in the description is the invoice of
  an open charge (``Invoice.rent_charge``, ``one_off_charge`` or ``insurance_charge``, or a
  line of a builder invoice) whose balance equals the amount;
- **strong**: a :class:`~banking.models.SenderAlias` whose normalised text occurs in the
  normalised description or sender, and the amount equals one open charge of that lease, or
  that lease's whole open balance (``charge_id`` is then ``None``: every open charge);
- **weak**: the amount equals exactly one open charge anywhere;
- **none** otherwise.

A tie within the first level that has candidates (two charges, two leases) is **none**, with
those candidates listed for staff; a lower level is not tried then. Only charges of a lease are
candidates (a one-off charge without a lease is paid from its own page). :func:`suggest_all`
reads the open balances, the aliases and the invoices named once for many transactions, so a
page's queries do not grow with its rows.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from banking.models import BankTransaction, Confidence, SenderAlias, normalise
from ledger.models import ZERO, Invoice, InvoiceLineItem, InvoiceStatus
from ledger.services import receivables

INVOICE_NUMBER = re.compile(r"INV-\d{4}-\d{4,}")
_KINDS = {
    "rent_charge": receivables.KIND_RENT,
    "one_off_charge": receivables.KIND_ONE_OFF,
    "insurance_charge": receivables.KIND_INSURANCE,
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A charge the deposit may pay (``charge_id`` ``None``: the lease's whole balance)."""

    lease: uuid.UUID
    charge_type: str
    charge_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class Suggestion:
    confidence: str
    lease: uuid.UUID | None = None
    charge_type: str = ""
    charge_id: uuid.UUID | None = None
    candidates: tuple[Candidate, ...] = ()


NONE = Suggestion(Confidence.NONE.value)


@dataclass(slots=True)
class Context:
    """What the levels read, once for many transactions."""

    rows: list[receivables.OpenBalance]
    by_charge: dict[tuple[str, uuid.UUID], receivables.OpenBalance] = field(default_factory=dict)
    by_lease: dict[uuid.UUID, list[receivables.OpenBalance]] = field(default_factory=dict)
    aliases: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    invoices: dict[str, set[tuple[str, uuid.UUID]]] = field(default_factory=dict)


def invoice_numbers(text: str) -> list[str]:
    return INVOICE_NUMBER.findall(text.upper())


def build_context(transactions: Iterable[BankTransaction], today: date) -> Context:
    rows = [
        row
        for row in receivables.open_balances(today)
        if row.lease_id is not None and row.balance > 0
    ]
    context = Context(rows)
    for row in rows:
        context.by_charge[(row.kind, row.charge_id)] = row
        context.by_lease.setdefault(_lease_of(row), []).append(row)
    context.aliases = list(SenderAlias.objects.values_list("lease_id", "normalised"))
    numbers = {number for txn in transactions for number in invoice_numbers(txn.description)}
    if numbers:
        context.invoices = _invoice_charges(numbers)
    return context


def _invoice_charges(numbers: set[str]) -> dict[str, set[tuple[str, uuid.UUID]]]:
    found: dict[str, set[tuple[str, uuid.UUID]]] = defaultdict(set)
    invoices = (
        Invoice.objects.filter(invoice_number__in=numbers)
        .exclude(internal_status=InvoiceStatus.VOID)
        .values("invoice_number", *_KINDS)
    )
    for invoice in invoices:
        for column, kind in _KINDS.items():
            if invoice[column] is not None:
                found[invoice["invoice_number"]].add((kind, invoice[column]))
    lines = (
        InvoiceLineItem.objects.filter(invoice__invoice_number__in=numbers)
        .exclude(invoice__internal_status=InvoiceStatus.VOID)
        .values_list("invoice__invoice_number", "rent_charge", "one_off_charge")
    )
    for number, rent, one_off in lines:
        if rent is not None:
            found[number].add((receivables.KIND_RENT, rent))
        if one_off is not None:
            found[number].add((receivables.KIND_ONE_OFF, one_off))
    return dict(found)


def _one(candidates: Sequence[Candidate], confidence: str) -> Suggestion:
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) == 1:
        [only] = unique
        return Suggestion(confidence, only.lease, only.charge_type, only.charge_id, unique)
    return Suggestion(Confidence.NONE.value, candidates=unique)


def _lease_of(row: receivables.OpenBalance) -> uuid.UUID:
    """The row's lease; :func:`build_context` keeps only rows that have one."""
    if row.lease_id is None:
        raise ValueError("an open balance without a lease is not a candidate")
    return row.lease_id


def _candidate(row: receivables.OpenBalance) -> Candidate:
    return Candidate(_lease_of(row), row.kind, row.charge_id)


def _exact(txn: BankTransaction, context: Context) -> list[Candidate]:
    found = []
    for number in invoice_numbers(txn.description):
        for key in sorted(context.invoices.get(number, ()), key=str):
            row = context.by_charge.get(key)
            if row is not None and row.balance == txn.amount:
                found.append(_candidate(row))
    return found


def _strong(txn: BankTransaction, context: Context) -> list[Candidate]:
    texts = (normalise(txn.description), normalise(txn.counterparty))
    leases = dict.fromkeys(
        lease for lease, alias in context.aliases if alias and any(alias in text for text in texts)
    )
    found = []
    for lease in leases:
        rows = context.by_lease.get(lease, [])
        equal = [_candidate(row) for row in rows if row.balance == txn.amount]
        if equal:
            found += equal
        elif rows and sum((row.balance for row in rows), ZERO) == txn.amount:
            found.append(Candidate(lease, "", None))
    return found


def _weak(txn: BankTransaction, context: Context) -> list[Candidate]:
    return [_candidate(row) for row in context.rows if row.balance == txn.amount]


def suggest_with(txn: BankTransaction, context: Context) -> Suggestion:
    for level, confidence in (
        (_exact, Confidence.EXACT),
        (_strong, Confidence.STRONG),
        (_weak, Confidence.WEAK),
    ):
        candidates = level(txn, context)
        if candidates:
            return _one(candidates, confidence.value)
    return NONE


def suggest_all(
    transactions: Sequence[BankTransaction], today: date
) -> dict[uuid.UUID, Suggestion]:
    """:func:`suggest` for each transaction, reading what is owed once."""
    if not transactions:
        return {}
    context = build_context(transactions, today)
    return {txn.pk: suggest_with(txn, context) for txn in transactions}


def suggest(transaction: BankTransaction, today: date) -> Suggestion:
    """The suggestion for one transaction (see the module docstring)."""
    return suggest_all([transaction], today)[transaction.pk]


def whole_balance(context: Context, lease: uuid.UUID) -> Decimal:
    return sum((row.balance for row in context.by_lease.get(lease, [])), ZERO)
