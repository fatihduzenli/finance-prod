"""Refunds and voids of ACH payments, made by an admin (PLAN-009 D6, D7).

Rules kept here, not in the views:

- **Refund**: only a ``settled`` ACH payment that is not itself a refund, holds a
  provider transaction reference and has something left to refund. The refundable
  amount is the payment's amount minus its refunds that are ``pending`` or ``settled``
  (a pending refund has been asked of the provider already). The amount is a
  ``Decimal`` of at least 0.01 with at most two decimals, at most the refundable amount.
- **Void**: only a ``pending`` or ``settled`` ACH payment that is not a refund, holds a
  reference and has no refund that is not voided. The whole amount; the payment
  itself becomes ``voided`` (no new row, as the webhook handlers model a void).
- Manual payments (wire, Zelle, check, cash, other) were never provider transactions and
  have no refund or processor void here; billing voids one locally with a reason
  (:func:`void_manual_payment`, PLAN-012f-c D2): nothing is sent to the provider, the
  charge is recomputed and ``payment.voided_manual`` is audited, with
  ``provider_marking: "to unmark"`` when a marking of the charge's provider invoice by
  this payment succeeded (D3: the provider has no unmark call). A deposit refund
  (``refund_deposit``, PLAN-010b D4) is money paid back by hand and is never refunded or
  voided.
- A payment on a one-off charge (kind ``one_off``) made by ACH follows the rent rules; its
  charge is locked and recomputed as a rent charge is.
- The admin's confirmation (the exact-value checkbox) is required; without it nothing
  is sent.

Both run under the row locks of the payment and its rent or one-off charge, held during the
provider call, so two admins confirming at once make one refund: the second sees the
first refund's row and the smaller refundable amount. A form's ``creation_key`` is kept
in the audit row; a resubmitted confirmation finds it and calls nothing.

A refund calls ``refund_transaction``, re-fetches the new refund transaction (when the
provider's answer names it) and the invoice, writes the refund row through
:func:`payments.webhooks.handlers.transaction.record_refund` (from the re-fetched
transaction, or from the request when its ID is unknown), recomputes the charge, copies
the invoice state through :mod:`payments.invoice_sync` and audits ``payment.refunded``.
A void calls ``void_transaction``, re-fetches the transaction and the invoice, marks
the payment ``voided``, recomputes and audits ``payment.voided``. A provider failure of
the refund or void call changes nothing beyond the audit row ``payment.refund_failed``
or ``payment.void_failed`` with the failure class and field names. A failed re-fetch
after the provider accepted the call does not undo the local record (the money moved):
the refund row is then made from the provider's answer to the refund, and the failure
is named in the audit row and in the message.

Provider identifiers appear in audit rows masked. Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

import payments
from accounts.roles import Role, meets, role_of
from ledger.models import (
    MANUAL_PAYMENT_METHODS,
    ZERO,
    AuditLog,
    Invoice,
    OneOffCharge,
    Payment,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RentCharge,
    SyncFailure,
)
from ledger.services import installments, one_off_charges, rent_run
from ledger.validators import PAYMENT_DATA_MESSAGE, validate_no_payment_data, visible_text
from ledger.wording import format_amount, provider_sentence
from payments import invoice_sync, lease_sync
from payments.errors import ProviderError, ProviderUnavailable
from payments.provider import TRANSACTION_REFUND, TransactionRecord, mask_identifier
from payments.webhooks.handlers import transaction as transaction_handler
from payments.webhooks.handlers.common import (
    ENTITY_INVOICE,
    ENTITY_PAYMENT,
    KIND_INVOICE,
    KIND_TRANSACTION,
    current_ref,
    fetch,
)

CENT = Decimal("0.01")

REFUNDED_ACTION = "payment.refunded"
REFUND_FAILED_ACTION = "payment.refund_failed"
VOIDED_ACTION = "payment.voided"
VOID_FAILED_ACTION = "payment.void_failed"
_REFUND_ACTIONS = (REFUNDED_ACTION, REFUND_FAILED_ACTION)
_VOID_ACTIONS = (VOIDED_ACTION, VOID_FAILED_ACTION)
#: The void of a payment recorded outside the processor (PLAN-012f-c D2).
VOIDED_MANUAL_ACTION = "payment.voided_manual"
#: A successful marking of the charge's provider invoice paid by a payment (its audit row's
#: ``after.payment``), rent or one-off (PLAN-009 D1, PLAN-012f-c D3).
_MARKED_ACTIONS = (
    rent_run.RENT.action("provider_marked"),
    one_off_charges.ONE_OFF.action("provider_marked"),
)
#: The manual void's audit value when the provider invoice stays marked paid (D3).
PROVIDER_MARKING_TO_UNMARK = "to unmark"
VOID_REASON_MAX_LENGTH = 200

#: Statuses of a refund that count against the refundable amount.
_OPEN_REFUND = (PaymentStatus.PENDING, PaymentStatus.SETTLED)

NOT_ACH = provider_sentence(
    "Only a bank debit made through {provider} can be refunded or voided here. A wire, Zelle "
    "or other payment never went through {provider}."
)
IS_REFUND = "A refund cannot itself be refunded or voided."
IS_DEPOSIT_REFUND = "A deposit refund was paid back by hand; it cannot be refunded or voided."
NO_REFERENCE = provider_sentence("This payment is not in {provider}.")
REFUND_NOT_SETTLED = "Only a settled payment can be refunded."
NOTHING_REFUNDABLE = "Nothing is left to refund on this payment."
VOID_NOT_OPEN = "Only a pending or settled payment can be voided; this one is {status}."
VOID_HAS_REFUNDS = "A payment that has refunds cannot be voided."
AMOUNT_TOO_SMALL = "The refund amount must be at least $0.01. Nothing was sent."
AMOUNT_NOT_CENTS = "The refund amount must have at most two decimals. Nothing was sent."
AMOUNT_OVER = (
    "The refund amount is more than the remaining refundable amount of {refundable}. "
    "Nothing was sent."
)
CONFIRM_REQUIRED = "Check the box to continue. Nothing was sent."
ALREADY_SUBMITTED = "This confirmation was already submitted. Nothing was sent again."
REFUNDED = provider_sentence("Refund of {amount} made through {provider} and recorded.")
REFUND_PENDING = provider_sentence(" It stays pending until {provider} settles it.")
VOIDED = provider_sentence("Payment voided through {provider} and recorded.")
CALL_FAILED = provider_sentence(
    "{provider} did not {verb} the payment: {failure}. Nothing was changed."
)
OUTCOME_UNKNOWN = provider_sentence(
    " {provider} could not be reached, so the {noun} may have been made: check {provider} "
    "before trying again."
)
REFETCH_FAILED = provider_sentence(
    " {provider} could not be read back afterwards; the record is completed when its event arrives."
)

#: The sentences shown on the confirmation pages.
REFUND_SENTENCE = provider_sentence(
    "A refund is sent to {provider} at once, returns the money to the operator through "
    "{provider} and cannot be undone here."
)
VOID_SENTENCE = provider_sentence(
    "A void cancels the whole payment in {provider} before it moves money and cannot be "
    "undone here."
)
REFUND_CONFIRMATION = "I confirm this refund of the amount entered above."

#: The void of a payment recorded outside the processor (PLAN-012f-c D2 to D5).
VOID_NOT_ALLOWED = "Voiding a payment needs the billing role or above. Nothing was changed."
MANUAL_IS_PROCESSOR = provider_sentence(
    "A bank debit made through {provider} is voided with Void, by an admin."
)
MANUAL_IS_CREDIT = "A credit applied from the lease cannot be voided here."
MANUAL_CREDIT_REFUND = "A credit refund was paid back by hand; it cannot be voided."
ALREADY_VOIDED = "This payment is already voided."
MANUAL_NOT_SETTLED = "Only a settled payment can be voided; this one is {status}."
MANUAL_NO_CHARGE = "Only a payment on a rent or one-off charge can be voided here."
REASON_REQUIRED = "A reason is required: say why the payment is voided."
REASON_TOO_LONG = "The reason must be at most 200 characters."
ALREADY_SUBMITTED_VOID = "This void was already submitted. Nothing was changed again."
VOIDED_MANUAL = (
    "Payment voided: {amount} is owed on the charge again. The record stays, marked void."
)
VOIDED_MANUAL_UNMARK = provider_sentence(
    " Invoice {number} is still marked paid in {provider}: unmark it in the {provider} dashboard."
)
#: The dialog's sentences (D4, D5).
VOID_MANUAL_SENTENCE = "Voiding returns {amount} to {charge}; the record stays, marked void."
VOID_MANUAL_MARKED = provider_sentence(
    "The {provider} invoice {number} was marked paid by this payment; after the void, unmark "
    "it in the {provider} dashboard."
)
VOID_MANUAL_PLAN = (
    "The installment plan was built on the remaining balance; voiding adds {amount} back to "
    "the charge, not to the plan."
)
VOID_CONFIRMATION = "I confirm the void of this whole payment of {amount}."


@dataclass(frozen=True, slots=True)
class PaymentFacts:
    """What the payment pages show and the services check, read without locks."""

    #: The provider transaction ID, unmasked (callers mask it); ``""`` when none.
    reference: str
    refunded: Decimal
    refundable: Decimal
    refund_refusal: str
    void_refusal: str

    @property
    def can_refund(self) -> bool:
        return not self.refund_refusal

    @property
    def can_void(self) -> bool:
        return not self.void_refusal


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    #: The form field a refusal belongs to (``""``: the whole form).
    field: str = ""
    replayed: bool = False
    #: True when the provider accepted the call but could not be read back.
    incomplete: bool = False
    refund: Payment | None = None


def transaction_reference(payment: Payment) -> str:
    return current_ref(ENTITY_PAYMENT, payment.pk, KIND_TRANSACTION)


def refunded_amount(payment: Payment) -> Decimal:
    """The payment's refunds that are pending or settled, to the cent."""
    total = Payment.objects.filter(
        parent_payment=payment, is_refund=True, status__in=_OPEN_REFUND
    ).aggregate(total=Sum("amount"))["total"]
    return Decimal(total or ZERO).quantize(CENT)


