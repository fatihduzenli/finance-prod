"""Annual insurance charges (docs/inputs/OWNER_ANSWERS_ROUND2.md, ADR-0005; PLAN-007 D9).

The owner's algorithm, keyed to the lease (ADR-0005 point 4):

- The **anniversary** of year Y is the store's open date in year Y; 29 February falls
  on 28 February in a year that is not a leap year.
- Year Y is **in scope** when its anniversary is on or before today and, if the store
  has closed, before ``closed_at``.
- The lease that **holds the store on the anniversary** owes that year: its start date
  (when known) is on or before the anniversary and its end date (when known) is on or
  after it. Prospective leases owe nothing yet.
- No charge while the store ``needs_open_date``. A year without a premium gets a charge
  with ``amount = NULL`` and ``needs_premium``; it is never debited.

Generation is idempotent on ``(lease, policy_year)`` (a unique constraint plus
``get_or_create``). :func:`insurance_due_soon` only reads: it lists the anniversaries
of the coming days for the run screen, with the charge when one exists. Insurance
autopay schedules are a later plan (7c). :func:`bill_now` (PLAN-014 I-b, the LEASE card's
"Bill now") creates one lease's charge of the current policy year by the same rule, audited
with the staff member who pressed it.

**Manual payments** (:func:`record_manual_payment`, PLAN-012d D6a): the rent rules
(method, amount, date, note and the form's creation key;
:func:`ledger.services.rent_run.payment_refusal`), kind ``insurance`` with the charge's
policy year, at most the charge's balance (:func:`balance`, as
:mod:`ledger.services.receivables` reads it), written under the charge's row lock with
the key looked up there, audited ``insurance_charge.payment_recorded``; it closes the
open promises it keeps. The paid state of an insurance charge is derived from its
payments, so nothing else is recomputed. An insurance charge has no provider marking job
and no review text: when it has an invoice at the payment provider the payment makes no
provider call, the charge is flagged for review and ``insurance_charge.provider_not_updated``
says why (:data:`NOT_MARKED`); staff mark that invoice by hand.
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from ledger.models import (
    ZERO,
    AuditLog,
    CollectionStatus,
    InsuranceCharge,
    InsurancePremium,
    Invoice,
    Lease,
    Payment,
    PaymentKind,
    PaymentStatus,
)
from ledger.services import contacts, rent_run
from ledger.validators import visible_text
from ledger.wording import provider_sentence

ENTITY_INSURANCE = "insurance_charge"
PAYMENT_ACTION = "insurance_charge.payment_recorded"
NOT_UPDATED_ACTION = "insurance_charge.provider_not_updated"
#: Why a manual payment on an insurance charge with a provider invoice did not mark it.
NOT_MARKED = provider_sentence(
    "Not marked paid in {provider}: an insurance invoice is marked paid by hand"
)
PAY_NEEDS_PREMIUM = "This insurance charge has no premium yet. Nothing was recorded."
CREATED_ACTION = "insurance_charge.created"
#: :func:`bill_now`'s sentences (PLAN-014 I-b).
BILL_CREATED = "This policy year's insurance is billed."
BILL_NEEDS_PREMIUM = (
    "This policy year's insurance is billed; it waits for the year's premium to be entered."
)
BILL_NO_OPEN_DATE = (
    "The store has no open date, so its policy year is not known. Nothing was billed."
)
BILL_NOT_OWED = "This lease owes no insurance for that policy year. Nothing was billed."
BILL_ALREADY = "This policy year's insurance is already billed. Nothing was billed."
#: The run screen lists insurance due within this many days (owner's algorithm).
DUE_SOON_DAYS = 7


@dataclass(frozen=True, slots=True)
class InsuranceReport:
    created: int
    existing: int
    needs_premium: int
    #: Leases skipped because their store has no open date.
    needs_open_date: int


@dataclass(frozen=True, slots=True)
class InsuranceDue:
    lease: Lease
    policy_year: int
    due_on: date
    #: The year's premium, or ``None`` when it is not entered yet.
    amount: Decimal | None
    charge: InsuranceCharge | None


def anniversary(open_date: date, year: int) -> date:
    """The open date in ``year``; 29 February becomes 28 February in a common year."""
    if open_date.month == 2 and open_date.day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return open_date.replace(year=year)


def holds_on(lease: Lease, day: date) -> bool:
    """Whether the lease held the store on ``day`` (ADR-0005: that lease owes the year)."""
    if lease.started_on is not None and lease.started_on > day:
        return False
    return lease.ended_on is None or day <= lease.ended_on


def in_scope(lease: Lease, year: int, today: date) -> date | None:
    """The anniversary of ``year`` when the lease owes it as of ``today``, else ``None``."""
    store = lease.store
    if store.open_date is None:
        return None
    due = anniversary(store.open_date, year)
    if due > today or (store.closed_at is not None and due >= store.closed_at):
        return None
    return due if holds_on(lease, due) else None


def owes(lease: Lease, year: int, today: date) -> bool:
    """Whether ``lease`` owes ``year`` as of ``today`` (no query: the lease's store is read
    with it): in scope (:func:`in_scope`) and not prospective, the rule of
    :func:`generate_insurance_charges` and :func:`bill_now`."""
    return (
        lease.collection_status != CollectionStatus.PROSPECTIVE
        and in_scope(lease, year, today) is not None
    )


def _leases() -> list[Lease]:
    return list(
        Lease.objects.select_related("store")
        .exclude(collection_status=CollectionStatus.PROSPECTIVE)
        .order_by("store__external_label", "id")
    )


def _premiums() -> dict[int, Decimal]:
    return dict(InsurancePremium.objects.values_list("policy_year", "amount"))


def policy_year(open_date: date, today: date) -> int:
    """The policy year ``today`` is in: the latest year whose anniversary (the store's open
    date, :func:`anniversary`) is on or before ``today``."""
    year = today.year
    return year if anniversary(open_date, year) <= today else year - 1


def _create(
    lease: Lease, year: int, due: date, premium: Decimal | None, *, actor: User | None
) -> tuple[InsuranceCharge, bool]:
    """The charge of ``lease`` for ``year``, created (and audited
    ``insurance_charge.created``) when it does not exist yet; whether it was created."""
    with transaction.atomic():
        charge, made = InsuranceCharge.objects.get_or_create(
            lease=lease, policy_year=year, defaults={"due_on": due, "amount": premium}
        )
        if made:
            AuditLog.objects.create(
                actor=actor,
                action=CREATED_ACTION,
                entity_type=ENTITY_INSURANCE,
                entity_id=str(charge.pk),
                before=None,
                after={
                    "lease": str(lease.pk),
                    "policy_year": year,
                    "due_on": due.isoformat(),
                    "amount": str(charge.amount) if charge.amount is not None else None,
                    "needs_premium": charge.needs_premium,
                },
            )
    return charge, made


def generate_insurance_charges(today: date) -> InsuranceReport:
    """Create every in-scope charge that does not exist yet; return the counts."""
    premiums = _premiums()
    created = existing = needs_premium = needs_open_date = 0
    for lease in _leases():
        store = lease.store
        if store.needs_open_date or store.open_date is None:
            needs_open_date += 1
            continue
        for year in range(store.open_date.year, today.year + 1):
            due = in_scope(lease, year, today)
            if due is None:
                continue
            charge, made = _create(lease, year, due, premiums.get(year), actor=None)
            if made:
                created += 1
                needs_premium += charge.needs_premium
            else:
                existing += 1
    return InsuranceReport(created, existing, needs_premium, needs_open_date)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What :func:`bill_now` did: ``ok`` and the one sentence to show; the charge it
    created, if any."""

    ok: bool
    message: str
    charge: InsuranceCharge | None = None


