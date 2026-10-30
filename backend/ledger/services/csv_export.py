"""The one CSV writer of every export (docs/qa/PLAN-011-dashboard-reports-mobile.md D5, D7).

Read only: nothing here touches the database or the payment provider.

**Cells** (:func:`cell`) are written from typed values, never pre-formatted text:

- ``Decimal`` is money: plain text with exactly two places (``1234.50``, ``-12.00``),
  never a float and never a thousands separator;
- ``date`` and ``datetime`` are ISO 8601;
- ``bool`` is ``yes`` or ``no``; ``None`` is the empty cell; ``int`` and ``UUID`` are
  their text;
- ``str`` is formula-safe (:func:`csv_text`): a text a spreadsheet would read as a formula
  starts with an apostrophe;
- any other type is refused (``TypeError``), a binary floating-point number above all:
  money is exact or it is not written.

**Headers** are plain-English labels with the unit where the column is money
(``Amount due (USD)``), each one listed in :data:`HEADERS`, the reviewed list; a writer
built with a name that is not in it refuses to start (``ValueError``). The one other
header shape is a month column of the month grid, ``YYYY-MM`` (the sheet's own heading,
:data:`MONTH_HEADER`).

**The writer** (:class:`CsvWriter`) streams: it yields the header line, then one line per
record, as the records come. After :data:`CSV_MAX_ROWS` records it writes a final
``truncated`` row naming the cap and stops reading. The ``csv`` module quotes commas,
quotes and line breaks, so a note never splits a row.
"""

from __future__ import annotations

import csv
import re
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime
from decimal import Decimal

#: The most data rows one export writes; one more row says the file was cut.
CSV_MAX_ROWS = 50_000
TRUNCATED = "truncated"
TRUNCATED_TEXT = "Only the first {limit} rows are exported; narrow the filters."
CENT = Decimal("0.01")
#: First characters that make a spreadsheet read a cell as a formula.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
#: A month column of the month grid (the sheet's heading).
MONTH_HEADER = re.compile(r"\A[0-9]{4}-[0-9]{2}\Z")

