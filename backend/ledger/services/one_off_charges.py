"""One-off charges: every amount charged once (docs/qa/PLAN-010b D3 to D6 as setup
charges; docs/qa/PLAN-010d D1, D2, D6 for every category).

Rules kept here, not in the views:

- **Creation** (:func:`create_charge`, the single path). Category ``setup`` (PLAN-010b):
  from the lease form's checklist (:func:`create_lease_with_checklist`: the lease and
  every ticked charge in one transaction, under the lease form's creation key, so a
  resubmitted form makes one lease and one set of charges), or one at a time from the
  lease page (:func:`add_charge`); collection ``none``; the fee type must be active; a
  lease has at most one non-legacy charge per fee type (a unique index). The other
  categories (``arrears``, ``adjustment``, ``damage``, ``other``; PLAN-010d D2) from the
  operator or lease page: a description (the invoice's line item, at most
  :data:`~ledger.models.DESCRIPTION_MAX_LENGTH` characters) and an internal note are
  required, the amount is above zero and at most :data:`AMOUNT_MAX`, and the
  collection decides what follows in the same transaction: for ``email``, ``manual``
  and ``charge_now`` a local invoice (numbered by :mod:`ledger.services.numbering`,
  ``draft``) and the job ``invoice_create``; for ``autopay_due`` no invoice and the job
  ``schedule_create`` (the provider generates the invoice on the due date). Refused,
  with nothing written: an operator without a provider customer (for anything but
  ``manual``; a ``manual`` charge then gets its local invoice and no job), ``autopay_due``
  or ``charge_now`` without a linked bank method, above the confirmed provider limit, or
  above the large-debit threshold without the confirmation; ``autopay_due`` due in the
  past or on a lease that ended; ``charge_now`` by anyone but an admin.
- **Status** is never set by hand. :func:`recompute` derives it from the settled
  ``one_off`` payments minus their settled refunds: ``paid``, ``partial`` or ``owed``. A
  deposit refund (``refund_deposit``) is money paid back at the lease's end and does
  not reopen the charge. ``waived`` is set only by :func:`waive`, ``voided`` only by
  :func:`mark_voided` once the provider confirmed the cancel.
- **Edit** (:func:`edit_charge`): cost, price, vendor and due date; the price is locked
  once any payment exists on the charge, and a waived or voided charge is not edited.
- **Waive** (:func:`waive`, admin): a note is mandatory; refused once any payment on the
  charge is settled, and refused while the charge has a live provider invoice
  (:func:`live_provider_invoice`; PLAN-011 D0): cancel first. Nothing is sent to the
  payment provider.
- **Delete the invoice of a waived charge** (:func:`delete_waived_invoice`, admin;
  PLAN-011 D0): the remedy for a charge waived before that rule. It queues
  ``invoice_delete`` with :data:`WAIVED_INVOICE_PARAM`; the worker deletes the provider
  invoice (:func:`payments.one_off_invoices.delete_waived`) and, once the provider
  confirmed, :func:`mark_waived_invoice_deleted` voids the local invoice and leaves the
  charge ``waived``. :func:`waived_with_live_invoice` lists the charges still to clear.
- **Cancel** (:func:`cancel_charge`, billing; PLAN-010d D6): a note is mandatory;
  refused once anything is paid or a payment is pending. It queues the delete of the
  provider invoice (``invoice_delete``) or, for an ``autopay_due`` charge whose
  schedule has not generated its invoice, of the schedule (``schedule_delete``); the
  charge is voided by the worker only after the provider confirmed.
- **Charge now** (:func:`charge_now_request`, :func:`charge_now_confirm`, admin;
  PLAN-010d D4): the confirmation page's details come from local data only (the bank
  method's last four and nickname from the stored methods snapshot); the confirm sets
  the charge's ``charge_now_key`` once and queues the ``invoice_pay`` job, which
  :func:`payments.one_off_invoices.pay` runs in the worker: one attempt. Refused, with
  nothing queued: the pay-an-invoice flag off (:func:`payments.pay_invoice_enabled`),
  anyone but an admin, a
  charge not collected by Charge now, waived, voided or with nothing owed, its invoice
  not at the provider yet or with a total that differs from the price, a payment
  pending, settled or returned on it, no linked bank method or one the stored snapshot
  does not show as a usable bank account, above the confirmed provider limit, above the
  large-debit threshold without the confirmation, a provider request already waiting,
  or less than a minute after a failed attempt.
- **Manual payments** (:func:`record_payment`): the rent rules (method, amount, date,
  note and the form's creation key; :func:`ledger.services.rent_run.payment_refusal`),
  kind ``one_off``; it closes the open promises it keeps
  (:func:`ledger.services.contacts.close_kept_by_payment`, PLAN-012 D4). The provider
  invoice, when one is linked and the payment covers the whole price (on a builder
  invoice: once the lines' payments together cover the invoice, PLAN-012c-a D2), is
  never marked here: the outcome says ``mark_due`` and the caller queues the
  ``one_off_payment_mark`` job, which runs :func:`mark_provider_invoice` in the worker
  with the rent rules of :func:`ledger.services.rent_run.mark_charge_invoice`. A partial
  payment, or one that differs from the invoice total, flags the charge and makes no
  call. A failed marking is retried by hand (:func:`retry_provider_mark`, the
  ``one_off_payment_retry_mark`` job), refused for ``RETRY_LOCK_SECONDS`` after the last
  attempt, as on rent charges.
- **Linking an invoice** (:func:`link_invoice`): the rent rules of
  :func:`ledger.services.rent_run.link_charge_invoice`, with the same sentences.
- **Re-debit and NSF fee** (PLAN-011b D5; made by :func:`ledger.services.nsf.reschedule`):
  a ``redebit`` charge and its ``nsf_fee`` companion (``companion_of``) share the
  re-debit's invoice or schedule, whose lines are the re-debit and the fee unless the fee
  is waived or voided (:func:`billed_companions`, :func:`billed_total`). A payment on that
  invoice is a payment of the re-debit charge and is **allocated** re-debit first: the
  re-debit's paid amount is capped at its price while a billed companion exists, and the
  rest pays the companions in creation order (:func:`allocated_overflow`);
  :func:`refresh_companions` recomputes them after the re-debit. A companion is not
  cancelled on its own (it has no invoice): cancelling the re-debit voids it too
  (:func:`mark_voided`); it is not waived while the re-debit's invoice is live.
  What a re-debit collects (up to its price; the fee never counts) is paid on the
  charge whose returned payment it collects again, rent, insurance or one-off
  (:func:`ledger.services.rent_run.redebit_collected`): take :func:`redebit_sources`
  before the re-debit's payments are recomputed and :func:`refresh_sources` after, and
  audit each change it returns.
- **Lines of one invoice** (PLAN-012 D6; made by :mod:`ledger.services.invoice_builder`):
  each line of a builder invoice is a charge of its own, created here with
  ``with_invoice=False`` (no invoice and no job of its own); a line with a fee type that
  is not a category is a ``setup`` charge with that fee type and the invoice's collection
  (``email`` or ``charge_now``). The invoice is linked to the **first** line's charge and
  its line items name every line's charge (``invoice_line_items.one_off_charge``); the
  later lines are billed with the first exactly as a companion fee is billed with its
  re-debit (:func:`billed_with`, :func:`billed_companions`): a payment on the invoice at
  the provider is a payment of the first charge and pays the lines in line order, what
  a later line's own payments already paid left out (:func:`allocated_overflow`);
  refunds and returns take it back from the last line first. Every line's charge has
  the invoice (:func:`invoices_of`, PLAN-012c-a D1): its page shows it, Link invoice is
  refused and Waive is refused while it is live. A later line is not cancelled or
  charged now on its own (the first line's cancel voids every line), but a manual
  payment may be recorded on any line, up to that line's balance (PLAN-012c-a D2): the
  invoice is marked paid at the provider once the lines' payments together cover it
  (:func:`invoice_covered`), and the marking clears every line's marking flag.

- **Installments** (PLAN-012e D9 to D11; plans made by
  :mod:`ledger.services.installments`): an ``installment`` charge is paid like any
  other, and its recompute brings its plan up to date (:func:`sync_plan`): ``completed``
  once every installment that is not voided is paid, ``attention`` while one has a
  returned payment not resolved or a payment on the parent itself lowered its balance
  below the plan's total (:func:`parent_paid_on_plan`, PLAN-012b-a D4; a payment on a
  parent re-checks its live plan here, the parent's row lock taken before the plan's),
  ``active`` otherwise; a completed or cancelled plan's
  installments count as paid on the parent (:func:`ledger.services.rent_run.plan_covered`),
  audited as ``covered_by_plan`` on the parent with no payment row. An installment is
  not waived, and not cancelled on its own while its plan is live: the plan is
  cancelled. A manual payment on a parent whose plan is live is refused, and so are a
  cancel (:data:`CANCEL_PARENT_ON_PLAN`) and a waive (:data:`WAIVE_PARENT_ON_PLAN`) of
  that parent: the plan is cancelled first (PLAN-012b-a). The release below is not a
  cancel and never passes through :func:`cancel_refusal`. A split
  parent's live invoice or schedule is released (PLAN-012b-a D3): the worker marks the
  invoice ``void`` (:func:`mark_invoice_released`) or supersedes the schedule reference
  (:func:`mark_schedule_released`) and the charge stays owed at its price; a failed
  release is retried with its flag (:func:`retry_kind`, :func:`retry_params`).

Every write is audited (entity ``one_off_charge``); provider identifiers are masked.
Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet, Sum
from django.db.models.functions import Trim, Upper
from django.utils import timezone

import payments
from accounts.roles import Role, meets, role_of
from ledger.models import (
    DESCRIPTION_MAX_LENGTH,
    LIVE_PLAN_STATUSES,
    NSF_FEE_CODES,
    ZERO,
    AuditLog,
    CollectionMethod,
    CollectionStatus,
    FeeType,
    InstallmentPlan,
    InstallmentPlanStatus,
    InsuranceCharge,
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentKind,
    PaymentStatus,
    RentCharge,
)
from ledger.services import contacts, insurance, leases, numbering, rent_run
from ledger.services.rent_run import (
    CENT,
    MONEY_MAX,
    PAY_ALREADY,
    PAY_NOTE_PAYMENT_DATA,
    PAY_NOTHING_DUE,
    PAY_OVER_BALANCE,
    PAY_RECORDED,
    PAY_WAIVED,
    LinkOutcome,
    MarkAction,
    MarkResult,
    PaymentOutcome,
)
from ledger.validators import validate_no_payment_data, visible_text
from payments import invoice_sync, jobs, offline_marking
from payments.models import ExternalRef, Job, JobKind, JobStatus

ENTITY_ONE_OFF_CHARGE = "one_off_charge"
#: The ``external_refs.external_kind`` of an ``autopay_due`` charge's provider schedule.
KIND_SCHEDULE = "schedule"
#: Largest amount of a one-off charge created from the form (PLAN-010d D2).
AMOUNT_MAX = Decimal("999999.99")

#: The categories the one-off form offers; ``setup`` comes from the lease checklist.
FORM_CATEGORIES: tuple[str, ...] = (
    OneOffCategory.ARREARS,
    OneOffCategory.ADJUSTMENT,
    OneOffCategory.DAMAGE,
    OneOffCategory.OTHER,
)
#: The collections of the one-off form; ``none`` is the setup flow.
FORM_COLLECTIONS: tuple[str, ...] = (
    CollectionMethod.AUTOPAY_DUE,
    CollectionMethod.EMAIL,
    CollectionMethod.MANUAL,
    CollectionMethod.CHARGE_NOW,
)
#: Collections that get a local invoice at creation (D2).
INVOICED_COLLECTIONS: tuple[str, ...] = (
    CollectionMethod.EMAIL,
    CollectionMethod.MANUAL,
    CollectionMethod.CHARGE_NOW,
)
#: Collections that debit the operator's linked bank method (D2, D4).
DEBIT_COLLECTIONS: tuple[str, ...] = (CollectionMethod.AUTOPAY_DUE, CollectionMethod.CHARGE_NOW)

# --- Fixed sentences ------------------------------------------------------------------

PRICE_NOT_POSITIVE = "The price must be more than zero."
AMOUNT_NOT_POSITIVE = "The amount must be more than zero."
COST_NEGATIVE = "The cost cannot be negative."
NOT_CENTS = "Amounts must have at most two decimals."
TOO_LARGE = "The amount is too large."
AMOUNT_TOO_LARGE = "The amount can be at most $999,999.99."
FEE_TYPE_INACTIVE = "This fee type is not active. Nothing was saved."
ALREADY_ON_LEASE = "This lease already has a charge for {name}. Nothing was saved."
TEXT_PAYMENT_DATA = PAY_NOTE_PAYMENT_DATA
ADDED = "{name} charge added."
SAVED = "Charge saved."
UNCHANGED = "Nothing was changed."
EDIT_WAIVED = "A waived charge cannot be edited. Nothing was saved."
EDIT_VOIDED = "A voided charge cannot be edited. Nothing was saved."
EDIT_ONE_OFF = (
    "Only a setup charge is edited here: its invoice is at the payment provider. Cancel the "
    "charge and create a new one. Nothing was saved."
)
PRICE_LOCKED = "The price cannot change once a payment is recorded. Nothing was saved."
WAIVE_NOTE_REQUIRED = "A note is required: say why the charge is waived."
WAIVE_SETTLED = "A charge with a settled payment cannot be waived. Nothing was changed."
WAIVE_ALREADY = "This charge is already waived. Nothing was changed."
WAIVE_VOIDED = "This charge is voided. Nothing was changed."
WAIVED = "Charge waived."
#: The sentences of PLAN-011 D0 name the provider as staff know it (``PROVIDER_LABEL``).
WAIVE_LIVE_INVOICE = (
    f"This charge has a live {payments.PROVIDER_LABEL} invoice. Cancel it first "
    f"({payments.PROVIDER_LABEL} deletes the invoice), then waive if anything is still owed."
)
#: What each closing action does, shown beside it on the charge page (PLAN-011 D0).
CANCEL_MEANING = (
    f"removes the {payments.PROVIDER_LABEL} invoice and closes the charge, nothing owed"
)
WAIVE_MEANING = (
    f"closes a charge that has no {payments.PROVIDER_LABEL} invoice without collecting it"
)
#: The dashboard's heading over :func:`waived_with_live_invoice` (D0).
WAIVED_LIVE_HEADING = f"Waived with a live {payments.PROVIDER_LABEL} invoice"
#: The line beside "Delete provider invoice" on the charge page (D0).
DELETE_INVOICE_HINT = (
    f"This charge is waived but its {payments.PROVIDER_LABEL} invoice is still live. The "
    "charge stays waived."
)
DELETE_INVOICE_NOT_WAIVED = (
    "Only a waived charge's invoice is deleted here; cancel the charge instead. Nothing was "
    "changed."
)
DELETE_INVOICE_NONE = (
    f"This charge has no live {payments.PROVIDER_LABEL} invoice. Nothing was changed."
)
DELETE_INVOICE_SHARED = (
    f"This {payments.PROVIDER_LABEL} invoice also bills charges that are not waived: cancel "
    "it from its first line's charge. Nothing was changed."
)
DELETE_INVOICE_REQUESTED = (
    "Delete requested. The invoice is marked void once the payment provider confirms the "
    "delete; the charge stays waived."
)
WAIVED_INVOICE_DELETED = "The provider invoice was deleted. The charge stays waived."
RETURNED = "An ACH payment on this charge was returned."
DISPUTED = "A payment on this charge is disputed."
TOTAL_MISMATCH = (
    "The invoice total at the payment provider differs from the price. The price was not changed."
)
SEVERAL_PROVIDER_INVOICES = (
    "Several invoices at the payment provider could be this charge's. Nothing was created; "
    "review them on the payment provider's dashboard."
)
OTHER_OPERATOR_REFERENCE = (
    "An invoice at the payment provider names this charge's invoice number but another "
    "operator's customer. It was not linked."
)
SECOND_PROVIDER_INVOICE = (
    "More than one invoice at the payment provider names this charge. Only the first is "
    "linked; review the others at the payment provider."
)

# One-off creation (PLAN-010d D2).
CHOOSE_CATEGORY = "Choose a category."
CHOOSE_COLLECTION = "Choose how the charge is collected."
DESCRIPTION_REQUIRED = "A description is required: it is the invoice's line."
DESCRIPTION_TOO_LONG = "The description can be at most 120 characters."
NOTE_REQUIRED = "An internal note is required: say why the charge is made."
DUE_REQUIRED = "Enter a due date."
DUE_IN_PAST = "Autopay on the due date needs a due date that is today or later."
LEASE_ENDED = "This lease has ended: autopay on the due date is not possible. Choose another way."
LEASE_OTHER_OPERATOR = "Choose one of this operator's leases. Nothing was saved."
NO_OPERATOR = "Choose an operator. Nothing was saved."
LEASE_NO_OPERATOR = "This lease has no operator: a one-off charge needs one."
OPERATOR_ARCHIVED = "The operator is archived. Nothing was saved."
NO_PROVIDER_CUSTOMER = (
    "The operator has no payment provider customer: only a manual payment is possible."
)
NO_BANK_METHOD = "The operator has no linked bank method: this collection is not possible."
CHARGE_NOW_ADMIN = "Only an admin can choose Charge now."
CREATED = "Charge created as {number}."
CREATED_NO_INVOICE = "Charge created. The payment provider schedules the invoice for {date}."
ALREADY_CREATED = "This charge was already created. Nothing was changed."
CREATED_MANUAL_LOCAL = (
    "Charge created as {number}. The operator has no payment provider customer, so no "
    "invoice is made there; record the payment when it arrives."
)

# Cancel (PLAN-010d D6).
VOID_NOTE_REQUIRED = "A note is required: say why the charge is cancelled."
CANCEL_VOIDED = "This charge is already voided. Nothing was changed."
CANCEL_SETUP = "A setup charge is waived, not cancelled. Nothing was changed."
CANCEL_WAIVED = "This charge is waived. Nothing was changed."
CANCEL_PAID = (
    "Money was received on this charge: it follows the refund path and cannot be cancelled. "
    "Nothing was changed."
)
PAY_VOIDED = "This charge is voided. Nothing was recorded."
CANCEL_PENDING = "A payment on this charge is pending. Nothing was changed."
CANCEL_COMPANION = (
    "This fee is billed on its re-debit's invoice: cancel the re-debit charge, which cancels "
    "the fee too. Nothing was changed."
)
CANCEL_LINE = (
    "This charge is a later line of a multi-line invoice: cancel the invoice from its draft "
    "or from its first line's charge, which cancels every line. Nothing was changed."
)
LINK_LINE = (
    "This charge is a later line of a multi-line invoice: it is billed on its first line's "
    "invoice. Nothing was linked."
)
CANCEL_BUSY = (
    "A request to the payment provider is already waiting for this charge. Nothing was changed."
)
CANCEL_REQUESTED = (
    "Cancel requested. The charge is voided once the payment provider confirms the delete."
)
VOIDED = "Charge voided."
WAIVE_INSTALLMENT = (
    "An installment is not waived on its own: cancel the installment plan instead. Nothing "
    "was changed."
)
CANCEL_INSTALLMENT = (
    "An installment is not cancelled on its own: cancel the installment plan instead. "
    "Nothing was changed."
)
#: A parent whose installment plan is live (PLAN-012b-a): its balance is owed through
#: the installments, so it is neither cancelled nor waived on its own.
CANCEL_PARENT_ON_PLAN = (
    "This charge is paid through its installment plan: cancel the plan instead. Nothing was "
    "changed."
)
WAIVE_PARENT_ON_PLAN = (
    "This charge is paid through its installment plan: cancel the plan first. Nothing was changed."
)

# Charge now (PLAN-010d D4).
CHARGE_NOW_DISABLED = "Charge now is not enabled in this environment."
CHARGE_NOW_NOT_ELIGIBLE = "This charge is not collected by Charge now. Nothing was charged."
CHARGE_NOW_CLOSED = "This charge is waived or voided. Nothing was charged."
CHARGE_NOW_NOTHING_DUE = "Nothing is owed on this charge. Nothing was charged."
CHARGE_NOW_HAS_PAYMENT = "A payment on this charge is pending or settled. Nothing was charged."
CHARGE_NOW_RETURNED = (
    "A payment on this charge was returned: Charge now does not debit again. Nothing was charged."
)
CHARGE_NOW_NOT_LINKED = (
    "The charge's invoice is not at the payment provider yet. Charge now is possible once it "
    "is linked."
)
CHARGE_NOW_TOTAL_DIFFERS = (
    "The invoice total differs from the price. Nothing was charged; review the invoice."
)
CHARGE_NOW_NO_METHOD = "The operator has no linked bank method. Nothing was charged."
CHARGE_NOW_METHOD_NOT_STORED = (
    "The stored list of the operator's payment methods does not show the linked bank method "
    "as a usable bank account. Refresh the payment methods on the operator's page first. "
    "Nothing was charged."
)
CHARGE_NOW_BUSY = (
    "A request to the payment provider is already waiting for this charge. Nothing was charged."
)
CHARGE_NOW_WAIT = "The last attempt failed. Charge now is possible again in {seconds} seconds."
CHARGE_NOW_ONE_ATTEMPT = "One attempt. If it fails you will see why and can try again later."
CHARGE_NOW_REQUESTED = "Charge requested: one attempt is made. This page shows the outcome."

CREATED_ACTION = "one_off_charge.created"
PAYMENT_ACTION = "one_off_charge.payment_recorded"
LARGE_DEBIT_ACTION = "one_off_charge.large_debit_confirmed"
CANCEL_ACTION = "one_off_charge.cancel_requested"
VOIDED_ACTION = "one_off_charge.voided"
WAIVED_INVOICE_DELETE_ACTION = "one_off_charge.waived_invoice_delete_requested"
WAIVED_INVOICE_DELETED_ACTION = "one_off_charge.waived_invoice_deleted"
#: The ``invoice_delete`` job parameter that makes it the remedy of a waived charge.
WAIVED_INVOICE_PARAM = "waived"
#: The ``invoice_delete`` or ``schedule_delete`` job parameter that makes it the release
#: of a split parent's provider object (PLAN-012b-a D3): deleted there, the charge kept.
RELEASE_PARAM = "release"
#: The audit action of a split parent's invoice released at the provider (D3).
INVOICE_RELEASED_ACTION = "invoice.released_for_installments"
#: The audit action of a split parent's schedule released at the provider (D3).
SCHEDULE_RELEASED_ACTION = "one_off_charge.schedule_released_for_installments"
#: What the charge page says of a released invoice (D5).
INVOICE_RELEASED = "Invoice {number} was released at the payment provider for the installment plan."
#: The ``invoice_create`` job parameter of an invoice builder invoice charged now
#: (PLAN-012 D6): once the invoice is at the provider, the worker queues the single
#: ``invoice_pay`` attempt.
CHARGE_NOW_PARAM = "charge_now"
CHARGE_NOW_REQUESTED_ACTION = "one_off_charge.charge_now_requested"
CHARGED_ACTION = "one_off_charge.charged"
CHARGE_FAILED_ACTION = "one_off_charge.charge_failed"
#: A payment of a first line (or re-debit) paid a later line (or the fee) (PLAN-012 D6).
ALLOCATED_ACTION = "one_off_charge.payment_allocated"
#: Audit actions that flag a charge for a provider-side reason (D3, D5).
SEVERAL_ACTION = "one_off_charge.provider_invoice_ambiguous"
OTHER_OPERATOR_ACTION = "one_off_charge.reference_other_operator"
SECOND_INVOICE_ACTION = "one_off_charge.second_invoice"


# --- Values and outcomes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChargeInput:
    """One one-off charge as staff entered it (a checklist row, the add form or the
    one-off form). ``fee_type`` is set for category ``setup`` only; ``description`` is
    required for the others. ``confirmed`` is the large-debit confirmation (D2)."""

    fee_type: FeeType | None
    price: Decimal
    cost: Decimal | None
    vendor: str
    due_date: date
    note: str = ""
    category: str = OneOffCategory.SETUP
    collection: str = CollectionMethod.NONE
    description: str = ""
    confirmed: bool = False
    #: The form's one-time key: a resubmitted form returns the charge the first made.
    creation_key: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    #: The form field a refusal belongs to (``None``: the whole form).
    field: str | None = None
    charge: OneOffCharge | None = None
    job: Job | None = None


class SetupRefused(Exception):  # noqa: N818 (reads as an outcome, like LeaseRefused)
    """A checklist row broke a rule; the whole save is rolled back."""

    def __init__(self, field: str | None, message: str, fee_type: FeeType | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.message = message
        self.fee_type = fee_type


# --- Reads ----------------------------------------------------------------------------


def checklist_types() -> list[FeeType]:
    """The active fee types, in catalogue order: one checklist row each (D5). The NSF fee
    types are never offered, even when an admin made them active (PLAN-011b D5)."""
    return list(
        FeeType.objects.filter(active=True)
        .exclude(code__in=NSF_FEE_CODES)
        .order_by("sort_order", "name", "id")
    )


def charges_of(lease: Lease) -> list[OneOffCharge]:
    return list(
        OneOffCharge.objects.filter(lease=lease)
        .select_related("fee_type", "lease", "deposit_resolution")
        .order_by("fee_type__sort_order", "fee_type__name", "due_date", "created_at", "id")
    )


def operator_charges(operator: Operator) -> list[OneOffCharge]:
    """Every one-off charge billed to the operator, newest due first."""
    return list(
        OneOffCharge.objects.filter(operator=operator)
        .select_related("fee_type", "lease__store")
        .order_by("-due_date", "-created_at", "id")
    )


def has_payment(charge: OneOffCharge) -> bool:
    return Payment.objects.filter(one_off_charge=charge).exists()


def has_settled_payment(charge: OneOffCharge) -> bool:
    return Payment.objects.filter(
        one_off_charge=charge,
        kind=PaymentKind.ONE_OFF,
        is_refund=False,
        status=PaymentStatus.SETTLED,
    ).exists()


def has_pending_payment(charge: OneOffCharge) -> bool:
    return Payment.objects.filter(one_off_charge=charge, status=PaymentStatus.PENDING).exists()


def provider_schedule(charge: OneOffCharge) -> str:
    """The provider schedule ID of an ``autopay_due`` charge, or ``""`` (unmasked)."""
    return (
        ExternalRef.objects.filter(
            entity_type=ENTITY_ONE_OFF_CHARGE,
            entity_id=charge.pk,
            provider=leases.PROVIDER_KEY,
            external_kind=KIND_SCHEDULE,
            superseded_at__isnull=True,
        )
        .values_list("external_id", flat=True)
        .first()
        or ""
    )


def invoice_is_live(invoice: Invoice) -> bool:
    """Whether a linked invoice is still live at the provider as far as we know: not
    ``void`` here and not last seen ``DELETED`` there (PLAN-011 D0)."""
    deleted = str(invoice.provider_status).strip().upper() == invoice_sync.PROVIDER_DELETED
    return invoice.internal_status != InvoiceStatus.VOID and not deleted


def invoices_of(charge: OneOffCharge) -> QuerySet[Invoice]:
    """The charge's invoices, by issue date and number: those linked to it and the
    builder invoice whose line item names it (PLAN-012c-a D1)."""
    return rent_run.charge_invoices(charge)


def live_provider_invoice(charge: OneOffCharge) -> tuple[Invoice, str] | None:
    """The charge's invoice with a current provider reference when it is live, and that
    reference; ``None`` otherwise (PLAN-011 D0)."""
    linked = rent_run.provider_invoice(charge)
    if linked is None or not invoice_is_live(linked[0]):
        return None
    return linked


def waived_with_live_invoice() -> list[OneOffCharge]:
    """Waived charges whose invoice is still live at the provider (one query; PLAN-011
    D0): the dashboard lists them until the remedy has cleared each."""
    current_ref = ExternalRef.objects.filter(
        entity_type=rent_run.ENTITY_INVOICE,
        entity_id=OuterRef("pk"),
        provider=leases.PROVIDER_KEY,
        external_kind=rent_run.KIND_INVOICE,
        superseded_at__isnull=True,
    )
    live = (
        Invoice.objects.filter(
            Q(one_off_charge=OuterRef("pk")) | Q(line_items__one_off_charge=OuterRef("pk"))
        )
        .exclude(internal_status=InvoiceStatus.VOID)
        .annotate(shown=Upper(Trim("provider_status")))
        .exclude(shown=invoice_sync.PROVIDER_DELETED)
        .filter(Exists(current_ref))
    )
    return list(
        OneOffCharge.objects.filter(status=OneOffChargeStatus.WAIVED)
        .filter(Exists(live))
        .select_related("operator", "lease__store", "lease__operator", "fee_type")
        .order_by("waived_at", "id")
    )


def _flagged_by(charge: OneOffCharge, action: str) -> bool:
    return AuditLog.objects.filter(
        entity_type=ENTITY_ONE_OFF_CHARGE, entity_id=str(charge.pk), action=action
    ).exists()


def review_message(charge: OneOffCharge) -> str:
    """Why a flagged charge needs review, or ``""`` when it is not flagged."""
    if not charge.needs_review:
        return ""
    parts = []
    # A later line's invoice bills its first line's total (PLAN-012c-a D1).
    if invoices_of(charge).exclude(total=billed_total(billed_with(charge) or charge)).exists():
        parts.append(TOTAL_MISMATCH)
    if _flagged_by(charge, ONE_OFF.action("linked_total_differs")):
        parts.append(rent_run.LINKED_TOTAL_DIFFERS)
    if charge.review_message:
        parts.append(f"{offline_marking.shown(charge.review_message)}.")
    if charge.payments.filter(status=PaymentStatus.RETURNED).exists():
        parts.append(RETURNED)
    if _flagged_by(charge, ONE_OFF.action("disputed")):
        parts.append(DISPUTED)
    for action, text in (
        (SEVERAL_ACTION, SEVERAL_PROVIDER_INVOICES),
        (OTHER_OPERATOR_ACTION, OTHER_OPERATOR_REFERENCE),
        (SECOND_INVOICE_ACTION, SECOND_PROVIDER_INVOICE),
    ):
        if _flagged_by(charge, action):
            parts.append(text)
    return " ".join(parts)


# --- Validation -----------------------------------------------------------------------


def _money_refusal(value: Decimal | None, *, field: str, positive: bool) -> Outcome | None:
    if value is None:
        return Outcome(False, PRICE_NOT_POSITIVE, field) if positive else None
    if not isinstance(value, Decimal) or not value.is_finite():
        return Outcome(False, PRICE_NOT_POSITIVE if positive else COST_NEGATIVE, field)
    if positive and value <= 0:
        return Outcome(False, PRICE_NOT_POSITIVE, field)
    if not positive and value < 0:
        return Outcome(False, COST_NEGATIVE, field)
    if value != value.quantize(CENT):
        return Outcome(False, NOT_CENTS, field)
    if value > MONEY_MAX:
        return Outcome(False, TOO_LARGE, field)
    return None


def _text_refusal(text: str, field: str) -> Outcome | None:
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return Outcome(False, TEXT_PAYMENT_DATA, field)
    return None


def refusal(item: ChargeInput) -> Outcome | None:
    """The first rule a setup ``item`` breaks before anything is written, or ``None``."""
    return (
        _money_refusal(item.price, field="price", positive=True)
        or _money_refusal(item.cost, field="cost", positive=False)
        or _text_refusal(item.vendor, "vendor")
        or _text_refusal(item.note, "note")
    )


def _amount_refusal(value: Decimal) -> Outcome | None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        return Outcome(False, AMOUNT_NOT_POSITIVE, "price")
    if value != value.quantize(CENT):
        return Outcome(False, NOT_CENTS, "price")
    if value > AMOUNT_MAX:
        return Outcome(False, AMOUNT_TOO_LARGE, "price")
    return None


def _lease_ended(lease: Lease, today: date) -> bool:
    return lease.collection_status == CollectionStatus.ENDED or (
        lease.ended_on is not None and lease.ended_on <= today
    )


def one_off_refusal(
    operator: Operator | None,
    lease: Lease | None,
    item: ChargeInput,
    actor: User | None,
    *,
    today: date | None = None,
) -> Outcome | None:
    """The first rule a one-off ``item`` (any category but ``setup``) breaks before
    anything is written, or ``None`` (D2). Reads local data only."""
    day = today or timezone.localdate()
    if operator is None:
        return Outcome(False, NO_OPERATOR, "operator")
    if operator.archived_at is not None:
        return Outcome(False, OPERATOR_ARCHIVED)
    if lease is not None and lease.operator_id != operator.pk:
        return Outcome(False, LEASE_OTHER_OPERATOR, "lease")
    if item.category not in FORM_CATEGORIES and not _billed_setup(item):
        return Outcome(False, CHOOSE_CATEGORY, "category")
    if item.collection not in FORM_COLLECTIONS:
        return Outcome(False, CHOOSE_COLLECTION, "collection")
    description = visible_text(item.description or "")
    if not description:
        return Outcome(False, DESCRIPTION_REQUIRED, "description")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        return Outcome(False, DESCRIPTION_TOO_LONG, "description")
    note = visible_text(item.note or "")
    if not note:
        return Outcome(False, NOTE_REQUIRED, "note")
    refused = (
        _text_refusal(description, "description")
        or _text_refusal(note, "note")
        or _amount_refusal(item.price)
    )
    if refused is not None:
        return refused
    if item.collection == CollectionMethod.CHARGE_NOW and not (
        actor is not None and meets(role_of(actor), Role.ADMIN)
    ):
        return Outcome(False, CHARGE_NOW_ADMIN, "collection")
    if item.collection == CollectionMethod.AUTOPAY_DUE:
        if item.due_date < day:
            return Outcome(False, DUE_IN_PAST, "due_date")
        if lease is not None and _lease_ended(lease, day):
            return Outcome(False, LEASE_ENDED, "collection")
    if item.collection != CollectionMethod.MANUAL and not leases.customer_ref(operator):
        return Outcome(False, NO_PROVIDER_CUSTOMER, "collection")
    if item.collection in DEBIT_COLLECTIONS:
        if not leases.linked_payment_method(operator):
            return Outcome(False, NO_BANK_METHOD, "collection")
        limit = leases.provider_limit_refusal([item.price])
        if limit:
            return Outcome(False, limit, "price")
        if leases.is_large_debit(item.price) and item.confirmed is not True:
            return Outcome(False, leases.large_debit_confirmation([item.price]), "confirm")
    return None


def _billed_setup(item: ChargeInput) -> bool:
    """A setup item billed on an invoice (a builder line with a fee type that is not a
    category, PLAN-012 D6): it has its fee type and is sent by e-mail or charged now."""
    return (
        item.category == OneOffCategory.SETUP
        and item.fee_type is not None
        and item.collection in (CollectionMethod.EMAIL, CollectionMethod.CHARGE_NOW)
    )


# --- Creation (PLAN-010b D5, PLAN-010d D2) ------------------------------------------


def create_lease_with_checklist(
    fields: Mapping[str, Any],
    *,
    rent: Decimal,
    creation_key: uuid.UUID,
    items: Sequence[ChargeInput],
    actor: User | None,
) -> tuple[Lease, bool, list[OneOffCharge]]:
    """Save a new lease and the ticked setup charges in one transaction (D5).

    A resubmitted form (same ``creation_key``) returns the lease the first one made and
    creates nothing. A refused row raises :class:`SetupRefused` and nothing is saved,
    the lease included.
    """
    with transaction.atomic():
        lease, created = leases.create_lease(
            fields, rent=rent, creation_key=creation_key, actor=actor
        )
        if not created:
            return lease, False, []
        charges = create_from_checklist(lease, items, actor=actor)
    return lease, True, charges


def create_from_checklist(
    lease: Lease, items: Sequence[ChargeInput], *, actor: User | None
) -> list[OneOffCharge]:
    """Create one setup charge per item, all or none (D5); raises :class:`SetupRefused`."""
    made: list[OneOffCharge] = []
    with transaction.atomic():
        for item in items:
            outcome = create_charge(lease.operator, lease, item, actor)
            if not outcome.ok or outcome.charge is None:
                raise SetupRefused(outcome.field, outcome.message, item.fee_type)
            made.append(outcome.charge)
    return made


def add_charge(lease_id: uuid.UUID, item: ChargeInput, *, actor: User | None) -> Outcome:
    """Add one setup charge to a lease (the lease page's form, billing)."""
    name = item.fee_type.name if item.fee_type is not None else ""
    try:
        with transaction.atomic():
            lease = Lease.objects.select_for_update().get(pk=lease_id)
            outcome = create_charge(lease.operator, lease, item, actor)
            if not outcome.ok:
                transaction.set_rollback(True)
            return outcome
    except IntegrityError:
        # A concurrent add of the same fee type committed first.
        return Outcome(False, ALREADY_ON_LEASE.format(name=name), "fee_type")


def create_charge(
    operator: Operator | None,
    lease: Lease | None,
    item: ChargeInput,
    actor: User | None,
    *,
    today: date | None = None,
    with_invoice: bool = True,
) -> Outcome:
    """Create one one-off charge (the single creation path, PLAN-010d D1, D2).

    Category ``setup`` with collection ``none`` follows PLAN-010b (``lease`` required,
    no invoice, no job); ``operator`` must then be the lease's. Every other item is
    checked by :func:`one_off_refusal` and written with its invoice and its job in one
    transaction; with ``with_invoice=False`` (a line of an invoice builder invoice,
    PLAN-012 D6) the charge alone is written and the caller makes the invoice and the
    job of all its lines.
    """
    if item.category == OneOffCategory.SETUP and item.collection == CollectionMethod.NONE:
        if lease is None or operator != lease.operator:
            return Outcome(False, LEASE_OTHER_OPERATOR, "lease")
        return _create_setup(lease, item, actor)
    refused = one_off_refusal(operator, lease, item, actor, today=today)
    if refused is not None or operator is None:
        return refused or Outcome(False, NO_OPERATOR, "operator")
    return _create_one_off(
        operator, lease, item, actor, today or timezone.localdate(), with_invoice=with_invoice
    )


def _create_setup(lease: Lease, item: ChargeInput, actor: User | None) -> Outcome:
    refused = refusal(item)
    if refused is not None:
        return refused
    if item.collection != CollectionMethod.NONE or item.fee_type is None:
        return Outcome(False, FEE_TYPE_INACTIVE, "fee_type")
    fee_type = FeeType.objects.filter(pk=item.fee_type.pk, active=True).first()
    if fee_type is None:
        return Outcome(False, FEE_TYPE_INACTIVE, "fee_type")
    taken = OneOffCharge.objects.filter(lease=lease, fee_type=fee_type, legacy=False)
    if taken.exists():
        return Outcome(False, ALREADY_ON_LEASE.format(name=fee_type.name), "fee_type")
    charge = OneOffCharge(
        operator_id=lease.operator_id,
        lease=lease,
        fee_type=fee_type,
        category=OneOffCategory.SETUP,
        collection=CollectionMethod.NONE,
        description=fee_type.name[:DESCRIPTION_MAX_LENGTH],
        cost=item.cost,
        price=item.price,
        vendor=item.vendor.strip(),
        due_date=item.due_date,
        note=item.note.strip(),
        status=OneOffChargeStatus.OWED,
    )
    try:
        with transaction.atomic():
            charge.save()
    except ValidationError:
        return Outcome(False, TEXT_PAYMENT_DATA, "vendor")
    except IntegrityError:
        return Outcome(False, ALREADY_ON_LEASE.format(name=fee_type.name), "fee_type")
    _audit(actor, CREATED_ACTION, charge, None, charge_state(charge))
    return Outcome(True, ADDED.format(name=fee_type.name), charge=charge)


def _create_one_off(
    operator: Operator,
    lease: Lease | None,
    item: ChargeInput,
    actor: User | None,
    today: date,
    *,
    with_invoice: bool = True,
) -> Outcome:
    collection = CollectionMethod(item.collection)
    has_customer = bool(leases.customer_ref(operator))
    with transaction.atomic():
        locked = Operator.objects.select_for_update().get(pk=operator.pk)
        if locked.archived_at is not None:
            return Outcome(False, OPERATOR_ARCHIVED)
        earlier = _charge_for_key(item.creation_key)
        if earlier is not None:
            return Outcome(True, ALREADY_CREATED, charge=earlier)
        fee_type = None
        if item.category == OneOffCategory.SETUP and item.fee_type is not None:
            fee_type = FeeType.objects.filter(pk=item.fee_type.pk, active=True).first()
            if fee_type is None:
                return Outcome(False, FEE_TYPE_INACTIVE, "fee_type")
            taken = OneOffCharge.objects.filter(lease=lease, fee_type=fee_type, legacy=False)
            if lease is not None and taken.exists():
                return Outcome(False, ALREADY_ON_LEASE.format(name=fee_type.name), "fee_type")
        charge = OneOffCharge(
            operator=locked,
            lease=lease,
            fee_type=fee_type,
            category=item.category,
            collection=collection,
            description=visible_text(item.description),
            price=item.price,
            due_date=item.due_date,
            note=visible_text(item.note),
            status=OneOffChargeStatus.OWED,
        )
        try:
            with transaction.atomic():
                charge.save()
        except ValidationError:
            return Outcome(False, TEXT_PAYMENT_DATA, "description")
        except IntegrityError:
            # A concurrent charge of this fee type on the lease committed first.
            name = fee_type.name if fee_type is not None else ""
            return Outcome(False, ALREADY_ON_LEASE.format(name=name), "fee_type")
        if not with_invoice:
            _audit(
                actor,
                CREATED_ACTION,
                charge,
                None,
                {**charge_state(charge), "note": charge.note, "invoice_number": None},
            )
            return Outcome(True, "", charge=charge)
        invoice = None
        if collection in INVOICED_COLLECTIONS:
            invoice = Invoice.objects.create(
                invoice_number=numbering.next_invoice_number(today.year),
                operator=locked,
                one_off_charge=charge,
                issue_date=today,
                due_date=max(item.due_date, today),
                memo=charge.description,
                subtotal=item.price,
                tax=ZERO,
                fees=ZERO,
                surcharge=ZERO,
                total=item.price,
                internal_status=InvoiceStatus.DRAFT,
                allow_card=False,
                allow_ach=True,
                legacy=False,
            )
        _audit(
            actor,
            CREATED_ACTION,
            charge,
            None,
            {
                **charge_state(charge),
                "invoice_number": invoice.invoice_number if invoice is not None else None,
                "note": charge.note,
                "creation_key": str(item.creation_key) if item.creation_key else None,
            },
        )
        if collection in DEBIT_COLLECTIONS and leases.is_large_debit(item.price):
            _audit(
                actor,
                LARGE_DEBIT_ACTION,
                charge,
                None,
                {
                    "amount": str(item.price),
                    "threshold": str(settings.LARGE_DEBIT_WARNING_AMOUNT),
                    "confirmed_by": actor.pk if actor is not None else None,
                },
            )
        job = None
        if collection == CollectionMethod.AUTOPAY_DUE:
            job = jobs.enqueue(JobKind.SCHEDULE_CREATE, charge, None, actor)
            message = CREATED_NO_INVOICE.format(date=item.due_date.isoformat())
        elif invoice is not None and (has_customer or collection != CollectionMethod.MANUAL):
            job = jobs.enqueue(JobKind.INVOICE_CREATE, charge, None, actor)
            message = CREATED.format(number=invoice.invoice_number)
        else:
            number = invoice.invoice_number if invoice is not None else ""
            message = CREATED_MANUAL_LOCAL.format(number=number)
    return Outcome(True, message, charge=charge, job=job)


def _charge_for_key(creation_key: uuid.UUID | None) -> OneOffCharge | None:
    """The charge a form with this one-time key already created, if any. Read under the
    operator's row lock, which every one-off creation of that operator takes."""
    if creation_key is None:
        return None
    row = AuditLog.objects.filter(
        action=CREATED_ACTION,
        entity_type=ENTITY_ONE_OFF_CHARGE,
        after__creation_key=str(creation_key),
    ).first()
    return OneOffCharge.objects.filter(pk=row.entity_id).first() if row is not None else None


# --- Edit and waive (D6) --------------------------------------------------------------


def edit_charge(
    charge_id: uuid.UUID,
    *,
    cost: Decimal | None,
    price: Decimal,
    vendor: str,
    due_date: date,
    actor: User | None,
) -> Outcome:
    """Change cost, price, vendor or due date; the price is locked once a payment exists."""
    vendor = (vendor or "").strip()
    refused = (
        _money_refusal(price, field="price", positive=True)
        or _money_refusal(cost, field="cost", positive=False)
        or _text_refusal(vendor, "vendor")
    )
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = OneOffCharge.objects.select_for_update().get(pk=charge_id)
        if charge.status == OneOffChargeStatus.WAIVED:
            return Outcome(False, EDIT_WAIVED)
        if charge.status == OneOffChargeStatus.VOIDED:
            return Outcome(False, EDIT_VOIDED)
        if charge.category != OneOffCategory.SETUP or charge.collection != CollectionMethod.NONE:
            return Outcome(False, EDIT_ONE_OFF)
        if (charge.cost, charge.price, charge.vendor, charge.due_date) == (
            cost,
            price,
            vendor,
            due_date,
        ):
            return Outcome(True, UNCHANGED, charge=charge)
        if price != charge.price and has_payment(charge):
            return Outcome(False, PRICE_LOCKED, "price")
        before = charge_state(charge)
        charge.cost = cost
        charge.price = price
        charge.vendor = vendor
        charge.due_date = due_date
        # No payment exists when the price changes, so the status stays what it is.
        charge.save(update_fields=["cost", "price", "vendor", "due_date", "updated_at"])
        _audit(actor, "one_off_charge.edited", charge, before, charge_state(charge))
    return Outcome(True, SAVED, charge=charge)


def waive(
    charge_id: uuid.UUID, *, note: str, actor: User | None, now: datetime | None = None
) -> Outcome:
    """Waive a one-off charge (admin): a note is mandatory; refused once a payment settled,
    while the charge has a live provider invoice (PLAN-011 D0) and while it is the parent
    of a live installment plan (PLAN-012b-a)."""
    text = visible_text(note or "")
    if not text:
        return Outcome(False, WAIVE_NOTE_REQUIRED, "note")
    refused = _text_refusal(text, "note")
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = OneOffCharge.objects.select_for_update().get(pk=charge_id)
        if charge.status == OneOffChargeStatus.WAIVED:
            return Outcome(False, WAIVE_ALREADY)
        if charge.status == OneOffChargeStatus.VOIDED:
            return Outcome(False, WAIVE_VOIDED)
        if charge.category == OneOffCategory.INSTALLMENT:
            return Outcome(False, WAIVE_INSTALLMENT)
        if rent_run.on_live_plan(one_off_charge=charge):
            return Outcome(False, WAIVE_PARENT_ON_PLAN)
        if has_settled_payment(charge) or charge.amount_paid > 0:
            return Outcome(False, WAIVE_SETTLED)
        # A companion fee is billed on its re-debit's invoice (PLAN-011b D5), a later line
        # on its first line's (PLAN-012 D6).
        billed_on = billed_with(charge) or charge
        if live_provider_invoice(billed_on) is not None:
            return Outcome(False, WAIVE_LIVE_INVOICE)
        before = charge_state(charge)
        charge.status = OneOffChargeStatus.WAIVED
        charge.waived_at = now or timezone.now()
        charge.waived_by = actor
        charge.waive_note = text
        charge.save(update_fields=["status", "waived_at", "waived_by", "waive_note", "updated_at"])
        _audit(
            actor, "one_off_charge.waived", charge, before, {**charge_state(charge), "note": text}
        )
    return Outcome(True, WAIVED, charge=charge)


# --- Cancel (PLAN-010d D6) ------------------------------------------------------------


def cancel_refusal(charge: OneOffCharge) -> str:
    """Why the charge cannot be cancelled now, or ``""``. Local data only."""
    if charge.status == OneOffChargeStatus.VOIDED:
        return CANCEL_VOIDED
    if charge.collection == CollectionMethod.NONE:
        return CANCEL_SETUP
    if (
        charge.plan_id is not None
        and InstallmentPlan.objects.filter(
            pk=charge.plan_id, status__in=LIVE_PLAN_STATUSES
        ).exists()
    ):
        return CANCEL_INSTALLMENT
    if rent_run.on_live_plan(one_off_charge=charge):
        return CANCEL_PARENT_ON_PLAN
    if charge.companion_of_id is not None:
        return CANCEL_COMPANION
    if is_later_line(charge):
        return CANCEL_LINE
    if charge.status == OneOffChargeStatus.WAIVED:
        return CANCEL_WAIVED
    lines = [charge, *_later_lines(charge)]
    if any(line.amount_paid > 0 or has_settled_payment(line) for line in lines):
        return CANCEL_PAID
    if any(has_pending_payment(line) for line in lines):
        return CANCEL_PENDING
    return ""


def cancel_job_kind(charge: OneOffCharge) -> str:
    """``schedule_delete`` for an ``autopay_due`` charge whose schedule has not
    generated its invoice yet; ``invoice_delete`` otherwise."""
    if (
        charge.collection == CollectionMethod.AUTOPAY_DUE
        and not invoices_of(charge).exists()
        and provider_schedule(charge)
    ):
        return JobKind.SCHEDULE_DELETE
    return JobKind.INVOICE_DELETE


def cancel_charge(charge_id: uuid.UUID, *, note: str, actor: User | None) -> Outcome:
    """Request the cancel of an unpaid one-off charge (billing; D6).

    The note is required and stored with the request; the delete is queued for the
    worker, which voids the charge only after the provider confirmed. Refused, with
    nothing queued, when anything is paid, a payment is pending, the charge is already
    voided or waived, it is a setup charge, or a provider request is already waiting.
    """
    text = visible_text(note or "")
    if not text:
        return Outcome(False, VOID_NOTE_REQUIRED, "note")
    refused = _text_refusal(text, "note")
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = OneOffCharge.objects.select_for_update().get(pk=charge_id)
        reason = cancel_refusal(charge)
        if reason:
            return Outcome(False, reason)
        if jobs.is_active(jobs.latest_for(charge)):
            return Outcome(False, CANCEL_BUSY)
        kind = cancel_job_kind(charge)
        _audit(
            actor,
            CANCEL_ACTION,
            charge,
            charge_state(charge),
            {**charge_state(charge), "note": text, "job_kind": kind},
        )
        job = jobs.enqueue(kind, charge, None, actor)
    return Outcome(True, CANCEL_REQUESTED, charge=charge, job=job)


# --- Deleting a waived charge's invoice (PLAN-011 D0) ---------------------------------


def delete_invoice_refusal(charge: OneOffCharge) -> str:
    """Why the invoice of the charge cannot be deleted by the remedy now, or ``""``."""
    if charge.status != OneOffChargeStatus.WAIVED:
        return DELETE_INVOICE_NOT_WAIVED
    linked = live_provider_invoice(charge)
    if linked is None:
        return DELETE_INVOICE_NONE
    if any(
        line.status not in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)
        for line in invoice_charges(linked[0])
    ):
        # A builder invoice billing other lines too (PLAN-012c-a D1).
        return DELETE_INVOICE_SHARED
    return ""


