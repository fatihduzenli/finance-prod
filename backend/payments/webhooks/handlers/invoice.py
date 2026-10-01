"""Invoice events (PLAN-008 D4; docs/DATA_OWNERSHIP.md §3).

The invoice is re-fetched. A local invoice is found by its ``external_refs`` row; if
there is none, one is made for the operator whose customer the invoice names: linked
to its rent charge by the rent run's matching when it names an active rent schedule
of that operator (an invoice the matching cannot place yet is left for the rent run),
and unlinked otherwise. The provider's status, sent, viewed and paid times and amount
paid are then copied through :func:`payments.invoice_sync.record_provider_state`, the
only writer of those columns. A provider total that differs from ours flags the
charge; the amount due is never changed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction

import payments
from ledger.models import ZERO, Invoice, Operator
from ledger.services import numbering, rent_run
from ledger.services.rent_run import MatchAction
from payments import invoice_sync
from payments.models import WebhookOutcome
from payments.operator_sync import ENTITY_OPERATOR, KIND_CUSTOMER
from payments.provider import InvoiceRecord, mask_identifier
from payments.webhooks.events import ignored
from payments.webhooks.handlers.common import (
    ENTITY_CHARGE,
    ENTITY_INSURANCE_CHARGE,
    ENTITY_INVOICE,
    KIND_INVOICE,
    NO_OBJECT_ID,
    NOT_AT_PROVIDER,
    add_ref,
    audit,
    entity_for,
    fetch,
)

UNKNOWN_CUSTOMER = ignored("unknown customer")
NOT_MATCHED = ignored("not matched")
NO_DATE = ignored("no invoice date")
NO_TOTAL = ignored("no usable total")


def handle(event_type: str, provider_object_id: str) -> str:
    if not provider_object_id:
        return NO_OBJECT_ID
    outcome, _ = sync_invoice(provider_object_id)
    return outcome


def sync_invoice(external_id: str) -> tuple[str, Invoice | None]:
    """Re-fetch one invoice and mirror it; the outcome and the local invoice, if any."""
    record = fetch(payments.get_provider().get_invoice, external_id)
    if record is None:
        return NOT_AT_PROVIDER, None
    invoice = local_invoice(external_id)
    if invoice is None:
        outcome, invoice = _create(record)
        if invoice is None:
            return outcome, None
    _apply(invoice, record)
    return WebhookOutcome.PROCESSED, invoice


def local_invoice(external_id: str) -> Invoice | None:
    ref = entity_for(KIND_INVOICE, external_id)
    if ref is None or ref.entity_type != ENTITY_INVOICE:
        return None
    return (
        Invoice.objects.select_related("operator", "rent_charge", "insurance_charge")
        .filter(pk=ref.entity_id)
        .first()
    )


def customer_operator(customer_external_id: str) -> Operator | None:
    """The operator whose current customer reference is this ID."""
    ref = entity_for(KIND_CUSTOMER, customer_external_id)
    if ref is None or ref.entity_type != ENTITY_OPERATOR:
        return None
    return Operator.objects.filter(pk=ref.entity_id).first()


def usable_total(total: Decimal | None) -> bool:
    return (
        total is not None
        and ZERO <= total <= rent_run.MONEY_MAX
        and total == total.quantize(rent_run.CENT)
    )


def _create(record: InvoiceRecord) -> tuple[str, Invoice | None]:
    operator = customer_operator(record.customer_external_id)
    if operator is None:
        return UNKNOWN_CUSTOMER, None
    line = rent_run.match_invoice(record, operator.pk, actor=None)
    if line is not None:
        if line.action is MatchAction.REPORTED:
            return NOT_MATCHED, None
        invoice = local_invoice(record.external_id)
        return (WebhookOutcome.PROCESSED, invoice) if invoice else (NOT_MATCHED, None)
    issued = invoice_sync.invoice_date(record)
    if issued is None:
        return NO_DATE, None
    total = record.total
    if total is None or not usable_total(total):
        return NO_TOTAL, None
    due = invoice_sync.due_date(record)
    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                invoice_number=numbering.next_invoice_number(issued.year),
                operator=operator,
                issue_date=issued,
                due_date=due if due is not None and due >= issued else issued,
                subtotal=total,
                tax=ZERO,
                total=total,
                allow_card=False,
                allow_ach=True,
                creation_key=uuid.uuid4(),
            )
            add_ref(ENTITY_INVOICE, invoice.pk, KIND_INVOICE, record.external_id)
            audit(
                "invoice.created_from_provider",
                ENTITY_INVOICE,
                invoice.pk,
                None,
                {
                    "invoice_number": invoice.invoice_number,
                    "operator": str(operator.pk),
                    "total": str(total),
                    "provider_invoice": mask_identifier(record.external_id),
                },
            )
    except IntegrityError:
        # Another process recorded the same provider invoice first; ours rolled back.
        found = local_invoice(record.external_id)
        return (WebhookOutcome.PROCESSED, found) if found else (NOT_MATCHED, None)
    return WebhookOutcome.PROCESSED, invoice


def _apply(invoice: Invoice, record: InvoiceRecord) -> None:
    changed = invoice_sync.record_provider_state(invoice.pk, record)
    if changed:
        audit(
            "invoice.provider_state_changed",
            ENTITY_INVOICE,
            invoice.pk,
            {name: values[0] for name, values in changed.items()},
            {
                **{name: values[1] for name, values in changed.items()},
                "provider_invoice": mask_identifier(record.external_id),
            },
        )
    if record.total is None or record.total == invoice.total:
        return
    for charge, entity in (
        (invoice.rent_charge, ENTITY_CHARGE),
        (invoice.insurance_charge, ENTITY_INSURANCE_CHARGE),
    ):
        if charge is not None and not charge.needs_review:
            charge.needs_review = True
            charge.save(update_fields=["needs_review", "updated_at"])
            audit(
                f"{entity}.total_mismatch",
                entity,
                charge.pk,
                {"needs_review": False},
                {
                    "needs_review": True,
                    "invoice_number": invoice.invoice_number,
                    "invoice_total": str(invoice.total),
                    "provider_total": str(record.total),
                },
            )
