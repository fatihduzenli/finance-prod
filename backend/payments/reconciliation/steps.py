"""The reconciliation steps a to e (docs/qa/PLAN-010-worker-and-reconciliation.md D7).

Each step takes the run's :class:`Context`, calls the provider only through
``ctx.provider`` (or the webhook handlers' own re-fetches, which use the same
rate-limited client), and returns its counts. Every write is idempotent and committed
item by item, so a run that stops halfway leaves nothing half-applied and the next run
redoes the window (D9). Nothing here sleeps: the limiter paces every request.

Detail texts name identifiers masked only (:func:`payments.provider.mask_identifier`),
and never an amount of a named payment.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from ledger.models import Invoice, Payment, PaymentMethod, PaymentStatus
from payments import invoice_sync
from payments.lease_sync import PROVIDER_KEY
from payments.models import ExternalRef, MismatchKind, ReconciliationRun
from payments.provider import (
    DateRange,
    PaymentProvider,
    Summary,
    TransactionQuery,
    TransactionRecord,
    mask_identifier,
)
from payments.reconciliation import mismatches
from payments.webhooks.handlers import transaction as transaction_handler
from payments.webhooks.handlers.common import (
    ENTITY_INVOICE,
    ENTITY_PAYMENT,
    KIND_INVOICE,
    KIND_TRANSACTION,
    audit,
    fetch,
)

Counts = Counter[str]

#: Step (a): invoices in these provider statuses are re-fetched, whatever their age.
#: The strings are the provider's, as stored (docs/CLAUDE.md Rule 2, TODO-VERIFY-003).
OPEN_INVOICE_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "SENT",
    "VIEWED",
    "PARTIALLY APPLIED",
    "PARTIALLY PAID",
    "ATTEMPTED",
)
#: ... and so is every invoice changed locally in the last this many days.
RECENT_INVOICE_DAYS = 7
#: Step (b): transactions per page. TODO-VERIFY-059: the largest ``per_page`` that
#: ``GET /transaction`` accepts; 100 is assumed.
TRANSACTIONS_PER_PAGE = 100
#: More pages than this in one window fails the run rather than stop silently.
MAX_TRANSACTION_PAGES = 50
#: Step (c): TODO-VERIFY-060: the key of a ``GET /query/depositDetail`` row that holds
#: the transaction's ID; ``transaction_id`` is assumed.
DEPOSIT_TRANSACTION_KEY = "transaction_id"
#: Step (c): a settled ACH payment of the deposit window that is at least this many days
#: old and in no deposit of the window is flagged; a younger one may not be batched yet.
UNDEPOSITED_AFTER_DAYS = 3
#: Longest provider status quoted in a detail text.
_STATUS_SHOWN = 40


class PageLimitReached(Exception):  # noqa: N818 (reads as a state, like the provider errors)
    """The transaction window has more pages than :data:`MAX_TRANSACTION_PAGES`."""


@dataclass
class Context:
    run: ReconciliationRun
    provider: PaymentProvider
    now: datetime
    #: Start of the transaction and rejection window (D7 b, d).
    window_start: datetime
    #: New mismatches of this run, by kind (D8).
    new_by_kind: Counts = field(default_factory=Counter)

    @property
    def today(self) -> date:
        return timezone.localdate(self.now)

    @property
    def window(self) -> DateRange:
        return DateRange(timezone.localdate(self.window_start), self.today)

    def mismatch(
        self,
        counts: Counts,
        kind: str,
        entity_type: str,
        entity_id: uuid.UUID,
        detail: str,
        *,
        resolved: bool = False,
    ) -> None:
        """Record one finding: ``mismatched`` always, and ``seen`` when an unresolved row
        of it already existed."""
        counts["mismatched"] += 1
        new = mismatches.record(
            self.run,
            kind,
            entity_type,
            entity_id,
            detail,
            resolved_at=self.now if resolved else None,
        )
        if new:
            self.new_by_kind[kind] += 1
        else:
            counts["seen"] += 1


def _status(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:_STATUS_SHOWN] if flat else "(none)"


# --- (a) invoices -------------------------------------------------------------------------


def invoices_to_check(now: datetime) -> list[tuple[uuid.UUID, str]]:
    """``(invoice ID, provider invoice ID)`` of every local invoice with a current
    provider reference whose status is open or that changed in the last week."""
    wanted = Invoice.objects.filter(
        Q(provider_status__in=OPEN_INVOICE_STATUSES)
        | Q(updated_at__gte=now - timedelta(days=RECENT_INVOICE_DAYS))
    ).values("pk")
    rows = (
        ExternalRef.objects.filter(
            provider=PROVIDER_KEY,
            entity_type=ENTITY_INVOICE,
            external_kind=KIND_INVOICE,
            superseded_at__isnull=True,
            entity_id__in=wanted,
        )
        .order_by("created_at", "pk")
        .values_list("entity_id", "external_id")
    )
    return list(rows)


def invoices(ctx: Context) -> Counts:
    """(a) Re-fetch each invoice by ID and copy the provider's state through the sync
    writer; a local status that differed is logged as resolved by the run."""
    counts: Counts = Counter()
    for invoice_id, external_id in invoices_to_check(ctx.now):
        counts["checked"] += 1
        record = fetch(ctx.provider.get_invoice, external_id)
        if record is None:
            counts["missing"] += 1
            continue
        with transaction.atomic():
            before = (
                Invoice.objects.filter(pk=invoice_id)
                .values_list("provider_status", flat=True)
                .first()
            )
            if before is None:
                counts["missing"] += 1
                continue
            changed = invoice_sync.record_provider_state(invoice_id, record)
            if changed:
                audit(
                    "invoice.provider_state_changed",
                    ENTITY_INVOICE,
                    invoice_id,
                    {name: values[0] for name, values in changed.items()},
                    {
                        **{name: values[1] for name, values in changed.items()},
                        "provider_invoice": mask_identifier(external_id),
                        "source": "reconciliation",
                    },
                )
            if "provider_status" in changed:
                ctx.mismatch(
                    counts,
                    MismatchKind.INVOICE_STATUS_DIFFERS,
                    ENTITY_INVOICE,
                    invoice_id,
                    f"Local status {_status(before)} was overwritten with the provider's "
                    f"{_status(record.raw_status)} (invoice {mask_identifier(external_id)}).",
                    resolved=True,
                )
            else:
                counts["matched"] += 1
    return counts


# --- (b) transactions ---------------------------------------------------------------------


def _drop_orphan_ref(external_id: str) -> None:
    """Remove the transaction reference of a payment row that no longer exists, so the
    handler can make the row again (the reference is unique per provider ID)."""
    ref = ExternalRef.objects.filter(
        provider=PROVIDER_KEY, external_kind=KIND_TRANSACTION, external_id=external_id
    ).first()
    if ref is None or ref.entity_type != ENTITY_PAYMENT:
        return
    if Payment.objects.filter(pk=ref.entity_id).exists():
        return
    ref.delete()
    audit(
        "external_ref.orphan_removed",
        ENTITY_PAYMENT,
        ref.entity_id,
        {"provider_transaction": mask_identifier(external_id)},
        None,
    )


def _apply_transaction(ctx: Context, record: TransactionRecord, counts: Counts) -> None:
    counts["checked"] += 1
    with transaction.atomic():
        existing = transaction_handler.local_payment(record.external_id)
        if existing is None:
            _drop_orphan_ref(record.external_id)
        transaction_handler.apply(record, settle=True)
        made = None if existing else transaction_handler.local_payment(record.external_id)
        if existing is not None:
            counts["matched"] += 1
        elif made is not None:
            counts["restored"] += 1
            ctx.mismatch(
                counts,
                MismatchKind.PAYMENT_RESTORED,
                ENTITY_PAYMENT,
                made.pk,
                f"No local payment held the provider's transaction "
                f"{mask_identifier(record.external_id)}; it was made from the transaction.",
            )
        else:
            counts["skipped"] += 1


def transactions(ctx: Context) -> Counts:
    """(b) List the window's transactions page by page and apply each through the
    webhook handler's own path; a payment we did not hold is made and logged."""
    counts: Counts = Counter()
    seen: set[str] = set()
    for page in range(1, MAX_TRANSACTION_PAGES + 1):
        records = ctx.provider.list_transactions(
            TransactionQuery(date_range=ctx.window, page=page, per_page=TRANSACTIONS_PER_PAGE)
        )
        counts["pages"] += 1
        fresh = [record for record in records if record.external_id not in seen]
        for record in fresh:
            seen.add(record.external_id)
            _apply_transaction(ctx, record, counts)
        if len(records) < TRANSACTIONS_PER_PAGE or not fresh:
            return counts
    raise PageLimitReached


