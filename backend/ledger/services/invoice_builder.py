"""The invoice builder (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D6, D7, D9).

A **draft** (:class:`~ledger.models.InvoiceDraft`) is local only: an operator, optionally
one of its leases, a memo, an optional due date and one or more lines, each a fee type
from the catalogue or free text, a quantity and a unit amount. Nothing reaches the
payment provider while it is a draft.

- :func:`create_draft` and :func:`update_draft` (billing; the lines are replaced as a
  set while the draft is a draft) refuse, with nothing written: no line, more than
  :data:`MAX_LINES`, a line with a fee type that is not offered (inactive, or an NSF fee
  type), without a description and without a fee type (the description defaults to the
  fee type's name), with a description over
  :data:`~ledger.models.DESCRIPTION_MAX_LENGTH` characters, a quantity that is not above
  zero or has more than four decimals, a unit amount that is not above zero, not in
  cents or above :data:`~ledger.services.one_off_charges.AMOUNT_MAX`, a line total
  (quantity times unit amount, rounded half up to the cent; never taken from the form)
  of zero or above that maximum, an invoice total above it; a card or bank account
  number in a description or the memo; a due date before today (New York); a lease of
  another operator; an archived operator.
- :func:`preview` is what the preview and print pages show: the lines, the total, the
  operator's and store's details, our draft number (``DRAFT-`` and eight characters)
  until it is sent and the invoice number after, the due date (the draft's, else today
  plus :data:`DUE_DAYS` days, New York).
- :func:`send` (billing; collection ``email``) and :func:`charge_now` (admin; collection
  ``charge_now``, the confirmation of PLAN-010d D4) convert the draft **in one
  transaction**, under the draft's row lock (a second send finds it sent and changes
  nothing): one one-off charge per line through
  :func:`ledger.services.one_off_charges.create_charge` (``with_invoice=False``; category
  the fee type's code when it is a category, ``other`` without a fee type, else
  ``setup`` with the fee type; description the line's text, with the quantity and unit
  amount when the quantity is not 1; price the line total; due the invoice's due date;
  all on the draft's lease or the operator alone), **one** local invoice (our number,
  the memo, the due date, linked to the first line's charge) whose line items carry
  every line and name its charge, and **one** ``invoice_create`` job for the first
  line's charge, whose request carries every line
  (:func:`payments.one_off_invoices.charge_amounts`). Charge now also sets the first
  charge's ``charge_now_key`` and audits the request; the worker queues the single
  ``invoice_pay`` attempt once the invoice is at the provider
  (:func:`payments.jobs.runners.invoice_create_job`). A payment on the invoice pays the
  lines in line order (:func:`ledger.services.one_off_charges.recompute`).
- :func:`cancel` (billing): a draft becomes ``cancelled``; a sent draft's invoice is
  cancelled through :func:`ledger.services.one_off_charges.cancel_charge` on its first
  line's charge (a note is required; refused once anything is paid or pending), which
  voids every line once the provider confirmed the delete.

Every write is audited (entity ``invoice_draft``; the charges as one-off charges).
Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.roles import Role, meets, role_of
from ledger.models import (
    DESCRIPTION_MAX_LENGTH,
    ZERO,
    AuditLog,
    CollectionMethod,
    FeeType,
    Invoice,
    InvoiceDraft,
    InvoiceDraftLine,
    InvoiceDraftStatus,
    InvoiceLineItem,
    InvoiceLineKind,
    InvoiceStatus,
    Lease,
    OneOffCategory,
    OneOffCharge,
    Operator,
    Store,
    format_quantity,
)
from ledger.services import leases, numbering, one_off_charges
from ledger.services.one_off_charges import (
    AMOUNT_MAX,
    CHARGE_NOW_PARAM,
    FORM_CATEGORIES,
    ChargeInput,
)
from ledger.services.rent_run import CENT
from ledger.validators import validate_no_payment_data, visible_text
from payments import jobs
from payments.models import Job, JobKind

ENTITY_DRAFT = "invoice_draft"
#: Days from today (New York) to the due date of an invoice sent without one.
DUE_DAYS = 15
#: Most lines on one invoice.
MAX_LINES = 50
#: Largest quantity a line takes (``numeric(12,4)``).
QUANTITY_MAX = Decimal("99999999.9999")
QUANTITY_STEP = Decimal("0.0001")
#: Longest memo.
MEMO_MAX_LENGTH = 2000

CREATED_ACTION = "invoice_draft.created"
UPDATED_ACTION = "invoice_draft.updated"
SENT_ACTION = "invoice_draft.sent"
CANCELLED_ACTION = "invoice_draft.cancelled"
CANCEL_REQUESTED_ACTION = "invoice_draft.cancel_requested"

# --- Fixed sentences ------------------------------------------------------------------

BILLING_ONLY = "Only billing or an admin can prepare or send an invoice. Nothing was changed."
CHARGE_NOW_ADMIN = one_off_charges.CHARGE_NOW_ADMIN
NO_LINES = "Add at least one line. Nothing was saved."
TOO_MANY_LINES = f"An invoice has at most {MAX_LINES} lines. Nothing was saved."
LINE_FEE_TYPE = "Line {line}: this fee type is not offered. Choose another or write the line."
LINE_DESCRIPTION_REQUIRED = "Line {line}: a description is required when there is no fee type."
LINE_DESCRIPTION_TOO_LONG = (
    f"Line {{line}}: the description can be at most {DESCRIPTION_MAX_LENGTH} characters."
)
LINE_PAYMENT_DATA = "Line {line}: " + one_off_charges.TEXT_PAYMENT_DATA
LINE_QUANTITY = (
    "Line {line}: the quantity must be more than zero, with at most four decimals, and at "
    "most 99,999,999.9999."
)
LINE_UNIT_AMOUNT = (
    "Line {line}: the unit amount must be more than zero, in cents, and at most $999,999.99."
)
LINE_TOTAL_ZERO = "Line {line}: the line total rounds to zero."
LINE_TOTAL_TOO_LARGE = "Line {line}: the line total can be at most $999,999.99."
LINE_SAME_FEE_TYPE = (
    "Line {line}: {name} is already on this invoice; a lease has one charge per fee type."
)
TOTAL_TOO_LARGE = "The invoice total can be at most $999,999.99. Nothing was saved."
MEMO_TOO_LONG = f"The memo can be at most {MEMO_MAX_LENGTH} characters."
MEMO_PAYMENT_DATA = one_off_charges.TEXT_PAYMENT_DATA
DUE_IN_PAST = "The due date cannot be in the past (New York time)."
LEASE_OTHER_OPERATOR = one_off_charges.LEASE_OTHER_OPERATOR
OPERATOR_ARCHIVED = one_off_charges.OPERATOR_ARCHIVED
NO_PROVIDER_CUSTOMER = (
    "The operator has no payment provider customer: the invoice cannot be sent. Nothing was sent."
)
NOT_A_DRAFT = "This invoice is no longer a draft. Nothing was changed."
ALREADY_SENT = "This draft was already sent as invoice {number}. Nothing was changed."
ALREADY_CANCELLED = "This draft is cancelled. Nothing was changed."
CREATED = "Draft {number} saved."
SAVED = "Draft saved."
SENT = (
    "Invoice {number} created. The payment provider e-mails it once it has the invoice; "
    "this page shows the outcome."
)
CHARGE_REQUESTED = (
    "Invoice {number} created. The single debit is made once the payment provider has the "
    "invoice; this page shows the outcome."
)
CANCELLED = "Draft cancelled. Nothing was sent."
#: The internal note of each line's charge.
LINE_NOTE = "Line {line} of invoice {number}, made with the invoice builder from {draft}."
LINE_PREFIX = "Line {line}: "


# --- Values and outcomes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineInput:
    """One line as staff entered it: a fee type or ``None``, a description (blank: the
    fee type's name), a quantity (``None``: 1) and a unit amount."""

    fee_type: FeeType | None
    description: str
    quantity: Decimal | None
    unit_amount: Decimal | None


@dataclass(frozen=True, slots=True)
class CleanLine:
    """A line that passed every rule, with its total."""

    fee_type: FeeType | None
    description: str
    quantity: Decimal
    unit_amount: Decimal
    line_total: Decimal


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    message: str
    #: The form field a refusal belongs to (``None``: the whole form).
    field: str | None = None
    draft: InvoiceDraft | None = None
    invoice: Invoice | None = None
    job: Job | None = None


@dataclass(frozen=True, slots=True)
class Preview:
    """What the preview and print pages show (D7)."""

    draft: InvoiceDraft
    lines: list[InvoiceDraftLine]
    total: Decimal
    company: str
    operator: Operator
    lease: Lease | None
    store: Store | None
    number: str
    issue_date: date
    due_date: date
    memo: str
    invoice: Invoice | None


@dataclass(frozen=True, slots=True)
class ChargeNowDetails:
    """The Charge now confirmation page of a draft, from local data only (10d D4)."""

    refusal: str
    total: Decimal
    last_four: str
    nickname: str
    large_debit: str


class _Refused(Exception):  # noqa: N818 (an outcome, as SetupRefused)
    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome


# --- Reads ----------------------------------------------------------------------------


def offered_fee_types() -> list[FeeType]:
    """The fee types a line may name: the active ones, NSF fee types never (as the setup
    checklist, PLAN-011b D5)."""
    return one_off_charges.checklist_types()


def category_for(fee_type: FeeType | None) -> str:
    """The category of a line's charge: the fee type's code when it is a category,
    ``other`` without a fee type, else ``setup`` (the charge then keeps the fee type)."""
    if fee_type is None:
        return OneOffCategory.OTHER
    if fee_type.code in FORM_CATEGORIES:
        return str(fee_type.code)
    return OneOffCategory.SETUP


def due_date_for(draft: InvoiceDraft, today: date | None = None) -> date:
    """The draft's due date, else today (New York) plus :data:`DUE_DAYS` days."""
    return draft.due_date or (today or timezone.localdate()) + timedelta(days=DUE_DAYS)


def lines_of(draft: InvoiceDraft) -> list[InvoiceDraftLine]:
    return list(draft.lines.select_related("fee_type").order_by("position"))


def total_of(lines: Sequence[InvoiceDraftLine | CleanLine]) -> Decimal:
    return sum((Decimal(line.line_total) for line in lines), ZERO)


def preview(draft: InvoiceDraft, today: date | None = None) -> Preview:
    """The preview of ``draft`` (D7): its invoice's figures once sent."""
    day = today or timezone.localdate()
    lines = lines_of(draft)
    invoice = draft.sent_invoice
    lease = draft.lease
    return Preview(
        draft=draft,
        lines=lines,
        total=invoice.total if invoice is not None else total_of(lines),
        company=str(settings.COMPANY_NAME),
        operator=draft.operator,
        lease=lease,
        store=lease.store if lease is not None else None,
        number=invoice.invoice_number if invoice is not None else draft.number,
        issue_date=invoice.issue_date if invoice is not None else day,
        due_date=invoice.due_date if invoice is not None else due_date_for(draft, day),
        memo=draft.memo,
        invoice=invoice,
    )


def sent_charges(draft: InvoiceDraft) -> list[OneOffCharge]:
    """The charges of a sent draft's invoice, in line order."""
    if draft.sent_invoice_id is None:
        return []
    return [
        line.one_off_charge
        for line in InvoiceLineItem.objects.filter(
            invoice_id=draft.sent_invoice_id, one_off_charge__isnull=False
        )
        .select_related("one_off_charge")
        .order_by("position")
        if line.one_off_charge is not None
    ]


def first_charge(draft: InvoiceDraft) -> OneOffCharge | None:
    """The charge the sent invoice is linked to (its first line's), or ``None``."""
    invoice = draft.sent_invoice
    return invoice.one_off_charge if invoice is not None else None


# --- Validation -----------------------------------------------------------------------


def _is_billing(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.BILLING)


def _is_admin(actor: User | None) -> bool:
    return actor is not None and meets(role_of(actor), Role.ADMIN)


def _guarded(text: str) -> bool:
    try:
        validate_no_payment_data(text)
    except ValidationError:
        return False
    return True


def _clean_line(index: int, line: LineInput, offered: set[uuid.UUID]) -> CleanLine:
    """The line with its total, or :class:`_Refused` with the first rule it breaks."""

    def refuse(template: str, field: str) -> _Refused:
        return _Refused(Outcome(False, template.format(line=index), f"lines-{index - 1}-{field}"))

    fee_type = line.fee_type
    if fee_type is not None and fee_type.pk not in offered:
        raise refuse(LINE_FEE_TYPE, "fee_type")
    description = visible_text(line.description or "")
    if not description and fee_type is not None:
        description = visible_text(fee_type.name)
    if not description:
        raise refuse(LINE_DESCRIPTION_REQUIRED, "description")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise refuse(LINE_DESCRIPTION_TOO_LONG, "description")
    if not _guarded(description):
        raise refuse(LINE_PAYMENT_DATA, "description")
    quantity = Decimal(1) if line.quantity is None else line.quantity
    if (
        not isinstance(quantity, Decimal)
        or not quantity.is_finite()
        or quantity <= 0
        or quantity != quantity.quantize(QUANTITY_STEP)
        or quantity > QUANTITY_MAX
    ):
        raise refuse(LINE_QUANTITY, "quantity")
    unit = line.unit_amount
    if (
        not isinstance(unit, Decimal)
        or not unit.is_finite()
        or unit <= 0
        or unit != unit.quantize(CENT)
        or unit > AMOUNT_MAX
    ):
        raise refuse(LINE_UNIT_AMOUNT, "unit_amount")
    total = (quantity * unit).quantize(CENT, rounding=ROUND_HALF_UP)
    if total <= 0:
        raise refuse(LINE_TOTAL_ZERO, "quantity")
    if total > AMOUNT_MAX:
        raise refuse(LINE_TOTAL_TOO_LARGE, "quantity")
    return CleanLine(fee_type, description, quantity.quantize(QUANTITY_STEP), unit, total)


def clean_lines(lines: Sequence[LineInput], *, lease: Lease | None) -> list[CleanLine]:
    """Every line checked and totalled, in order; :class:`_Refused` at the first rule
    one breaks. On a lease, a fee type billed as a setup charge appears once (a lease has
    one charge per fee type)."""
    if not lines:
        raise _Refused(Outcome(False, NO_LINES))
    if len(lines) > MAX_LINES:
        raise _Refused(Outcome(False, TOO_MANY_LINES))
    offered = {fee_type.pk for fee_type in offered_fee_types()}
    cleaned = [_clean_line(index, line, offered) for index, line in enumerate(lines, start=1)]
    if lease is not None:
        seen: set[uuid.UUID] = set()
        for index, line in enumerate(cleaned, start=1):
            if line.fee_type is None or category_for(line.fee_type) != OneOffCategory.SETUP:
                continue
            if line.fee_type.pk in seen:
                raise _Refused(
                    Outcome(
                        False,
                        LINE_SAME_FEE_TYPE.format(line=index, name=line.fee_type.name),
                        f"lines-{index - 1}-fee_type",
                    )
                )
            seen.add(line.fee_type.pk)
    if total_of(cleaned) > AMOUNT_MAX:
        raise _Refused(Outcome(False, TOTAL_TOO_LARGE))
    return cleaned


def _header_refusal(
    operator: Operator, lease: Lease | None, memo: str, due_date: date | None, today: date
) -> Outcome | None:
    if operator.archived_at is not None:
        return Outcome(False, OPERATOR_ARCHIVED)
    if lease is not None and lease.operator_id != operator.pk:
        return Outcome(False, LEASE_OTHER_OPERATOR, "lease")
    if len(memo) > MEMO_MAX_LENGTH:
        return Outcome(False, MEMO_TOO_LONG, "memo")
    if not _guarded(memo):
        return Outcome(False, MEMO_PAYMENT_DATA, "memo")
    if due_date is not None and due_date < today:
        return Outcome(False, DUE_IN_PAST, "due_date")
    return None


# --- Drafts ---------------------------------------------------------------------------


def create_draft(
    operator: Operator,
    lease: Lease | None,
    memo: str,
    due_date: date | None,
    lines: Sequence[LineInput],
    actor: User | None,
    *,
    today: date | None = None,
    creation_key: uuid.UUID | None = None,
) -> Outcome:
    """Save a new draft and its lines (billing); nothing reaches the provider. A form
    sent again with the same one-time ``creation_key`` returns the draft the first one
    made (looked up under the operator's row lock)."""
    if not _is_billing(actor):
        return Outcome(False, BILLING_ONLY)
    day = today or timezone.localdate()
    text = visible_text(memo or "")
    refused = _header_refusal(operator, lease, text, due_date, day)
    if refused is not None:
        return refused
    try:
        cleaned = clean_lines(lines, lease=lease)
    except _Refused as error:
        return error.outcome
    with transaction.atomic():
        Operator.objects.select_for_update().get(pk=operator.pk)
        earlier = _draft_for_key(creation_key)
        if earlier is not None:
            return Outcome(True, CREATED.format(number=earlier.number), draft=earlier)
        draft = InvoiceDraft.objects.create(
            operator=operator,
            lease=lease,
            memo=text,
            due_date=due_date,
            status=InvoiceDraftStatus.DRAFT,
            created_by=actor,
        )
        _write_lines(draft, cleaned)
        _audit(
            actor,
            CREATED_ACTION,
            draft,
            None,
            {**draft_state(draft), "creation_key": str(creation_key) if creation_key else None},
        )
    return Outcome(True, CREATED.format(number=draft.number), draft=draft)


def _draft_for_key(creation_key: uuid.UUID | None) -> InvoiceDraft | None:
    if creation_key is None:
        return None
    row = AuditLog.objects.filter(
        action=CREATED_ACTION, entity_type=ENTITY_DRAFT, after__creation_key=str(creation_key)
    ).first()
    return InvoiceDraft.objects.filter(pk=row.entity_id).first() if row is not None else None


def update_draft(
    draft_id: uuid.UUID,
    lease: Lease | None,
    memo: str,
    due_date: date | None,
    lines: Sequence[LineInput],
    actor: User | None,
    *,
    today: date | None = None,
) -> Outcome:
    """Change a draft (billing) while it is a draft: the lease, the memo, the due date,
    and the lines, replaced as a set."""
    if not _is_billing(actor):
        return Outcome(False, BILLING_ONLY)
    day = today or timezone.localdate()
    text = visible_text(memo or "")
    with transaction.atomic():
        draft = (
            InvoiceDraft.objects.select_for_update(of=("self",))
            .select_related("operator")
            .get(pk=draft_id)
        )
        if draft.status != InvoiceDraftStatus.DRAFT:
            return Outcome(False, NOT_A_DRAFT, draft=draft)
        refused = _header_refusal(draft.operator, lease, text, due_date, day)
        if refused is not None:
            return refused
        try:
            cleaned = clean_lines(lines, lease=lease)
        except _Refused as error:
            return error.outcome
        before = draft_state(draft)
        draft.lease = lease
        draft.memo = text
        draft.due_date = due_date
        draft.save(update_fields=["lease", "memo", "due_date", "updated_at"])
        draft.lines.all().delete()
        _write_lines(draft, cleaned)
        after = draft_state(draft)
        if after != before:
            _audit(actor, UPDATED_ACTION, draft, before, after)
    return Outcome(True, SAVED, draft=draft)


def _write_lines(draft: InvoiceDraft, lines: Sequence[CleanLine]) -> None:
    InvoiceDraftLine.objects.bulk_create(
        [
            InvoiceDraftLine(
                draft=draft,
                position=position,
                fee_type=line.fee_type,
                description=line.description,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                line_total=line.line_total,
            )
            for position, line in enumerate(lines, start=1)
        ]
    )


# --- Send and Charge now --------------------------------------------------------------


def charge_description(line: InvoiceDraftLine | CleanLine) -> str:
    """The charge's description: the line's text, followed by the quantity and the unit
    amount when the quantity is not 1, cut to fit
    :data:`~ledger.models.DESCRIPTION_MAX_LENGTH`."""
    quantity = Decimal(line.quantity)
    if quantity == 1:
        return line.description
    suffix = f" ({format_quantity(quantity)} × {Decimal(line.unit_amount)})"
    return line.description[: DESCRIPTION_MAX_LENGTH - len(suffix)].rstrip() + suffix


def _charge_now_refusal(operator: Operator, total: Decimal) -> str:
    """Why the draft's total cannot be charged now, or ``""`` (10d D4; local data)."""
    if not one_off_charges.charge_now_enabled():
        return one_off_charges.CHARGE_NOW_DISABLED
    if not leases.customer_ref(operator):
        return NO_PROVIDER_CUSTOMER
    if not leases.linked_payment_method(operator):
        return one_off_charges.CHARGE_NOW_NO_METHOD
    method = leases.linked_method_record(operator)
    if method is None or not method.is_usable_bank:
        return one_off_charges.CHARGE_NOW_METHOD_NOT_STORED
    return leases.provider_limit_refusal([total])


def charge_now_details(draft: InvoiceDraft, actor: User | None) -> ChargeNowDetails:
    """The confirmation page of Charge now on a draft (admin). Never calls the provider."""
    total = total_of(lines_of(draft))
    if not _is_admin(actor):
        refusal = CHARGE_NOW_ADMIN
    elif draft.status != InvoiceDraftStatus.DRAFT:
        refusal = NOT_A_DRAFT
    else:
        refusal = _charge_now_refusal(draft.operator, total)
    method = leases.linked_method_record(draft.operator)
    return ChargeNowDetails(
        refusal=refusal,
        total=total,
        last_four=method.last_four if method is not None else "",
        nickname=method.nickname if method is not None else "",
        large_debit=leases.large_debit_confirmation([total]),
    )


def send(draft_id: uuid.UUID, actor: User | None, *, today: date | None = None) -> Outcome:
    """Send the draft by e-mail (billing): see the module docstring."""
    if not _is_billing(actor):
        return Outcome(False, BILLING_ONLY)
    return _convert(draft_id, actor, CollectionMethod.EMAIL, confirmed=False, today=today)


def charge_now(
    draft_id: uuid.UUID, actor: User | None, *, confirmed: bool, today: date | None = None
) -> Outcome:
    """Charge the draft now (admin, the 10d confirmation): see the module docstring."""
    if not _is_admin(actor):
        return Outcome(False, CHARGE_NOW_ADMIN)
    return _convert(
        draft_id, actor, CollectionMethod.CHARGE_NOW, confirmed=confirmed is True, today=today
    )


def _convert(
    draft_id: uuid.UUID,
    actor: User | None,
    collection: CollectionMethod,
    *,
    confirmed: bool,
    today: date | None,
) -> Outcome:
    day = today or timezone.localdate()
    with transaction.atomic():
        draft = (
            InvoiceDraft.objects.select_for_update(of=("self",))
            .select_related("operator", "lease", "sent_invoice")
            .get(pk=draft_id)
        )
        if draft.status == InvoiceDraftStatus.SENT and draft.sent_invoice is not None:
            number = draft.sent_invoice.invoice_number
            return Outcome(False, ALREADY_SENT.format(number=number), draft=draft)
        if draft.status == InvoiceDraftStatus.CANCELLED:
            return Outcome(False, ALREADY_CANCELLED, draft=draft)
        operator = Operator.objects.select_for_update().get(pk=draft.operator_id)
        lease = draft.lease
        refused = _header_refusal(operator, lease, draft.memo, draft.due_date, day)
        if refused is not None:
            return Outcome(False, refused.message, refused.field, draft=draft)
        stored = lines_of(draft)
        try:
            lines = clean_lines(
                [
                    LineInput(line.fee_type, line.description, line.quantity, line.unit_amount)
                    for line in stored
                ],
                lease=lease,
            )
        except _Refused as error:
            return Outcome(False, error.outcome.message, error.outcome.field, draft=draft)
        total = total_of(lines)
        if not leases.customer_ref(operator):
            return Outcome(False, NO_PROVIDER_CUSTOMER, draft=draft)
        large = leases.is_large_debit(total)
        if collection == CollectionMethod.CHARGE_NOW:
            refusal = _charge_now_refusal(operator, total)
            if refusal:
                return Outcome(False, refusal, draft=draft)
            if large and not confirmed:
                return Outcome(
                    False, leases.large_debit_confirmation([total]), "confirm", draft=draft
                )
        due = due_date_for(draft, day)
        number = numbering.next_invoice_number(day.year)
        charges = []
        for position, line in enumerate(lines, start=1):
            category = category_for(line.fee_type)
            item = ChargeInput(
                fee_type=line.fee_type if category == OneOffCategory.SETUP else None,
                price=line.line_total,
                cost=None,
                vendor="",
                due_date=due,
                note=LINE_NOTE.format(line=position, number=number, draft=draft.number),
                category=category,
                collection=collection,
                description=charge_description(line),
                # The large-debit confirmation is the invoice total's, checked above.
                confirmed=True,
            )
            made = one_off_charges.create_charge(
                operator, lease, item, actor, today=day, with_invoice=False
            )
            if not made.ok or made.charge is None:
                # Nothing is kept: the charges made so far and the number go back.
                transaction.set_rollback(True)
                prefix = LINE_PREFIX.format(line=position)
                return Outcome(False, prefix + made.message, draft=draft)
            charges.append(made.charge)
        invoice = Invoice.objects.create(
            invoice_number=number,
            operator=operator,
            one_off_charge=charges[0],
            issue_date=day,
            due_date=due,
            memo=draft.memo,
            subtotal=total,
            tax=ZERO,
            fees=ZERO,
            surcharge=ZERO,
            total=total,
            internal_status=InvoiceStatus.DRAFT,
            allow_card=False,
            allow_ach=True,
            legacy=False,
        )
        InvoiceLineItem.objects.bulk_create(
            [
                InvoiceLineItem(
                    invoice=invoice,
                    position=position,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_amount,
                    line_total=line.line_total,
                    one_off_charge=charge,
                    kind=InvoiceLineKind.CHARGE,
                )
                for position, (line, charge) in enumerate(zip(lines, charges, strict=True), 1)
            ]
        )
        first = charges[0]
        params: dict[str, object] | None = None
        if collection == CollectionMethod.CHARGE_NOW:
            first.charge_now_key = uuid.uuid4()
            first.save(update_fields=["charge_now_key", "updated_at"])
            _charge_audit(
                actor,
                one_off_charges.CHARGE_NOW_REQUESTED_ACTION,
                first,
                {"amount": str(total), "invoice_number": number, "large_debit_confirmed": large},
            )
            if large:
                _charge_audit(
                    actor,
                    one_off_charges.LARGE_DEBIT_ACTION,
                    first,
                    {
                        "amount": str(total),
                        "threshold": str(settings.LARGE_DEBIT_WARNING_AMOUNT),
                        "confirmed_by": actor.pk if actor is not None else None,
                    },
                )
            params = {CHARGE_NOW_PARAM: True, "confirmed": confirmed}
        job = jobs.enqueue(JobKind.INVOICE_CREATE, first, params, actor)
        before = draft_state(draft)
        draft.status = InvoiceDraftStatus.SENT
        draft.sent_invoice = invoice
        draft.save(update_fields=["status", "sent_invoice", "updated_at"])
        _audit(
            actor,
            SENT_ACTION,
            draft,
            before,
            {
                **draft_state(draft),
                "invoice_number": number,
                "collection": collection.value,
                "charges": [str(charge.pk) for charge in charges],
                "due_date": due.isoformat(),
            },
        )
    template = CHARGE_REQUESTED if collection == CollectionMethod.CHARGE_NOW else SENT
    return Outcome(True, template.format(number=number), draft=draft, invoice=invoice, job=job)


# --- Cancel ---------------------------------------------------------------------------


def cancel(draft_id: uuid.UUID, actor: User | None, *, note: str = "") -> Outcome:
    """Cancel a draft (billing): a draft becomes ``cancelled``; a sent one's invoice is
    cancelled through :func:`ledger.services.one_off_charges.cancel_charge` on its first
    line's charge, which voids every line once the provider confirmed."""
    if not _is_billing(actor):
        return Outcome(False, BILLING_ONLY)
    with transaction.atomic():
        draft = (
            InvoiceDraft.objects.select_for_update(of=("self",))
            .select_related("sent_invoice")
            .get(pk=draft_id)
        )
        if draft.status == InvoiceDraftStatus.CANCELLED:
            return Outcome(False, ALREADY_CANCELLED, draft=draft)
        if draft.status == InvoiceDraftStatus.DRAFT:
            before = draft_state(draft)
            draft.status = InvoiceDraftStatus.CANCELLED
            draft.save(update_fields=["status", "updated_at"])
            _audit(actor, CANCELLED_ACTION, draft, before, draft_state(draft))
            return Outcome(True, CANCELLED, draft=draft)
        first = first_charge(draft)
    if first is None:  # a sent draft always has its invoice's first charge
        return Outcome(False, NOT_A_DRAFT, draft=draft)
    outcome = one_off_charges.cancel_charge(first.pk, note=note, actor=actor)
    if outcome.ok:
        _audit(
            actor,
            CANCEL_REQUESTED_ACTION,
            draft,
            None,
            {"invoice_number": draft.sent_invoice.invoice_number if draft.sent_invoice else None},
        )
    return Outcome(outcome.ok, outcome.message, outcome.field, draft=draft, job=outcome.job)


# --- Helpers --------------------------------------------------------------------------


def draft_state(draft: InvoiceDraft) -> dict[str, Any]:
    """The audited fields of a draft, its lines included."""
    lines = lines_of(draft)
    return {
        "operator": str(draft.operator_id),
        "lease": str(draft.lease_id) if draft.lease_id else None,
        "memo": draft.memo,
        "due_date": draft.due_date.isoformat() if draft.due_date else None,
        "status": draft.status,
        "lines": [
            {
                "position": line.position,
                "fee_type": line.fee_type.code if line.fee_type is not None else None,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_amount": str(line.unit_amount),
                "line_total": str(line.line_total),
            }
            for line in lines
        ],
        "total": str(total_of(lines)),
    }


def _audit(
    actor: User | None,
    action: str,
    draft: InvoiceDraft,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=ENTITY_DRAFT,
        entity_id=str(draft.pk),
        before=before,
        after=after,
    )


def _charge_audit(
    actor: User | None, action: str, charge: OneOffCharge, after: dict[str, Any]
) -> None:
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
        entity_id=str(charge.pk),
        before=None,
        after=after,
    )