def delete_waived_invoice(charge_id: uuid.UUID, *, actor: User | None) -> Outcome:
    """Request the delete of a waived charge's live provider invoice (admin; D0).

    Queues ``invoice_delete`` with :data:`WAIVED_INVOICE_PARAM`; the charge's status is
    never changed. Refused, with nothing queued, when the charge is not waived, has no
    live provider invoice, or a provider request is already waiting.
    """
    with transaction.atomic():
        charge = OneOffCharge.objects.select_for_update().get(pk=charge_id)
        reason = delete_invoice_refusal(charge)
        if reason:
            return Outcome(False, reason)
        if jobs.is_active(jobs.latest_for(charge)):
            return Outcome(False, CANCEL_BUSY)
        _audit(
            actor,
            WAIVED_INVOICE_DELETE_ACTION,
            charge,
            charge_state(charge),
            charge_state(charge),
        )
        job = jobs.enqueue(JobKind.INVOICE_DELETE, charge, {WAIVED_INVOICE_PARAM: True}, actor)
    return Outcome(True, DELETE_INVOICE_REQUESTED, charge=charge, job=job)


def mark_waived_invoice_deleted(
    charge_id: uuid.UUID, *, actor: User | None, context: Mapping[str, Any]
) -> Outcome:
    """Mark the waived charge's invoice void once the provider confirmed its delete (D0;
    the worker only). The charge stays ``waived``; audited
    :data:`WAIVED_INVOICE_DELETED_ACTION`."""
    with transaction.atomic():
        charge = OneOffCharge.objects.select_for_update().get(pk=charge_id)
        if charge.status != OneOffChargeStatus.WAIVED:
            return Outcome(False, DELETE_INVOICE_NOT_WAIVED)
        before = charge_state(charge)
        invoices_of(charge).exclude(internal_status=InvoiceStatus.VOID).update(
            internal_status=InvoiceStatus.VOID, updated_at=timezone.now()
        )
        _audit(actor, WAIVED_INVOICE_DELETED_ACTION, charge, before, {**before, **context})
    return Outcome(True, WAIVED_INVOICE_DELETED, charge=charge)