def _common_refusal(payment: Payment, reference: str) -> str:
    if payment.kind == PaymentKind.REFUND_DEPOSIT:
        return IS_DEPOSIT_REFUND
    if payment.is_refund:
        return IS_REFUND
    if payment.method != PaymentMethod.ACH:
        return NOT_ACH
    if not reference:
        return NO_REFERENCE
    return ""


def _refund_refusal(payment: Payment, reference: str, refundable: Decimal) -> str:
    refusal = _common_refusal(payment, reference)
    if refusal:
        return refusal
    if payment.status != PaymentStatus.SETTLED:
        return REFUND_NOT_SETTLED
    if refundable <= 0:
        return NOTHING_REFUNDABLE
    return ""


def _void_refusal(payment: Payment, reference: str) -> str:
    refusal = _common_refusal(payment, reference)
    if refusal:
        return refusal
    if payment.status not in (PaymentStatus.PENDING, PaymentStatus.SETTLED):
        return VOID_NOT_OPEN.format(status=payment.get_status_display().lower())
    if payment.refunds.exclude(status=PaymentStatus.VOIDED).exists():
        return VOID_HAS_REFUNDS
    return ""


def payment_facts(payment: Payment) -> PaymentFacts:
    reference = transaction_reference(payment)
    refunded = refunded_amount(payment)
    # A voided payment (a processor void or a manual one, PLAN-012f-c) has nothing left
    # to refund.
    if payment.status == PaymentStatus.VOIDED:
        refundable = ZERO.quantize(CENT)
    else:
        refundable = max(ZERO, payment.amount - refunded).quantize(CENT)
    return PaymentFacts(
        reference=reference,
        refunded=refunded,
        refundable=refundable,
        refund_refusal=_refund_refusal(payment, reference, refundable),
        void_refusal=_void_refusal(payment, reference),
    )


