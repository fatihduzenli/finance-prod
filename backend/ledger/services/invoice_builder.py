"""The invoice builder (docs/qa/PLAN-012-contact-log-invoice-builder-disputes.md D6, D7, D9).

A **draft** (:class:`~ledger.models.InvoiceDraft`) is local only: an operator, optionally
one of its leases, a memo, an optional due date and one or more lines, each an item, an
optional detail (the description), a quantity and a unit amount. Nothing reaches the
payment provider while it is a draft.

An **item** (docs/qa/PLAN-012b-builder-item-combo.md D1) is a name: one that matches an
offered catalogue item's name (:func:`offered_fee_types`; exactly, ignoring case and
surrounding spaces; the first in catalogue order when two match) *is* that item and the
line names its fee type; any other name is a typed item, without a fee type. A typed
item may be saved to the catalogue (``save_item``, D4): on Save draft and Edit, inside
the same transaction and under the operator's row lock, a fee type is created with the
name, a slug code (ASCII, at most 64 characters, ``-2``, ``-3`` ... on a collision; never
an NSF code or a category's code), the line's unit amount as its default price, last in
the catalogue, audited ``fee_type.created`` with the draft named; every line of the draft
with that name then names it. A name held by an inactive or NSF fee type is refused for
saving (:data:`LINE_SAVE_NOT_OFFERED`).

- :func:`create_draft` and :func:`update_draft` (billing; the lines are replaced as a
  set while the draft is a draft) refuse, with nothing written: no line, more than
  :data:`MAX_LINES`, a line without an item, with an item over
  :data:`~ledger.models.NAME_MAX_LENGTH` characters, with a description over
  :data:`~ledger.models.DESCRIPTION_MAX_LENGTH` characters, a quantity that is not above
  zero or has more than four decimals, a unit amount that is not above zero, not in
  cents or above :data:`~ledger.services.one_off_charges.AMOUNT_MAX`, a line total
  (quantity times unit amount, rounded half up to the cent; never taken from the form)
  of zero or above that maximum, an invoice total above it; a card or bank account
  number in an item, a description or the memo; Save as catalogue item on a name an
  inactive or NSF fee type holds; a due date before today (New York); a lease of
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
  ``setup`` with the fee type; description the line's item, with the quantity and unit
  amount when the quantity is not 1; the line's detail appended to its internal note;
  price the line total; due the invoice's due date;
  all on the draft's lease or the operator alone), **one** local invoice (our number,
  the memo, the due date, linked to the first line's charge) whose line items carry
  every line (the item as the text, the description as the detail) and name its charge,
  and **one** ``invoice_create`` job for the first
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
- :func:`delete_draft` (billing; PLAN-012d D3): a never-sent draft is deleted with its
  lines, audited ``invoice_draft.deleted``; a sent invoice keeps the cancel above.

**Rent lines** (docs/qa/PLAN-012b-b-catch-up-rent-lines-wizard-fixes.md D4 to D7): a line
may name a lease of the draft's operator and a month (``rent_lease``, ``rent_month``)
instead of an item. :func:`clean_lines` refuses, with nothing written: a lease or a month
without the other, a lease of another operator, a month that is not a first day, before
the lease's start month or (an ended lease) after its end month, a lease without a start
date, a lease debited twice a month (its months are the rent run's), a month with no
rent in effect, a lease whose rent autopay schedule is live (it would debit the month
too), a quantity other than 1, the same lease and month twice on the draft
(:data:`LINE_RENT_TWICE`) or on another open draft of the operator, a month whose charge
is paid, partly paid, pending, waived, on a live invoice or on a live installment plan
(:data:`LINE_RENT_BILLED`), an amount other than an unpaid charge's own, and an invoice
of more than one rent line and nothing else. The amount defaults (an empty unit amount)
to the charge's when the run made it, else the rent in effect that month
(:func:`ledger.services.rent_run.first_month`: prorated for a partial start month); the
item is "Rent <Month YYYY>, store <label>" and the detail, unless typed, the proration
text. On send the line's charge is made or taken by
:func:`ledger.services.rent_run.create_month` (due the invoice's due date), its
``billed_by_invoice`` set and the invoice line names it (``rent_charge``); an invoice of
one rent line alone is linked to that charge (``invoices.rent_charge``) and its job acts
on it, any other invoice is linked to its first one-off line's charge as above. A payment
on the invoice pays the rent charge as it pays a later line
(:func:`ledger.services.one_off_charges.rent_share`). Never a provider call here.

**Existing-charge lines** (docs/qa/PLAN-012d-store-workspace.md D10): a line may name an
open one-off charge of the draft's lease (``existing_charge``) instead of an item:
:func:`open_charges` lists them (unpaid, nothing paid or pending, no live invoice,
provider schedule or Charge now, no installment plan, not an installment, a companion
fee or a legacy row; :func:`existing_charge_closed`). :func:`clean_lines` refuses, with
nothing written: a charge of another lease or operator (or a draft without a lease), a
charge that is no longer open, a quantity other than 1, an amount other than the
charge's balance (an empty one is the balance; above is :data:`LINE_EXISTING_AMOUNT`,
below :data:`LINE_EXISTING_BELOW`: the builder never reprices a charge, D10 amended of
2026-10-16; staff edit the charge first), the same charge twice on the draft
(:data:`LINE_EXISTING_TWICE`) or on another open draft of the operator. The item is the
charge's label. On send the charge is **attached** to the invoice instead of a new one
being made (:func:`_attach_existing`, under its row lock, checked again, the line's
amount still its balance, else :data:`LINE_EXISTING_CHANGED`): its collection becomes the
invoice's (its price never changes), the invoice line names it, audited
``one_off_charge.attached_to_invoice``.

:func:`rent_amount` is the amount a Rent line takes by default (the endpoint
``invoices:rent_amount`` fills the line with it): the run's charge's, else
:func:`ledger.services.rent_run.first_month`'s, with the proration text; or why the
month cannot be billed.

Every write is audited (entity ``invoice_draft``; the charges as one-off charges).
Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.text import slugify

from accounts.roles import Role, meets, role_of
from ledger.models import (
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    NSF_FEE_CODES,
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
    LeaseTransfer,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    RentCharge,
    RentChargeStatus,
    ScheduleKind,
    Store,
    format_quantity,
)
from ledger.services import leases, numbering, one_off_charges, rent_run
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
DELETED_ACTION = "invoice_draft.deleted"
CANCEL_REQUESTED_ACTION = "invoice_draft.cancel_requested"
#: A catalogue item saved from the builder (D4); the admin's own is ``.created_in_admin``.
ENTITY_FEE_TYPE = "fee_type"
FEE_TYPE_CREATED_ACTION = "fee_type.created"
#: Longest fee type code (``fee_types.code``).
CODE_MAX_LENGTH = 64
#: The code of a saved item whose name has no ASCII letter or digit.
CODE_FALLBACK = "item"
#: Codes a saved item never takes: the NSF fee types', and the categories' (a fee type
#: with such a code bills under that category, not as a catalogue item).
RESERVED_CODES = frozenset((*NSF_FEE_CODES, *FORM_CATEGORIES))
#: Tries at a free code when a concurrent save takes the one found.
SAVE_ATTEMPTS = 5

# --- Fixed sentences ------------------------------------------------------------------

BILLING_ONLY = "Only billing or an admin can prepare or send an invoice. Nothing was changed."
CHARGE_NOW_ADMIN = one_off_charges.CHARGE_NOW_ADMIN
NO_LINES = "Add at least one line. Nothing was saved."
TOO_MANY_LINES = f"An invoice has at most {MAX_LINES} lines. Nothing was saved."
LINE_ITEM_REQUIRED = "Line {line}: an item is required."
LINE_ITEM_TOO_LONG = f"Line {{line}}: the item can be at most {NAME_MAX_LENGTH} characters."
LINE_ITEM_PAYMENT_DATA = "Line {line}: " + one_off_charges.TEXT_PAYMENT_DATA
LINE_SAVE_NOT_OFFERED = (
    "Line {line}: A catalogue item with this name exists but is not offered; an admin can "
    "change it in the catalogue."
)
LINE_SAVE_BUSY = (
    "Line {line}: the catalogue item could not be saved because the catalogue changed at "
    "the same moment. Nothing was saved; try again."
)
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
LINE_RENT_INCOMPLETE = "Line {line}: choose the lease and the month of the rent."
LINE_RENT_LEASE = "Line {line}: choose one of this operator's leases for the rent."
LINE_RENT_MONTH = "Line {line}: the rent month must be a month (its first day)."
LINE_RENT_NO_START = "Line {line}: the lease of store {store} has no start date yet."
LINE_RENT_BEFORE_START = (
    "Line {line}: the lease of store {store} starts in {start}; the rent month cannot be before it."
)
LINE_RENT_AFTER_END = (
    "Line {line}: the lease of store {store} ended in {end}; the rent month cannot be after it."
)
LINE_RENT_TWICE_MONTHLY = (
    "Line {line}: the lease of store {store} is debited twice a month; its rent is billed "
    "by the rent run."
)
LINE_RENT_NO_RENT = "Line {line}: no rent is in effect in {month} on the lease of store {store}."
LINE_RENT_AUTOPAY = (
    "Line {line}: the rent of store {store} is collected by autopay; stop autopay before "
    "billing it by invoice."
)
LINE_RENT_QUANTITY = "Line {line}: a rent line's quantity is 1."
LINE_RENT_TWICE = "Line {line}: rent {month} of store {store} is already on this invoice."
LINE_RENT_ON_DRAFT = "Line {line}: rent {month} of store {store} is already on draft {draft}."
LINE_RENT_BILLED = "Line {line}: rent {month} of store {store} is already billed or paid."
LINE_RENT_WAIVED = "Line {line}: rent {month} of store {store} is waived."
LINE_RENT_AMOUNT = (
    "Line {line}: rent {month} of store {store} is already charged at {amount}; the line's "
    "amount must be {amount}."
)
LINE_EXISTING_INVALID = "Line {line}: a line bills either rent or an existing charge."
LINE_EXISTING_OTHER = "Line {line}: choose an open charge of this draft's lease."
LINE_EXISTING_NOT_OPEN = (
    "Line {line}: {charge} is no longer open (paid, pending, billed, scheduled or on an "
    "installment plan)."
)
LINE_EXISTING_QUANTITY = "Line {line}: an existing charge's quantity is 1."
LINE_EXISTING_AMOUNT = "Line {line}: the amount of {charge} can be at most its balance, {balance}."
LINE_EXISTING_BELOW = (
    "Line {line}: {charge} is billed at its balance, {balance}. To change the amount, edit the "
    "charge first."
)
LINE_EXISTING_CHANGED = (
    "Line {line}: {charge} changed meanwhile; its balance is now {balance}. Edit the draft and "
    "send it again."
)
LINE_EXISTING_TWICE = "Line {line}: {charge} is already on this invoice."
LINE_EXISTING_ON_DRAFT = "Line {line}: {charge} is already on draft {draft}."
#: The Item box's text of an existing open charge (PLAN-012d D10).
EXISTING_PREFIX = "Existing open charge: "
EXISTING_LABEL = EXISTING_PREFIX + "{charge} ({balance})"
ATTACHED_ACTION = "one_off_charge.attached_to_invoice"
RENT_ALONE = (
    "An invoice of rent alone has one rent line: bill the other months on their own "
    "invoices or with another line. Nothing was saved."
)
RENT_ALONE_CHARGE_NOW = (
    "Charge now needs a line other than rent on the invoice. Nothing was charged."
)
RENT_ALONE_CANCEL = (
    "This invoice bills rent alone; it cannot be cancelled from the invoice builder. "
    "Nothing was changed."
)
#: The item of a rent line (PLAN-012b-b D4).
RENT_ITEM = "Rent {month}, store {store}"
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
DELETED = "Draft {number} deleted. Nothing was sent."
DELETE_SENT = (
    "This invoice was sent; it cannot be deleted. Cancel it from its page. Nothing was changed."
)
DELETE_TRANSFER = (
    "This draft belongs to a transfer; cancel it from its page instead. Nothing was changed."
)
#: The internal note of each line's charge.
LINE_NOTE = "Line {line} of invoice {number}, made with the invoice builder from {draft}."
#: Appended to that note when the line has a detail (D3).
LINE_NOTE_DETAIL = " Detail: {detail}"
LINE_PREFIX = "Line {line}: "


# --- Values and outcomes --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineInput:
    """One line as staff entered it: an item (a name; resolved to a catalogue item by
    :func:`clean_lines`), an optional detail (``description``), a quantity (``None``: 1),
    a unit amount, and whether a typed item is saved to the catalogue (D4). A Rent line
    (PLAN-012b-b D4) names a lease and a month instead of an item (``None``: the
    default amount)."""

    item: str
    description: str
    quantity: Decimal | None
    unit_amount: Decimal | None
    save_item: bool = False
    rent_lease: uuid.UUID | None = None
    rent_month: date | None = None
    #: An open one-off charge of the draft's lease the line bills (PLAN-012d D10).
    existing_charge: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CleanLine:
    """A line that passed every rule, with its total: ``fee_type`` is the offered
    catalogue item its ``item`` names, else ``None``; ``save_item`` is true only on a
    typed item to be saved to the catalogue."""

    item: str
    fee_type: FeeType | None
    description: str
    quantity: Decimal
    unit_amount: Decimal
    line_total: Decimal
    save_item: bool = False
    #: A Rent line's lease and month (PLAN-012b-b D4), and the proration text of the
    #: month when its amount is the prorated default.
    rent_lease: Lease | None = None
    rent_month: date | None = None
    proration_text: str = ""
    #: An existing-charge line's charge (PLAN-012d D10).
    existing_charge: OneOffCharge | None = None

    @property
    def is_rent(self) -> bool:
        return self.rent_lease is not None

    @property
    def is_existing(self) -> bool:
        return self.existing_charge is not None


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
    """The fee types a line may name: the active ones, NSF fee types never (PLAN-011b
    D5), whether or not the setup checklist shows them (PLAN-012d D9)."""
    return one_off_charges.offered_types()


def catalogue_key(name: str) -> str:
    """What two item names are compared by: case and surrounding spaces ignored."""
    return visible_text(name or "").casefold()


def _offered_by_name(offered: Sequence[FeeType]) -> dict[str, FeeType]:
    """Offered fee types by :func:`catalogue_key` of their names; the first in catalogue
    order when two share a name."""
    by_name: dict[str, FeeType] = {}
    for fee_type in offered:
        by_name.setdefault(catalogue_key(fee_type.name), fee_type)
    return by_name


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
    return list(
        draft.lines.select_related("fee_type", "rent_lease", "existing_charge").order_by("position")
    )


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
    """The charge the sent invoice is linked to (its first one-off line's), or ``None``
    (an invoice of rent alone is linked to its rent charge, PLAN-012b-b D5)."""
    invoice = draft.sent_invoice
    return invoice.one_off_charge if invoice is not None else None


def sent_rent_charges(draft: InvoiceDraft) -> list[RentCharge]:
    """The rent charges of a sent draft's Rent lines, in line order (PLAN-012b-b D5)."""
    if draft.sent_invoice_id is None:
        return []
    return list(
        RentCharge.objects.filter(invoice_lines__invoice_id=draft.sent_invoice_id)
        .select_related("lease__store")
        .order_by("invoice_lines__position")
    )


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


def _clean_line(
    index: int, line: LineInput, by_name: dict[str, FeeType], held: set[str]
) -> CleanLine:
    """The line with its total and its catalogue item, or :class:`_Refused` with the
    first rule it breaks. ``held`` are the names of the fee types that are not offered."""

    def refuse(template: str, field: str) -> _Refused:
        return _Refused(Outcome(False, template.format(line=index), f"lines-{index - 1}-{field}"))

    item = visible_text(line.item or "")
    if not item:
        raise refuse(LINE_ITEM_REQUIRED, "item")
    if len(item) > NAME_MAX_LENGTH:
        raise refuse(LINE_ITEM_TOO_LONG, "item")
    if not _guarded(item):
        raise refuse(LINE_ITEM_PAYMENT_DATA, "item")
    fee_type = by_name.get(catalogue_key(item))
    if fee_type is not None:
        item = visible_text(fee_type.name)
    save_item = line.save_item is True and fee_type is None
    if save_item and catalogue_key(item) in held:
        raise refuse(LINE_SAVE_NOT_OFFERED, "save_item")
    description = visible_text(line.description or "")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise refuse(LINE_DESCRIPTION_TOO_LONG, "description")
    if not _guarded(description):
        raise refuse(LINE_PAYMENT_DATA, "description")
    quantity = _quantity(index, line)
    unit = _unit_amount(index, line.unit_amount)
    total = (quantity * unit).quantize(CENT, rounding=ROUND_HALF_UP)
    if total <= 0:
        raise refuse(LINE_TOTAL_ZERO, "quantity")
    if total > AMOUNT_MAX:
        raise refuse(LINE_TOTAL_TOO_LARGE, "quantity")
    return CleanLine(
        item,
        fee_type,
        description,
        quantity.quantize(QUANTITY_STEP),
        unit,
        total,
        save_item,
    )


def _line_refusal(index: int, template: str, field: str, **words: object) -> _Refused:
    return _Refused(
        Outcome(False, template.format(line=index, **words), f"lines-{index - 1}-{field}")
    )


def _quantity(index: int, line: LineInput) -> Decimal:
    quantity = Decimal(1) if line.quantity is None else line.quantity
    if (
        not isinstance(quantity, Decimal)
        or not quantity.is_finite()
        or quantity <= 0
        or quantity != quantity.quantize(QUANTITY_STEP)
        or quantity > QUANTITY_MAX
    ):
        raise _line_refusal(index, LINE_QUANTITY, "quantity")
    return quantity


def _unit_amount(index: int, unit: Decimal | None) -> Decimal:
    if (
        not isinstance(unit, Decimal)
        or not unit.is_finite()
        or unit <= 0
        or unit != unit.quantize(CENT)
        or unit > AMOUNT_MAX
    ):
        raise _line_refusal(index, LINE_UNIT_AMOUNT, "unit_amount")
    return unit


def month_label(month: date) -> str:
    """``October 2026``."""
    return f"{month:%B %Y}"


def rent_charge_refusal(charge: RentCharge) -> str:
    """Why a Rent line cannot take the month's existing charge, as a template of
    :data:`LINE_RENT_BILLED` or :data:`LINE_RENT_WAIVED`, or ``""`` (PLAN-012b-b D4, D5):
    paid, partly paid or pending; on a live invoice (a local one not yet at the provider
    included); on a live installment plan."""
    if charge.status == RentChargeStatus.WAIVED:
        return LINE_RENT_WAIVED
    if (
        charge.status in (RentChargeStatus.PAID, RentChargeStatus.PARTIAL, RentChargeStatus.PENDING)
        or charge.amount_paid > 0
        or rent_run.on_live_plan(rent_charge=charge)
        or any(
            one_off_charges.invoice_is_live(invoice) for invoice in rent_run.charge_invoices(charge)
        )
    ):
        return LINE_RENT_BILLED
    return ""


def _clean_rent_line(index: int, line: LineInput, operator: Operator | None) -> CleanLine:
    """A Rent line checked (PLAN-012b-b D4; see the module docstring), with its item, its
    detail and its amount, or :class:`_Refused`."""
    if line.rent_lease is None or line.rent_month is None:
        raise _line_refusal(index, LINE_RENT_INCOMPLETE, "rent_lease")
    lease = (
        Lease.objects.select_related("store").filter(pk=line.rent_lease).first()
        if isinstance(line.rent_lease, uuid.UUID)
        else None
    )
    if lease is None or operator is None or lease.operator_id != operator.pk:
        raise _line_refusal(index, LINE_RENT_LEASE, "rent_lease")
    month = line.rent_month
    if not isinstance(month, date) or month.day != 1:
        raise _line_refusal(index, LINE_RENT_MONTH, "rent_month")
    words: dict[str, object] = {"store": lease.store.external_label, "month": month_label(month)}
    if lease.started_on is None:
        raise _line_refusal(index, LINE_RENT_NO_START, "rent_lease", **words)
    if month < lease.started_on.replace(day=1):
        start = month_label(lease.started_on)
        raise _line_refusal(index, LINE_RENT_BEFORE_START, "rent_month", start=start, **words)
    if lease.ended_on is not None and month > lease.ended_on.replace(day=1):
        end = month_label(lease.ended_on)
        raise _line_refusal(index, LINE_RENT_AFTER_END, "rent_month", end=end, **words)
    if lease.second_debit_day is not None:
        raise _line_refusal(index, LINE_RENT_TWICE_MONTHLY, "rent_lease", **words)
    if any(row.kind == ScheduleKind.RENT for row, _ in leases.stoppable_schedules(lease)):
        raise _line_refusal(index, LINE_RENT_AUTOPAY, "rent_lease", **words)
    if line.quantity is not None and line.quantity != 1:
        raise _line_refusal(index, LINE_RENT_QUANTITY, "quantity")
    existing = RentCharge.objects.filter(lease=lease, period_month=month, seq=1).first()
    text = ""
    if existing is not None:
        refusal = rent_charge_refusal(existing)
        if refusal:
            raise _line_refusal(index, refusal, "rent_month", **words)
        default = Decimal(existing.amount_due)
        text = existing.proration_text or ""
    else:
        due = rent_run.debit_date(lease, month, lease.debit_day or 1)
        rent = leases.rent_on(lease, due)
        if rent is None:
            raise _line_refusal(index, LINE_RENT_NO_RENT, "rent_month", **words)
        prorated = rent_run.first_month(lease, month, Decimal(rent.amount))
        default, text = prorated.amount, prorated.text
    unit = default if line.unit_amount is None else _unit_amount(index, line.unit_amount)
    if existing is not None and unit != default:
        amount = leases.format_amount(default)
        raise _line_refusal(index, LINE_RENT_AMOUNT, "unit_amount", amount=amount, **words)
    description = visible_text(line.description or "")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise _line_refusal(index, LINE_DESCRIPTION_TOO_LONG, "description")
    if not _guarded(description):
        raise _line_refusal(index, LINE_PAYMENT_DATA, "description")
    proration = text if unit == default else ""
    return CleanLine(
        item=RENT_ITEM.format(month=month_label(month), store=lease.store.external_label),
        fee_type=None,
        description=description or proration,
        quantity=Decimal(1).quantize(QUANTITY_STEP),
        unit_amount=unit,
        line_total=unit,
        rent_lease=lease,
        rent_month=month,
        proration_text=proration,
    )


def _check_rent_lines(
    cleaned: Sequence[CleanLine], operator: Operator | None, draft: uuid.UUID | None
) -> None:
    """One Rent line per lease and month on the draft and on the operator's other open
    drafts; an invoice of more than one Rent line has another line (PLAN-012b-b D4)."""
    rents = [(index, line) for index, line in enumerate(cleaned, start=1) if line.is_rent]
    if not rents:
        return
    if len(rents) == len(cleaned) and len(rents) > 1:
        raise _Refused(Outcome(False, RENT_ALONE))
    seen: set[tuple[uuid.UUID, date]] = set()
    others = (
        InvoiceDraftLine.objects.filter(
            draft__operator=operator,
            draft__status=InvoiceDraftStatus.DRAFT,
            rent_lease__isnull=False,
        )
        .exclude(draft_id=draft)
        .select_related("draft")
    )
    elsewhere = {(row.rent_lease_id, row.rent_month): row.draft for row in others}
    for index, line in rents:
        if line.rent_lease is None or line.rent_month is None:  # pragma: no cover (is_rent)
            continue
        key = (line.rent_lease.pk, line.rent_month)
        words: dict[str, object] = {
            "store": line.rent_lease.store.external_label,
            "month": month_label(line.rent_month),
        }
        if key in seen:
            raise _line_refusal(index, LINE_RENT_TWICE, "rent_month", **words)
        seen.add(key)
        other = elsewhere.get(key)
        if other is not None:
            raise _line_refusal(
                index, LINE_RENT_ON_DRAFT, "rent_month", draft=other.number, **words
            )


def charge_label(charge: OneOffCharge) -> str:
    """What an existing charge is called on a line: its description, else its fee
    type's name."""
    if charge.description:
        return visible_text(charge.description)
    return visible_text(charge.fee_type.name) if charge.fee_type is not None else "Charge"


def existing_label(charge: OneOffCharge) -> str:
    """``Existing open charge: Security deposit ($500.00)``, the Item box's text."""
    return EXISTING_LABEL.format(
        charge=charge_label(charge), balance=leases.format_amount(Decimal(charge.balance))
    )


def existing_charge_closed(charge: OneOffCharge) -> bool:
    """Whether ``charge`` can no longer be billed by an existing-charge line (PLAN-012d
    D10), being: not owed or anything paid; a legacy row, an installment, a companion fee; a
    pending payment, a live invoice (a local one not yet at the provider included), a
    provider schedule, a Charge now attempt or an installment plan of its own (one
    query each, local data only)."""
    return (
        charge.status != OneOffChargeStatus.OWED
        or Decimal(charge.amount_paid) != ZERO
        or charge.legacy
        or charge.category == OneOffCategory.INSTALLMENT
        or charge.plan_id is not None
        or charge.companion_of_id is not None
        or charge.charge_now_key is not None
        or one_off_charges.has_pending_payment(charge)
        or bool(one_off_charges.provider_schedule(charge))
        or charge.installment_plans.exists()
        or any(
            one_off_charges.invoice_is_live(invoice)
            for invoice in one_off_charges.invoices_of(charge)
        )
    )


def open_charges(lease: Lease | None) -> list[OneOffCharge]:
    """The open one-off charges of ``lease`` an existing-charge line may bill, in due
    order (PLAN-012d D10): e.g. the setup checklist's deposit. Empty without a lease."""
    if lease is None or lease.operator_id is None:
        return []
    rows = (
        OneOffCharge.objects.filter(
            lease=lease,
            operator_id=lease.operator_id,
            status=OneOffChargeStatus.OWED,
            amount_paid=ZERO,
            legacy=False,
            plan__isnull=True,
            companion_of__isnull=True,
        )
        .exclude(category=OneOffCategory.INSTALLMENT)
        .select_related("fee_type")
        .order_by("due_date", "created_at", "id")
    )
    return [charge for charge in rows if not existing_charge_closed(charge)]


def _clean_existing_line(
    index: int, line: LineInput, lease: Lease | None, operator: Operator | None
) -> CleanLine:
    """An existing-charge line checked (PLAN-012d D10; see the module docstring), with
    the charge's label as its item, or :class:`_Refused`."""
    if line.rent_lease is not None or line.rent_month is not None:
        raise _line_refusal(index, LINE_EXISTING_INVALID, "item")
    charge = (
        OneOffCharge.objects.select_related("fee_type").filter(pk=line.existing_charge).first()
        if isinstance(line.existing_charge, uuid.UUID)
        else None
    )
    if (
        charge is None
        or lease is None
        or operator is None
        or charge.lease_id != lease.pk
        or charge.operator_id != operator.pk
    ):
        raise _line_refusal(index, LINE_EXISTING_OTHER, "item")
    label = charge_label(charge)
    if existing_charge_closed(charge):
        raise _line_refusal(index, LINE_EXISTING_NOT_OPEN, "item", charge=label)
    if line.quantity is not None and line.quantity != 1:
        raise _line_refusal(index, LINE_EXISTING_QUANTITY, "quantity")
    balance = Decimal(charge.balance)
    unit = balance if line.unit_amount is None else _unit_amount(index, line.unit_amount)
    if unit != balance:
        raise _line_refusal(
            index,
            LINE_EXISTING_AMOUNT if unit > balance else LINE_EXISTING_BELOW,
            "unit_amount",
            charge=label,
            balance=leases.format_amount(balance),
        )
    description = visible_text(line.description or "")
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise _line_refusal(index, LINE_DESCRIPTION_TOO_LONG, "description")
    if not _guarded(description):
        raise _line_refusal(index, LINE_PAYMENT_DATA, "description")
    return CleanLine(
        item=label[:NAME_MAX_LENGTH],
        fee_type=None,
        description=description,
        quantity=Decimal(1).quantize(QUANTITY_STEP),
        unit_amount=unit,
        line_total=unit,
        existing_charge=charge,
    )


def _check_existing_lines(
    cleaned: Sequence[CleanLine], operator: Operator | None, draft: uuid.UUID | None
) -> None:
    """One line per existing charge on the draft and on the operator's other open drafts
    (PLAN-012d D10)."""
    existing = [(index, line) for index, line in enumerate(cleaned, start=1) if line.is_existing]
    if not existing:
        return
    others = (
        InvoiceDraftLine.objects.filter(
            draft__operator=operator,
            draft__status=InvoiceDraftStatus.DRAFT,
            existing_charge__isnull=False,
        )
        .exclude(draft_id=draft)
        .select_related("draft")
    )
    elsewhere = {row.existing_charge_id: row.draft for row in others}
    seen: set[uuid.UUID] = set()
    for index, line in existing:
        if line.existing_charge is None:  # pragma: no cover (is_existing)
            continue
        key = line.existing_charge.pk
        if key in seen:
            raise _line_refusal(index, LINE_EXISTING_TWICE, "item", charge=line.item)
        seen.add(key)
        other = elsewhere.get(key)
        if other is not None:
            raise _line_refusal(
                index, LINE_EXISTING_ON_DRAFT, "item", charge=line.item, draft=other.number
            )


def rent_amount(lease: Lease, month: date) -> tuple[Decimal | None, str, str]:
    """The default amount of a Rent line of ``lease`` and ``month`` (PLAN-012d D10): the
    rent run's charge's, else :func:`ledger.services.rent_run.first_month`'s (prorated
    for a partial first month), with the proration text; ``(None, "", why)`` when the
    month cannot be billed by invoice. Reads local data only."""
    try:
        line = _clean_rent_line(
            1,
            LineInput("", "", None, None, rent_lease=lease.pk, rent_month=month),
            lease.operator,
        )
    except _Refused as error:
        return None, "", error.outcome.message.removeprefix(LINE_PREFIX.format(line=1))
    return line.unit_amount, line.proration_text, ""


def clean_lines(
    lines: Sequence[LineInput],
    *,
    lease: Lease | None,
    operator: Operator | None = None,
    draft: uuid.UUID | None = None,
) -> list[CleanLine]:
    """Every line checked, totalled and resolved to its catalogue item, in order;
    :class:`_Refused` at the first rule one breaks. A typed item that another line of the
    draft saves to the catalogue is saved with it (it will name the same item). On a
    lease, a catalogue item billed as a setup charge appears once (a lease has one charge
    per fee type), a saved item included. A Rent line (a lease or a month given) is
    checked against ``operator`` and the operator's open drafts other than ``draft``
    (PLAN-012b-b D4)."""
    if not lines:
        raise _Refused(Outcome(False, NO_LINES))
    if len(lines) > MAX_LINES:
        raise _Refused(Outcome(False, TOO_MANY_LINES))
    offered = offered_fee_types()
    offered_ids = {fee_type.pk for fee_type in offered}
    held = {
        catalogue_key(fee_type.name)
        for fee_type in FeeType.objects.exclude(pk__in=offered_ids).only("name")
    }
    by_name = _offered_by_name(offered)
    cleaned = [
        _clean_existing_line(index, line, lease, operator)
        if line.existing_charge is not None
        else _clean_rent_line(index, line, operator)
        if line.rent_lease is not None or line.rent_month is not None
        else _clean_line(index, line, by_name, held)
        for index, line in enumerate(lines, start=1)
    ]
    saved = {catalogue_key(line.item) for line in cleaned if line.save_item}
    cleaned = [
        replace(line, save_item=True)
        if line.fee_type is None and catalogue_key(line.item) in saved
        else line
        for line in cleaned
    ]
    if lease is not None:
        seen: set[object] = set()
        for index, line in enumerate(cleaned, start=1):
            if line.save_item:
                key: object = catalogue_key(line.item)
            elif line.fee_type is not None and category_for(line.fee_type) == OneOffCategory.SETUP:
                key = line.fee_type.pk
            elif line.existing_charge is not None and line.existing_charge.fee_type_id:
                # The lease's own charge of that fee type (PLAN-012d D10).
                key = line.existing_charge.fee_type_id
            else:
                continue
            if key in seen:
                raise _Refused(
                    Outcome(
                        False,
                        LINE_SAME_FEE_TYPE.format(line=index, name=line.item),
                        f"lines-{index - 1}-item",
                    )
                )
            seen.add(key)
    _check_rent_lines(cleaned, operator, draft)
    _check_existing_lines(cleaned, operator, draft)
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
        with transaction.atomic():
            Operator.objects.select_for_update().get(pk=operator.pk)
            cleaned = clean_lines(lines, lease=lease, operator=operator)
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
            _write_lines(draft, _save_catalogue_items(draft, cleaned, actor))
            _audit(
                actor,
                CREATED_ACTION,
                draft,
                None,
                {
                    **draft_state(draft),
                    "creation_key": str(creation_key) if creation_key else None,
                },
            )
    except _Refused as error:
        return error.outcome
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
    try:
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
            # A saved catalogue item is created, and a Rent line checked against the
            # operator's other drafts, under the operator's row lock (D4; PLAN-012b-b D4).
            Operator.objects.select_for_update().get(pk=draft.operator_id)
            cleaned = clean_lines(lines, lease=lease, operator=draft.operator, draft=draft.pk)
            before = draft_state(draft)
            draft.lease = lease
            draft.memo = text
            draft.due_date = due_date
            draft.save(update_fields=["lease", "memo", "due_date", "updated_at"])
            draft.lines.all().delete()
            _write_lines(draft, _save_catalogue_items(draft, cleaned, actor))
            after = draft_state(draft)
            if after != before:
                _audit(actor, UPDATED_ACTION, draft, before, after)
    except _Refused as error:
        return error.outcome
    return Outcome(True, SAVED, draft=draft)


def _is_offered(fee_type: FeeType) -> bool:
    return fee_type.active and fee_type.code not in NSF_FEE_CODES


def _named(name: str) -> FeeType | None:
    """The fee type whose name is ``name`` (case and surrounding spaces ignored): the
    first offered one in catalogue order, else the first one that is not offered, else
    ``None``."""
    key = catalogue_key(name)
    matches = [
        fee_type
        for fee_type in FeeType.objects.order_by("sort_order", "name", "id")
        if catalogue_key(fee_type.name) == key
    ]
    offered = [fee_type for fee_type in matches if _is_offered(fee_type)]
    found = offered or matches
    return found[0] if found else None


def _free_code(name: str) -> str:
    """A code no fee type has: the name's ASCII slug (:data:`CODE_FALLBACK` when it has
    none), then ``-2``, ``-3`` ... on a collision, cut to :data:`CODE_MAX_LENGTH`."""
    base = slugify(name)[:CODE_MAX_LENGTH].strip("-_") or CODE_FALLBACK
    taken = {*FeeType.objects.values_list("code", flat=True), *RESERVED_CODES}
    if base not in taken:
        return base
    suffix = 2
    while True:
        tail = f"-{suffix}"
        code = base[: CODE_MAX_LENGTH - len(tail)].rstrip("-_") + tail
        if code not in taken:
            return code
        suffix += 1


def _catalogue_item(
    index: int, line: CleanLine, draft: InvoiceDraft, actor: User | None
) -> FeeType:
    """The catalogue item ``line`` saves (D4): the offered one with its name if one
    appeared since the line was checked, else a new one, audited. :class:`_Refused` when
    a fee type that is not offered holds the name."""
    for _ in range(SAVE_ATTEMPTS):
        found = _named(line.item)
        if found is not None:
            if _is_offered(found):
                return found
            raise _Refused(
                Outcome(
                    False,
                    LINE_SAVE_NOT_OFFERED.format(line=index),
                    f"lines-{index - 1}-save_item",
                )
            )
        highest = FeeType.objects.aggregate(highest=Max("sort_order"))["highest"]
        try:
            with transaction.atomic():
                fee_type = FeeType.objects.create(
                    code=_free_code(line.item),
                    name=line.item,
                    default_cost=None,
                    default_price=line.unit_amount,
                    refundable=False,
                    active=True,
                    # Not a setup checklist row until an admin says so (PLAN-012d D9).
                    show_in_setup=False,
                    sort_order=(highest or 0) + 1,
                )
        except IntegrityError:
            # A concurrent save took the code: look again.
            continue
        AuditLog.objects.create(
            actor=actor,
            action=FEE_TYPE_CREATED_ACTION,
            entity_type=ENTITY_FEE_TYPE,
            entity_id=str(fee_type.pk),
            before=None,
            after={
                "code": fee_type.code,
                "name": fee_type.name,
                "default_cost": None,
                "default_price": str(fee_type.default_price),
                "refundable": fee_type.refundable,
                "active": fee_type.active,
                "show_in_setup": fee_type.show_in_setup,
                "sort_order": fee_type.sort_order,
                "draft": draft.number,
                "draft_id": str(draft.pk),
            },
        )
        return fee_type
    raise _Refused(Outcome(False, LINE_SAVE_BUSY.format(line=index), f"lines-{index - 1}-item"))


def _save_catalogue_items(
    draft: InvoiceDraft, lines: Sequence[CleanLine], actor: User | None
) -> list[CleanLine]:
    """The lines with every saved item in the catalogue and named (D4): one catalogue
    item per name, however many lines save it. Runs inside the caller's transaction,
    under the operator's row lock."""
    made: dict[str, FeeType] = {}
    linked = []
    for index, line in enumerate(lines, start=1):
        if not line.save_item:
            linked.append(line)
            continue
        key = catalogue_key(line.item)
        if key not in made:
            made[key] = _catalogue_item(index, line, draft, actor)
        fee_type = made[key]
        linked.append(replace(line, fee_type=fee_type, item=visible_text(fee_type.name)))
    return linked


def _write_lines(draft: InvoiceDraft, lines: Sequence[CleanLine]) -> None:
    InvoiceDraftLine.objects.bulk_create(
        [
            InvoiceDraftLine(
                draft=draft,
                position=position,
                fee_type=line.fee_type,
                item=line.item,
                description=line.description,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                line_total=line.line_total,
                rent_lease=line.rent_lease,
                rent_month=line.rent_month,
                existing_charge=line.existing_charge,
            )
            for position, line in enumerate(lines, start=1)
        ]
    )


# --- Send and Charge now --------------------------------------------------------------


def charge_description(line: InvoiceDraftLine | CleanLine) -> str:
    """The charge's description: the line's item, followed by the quantity and the unit
    amount when the quantity is not 1, cut to fit
    :data:`~ledger.models.DESCRIPTION_MAX_LENGTH`."""
    quantity = Decimal(line.quantity)
    suffix = (
        "" if quantity == 1 else f" ({format_quantity(quantity)} × {Decimal(line.unit_amount)})"
    )
    return line.item[: DESCRIPTION_MAX_LENGTH - len(suffix)].rstrip() + suffix


def charge_note(line: CleanLine, position: int, number: str, draft: InvoiceDraft) -> str:
    """The internal note of a line's charge: :data:`LINE_NOTE`, then the line's detail
    when it has one (D3)."""
    note = LINE_NOTE.format(line=position, number=number, draft=draft.number)
    if line.description:
        note += LINE_NOTE_DETAIL.format(detail=line.description)
    return note


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
                    LineInput(
                        line.item,
                        line.description,
                        line.quantity,
                        line.unit_amount,
                        rent_lease=line.rent_lease_id,
                        rent_month=line.rent_month,
                        existing_charge=line.existing_charge_id,
                    )
                    for line in stored
                ],
                lease=lease,
                operator=operator,
                draft=draft.pk,
            )
        except _Refused as error:
            return Outcome(False, error.outcome.message, error.outcome.field, draft=draft)
        total = total_of(lines)
        if not leases.customer_ref(operator):
            return Outcome(False, NO_PROVIDER_CUSTOMER, draft=draft)
        large = leases.is_large_debit(total)
        rent_alone = all(line.is_rent for line in lines)
        if collection == CollectionMethod.CHARGE_NOW:
            if rent_alone:
                return Outcome(False, RENT_ALONE_CHARGE_NOW, draft=draft)
            refusal = _charge_now_refusal(operator, total)
            if refusal:
                return Outcome(False, refusal, draft=draft)
            if large and not confirmed:
                return Outcome(
                    False, leases.large_debit_confirmation([total]), "confirm", draft=draft
                )
        due = due_date_for(draft, day)
        number = numbering.next_invoice_number(day.year)
        charges: list[OneOffCharge | RentCharge] = []
        attached: list[tuple[OneOffCharge, dict[str, Any]]] = []
        for position, line in enumerate(lines, start=1):
            if line.is_rent or line.is_existing:
                try:
                    if line.is_rent:
                        charges.append(_rent_charge(line, position, due, draft, actor))
                    else:
                        taken, before = _attach_existing(line, position, collection, lease)
                        charges.append(taken)
                        attached.append((taken, before))
                except _Refused as error:
                    transaction.set_rollback(True)
                    return Outcome(False, error.outcome.message, error.outcome.field, draft=draft)
                continue
            category = category_for(line.fee_type)
            item = ChargeInput(
                fee_type=line.fee_type if category == OneOffCategory.SETUP else None,
                price=line.line_total,
                cost=None,
                vendor="",
                due_date=due,
                note=charge_note(line, position, number, draft),
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
        one_offs = [charge for charge in charges if isinstance(charge, OneOffCharge)]
        rents = [charge for charge in charges if isinstance(charge, RentCharge)]
        invoice = Invoice.objects.create(
            invoice_number=number,
            operator=operator,
            # The first one-off line's charge; an invoice of one rent line alone is its
            # rent charge's (PLAN-012b-b D5).
            one_off_charge=one_offs[0] if one_offs else None,
            rent_charge=rents[0] if not one_offs else None,
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
                    description=line.item,
                    details=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_amount,
                    line_total=line.line_total,
                    one_off_charge=charge if isinstance(charge, OneOffCharge) else None,
                    rent_charge=charge if isinstance(charge, RentCharge) else None,
                    kind=InvoiceLineKind.CHARGE,
                )
                for position, (line, charge) in enumerate(zip(lines, charges, strict=True), 1)
            ]
        )
        for rent in rents:
            _billed_by(rent, invoice, actor)
        for taken, before in attached:
            _attached(taken, before, invoice, actor)
        params: dict[str, object] | None = None
        if not one_offs:
            # One rent line alone: the provider invoice is its rent charge's.
            job = jobs.enqueue(JobKind.INVOICE_CREATE, rents[0], params, actor)
            _sent(draft, invoice, actor, number, collection, charges, due)
            return Outcome(True, SENT.format(number=number), draft=draft, invoice=invoice, job=job)
        first = one_offs[0]
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
        _sent(draft, invoice, actor, number, collection, charges, due)
    template = CHARGE_REQUESTED if collection == CollectionMethod.CHARGE_NOW else SENT
    return Outcome(True, template.format(number=number), draft=draft, invoice=invoice, job=job)