# --- Releasing a split parent's provider object (PLAN-012b-a D3, D5) -------------------


def mark_invoice_released(
    invoice: Invoice, *, actor: User | None, context: Mapping[str, Any]
) -> None:
    """Mark a split parent's invoice void once the provider confirmed its delete (D3; the
    worker only, under the parent's row lock). The charge is not touched: it stays owed
    at its price. Audited :data:`INVOICE_RELEASED_ACTION` on the invoice."""
    before = {
        "internal_status": invoice.internal_status,
        "provider_status": invoice.provider_status,
    }
    Invoice.objects.filter(pk=invoice.pk).update(
        internal_status=InvoiceStatus.VOID, updated_at=timezone.now()
    )
    invoice.refresh_from_db(fields=["internal_status", "provider_status"])
    AuditLog.objects.create(
        actor=actor,
        action=INVOICE_RELEASED_ACTION,
        entity_type=rent_run.ENTITY_INVOICE,
        entity_id=str(invoice.pk),
        before=before,
        after={
            "internal_status": invoice.internal_status,
            "provider_status": invoice.provider_status,
            "invoice_number": invoice.invoice_number,
            **context,
        },
    )


def mark_schedule_released(
    charge: OneOffCharge, *, actor: User | None, context: Mapping[str, Any]
) -> None:
    """Supersede a split parent's schedule reference once the provider confirmed its
    delete, as the cancel path does (D3; the worker only, under the parent's row lock).
    The charge stays owed at its price. Audited :data:`SCHEDULE_RELEASED_ACTION`."""
    ExternalRef.objects.filter(
        entity_type=ENTITY_ONE_OFF_CHARGE,
        entity_id=charge.pk,
        external_kind=KIND_SCHEDULE,
        superseded_at__isnull=True,
    ).update(superseded_at=timezone.now())
    state = charge_state(charge)
    _audit(actor, SCHEDULE_RELEASED_ACTION, charge, state, {**state, **context})


