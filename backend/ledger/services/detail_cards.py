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

:func:`collection_state` says how much of a charge is collected (PLAN-014 B3) and
:func:`lease_collections` gives the store workspace's LEASE card its Sign fee and Insurance rows
and the note about last year's insurance. :func:`lease_account` gives the LEASE card its billing
lines and :func:`lease_accounts` the operator page's PAYMENT card one line per lease (PLAN-014
C3).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.urls import reverse

from finance.dates import day_text
from ledger.models import (
    ZERO,
    AuditLog,
    CollectionStatus,
    DepositResolution,
    InsuranceCharge,
    Invoice,
    InvoiceDraft,
    Lease,
    OneOffCategory,
    OneOffCharge,
    OneOffChargeStatus,
    Operator,
    RentCharge,
)
from ledger.services import audit, installments, insurance, one_off_charges, receivables, rent_run
from ledger.wording import format_amount
from payments import lease_sync

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
#: A source that names what the charge is, read "created <date> as ..." (not "from").
SOURCE_AS = "as "
#: A rent charge's bank debit from Add payment (PLAN-012f-b D19).
SOURCE_BANK_DEBIT = SOURCE_AS + "a bank debit of {rent}"


@dataclass(frozen=True, slots=True)
class ActivityRow:
    at: datetime
    action: str
    #: Who acted (``None``: the system); the card prints it with the ``staff`` filter
    #: (PLAN-012f-c D11).
    user: User | None
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
                user=row.actor,
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
        return SOURCE_BANK_DEBIT.format(
            rent=installments.parent_screen_label(charge.source_rent_charge)
        )
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
        # A source that says what the charge is ("as a bank debit of ...") reads without
        # "from" (PLAN-012f-b D19).
        joined = source if source.startswith(SOURCE_AS) else f"from {source}"
        made = f"created {day_text(created)}" + (f" {joined}" if source else "")
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


# --- The LEASE card's collection rows (PLAN-014 B3) ------------------------------------------

#: The catalogue code of the sign fee (ledger migration 0013).
SIGN_FEE_CODE = "sign_fee"
COLLECTED = "collected"
PARTIAL = "partial"
UNCOLLECTED = "uncollected"
UNBILLED = "unbilled"
#: The colour of each state (the card's ``side-<tone>`` class): green, orange, red, grey.
TONES: dict[str, str] = {
    COLLECTED: "paid",
    PARTIAL: "partial",
    UNCOLLECTED: "unpaid",
    UNBILLED: "muted",
}
#: A one-off charge in one of these states is not billed any more.
_NOT_BILLED = (OneOffChargeStatus.WAIVED, OneOffChargeStatus.VOIDED)


@dataclass(frozen=True, slots=True)
class CollectionState:
    """How much of one charge is collected: ``code`` one of ``collected``, ``partial``,
    ``uncollected``, ``unbilled``; ``text`` as the card reads it; ``tone`` its colour; what
    was paid and what is due (``None``: an insurance charge still waiting for its premium);
    ``url`` the charge's page ("" without a charge, or for an insurance charge, which has
    none)."""

    code: str
    text: str
    tone: str
    paid: Decimal
    due: Decimal | None
    url: str


def collection_state(
    charge: OneOffCharge | InsuranceCharge | None, *, balance: Decimal | None = None
) -> CollectionState:
    """The state of ``charge``: ``✓ collected`` (nothing owed, something paid),
    ``$x of $y · partly collected``, ``not collected`` (nothing paid) or ``not billed`` (no
    charge). ``balance`` is an insurance charge's balance when the caller read it already
    (:func:`ledger.services.insurance.balances`); a one-off charge carries its own."""
    if charge is None:
        return CollectionState(UNBILLED, "not billed", TONES[UNBILLED], ZERO, None, "")
    if isinstance(charge, OneOffCharge):
        due: Decimal | None = charge.price
        owed = charge.balance
        url = reverse("ledger:one_off_charges:detail", kwargs={"pk": charge.pk})
    else:
        due = charge.amount
        owed = balance if balance is not None else insurance.balance(charge)
        url = ""
    paid = max((due or ZERO) - owed, ZERO)
    if due is not None and owed <= 0 and paid > 0:
        code, text = COLLECTED, "✓ collected"
    elif due is not None and 0 < paid < due:
        code = PARTIAL
        text = f"{format_amount(paid)} of {format_amount(due)} · partly collected"
    else:
        code, text = UNCOLLECTED, "not collected"
    return CollectionState(code, text, TONES[code], paid, due, url)


def policy_label(year: int) -> str:
    """``Insurance 2026–27``."""
    return f"Insurance {year}–{(year + 1) % 100:02d}"


@dataclass(frozen=True, slots=True)
class LeaseCollections:
    """The LEASE card's rows under Deposit: the sign fee, with "Bill now" while it is not
    billed (the invoice builder with the sign fee preselected); the insurance of the current
    policy year (``insurance_label`` "" when the store has no open date) and the note about
    the previous year ("" when it was collected, or the lease did not hold the store then)."""

    sign_fee: CollectionState
    sign_fee_bill_url: str
    insurance_label: str = ""
    insurance: CollectionState | None = None
    insurance_note: str = ""