def void_confirmation(payment: Payment) -> str:
    return VOID_CONFIRMATION.format(amount=format_amount(payment.amount))


# --- Refund ----------------------------------------------------------------------------


def refund_payment(
    payment_id: uuid.UUID,
    *,
    amount: Decimal,
    confirmed: bool,
    actor: User | None,
    creation_key: uuid.UUID | None = None,
) -> Outcome:
    """Refund ``amount`` of an ACH payment at the provider and record it (D6)."""
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount < CENT:
        return Outcome(False, AMOUNT_TOO_SMALL, "amount")
    if amount != amount.quantize(CENT):
        return Outcome(False, AMOUNT_NOT_CENTS, "amount")
    if not confirmed:
        return Outcome(False, CONFIRM_REQUIRED, "confirm")
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        charge = _lock_charge(payment)
        if creation_key is not None and _key_used(payment, _REFUND_ACTIONS, creation_key):
            return Outcome(True, ALREADY_SUBMITTED, replayed=True)
        facts = payment_facts(payment)
        if facts.refund_refusal:
            return Outcome(False, facts.refund_refusal)
        if amount > facts.refundable:
            return Outcome(
                False, AMOUNT_OVER.format(refundable=format_amount(facts.refundable)), "amount"
            )
        reference = facts.reference
        before = {
            **_payment_state(payment),
            "refunded": str(facts.refunded),
            "refundable": str(facts.refundable),
            "charge": _charge_state(charge),
        }
        context = {
            "amount": str(amount),
            "provider_transaction": mask_identifier(reference),
            "creation_key": str(creation_key) if creation_key is not None else None,
        }
        provider = payments.get_provider()
        try:
            answer = provider.refund_transaction(reference, amount)
        except ProviderError as error:
            return _failed(
                actor, REFUND_FAILED_ACTION, payment, before, context, error, "refund", "refund"
            )
        refund_id = answer.external_id if answer.external_id != reference else ""
        refetch_failure = ""
        record: TransactionRecord | None = None
        if refund_id:
            try:
                record = fetch(provider.get_transaction, refund_id)
            except ProviderError as error:
                # The provider's answer names the refund: the row keeps its ID.
                refetch_failure = lease_sync.failure_name(error)
                record = answer
        if record is not None and not _is_refund_of(record, reference):
            record = None
        refund = transaction_handler.record_refund(payment, amount, record)
        invoice_changes, invoice_failure = _refresh_invoice(payment)
        refetch_failure = refetch_failure or invoice_failure
        _recompute(charge)
        refund.refresh_from_db()
        after = {
            **_payment_state(payment),
            **context,
            "refund_payment": str(refund.pk),
            "refund_status": refund.status,
            "provider_refund": mask_identifier(refund_id) if refund_id else None,
            "refunded": str(refunded_amount(payment)),
            "refundable": str(payment_facts(payment).refundable),
            "charge": _charge_state(charge),
            "invoice_changes": invoice_changes,
            "refetch_failure": refetch_failure or None,
        }
        _audit(actor, REFUNDED_ACTION, payment.pk, before, after)
    message = REFUNDED.format(amount=format_amount(amount))
    if refund.status == PaymentStatus.PENDING:
        message += REFUND_PENDING
    if refetch_failure:
        message += REFETCH_FAILED
    return Outcome(True, message, incomplete=bool(refetch_failure), refund=refund)