def bill_now(lease: Lease, today: date, *, actor: User | None) -> Outcome:
    """The LEASE card's "Bill now" on the insurance row (PLAN-014 I-b): the charge of the
    current policy year of ``lease``, by the rule of :func:`generate_insurance_charges` (the
    store's open date, the lease not prospective and holding the store on the anniversary,
    before the store closed; the year's premium, or none and ``needs_premium``), audited
    with ``actor``. Refused with one sentence, writing nothing, when the store has no open
    date, the lease does not owe this year or the year is already billed (a second press)."""
    store = lease.store
    if store.needs_open_date or store.open_date is None:
        return Outcome(False, BILL_NO_OPEN_DATE)
    year = policy_year(store.open_date, today)
    due = in_scope(lease, year, today)
    if due is None or not owes(lease, year, today):
        return Outcome(False, BILL_NOT_OWED)
    premium = InsurancePremium.objects.filter(policy_year=year).values_list("amount", flat=True)
    charge, made = _create(lease, year, due, premium.first(), actor=actor)
    if not made:
        return Outcome(False, BILL_ALREADY)
    return Outcome(True, BILL_NEEDS_PREMIUM if charge.needs_premium else BILL_CREATED, charge)


def insurance_due_soon(today: date, days: int = DUE_SOON_DAYS) -> list[InsuranceDue]:
    """Anniversaries from ``today`` to ``today + days`` inclusive, by date. Read-only."""
    end = today + timedelta(days=days)
    premiums = _premiums()
    found: list[InsuranceDue] = []
    for lease in _leases():
        store = lease.store
        if store.open_date is None:
            continue
        for year in sorted({today.year, end.year}):
            if year < store.open_date.year:
                continue
            due = anniversary(store.open_date, year)
            if not today <= due <= end:
                continue
            if store.closed_at is not None and due >= store.closed_at:
                continue
            if not holds_on(lease, due):
                continue
            charge = lease.insurance_charges.filter(policy_year=year).first()
            found.append(InsuranceDue(lease, year, due, premiums.get(year), charge))
    return sorted(found, key=lambda item: (item.due_on, item.lease.store.external_label))