def released_invoices(charge: OneOffCharge) -> list[str]:
    """What the charge page says of the charge's invoices released for an installment
    plan (D5), one sentence each, by invoice number."""
    invoices = {str(invoice.pk): invoice.invoice_number for invoice in invoices_of(charge)}
    released = AuditLog.objects.filter(
        action=INVOICE_RELEASED_ACTION,
        entity_type=rent_run.ENTITY_INVOICE,
        entity_id__in=list(invoices),
    ).values_list("entity_id", flat=True)
    numbers = sorted({invoices[entity_id] for entity_id in released})
    return [INVOICE_RELEASED.format(number=number) for number in numbers]


# --- Charge now (PLAN-010d D4) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChargeNowDetails:
    """What the Charge now confirmation page shows, from local data only.

    ``refusal`` is the sentence of the first rule the charge breaks now (``""``: it may
    be charged); ``large_debit`` the confirmation sentence when the amount is above the
    warning threshold; ``wait_seconds`` the seconds left before a failed attempt may be
    repeated (0: now)."""

    refusal: str
    operator: Operator | None
    store_label: str
    amount: Decimal
    invoice_number: str
    last_four: str
    nickname: str
    large_debit: str
    wait_seconds: int


def charge_now_enabled() -> bool:
    """The pay-an-invoice flag (:func:`payments.pay_invoice_enabled`): off, nothing is
    queued and the provider is never reached (D4)."""
    return payments.pay_invoice_enabled()


