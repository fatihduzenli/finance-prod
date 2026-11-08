"""The move-in invoice of a new lease (docs/qa/PLAN-014b item 1, Fable's decision 6).

When the New lease page saves an ``active`` lease with an operator, or a ``prospective`` one
(a start after today, PLAN-015b §9.1; the owner's option (a), 2026-11-07),
:func:`create_draft` makes one invoice-builder **draft** (local only; nothing reaches the
payment provider until staff send it): one Rent line for the start month (``rent_lease``,
``rent_month``; its amount left to the builder, which takes the rent run's charge's when the
run or the lease's catch-up already made it, else :func:`ledger.services.rent_run.first_month`'s,
prorated for a partial first month) and one existing-charge line per setup charge the page
created. Its one-time key is the form's creation key with :data:`KEY_SUFFIX`, so a replayed
form finds the same draft.

**How the rent run recognises the Rent line.** The run's "already created" rule is
``rent_charges``' uniqueness on ``(lease, period_month, seq)``
(:func:`ledger.services.rent_run.create_month`, ``get_or_create``). A draft's Rent line holds
no charge; sending the draft makes the start month's charge through the same ``create_month``
(or takes the one the run made meanwhile, unpaid and at the line's amount) and marks it
``billed_by_invoice``. The run then finds that charge and counts it as already created; a run
before the send makes the charge itself and the send takes it. Either way the month has one
charge and is billed once. A prospective lease is the same: the run skips it until the daily
job (:func:`ledger.services.leases.activate_due`) or staff's "Activate now" makes it active,
and the catch-up that follows leaves a month that has its charge as it is.

The draft cannot be made, and the lease stays saved, when the lease has no operator or no
start date, or the builder refuses a line (a lease debited twice a month: its months are the
rent run's): :func:`create_draft` returns the reason as one sentence.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ledger.models import (
    AuditLog,
    CollectionMethod,
    CollectionStatus,
    Invoice,
    InvoiceDraft,
    InvoiceDraftStatus,
    InvoiceStatus,
    Lease,
    OneOffCharge,
    OneOffChargeStatus,
    Payment,
    PaymentStatus,
    RentCharge,
)
from ledger.services import invoice_builder
from payments import jobs
from payments import one_off_invoices as one_off_invoices_params
from payments.models import Job, JobKind

#: Appended to the New lease form's creation key for the draft's own key (uuid5).
KEY_SUFFIX = "move-in-invoice"
MEMO = "Move-in invoice"
NO_OPERATOR = "The lease has no operator, so no move-in invoice was made."
NOT_ACTIVE = "The lease is not active, so no move-in invoice was made."
#: The statuses a move-in draft is made for (a paused or ended lease gets none).
DRAFTED_STATUSES = frozenset({CollectionStatus.ACTIVE, CollectionStatus.PROSPECTIVE})
NO_START = "The lease has no start date, so no move-in invoice was made."
REFUSED = "The move-in invoice could not be made: {reason}"
_LINE_PREFIX = re.compile(r"\ALine \d+: ")


@dataclass(frozen=True, slots=True)
class MoveIn:
    """The draft made, or why none was made (one sentence)."""

    draft: InvoiceDraft | None
    message: str


def draft_key(creation_key: uuid.UUID) -> uuid.UUID:
    """The draft's one-time key: the lease form's key with :data:`KEY_SUFFIX`."""
    return uuid.uuid5(creation_key, KEY_SUFFIX)


def create_draft(
    lease: Lease,
    charges: Sequence[OneOffCharge],
    *,
    creation_key: uuid.UUID,
    actor: User | None,
    today: date | None = None,
) -> MoveIn:
    """The move-in draft of ``lease`` (see the module docstring): due on the start date, or
    today when the lease started earlier (the builder refuses a due date in the past)."""
    operator = lease.operator
    if operator is None:
        return MoveIn(None, NO_OPERATOR)
    if lease.collection_status not in DRAFTED_STATUSES:
        return MoveIn(None, NOT_ACTIVE)
    start = lease.started_on
    if start is None:
        return MoveIn(None, NO_START)
    day = today or timezone.localdate()
    lines = [
        invoice_builder.LineInput(
            "", "", None, None, rent_lease=lease.pk, rent_month=start.replace(day=1)
        ),
        *(
            invoice_builder.LineInput("", "", None, None, existing_charge=charge.pk)
            for charge in charges
        ),
    ]
    outcome = invoice_builder.create_draft(
        operator,
        lease,
        MEMO,
        max(start, day),
        lines,
        actor,
        today=day,
        creation_key=draft_key(creation_key),
    )
    if not outcome.ok or outcome.draft is None:
        reason = _LINE_PREFIX.sub("", outcome.message)
        return MoveIn(None, REFUSED.format(reason=reason[:1].lower() + reason[1:]))
    return MoveIn(outcome.draft, outcome.message)


# --- Rebuild and void (docs/qa/15/PLAN-015d §3 (1)) ----------------------------------------

#: What becomes of the rent charges of a voided invoice: void (never due), or following the
#: lease's start as it is now (a month before the start month void, the start month re-priced).
RENT_VOID = "void"
RENT_FOLLOW_START = "follow_start"
VOIDED = "Invoice voided."
REDRAFTED = "Invoice voided and a new move-in draft prepared."
NO_DRAFT = "The lease has no move-in draft to rebuild."
VOID_REQUESTED_ACTION = "invoice.void_requested"
VOIDED_ACTION = "invoice.voided"
DETACHED_ACTION = "one_off_charge.detached_from_invoice"
VOID_NOTE = "Invoice {number} voided."


def current_draft(lease: Lease) -> InvoiceDraft | None:
    """The lease's move-in draft not sent yet (the latest), or ``None``."""
    return (
        InvoiceDraft.objects.filter(lease=lease, memo=MEMO, status=InvoiceDraftStatus.DRAFT)
        .filter(lines__rent_lease=lease)
        .order_by("-created_at")
        .first()
    )