# --- (c) deposits -------------------------------------------------------------------------


def _transaction_id(row: Summary) -> str:
    value = row.get(DEPOSIT_TRANSACTION_KEY)
    if isinstance(value, bool) or not isinstance(value, str | int):
        return ""
    return str(value).strip()


def _payment_of(external_id: str) -> Payment | None:
    ref = ExternalRef.objects.filter(
        provider=PROVIDER_KEY,
        external_kind=KIND_TRANSACTION,
        external_id=external_id,
        entity_type=ENTITY_PAYMENT,
        superseded_at__isnull=True,
    ).first()
    return Payment.objects.filter(pk=ref.entity_id).first() if ref is not None else None


def deposits(ctx: Context) -> Counts:
    """(c) Read each deposit of the window and match its rows to settled payments by the
    transaction ID; flag rows we cannot place, and settled payments no deposit holds.

    Unverified until live data (TODO-VERIFY-030, 060): the detail rows' shape."""
    counts: Counts = Counter()
    span = DateRange(ctx.today - timedelta(days=settings.RECONCILE_DEPOSIT_DAYS), ctx.today)
    deposited: set[uuid.UUID] = set()
    for deposit in ctx.provider.list_deposits(span):
        batch = mask_identifier(deposit.external_id)
        detail = ctx.provider.get_deposit(span, deposit.external_id)
        counts["deposits"] += 1
        for row in detail.items:
            external_id = _transaction_id(row)
            if not external_id:
                counts["unreadable"] += 1
                continue
            counts["checked"] += 1
            payment = _payment_of(external_id)
            if payment is None:
                ctx.mismatch(
                    counts,
                    MismatchKind.DEPOSIT_UNMATCHED,
                    mismatches.ENTITY_PROVIDER_TRANSACTION,
                    mismatches.provider_entity_id(KIND_TRANSACTION, external_id),
                    f"Deposit {batch} holds transaction {mask_identifier(external_id)}, "
                    "for which no local payment exists.",
                )
            elif payment.status != PaymentStatus.SETTLED:
                ctx.mismatch(
                    counts,
                    MismatchKind.DEPOSIT_UNMATCHED,
                    ENTITY_PAYMENT,
                    payment.pk,
                    f"Deposit {batch} holds transaction {mask_identifier(external_id)}, "
                    f"whose local payment is {payment.status}, not settled.",
                )
            else:
                counts["matched"] += 1
                deposited.add(payment.pk)
    undeposited = (
        Payment.objects.filter(
            status=PaymentStatus.SETTLED,
            method=PaymentMethod.ACH,
            is_refund=False,
            settled_on__gte=span.start,
            settled_on__lte=ctx.today - timedelta(days=UNDEPOSITED_AFTER_DAYS),
        )
        .filter(
            Exists(
                ExternalRef.objects.filter(
                    entity_type=ENTITY_PAYMENT,
                    entity_id=OuterRef("pk"),
                    external_kind=KIND_TRANSACTION,
                    superseded_at__isnull=True,
                )
            )
        )
        .exclude(pk__in=deposited)
        .order_by("settled_on", "pk")
        .values_list("pk", "settled_on")
    )
    for payment_id, settled_on in undeposited:
        if settled_on is None:  # excluded by the filter; for the type checker
            continue
        ctx.mismatch(
            counts,
            MismatchKind.SETTLED_PAYMENT_UNDEPOSITED,
            ENTITY_PAYMENT,
            payment_id,
            f"Settled on {settled_on.isoformat()} and in no deposit of the last "
            f"{settings.RECONCILE_DEPOSIT_DAYS} days.",
        )
    return counts