def _is_refund_of(record: TransactionRecord, reference: str) -> bool:
    """Whether the re-fetched transaction reads as a refund of ``reference``."""
    return record.parent_external_id in ("", reference) and record.kind in (
        "",
        TRANSACTION_REFUND,
    )


# --- Void ------------------------------------------------------------------------------


def void_payment(
    payment_id: uuid.UUID,
    *,
    confirmed: bool,
    actor: User | None,
    creation_key: uuid.UUID | None = None,
) -> Outcome:
    """Void an ACH payment at the provider; the payment becomes ``voided`` (D6)."""
    if not confirmed:
        return Outcome(False, CONFIRM_REQUIRED, "confirm")
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        charge = _lock_charge(payment)
        if creation_key is not None and _key_used(payment, _VOID_ACTIONS, creation_key):
            return Outcome(True, ALREADY_SUBMITTED, replayed=True)
        facts = payment_facts(payment)
        if facts.void_refusal:
            return Outcome(False, facts.void_refusal)
        reference = facts.reference
        before = {
            **_payment_state(payment),
            "charge": _charge_state(charge),
        }
        context = {
            "amount": str(payment.amount),
            "provider_transaction": mask_identifier(reference),
            "creation_key": str(creation_key) if creation_key is not None else None,
        }
        provider = payments.get_provider()
        try:
            answer = provider.void_transaction(reference)
        except ProviderError as error:
            return _failed(
                actor, VOID_FAILED_ACTION, payment, before, context, error, "void", "void"
            )
        refetch_failure = ""
        record = answer
        try:
            record = provider.get_transaction(reference)
        except ProviderError as error:
            refetch_failure = lease_sync.failure_name(error)
        transaction_handler.record_void(payment, record)
        # PLAN-012f-c D2: both voids read alike under the payment page's DETAILS.
        voided_at = timezone.now()
        Payment.objects.filter(pk=payment.pk).update(
            voided_at=voided_at, voided_by=actor, updated_at=voided_at
        )
        invoice_changes, invoice_failure = _refresh_invoice(payment)
        refetch_failure = refetch_failure or invoice_failure
        _recompute(charge)
        payment.refresh_from_db()
        after = {
            **_payment_state(payment),
            **context,
            "provider_shows_voided": record.is_voided,
            "charge": _charge_state(charge),
            "invoice_changes": invoice_changes,
            "refetch_failure": refetch_failure or None,
        }
        _audit(actor, VOIDED_ACTION, payment.pk, before, after)
    message = VOIDED
    if refetch_failure:
        message += REFETCH_FAILED
    return Outcome(True, message, incomplete=bool(refetch_failure))