# --- Manual payments (PLAN-012d D6a) ----------------------------------------------------


def balance(charge: InsuranceCharge) -> Decimal:
    """What ``charge`` still owes, as :func:`ledger.services.receivables.open_balances`
    lists it: its amount less its settled payments and what its re-debits collected, plus
    its settled refunds, less what its live re-debits have still to collect."""
    return balances([charge])[charge.pk]


def balances(charges: Sequence[InsuranceCharge]) -> dict[uuid.UUID, Decimal]:
    """:func:`balance` of each of ``charges`` (by id), in two queries whatever their number
    (PLAN-014 B3: the LEASE card reads this year's and last year's)."""
    from ledger.services import receivables  # receivables imports one_off_charges, this

    ids = [charge.pk for charge in charges]
    if not ids:
        return {}
    paid: dict[uuid.UUID, Decimal] = {}
    refunded: dict[uuid.UUID, Decimal] = {}
    settled = (
        Payment.objects.filter(
            insurance_charge__in=ids, kind=PaymentKind.INSURANCE, status=PaymentStatus.SETTLED
        )
        .values("insurance_charge", "is_refund")
        .annotate(total=Sum("amount"))
        .order_by()
        .values_list("insurance_charge", "is_refund", "total")
    )
    for charge_id, is_refund, total in settled:
        (refunded if is_refund else paid)[charge_id] = Decimal(total or ZERO)
    carried, collected = receivables.insurance_redebits(ids)
    return {
        charge.pk: receivables.insurance_owed(
            charge.amount,
            paid=paid.get(charge.pk, ZERO),
            refunded=refunded.get(charge.pk, ZERO),
            collected=collected.get(charge.pk, ZERO),
            carried=carried.get(charge.pk, ZERO),
            legacy_status=charge.legacy_status,
        )
        for charge in charges
    }


def _provider_invoice(charge: InsuranceCharge) -> Invoice | None:
    """The charge's invoice with a current provider reference, if any."""
    from payments.models import ExternalRef

    for invoice in Invoice.objects.filter(insurance_charge=charge).order_by(
        "issue_date", "invoice_number"
    ):
        if ExternalRef.objects.filter(
            entity_type="invoice",
            entity_id=invoice.pk,
            external_kind="invoice",
            superseded_at__isnull=True,
        ).exists():
            return invoice
    return None


