"""``invoice_line_links``: are the invoice builder's invoices and their lines' charges
linked both ways? (docs/qa/PLAN-012c-a D5).

Every line of a builder invoice is a one-off charge named by its line item
(``invoice_line_items.one_off_charge``); the invoice itself is linked to the first
line's charge (``invoices.one_off_charge``). This command lists, per builder invoice (one
a sent draft names, or whose line items name a charge), each line item that names no
charge, each charge the draft's send made (its ``invoice_draft.sent`` audit row) that no
line item names, and an invoice whose linked charge has no line item on it, or that is
linked to no charge. It reads only: nothing is written, nothing is queued and no
external service is called. With ``--check`` it exits 1 when it lists anything (the
repair is a separate, owner-approved step).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q

from ledger.models import AuditLog, Invoice, InvoiceLineItem
from ledger.services import invoice_builder


def problems() -> list[str]:
    """One sentence per missing link, by invoice number then line."""
    invoices = (
        Invoice.objects.filter(
            Q(drafts__isnull=False) | Q(line_items__one_off_charge__isnull=False)
        )
        .distinct()
        .order_by("invoice_number")
    )
    found: list[str] = []
    for invoice in invoices:
        rows = InvoiceLineItem.objects.filter(invoice=invoice)
        named = {
            str(charge_id)
            for charge_id in rows.filter(one_off_charge__isnull=False).values_list(
                "one_off_charge_id", flat=True
            )
        }
        for position in (
            rows.filter(one_off_charge__isnull=True)
            .order_by("position")
            .values_list("position", flat=True)
        ):
            found.append(f"{invoice.invoice_number}: line {position} names no charge.")
        sent = _sent_charges(invoice)
        for charge in sent:
            if charge not in named:
                found.append(f"{invoice.invoice_number}: charge {charge} has no line on it.")
        linked = str(invoice.one_off_charge_id) if invoice.one_off_charge_id else ""
        if not linked:
            found.append(f"{invoice.invoice_number}: the invoice is linked to no charge.")
        elif linked not in named and linked not in sent:
            found.append(f"{invoice.invoice_number}: charge {linked} has no line on it.")
    return found


def _sent_charges(invoice: Invoice) -> list[str]:
    """The charges the sends of the drafts naming ``invoice`` made, as their audit rows
    list them."""
    drafts = [str(pk) for pk in invoice.drafts.values_list("pk", flat=True)]
    charges: list[str] = []
    for after in AuditLog.objects.filter(
        action=invoice_builder.SENT_ACTION,
        entity_type=invoice_builder.ENTITY_DRAFT,
        entity_id__in=drafts,
    ).values_list("after", flat=True):
        listed = after.get("charges") if isinstance(after, dict) else None
        if isinstance(listed, list):
            charges.extend(str(charge) for charge in listed if str(charge) not in charges)
    return charges


class Command(BaseCommand):
    help = (
        "List the invoice builder's invoices whose line items name no charge or whose "
        "linked charge has no line item. Reads only."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--check", action="store_true", help="Exit 1 when anything is listed.")

    def handle(self, *args: Any, **options: Any) -> None:
        found = problems()
        for sentence in found:
            self.stdout.write(sentence)
        if not found:
            self.stdout.write("Every builder invoice line names its charge.")
            return
        summary = f"{len(found)} missing link(s) on builder invoices."
        if options["check"]:
            raise CommandError(summary)
        self.stdout.write(summary)
