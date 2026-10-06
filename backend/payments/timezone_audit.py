"""The New York time-zone audit and its one-time realignment (docs/qa/PLAN-011b D1).

Until PLAN-011b the project's ``TIME_ZONE`` was UTC, so every date the app took from a
timestamp was the UTC date. Now it is the New York date. A few stored dates were taken
from a timestamp that falls on another day in New York (an evening in New York is the
next day in UTC). :func:`audit` lists them; :func:`apply` rewrites exactly those rows.
Nothing else is ever rewritten.

**Candidates** (each date with the timestamp it was taken from):

- ``payments.initiated_on`` from ``payments.provider_created_at`` (ACH payments, the
  rows the provider's events made);
- ``payments.settled_on`` from the settlement timestamp kept in ``payments.raw_summary``
  under :data:`SETTLED_AT_KEY` (ACH payments; the summary is the provider's transaction
  as first fetched, so a payment fetched before it settled has no settlement time there
  and is counted under "no source timestamp");
- ``invoices.issue_date`` from ``invoices.created_at`` (every invoice but the legacy
  ones of the sheet import).

**The rule.** A row is listed when its stored date is the UTC date of its source and
not the New York date: that is a date the old setting gave and the new one would not.
The new date is the New York date. A stored date that matches neither (a provider's own
invoice date, a date set by hand) is not a time-zone artefact: it is counted and shown
apart, and never rewritten. A row whose source is missing is counted only.

**Unchanged by design** (:data:`UNCHANGED_BY_DESIGN`): dates set by staff or by a rule,
never taken from a timestamp. They are printed with the reason and never touched.

:func:`apply` reruns the audit inside one transaction with the rows locked, refuses
unless the caller's count equals the audit's total, rewrites each listed row with an
``UPDATE ... WHERE <column> = <old date>`` (a row changed since the audit makes the whole
run roll back), and writes one ``audit_log`` row per change, action
:data:`ACTION`, with the column's value before and after.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Model

from ledger.models import AuditLog, Invoice, Payment, PaymentMethod
from payments.provider import mask_identifier
from payments.stax.mapping import TRANSACTION_SETTLED_AT_KEY

#: The key of the provider's settlement time in a payment's ``raw_summary``.
SETTLED_AT_KEY = TRANSACTION_SETTLED_AT_KEY
ACTION = "timezone.date_realigned"

PAYMENTS_INITIATED_ON = "payments.initiated_on"
PAYMENTS_SETTLED_ON = "payments.settled_on"
INVOICES_ISSUE_DATE = "invoices.issue_date"
#: The audited columns, in the order they are printed.
COLUMNS: tuple[str, ...] = (PAYMENTS_INITIATED_ON, PAYMENTS_SETTLED_ON, INVOICES_ISSUE_DATE)
#: Where each audited column's date comes from.
SOURCES: dict[str, str] = {
    PAYMENTS_INITIATED_ON: "payments.provider_created_at",
    PAYMENTS_SETTLED_ON: f"payments.raw_summary.{SETTLED_AT_KEY}",
    INVOICES_ISSUE_DATE: "invoices.created_at",
}
#: Dates that are never candidates, with the reason (D1).
UNCHANGED_BY_DESIGN: tuple[tuple[str, str], ...] = (
    ("rent_charges.due_on", "set by the rent rule from the period month and the debit day"),
    (
        "rent_charges.period_month",
        "the first day of the rent month, set by the rent rule",
    ),
    ("one_off_charges.due_date", "set by staff when the charge is made"),
    ("invoices.due_date", "the provider's due date or the charge's due date, never a time"),
    (
        "invoices.issue_date (legacy rows)",
        "copied from the sheet by the import",
    ),
    (
        "payments.initiated_on and settled_on (Zelle, wire, other)",
        "entered by staff with the payment",
    ),
    ("insurance_charges.due_on", "the store's open-date anniversary"),
    ("deposit_resolutions.resolved_on", "entered by staff"),
    ("store_costs.effective_from and effective_to", "entered by staff"),
    ("leases.started_on and ended_on", "entered by staff"),
)

_MODELS: dict[str, type[Model]] = {"payments": Payment, "invoices": Invoice}
_ENTITY: dict[str, str] = {"payments": "payment", "invoices": "invoice"}


class AuditCountMismatch(Exception):  # noqa: N818 (reads as an outcome)
    """``--audited`` does not equal the current audit's total: nothing was changed."""


class RowChangedSinceAudit(Exception):  # noqa: N818 (reads as an outcome)
    """A listed row no longer holds its old date: the whole run was rolled back."""


@dataclass(frozen=True, slots=True)
class Change:
    """One stored date and the date its source gives in New York."""

    column: str
    row_id: uuid.UUID
    old: date
    new: date

    @property
    def table(self) -> str:
        return self.column.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.column.split(".", 1)[1]

    def line(self) -> str:
        return f"{self.column} {mask_identifier(str(self.row_id))} {self.old} -> {self.new}"