# --- (d) ACH rejections -------------------------------------------------------------------


def rejections(ctx: Context) -> Counts:
    """(d) List the window's ACH rejections; one whose payment is not returned here is
    applied through the transaction handler's return path and logged.

    TODO-VERIFY-061: a rejection's ``ach_reject_transaction_id`` is assumed to be the ID
    of the rejected transaction itself. A rejection that names no local payment is only
    counted: nothing is made from it."""
    counts: Counts = Counter()
    for rejection in ctx.provider.list_ach_rejections(ctx.window):
        counts["checked"] += 1
        external_id = rejection.external_id
        with transaction.atomic():
            payment = transaction_handler.local_payment(external_id)
            if payment is None:
                counts["unmatched"] += 1
                continue
            if payment.status == PaymentStatus.RETURNED:
                counts["matched"] += 1
                continue
            record = fetch(ctx.provider.get_transaction, external_id)
            if record is None:
                counts["unmatched"] += 1
                continue
            # The rejection list is the provider's own statement that the debit came
            # back, even when the re-fetched transaction does not show it yet.
            transaction_handler.apply(replace(record, returned=True), settle=False)
            payment.refresh_from_db()
            if payment.status != PaymentStatus.RETURNED:
                counts["unapplied"] += 1
                continue
            counts["restored"] += 1
            ctx.mismatch(
                counts,
                MismatchKind.RETURN_MISSED_BY_WEBHOOK,
                ENTITY_PAYMENT,
                payment.pk,
                f"The provider lists transaction {mask_identifier(external_id)} as an ACH "
                "rejection; no webhook had reported the return. It is now in the returns "
                "queue.",
            )
    return counts