#: Every column name an export may write: plain English, the unit where money.
HEADERS: frozenset[str] = frozenset(
    {
        # Shared
        "Operator",
        "Store",
        "Store number",
        "Brand",
        "State",
        "City",
        "Channel",
        "Channels",
        "Stores",
        "Status",
        "Month",
        "Date",
        "Due date",
        "Description",
        "Note",
        "Kind",
        "Level",
        "Line",
        "Detail",
        # Payments ledger
        "Payment ID",
        "Paid date",
        "Initiated date",
        "Charge kind",
        "Rent month",
        "Debit of the month",
        "Policy year",
        "Method",
        "Refund",
        "Refund of payment ID",
        "Amount (USD)",
        "Reference",
        "Charge has a note",
        "Provider transaction (masked)",
        "Return code",
        "Recorded by",
        # PLAN-012e D12: the charge an installment's plan splits.
        "Installment of",
        # Profit and loss
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
        # Audit log
        "Audit entry",
        "Time (UTC)",
        "Time (New York)",
        "Action",
        "User",
        "Record type",
        "Record (masked)",
        "Before (JSON)",
        "After (JSON)",
        # Arrears
        "Months behind",
        "Months-behind bucket",
        "Total owed (USD)",
        "Overdue (USD)",
        "Last payment date",
        # Aging
        "Current (USD)",
        "1 to 30 days (USD)",
        "31 to 60 days (USD)",
        "61 to 90 days (USD)",
        "Over 90 days (USD)",
        "Total (USD)",
        "Open charges",
        # Revenue by month
        "Insurance billed (USD)",
        "Insurance collected (USD)",
        "Other fees billed (USD)",
        "Other fees collected (USD)",
        "Total billed (USD)",
        "Total collected (USD)",
        # Payments by method
        "ACH (USD)",
        "Zelle (USD)",
        "Wire (USD)",
        "Check (USD)",
        "Cash (USD)",
        "Other (USD)",
        "Payments",
        # Deposits versus transactions
        "Deposit reference (masked)",
        "Gross (USD)",
        "Fees (USD)",
        "Net (USD)",
        "Settled ACH payments (USD)",
        "Settled ACH refunds (USD)",
        "Difference (USD)",
        # Customer statement
        "Section",
        "Amount due (USD)",
        "Paid (USD)",
        "Balance (USD)",
        # Operators
        "Legal name",
        "Company",
        "Email",
        "Phone",
        "SMS phone",
        "SMS consent given on",
        "E-mail consent given on",
        "Provider link",
        "Enrolment",
        "Needs review",
        "Archived on",
        # Stores
        "Open date",
        "Closed on",
        "Needs open date",
        # Leases
        "Rent now (USD)",
        "Debit day",
        "Second debit day",
        "Started on",
        "Ended on",
        "Charges have a note",
        # Rent charges and the run
        "Carried balance (USD)",
        "Last month status",
        "Last month balance (USD)",
        # One-off charges
        "Category",
        "Collection",
        "Price (USD)",
        "Cost (USD)",
        "Deposit",
        # Returns
        "Return reason",
        "Returned at",
        # Webhooks
        "Received at",
        "Event type",
        "Name from",
        "Provider object (masked)",
        "Outcome",
        "Failed attempts",
        "Duplicates",
        # Reconciliation mismatches
        "Found at",
        "Mismatch",
        "Resolved at",
        "Resolved by",
        "Resolution note",
        # Documents
        "File name",
        "File type",
        "Size (bytes)",
        "Uploaded at",
        "Uploaded by",
        "Deleted at",
        # Contact log (PLAN-011b D7, PLAN-012 D2 to D5)
        "Promised date",
        "Promised amount (USD)",
        "Promise status",
        "Closes promise",
        "Recorded at",
        "Corrections",
        "Delete reason",
        # Disputes (PLAN-012 D8)
        "Respond by",
        "Days left",
        "Reason",
        "Created at provider",
        "Updated at provider",
        "Evidence files",
        "Read at",
    }
)


def reviewed(name: str) -> bool:
    """Whether ``name`` may head a column: a listed label or a grid month."""
    return name in HEADERS or MONTH_HEADER.match(name) is not None


def csv_text(value: str) -> str:
    """A text cell; one a spreadsheet would read as a formula starts with an apostrophe."""
    return f"'{value}" if value.startswith(FORMULA_START) else value


def money(value: Decimal) -> str:
    """Money as plain text with two places, from the exact decimal."""
    return format(value.quantize(CENT), "f")


def cell(value: object) -> str:
    """One cell from a typed value (see the module docstring)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        return money(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, int | uuid.UUID):
        return str(value)
    if isinstance(value, str):
        return csv_text(value)
    # Anything else, a binary floating-point number above all, is refused: money is exact.
    raise TypeError(f"No CSV cell for a {type(value).__name__}.")


class CsvLine:
    """A file-like object whose ``write`` returns the line, for a streamed ``csv.writer``."""

    def write(self, value: str) -> str:
        return value


class CsvWriter:
    """The header, then the records, as CSV lines, at most ``limit`` records."""

    def __init__(self, header: Sequence[str], *, limit: int | None = None) -> None:
        unknown = [name for name in header if not reviewed(name)]
        if unknown:
            raise ValueError(f"Column names not in the reviewed list: {', '.join(unknown)}")
        self.header = list(header)
        self.limit = CSV_MAX_ROWS if limit is None else limit

    def lines(self, records: Iterable[Sequence[object]]) -> Iterator[str]:
        writer = csv.writer(CsvLine())
        yield writer.writerow(self.header)
        for count, record in enumerate(records):
            if count == self.limit:
                yield writer.writerow([TRUNCATED, TRUNCATED_TEXT.format(limit=self.limit)])
                return
            yield writer.writerow([cell(value) for value in record])