@dataclass(slots=True)
class Audit:
    #: Rows to realign: the stored date is the source's UTC date, not its New York date.
    changes: list[Change] = field(default_factory=list)
    #: Rows whose date differs from both: not a time-zone artefact, never rewritten.
    other: list[Change] = field(default_factory=list)
    #: Column -> rows with no source timestamp.
    no_source: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COLUMNS, 0))

    @property
    def total(self) -> int:
        return len(self.changes)

    def counts(self) -> dict[str, int]:
        found = dict.fromkeys(COLUMNS, 0)
        for change in self.changes:
            found[change.column] += 1
        return found


def project_zone() -> ZoneInfo:
    return ZoneInfo(settings.TIME_ZONE)


def parse_timestamp(value: object) -> datetime | None:
    """A stored ISO 8601 timestamp (a space may separate date and time); a naive one is
    UTC, as the adapter reads it. ``None`` when missing or unreadable: never guessed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _judge(found: Audit, column: str, row_id: uuid.UUID, stored: date, source: datetime) -> None:
    utc_day = source.astimezone(UTC).date()
    new = source.astimezone(project_zone()).date()
    if stored == new:
        return
    change = Change(column, row_id, stored, new)
    (found.changes if stored == utc_day else found.other).append(change)


def _payments(locked: bool) -> Iterator[tuple[Any, ...]]:
    rows = Payment.objects.filter(method=PaymentMethod.ACH).order_by("pk")
    if locked:
        rows = rows.select_for_update()
    yield from rows.values_list(
        "pk", "initiated_on", "settled_on", "provider_created_at", "raw_summary"
    ).iterator(chunk_size=2000)


def audit(*, locked: bool = False) -> Audit:
    """The rows whose stored date would change (see the module docstring). Read only;
    with ``locked`` (inside :func:`apply`'s transaction) the rows read are locked."""
    found = Audit()
    for pk, initiated_on, settled_on, created_at, summary in _payments(locked):
        if initiated_on is not None:
            if isinstance(created_at, datetime):
                _judge(found, PAYMENTS_INITIATED_ON, pk, initiated_on, created_at)
            else:
                found.no_source[PAYMENTS_INITIATED_ON] += 1
        if settled_on is not None:
            raw = summary.get(SETTLED_AT_KEY) if isinstance(summary, dict) else None
            settled_at = parse_timestamp(raw)
            if settled_at is not None:
                _judge(found, PAYMENTS_SETTLED_ON, pk, settled_on, settled_at)
            else:
                found.no_source[PAYMENTS_SETTLED_ON] += 1
    invoices = Invoice.objects.filter(legacy=False).order_by("pk")
    if locked:
        invoices = invoices.select_for_update()
    for pk, issue_date, created in invoices.values_list("pk", "issue_date", "created_at").iterator(
        chunk_size=2000
    ):
        _judge(found, INVOICES_ISSUE_DATE, pk, issue_date, created)
    return found


def report_lines(found: Audit) -> Iterator[str]:
    """The audit as printed by ``tz_audit``: every listed row, the counts, the rows left
    alone, and the columns unchanged by design with their reason. IDs are masked."""
    yield f"Time zone audit: stored dates against their source in {settings.TIME_ZONE}."
    yield "Rows to realign (table.column, masked id, old -> new):"
    if not found.changes:
        yield "  none"
    for change in found.changes:
        yield f"  {change.line()}"
    yield "Counts:"
    counts = found.counts()
    for column in COLUMNS:
        yield f"  {column} (from {SOURCES[column]}): {counts[column]}"
    yield f"  total: {found.total}"
    yield "Rows whose date differs but is not a time-zone artefact (never rewritten):"
    if not found.other:
        yield "  none"
    for change in found.other:
        yield f"  {change.line()}"
    yield "Rows without a source timestamp (not audited):"
    for column in COLUMNS:
        yield f"  {column}: {found.no_source[column]}"
    yield "Unchanged by design:"
    for column, reason in UNCHANGED_BY_DESIGN:
        yield f"  {column}: {reason}"
    yield f"To apply: tz_apply --audited {found.total}"


def _rewrite(changes: Iterable[Change]) -> None:
    for change in changes:
        model = _MODELS[change.table]
        updated = model._default_manager.filter(
            pk=change.row_id, **{change.field: change.old}
        ).update(**{change.field: change.new})
        if updated != 1:
            raise RowChangedSinceAudit
        AuditLog.objects.create(
            actor=None,
            action=ACTION,
            entity_type=_ENTITY[change.table],
            entity_id=str(change.row_id),
            before={change.field: change.old.isoformat()},
            after={
                change.field: change.new.isoformat(),
                "source": SOURCES[change.column],
                "time_zone": settings.TIME_ZONE,
            },
        )


def apply(audited: int) -> Audit:
    """Rewrite exactly the rows of the current audit, in one transaction, when
    ``audited`` equals its total; otherwise raise :class:`AuditCountMismatch` and change
    nothing. Returns the audit that was applied."""
    with transaction.atomic():
        found = audit(locked=True)
        if audited != found.total:
            raise AuditCountMismatch
        _rewrite(found.changes)
    return found
