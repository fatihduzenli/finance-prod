"""What the detail family's cards read (docs/qa/PLAN-012f-a-design-follow-up.md D4; DESIGN-SPEC
§4 "Charge detail"): the charge, payment, invoice, lease and calendar entry pages.

Read only; nothing here writes or calls the provider.

:func:`activity` is the Activity card: the audit rows of the records a page names (the record
itself and the rows the page has already read, such as a charge's invoices and payments), newest
first, at most :data:`ACTIVITY_SIZE`, with one query, and whether older rows exist (the card then
links the store page's History tab). Each row carries its time, the action in words
(:func:`action_words`: "Charge created", "Invoice sent", "Payment recorded"), by whom and the
row's note when its ``after`` has one, masked as on the audit page.

:func:`invoice_rows` is the Invoices card: the charge's invoices
(:func:`ledger.services.rent_run.charge_invoices`) with their line count and the invoice
builder's draft that sent them (its page is the card's "view"), in the one query the page
already ran. :func:`one_off_source` and :func:`rent_source` name where a charge came from, from
fields that exist ("" when they do not say). :func:`header_line` joins the parts of a page's
muted line that exist. :func:`lease_open_balance` is the lease page's Open balance tile.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Count, OuterRef, Q, Subquery

from finance.dates import day_text
from ledger.models import (
    AuditLog,
    DepositResolution,
    Invoice,
    InvoiceDraft,
    Lease,
    OneOffCategory,
    OneOffCharge,
    RentCharge,
)
from ledger.services import audit, one_off_charges, receivables, rent_run

#: The newest rows the Activity card shows; older ones are on the store page's History tab.
ACTIVITY_SIZE = 20

#: The longest memo an invoice's subline shows; a longer one ends in an ellipsis.
MEMO_SHOWN = 40

#: The record a row is about, as the Activity card names it (the first word of the action).
_NOUNS: dict[str, str] = {
    "one_off_charge": "Charge",
    "rent_charge": "Charge",
    "insurance_charge": "Charge",
    "invoice": "Invoice",
    "invoice_draft": "Invoice",
    "payment": "Payment",
    "lease": "Lease",
    "collection_schedule": "Autopay",
    "calendar_entry": "Calendar entry",
    "installment_plan": "Installment plan",
    "contact_entry": "Contact",
}

SOURCE_SPREADSHEET = "the spreadsheet"
SOURCE_PLAN = "an installment plan"
SOURCE_RETURN = "a returned payment"
SOURCE_BUILDER = "the invoice builder"


@dataclass(frozen=True, slots=True)
class ActivityRow:
    at: datetime
    action: str
    user: str
    note: str


@dataclass(frozen=True, slots=True)
class Activity:
    rows: list[ActivityRow]
    #: True when older rows exist than those shown.
    more: bool


def action_words(action: str) -> str:
    """``one_off_charge.payment_recorded`` as "Charge payment recorded": the record's noun,
    then the action's words."""
    kind, _, verb = action.partition(".")
    noun = _NOUNS.get(kind, kind.replace("_", " ").strip().capitalize())
    words = verb.replace("_", " ").replace(".", " ").strip()
    return f"{noun} {words}".strip() if words else noun


def _note(after: object) -> str:
    if not isinstance(after, dict):
        return ""
    note = after.get("note")
    if not isinstance(note, str) or not note.strip():
        return ""
    shown = audit.masked(note.strip())
    return str(shown)


def activity(records: Iterable[tuple[str, object]]) -> Activity:
    """The audit rows of ``records`` (``(entity type, id)`` pairs), newest first, at most
    :data:`ACTIVITY_SIZE` (one query; none when ``records`` is empty)."""
    ids: dict[str, list[str]] = {}
    for kind, pk in records:
        ids.setdefault(kind, []).append(str(pk))
    if not ids:
        return Activity([], False)
    condition = Q()
    for kind, values in ids.items():
        condition |= Q(entity_type=kind, entity_id__in=values)
    found = list(
        AuditLog.objects.filter(condition)
        .select_related("actor")
        .order_by("-at", "-id")[: ACTIVITY_SIZE + 1]
    )
    return Activity(
        [
            ActivityRow(
                at=row.at,
                action=action_words(row.action),
                user=row.actor.get_username() if row.actor is not None else "",
                note=_note(row.after),
            )
            for row in found[:ACTIVITY_SIZE]
        ],
        len(found) > ACTIVITY_SIZE,
    )