def sent_invoice(lease: Lease) -> Invoice | None:
    """The lease's sent move-in invoice that is not void (the latest), or ``None``."""
    draft = (
        InvoiceDraft.objects.filter(lease=lease, memo=MEMO, status=InvoiceDraftStatus.SENT)
        .exclude(sent_invoice__internal_status=InvoiceStatus.VOID)
        .select_related("sent_invoice")
        .order_by("-created_at")
        .first()
    )
    return draft.sent_invoice if draft is not None else None


def invoice_charges(invoice: Invoice) -> tuple[list[RentCharge], list[OneOffCharge]]:
    """The rent and one-off charges of the invoice's lines, in line order."""
    rents: list[RentCharge] = []
    one_offs: list[OneOffCharge] = []
    for item in invoice.line_items.select_related("rent_charge", "one_off_charge").order_by(
        "position"
    ):
        if item.rent_charge is not None:
            rents.append(item.rent_charge)
        if item.one_off_charge is not None:
            one_offs.append(item.one_off_charge)
    return rents, one_offs


def invoice_paid(invoice: Invoice) -> bool:
    """Whether anything was paid, or is pending, on the invoice or one of its charges."""
    rents, one_offs = invoice_charges(invoice)
    return (
        Payment.objects.filter(is_refund=False)
        .filter(status__in=[PaymentStatus.PENDING, PaymentStatus.SETTLED])
        .filter(
            Q(invoice=invoice)
            | Q(rent_charge__in=[charge.pk for charge in rents])
            | Q(one_off_charge__in=[charge.pk for charge in one_offs])
        )
        .exists()
    )


def rebuild_draft(lease: Lease, actor: User | None, *, today: date | None = None) -> MoveIn:
    """The lease's move-in draft made again for its start date as it is now (PLAN-015d §3
    (1)): the unsent draft is deleted (audited with ``"rebuilt": true``) and a new one made by
    :func:`create_draft` with a new one-time key, its existing-charge lines kept. Local only."""
    with transaction.atomic():
        old = current_draft(lease)
        if old is None:
            return MoveIn(None, NO_DRAFT)
        charges = [
            line.existing_charge
            for line in invoice_builder.lines_of(old)
            if line.existing_charge is not None
        ]
        invoice_builder.discard_draft(old, actor, {"rebuilt": True})
        made = create_draft(lease, charges, creation_key=uuid.uuid4(), actor=actor, today=today)
        if made.draft is None:
            transaction.set_rollback(True)
    return made


def request_void(invoice: Invoice, actor: User | None, *, rent: str, redraft: bool) -> Job:
    """Queue the ``invoice_void`` job of a sent, unpaid invoice (the worker deletes it at the
    provider; :func:`after_void` follows), audited :data:`VOID_REQUESTED_ACTION`."""
    rents, one_offs = invoice_charges(invoice)
    entity: OneOffCharge | RentCharge = (
        invoice.one_off_charge
        or (one_offs[0] if one_offs else None)
        or invoice.rent_charge
        or rents[0]
    )
    job = jobs.enqueue(
        JobKind.INVOICE_VOID,
        entity,
        {
            one_off_invoices_params.VOID_INVOICE_PARAM: str(invoice.pk),
            one_off_invoices_params.VOID_RENT_PARAM: rent,
            one_off_invoices_params.VOID_REDRAFT_PARAM: redraft,
        },
        actor,
    )
    AuditLog.objects.create(
        actor=actor,
        action=VOID_REQUESTED_ACTION,
        entity_type="invoice",
        entity_id=str(invoice.pk),
        before=None,
        after={"invoice_number": invoice.invoice_number, "rent": rent, "redraft": redraft},
    )
    return job