def _payment_for_key(charge: InsuranceCharge, creation_key: uuid.UUID) -> bool:
    return AuditLog.objects.filter(
        action=PAYMENT_ACTION,
        entity_type=ENTITY_INSURANCE,
        entity_id=str(charge.pk),
        after__creation_key=str(creation_key),
    ).exists()


def _state(charge: InsuranceCharge) -> dict[str, object]:
    return {
        "lease": str(charge.lease_id),
        "policy_year": charge.policy_year,
        "amount": str(charge.amount) if charge.amount is not None else None,
        "needs_review": charge.needs_review,
    }


def record_manual_payment(
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
    allocation_group: uuid.UUID | None = None,
    credit_id: uuid.UUID | None = None,
) -> rent_run.PaymentOutcome:
    """Record a Zelle, wire, check or other payment (or, with ``credit_id``, a credit applied)
    against an insurance charge (see the module docstring). Never calls the provider."""
    outcome = rent_run.PaymentOutcome
    text = visible_text(note or "")
    reference = (reference or "").strip()
    refused = rent_run.payment_refusal(
        method, amount, paid_on, text, today, credit=credit_id is not None
    )
    if refused is not None:
        return refused
    with transaction.atomic():
        charge = InsuranceCharge.objects.select_for_update().get(pk=charge_id)
        if creation_key is not None and _payment_for_key(charge, creation_key):
            return outcome(True, rent_run.PAY_ALREADY, replayed=True)
        if charge.amount is None:
            return outcome(False, PAY_NEEDS_PREMIUM)
        owed = balance(charge)
        if owed <= 0:
            return outcome(False, rent_run.PAY_NOTHING_DUE)
        if amount > owed:
            return outcome(False, rent_run.PAY_OVER_BALANCE, "amount")
        before = _state(charge)
        payment = Payment(
            insurance_charge=charge,
            kind=PaymentKind.INSURANCE,
            policy_year=charge.policy_year,
            method=method,
            amount=amount,
            status=PaymentStatus.SETTLED,
            initiated_on=paid_on,
            settled_on=paid_on,
            reference=reference,
            note=text,
            recorded_by=actor,
            allocation_group=allocation_group,
            credit_id=credit_id,
        )
        try:
            payment.save()
        except ValidationError:
            # The card-number guard: the transaction is rolled back with nothing saved.
            transaction.set_rollback(True)
            return outcome(False, rent_run.PAY_NOTE_PAYMENT_DATA, "note")
        contacts.close_kept_by_payment(payment)
        AuditLog.objects.create(
            actor=actor,
            action=PAYMENT_ACTION,
            entity_type=ENTITY_INSURANCE,
            entity_id=str(charge.pk),
            before=before,
            after={
                **_state(charge),
                "payment": str(payment.pk),
                "method": method,
                "amount": str(amount),
                "paid_on": paid_on.isoformat(),
                "balance": str(owed - amount),
                "creation_key": str(creation_key) if creation_key is not None else None,
            },
        )
        invoice = _provider_invoice(charge)
        if invoice is None:
            return outcome(
                True,
                rent_run.PAY_RECORDED,
                payment=payment,
                provider=rent_run.MarkResult(rent_run.MarkAction.NO_INVOICE),
            )
        before = _state(charge)
        if not charge.needs_review:
            charge.needs_review = True
            charge.save(update_fields=["needs_review", "updated_at"])
        AuditLog.objects.create(
            actor=actor,
            action=NOT_UPDATED_ACTION,
            entity_type=ENTITY_INSURANCE,
            entity_id=str(charge.pk),
            before=before,
            after={
                **_state(charge),
                "payment": str(payment.pk),
                "invoice_number": invoice.invoice_number,
                "reason": str(NOT_MARKED),
            },
        )
    return outcome(
        True,
        rent_run.PAY_RECORDED,
        payment=payment,
        provider=rent_run.MarkResult(rent_run.MarkAction.NOT_UPDATED, NOT_MARKED),
    )