@dataclass(frozen=True, slots=True)
class InvoiceRow:
    invoice: Invoice
    #: "move-in · 2 lines": the invoice's memo (else ``kind``) and its line count.
    subline: str
    #: The invoice builder draft that sent the invoice (its page), or ``None``.
    draft_id: uuid.UUID | None


def _lines(count: int) -> str:
    if count <= 0:
        return ""
    return "1 line" if count == 1 else f"{count} lines"


def _short(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= MEMO_SHOWN else line[: MEMO_SHOWN - 1].rstrip() + "…"


def invoice_rows(charge: OneOffCharge | RentCharge, kind: str) -> list[InvoiceRow]:
    """The charge's invoices (:func:`ledger.services.rent_run.charge_invoices`, one query)
    with their subline (the memo, else ``kind``, and the line count) and the builder draft
    that sent each."""
    found = rent_run.charge_invoices(charge).annotate(
        line_count=Count("line_items", distinct=True),
        draft_pk=Subquery(
            InvoiceDraft.objects.filter(sent_invoice=OuterRef("pk")).values("pk")[:1]
        ),
    )
    return [
        InvoiceRow(
            invoice=invoice,
            subline=" · ".join(
                part for part in (_short(invoice.memo) or kind, _lines(invoice.line_count)) if part
            ),
            draft_id=invoice.draft_pk,
        )
        for invoice in found
    ]


def kind_words(charge: OneOffCharge, resolution: DepositResolution | None) -> str:
    """The one-off charge's kind badge: "Deposit · held, refundable" for a refundable fee
    type (what was collected is held until it is resolved; "Deposit · refundable" while
    nothing is collected; "Deposit · refunded" and the like once resolved), else its
    category ("Damage", "Setup")."""
    if charge.fee_type is not None and charge.fee_type.refundable:
        if resolution is not None:
            return f"Deposit · {resolution.get_kind_display().lower()}"
        if charge.amount_paid > 0:
            return "Deposit · held, refundable"
        return "Deposit · refundable"
    return one_off_charges.kind_label(charge)


def one_off_source(charge: OneOffCharge, invoices: Sequence[InvoiceRow]) -> str:
    """Where a one-off charge came from, as far as its fields say; ``""`` otherwise."""
    if charge.legacy:
        return SOURCE_SPREADSHEET
    if charge.category == OneOffCategory.INSTALLMENT:
        return SOURCE_PLAN
    if charge.source_rent_charge is not None:
        # PLAN-012f-b D19: a rent charge's bank debit from Add payment, not a return.
        return one_off_charges.rent_debit_description(charge.source_rent_charge)
    if charge.category in (OneOffCategory.REDEBIT, OneOffCategory.NSF_FEE):
        return SOURCE_RETURN
    if any(row.draft_id is not None for row in invoices):
        return SOURCE_BUILDER
    return ""


def rent_source(charge: RentCharge) -> str:
    """Where a rent charge came from, as far as its fields say; ``""`` otherwise."""
    if charge.legacy:
        return SOURCE_SPREADSHEET
    if charge.billed_by_invoice_id is not None:
        return SOURCE_BUILDER
    return ""


def header_line(
    *,
    store: str = "",
    operator: str = "",
    created: datetime | date | None = None,
    source: str = "",
    due: date | None = None,
) -> str:
    """ "#1991 Istanbul · fatih · created Oct 14, 2026 from the lease checklist · due Nov 1,
    2026": the parts that exist, joined by " · "."""
    made = ""
    if created is not None:
        made = f"created {day_text(created)}" + (f" from {source}" if source else "")
    parts = (store, operator, made, f"due {day_text(due)}" if due is not None else "")
    return " · ".join(part for part in parts if part)


def lease_open_balance(lease: Lease, today: date) -> Decimal | None:
    """What the lease's open charges still owe, as the receivables read them
    (:func:`ledger.services.receivables.open_balances` of its operator, the lease's rows);
    ``None`` for a lease without an operator."""
    if lease.operator_id is None:
        return None
    rows = receivables.open_balances(today, operator=lease.operator_id)
    return sum((row.balance for row in rows if row.lease_id == lease.pk), Decimal("0.00"))