@dataclass(frozen=True, slots=True)
class Voided:
    ok: bool
    message: str
    draft: InvoiceDraft | None = None


def after_void(
    invoice: Invoice,
    actor: User | None,
    *,
    rent: str,
    redraft: bool,
    context: dict[str, object],
    today: date | None = None,
) -> Voided:
    """The ledger's side of an invoice voided at the provider (the worker, in its
    transaction): the invoice ``void``; each existing charge attached to it (a setup charge,
    the deposit) detached, its collection as it was before, so a new draft can take it; any
    other one-off line voided; each rent charge void (:data:`RENT_VOID`) or following the
    lease's start (:data:`RENT_FOLLOW_START`: before the start month void, the start month
    re-priced, a later month priced in full); ``billed_by_invoice`` is kept as history.
    With ``redraft`` a new move-in draft is made for the lease (its Rent line and the
    detached charges)."""
    from ledger.services import rent_run

    now = timezone.now()
    Invoice.objects.filter(pk=invoice.pk).update(internal_status=InvoiceStatus.VOID, updated_at=now)
    AuditLog.objects.create(
        actor=actor,
        action=VOIDED_ACTION,
        entity_type="invoice",
        entity_id=str(invoice.pk),
        before={"internal_status": invoice.internal_status},
        after={
            "internal_status": InvoiceStatus.VOID,
            "invoice_number": invoice.invoice_number,
            **context,
        },
    )
    rents, one_offs = invoice_charges(invoice)
    detached: list[OneOffCharge] = []
    note = VOID_NOTE.format(number=invoice.invoice_number)
    for charge in one_offs:
        attached = (
            AuditLog.objects.filter(
                action=invoice_builder.ATTACHED_ACTION,
                entity_id=str(charge.pk),
                after__invoice_number=invoice.invoice_number,
            )
            .order_by("-at")
            .first()
        )
        locked = OneOffCharge.objects.select_for_update().get(pk=charge.pk)
        if attached is not None and isinstance(attached.before, dict):
            before = {"collection": locked.collection}
            locked.collection = attached.before.get("collection", CollectionMethod.NONE)
            locked.save(update_fields=["collection", "updated_at"])
            AuditLog.objects.create(
                actor=actor,
                action=DETACHED_ACTION,
                entity_type="one_off_charge",
                entity_id=str(locked.pk),
                before=before,
                after={"collection": locked.collection, "invoice_number": invoice.invoice_number},
            )
            detached.append(locked)
        elif locked.status not in (OneOffChargeStatus.VOIDED, OneOffChargeStatus.WAIVED):
            before = {"status": locked.status}
            locked.status = OneOffChargeStatus.VOIDED
            locked.voided_at = now
            locked.voided_by = actor
            locked.void_note = note
            locked.save(
                update_fields=["status", "voided_at", "voided_by", "void_note", "updated_at"]
            )
            AuditLog.objects.create(
                actor=actor,
                action="one_off_charge.voided",
                entity_type="one_off_charge",
                entity_id=str(locked.pk),
                before=before,
                after={"status": locked.status, "note": note, **context},
            )
    lease: Lease | None = None
    for rent_charge in rents:
        lease = rent_charge.lease
        started = lease.started_on
        start = started.replace(day=1) if started is not None else None
        words: dict[str, Any] = {"invoice_number": invoice.invoice_number}
        if rent == RENT_FOLLOW_START and start is not None and rent_charge.period_month >= start:
            # The new start month re-priced; a month after it (the old start month of a
            # start moved earlier) becomes a full month.
            rent_run.reamount_start_month(rent_charge, actor, words)
        else:
            rent_run.void_charge(rent_charge, actor, words)
    if lease is None:
        draft = InvoiceDraft.objects.filter(sent_invoice=invoice).select_related("lease").first()
        lease = draft.lease if draft is not None else None
    if not redraft or lease is None:
        return Voided(True, VOIDED)
    made = create_draft(lease, detached, creation_key=uuid.uuid4(), actor=actor, today=today)
    return Voided(True, REDRAFTED if made.draft is not None else made.message, made.draft)
