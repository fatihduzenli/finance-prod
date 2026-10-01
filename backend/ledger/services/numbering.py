"""Invoice numbers ``INV-YYYY-NNNN`` (owner's format, DATA_OWNERSHIP §6; PLAN-007 B2).

One counter per calendar year, kept as a ``sync_state`` row named
``invoice_number:<year>`` whose ``cursor`` holds the last number issued. The row is
locked (``SELECT ... FOR UPDATE``) while the next number is taken, so two transactions
never receive the same number, and the increment commits or rolls back with the invoice
that uses it: numbers are gap-free and never reused. A number some other path already
gave an invoice (demo data, a carried-forward legacy number) is passed over, never
issued twice. Past 9999 the sequence simply grows a digit (the schema accepts four or
more).
"""

from __future__ import annotations

from django.db import transaction

from ledger.models import Invoice
from payments.models import SyncState

JOB_PREFIX = "invoice_number:"
FIRST_YEAR = 2000
LAST_YEAR = 2100


def counter_name(year: int) -> str:
    return f"{JOB_PREFIX}{year}"


def format_number(year: int, sequence: int) -> str:
    return f"INV-{year:04d}-{sequence:04d}"


def next_invoice_number(year: int) -> str:
    """Take the next number of ``year``. Must run inside the caller's transaction, so a
    rollback of the invoice also returns its number."""
    if not FIRST_YEAR <= year <= LAST_YEAR:
        raise ValueError("Invoice year out of range")
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("next_invoice_number must run inside a transaction")
    name = counter_name(year)
    SyncState.objects.get_or_create(job_name=name, defaults={"cursor": "0"})
    row = SyncState.objects.select_for_update().get(job_name=name)
    sequence = int(row.cursor or "0") + 1
    while Invoice.objects.filter(invoice_number=format_number(year, sequence)).exists():
        sequence += 1
    row.cursor = str(sequence)
    row.save(update_fields=["cursor"])
    return format_number(year, sequence)