def _sent(
    draft: InvoiceDraft,
    invoice: Invoice,
    actor: User | None,
    number: str,
    collection: CollectionMethod,
    charges: Sequence[OneOffCharge | RentCharge],
    due: date,
) -> None:
    """The draft becomes ``sent`` with its invoice, audited with every line's charge."""
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
            "charges": [str(row.pk) for row in charges if isinstance(row, OneOffCharge)],
            "rent_charges": [str(row.pk) for row in charges if isinstance(row, RentCharge)],
            "due_date": due.isoformat(),
        },
    )


def _rent_charge(
    line: CleanLine, position: int, due: date, draft: InvoiceDraft, actor: User | None
) -> RentCharge:
    """The month's rent charge of a Rent line on send (PLAN-012b-b D5): made by
    :func:`ledger.services.rent_run.create_month` (due ``due``, the line's amount and
    proration text), or the existing one taken, checked again under its row lock (an
    unpaid charge with nothing live, at the line's amount); :class:`_Refused` otherwise.
    Runs in the caller's transaction."""
    lease, month = line.rent_lease, line.rent_month
    if lease is None or month is None:  # pragma: no cover (only Rent lines come here)
        raise ValueError("Not a Rent line.")
    made, created = rent_run.create_month(
        lease,
        month,
        actor,
        due_on=due,
        amount=line.line_total,
        proration_text=line.proration_text,
        context={"invoice_draft": str(draft.pk), "line": position},
    )
    charge = RentCharge.objects.select_for_update().get(pk=made.pk)
    if created:
        return charge
    words: dict[str, object] = {"store": lease.store.external_label, "month": month_label(month)}
    refusal = rent_charge_refusal(charge)
    if refusal:
        raise _line_refusal(position, refusal, "rent_month", **words)
    if charge.amount_due != line.line_total:
        amount = leases.format_amount(Decimal(charge.amount_due))
        raise _line_refusal(position, LINE_RENT_AMOUNT, "unit_amount", amount=amount, **words)
    return charge