def _charge_operator(charge: OneOffCharge) -> Operator | None:
    if charge.operator is not None:
        return charge.operator
    return charge.lease.operator if charge.lease is not None else None


def _last_charge_now_event(charge: OneOffCharge) -> AuditLog | None:
    return (
        AuditLog.objects.filter(
            entity_type=ENTITY_ONE_OFF_CHARGE,
            entity_id=str(charge.pk),
            action__in=(CHARGE_NOW_REQUESTED_ACTION, CHARGED_ACTION, CHARGE_FAILED_ACTION),
        )
        .order_by("-id")
        .first()
    )


def charge_now_wait(charge: OneOffCharge, now: datetime | None = None) -> int:
    """Seconds until Charge now may be repeated after a failed attempt (0: now). The
    lock is the manual retry's (``RETRY_LOCK_SECONDS``, one minute)."""
    last = _last_charge_now_event(charge)
    if last is None or last.action != CHARGE_FAILED_ACTION:
        return 0
    return rent_run.retry_wait_after(last, now)


def charge_now_refusal(charge: OneOffCharge) -> str:
    """The first rule that stops Charge now on this charge, or ``""``. Local data only;
    the role, the large-debit box, a waiting job and the one-minute lock are checked by
    :func:`charge_now_confirm`, and the invoice's state at the provider by the worker."""
    if not charge_now_enabled():
        return CHARGE_NOW_DISABLED
    if (
        charge.collection != CollectionMethod.CHARGE_NOW
        or charge.category not in (*FORM_CATEGORIES, OneOffCategory.SETUP)
        or is_later_line(charge)
    ):
        return CHARGE_NOW_NOT_ELIGIBLE
    if charge.status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED):
        return CHARGE_NOW_CLOSED
    paid = charge.payments.filter(is_refund=False)
    if paid.filter(status__in=(PaymentStatus.PENDING, PaymentStatus.SETTLED)).exists():
        return CHARGE_NOW_HAS_PAYMENT
    if paid.filter(status=PaymentStatus.RETURNED).exists():
        return CHARGE_NOW_RETURNED
    billed = billed_total(charge)
    if billed - paid_in(charge) <= 0:
        return CHARGE_NOW_NOTHING_DUE
    linked = rent_run.provider_invoice(charge)
    if linked is None:
        return CHARGE_NOW_NOT_LINKED
    if linked[0].total != billed:
        return CHARGE_NOW_TOTAL_DIFFERS
    operator = _charge_operator(charge)
    if not leases.linked_payment_method(operator):
        return CHARGE_NOW_NO_METHOD
    method = leases.linked_method_record(operator)
    if method is None or not method.is_usable_bank:
        return CHARGE_NOW_METHOD_NOT_STORED
    return leases.provider_limit_refusal([billed])


def charge_now_request(charge: OneOffCharge, actor: User | None) -> ChargeNowDetails:
    """The confirmation page of Charge now (admin; D4). Never calls the provider."""
    operator = _charge_operator(charge)
    refusal = (
        CHARGE_NOW_ADMIN
        if actor is None or not meets(role_of(actor), Role.ADMIN)
        else charge_now_refusal(charge)
    )
    linked = rent_run.provider_invoice(charge)
    method = leases.linked_method_record(operator)
    amount = billed_total(charge)
    return ChargeNowDetails(
        refusal=refusal,
        operator=operator,
        store_label=charge.lease.store.external_label if charge.lease is not None else "",
        amount=amount,
        invoice_number=linked[0].invoice_number if linked is not None else "",
        last_four=method.last_four if method is not None else "",
        nickname=method.nickname if method is not None else "",
        large_debit=leases.large_debit_confirmation([amount]),
        wait_seconds=charge_now_wait(charge),
    )