# --- Void of a payment recorded outside the processor (PLAN-012f-c D2 to D4) ---------------


def _manual_refusal(payment: Payment) -> str:
    """Why ``payment`` cannot be voided as a manual payment; ``""`` when it can."""
    if payment.kind == PaymentKind.REFUND_DEPOSIT:
        return IS_DEPOSIT_REFUND
    if payment.is_refund:
        return IS_REFUND
    if payment.kind == PaymentKind.CREDIT_REFUND:
        return MANUAL_CREDIT_REFUND
    if payment.method == PaymentMethod.ACH:
        return MANUAL_IS_PROCESSOR
    if payment.method not in MANUAL_PAYMENT_METHODS:
        return MANUAL_IS_CREDIT
    if payment.status == PaymentStatus.VOIDED:
        return ALREADY_VOIDED
    if payment.status != PaymentStatus.SETTLED:
        return MANUAL_NOT_SETTLED.format(status=payment.get_status_display().lower())
    if payment.rent_charge_id is None and payment.one_off_charge_id is None:
        return MANUAL_NO_CHARGE
    return ""


def manual_void_refusal(payment: Payment) -> str:
    """Why the payment page offers no "Void payment" for ``payment`` (``""``: it does)."""
    return _manual_refusal(payment)


def _reason_refusal(reason: str) -> str:
    if not reason:
        return REASON_REQUIRED
    if len(reason) > VOID_REASON_MAX_LENGTH:
        return REASON_TOO_LONG
    try:
        validate_no_payment_data(reason)
    except ValidationError:
        return PAYMENT_DATA_MESSAGE
    return ""