def _billed_by(charge: RentCharge, invoice: Invoice, actor: User | None) -> None:
    """Name the invoice on the rent charge (PLAN-012b-b D5, D6), audited."""
    before = {"billed_by_invoice": str(charge.billed_by_invoice_id or "") or None}
    charge.billed_by_invoice = invoice
    charge.save(update_fields=["billed_by_invoice", "updated_at"])
    AuditLog.objects.create(
        actor=actor,
        action="rent_charge.billed_by_invoice",
        entity_type=rent_run.ENTITY_CHARGE,
        entity_id=str(charge.pk),
        before=before,
        after={
            **rent_run.charge_state(charge),
            "billed_by_invoice": str(invoice.pk),
            "invoice_number": invoice.invoice_number,
        },
    )


def _attach_existing(
    line: CleanLine, position: int, collection: CollectionMethod, lease: Lease | None
) -> tuple[OneOffCharge, dict[str, Any]]:
    """The charge of an existing-charge line on send (PLAN-012d D10), under its row lock
    and checked again (open, of the draft's lease and operator, the line's amount still
    its balance, else :data:`LINE_EXISTING_CHANGED`): its collection becomes the invoice's,
    its price unchanged (D10 amended); with its state before, for :func:`_attached`.
    :class:`_Refused` otherwise. Runs in the caller's transaction."""
    if line.existing_charge is None:  # pragma: no cover (only existing lines come here)
        raise ValueError("Not an existing-charge line.")
    charge = (
        OneOffCharge.objects.select_for_update(of=("self",))
        .select_related("fee_type")
        .get(pk=line.existing_charge.pk)
    )
    label = charge_label(charge)
    if lease is None or charge.lease_id != lease.pk or charge.operator_id != lease.operator_id:
        raise _line_refusal(position, LINE_EXISTING_OTHER, "item")
    if existing_charge_closed(charge):
        raise _line_refusal(position, LINE_EXISTING_NOT_OPEN, "item", charge=label)
    balance = Decimal(charge.balance)
    if line.line_total != balance:
        raise _line_refusal(
            position,
            LINE_EXISTING_CHANGED,
            "unit_amount",
            charge=label,
            balance=leases.format_amount(balance),
        )
    before = one_off_charges.charge_state(charge)
    charge.collection = collection
    charge.save(update_fields=["collection", "updated_at"])
    charge.refresh_from_db()
    return charge, before