def charge_now_confirm(charge_id: uuid.UUID, *, actor: User | None, confirmed: bool) -> Outcome:
    """Queue the single Charge now attempt (admin; D4): the charge's ``charge_now_key``
    is set if it has none (it never changes after), the request is audited and the
    ``invoice_pay`` job queued, under the charge's row lock, so two confirms queue one
    job."""
    if actor is None or not meets(role_of(actor), Role.ADMIN):
        return Outcome(False, CHARGE_NOW_ADMIN)
    with transaction.atomic():
        charge = (
            OneOffCharge.objects.select_for_update(of=("self",))
            .select_related("operator", "lease")
            .get(pk=charge_id)
        )
        refusal = charge_now_refusal(charge)
        if refusal:
            return Outcome(False, refusal)
        if jobs.is_active(jobs.latest_for(charge)):
            return Outcome(False, CHARGE_NOW_BUSY)
        wait = charge_now_wait(charge)
        if wait:
            return Outcome(False, CHARGE_NOW_WAIT.format(seconds=wait))
        amount = billed_total(charge)
        large = leases.is_large_debit(amount)
        if large and confirmed is not True:
            return Outcome(False, leases.large_debit_confirmation([amount]), "confirm")
        if charge.charge_now_key is None:
            charge.charge_now_key = uuid.uuid4()
            charge.save(update_fields=["charge_now_key", "updated_at"])
        linked = rent_run.provider_invoice(charge)
        _audit(
            actor,
            CHARGE_NOW_REQUESTED_ACTION,
            charge,
            None,
            {
                "amount": str(amount),
                "invoice_number": linked[0].invoice_number if linked is not None else None,
                "large_debit_confirmed": large,
            },
        )
        job = jobs.enqueue(JobKind.INVOICE_PAY, charge, {"confirmed": confirmed is True}, actor)
    return Outcome(True, CHARGE_NOW_REQUESTED, charge=charge, job=job)


def record_charge_now(
    charge_id: uuid.UUID, actor: User | None, action: str, context: Mapping[str, Any]
) -> None:
    """Audit the outcome of the worker's attempt (``charged`` or ``charge_failed``)."""
    charge = OneOffCharge.objects.get(pk=charge_id)
    _audit(actor, action, charge, None, dict(context))


#: The provider requests of a one-off charge that the page's single retry repeats (D3).
PROVIDER_JOB_KINDS: frozenset[str] = frozenset(
    {
        JobKind.INVOICE_CREATE,
        JobKind.INVOICE_SEND,
        JobKind.INVOICE_DELETE,
        JobKind.SCHEDULE_CREATE,
        JobKind.SCHEDULE_DELETE,
    }
)


def retry_kind(charge: OneOffCharge) -> str | None:
    """The job kind the manual retry queues, or ``None`` when there is nothing to retry:
    the charge's latest job is a failed provider request. A failed create whose invoice
    is at the provider but not sent retries the e-mail alone; a failed cancel retries
    the cancel only while the charge may still be cancelled."""
    job = jobs.latest_for(charge)
    if (
        job is None
        or job.kind not in PROVIDER_JOB_KINDS
        or job.status not in (JobStatus.FAILED, JobStatus.DEAD)
        or charge.status == OneOffChargeStatus.VOIDED
    ):
        return None
    if job.kind in (JobKind.INVOICE_DELETE, JobKind.SCHEDULE_DELETE):
        if job.params.get(RELEASE_PARAM) is True:
            # A failed release (PLAN-012b-a D3) is repeated as it was, never as a cancel.
            return str(job.kind)
        return None if cancel_refusal(charge) else cancel_job_kind(charge)
    if job.kind == JobKind.INVOICE_CREATE and charge.collection == CollectionMethod.EMAIL:
        linked = rent_run.provider_invoice(charge)
        if linked is not None and linked[0].internal_status != InvoiceStatus.SENT:
            return JobKind.INVOICE_SEND
    return str(job.kind)


def retry_params(charge: OneOffCharge) -> dict[str, bool] | None:
    """The parameters the manual retry queues with :func:`retry_kind`: the release flag
    of a failed release (PLAN-012b-a D3); ``None`` otherwise."""
    job = jobs.latest_for(charge)
    if (
        job is not None
        and job.kind in (JobKind.INVOICE_DELETE, JobKind.SCHEDULE_DELETE)
        and job.params.get(RELEASE_PARAM) is True
        and retry_kind(charge) is not None
    ):
        return {RELEASE_PARAM: True}
    return None


def cancel_note(charge: OneOffCharge) -> str:
    """The note of the latest cancel request of the charge, or ``""``."""
    row = (
        AuditLog.objects.filter(
            entity_type=ENTITY_ONE_OFF_CHARGE, entity_id=str(charge.pk), action=CANCEL_ACTION
        )
        .order_by("-id")
        .first()
    )
    after = row.after if row is not None else None
    return str(after.get("note", "")) if isinstance(after, dict) else ""


def mark_voided(
    charge_id: uuid.UUID,
    *,
    actor: User | None,
    context: Mapping[str, Any],
    now: datetime | None = None,
) -> Outcome:
    """Void the charge and its invoice once the provider confirmed the delete (D6; the
    worker only). The note is the cancel request's. Refused when a payment arrived."""
    with transaction.atomic():
        charge = OneOffCharge.objects.select_for_update().get(pk=charge_id)
        if charge.status == OneOffChargeStatus.VOIDED:
            return Outcome(True, VOIDED, charge=charge)
        reason = cancel_refusal(charge)
        if reason:
            return Outcome(False, reason)
        note = cancel_note(charge) or VOID_NOTE_REQUIRED
        before = charge_state(charge)
        charge.status = OneOffChargeStatus.VOIDED
        charge.voided_at = now or timezone.now()
        charge.voided_by = actor
        charge.void_note = note
        charge.save(update_fields=["status", "voided_at", "voided_by", "void_note", "updated_at"])
        invoices_of(charge).exclude(internal_status=InvoiceStatus.VOID).update(
            internal_status=InvoiceStatus.VOID, updated_at=timezone.now()
        )
        _audit(
            actor,
            VOIDED_ACTION,
            charge,
            before,
            {**charge_state(charge), "note": note, **context},
        )
        _void_companions(charge, actor, note, context, now)
    return Outcome(True, VOIDED, charge=charge)


def _void_companions(
    charge: OneOffCharge,
    actor: User | None,
    note: str,
    context: Mapping[str, Any],
    now: datetime | None,
) -> None:
    """The fee of a voided re-debit goes with it (PLAN-011b D5), and so do the later
    lines of a voided first line (PLAN-012 D6): their lines were on the deleted invoice
    or schedule. A waived or already voided one is left as it is."""
    for companion in _companion_rows(charge, lock=True):
        if companion.status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED):
            continue
        before = charge_state(companion)
        companion.status = OneOffChargeStatus.VOIDED
        companion.voided_at = now or timezone.now()
        companion.voided_by = actor
        companion.void_note = note
        companion.save(
            update_fields=["status", "voided_at", "voided_by", "void_note", "updated_at"]
        )
        _audit(
            actor,
            VOIDED_ACTION,
            companion,
            before,
            {**charge_state(companion), "note": note, "with": str(charge.pk), **context},
        )


def flag(charge_id: uuid.UUID, action: str, context: Mapping[str, Any]) -> None:
    """Flag the charge for review for a provider-side reason (D3, D5); audited once."""
    with transaction.atomic():
        charge = OneOffCharge.objects.select_for_update().get(pk=charge_id)
        before = charge_state(charge)
        if not charge.needs_review:
            charge.needs_review = True
            charge.save(update_fields=["needs_review", "updated_at"])
        _audit(None, action, charge, before, {**charge_state(charge), **context})


# --- Payments (D6) --------------------------------------------------------------------


def record_payment(
    charge_id: uuid.UUID,
    *,
    method: str,
    amount: Decimal,
    paid_on: date,
    note: str,
    actor: User | None,
    creation_key: uuid.UUID | None = None,
    today: date | None = None,
    reference: str = "",
) -> PaymentOutcome:
    """Record a Zelle, wire or other payment against a one-off charge (kind ``one_off``).

    Never calls the provider: when the charge has a provider invoice and the payment
    covers the whole price at its total, the outcome has ``mark_due=True`` and the
    caller queues :func:`mark_provider_invoice` for the worker. On a builder invoice
    (any line, PLAN-012c-a D2) the rule is the invoice's: ``mark_due`` once the lines'
    payments together cover it (:func:`invoice_covered`), the partial-amount flag on
    the paying charge before. The amount is at most the charge's balance: a later line's
    own price less what it was paid, never more. ``creation_key`` is kept in the audit
    row and looked up under the charge's row lock (on a builder invoice, every line's
    lock, first line first).
    """
    text = visible_text(note or "")
    reference = (reference or "").strip()
    refused = rent_run.payment_refusal(method, amount, paid_on, text, today)
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = lock_lines(charge_id)
        if creation_key is not None and _payment_for_key(charge, creation_key):
            return PaymentOutcome(True, PAY_ALREADY, replayed=True)
        if charge.status == OneOffChargeStatus.WAIVED:
            return PaymentOutcome(False, PAY_WAIVED)
        if charge.status == OneOffChargeStatus.VOIDED:
            return PaymentOutcome(False, PAY_VOIDED)
        if rent_run.on_live_plan(one_off_charge=charge):
            return PaymentOutcome(False, rent_run.PAY_ON_PLAN)
        billed, collected = _billed_and_collected(charge)
        balance = billed - collected
        if balance <= 0:
            return PaymentOutcome(False, PAY_NOTHING_DUE)
        if amount > balance:
            return PaymentOutcome(False, PAY_OVER_BALANCE, "amount")
        whole = balance == billed and amount == balance
        before = charge_state(charge)
        sources = redebit_sources(charge)
        payment = Payment(
            one_off_charge=charge,
            kind=PaymentKind.ONE_OFF,
            method=method,
            amount=amount,
            status=PaymentStatus.SETTLED,
            initiated_on=paid_on,
            settled_on=paid_on,
            reference=reference,
            note=text,
            recorded_by=actor,
        )
        try:
            payment.save()
        except ValidationError:
            # The card-number guard: the transaction is rolled back with nothing saved.
            transaction.set_rollback(True)
            return PaymentOutcome(False, PAY_NOTE_PAYMENT_DATA, "note")
        recompute(charge)
        contacts.close_kept_by_payment(payment)
        _audit(
            actor,
            PAYMENT_ACTION,
            charge,
            before,
            {
                **charge_state(charge),
                "payment": str(payment.pk),
                "method": method,
                "amount": str(amount),
                "paid_on": paid_on.isoformat(),
                "creation_key": str(creation_key) if creation_key is not None else None,
            },
        )
        for companion, companion_before, companion_after in refresh_companions(charge):
            _audit(
                actor,
                ALLOCATED_ACTION,
                companion,
                companion_before,
                {**companion_after, "payment": str(payment.pk), "allocated_from": str(charge.pk)},
            )
        for change in refresh_sources(sources):
            AuditLog.objects.create(
                actor=actor,
                action=change.action,
                entity_type=change.entity_type,
                entity_id=str(change.entity_id),
                before=change.before,
                after={**change.after, "payment": str(payment.pk), "redebit": str(charge.pk)},
            )
        linked = rent_run.provider_invoice(charge)
        if linked is None:
            return PaymentOutcome(
                True, PAY_RECORDED, payment=payment, provider=MarkResult(MarkAction.NO_INVOICE)
            )
        reason = _not_marked_reason(linked[0], whole=whole, amount=amount)
        if reason:
            result = rent_run.not_updated(ONE_OFF, charge, payment, linked, reason, actor)
            return PaymentOutcome(True, PAY_RECORDED, payment=payment, provider=result)
    return PaymentOutcome(True, PAY_RECORDED, payment=payment, mark_due=True)


def _billed_and_collected(charge: OneOffCharge) -> tuple[Decimal, Decimal]:
    """What a manual payment on ``charge`` may pay at most, as what is billed and what
    was collected of it. A charge billed with another (a later line or a companion fee):
    its price and what it holds (its own payments and its share of the other's). Any
    other charge: what its invoice bills (its price and its billed companions' or later
    lines', PLAN-011b D5, PLAN-012 D6), which the payment pays in order, and what was
    paid on it and on those companions or lines themselves (PLAN-012c-a D2)."""
    if billed_with(charge) is not None:
        return Decimal(charge.price), paid_in(charge) + allocated_overflow(charge)
    companions = billed_companions(charge)
    collected = paid_in(charge) + sum((paid_in(row) for row in companions), ZERO)
    return billed_total(charge), collected


