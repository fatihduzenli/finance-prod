"""Provider state of an invoice matched to a rent charge (PLAN-007 D7).

``invoices.provider_status`` is written only through :func:`payments.sync_writer.
provider_write`, which is private to this package; the rent run in ``ledger`` reaches
it through :func:`record_provider_status`. Reading an invoice's date from a provider
record is also kept here, so ``ledger`` never handles provider field names.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from ledger.models import Invoice
from payments.provider import InvoiceRecord
from payments.sync_writer import provider_write

#: TODO-VERIFY-038: the keys and formats of an invoice's date, due date and schedule in
#: a ``GET /invoice`` response. The request names ``invoice_date_at``, ``due_at`` and
#: ``schedule_id`` and an ISO 8601 value (``YYYY-MM-DD``, optionally followed by a time)
#: are assumed.
INVOICE_DATE_KEY = "invoice_date_at"
DUE_DATE_KEY = "due_at"
SCHEDULE_KEY = "schedule_id"


def record_provider_status(invoice_id: UUID, raw_status: str) -> None:
    """Copy the provider's status string, unmodified, onto the local invoice (Rule 2)."""
    with provider_write():
        Invoice.objects.filter(pk=invoice_id).exclude(provider_status=raw_status).update(
            provider_status=raw_status
        )


def _date(record: InvoiceRecord, key: str) -> date | None:
    """The calendar date of a summary field, or ``None`` when it is missing, unparseable,
    or names a different day in UTC than the one it states (a month-boundary time)."""
    value = record.raw_summary.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    stated = moment.date()
    if moment.tzinfo is not None and moment.astimezone(UTC).date() != stated:
        return None
    return stated


def invoice_date(record: InvoiceRecord) -> date | None:
    """The invoice's own date, or ``None`` when it cannot be determined (D7)."""
    return _date(record, INVOICE_DATE_KEY)


def due_date(record: InvoiceRecord) -> date | None:
    return _date(record, DUE_DATE_KEY)


def schedule_id(record: InvoiceRecord) -> str:
    """The schedule the provider says generated the invoice, or ``""``."""
    value = record.raw_summary.get(SCHEDULE_KEY)
    return str(value) if isinstance(value, str | int) and not isinstance(value, bool) else ""
