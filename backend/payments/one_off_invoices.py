"""The provider side of one-off charges, run by the worker only
(docs/qa/PLAN-010d-one-off-charges-and-audit-page.md D2, D3, D5, D6).

Each public function is one job kind's work and returns ``(ok, text)``: whether the job
is ``done`` or ``failed``, and a fixed sentence with, on a provider failure, the error
class and the field names it rejected (never provider text).

- :func:`create_provider_invoice` (``invoice_create``; for an invoice builder invoice,
  PLAN-012 D6, every line of the invoice and its memo): local first (docs/CLAUDE.md
  Rule 5): the charge's local invoice exists and carries our number. Before the first
  create call an attempt is recorded; a run that finds an earlier attempt lists the
  customer's invoices of the last :data:`LOOKBACK_DAYS` days first
  (:func:`find_provider_invoices`: the one whose reference is our number, else the one
  whose total and date match ours uniquely) and creates nothing when it finds one;
  several candidates flag the charge and stop. The invoice is created with our number
  as its reference and the charge's description as its one line item; its external
  reference and its provider state are written (the latter only through
  :mod:`payments.invoice_sync`) and it becomes ``ready``. For collection ``email`` it
  is then sent (:func:`send`); a failed send leaves it linked and ``ready``.
- :func:`send` (``invoice_send``): the e-mail alone, then a re-fetch; ``sent`` once the
  provider shows it sent (a later webhook does the same, :func:`note_sent`).
- :func:`delete` (``invoice_delete``): the cancel of an invoiced charge (D6). The
  invoice is re-fetched (a paid one is never deleted), deleted (a 404 counts as already
  deleted) and re-fetched again; only an invoice the provider shows ``DELETED`` or no
  longer has lets :func:`ledger.services.one_off_charges.mark_voided` void the charge.
- :func:`delete_waived` (``invoice_delete`` with ``params.waived``; PLAN-011 D0): the
  remedy for a charge waived while its invoice was live. The same re-fetch, delete and
  re-fetch as :func:`delete`; once the provider shows the invoice ``DELETED`` (or no
  longer has it) :func:`ledger.services.one_off_charges.mark_waived_invoice_deleted`
  marks the local invoice void. The charge stays ``waived``.
- :func:`release_invoice` and :func:`release_schedule` (``invoice_delete`` and
  ``schedule_delete`` with ``params.release``; PLAN-012b-a D3): the live single-charge
  invoice or the unfired schedule of a split parent, deleted at the provider exactly as
  the cancel deletes it, then, in one transaction under the parent's row lock, the
  invoice ``void`` with the provider's ``DELETED`` (or the schedule reference
  superseded), the charge untouched, and the plan's side
  (:func:`ledger.services.installments.after_release`). A second run finds the object
  gone and only completes the plan's side. The job's entity may be a rent charge
  (PLAN-012b-b D13): its own live invoice of rent alone is released the same way, and
  its ``schedule_delete`` moves the lease's rent autopay past the charge's month
  (:func:`release_rent_autopay`) before the plan's side. Pause (PLAN-012f-b D16) queues
  the same ``schedule_delete`` on an installment: its schedule is deleted the same way and
  its reference superseded, with no plan side (the plan is already ``paused``); one
  whose schedule had already made its invoice is audited
  (:data:`ledger.services.installments.PAUSE_FIRED_ACTION`) for the plan page.
- :func:`schedule` (``schedule_create``): the one-occurrence schedule of an
  ``autopay_due`` charge on its due date with the operator's linked bank method, its
  reference ``OOC-<first 8 of the charge id>``; a re-debit's schedule (PLAN-011b D5)
  carries two lines, the re-debit and its NSF fee unless waived (:func:`charge_amounts`),
  and the invoice it generates links to the re-debit charge; :func:`delete_schedule`
  (``schedule_delete``) deletes it while it has generated no invoice.
- :func:`move_schedule` and :func:`move_invoice` (``schedule_create`` and
  ``invoice_create`` with ``params.reschedule``; PLAN-012e D11): an installment moved
  to a new date. The schedule's rule is updated to one occurrence on the new date
  (refused once it generated its invoice, or when the date has passed); the provider
  invoice's due date is updated (a paid or deleted one is never touched). Without a
  provider object yet, each creates it as the plain job does.
- :func:`pay` (``invoice_pay``): "Charge now" (D4), one attempt. The local rules are
  checked again (:func:`ledger.services.one_off_charges.charge_now_refusal`), the
  invoice is re-fetched (one the provider shows paid or deleted is never charged), then
  ``pay_invoice`` is called once with the linked bank method and the charge's stored
  ``charge_now_key`` as the idempotency identifier; the adapter repeats nothing. The
  answer is applied through the transaction handler, so a pending ACH ``one_off``
  payment appears and settles by webhooks. A failure is audited with the error class
  and the field names; the charge stays open and Charge now returns after one minute.
- :func:`link_from_provider` (the invoice webhook, D5): an invoice whose reference is the
  number of one of our unlinked one-off invoices is linked to it; one generated by a
  one-off charge's schedule gets its local invoice. One naming another operator's
  invoice, or a second invoice for a charge, is not linked and flags the charge.

- :func:`create_rent_invoice` and :func:`send_rent` (``invoice_create`` and
  ``invoice_send`` on a rent charge; PLAN-012b-b D5): an invoice builder invoice of one
  Rent line alone, linked to the month's rent charge. The same check before create,
  create, link and e-mail as above, the failures audited on the rent charge. A builder
  invoice with a Rent line and other lines is the first one-off line's as above: its
  provider lines carry the Rent line (:func:`charge_amounts`).

A ``4xx`` is never retried (the transport retries nothing that is not idempotent); the
charge page shows the failure and offers the single manual retry, which is a new job.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

import payments
from finance.dates import day_text
from ledger.models import (
    LIVE_PLAN_STATUSES,
    ZERO,
    AuditLog,
    CollectionMethod,
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    Payment,
    PaymentStatus,
    RentCharge,
    RentChargeStatus,
    SyncFailure,
    format_quantity,
)
from ledger.services import installments, leases, numbering, one_off_charges, rent_run
from ledger.wording import provider_sentence
from payments import invoice_sync, operator_sync
from payments.errors import ProviderError, ProviderRejected
from payments.models import ExternalRef, JobEntity, WebhookOutcome
from payments.provider import (
    Amounts,
    InvoiceInput,
    InvoiceQuery,
    InvoiceRecord,
    InvoiceUpdate,
    LineItem,
    PaymentProvider,
    PayWithStoredMethod,
    ScheduleInput,
    ScheduleQuery,
    ScheduleRecord,
    ScheduleUpdate,
    mask_identifier,
)
from payments.webhooks.events import ignored
from payments.webhooks.handlers import transaction as transaction_handler

Result = tuple[bool, str]

PROVIDER_KEY = operator_sync.PROVIDER_KEY
ENTITY_INVOICE = "invoice"
KIND_INVOICE = "invoice"
ENTITY_CHARGE = one_off_charges.ENTITY_ONE_OFF_CHARGE
KIND_SCHEDULE = one_off_charges.KIND_SCHEDULE
NOT_FOUND = 404
#: How far back the check-before-create looks (D3).
LOOKBACK_DAYS = 7
#: The hour (UTC) of a schedule's single debit, as the rent schedules' (PLAN-007 D3).
DEBIT_HOUR_UTC = leases.DEBIT_HOUR_UTC
#: The prefix of a schedule's reference: ``OOC-`` and the first 8 of the charge's id.
SCHEDULE_REFERENCE_PREFIX = "OOC-"

INVOICE_ATTEMPT_ACTION = "one_off_charge.provider_invoice_attempted"
#: The rent charge of an invoice of one Rent line alone (PLAN-012b-b D5).
ENTITY_RENT_CHARGE = rent_run.ENTITY_CHARGE
RENT_INVOICE_ATTEMPT_ACTION = "rent_charge.provider_invoice_attempted"
SCHEDULE_ATTEMPT_ACTION = "one_off_charge.provider_schedule_attempted"

# --- Fixed sentences ------------------------------------------------------------------

NO_CHARGE = "The charge no longer exists."
VOIDED_NOTHING = "The charge is voided. Nothing was sent."
NO_LOCAL_INVOICE = provider_sentence("The charge has no invoice to create in {provider}.")
NO_CUSTOMER = provider_sentence("The operator is not connected to {provider}. Nothing was sent.")
NO_BANK_METHOD = provider_sentence(
    "The operator has no bank method connected in {provider}. Autopay was not set up."
)
CREATED = provider_sentence("Invoice {number} created in {provider}.")
ADOPTED = provider_sentence(
    "Invoice {number} found in {provider} and attached; nothing was created."
)
ALREADY_CREATED = provider_sentence("Invoice {number} is already in {provider}.")
CREATE_FAILED = provider_sentence("{provider} did not create invoice {number}")
LINK_CONFLICT = provider_sentence(
    "The {provider} invoice is already attached to another record. Nothing was changed."
)
SENT = "Invoice {number} sent by e-mail."
SEND_REQUESTED = provider_sentence(
    "Invoice {number} was sent by e-mail; {provider} does not show it sent yet."
)
SEND_FAILED = provider_sentence("Invoice {number} is in {provider}, but sending it failed")
ALREADY_SENT = "Invoice {number} was already sent."
NOT_LINKED = provider_sentence("The charge's invoice is not in {provider} yet. Nothing was sent.")
DELETE_PAID = provider_sentence("{provider} shows the invoice paid. Nothing was cancelled.")
DELETE_NOT_CONFIRMED = provider_sentence(
    "{provider} did not confirm the cancellation. The charge stays open; retry later."
)
DELETE_FAILED = provider_sentence("{provider} did not cancel the invoice")
WAIVED_DELETE_NOT_CONFIRMED = provider_sentence(
    "{provider} did not confirm the cancellation. The invoice may still be open in {provider}; "
    "retry from the charge page."
)
SCHEDULED = "Autopay set up for {date}."
ALREADY_SCHEDULED = "Autopay is already set up for this charge."
SCHEDULE_ADOPTED = provider_sentence(
    "The autopay was found in {provider} and attached; none was created."
)
SCHEDULE_FAILED = provider_sentence("{provider} did not set up the autopay")
DUE_PASSED = "The due date has passed. Autopay was not set up; cancel the charge."
NOT_AUTOPAY = "This charge is not collected by autopay on its due date."
SCHEDULE_FIRED = (
    "Autopay already made its invoice. Nothing was voided; cancel again to delete "
    "the invoice once it shows on this page."
)
SCHEDULE_DELETE_FAILED = provider_sentence("{provider} did not cancel the autopay")
SCHEDULE_NOT_CONFIRMED = provider_sentence(
    "{provider} still lists the autopay. The charge stays open; retry later."
)
RETRY_HINT = " Retry from the charge page."
CHARGED = "Charged: the payment is pending until the bank settles it."
CHARGED_NOT_SHOWN = provider_sentence(
    "{provider} accepted the charge; the payment appears here when {provider} reports it."
)
CHARGE_FAILED = provider_sentence("{provider} did not charge the invoice")
CHARGE_STAYS_OPEN = " The charge stays open; Charge now is possible again in one minute."
CHARGE_PAID_AT_PROVIDER = provider_sentence(
    "{provider} shows the invoice paid. Nothing was charged."
)
CHARGE_DELETED = provider_sentence("{provider} shows the invoice cancelled. Nothing was charged.")
CHARGE_NOW_NO_KEY = "The charge has no Charge now identifier. Nothing was charged."
MOVED_SCHEDULE = "Autopay moved to {date}."
MOVE_SCHEDULE_FIRED = "Autopay already made its invoice. Its date was not changed."
MOVE_SCHEDULE_FAILED = provider_sentence("{provider} did not move the autopay")
MOVED_INVOICE = provider_sentence("Invoice {number} in {provider} is now due {date}.")
MOVE_INVOICE_CLOSED = provider_sentence(
    "{provider} shows the invoice paid or cancelled. Its due date was not changed."
)
MOVE_INVOICE_FAILED = provider_sentence("{provider} did not change the invoice's due date")
RELEASE_SHARED = (
    "The invoice bills other charges too; it is not released for one of them. Nothing was deleted."
)
AUTOPAY_MOVE_FAILED = "The rent autopay was not moved past this month; the charge stays open"
RELEASE_SCHEDULE_FIRED = (
    "Autopay already made its invoice. The charge stays open; cancel the "
    "installment plan, then collect or cancel that invoice."
)

OTHER_OPERATOR = ignored("reference of another operator")
SECOND_INVOICE = ignored("second invoice for a one-off charge")


# --- Reads ----------------------------------------------------------------------------


def _charge(charge_id: uuid.UUID) -> OneOffCharge | None:
    return (
        OneOffCharge.objects.select_related("operator", "lease", "lease__store", "fee_type")
        .filter(pk=charge_id)
        .first()
    )


def _invoice(charge: OneOffCharge) -> Invoice | None:
    return charge.invoices.order_by("issue_date", "invoice_number").first()


def _ref(invoice: Invoice) -> str:
    return (
        ExternalRef.objects.filter(
            entity_type=ENTITY_INVOICE,
            entity_id=invoice.pk,
            provider=PROVIDER_KEY,
            external_kind=KIND_INVOICE,
            superseded_at__isnull=True,
        )
        .values_list("external_id", flat=True)
        .first()
        or ""
    )


def _operator(charge: OneOffCharge) -> Operator | None:
    if charge.operator is not None:
        return charge.operator
    return charge.lease.operator if charge.lease is not None else None


def _attempted(charge: OneOffCharge, action: str) -> bool:
    return AuditLog.objects.filter(
        entity_type=ENTITY_CHARGE, entity_id=str(charge.pk), action=action
    ).exists()


def _failure(error: ProviderError) -> str:
    """The failure in words, as the pages show it (copy list §4.1: codes are never shown);
    the class and the rejected key names stay in the audit row."""
    return SyncFailure(operator_sync.failure_name(error)).label


def _fetch(call: Callable[[str], Any], external_id: str) -> Any:
    """Re-fetch one object; ``None`` when the provider answers that it does not exist."""
    try:
        return call(external_id)
    except ProviderRejected as error:
        if error.status == NOT_FOUND:
            return None
        raise


def _audit(
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    after: dict[str, Any],
    before: dict[str, Any] | None = None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
    )


def _failed(
    charge: OneOffCharge | RentCharge,
    actor: User | None,
    action: str,
    error: ProviderError,
    text: str,
) -> Result:
    failure = _failure(error)
    _audit(
        actor,
        action,
        ENTITY_RENT_CHARGE if isinstance(charge, RentCharge) else ENTITY_CHARGE,
        charge.pk,
        {
            "failure": operator_sync.failure_name(error),
            "fields": operator_sync.failure_fields(error),
        },
    )
    return False, f"{text}: {failure}.{RETRY_HINT}"


# --- Check before create (D3) -------------------------------------------------------


def find_provider_invoices(
    provider: PaymentProvider, operator: Operator, customer: str, invoice: Invoice
) -> list[InvoiceRecord]:
    """The customer's unlinked, undeleted provider invoices of the last
    :data:`LOOKBACK_DAYS` days that are ``invoice``: those whose reference is its number,
    else those without a reference whose total and date are its own. One call."""
    if operator.email:
        query = InvoiceQuery(operator_email=operator.email)
    else:
        # TODO-VERIFY-021: no e-mail to filter on; the first page of the list is read.
        query = InvoiceQuery(company_name=operator.company or "")
    records = provider.list_invoices(query)
    linked = set(
        ExternalRef.objects.filter(
            provider=PROVIDER_KEY,
            external_kind=KIND_INVOICE,
            external_id__in=[record.external_id for record in records],
        ).values_list("external_id", flat=True)
    )
    since = invoice.issue_date - timedelta(days=LOOKBACK_DAYS)
    candidates = []
    for record in records:
        issued = invoice_sync.invoice_date(record)
        if (
            record.customer_external_id == customer
            and record.external_id not in linked
            and not invoice_sync.shows_deleted(record)
            and (issued is None or issued >= since)
        ):
            candidates.append(record)
    by_reference = [record for record in candidates if record.reference == invoice.invoice_number]
    if by_reference:
        return by_reference
    return [
        record
        for record in candidates
        if not record.reference
        and record.total == invoice.total
        and invoice_sync.invoice_date(record) == invoice.issue_date
    ]


def _builder_lines(charge: OneOffCharge) -> list[InvoiceLineItem]:
    """The line items of the invoice the invoice builder made for ``charge`` (PLAN-012
    D6), Rent lines included (PLAN-012b-b D5), in line order, without the lines whose
    charge is waived or voided; empty for any other charge."""
    return list(
        InvoiceLineItem.objects.filter(invoice__one_off_charge=charge)
        .filter(Q(one_off_charge__isnull=False) | Q(rent_charge__isnull=False))
        .exclude(one_off_charge__status__in=[OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED])
        .exclude(rent_charge__status=RentChargeStatus.WAIVED)
        .select_related("one_off_charge")
        .order_by("position")
    )


def _rent_lines(invoice: Invoice) -> list[InvoiceLineItem]:
    """The Rent lines of an invoice of rent alone, not waived, in line order."""
    return list(
        InvoiceLineItem.objects.filter(invoice=invoice, rent_charge__isnull=False)
        .exclude(rent_charge__status=RentChargeStatus.WAIVED)
        .order_by("position")
    )


def _line_amounts(items: list[InvoiceLineItem]) -> Amounts:
    """One provider line per line item, no tax; the total is their sum."""
    provider_lines = tuple(_provider_line(item) for item in items)
    amount = sum((Decimal(item.line_total) for item in items), ZERO)
    return Amounts(amount, amount, ZERO, provider_lines)


def _provider_line(item: InvoiceLineItem) -> LineItem:
    """One builder line for the provider: its text (the item, PLAN-012b D3), its quantity
    and unit price when they multiply to its total exactly, else quantity 1 at its total
    with the charge's description (which names the quantity), so that the lines always
    sum to the invoice's total; its detail as the line's ``details`` either way."""
    quantity = Decimal(item.quantity)
    if quantity * Decimal(item.unit_price) == Decimal(item.line_total):
        return LineItem(
            item.description,
            Decimal(format_quantity(quantity)),
            Decimal(item.unit_price),
            details=item.details,
        )
    text = item.one_off_charge.description if item.one_off_charge is not None else ""
    return LineItem(
        text or item.description, Decimal(1), Decimal(item.line_total), details=item.details
    )


def charge_amounts(charge: OneOffCharge, total: Decimal | None = None) -> Amounts:
    """What the charge's invoice or schedule bills, no tax. One line (the description,
    quantity 1, ``total`` or the price), except for a re-debit with a billed NSF fee
    (PLAN-011b D5): one line for the re-debit and one per billed companion, and the
    total is their sum; and for the first line of an invoice builder invoice (PLAN-012
    D6): one line per line of the invoice whose charge is neither waived nor voided,
    and the total is their sum."""
    builder = _builder_lines(charge)
    if builder:
        return _line_amounts(builder)
    companions = one_off_charges.billed_companions(charge)
    if not companions:
        amount = charge.price if total is None else total
        return Amounts(amount, amount, ZERO, (LineItem(charge.description, Decimal(1), amount),))
    lines = (
        LineItem(charge.description, Decimal(1), charge.price),
        *(LineItem(item.description, Decimal(1), item.price) for item in companions),
    )
    amount = sum((line.total for line in lines), ZERO)
    return Amounts(amount, amount, ZERO, lines)


def _invoice_input(charge: OneOffCharge, invoice: Invoice, customer: str) -> InvoiceInput:
    """D3: one line item (the description, quantity 1), no tax, our number as reference;
    a re-debit's invoice carries its NSF fee as a second line, an invoice builder
    invoice every line and its memo (:func:`charge_amounts`; PLAN-012 D6)."""
    built = (
        InvoiceLineItem.objects.filter(invoice=invoice)
        .filter(Q(one_off_charge__isnull=False) | Q(rent_charge__isnull=False))
        .exists()
    )
    return InvoiceInput(
        customer_external_id=customer,
        amounts=charge_amounts(charge, invoice.total),
        due_on=invoice.due_date,
        invoice_date=invoice.issue_date,
        memo=invoice.memo if built else "",
        reference=invoice.invoice_number,
    )


def _link(invoice: Invoice, record: InvoiceRecord, actor: User | None, action: str) -> bool:
    """Write the invoice's external reference and copy the provider state; ``False``
    when the provider's invoice is already another record's."""
    with transaction.atomic():
        try:
            with transaction.atomic():
                ExternalRef.objects.create(
                    entity_type=ENTITY_INVOICE,
                    entity_id=invoice.pk,
                    provider=PROVIDER_KEY,
                    external_kind=KIND_INVOICE,
                    external_id=record.external_id,
                )
        except IntegrityError:
            # The webhook may have linked it first (D5); anything else is a conflict.
            if _ref(invoice) != record.external_id:
                return False
        changes = invoice_sync.record_provider_state(invoice.pk, record)
        Invoice.objects.filter(pk=invoice.pk, internal_status=InvoiceStatus.DRAFT).update(
            internal_status=InvoiceStatus.READY, updated_at=timezone.now()
        )
        _audit(
            actor,
            action,
            ENTITY_INVOICE,
            invoice.pk,
            {
                "invoice_number": invoice.invoice_number,
                ENTITY_CHARGE: str(invoice.one_off_charge_id),
                **(
                    {ENTITY_RENT_CHARGE: str(invoice.rent_charge_id)}
                    if invoice.rent_charge_id is not None
                    else {}
                ),
                "total": str(invoice.total),
                "provider_invoice": mask_identifier(record.external_id),
                "provider_changes": changes,
            },
        )
    return True


def create_provider_invoice(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``invoice_create`` job (D3); see the module docstring."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status == OneOffChargeStatus.VOIDED:
        return True, VOIDED_NOTHING
    invoice = _invoice(charge)
    if invoice is None:
        return False, str(NO_LOCAL_INVOICE)
    number = invoice.invoice_number
    ref = _ref(invoice)
    text = ALREADY_CREATED.format(number=number)
    if not ref:
        operator = _operator(charge)
        customer = leases.customer_ref(operator)
        if operator is None or not customer:
            return False, str(NO_CUSTOMER)
        provider = payments.get_provider()
        try:
            found: list[InvoiceRecord] = []
            if _attempted(charge, INVOICE_ATTEMPT_ACTION):
                found = find_provider_invoices(provider, operator, customer, invoice)
            if len(found) > 1:
                one_off_charges.flag(
                    charge.pk,
                    one_off_charges.SEVERAL_ACTION,
                    {"invoice_number": number, "candidates": len(found)},
                )
                return False, one_off_charges.SEVERAL_PROVIDER_INVOICES
            if found:
                record, action, text = found[0], "invoice.provider_adopted", ADOPTED
            else:
                _audit(actor, INVOICE_ATTEMPT_ACTION, ENTITY_CHARGE, charge.pk, {"number": number})
                record = provider.create_invoice(_invoice_input(charge, invoice, customer))
                action, text = "invoice.provider_created", CREATED
        except ProviderError as error:
            failed = _failed(
                charge,
                actor,
                "one_off_charge.provider_invoice_failed",
                error,
                CREATE_FAILED.format(number=number),
            )
            # PLAN-012e-2 D1: a charge already paid by hand is flagged until a later
            # create succeeds and its mark runs.
            one_off_charges.flag_not_marked(
                charge.pk,
                failure=operator_sync.failure_name(error),
                fields=operator_sync.failure_fields(error),
                actor=actor,
            )
            return failed
        if not _link(invoice, record, actor, action):
            return False, str(LINK_CONFLICT)
        text = text.format(number=number)
        ref = record.external_id
    invoice.refresh_from_db()
    unsent = invoice.internal_status != InvoiceStatus.SENT
    if charge.collection == CollectionMethod.EMAIL and unsent:
        ok, sent = _send(charge, invoice, ref, actor)
        return ok, f"{text} {sent}"
    return True, text


# --- Send (D3) ------------------------------------------------------------------------


def note_sent(invoice: Invoice, record: InvoiceRecord) -> None:
    """A one-off invoice (or an invoice builder invoice of rent alone, PLAN-012b-b D5)
    the provider shows sent becomes ``sent`` (the webhook and the re-fetch after
    sending)."""
    ours = invoice.one_off_charge_id is not None or (
        InvoiceLineItem.objects.filter(invoice=invoice, rent_charge__isnull=False).exists()
    )
    if not ours or not invoice_sync.shows_sent(record):
        return
    Invoice.objects.filter(
        pk=invoice.pk, internal_status__in=[InvoiceStatus.DRAFT, InvoiceStatus.READY]
    ).update(internal_status=InvoiceStatus.SENT, updated_at=timezone.now())


def _send(
    charge: OneOffCharge | RentCharge, invoice: Invoice, ref: str, actor: User | None
) -> Result:
    number = invoice.invoice_number
    provider = payments.get_provider()
    entity = ENTITY_RENT_CHARGE if isinstance(charge, RentCharge) else ENTITY_CHARGE
    try:
        provider.send_invoice_email(ref)
        record = provider.get_invoice(ref)
    except ProviderError as error:
        return _failed(
            charge,
            actor,
            f"{entity}.invoice_send_failed",
            error,
            SEND_FAILED.format(number=number),
        )
    invoice_sync.record_provider_state(invoice.pk, record)
    note_sent(invoice, record)
    shown = invoice_sync.shows_sent(record)
    _audit(
        actor,
        "invoice.sent",
        ENTITY_INVOICE,
        invoice.pk,
        {
            "invoice_number": number,
            "provider_invoice": mask_identifier(ref),
            "shown_sent": shown,
        },
    )
    return True, (SENT if shown else SEND_REQUESTED).format(number=number)


def send(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``invoice_send`` job: the e-mail alone (the retry of a failed send)."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status == OneOffChargeStatus.VOIDED:
        return True, VOIDED_NOTHING
    invoice = _invoice(charge)
    ref = _ref(invoice) if invoice is not None else ""
    if invoice is None or not ref:
        return False, str(NOT_LINKED)
    if invoice.internal_status == InvoiceStatus.SENT:
        return True, ALREADY_SENT.format(number=invoice.invoice_number)
    return _send(charge, invoice, ref, actor)


# --- An invoice of rent alone (PLAN-012b-b D5) -----------------------------------------


def _rent_charge(charge_id: uuid.UUID) -> RentCharge | None:
    return RentCharge.objects.select_related("lease__operator").filter(pk=charge_id).first()


def _rent_invoice(charge: RentCharge) -> Invoice | None:
    """The charge's invoice builder invoice of rent alone (its latest)."""
    return (
        Invoice.objects.filter(rent_charge=charge, line_items__rent_charge=charge)
        .order_by("-issue_date", "-invoice_number")
        .first()
    )