def marked_invoice(payment: Payment) -> Invoice | None:
    """The invoice a marking job for ``payment`` marked paid at the provider: the payment's
    charge's provider invoice when a ``<charge>.provider_marked`` audit row names this
    payment (its ``after.payment``); ``None`` when no marking succeeded."""
    charge = payment.rent_charge or payment.one_off_charge
    if charge is None:
        return None
    marked = AuditLog.objects.filter(
        action__in=_MARKED_ACTIONS, after__payment=str(payment.pk)
    ).exists()
    if not marked:
        return None
    linked = rent_run.provider_invoice(charge)
    return linked[0] if linked is not None else None


def _paid_day(payment: Payment) -> date | None:
    return payment.settled_on or payment.initiated_on


def _plan_after(payment: Payment) -> bool:
    """Whether an installment plan on the payment's charge (any status) was created after
    the payment's day (``settled_on``, else ``initiated_on``; D4)."""
    charge = payment.rent_charge or payment.one_off_charge
    day = _paid_day(payment)
    if charge is None or day is None:
        return False
    for created_at in charge.installment_plans.values_list("created_at", flat=True):
        created_on = timezone.localdate(created_at)
        if created_on > day or (created_on == day and created_at > payment.created_at):
            return True
    return False


def void_manual_facts(payment: Payment) -> list[str]:
    """The dialog's sentences (D4, D5): the consequence; the marked invoice's when a marking
    succeeded; the installment plan's when a plan on the charge came after the payment."""
    charge = payment.rent_charge or payment.one_off_charge
    label = installments.parent_screen_label(charge) if charge is not None else "the charge"
    amount = format_amount(payment.amount)
    sentences = [VOID_MANUAL_SENTENCE.format(amount=amount, charge=label)]
    invoice = marked_invoice(payment)
    if invoice is not None:
        sentences.append(VOID_MANUAL_MARKED.format(number=invoice.invoice_number))
    if _plan_after(payment):
        sentences.append(VOID_MANUAL_PLAN.format(amount=amount))
    return sentences


def void_manual_payment(
    payment_id: uuid.UUID,
    *,
    reason: str,
    actor: User | None,
    creation_key: uuid.UUID | None = None,
) -> Outcome:
    """Void a payment recorded outside the processor (D2): billing and above, a settled
    Zelle, wire, check, cash or other payment on a rent or one-off charge, with a reason.
    Nothing is sent to the provider; the charge is recomputed under its row lock and one
    audit row ``payment.voided_manual`` is written."""
    if actor is None or not meets(role_of(actor), Role.BILLING):
        return Outcome(False, VOID_NOT_ALLOWED)
    reason = visible_text(reason or "")
    refusal = _reason_refusal(reason)
    if refusal:
        return Outcome(False, refusal, "reason")
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        charge = _lock_charge(payment)
        if creation_key is not None and _key_used(payment, (VOIDED_MANUAL_ACTION,), creation_key):
            return Outcome(True, ALREADY_SUBMITTED_VOID, replayed=True)
        refusal = _manual_refusal(payment)
        if refusal:
            return Outcome(False, refusal)
        invoice = marked_invoice(payment)
        before = {**_payment_state(payment), "charge": _charge_state(charge)}
        payment.status = PaymentStatus.VOIDED
        payment.voided_at = timezone.now()
        payment.voided_by = actor
        payment.void_reason = reason
        payment.save(
            update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"]
        )
        _recompute(charge)
        after: dict[str, Any] = {
            **_payment_state(payment),
            "reason": reason,
            "charge": _charge_state(charge),
            "creation_key": str(creation_key) if creation_key is not None else None,
        }
        if invoice is not None:
            after |= {
                "provider_marking": PROVIDER_MARKING_TO_UNMARK,
                "marked_invoice": str(invoice.pk),
                "invoice_number": invoice.invoice_number,
            }
        _audit(actor, VOIDED_MANUAL_ACTION, payment.pk, before, after)
    message = VOIDED_MANUAL.format(amount=format_amount(payment.amount))
    if invoice is not None:
        message += VOIDED_MANUAL_UNMARK.format(number=invoice.invoice_number)
    return Outcome(True, message)


