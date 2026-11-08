"""Transaction events: the payment state machine (PLAN-008 D4, D5, D12).

The transaction is re-fetched; only an ACH transaction is applied (a card transaction is
``ignored: card``). Its invoice, named by the transaction, is re-fetched and mirrored
too, and the payment is linked to it and to the invoice's charge. The payment is found
or made by its ``external_refs`` row (``external_kind = transaction``).

States, from the re-fetched transaction only:

- a new payment is ``pending``; a pending payment puts its charge in ``pending``;
- ``update_transaction_settled`` with the transaction settled: ``settled``, and only
  then is the charge paid (``paid`` or ``partial``, recomputed from settled payments);
- the transaction returned (on any event, as a return never pays anything):
  ``returned`` with its code and reason, the charge reopened (``unpaid`` or
  ``partial``) and flagged for review, and the payment enters the returns queue;
- the transaction voided: ``voided``;
- a refund (a transaction of type refund naming its parent) is a payment of its own,
  ``is_refund``, linked to the parent's payment, charge and invoice. A refund an admin
  made here (PLAN-009 D6, :func:`record_refund`) whose provider ID was not known at the
  time is recorded without one; the first event of a refund of that parent and amount
  gives that row its ID instead of making a second row.

A transaction for an invoice linked to a one-off charge is a payment of kind ``one_off``
on that charge (PLAN-010b D6, PLAN-010d D7); it pays, reopens and flags that charge as a
rent payment does its rent charge. On a re-debit's invoice (PLAN-011b D5) the payment pays
the re-debit first and what is left pays its NSF fee, and on a multi-line invoice of the
invoice builder (PLAN-012 D6) it pays the lines' charges in line order
(:func:`ledger.services.one_off_charges.refresh_companions`), each audited; and the
re-debit's part is paid on the charge the returned payment was for, which reopens again
when that payment is returned, refunded or voided
(:func:`ledger.services.one_off_charges.refresh_sources`), each audited.

A payment that settles keeps the operator's open promises to pay it meets
(:func:`ledger.services.contacts.close_kept_by_payment`, PLAN-012 D4). A returned debit
gets, in the return's transaction, its NSF fee, its automatic re-debit (R01, R09) or the
lease's Needs review flag, and template F (:func:`ledger.services.nsf.on_return`, PLAN-014
F2); a settled automatic re-debit resolves the return it collected again
(:func:`ledger.services.nsf.on_settled`). A void never messages the operator.

A returned debit whose money staff kept as a credit (PLAN-015d's "Keep as credit", PLAN-015e)
reverses that credit (:func:`ledger.services.credits.reverse_from_return`): the payment held as
the credit (the row the transaction names, re-pointed whole, or a row split off it, which names
it in ``split_from`` and is returned with it, resolved at once) has no charge, so its return
reaches the credit; the credit payments that applied it are returned too and their charges
owed again. A returned payment with neither a charge nor a credit is logged and audited
``payment.return_unplaced``. An event that shows a payment returned already returns what its
first return could not reach (a row split off it before ledger 0077 linked it, a credit not
reversed yet); nothing else.

``returned`` and ``voided`` are final. Every write is keyed on provider IDs and every
recomputation is pure, so a repeated or replayed event changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.db.models import Exists, OuterRef
from django.utils import timezone

import payments
from ledger.models import (
    InsuranceCharge,
    Invoice,
    OneOffCharge,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    ResolutionKind,
)
from ledger.services import contacts, credits, one_off_charges, rent_run
from payments.models import ExternalRef, WebhookOutcome
from payments.provider import (
    METHOD_BANK,
    TRANSACTION_REFUND,
    TRANSACTION_VOID,
    TransactionRecord,
    mask_identifier,
)
from payments.webhooks.events import ignored
from payments.webhooks.handlers.common import (
    ENTITY_CHARGE,
    ENTITY_INSURANCE_CHARGE,
    ENTITY_INVOICE,
    ENTITY_ONE_OFF_CHARGE,
    ENTITY_PAYMENT,
    KIND_INVOICE,
    KIND_TRANSACTION,
    NO_OBJECT_ID,
    NOT_AT_PROVIDER,
    add_ref,
    audit,
    current_ref,
    entity_for,
    fetch,
    json_summary,
)
from payments.webhooks.handlers.invoice import customer_lease, sync_invoice, usable_total

SETTLED_EVENT = "update_transaction_settled"
METHOD_CARD = "card"

CARD = ignored("card")
NOT_ACH = ignored("not ACH")
NO_TOTAL = ignored("no usable total")
UNSUCCESSFUL = ignored("unsuccessful")
NO_PARENT = ignored("refund parent unknown")
OTHER_OPERATOR = ignored("customer mismatch")
NOT_SETTLED = ignored("not settled yet")

_FINAL = (PaymentStatus.RETURNED, PaymentStatus.VOIDED)
UNPLACED_ACTION = "payment.return_unplaced"
#: PLAN-015e: a row split off a returned payment is returned with it and resolved at once;
#: the payment the transaction names carries the return in the returns queue.
SPLIT_RESOLVED = (
    "Returned with the payment it was split from (the same bank debit); see that return."
)

logger = logging.getLogger(__name__)


def handle(event_type: str, provider_object_id: str) -> str:
    if not provider_object_id:
        return NO_OBJECT_ID
    record = fetch(payments.get_provider().get_transaction, provider_object_id)
    if record is None:
        return NOT_AT_PROVIDER
    return apply(record, settle=event_type == SETTLED_EVENT)


def local_payment(external_id: str) -> Payment | None:
    ref = entity_for(KIND_TRANSACTION, external_id)
    if ref is None or ref.entity_type != ENTITY_PAYMENT:
        return None
    return Payment.objects.select_for_update().filter(pk=ref.entity_id).first()


def apply(record: TransactionRecord, *, settle: bool, parent_lookup: bool = True) -> str:
    """Apply one re-fetched transaction; the outcome to store."""
    if record.method != METHOD_BANK:
        return CARD if record.method == METHOD_CARD else NOT_ACH
    parent: Payment | None = None
    if record.kind == TRANSACTION_REFUND:
        parent = _parent(record, parent_lookup)
        if parent is None:
            return NO_PARENT
    invoice_id = record.invoice_external_id or (
        current_ref(ENTITY_INVOICE, parent.invoice_id, KIND_INVOICE)
        if parent is not None and parent.invoice_id is not None
        else ""
    )
    invoice: Invoice | None = None
    if invoice_id:
        _, invoice = sync_invoice(invoice_id)
    if invoice is not None and record.customer_external_id:
        lease = customer_lease(record.customer_external_id)
        if lease is None or lease.operator_id != invoice.operator_id:
            return OTHER_OPERATOR
    payment = local_payment(record.external_id)
    if payment is None and parent is not None:
        payment = _unlinked_refund(parent, record)
    if payment is None:
        if record.success is False and not record.returned:
            return UNSUCCESSFUL
        total = record.total
        if total is None or total <= 0 or not usable_total(total):
            return NO_TOTAL
        payment = _create(record, total, invoice, parent)
    elif payment.invoice_id is None and invoice is not None and parent is None:
        _link(payment, invoice)
    return _transition(payment, record, settle=settle)


def _parent(record: TransactionRecord, lookup: bool) -> Payment | None:
    """The refunded payment; its transaction is applied first when we have no row."""
    if not record.parent_external_id:
        return None
    parent = local_payment(record.parent_external_id)
    if parent is None and lookup:
        parent_record = fetch(payments.get_provider().get_transaction, record.parent_external_id)
        if parent_record is not None and parent_record.kind != TRANSACTION_REFUND:
            apply(parent_record, settle=False, parent_lookup=False)
            parent = local_payment(record.parent_external_id)
    return parent


def _unlinked_refund(parent: Payment, record: TransactionRecord) -> Payment | None:
    """A refund row of ``parent`` with this amount and no provider ID yet, given the ID.

    Such a row is written by :func:`record_refund` when the provider's answer to the
    refund did not name the new transaction; the oldest one is taken.
    """
    if record.total is None:
        return None
    payment = (
        Payment.objects.select_for_update()
        .filter(parent_payment=parent, is_refund=True, amount=record.total)
        .exclude(status=PaymentStatus.VOIDED)
        .exclude(
            Exists(
                ExternalRef.objects.filter(
                    entity_type=ENTITY_PAYMENT,
                    entity_id=OuterRef("pk"),
                    external_kind=KIND_TRANSACTION,
                    superseded_at__isnull=True,
                )
            )
        )
        .order_by("created_at", "pk")
        .first()
    )
    if payment is None:
        return None
    add_ref(ENTITY_PAYMENT, payment.pk, KIND_TRANSACTION, record.external_id)
    audit(
        "payment.provider_id_recorded",
        ENTITY_PAYMENT,
        payment.pk,
        None,
        {**_state(payment), "provider_transaction": mask_identifier(record.external_id)},
    )
    return payment


def record_refund(parent: Payment, amount: Decimal, record: TransactionRecord | None) -> Payment:
    """The refund row of a refund an admin made of ``parent`` (PLAN-009 D6).

    ``record`` is the re-fetched refund transaction when the provider named it: the row
    is made from it as an event would make it (or is the row an event already made),
    then moved to the state it shows. Without it, the row is made from the request,
    ``pending`` and with no provider ID; the refund's first event gives it its ID
    (:func:`_unlinked_refund`). The caller holds the row locks.
    """
    if record is not None:
        payment = local_payment(record.external_id)
        if payment is None:
            total = record.total
            usable = total is not None and total > 0 and usable_total(total)
            payment = _create(record, total if usable and total else amount, None, parent)
        _transition(payment, record, settle=True)
        return payment
    payment = Payment.objects.create(
        invoice=parent.invoice,
        method=PaymentMethod.ACH,
        amount=amount,
        status=PaymentStatus.PENDING,
        is_refund=True,
        parent_payment=parent,
        initiated_on=timezone.localdate(),
        kind=parent.kind,
        policy_year=parent.policy_year,
        rent_charge_id=parent.rent_charge_id,
        insurance_charge_id=parent.insurance_charge_id,
        charge_adjustment_id=parent.charge_adjustment_id,
        one_off_charge_id=parent.one_off_charge_id,
    )
    audit("payment.created_from_request", ENTITY_PAYMENT, payment.pk, None, _state(payment))
    _refresh_charge(payment, returned=False)
    return payment


def record_void(payment: Payment, record: TransactionRecord) -> None:
    """The void an admin made of ``payment`` (PLAN-009 D6): the provider accepted it, so
    the payment is ``voided`` even when ``record`` (the re-fetch, or the provider's
    answer when the re-fetch failed) does not show it yet. No new row: a void is not a
    new transaction. The caller holds the row locks."""
    _transition(payment, replace(record, is_voided=True), settle=False)


def _kind(invoice: Invoice | None) -> dict[str, Any]:
    """The charge fields of a payment made against ``invoice``."""
    if invoice is None:
        return {"kind": PaymentKind.RENT, "policy_year": None}
    if invoice.insurance_charge is not None:
        return {
            "kind": PaymentKind.INSURANCE,
            "policy_year": invoice.insurance_charge.policy_year,
            "insurance_charge": invoice.insurance_charge,
        }
    if invoice.charge_adjustment_id is not None:
        return {
            "kind": PaymentKind.ADJUSTMENT,
            "policy_year": None,
            "charge_adjustment_id": invoice.charge_adjustment_id,
        }
    if invoice.one_off_charge_id is not None:
        return {
            "kind": PaymentKind.ONE_OFF,
            "policy_year": None,
            "one_off_charge_id": invoice.one_off_charge_id,
        }
    return {"kind": PaymentKind.RENT, "policy_year": None, "rent_charge": invoice.rent_charge}


def _local_date(moment: datetime | None) -> date:
    return timezone.localdate(moment) if moment is not None else timezone.localdate()


def _create(
    record: TransactionRecord, total: Decimal, invoice: Invoice | None, parent: Payment | None
) -> Payment:
    if parent is not None:
        charges: dict[str, Any] = {
            "kind": parent.kind,
            "policy_year": parent.policy_year,
            "rent_charge_id": parent.rent_charge_id,
            "insurance_charge_id": parent.insurance_charge_id,
            "charge_adjustment_id": parent.charge_adjustment_id,
            "one_off_charge_id": parent.one_off_charge_id,
        }
        invoice = invoice or parent.invoice
    else:
        charges = _kind(invoice)
    payment = Payment.objects.create(
        invoice=invoice,
        method=PaymentMethod.ACH,
        amount=total,
        status=PaymentStatus.PENDING,
        is_refund=parent is not None,
        parent_payment=parent,
        initiated_on=_local_date(record.created_at or record.fetched_at),
        provider_created_at=record.created_at,
        raw_summary=json_summary(record.raw_summary),
        **charges,
    )
    add_ref(ENTITY_PAYMENT, payment.pk, KIND_TRANSACTION, record.external_id)
    audit(
        "payment.created_from_provider",
        ENTITY_PAYMENT,
        payment.pk,
        None,
        {**_state(payment), "provider_transaction": mask_identifier(record.external_id)},
    )
    _refresh_charge(payment, returned=False)
    return payment


def _link(payment: Payment, invoice: Invoice) -> None:
    before = _state(payment)
    for name, value in _kind(invoice).items():
        setattr(payment, name, value)
    payment.invoice = invoice
    payment.save()
    audit("payment.linked", ENTITY_PAYMENT, payment.pk, before, _state(payment))
    _refresh_charge(payment, returned=False)


def _target(payment: Payment, record: TransactionRecord, *, settle: bool) -> PaymentStatus | None:
    """The state the re-fetched transaction moves the payment to, or ``None``."""
    current = PaymentStatus(payment.status)
    if current in _FINAL:
        return None
    if record.is_voided or record.kind == TRANSACTION_VOID:
        return PaymentStatus.VOIDED
    if record.returned:
        return PaymentStatus.RETURNED
    if settle and record.settled_at is not None and current == PaymentStatus.PENDING:
        return PaymentStatus.SETTLED
    return None


def _transition(payment: Payment, record: TransactionRecord, *, settle: bool) -> str:
    target = _target(payment, record, settle=settle)
    if target is None:
        if record.returned and payment.status == PaymentStatus.RETURNED:
            missed = _return_missed(payment, record)
            if missed:
                from ledger.services import nsf

                nsf.on_credit_return(payment, missed, timezone.localdate())
        not_yet = (
            settle
            and payment.status == PaymentStatus.PENDING
            and record.settled_at is None
            and not record.returned
        )
        return NOT_SETTLED if not_yet else WebhookOutcome.PROCESSED
    before = _state(payment)
    payment.status = target
    if target == PaymentStatus.SETTLED:
        payment.settled_on = _local_date(record.settled_at)
    elif target == PaymentStatus.RETURNED:
        payment.returned_at = timezone.now()
        payment.return_code = record.return_code
        payment.return_reason = record.return_reason
    payment.save()
    audit(
        "payment.status_changed",
        ENTITY_PAYMENT,
        payment.pk,
        before,
        {**_state(payment), "provider_transaction": mask_identifier(record.external_id)},
    )
    reversal = _refresh_charge(payment, returned=target == PaymentStatus.RETURNED)
    # Imported here: nsf reaches the messaging engine, whose imports reach this module.
    from ledger.services import nsf

    if target == PaymentStatus.RETURNED:
        # PLAN-015e: the rows split off it are the same bank debit.
        reversals = [reversal] if reversal is not None else []
        reversals += _return_split_rows(payment, record)
        # PLAN-014 F2: the NSF fee, the automatic re-debit or Needs review, and template F.
        nsf.on_return(payment, timezone.localdate())
        if reversals:
            # PLAN-015e: the credit's lease flagged, the managers and the operator told.
            nsf.on_credit_return(payment, reversals, timezone.localdate())
    if target == PaymentStatus.SETTLED:
        contacts.close_kept_by_payment(payment)
        nsf.on_settled(payment)
    return WebhookOutcome.PROCESSED


def _return_split_rows(payment: Payment, record: TransactionRecord) -> list[credits.CreditReversal]:
    """Return the rows split off the returned ``payment`` (PLAN-015e: held as a credit, so
    each reverses its credit), with the payment's return code and reason, resolved at once
    (:data:`SPLIT_RESOLVED`), each audited; the credits reversed."""
    reversals: list[credits.CreditReversal] = []
    rows = (
        Payment.objects.select_for_update()
        .filter(split_from=payment)
        .exclude(status__in=_FINAL)
        .order_by("created_at", "id")
    )
    for row in rows:
        before = _state(row)
        moment = timezone.now()
        row.status = PaymentStatus.RETURNED
        row.returned_at = payment.returned_at or moment
        row.return_code = payment.return_code or record.return_code
        row.return_reason = payment.return_reason or record.return_reason
        row.resolved_at = moment
        row.resolved_by = None
        row.resolution_note = SPLIT_RESOLVED
        row.resolution_kind = ResolutionKind.NOTE
        row.save()
        audit(
            "payment.status_changed",
            ENTITY_PAYMENT,
            row.pk,
            before,
            {
                **_state(row),
                "split_from": str(payment.pk),
                "provider_transaction": mask_identifier(record.external_id),
            },
        )
        reversal = _refresh_charge(row, returned=True)
        if reversal is not None:
            reversals.append(reversal)
    return reversals


def _return_missed(payment: Payment, record: TransactionRecord) -> list[credits.CreditReversal]:
    """An event showing ``payment`` returned already (a replay, the nightly check): what its
    first return could not reach is returned now (PLAN-015e): its credit not reversed yet,
    the rows split off it before ledger 0077 linked them. Nothing when there is none."""
    reversals = _return_split_rows(payment, record)
    if payment.credit_id is not None and payment.rent_charge_id is None:
        reversal = credits.reverse_from_return(payment, _refresh_returned)
        if reversal is not None:
            reversals.insert(0, reversal)
    return reversals


def _refresh_returned(payment: Payment) -> None:
    """A credit payment returned with its credit: its charge reopened and flagged."""
    _refresh_charge(payment, returned=True)


def _refresh_charge(payment: Payment, *, returned: bool) -> credits.CreditReversal | None:
    """Recompute the rent or one-off charge, and on a return flag the charge for review; on
    the return of a payment held as a credit, reverse the credit (PLAN-015e), which is
    returned. A returned payment with neither a charge, an adjustment nor a credit is logged
    and audited (:data:`UNPLACED_ACTION`)."""
    context = {"payment": str(payment.pk), "payment_status": payment.status}
    if returned:
        context |= {"return_code": payment.return_code, "return_reason": payment.return_reason}
    if payment.rent_charge_id is not None:
        charge = RentCharge.objects.select_for_update().get(pk=payment.rent_charge_id)
        before = rent_run.charge_state(charge)
        rent_run.recompute_charge(charge)
        if returned and not charge.needs_review:
            charge.needs_review = True
            charge.save(update_fields=["needs_review", "updated_at"])
        after = rent_run.charge_state(charge)
        if returned:
            audit("rent_charge.ach_returned", ENTITY_CHARGE, charge.pk, before, after | context)
        elif after != before:
            audit("rent_charge.payment_applied", ENTITY_CHARGE, charge.pk, before, after | context)
    elif payment.one_off_charge_id is not None:
        one_off = OneOffCharge.objects.select_for_update().get(pk=payment.one_off_charge_id)
        sources = one_off_charges.redebit_sources(one_off)
        before = one_off_charges.charge_state(one_off)
        one_off_charges.recompute(one_off)
        if returned and not one_off.needs_review:
            one_off.needs_review = True
            one_off.save(update_fields=["needs_review", "updated_at"])
        after = one_off_charges.charge_state(one_off)
        if returned:
            audit(
                "one_off_charge.ach_returned",
                ENTITY_ONE_OFF_CHARGE,
                one_off.pk,
                before,
                after | context,
            )
        elif after != before:
            audit(
                "one_off_charge.payment_applied",
                ENTITY_ONE_OFF_CHARGE,
                one_off.pk,
                before,
                after | context,
            )
        _allocate_to_companions(one_off, context)
        _apply_to_sources(one_off, sources, context)
    elif payment.insurance_charge_id is not None:
        if returned:
            insurance = InsuranceCharge.objects.select_for_update().get(
                pk=payment.insurance_charge_id
            )
            flagged = insurance.needs_review
            if not flagged:
                insurance.needs_review = True
                insurance.save(update_fields=["needs_review", "updated_at"])
            audit(
                "insurance_charge.ach_returned",
                ENTITY_INSURANCE_CHARGE,
                insurance.pk,
                {"needs_review": flagged},
                {"needs_review": True, "policy_year": insurance.policy_year} | context,
            )
    elif payment.credit_id is not None:
        if returned:
            return credits.reverse_from_return(payment, _refresh_returned)
    elif returned and payment.charge_adjustment_id is None:
        logger.warning("Returned payment %s has neither a charge nor a credit", payment.pk)
        audit(UNPLACED_ACTION, ENTITY_PAYMENT, payment.pk, None, _state(payment) | context)
    return None


def _allocate_to_companions(charge: OneOffCharge, context: dict[str, Any]) -> None:
    """PLAN-011b D5: a payment on a re-debit's invoice pays the re-debit first, then its
    NSF fee; PLAN-012 D6: a payment on a multi-line invoice pays its first line's charge,
    then each later line's in line order (a return or refund takes it back from the last
    line first); PLAN-012b-b D5: a Rent line's rent charge is paid as a later line
    (``rent_charge.payment_allocated``). Each charge whose paid amount or status moved
    gets its own audit row."""
    for companion, before, after in one_off_charges.refresh_companions(charge):
        action, entity = one_off_charges.allocation_audit(companion)
        audit(
            action,
            entity,
            companion.pk,
            before,
            after | context | {"allocated_from": str(charge.pk)},
        )


def _apply_to_sources(
    charge: OneOffCharge,
    sources: list[tuple[one_off_charges.Source, dict[str, Any]]],
    context: dict[str, Any],
) -> None:
    """PLAN-011b D5: what a re-debit collects is paid on the charge its returned payment
    was for (rent, insurance or one-off); each source that moved gets its own audit row."""
    for change in one_off_charges.refresh_sources(sources):
        extra = {"redebit": str(charge.pk)}
        if change.allocated_from is not None:
            extra["allocated_from"] = str(change.allocated_from)
        audit(
            change.action,
            change.entity_type,
            change.entity_id,
            change.before,
            change.after | context | extra,
        )


def _state(payment: Payment) -> dict[str, Any]:
    return {
        "status": payment.status,
        "kind": payment.kind,
        "policy_year": payment.policy_year,
        "amount": str(payment.amount),
        "is_refund": payment.is_refund,
        "invoice": str(payment.invoice_id) if payment.invoice_id else None,
        "rent_charge": str(payment.rent_charge_id) if payment.rent_charge_id else None,
        "insurance_charge": (
            str(payment.insurance_charge_id) if payment.insurance_charge_id else None
        ),
        "one_off_charge": str(payment.one_off_charge_id) if payment.one_off_charge_id else None,
        "settled_on": payment.settled_on.isoformat() if payment.settled_on else None,
        "return_code": payment.return_code,
    }