def create_rent_invoice(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``invoice_create`` job of an invoice builder invoice of one Rent line alone
    (PLAN-012b-b D5): the check before create, the create with our number as reference
    and the invoice's lines and memo, the link, then the e-mail; as
    :func:`create_provider_invoice` does for a one-off charge."""
    charge = _rent_charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    invoice = _rent_invoice(charge)
    if invoice is None:
        return False, str(NO_LOCAL_INVOICE)
    if invoice.internal_status == InvoiceStatus.VOID:
        return True, VOIDED_NOTHING
    number = invoice.invoice_number
    ref = _ref(invoice)
    text = ALREADY_CREATED.format(number=number)
    if not ref:
        operator = charge.lease.operator
        customer = leases.customer_ref(operator)
        if operator is None or not customer:
            return False, str(NO_CUSTOMER)
        provider = payments.get_provider()
        try:
            found: list[InvoiceRecord] = []
            if AuditLog.objects.filter(
                entity_type=ENTITY_RENT_CHARGE,
                entity_id=str(charge.pk),
                action=RENT_INVOICE_ATTEMPT_ACTION,
            ).exists():
                found = find_provider_invoices(provider, operator, customer, invoice)
            if len(found) > 1:
                _flag_rent(charge, {"invoice_number": number, "candidates": len(found)})
                return False, one_off_charges.SEVERAL_PROVIDER_INVOICES
            if found:
                record, action, text = found[0], "invoice.provider_adopted", ADOPTED
            else:
                _audit(
                    actor,
                    RENT_INVOICE_ATTEMPT_ACTION,
                    ENTITY_RENT_CHARGE,
                    charge.pk,
                    {"number": number},
                )
                record = provider.create_invoice(
                    InvoiceInput(
                        customer_external_id=customer,
                        amounts=_line_amounts(_rent_lines(invoice)),
                        due_on=invoice.due_date,
                        invoice_date=invoice.issue_date,
                        memo=invoice.memo,
                        reference=number,
                    )
                )
                action, text = "invoice.provider_created", CREATED
        except ProviderError as error:
            return _failed(
                charge,
                actor,
                "rent_charge.provider_invoice_failed",
                error,
                CREATE_FAILED.format(number=number),
            )
        if not _link(invoice, record, actor, action):
            return False, str(LINK_CONFLICT)
        text = text.format(number=number)
        ref = record.external_id
    invoice.refresh_from_db()
    if invoice.internal_status != InvoiceStatus.SENT:
        ok, sent = _send(charge, invoice, ref, actor)
        return ok, f"{text} {sent}"
    return True, text


def send_rent(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``invoice_send`` job of an invoice of rent alone: the e-mail alone."""
    charge = _rent_charge(charge_id)
    invoice = _rent_invoice(charge) if charge is not None else None
    if charge is None or invoice is None:
        return False, str(NOT_LINKED)
    ref = _ref(invoice)
    if not ref:
        return False, str(NOT_LINKED)
    if invoice.internal_status == InvoiceStatus.SENT:
        return True, ALREADY_SENT.format(number=invoice.invoice_number)
    return _send(charge, invoice, ref, actor)


def _flag_rent(charge: RentCharge, context: dict[str, Any]) -> None:
    """Flag the rent charge for review: several provider invoices could be ours."""
    with transaction.atomic():
        locked = RentCharge.objects.select_for_update().get(pk=charge.pk)
        before = rent_run.charge_state(locked)
        if not locked.needs_review:
            locked.needs_review = True
            locked.save(update_fields=["needs_review", "updated_at"])
        _audit(
            None,
            "rent_charge.provider_invoice_ambiguous",
            ENTITY_RENT_CHARGE,
            locked.pk,
            {**rent_run.charge_state(locked), **context},
            before,
        )


# --- Delete (D6) ----------------------------------------------------------------------


def _void(charge: OneOffCharge, actor: User | None, context: dict[str, Any]) -> Result:
    outcome = one_off_charges.mark_voided(charge.pk, actor=actor, context=context)
    return outcome.ok, outcome.message


def delete(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``invoice_delete`` job (D6); see the module docstring."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status == OneOffChargeStatus.VOIDED:
        return True, one_off_charges.VOIDED
    reason = one_off_charges.cancel_refusal(charge)
    if reason:
        return False, reason
    invoice = _invoice(charge)
    ref = _ref(invoice) if invoice is not None else ""
    provider = payments.get_provider()
    try:
        if invoice is not None and not ref and _attempted(charge, INVOICE_ATTEMPT_ACTION):
            # A create that timed out may have made it: look before voiding (Rule 5).
            operator = _operator(charge)
            customer = leases.customer_ref(operator)
            found = (
                find_provider_invoices(provider, operator, customer, invoice)
                if operator is not None and customer
                else []
            )
            if len(found) > 1:
                one_off_charges.flag(
                    charge.pk,
                    one_off_charges.SEVERAL_ACTION,
                    {"invoice_number": invoice.invoice_number, "candidates": len(found)},
                )
                return False, one_off_charges.SEVERAL_PROVIDER_INVOICES
            if found:
                if not _link(invoice, found[0], actor, "invoice.provider_adopted"):
                    return False, str(LINK_CONFLICT)
                ref = found[0].external_id
        if not ref:
            return _void(charge, actor, {"provider_invoice": None})
        record = _fetch(provider.get_invoice, ref)
        if record is not None and invoice_sync.shows_paid(record):
            return False, str(DELETE_PAID)
        if record is not None and not invoice_sync.shows_deleted(record):
            try:
                provider.delete_invoice(ref)
            except ProviderRejected as error:
                if error.status != NOT_FOUND:
                    raise
            record = _fetch(provider.get_invoice, ref)
    except ProviderError as error:
        return _failed(charge, actor, "one_off_charge.invoice_delete_failed", error, DELETE_FAILED)
    if record is not None and not invoice_sync.shows_deleted(record):
        return False, str(DELETE_NOT_CONFIRMED)
    if record is not None and invoice is not None:
        invoice_sync.record_provider_state(invoice.pk, record)
    return _void(
        charge,
        actor,
        {
            "provider_invoice": mask_identifier(ref),
            "provider_status": record.raw_status if record is not None else "not found",
        },
    )


def delete_waived(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``invoice_delete`` job of the waived-charge remedy (PLAN-011 D0); see the
    module docstring. A charge with no live invoice any more is done, with nothing sent."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status != OneOffChargeStatus.WAIVED:
        return False, one_off_charges.DELETE_INVOICE_NOT_WAIVED
    linked = one_off_charges.live_provider_invoice(charge)
    if linked is None:
        return True, one_off_charges.DELETE_INVOICE_NONE
    invoice, ref = linked
    provider = payments.get_provider()
    try:
        record = _fetch(provider.get_invoice, ref)
        if record is not None and invoice_sync.shows_paid(record):
            return False, str(DELETE_PAID)
        if record is not None and not invoice_sync.shows_deleted(record):
            try:
                provider.delete_invoice(ref)
            except ProviderRejected as error:
                if error.status != NOT_FOUND:
                    raise
            record = _fetch(provider.get_invoice, ref)
    except ProviderError as error:
        return _failed(
            charge, actor, "one_off_charge.waived_invoice_delete_failed", error, DELETE_FAILED
        )
    if record is not None and not invoice_sync.shows_deleted(record):
        return False, str(WAIVED_DELETE_NOT_CONFIRMED)
    if record is not None:
        invoice_sync.record_provider_state(invoice.pk, record)
    outcome = one_off_charges.mark_waived_invoice_deleted(
        charge.pk,
        actor=actor,
        context={
            "invoice_number": invoice.invoice_number,
            "provider_invoice": mask_identifier(ref),
            "provider_status": record.raw_status if record is not None else "not found",
        },
    )
    return outcome.ok, outcome.message


# --- Releasing a split parent's object (PLAN-012b-a D3) --------------------------------


Parent = OneOffCharge | RentCharge


def _release_target(charge_id: uuid.UUID, entity_type: str) -> Parent | Result:
    """The parent of an installment plan named by the job's entity (a one-off or a rent
    charge, PLAN-012b-b D13), or an installment whose schedule Pause releases
    (PLAN-012f-b D16), or the job's outcome when it is neither."""
    charge: Parent | None = (
        _rent_charge(charge_id) if entity_type == JobEntity.RENT_CHARGE else _charge(charge_id)
    )
    if charge is None:
        return False, NO_CHARGE
    if isinstance(charge, OneOffCharge) and charge.plan_id is not None:
        return charge
    if not charge.installment_plans.exists():
        return False, installments.RELEASE_NO_PLAN
    return charge


def _released(
    parent: Parent, actor: User | None, mark: Callable[[Parent], dict[str, Any]]
) -> Result:
    """The local side of a release, in one transaction under the parent's row lock:
    ``mark`` (which returns the audit context), then the plan's side
    (:func:`ledger.services.installments.after_release`); an installment's release (a
    paused plan's next debit, PLAN-012f-b D16) has no plan side beyond ``mark``."""
    with transaction.atomic():
        locked: Parent
        if isinstance(parent, RentCharge):
            locked = RentCharge.objects.select_for_update().get(pk=parent.pk)
        else:
            locked = OneOffCharge.objects.select_for_update().get(pk=parent.pk)
        context = mark(locked)
        if isinstance(locked, OneOffCharge) and locked.plan_id is not None:
            return True, str(installments.PAUSE_RELEASED)
        return installments.after_release(locked, actor, context)


def _release_failed(charge: Parent, actor: User | None, error: ProviderError, text: str) -> Result:
    action = (
        "rent_charge.release_failed"
        if isinstance(charge, RentCharge)
        else "one_off_charge.release_failed"
    )
    return _failed(charge, actor, action, error, text)


def release_invoice(
    charge_id: uuid.UUID, actor: User | None, entity_type: str = JobEntity.ONE_OFF_CHARGE
) -> Result:
    """The ``invoice_delete`` job with ``params.release``: the split parent's live
    single-charge invoice (a one-off charge's, or a rent charge's of rent alone,
    PLAN-012b-b D13; the parent named by ``entity_type``) deleted at the provider as
    :func:`delete` deletes it (a paid one never; a 404 counts as deleted; re-fetched
    after), then, locally, the invoice ``void`` with the provider's ``DELETED`` and the
    charge untouched. A parent with no live invoice any more (a second run) deletes
    nothing and only completes the plan's side."""
    target = _release_target(charge_id, entity_type)
    if not isinstance(target, OneOffCharge | RentCharge):
        return target
    charge = target
    linked = rent_run.provider_invoice(charge)
    if linked is None or not one_off_charges.invoice_is_live(linked[0]):
        return _released(charge, actor, lambda parent: {})
    invoice, ref = linked
    own = invoice.rent_charge_id if isinstance(charge, RentCharge) else invoice.one_off_charge_id
    if own != charge.pk or one_off_charges.invoice_line_count(invoice) > 1:
        return False, RELEASE_SHARED
    provider = payments.get_provider()
    try:
        record = _fetch(provider.get_invoice, ref)
        if record is not None and invoice_sync.shows_paid(record):
            return False, str(DELETE_PAID)
        if record is not None and not invoice_sync.shows_deleted(record):
            try:
                provider.delete_invoice(ref)
            except ProviderRejected as error:
                if error.status != NOT_FOUND:
                    raise
            record = _fetch(provider.get_invoice, ref)
    except ProviderError as error:
        return _release_failed(charge, actor, error, DELETE_FAILED)
    if record is not None and not invoice_sync.shows_deleted(record):
        return False, str(DELETE_NOT_CONFIRMED)

    def mark(parent: Parent) -> dict[str, Any]:
        context = {
            "invoice_number": invoice.invoice_number,
            "provider_invoice": mask_identifier(ref),
            "provider_status": record.raw_status if record is not None else "not found",
        }
        invoice.refresh_from_db()
        if invoice.internal_status == InvoiceStatus.VOID:
            # A concurrent run marked it first.
            return context
        if record is not None:
            invoice_sync.record_provider_state(invoice.pk, record)
        else:
            invoice_sync.record_provider_status(invoice.pk, invoice_sync.PROVIDER_DELETED)
        one_off_charges.mark_invoice_released(
            invoice, actor=actor, context={**context, "charge": str(parent.pk)}
        )
        return context

    return _released(charge, actor, mark)


def _release_fired(charge: OneOffCharge, actor: User | None) -> Result:
    """A schedule that made its invoice before its release: a split parent's refusal, or,
    for an installment Pause released (PLAN-012f-b D3-1), an audit row the plan page reads
    ("one debit had already gone out") and the job's own sentence."""
    if charge.plan_id is None:
        return False, RELEASE_SCHEDULE_FIRED
    _audit(
        actor,
        installments.PAUSE_FIRED_ACTION,
        ENTITY_CHARGE,
        charge.pk,
        {"plan": str(charge.plan_id), "installment_no": charge.installment_no},
    )
    return False, installments.PAUSE_FIRED


def release_schedule(
    charge_id: uuid.UUID, actor: User | None, entity_type: str = JobEntity.ONE_OFF_CHARGE
) -> Result:
    """The ``schedule_delete`` job with ``params.release``: the split parent's schedule
    deleted at the provider as :func:`delete_schedule` deletes it (a 404 counts as gone;
    then the provider must neither list it nor show an invoice it generated), then,
    locally, its reference superseded and the charge untouched. A parent with no current
    schedule any more (a second run) deletes nothing and only completes the plan's side.
    A rent parent's (``entity_type`` rent charge) moves the lease's rent autopay past
    the month instead (:func:`release_rent_autopay`)."""
    if entity_type == JobEntity.RENT_CHARGE:
        return release_rent_autopay(charge_id, actor)
    target = _release_target(charge_id, entity_type)
    if not isinstance(target, OneOffCharge):
        return target if isinstance(target, tuple) else (False, NO_CHARGE)
    charge = target
    if one_off_charges.invoices_of(charge).exists():
        return _release_fired(charge, actor)
    ref = _schedule_ref(charge)
    if not ref:
        return _released(charge, actor, lambda parent: {})
    provider = payments.get_provider()
    try:
        try:
            provider.delete_scheduled_invoice(ref)
        except ProviderRejected as error:
            if error.status != NOT_FOUND:
                raise
        generated = [
            record
            for record in provider.list_invoices(InvoiceQuery(schedule_external_id=ref))
            if invoice_sync.schedule_id(record) == ref and not invoice_sync.shows_deleted(record)
        ]
        still = _fetch(provider.get_scheduled_invoice, ref)
    except ProviderError as error:
        return _release_failed(charge, actor, error, SCHEDULE_DELETE_FAILED)
    if generated:
        return _release_fired(charge, actor)
    if still is not None:
        return False, str(SCHEDULE_NOT_CONFIRMED)

    def mark(parent: Parent) -> dict[str, Any]:
        context = {"schedule": mask_identifier(ref)}
        if isinstance(parent, OneOffCharge):
            one_off_charges.mark_schedule_released(parent, actor=actor, context=context)
        return context

    return _released(charge, actor, mark)


def release_rent_autopay(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``schedule_delete`` job with ``params.release`` on a split rent charge
    (PLAN-012b-b D13): while its plan is live and no payment on it is pending, the
    lease's rent autopay is moved past the charge's month
    (:func:`ledger.services.leases.move_autopay`, the charge's debit and due date), then
    the plan's side. A failed move fails the job with its sentence (Retry from the charge
    page repeats it); the charge is never voided. A plan no longer live moves nothing."""
    target = _release_target(charge_id, JobEntity.RENT_CHARGE)
    if not isinstance(target, RentCharge):
        return target if isinstance(target, tuple) else (False, NO_CHARGE)
    charge = target
    context: dict[str, Any] = {}
    plan = installments.plan_of(charge)
    if plan is not None and plan.status in LIVE_PLAN_STATUSES:
        if Payment.objects.filter(rent_charge=charge, status=PaymentStatus.PENDING).exists():
            return False, installments.PENDING
        outcome = leases.move_autopay(
            charge.lease_id, seq=charge.seq, skipped=charge.due_on, plan_id=plan.pk, actor=actor
        )
        if not outcome.ok:
            if outcome.failure in SyncFailure.values:
                words = SyncFailure(outcome.failure).label
                return False, f"{AUTOPAY_MOVE_FAILED}: {words}.{RETRY_HINT}"
            return False, f"{AUTOPAY_MOVE_FAILED}. {outcome.message}{RETRY_HINT}"
        context = {"autopay": outcome.message}
    return _released(charge, actor, lambda parent: context)


# --- Schedules (D2, D6) ---------------------------------------------------------------


def schedule_reference(charge: OneOffCharge) -> str:
    """``OOC-`` and the first 8 characters of the charge's id (D2)."""
    return f"{SCHEDULE_REFERENCE_PREFIX}{str(charge.pk)[:8]}"


def debit_moment(due: date, now: datetime) -> datetime:
    """The schedule's single debit: the due date at :data:`DEBIT_HOUR_UTC`, or, when that
    is no longer in the future (a charge due today), the next full hour."""
    moment = datetime(due.year, due.month, due.day, DEBIT_HOUR_UTC, tzinfo=UTC)
    if moment > now:
        return moment
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def schedule_rule(due: date, now: datetime) -> str:
    """One occurrence on the due date: ``DTSTART=<YYYYMMDDTHHMMSSZ>;FREQ=DAILY;COUNT=1``
    (TODO-VERIFY-019's working assumption)."""
    return f"DTSTART={debit_moment(due, now):%Y%m%dT%H%M%SZ};FREQ=DAILY;COUNT=1"


def _schedule_ref(charge: OneOffCharge) -> str:
    return one_off_charges.provider_schedule(charge)


def _adoptable(
    provider: PaymentProvider, customer: str, charge: OneOffCharge
) -> list[ScheduleRecord]:
    """Unlinked schedules of the customer with this amount and one debit on the due
    date (the check before a second create)."""
    linked = set(
        ExternalRef.objects.filter(provider=PROVIDER_KEY, external_kind=KIND_SCHEDULE).values_list(
            "external_id", flat=True
        )
    )
    day = f"DTSTART={charge.due_date:%Y%m%d}T"
    return [
        record
        for record in provider.list_scheduled_invoices(ScheduleQuery(customer_external_id=customer))
        if record.external_id not in linked
        and record.total == one_off_charges.billed_total(charge)
        and record.rule.startswith(day)
        and "COUNT=1" in record.rule
    ]


def _link_schedule(charge: OneOffCharge, external_id: str) -> None:
    ExternalRef.objects.create(
        entity_type=ENTITY_CHARGE,
        entity_id=charge.pk,
        provider=PROVIDER_KEY,
        external_kind=KIND_SCHEDULE,
        external_id=external_id,
    )


def schedule(charge_id: uuid.UUID, actor: User | None, *, now: datetime | None = None) -> Result:
    """The ``schedule_create`` job (D2); see the module docstring."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status == OneOffChargeStatus.VOIDED:
        return True, VOIDED_NOTHING
    if charge.collection != CollectionMethod.AUTOPAY_DUE:
        return False, NOT_AUTOPAY
    if _schedule_ref(charge):
        return True, ALREADY_SCHEDULED
    moment = now or timezone.now()
    if charge.due_date < timezone.localdate(moment):
        return False, DUE_PASSED
    operator = _operator(charge)
    customer = leases.customer_ref(operator)
    if operator is None or not customer:
        return False, str(NO_CUSTOMER)
    method = leases.linked_payment_method(operator)
    if not method:
        return False, str(NO_BANK_METHOD)
    provider = payments.get_provider()
    rule = schedule_rule(charge.due_date, moment)
    request = ScheduleInput(
        customer_external_id=customer,
        payment_method_external_id=method,
        amounts=charge_amounts(charge),
        rule=rule,
        email_notification=bool(operator.email),
        reference=schedule_reference(charge),
    )
    try:
        found: list[ScheduleRecord] = []
        if _attempted(charge, SCHEDULE_ATTEMPT_ACTION):
            found = _adoptable(provider, customer, charge)
        if len(found) > 1:
            one_off_charges.flag(
                charge.pk, one_off_charges.SEVERAL_ACTION, {"candidates": len(found)}
            )
            return False, one_off_charges.SEVERAL_PROVIDER_INVOICES
        if found:
            record, text, action = found[0], SCHEDULE_ADOPTED, "one_off_charge.schedule_adopted"
        else:
            _audit(actor, SCHEDULE_ATTEMPT_ACTION, ENTITY_CHARGE, charge.pk, {"rule": rule})
            record = provider.create_scheduled_invoice(request)
            text = SCHEDULED.format(date=day_text(charge.due_date))
            action = "one_off_charge.schedule_created"
    except ProviderError as error:
        return _failed(charge, actor, "one_off_charge.schedule_failed", error, SCHEDULE_FAILED)
    with transaction.atomic():
        _link_schedule(charge, record.external_id)
        _audit(
            actor,
            action,
            ENTITY_CHARGE,
            charge.pk,
            {
                "schedule": mask_identifier(record.external_id),
                "rule": record.rule or rule,
                "payment_method": mask_identifier(method),
                "reference": request.reference,
                "email_notification": request.email_notification,
            },
        )
    return True, str(text)


def delete_schedule(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``schedule_delete`` job (D6): the schedule of an ``autopay_due`` charge that
    has generated no invoice is deleted (a 404 counts as gone), then the provider must
    neither list it nor show an invoice it generated before the charge is voided. A
    charge that has an invoice by now is cancelled as an invoiced one (:func:`delete`)."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status == OneOffChargeStatus.VOIDED:
        return True, one_off_charges.VOIDED
    reason = one_off_charges.cancel_refusal(charge)
    if reason:
        return False, reason
    if charge.invoices.exists():
        return delete(charge_id, actor)
    ref = _schedule_ref(charge)
    provider = payments.get_provider()
    try:
        if not ref and _attempted(charge, SCHEDULE_ATTEMPT_ACTION):
            operator = _operator(charge)
            customer = leases.customer_ref(operator)
            found = _adoptable(provider, customer, charge) if customer else []
            if len(found) > 1:
                one_off_charges.flag(
                    charge.pk, one_off_charges.SEVERAL_ACTION, {"candidates": len(found)}
                )
                return False, one_off_charges.SEVERAL_PROVIDER_INVOICES
            if found:
                _link_schedule(charge, found[0].external_id)
                ref = found[0].external_id
        if not ref:
            return _void(charge, actor, {"schedule": None})
        try:
            provider.delete_scheduled_invoice(ref)
        except ProviderRejected as error:
            if error.status != NOT_FOUND:
                raise
        generated = [
            record
            for record in provider.list_invoices(InvoiceQuery(schedule_external_id=ref))
            if invoice_sync.schedule_id(record) == ref and not invoice_sync.shows_deleted(record)
        ]
        still = _fetch(provider.get_scheduled_invoice, ref)
    except ProviderError as error:
        return _failed(
            charge, actor, "one_off_charge.schedule_delete_failed", error, SCHEDULE_DELETE_FAILED
        )
    if generated:
        return False, SCHEDULE_FIRED
    if still is not None:
        return False, str(SCHEDULE_NOT_CONFIRMED)
    ok, text = _void(charge, actor, {"schedule": mask_identifier(ref)})
    if ok:
        ExternalRef.objects.filter(
            entity_type=ENTITY_CHARGE,
            entity_id=charge.pk,
            external_kind=KIND_SCHEDULE,
            superseded_at__isnull=True,
        ).update(superseded_at=timezone.now())
    return ok, text


# --- Moving an installment (PLAN-012e D11) --------------------------------------------


def move_schedule(
    charge_id: uuid.UUID, actor: User | None, *, now: datetime | None = None
) -> Result:
    """The ``schedule_create`` job with ``params.reschedule``; see the module docstring."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status == OneOffChargeStatus.VOIDED:
        return True, VOIDED_NOTHING
    ref = _schedule_ref(charge)
    if not ref:
        return schedule(charge_id, actor, now=now)
    moment = now or timezone.now()
    if charge.due_date < timezone.localdate(moment):
        return False, DUE_PASSED
    if charge.invoices.exists():
        return False, MOVE_SCHEDULE_FIRED
    rule = schedule_rule(charge.due_date, moment)
    provider = payments.get_provider()
    try:
        generated = [
            record
            for record in provider.list_invoices(InvoiceQuery(schedule_external_id=ref))
            if invoice_sync.schedule_id(record) == ref and not invoice_sync.shows_deleted(record)
        ]
        if generated:
            return False, MOVE_SCHEDULE_FIRED
        record = provider.update_scheduled_invoice(ref, ScheduleUpdate(rule=rule))
    except ProviderError as error:
        return _failed(
            charge, actor, "one_off_charge.schedule_move_failed", error, MOVE_SCHEDULE_FAILED
        )
    _audit(
        actor,
        "one_off_charge.schedule_moved",
        ENTITY_CHARGE,
        charge.pk,
        {"schedule": mask_identifier(ref), "rule": record.rule or rule},
    )
    return True, MOVED_SCHEDULE.format(date=day_text(charge.due_date))


def move_invoice(charge_id: uuid.UUID, actor: User | None) -> Result:
    """The ``invoice_create`` job with ``params.reschedule``; see the module docstring."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    if charge.status == OneOffChargeStatus.VOIDED:
        return True, VOIDED_NOTHING
    invoice = _invoice(charge)
    ref = _ref(invoice) if invoice is not None else ""
    if invoice is None or not ref:
        return create_provider_invoice(charge_id, actor)
    provider = payments.get_provider()
    try:
        record = _fetch(provider.get_invoice, ref)
        if record is None or invoice_sync.shows_paid(record) or invoice_sync.shows_deleted(record):
            return False, str(MOVE_INVOICE_CLOSED)
        record = provider.update_invoice(ref, InvoiceUpdate(due_on=invoice.due_date))
    except ProviderError as error:
        return _failed(
            charge, actor, "one_off_charge.invoice_move_failed", error, MOVE_INVOICE_FAILED
        )
    invoice_sync.record_provider_state(invoice.pk, record)
    _audit(
        actor,
        "one_off_charge.invoice_moved",
        ENTITY_CHARGE,
        charge.pk,
        {
            "invoice_number": invoice.invoice_number,
            "provider_invoice": mask_identifier(ref),
            "due_date": invoice.due_date.isoformat(),
        },
    )
    return True, MOVED_INVOICE.format(
        number=invoice.invoice_number, date=day_text(invoice.due_date)
    )


# --- Charge now (D4) -------------------------------------------------------------------


def _pay_failed(
    charge: OneOffCharge, actor: User | None, context: dict[str, Any], text: str
) -> Result:
    one_off_charges.record_charge_now(
        charge.pk, actor, one_off_charges.CHARGE_FAILED_ACTION, context
    )
    return False, text


def pay(charge_id: uuid.UUID, actor: User | None, *, confirmed: bool) -> Result:
    """The ``invoice_pay`` job (D4); see the module docstring."""
    charge = _charge(charge_id)
    if charge is None:
        return False, NO_CHARGE
    refusal = one_off_charges.charge_now_refusal(charge)
    amount = one_off_charges.billed_total(charge)
    if not refusal and leases.is_large_debit(amount) and not confirmed:
        refusal = leases.large_debit_confirmation([amount])
    if not refusal and charge.charge_now_key is None:
        refusal = CHARGE_NOW_NO_KEY
    if refusal:
        return _pay_failed(charge, actor, {"refused": refusal}, refusal)
    linked = rent_run.provider_invoice(charge)
    operator = _operator(charge)
    method = leases.linked_payment_method(operator)
    if linked is None or not method or charge.charge_now_key is None:  # checked above
        return False, one_off_charges.CHARGE_NOW_NOT_LINKED
    invoice, ref = linked
    provider = payments.get_provider()
    try:
        record = provider.get_invoice(ref)
    except ProviderError as error:
        return _pay_failed(
            charge,
            actor,
            {
                "failure": operator_sync.failure_name(error),
                "fields": operator_sync.failure_fields(error),
            },
            f"{CHARGE_FAILED}: {_failure(error)}.{CHARGE_STAYS_OPEN}",
        )
    invoice_sync.record_provider_state(invoice.pk, record)
    if invoice_sync.shows_paid(record) or invoice_sync.shows_deleted(record):
        text = str(CHARGE_PAID_AT_PROVIDER if invoice_sync.shows_paid(record) else CHARGE_DELETED)
        return _pay_failed(
            charge, actor, {"refused": text, "provider_status": record.raw_status}, text
        )
    request = PayWithStoredMethod(
        payment_method_external_id=method,
        idempotency_id=str(charge.charge_now_key),
        email_receipt=True,
    )
    try:
        transaction_record = provider.pay_invoice(ref, request)
    except ProviderError as error:
        return _pay_failed(
            charge,
            actor,
            {
                "failure": operator_sync.failure_name(error),
                "fields": operator_sync.failure_fields(error),
                "provider_invoice": mask_identifier(ref),
            },
            f"{CHARGE_FAILED}: {_failure(error)}.{CHARGE_STAYS_OPEN}",
        )
    if not transaction_record.invoice_external_id:
        transaction_record = replace(transaction_record, invoice_external_id=ref)
    with transaction.atomic():
        outcome = transaction_handler.apply(transaction_record, settle=False)
        payment = transaction_handler.local_payment(transaction_record.external_id)
    one_off_charges.record_charge_now(
        charge.pk,
        actor,
        one_off_charges.CHARGED_ACTION,
        {
            "amount": str(amount),
            "invoice_number": invoice.invoice_number,
            "provider_invoice": mask_identifier(ref),
            "provider_transaction": mask_identifier(transaction_record.external_id),
            "payment_method": mask_identifier(method),
            "payment": str(payment.pk) if payment is not None else None,
            "outcome": outcome,
        },
    )
    return True, CHARGED if payment is not None else str(CHARGED_NOT_SHOWN)


# --- Webhook linking (D5) -------------------------------------------------------------


def link_from_provider(
    record: InvoiceRecord, operator: Operator
) -> tuple[str, Invoice | None] | None:
    """Place a provider invoice we hold no row for that belongs to a one-off charge, or
    ``None`` when it belongs to none (the rent rules then apply). ``operator`` is the
    operator whose customer the invoice names."""
    if record.reference:
        invoice = (
            Invoice.objects.filter(invoice_number=record.reference, one_off_charge__isnull=False)
            .select_related("one_off_charge")
            .first()
        )
        if invoice is not None:
            return _link_by_reference(invoice, record, operator)
    schedule_ref = invoice_sync.schedule_id(record)
    if schedule_ref:
        ref = ExternalRef.objects.filter(
            entity_type=ENTITY_CHARGE,
            provider=PROVIDER_KEY,
            external_kind=KIND_SCHEDULE,
            external_id=schedule_ref,
            superseded_at__isnull=True,
        ).first()
        if ref is not None:
            return _create_for_schedule(ref.entity_id, record, operator)
    return None


def _link_by_reference(
    invoice: Invoice, record: InvoiceRecord, operator: Operator
) -> tuple[str, Invoice | None]:
    charge_id = invoice.one_off_charge_id
    if charge_id is None:  # filtered on it; kept for the type checker
        return SECOND_INVOICE, None
    masked = mask_identifier(record.external_id)
    if invoice.operator_id != operator.pk:
        one_off_charges.flag(
            charge_id,
            one_off_charges.OTHER_OPERATOR_ACTION,
            {"invoice_number": invoice.invoice_number, "provider_invoice": masked},
        )
        return OTHER_OPERATOR, None
    current = _ref(invoice)
    if current == record.external_id:
        return WebhookOutcome.PROCESSED, invoice
    if current:
        one_off_charges.flag(
            charge_id,
            one_off_charges.SECOND_INVOICE_ACTION,
            {"invoice_number": invoice.invoice_number, "provider_invoice": masked},
        )
        return SECOND_INVOICE, None
    if not _link(invoice, record, None, "invoice.linked_by_reference"):
        return SECOND_INVOICE, None
    invoice.refresh_from_db()
    return WebhookOutcome.PROCESSED, invoice


def _usable(total: Decimal | None) -> bool:
    return (
        total is not None
        and ZERO < total <= rent_run.MONEY_MAX
        and total == total.quantize(rent_run.CENT)
    )


def _create_for_schedule(
    charge_id: uuid.UUID, record: InvoiceRecord, operator: Operator
) -> tuple[str, Invoice | None]:
    masked = mask_identifier(record.external_id)
    with transaction.atomic():
        charge = (
            OneOffCharge.objects.select_for_update(of=("self",))
            .select_related("lease")
            .get(pk=charge_id)
        )
        if charge.operator_id_billed != operator.pk:
            one_off_charges.flag(
                charge.pk, one_off_charges.OTHER_OPERATOR_ACTION, {"provider_invoice": masked}
            )
            return OTHER_OPERATOR, None
        if charge.invoices.exists():
            one_off_charges.flag(
                charge.pk, one_off_charges.SECOND_INVOICE_ACTION, {"provider_invoice": masked}
            )
            return SECOND_INVOICE, None
        issued = invoice_sync.invoice_date(record) or timezone.localdate()
        due = invoice_sync.due_date(record)
        expected = one_off_charges.billed_total(charge)
        total = record.total if _usable(record.total) and record.total is not None else expected
        invoice = Invoice.objects.create(
            invoice_number=numbering.next_invoice_number(issued.year),
            operator=operator,
            one_off_charge=charge,
            issue_date=issued,
            due_date=due if due is not None and due >= issued else issued,
            memo=charge.description,
            subtotal=total,
            tax=ZERO,
            fees=ZERO,
            surcharge=ZERO,
            total=total,
            internal_status=InvoiceStatus.READY,
            allow_card=False,
            allow_ach=True,
        )
        ExternalRef.objects.create(
            entity_type=ENTITY_INVOICE,
            entity_id=invoice.pk,
            provider=PROVIDER_KEY,
            external_kind=KIND_INVOICE,
            external_id=record.external_id,
        )
        _audit(
            None,
            "invoice.created_from_schedule",
            ENTITY_INVOICE,
            invoice.pk,
            {
                "invoice_number": invoice.invoice_number,
                ENTITY_CHARGE: str(charge.pk),
                "total": str(total),
                "provider_invoice": masked,
                "schedule": mask_identifier(invoice_sync.schedule_id(record)),
            },
        )
        if total != expected:
            one_off_charges.flag(
                charge.pk,
                "one_off_charge.total_mismatch",
                {"invoice_number": invoice.invoice_number, "provider_total": str(total)},
            )
    return WebhookOutcome.PROCESSED, invoice