def _not_marked_reason(invoice: Invoice, *, whole: bool, amount: Decimal) -> str:
    """Why the provider invoice is not marked after a manual payment (the flag text of
    :mod:`payments.offline_marking`), or ``""`` to mark it. A builder invoice is marked
    once :func:`invoice_covered`, when its total is what its lines bill (PLAN-012c-a
    D2); any other invoice when the payment pays the whole charge at the invoice's total."""
    if is_builder_invoice(invoice):
        if not invoice_covered(invoice):
            return offline_marking.PARTIAL_AMOUNT
        if invoice.total != invoice_billed(invoice):
            return offline_marking.TOTAL_DIFFERS
        return ""
    if not whole:
        return offline_marking.PARTIAL_AMOUNT
    if invoice.total != amount:
        return offline_marking.TOTAL_DIFFERS
    return ""


def mark_provider_invoice(
    charge_id: uuid.UUID, payment_id: uuid.UUID, *, actor: User | None
) -> MarkResult:
    """Mark the one-off charge's provider invoice paid for this manual payment (the worker's
    ``one_off_payment_mark`` job)."""
    return rent_run.mark_charge_invoice(ONE_OFF, charge_id, payment_id, actor=actor)


def retry_provider_mark(charge_id: uuid.UUID, *, actor: User | None) -> MarkResult:
    """Retry the last failed marking of the one-off charge's provider invoice (the worker's
    ``one_off_payment_retry_mark`` job), refused for ``RETRY_LOCK_SECONDS`` after it."""
    return rent_run.retry_charge_mark(ONE_OFF, charge_id, actor=actor)


def retry_wait(charge: OneOffCharge, now: datetime | None = None) -> int | None:
    """``None`` when the charge has no failed marking to retry; otherwise the whole
    seconds until the retry is allowed (0: now)."""
    return rent_run.charge_retry_wait(ONE_OFF, charge, now)


def link_invoice(
    charge_id: uuid.UUID,
    invoice_id: uuid.UUID,
    *,
    confirm: bool,
    note: str,
    actor: User | None,
) -> LinkOutcome:
    """Link an unlinked invoice of the lease's operator to the one-off charge (the rent
    rules and sentences); never to a later line of a builder invoice (PLAN-012 D6)."""
    charge = OneOffCharge.objects.filter(pk=charge_id).first()
    if charge is not None and is_later_line(charge):
        return LinkOutcome(False, LINK_LINE)
    return rent_run.link_charge_invoice(
        ONE_OFF, charge_id, invoice_id, confirm=confirm, note=note, actor=actor
    )


def _payment_for_key(charge: OneOffCharge, creation_key: uuid.UUID) -> bool:
    return AuditLog.objects.filter(
        action=PAYMENT_ACTION,
        entity_type=ENTITY_ONE_OFF_CHARGE,
        entity_id=str(charge.pk),
        after__creation_key=str(creation_key),
    ).exists()


# --- Recomputation --------------------------------------------------------------------


def settled(charge: OneOffCharge) -> Decimal:
    """Settled ``one_off`` payments minus their settled refunds, to the cent. A deposit
    refund (``refund_deposit``) is not counted."""
    rows = Payment.objects.filter(
        one_off_charge=charge, kind=PaymentKind.ONE_OFF, status=PaymentStatus.SETTLED
    )
    paid = rows.filter(is_refund=False).aggregate(total=Sum("amount"))["total"] or ZERO
    refunded = rows.filter(is_refund=True).aggregate(total=Sum("amount"))["total"] or ZERO
    return (Decimal(paid) - Decimal(refunded)).quantize(CENT)


def paid_in(charge: OneOffCharge) -> Decimal:
    """:func:`settled`, plus what the re-debits of the charge's returned payments
    collected again for it (PLAN-011b D5) and what the installments of its ended plans
    were paid (PLAN-012e D9, D11)."""
    return (
        settled(charge)
        + rent_run.redebit_collected(one_off_charge=charge)
        + rent_run.plan_covered(one_off_charge=charge)
    )


def _later_lines(charge: OneOffCharge, *, lock: bool = False) -> list[OneOffCharge]:
    """The charges of the later lines of the invoice billed for ``charge`` (an invoice
    builder invoice whose first line it is, PLAN-012 D6), in line order, whatever their
    status. Empty for any other charge."""
    rows = OneOffCharge.objects.filter(invoice_lines__invoice__one_off_charge=charge).exclude(
        pk=charge.pk
    )
    if lock:
        rows = rows.select_for_update(of=("self",))
    return list(rows.order_by("invoice_lines__position"))


def _companion_rows(charge: OneOffCharge, *, lock: bool = False) -> list[OneOffCharge]:
    """Every charge billed with ``charge``, whatever its status, in the order a payment
    pays them: a re-debit's companion fees in creation order (PLAN-011b D5), a first
    line's later lines in line order (PLAN-012 D6)."""
    fees = OneOffCharge.objects.filter(companion_of=charge)
    if lock:
        fees = fees.select_for_update()
    return [*fees.order_by("created_at", "id"), *_later_lines(charge, lock=lock)]


def billed_with(charge: OneOffCharge) -> OneOffCharge | None:
    """The charge whose invoice or schedule bills ``charge`` as a later line: the
    re-debit of a companion fee (PLAN-011b D5) or the first line of a builder invoice
    (PLAN-012 D6); ``None`` for any other charge."""
    if charge.companion_of_id is not None:
        return OneOffCharge.objects.get(pk=charge.companion_of_id)
    return (
        OneOffCharge.objects.filter(invoices__line_items__one_off_charge=charge)
        .exclude(pk=charge.pk)
        .first()
    )


def is_later_line(charge: OneOffCharge) -> bool:
    """Whether ``charge`` is a later line of a builder invoice (PLAN-012 D6)."""
    return (
        charge.companion_of_id is None
        and InvoiceLineItem.objects.filter(one_off_charge=charge)
        .exclude(invoice__one_off_charge=charge)
        .exists()
    )


def is_builder_invoice(invoice: Invoice) -> bool:
    """Whether the invoice builder made ``invoice``: its line items name its charges."""
    return InvoiceLineItem.objects.filter(invoice=invoice, one_off_charge__isnull=False).exists()


def invoice_charges(invoice: Invoice) -> list[OneOffCharge]:
    """The charges ``invoice`` bills, whatever their status: the charge it is linked to,
    then its line items' charges in line order (PLAN-012c-a D1)."""
    lines = list(
        OneOffCharge.objects.filter(invoice_lines__invoice=invoice).order_by(
            "invoice_lines__position"
        )
    )
    first = invoice.one_off_charge
    if first is None:
        return lines
    return [first, *(line for line in lines if line.pk != first.pk)]


def _billed_lines(invoice: Invoice) -> list[OneOffCharge]:
    return [
        row
        for row in invoice_charges(invoice)
        if row.status not in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)
    ]


def invoice_billed(invoice: Invoice) -> Decimal:
    """What ``invoice`` bills: the prices of its charges neither waived nor voided (as
    :func:`billed_total` counts them)."""
    return sum((Decimal(row.price) for row in _billed_lines(invoice)), ZERO)


def invoice_covered(invoice: Invoice) -> bool:
    """Whether the settled payments (less settled refunds) on the charges ``invoice``
    bills, waived and voided lines left out, reach what it bills (:func:`invoice_billed`;
    PLAN-012c-a D2). Pending payments never count."""
    lines = _billed_lines(invoice)
    billed = sum((Decimal(row.price) for row in lines), ZERO)
    return billed > 0 and sum((paid_in(row) for row in lines), ZERO) >= billed


def lock_lines(charge_id: uuid.UUID) -> OneOffCharge:
    """The charge under its row lock. A line of a builder invoice takes the locks of
    every line of that invoice first, first line first and then in line order (the order
    :func:`refresh_companions` takes them in), so payments on two lines are serialised
    (PLAN-012c-a D2)."""
    first_id = (
        InvoiceLineItem.objects.filter(one_off_charge_id=charge_id)
        .values_list("invoice__one_off_charge", flat=True)
        .first()
    )
    if first_id is not None:
        first = OneOffCharge.objects.select_for_update().get(pk=first_id)
        _later_lines(first, lock=True)
    return OneOffCharge.objects.select_for_update().get(pk=charge_id)


def _marked_siblings(charge: OneOffCharge, invoice: Invoice) -> list[OneOffCharge]:
    """The other charges of a builder ``invoice`` (any status), whose marking flags are
    cleared once it is marked paid (PLAN-012c-a D2); locked first line first, as
    :func:`lock_lines` takes them."""
    if not is_builder_invoice(invoice):
        return []
    ids = [row.pk for row in invoice_charges(invoice) if row.pk != charge.pk]
    locked = {row.pk: row for row in OneOffCharge.objects.select_for_update().filter(pk__in=ids)}
    return [locked[pk] for pk in ids if pk in locked]


def billed_companions(charge: OneOffCharge) -> list[OneOffCharge]:
    """The charges billed with ``charge`` (PLAN-011b D5, PLAN-012 D6): neither waived
    nor voided, in the order a payment pays them. Empty for any charge but a re-debit or
    the first line of a multi-line invoice."""
    return [
        row
        for row in _companion_rows(charge)
        if row.status not in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)
    ]


def billed_total(charge: OneOffCharge) -> Decimal:
    """What the charge's invoice or schedule bills: its price and its billed companions'."""
    return sum((Decimal(item.price) for item in billed_companions(charge)), Decimal(charge.price))


def allocated_overflow(fee: OneOffCharge) -> Decimal:
    """The part of ``fee`` paid by the payments on the charge it is billed with
    (:func:`billed_with`; PLAN-011b D5, PLAN-012 D6): the settled amount beyond that
    charge's price, less what the earlier companions took, between zero and what the
    fee's own payments leave of its price (PLAN-012c-a D2: a later line paid on its own
    takes no more)."""
    source = billed_with(fee)
    if source is None:
        return ZERO
    remaining = paid_in(source) - Decimal(source.price)
    for companion in billed_companions(source):
        open_part = max(Decimal(companion.price) - paid_in(companion), ZERO)
        share = max(min(remaining, open_part), ZERO)
        if companion.pk == fee.pk:
            return share
        remaining -= share
    return ZERO


def refresh_companions(
    charge: OneOffCharge,
) -> list[tuple[OneOffCharge, dict[str, Any], dict[str, Any]]]:
    """Recompute the companions or later lines of ``charge`` after its payments changed
    (PLAN-011b D5, PLAN-012 D6), under their row locks; each one that changed, with its
    state before and after, for the caller's audit row."""
    changed = []
    for companion in _companion_rows(charge, lock=True):
        before = charge_state(companion)
        recompute(companion)
        after = charge_state(companion)
        if after != before:
            changed.append((companion, before, after))
    return changed


def recompute(charge: OneOffCharge) -> None:
    """Recompute the paid amount and the status from the payments (D3).

    Paid in full: ``paid``; partly: ``partial``; nothing: ``owed``. A waived charge is
    left as it is. The caller holds the row lock and writes the audit row. A re-debit
    with a billed companion, or the first line of a multi-line invoice, keeps at most its
    price; a companion fee or a later line adds what the payments on the charge it is
    billed with leave over (PLAN-011b D5, PLAN-012 D6). What the re-debits of the
    charge's returned payments collected again counts as paid (:func:`paid_in`).
    """
    if charge.status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED):
        return
    paid = paid_in(charge)
    if billed_companions(charge):
        paid = min(paid, charge.price)
    else:
        paid += allocated_overflow(charge)
    if paid >= charge.price and paid > 0:
        status = OneOffChargeStatus.PAID
    elif paid > 0:
        status = OneOffChargeStatus.PARTIAL
    else:
        status = OneOffChargeStatus.OWED
    if (charge.amount_paid, charge.status) != (paid, status):
        charge.amount_paid = paid
        charge.status = status
        charge.save(update_fields=["amount_paid", "status", "updated_at"])
    if charge.plan_id is not None:
        sync_plan(charge.plan_id)
    elif charge.category != OneOffCategory.INSTALLMENT:
        # A payment on a split parent while its plan is live (PLAN-012b-a D4): the caller
        # holds the parent's row lock; the plan's is taken after it.
        with transaction.atomic():
            for plan in InstallmentPlan.objects.select_for_update().filter(
                one_off_charge=charge, status__in=LIVE_PLAN_STATUSES
            ):
                apply_plan_status(plan, actor=None)


# --- Installment plans (PLAN-012e D9 to D11) ------------------------------------------