def _attached(
    charge: OneOffCharge, before: dict[str, Any], invoice: Invoice, actor: User | None
) -> None:
    """Name the invoice that now bills an existing charge (PLAN-012d D10), audited."""
    AuditLog.objects.create(
        actor=actor,
        action=ATTACHED_ACTION,
        entity_type=one_off_charges.ENTITY_ONE_OFF_CHARGE,
        entity_id=str(charge.pk),
        before=before,
        after={
            **one_off_charges.charge_state(charge),
            "invoice": str(invoice.pk),
            "invoice_number": invoice.invoice_number,
        },
    )


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
    if first is None:
        # A sent draft has its invoice's first charge, unless it bills rent alone.
        message = RENT_ALONE_CANCEL if sent_rent_charges(draft) else NOT_A_DRAFT
        return Outcome(False, message, draft=draft)
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


def delete_draft(draft_id: uuid.UUID, actor: User | None) -> Outcome:
    """Delete a never-sent draft outright (billing; PLAN-012d D3): its lines go with it,
    audited ``invoice_draft.deleted`` with the draft as it was. A sent invoice keeps its
    cancel form (:data:`DELETE_SENT`); a cancelled draft is left as it is
    (:data:`ALREADY_CANCELLED`); the draft a transfer made is cancelled instead
    (:data:`DELETE_TRANSFER`). Nothing reaches the payment provider."""
    if not _is_billing(actor):
        return Outcome(False, BILLING_ONLY)
    with transaction.atomic():
        draft = InvoiceDraft.objects.select_for_update().filter(pk=draft_id).first()
        if draft is None:
            return Outcome(False, NOT_A_DRAFT)
        if draft.status == InvoiceDraftStatus.SENT:
            return Outcome(False, DELETE_SENT, draft=draft)
        if draft.status != InvoiceDraftStatus.DRAFT:
            return Outcome(False, ALREADY_CANCELLED, draft=draft)
        if LeaseTransfer.objects.filter(draft=draft).exists():
            return Outcome(False, DELETE_TRANSFER, draft=draft)
        number = draft.number
        _audit(actor, DELETED_ACTION, draft, draft_state(draft), None)
        draft.delete()
    return Outcome(True, DELETED.format(number=number))


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
                "item": line.item,
                "fee_type": line.fee_type.code if line.fee_type is not None else None,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_amount": str(line.unit_amount),
                "line_total": str(line.line_total),
                **(
                    {
                        "rent_lease": str(line.rent_lease_id),
                        "rent_month": line.rent_month.isoformat(),
                    }
                    if line.rent_lease_id is not None and line.rent_month is not None
                    else {}
                ),
                **(
                    {"existing_charge": str(line.existing_charge_id)}
                    if line.existing_charge_id is not None
                    else {}
                ),
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