# --- Helpers ---------------------------------------------------------------------------


def _lock_charge(payment: Payment) -> RentCharge | OneOffCharge | None:
    if payment.rent_charge_id is not None:
        return RentCharge.objects.select_for_update().get(pk=payment.rent_charge_id)
    if payment.one_off_charge_id is not None:
        return OneOffCharge.objects.select_for_update().get(pk=payment.one_off_charge_id)
    return None


def _recompute(charge: RentCharge | OneOffCharge | None) -> None:
    if isinstance(charge, RentCharge):
        rent_run.recompute_charge(charge)
    elif isinstance(charge, OneOffCharge):
        one_off_charges.recompute(charge)


def _charge_state(charge: RentCharge | OneOffCharge | None) -> dict[str, Any] | None:
    if isinstance(charge, RentCharge):
        return rent_run.charge_state(charge)
    if isinstance(charge, OneOffCharge):
        return one_off_charges.charge_state(charge)
    return None


def _key_used(payment: Payment, actions: tuple[str, ...], creation_key: uuid.UUID) -> bool:
    return AuditLog.objects.filter(
        action__in=actions,
        entity_type=ENTITY_PAYMENT,
        entity_id=str(payment.pk),
        after__creation_key=str(creation_key),
    ).exists()


def _refresh_invoice(payment: Payment) -> tuple[dict[str, Any], str]:
    """Re-fetch the payment's invoice and copy its state; the changes and any failure."""
    if payment.invoice_id is None:
        return {}, ""
    reference = current_ref(ENTITY_INVOICE, payment.invoice_id, KIND_INVOICE)
    if not reference:
        return {}, ""
    try:
        record = payments.get_provider().get_invoice(reference)
    except ProviderError as error:
        return {}, lease_sync.failure_name(error)
    return invoice_sync.record_provider_state(payment.invoice_id, record), ""


def _failed(
    actor: User | None,
    action: str,
    payment: Payment,
    before: dict[str, Any],
    context: dict[str, Any],
    error: ProviderError,
    verb: str,
    noun: str,
) -> Outcome:
    failure = lease_sync.failure_name(error)
    fields = lease_sync.failure_fields(error)
    _audit(
        actor,
        action,
        payment.pk,
        before,
        {**before, **context, "failure": failure, "fields": fields},
    )
    message = CALL_FAILED.format(verb=verb, failure=SyncFailure(failure).label)
    if isinstance(error, ProviderUnavailable):
        message += OUTCOME_UNKNOWN.format(noun=noun)
    return Outcome(False, message)


def _payment_state(payment: Payment) -> dict[str, Any]:
    return {
        "status": payment.status,
        "method": payment.method,
        "amount": str(payment.amount),
        "is_refund": payment.is_refund,
        "rent_charge": str(payment.rent_charge_id) if payment.rent_charge_id else None,
        "one_off_charge": str(payment.one_off_charge_id) if payment.one_off_charge_id else None,
        "invoice": str(payment.invoice_id) if payment.invoice_id else None,
    }


def _audit(
    actor: User | None,
    action: str,
    payment_id: uuid.UUID,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_PAYMENT,
        entity_id=str(payment_id),
        before=before,
        after=after,
    )