ENTITY_PLAN = "installment_plan"
PLAN_STATUS_ACTION = "installment_plan.status_changed"
#: The action of the audit row of a parent whose paid amount follows its plan.
COVERED_BY_PLAN = "covered_by_plan"
#: Why a plan is ``attention`` (PLAN-012e D11, PLAN-012b-a D4): the ``reason`` audited.
INSTALLMENT_RETURNED = "installment_returned"
PARENT_PAID = "parent_paid"


def plan_state(plan: InstallmentPlan) -> dict[str, Any]:
    """The audited fields of an installment plan."""
    return {
        "rent_charge": str(plan.rent_charge_id) if plan.rent_charge_id else None,
        "one_off_charge": str(plan.one_off_charge_id) if plan.one_off_charge_id else None,
        "operator": str(plan.operator_id),
        "lease": str(plan.lease_id) if plan.lease_id else None,
        "count": plan.count,
        "first_due": plan.first_due.isoformat(),
        "cadence": plan.cadence,
        "collection": plan.collection,
        "status": plan.status,
    }


def parent_paid_on_plan(plan: InstallmentPlan) -> Decimal:
    """What was paid on a one-off parent itself while its plan runs (PLAN-012b-a D3, D4):
    how far the parent's balance, the plan's own installments left out, is below the
    plan's total (the prices of its installments not voided). A payment on the parent's
    old invoice before its release, or a late one after it. Zero for a rent parent, a
    waived or voided parent, or nothing paid."""
    if plan.one_off_charge_id is None:
        return ZERO
    parent = OneOffCharge.objects.get(pk=plan.one_off_charge_id)
    if parent.status in (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED):
        return ZERO
    rows = OneOffCharge.objects.filter(plan=plan, category=OneOffCategory.INSTALLMENT)
    total = rows.exclude(status=OneOffChargeStatus.VOIDED).aggregate(total=Sum("price"))["total"]
    paid = paid_in(parent)
    if plan.status in (InstallmentPlanStatus.COMPLETED, InstallmentPlanStatus.CANCELLED):
        # This plan's installments count as paid on the parent then (plan_covered).
        paid -= Decimal(rows.aggregate(total=Sum("amount_paid"))["total"] or ZERO)
    balance = Decimal(parent.price) - min(paid, Decimal(parent.price))
    return max(Decimal(total or ZERO) - balance, ZERO)


def plan_attention_reason(plan: InstallmentPlan) -> str:
    """Why a live plan needs attention, or ``""``: :data:`INSTALLMENT_RETURNED` while an
    installment has a returned payment not resolved (D11), :data:`PARENT_PAID` while a
    payment on the parent itself lowered its balance below the plan's total
    (PLAN-012b-a D4)."""
    returned = Payment.objects.filter(
        one_off_charge__plan=plan,
        status=PaymentStatus.RETURNED,
        is_refund=False,
        resolved_at__isnull=True,
    )
    if returned.exists():
        return INSTALLMENT_RETURNED
    if parent_paid_on_plan(plan) > 0:
        return PARENT_PAID
    return ""


def plan_status(plan: InstallmentPlan) -> str:
    """What the plan's status should be now (D9, D11): ``completed`` once every
    installment that is not voided is paid, ``attention`` while one has a returned
    payment not resolved or a payment on the parent lowered its balance below the plan's
    total (:func:`plan_attention_reason`, PLAN-012b-a D4), ``active`` otherwise;
    ``cancelled`` stays."""
    if plan.status == InstallmentPlanStatus.CANCELLED:
        return InstallmentPlanStatus.CANCELLED
    statuses = list(
        OneOffCharge.objects.filter(plan=plan)
        .exclude(status=OneOffChargeStatus.VOIDED)
        .values_list("status", flat=True)
    )
    if statuses and all(status == OneOffChargeStatus.PAID for status in statuses):
        return InstallmentPlanStatus.COMPLETED
    if plan_attention_reason(plan):
        return InstallmentPlanStatus.ATTENTION
    return InstallmentPlanStatus.ACTIVE


def apply_plan_status(plan: InstallmentPlan, *, actor: User | None) -> InstallmentPlan:
    """Set the plan's status to :func:`plan_status` when it differs, audited
    :data:`PLAN_STATUS_ACTION` (with the ``reason`` of an ``attention``). The caller
    holds the plan's row lock."""
    status = plan_status(plan)
    if status != plan.status:
        before = plan_state(plan)
        plan.status = status
        plan.save(update_fields=["status", "updated_at"])
        after = plan_state(plan)
        if status == InstallmentPlanStatus.ATTENTION:
            after["reason"] = plan_attention_reason(plan)
        AuditLog.objects.create(
            actor=actor,
            action=PLAN_STATUS_ACTION,
            entity_type=ENTITY_PLAN,
            entity_id=str(plan.pk),
            before=before,
            after=after,
        )
    return plan


def lock_plan_parent(plan_id: uuid.UUID) -> None:
    """Take the row lock of the plan's parent, before the plan's (PLAN-012b-a D4): a
    payment on the parent locks the parent, then its plan; so does every plan path."""
    plan = InstallmentPlan.objects.only("rent_charge_id", "one_off_charge_id").get(pk=plan_id)
    if plan.rent_charge_id is not None:
        RentCharge.objects.select_for_update().filter(pk=plan.rent_charge_id).first()
    elif plan.one_off_charge_id is not None:
        OneOffCharge.objects.select_for_update().filter(pk=plan.one_off_charge_id).first()


def sync_plan(plan_id: uuid.UUID, *, actor: User | None = None) -> InstallmentPlan:
    """Bring the plan's status up to date (:func:`plan_status`) and recompute its parent
    (D9): a completed or cancelled plan's installments count as paid on the parent.
    Each change is audited: the plan's status, and the parent's paid amount and status
    as ``<entity>.covered_by_plan``. Takes the parent's row lock, then the plan's; the
    caller may hold an installment's."""
    with transaction.atomic():
        lock_plan_parent(plan_id)
        plan = InstallmentPlan.objects.select_for_update().get(pk=plan_id)
        apply_plan_status(plan, actor=actor)
        recompute_parent(plan, actor=actor, context={"plan": str(plan.pk)})
    return plan


def recompute_parent(
    plan: InstallmentPlan, *, actor: User | None, context: Mapping[str, Any]
) -> None:
    """Recompute the plan's parent under its row lock and audit a change as
    ``<entity>.covered_by_plan`` (no payment row is written)."""
    if plan.rent_charge_id is not None:
        rent = RentCharge.objects.select_for_update().get(pk=plan.rent_charge_id)
        before = rent_run.charge_state(rent)
        rent_run.recompute_charge(rent)
        after = rent_run.charge_state(rent)
        entity, entity_id = rent_run.ENTITY_CHARGE, rent.pk
    elif plan.one_off_charge_id is not None:
        parent = OneOffCharge.objects.select_for_update().get(pk=plan.one_off_charge_id)
        before = charge_state(parent)
        recompute(parent)
        after = charge_state(parent)
        entity, entity_id = ENTITY_ONE_OFF_CHARGE, parent.pk
    else:
        raise ValueError("An installment plan has a parent.")
    if after != before:
        AuditLog.objects.create(
            actor=actor,
            action=f"{entity}.{COVERED_BY_PLAN}",
            entity_type=entity,
            entity_id=str(entity_id),
            before=before,
            after={**after, "plan_status": plan.status, **context},
        )


Source = RentCharge | InsuranceCharge | OneOffCharge


@dataclass(frozen=True, slots=True)
class SourceChange:
    """A charge a re-debit's payments moved (PLAN-011b D5), for the caller's audit row:
    ``action`` on ``entity_type`` ``entity_id``, ``allocated_from`` the re-debit whose
    payments paid a companion fee."""

    action: str
    entity_type: str
    entity_id: uuid.UUID
    before: dict[str, Any]
    after: dict[str, Any]
    allocated_from: uuid.UUID | None = None


def redebit_sources(charge: OneOffCharge) -> list[tuple[Source, dict[str, Any]]]:
    """The charges a re-debit collects again for, nearest first (the charge its
    returned payment was for; when that is itself a re-debit, that one's source too),
    each under its row lock with its state now. Empty for any other charge. Taken
    before the re-debit's payments are recomputed; :func:`refresh_sources` after."""
    found: list[tuple[Source, dict[str, Any]]] = []
    current = charge
    while current.category == OneOffCategory.REDEBIT and current.source_payment_id is not None:
        payment = Payment.objects.get(pk=current.source_payment_id)
        source: Source
        if payment.rent_charge_id is not None:
            source = RentCharge.objects.select_for_update().get(pk=payment.rent_charge_id)
        elif payment.insurance_charge_id is not None:
            source = InsuranceCharge.objects.select_for_update().get(pk=payment.insurance_charge_id)
        elif payment.one_off_charge_id is not None:
            source = OneOffCharge.objects.select_for_update().get(pk=payment.one_off_charge_id)
        else:
            break
        found.append((source, source_state(source)))
        if not isinstance(source, OneOffCharge):
            break
        current = source
    return found


def refresh_sources(sources: list[tuple[Source, dict[str, Any]]]) -> list[SourceChange]:
    """Recompute the charges of :func:`redebit_sources` (a one-off source's companion
    fees too), nearest first; each one whose state moved."""
    changes = []
    for source, before in sources:
        if isinstance(source, RentCharge):
            rent_run.recompute_charge(source)
            action, entity = "rent_charge.payment_applied", rent_run.ENTITY_CHARGE
        elif isinstance(source, OneOffCharge):
            recompute(source)
            action, entity = "one_off_charge.payment_applied", ENTITY_ONE_OFF_CHARGE
        else:
            action, entity = "insurance_charge.payment_applied", insurance.ENTITY_INSURANCE
        after = source_state(source)
        if after != before:
            changes.append(SourceChange(action, entity, source.pk, before, after))
        if isinstance(source, OneOffCharge):
            changes.extend(
                SourceChange(
                    "one_off_charge.payment_allocated",
                    ENTITY_ONE_OFF_CHARGE,
                    companion.pk,
                    companion_before,
                    companion_after,
                    allocated_from=source.pk,
                )
                for companion, companion_before, companion_after in refresh_companions(source)
            )
    return changes


def source_state(source: Source) -> dict[str, Any]:
    """The audited fields of a re-debit's source charge. An insurance charge stores no
    paid amount (its paid state is derived): its ``amount_paid`` is derived here, as the
    open balances derive it."""
    if isinstance(source, RentCharge):
        return rent_run.charge_state(source)
    if isinstance(source, OneOffCharge):
        return charge_state(source)
    rows = Payment.objects.filter(
        insurance_charge=source, kind=PaymentKind.INSURANCE, status=PaymentStatus.SETTLED
    )
    paid = rows.filter(is_refund=False).aggregate(total=Sum("amount"))["total"] or ZERO
    refunded = rows.filter(is_refund=True).aggregate(total=Sum("amount"))["total"] or ZERO
    collected = rent_run.redebit_collected(insurance_charge=source)
    return {
        "lease": str(source.lease_id),
        "policy_year": source.policy_year,
        "amount": str(source.amount) if source.amount is not None else None,
        "amount_paid": str((Decimal(paid) - Decimal(refunded) + collected).quantize(CENT)),
        "needs_review": source.needs_review,
    }


# --- Helpers --------------------------------------------------------------------------


def charge_state(charge: OneOffCharge) -> dict[str, Any]:
    """The audited fields of a one-off charge."""
    return {
        "operator": str(charge.operator_id) if charge.operator_id else None,
        "lease": str(charge.lease_id) if charge.lease_id else None,
        "category": charge.category,
        "collection": charge.collection,
        "description": charge.description,
        "fee_type": charge.fee_type.code if charge.fee_type is not None else None,
        "cost": str(charge.cost) if charge.cost is not None else None,
        "price": str(charge.price),
        "vendor": charge.vendor,
        "due_date": charge.due_date.isoformat(),
        "amount_paid": str(charge.amount_paid),
        "status": charge.status,
        "needs_review": charge.needs_review,
    }


def _audit(
    actor: User | None,
    action: str,
    charge: OneOffCharge,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_ONE_OFF_CHARGE,
        entity_id=str(charge.pk),
        before=before,
        after=after,
    )


#: One-off charges, for the shared payment, marking and link rules of the rent run.
ONE_OFF = rent_run.ChargeKind(
    OneOffCharge,
    ENTITY_ONE_OFF_CHARGE,
    "price",
    charge_state,
    billed=billed_total,
    locker=lock_lines,
    siblings=_marked_siblings,
)