def _sign_fee_bill_url(lease: Lease) -> str:
    query = urlencode({"store": str(lease.store_id), "lease": str(lease.pk), "item": SIGN_FEE_CODE})
    return f"{reverse('ledger:invoices:new_store', kwargs={'pk': lease.store_id})}?{query}"


def _previous_year_note(
    lease: Lease, year: int, charge: InsuranceCharge | None, owed: Decimal | None, today: date
) -> str:
    label = policy_label(year)
    if charge is None:
        return f"{label} never billed" if insurance.in_scope(lease, year, today) else ""
    if collection_state(charge, balance=owed).code not in (UNCOLLECTED, PARTIAL):
        return ""
    premium = f" ({format_amount(charge.amount)})" if charge.amount is not None else ""
    return f"{label}{premium} not collected"


def lease_collections(lease: Lease, today: date) -> LeaseCollections:
    """The LEASE card's collection rows of ``lease`` (whose store the caller read with it) in
    three queries, four or five with insurance charges (their balances,
    :func:`ledger.services.insurance.balances`)."""
    sign_fee = (
        OneOffCharge.objects.filter(lease=lease, fee_type__code=SIGN_FEE_CODE)
        .exclude(status__in=_NOT_BILLED)
        .order_by("-created_at", "-id")
        .first()
    )
    sign_state = collection_state(sign_fee)
    found = LeaseCollections(
        sign_fee=sign_state,
        sign_fee_bill_url=_sign_fee_bill_url(lease) if sign_state.code == UNBILLED else "",
    )
    store = lease.store
    if store.open_date is None:
        return found
    year = insurance.policy_year(store.open_date, today)
    charges = {
        charge.policy_year: charge
        for charge in InsuranceCharge.objects.filter(lease=lease, policy_year__in=(year, year - 1))
    }
    owed = insurance.balances(list(charges.values()))
    current, previous = charges.get(year), charges.get(year - 1)
    return replace(
        found,
        insurance_label=policy_label(year),
        insurance=collection_state(current, balance=owed.get(current.pk) if current else None),
        insurance_note=(
            _previous_year_note(
                lease, year - 1, previous, owed.get(previous.pk) if previous else None, today
            )
            if year - 1 >= store.open_date.year
            else ""
        ),
    )


# --- Billing details per lease (PLAN-014 C3) ------------------------------------------

#: The LEASE and PAYMENT cards' bank line when the lease's account has no linked method.
NO_BANK_ON_FILE = "no bank account on file"


@dataclass(frozen=True, slots=True)
class LeaseAccount:
    """A lease's billing line for the LEASE card and the operator page's PAYMENT card: the
    company billed, the billing e-mail, the bank line ("Bank ····4821" or
    :data:`NO_BANK_ON_FILE`, from the cached last four of the account's lease) and the
    lease whose account it shares, if any."""

    lease: Lease
    company: str
    email: str
    bank: str
    shares: Lease | None

    @property
    def has_bank(self) -> bool:
        """Whether the bank line names a method (the LEASE card's "Replace", PLAN-014 C4)."""
        return self.bank != NO_BANK_ON_FILE

    @property
    def shares_label(self) -> str:
        """The LEASE card's link text for the store whose account this lease shares
        (PLAN-014 I-b): "#1999 fatih", or the store's label alone without a number; "" when
        the lease uses its own account."""
        if self.shares is None:
            return ""
        store = self.shares.store
        if store.store_number is None:
            return store.external_label
        return f"#{store.store_number} {store.external_label}"

    @property
    def switchable(self) -> bool:
        """Whether the LEASE card offers "Switch to own account" (PLAN-014 C9): the lease
        shares another lease's account and holds a customer of its own (one query, only
        for a sharer and only when read)."""
        return self.shares is not None and bool(
            lease_sync.current_refs(self.lease).get(lease_sync.KIND_CUSTOMER)
        )


def bank_text(last4: str) -> str:
    """ "Bank ····4821", or :data:`NO_BANK_ON_FILE` without four digits."""
    return f"Bank ····{last4}" if last4 else NO_BANK_ON_FILE


def lease_account(lease: Lease) -> LeaseAccount:
    """The LEASE card's billing lines of ``lease`` (at most two queries; none with the
    billing rows selected)."""
    billing = lease_sync.billing_of(lease)
    shares = billing.shares_account_of
    owner = lease_sync.billing_of(shares) if shares is not None else billing
    return LeaseAccount(
        lease=lease,
        company=billing.company_name,
        email=billing.email,
        bank=bank_text(owner.bank_last4),
        shares=shares,
    )


def lease_accounts(operator: Operator) -> list[LeaseAccount]:
    """The PAYMENT card's line per lease of the operator not ended, by store (one query)."""
    rows = (
        Lease.objects.filter(operator=operator, ended_on__isnull=True)
        .exclude(collection_status=CollectionStatus.ENDED)
        .select_related(
            "store", "billing", "billing__shares_account_of", "billing__shares_account_of__billing"
        )
        .order_by(F("store__store_number").asc(nulls_last=True), "store__external_label", "id")
    )
    return [lease_account(lease) for lease in rows]