# --- (e) pending payments -----------------------------------------------------------------


def business_days_between(start: date, end: date) -> int:
    """Monday-to-Friday days after ``start`` up to and including ``end`` (no holiday
    table); 0 when ``end`` is not after ``start``."""
    days = (end - start).days
    if days <= 0:
        return 0
    weeks, extra = divmod(days, 7)
    return weeks * 5 + sum(
        1 for offset in range(1, extra + 1) if (start + timedelta(days=offset)).weekday() < 5
    )


def pending(ctx: Context) -> Counts:
    """(e) Flag each pending payment initiated more than
    ``PENDING_PAYMENT_MAX_BUSINESS_DAYS`` US business days ago."""
    counts: Counts = Counter()
    limit = settings.PENDING_PAYMENT_MAX_BUSINESS_DAYS
    rows = (
        Payment.objects.filter(status=PaymentStatus.PENDING, initiated_on__isnull=False)
        .order_by("initiated_on", "pk")
        .values_list("pk", "initiated_on")
    )
    for payment_id, initiated_on in rows:
        if initiated_on is None:  # excluded by the filter; for the type checker
            continue
        counts["checked"] += 1
        age = business_days_between(initiated_on, ctx.today)
        if age <= limit:
            counts["matched"] += 1
            continue
        ctx.mismatch(
            counts,
            MismatchKind.PENDING_TOO_OLD,
            ENTITY_PAYMENT,
            payment_id,
            f"Pending since {initiated_on.isoformat()}: {age} business days, more than "
            f"the limit of {limit}.",
        )
    return counts


#: The steps in order, by letter (D7); (f) is the report, written by the run itself.
STEPS: tuple[tuple[str, Callable[[Context], Counts]], ...] = (
    ("a", invoices),
    ("b", transactions),
    ("c", deposits),
    ("d", rejections),
    ("e", pending),
)
