"""Provider state of an invoice matched to a rent charge (PLAN-007 D7, PLAN-008 D4).

``invoices.provider_status`` is written only through :func:`payments.sync_writer.
provider_write`, which is private to this package; the rent run in ``ledger`` reaches
it through :func:`record_provider_status`, the webhook handlers through
:func:`record_provider_state`, which also copies the sent, viewed and paid times and
the amount paid. Reading an invoice's date from a provider record is also kept here, so
``ledger`` never handles provider field names.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
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
#: TODO-VERIFY-055: the status string of an invoice the provider shows paid in full.
#: ``PAID`` is assumed (PLAN-009 D1); compared stripped and upper-cased.
PROVIDER_PAID = "PAID"


def shows_paid(record: InvoiceRecord) -> bool:
    """Whether the provider's invoice is already paid (PLAN-009 D1 pre-check)."""
    return record.raw_status.strip().upper() == PROVIDER_PAID


def record_provider_status(invoice_id: UUID, raw_status: str) -> None:
    """Copy the provider's status string, unmodified, onto the local invoice (Rule 2)."""
    with provider_write():
        Invoice.objects.filter(pk=invoice_id).exclude(provider_status=raw_status).update(
            provider_status=raw_status
        )


#: The invoice columns that mirror the provider (PLAN-008 D4), in display order.
PROVIDER_FIELDS: tuple[str, ...] = (
    "provider_status",
    "sent_at",
    "viewed_at",
    "paid_at",
    "amount_paid",
)
_CENT = Decimal("0.01")
#: Largest amount ``numeric(12,2)`` holds.
_MONEY_MAX = Decimal("9999999999.99")


def _provider_values(record: InvoiceRecord) -> dict[str, Any]:
    """The provider's values for :data:`PROVIDER_FIELDS`. An amount paid that is
    negative, has more than two decimals or does not fit the column is left out, as is
    every time that is unknown: a value we hold is never replaced by a guess."""
    values: dict[str, Any] = {"provider_status": record.raw_status}
    for name in ("sent_at", "viewed_at", "paid_at"):
        moment = getattr(record, name)
        if moment is not None:
            values[name] = moment
    paid = record.amount_paid
    if paid is not None and 0 <= paid <= _MONEY_MAX and paid == paid.quantize(_CENT):
        values["amount_paid"] = paid
    return values


def _shown(value: object) -> object:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def record_provider_state(invoice_id: UUID, record: InvoiceRecord) -> dict[str, Any]:
    """Copy the provider's status, times and amount paid onto the local invoice.

    Returns ``{field: [before, after]}`` for each field that changed (empty when none
    did), as text for the audit row; nothing is written when nothing changed.
    """
    values = _provider_values(record)
    changed: dict[str, Any] = {}
    with provider_write():
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        for name, value in values.items():
            current = getattr(invoice, name)
            if current != value:
                changed[name] = [
                    _shown(current) if current is not None else None,
                    _shown(value),
                ]
        if changed:
            Invoice.objects.filter(pk=invoice_id).update(**{name: values[name] for name in changed})
    return changed


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
